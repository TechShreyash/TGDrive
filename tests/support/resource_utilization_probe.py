"""Manual synthetic media measurements; no real Telegram, MongoDB or credentials."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import resource
import tempfile
import time
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock

from tests.media.test_pooled_media import account
from tests.media.test_telegram_cache import message
from tgdrive.media import media_reader
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_scheduler import MediaScheduler
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import TelegramMessage

BLOCK = media_reader.BLOCK


class DelayedIterator:
    def __init__(self, inner, delay, monitor):
        self.inner, self.delay, self.monitor = inner, delay, monitor

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(self.delay)
        data = await anext(self.inner)
        self.monitor["origin_bytes"] += len(data)
        return data

    async def close(self):
        await self.inner.close()


def source(client, identity, size):
    native = message(identity, size)
    native.id = identity
    return TelegramMessage(client, native)


async def run(args, directory):
    payload = b"x" * (32 * BLOCK)
    clients = [account(str(index), payload) for index in range(3)]
    monitor = Counter()
    for client in clients:
        client._media_scheduler = MediaScheduler(limit=8, rate=48)
        client.native.payloads = {identity: payload for identity in range(1, 5)}
        native_download = client.native.iter_download
        client.native.iter_download = lambda *a, original=native_download, **kw: (
            DelayedIterator(original(*a, **kw), 0.02, monitor)
        )
        client.get_messages = AsyncMock(
            side_effect=lambda _channel, message_ids, client=client: source(
                client, message_ids, len(payload)
            )
        )
    pool = ClientPool(clients, media_cache_dir=directory, media_per_bot=3)
    cache = pool._media_read_cache = media_reader.MediaReadCache(
        directory,
        MediaCachePolicy(
            memory_max_bytes=64 * BLOCK,
            disk_max_bytes=0,
            scratch_max_bytes=args.scratch_blocks * BLOCK,
            min_free_bytes=0,
        ),
    )
    results = []
    await pool.start()
    stop = asyncio.Event()
    peak = Counter()

    async def sample():
        while not stop.is_set():
            snapshot = pool.performance_snapshot()
            peak["queued_requests"] = max(peak["queued_requests"], snapshot["queued"])
            peak["scratch_waiters"] = max(
                peak["scratch_waiters"], cache.scratch.waiters
            )
            peak["oldest_queue_wait_seconds"] = max(
                peak["oldest_queue_wait_seconds"],
                max((row["waiting_seconds"] for row in snapshot["queue"]), default=0),
            )
            await asyncio.sleep(0.005)

    async def consume(
        label, identity, *, offset=0, count=32, slow=0, background=False, take=None
    ):
        before = monitor["origin_bytes"]
        started = time.monotonic()
        media = source(clients[0], identity, len(payload))
        stream = media_reader.stream_pooled_media(
            pool,
            clients[0],
            media,
            channel=1,
            offset=offset,
            limit=count,
            background=background,
        )
        first = None
        delivered = chunks = 0
        try:
            async for data in stream:
                first = first if first is not None else time.monotonic() - started
                assert data == b"x" * len(data)
                delivered += len(data)
                chunks += 1
                if take is not None and chunks == take:
                    break
                if slow:
                    await asyncio.sleep(slow)
        finally:
            await stream.aclose()
        elapsed = time.monotonic() - started
        row = {
            "scenario": label,
            "first_byte_seconds": first,
            "elapsed_seconds": elapsed,
            "delivered_bytes": delivered,
            "delivered_bytes_per_second": delivered / elapsed,
            "origin_bytes_during_request": monitor["origin_bytes"] - before,
        }
        results.append(row)
        return row

    sampling = asyncio.create_task(sample())
    try:
        await consume("cold_sequential", 1, count=16)
        await consume("warm_sequential", 1, count=16)
        await consume("cold_seek", 1, offset=30, count=2)
        await consume("full_download", 2)
        await asyncio.gather(consume("concurrent_a", 3), consume("concurrent_b", 4))
        await consume("slow_delivery", 2, count=8, slow=0.12)
        await asyncio.gather(
            consume("background", 3, count=16, background=True),
            consume("foreground_seek", 4, offset=24, count=2),
        )
        await pool.defer_client(clients[0].name, time.time() + 0.4)
        await consume("cooldown_fallback", 1, offset=20, count=4)
        await consume("cancelled_after_head", 1, offset=16, count=16, take=1)
        assert cache.scratch.used == cache._active_streams == 0
        assert not cache.memory.flights
        return {
            "scope": "Synthetic pooled-reader consumer; no HTTP or Telegram network",
            "initial_blocks": args.initial_blocks,
            "scratch_blocks": args.scratch_blocks,
            "configured_accounts": 3,
            "media_per_account": 3,
            "native_request_limit": 8,
            "native_parts_per_second": 48,
            "synthetic_half_mib_delay_seconds": 0.02,
            "origin_bytes": monitor["origin_bytes"],
            "cache_effective_memory_bytes": cache.effective_memory_bytes,
            "scratch_peak_bytes": cache.scratch.peak * BLOCK,
            "observed_peaks": dict(peak),
            "results": results,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        }
    finally:
        stop.set()
        await sampling
        await pool.stop()
        await cache.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initial-blocks", type=int, choices=(2, 9), default=2)
    parser.add_argument("--scratch-blocks", type=int, choices=(4, 32), default=32)
    parser.add_argument("--git-revision", required=True)
    parser.add_argument("--git-status", required=True)
    args = parser.parse_args()
    original = media_reader._AdaptiveReadAhead

    class ProbeReadAhead(original):
        def __init__(self, maximum):
            super().__init__(maximum)
            self.window = min(maximum, args.initial_blocks)

    media_reader._AdaptiveReadAhead = ProbeReadAhead
    with tempfile.TemporaryDirectory(prefix="tgdrive-resource-probe-") as directory:
        result = asyncio.run(run(args, Path(directory)))
    root = Path(__file__).resolve().parents[2]
    result["git_revision"] = args.git_revision
    result["git_status"] = args.git_status
    result["source_sha256"] = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "tgdrive/media/media_reader.py", Path(__file__).resolve())
    }
    for metric in (
        "memory.max",
        "memory.peak",
        "memory.events",
        "memory.swap.max",
        "memory.swap.peak",
    ):
        path = Path("/sys/fs/cgroup") / metric
        if path.exists():
            result[metric] = path.read_text().strip()
    print(json.dumps(result, sort_keys=True))
