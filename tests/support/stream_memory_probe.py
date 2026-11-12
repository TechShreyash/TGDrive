"""Bounded synthetic ASGI probe without provider or database network."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import resource
import sys
import tempfile
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from starlette.exceptions import HTTPException

from tests.media.test_pooled_media import account
from tests.media.test_telegram_cache import message
from tests.support.app_fixtures import FakeRuntime, make_settings
from tests.support.mongo_fixtures import new_store
from tests.support.resource_utilization_probe import DelayedIterator
from tgdrive.core.security import SESSION_COOKIE
from tgdrive.core.system_resources import memory_snapshot
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import BLOCK, MediaReadCache
from tgdrive.media.media_scheduler import MediaScheduler
from tgdrive.media.preloading import PreloadManager
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import TelegramMessage
from tgdrive.transfers.bandwidth import FileBandwidth
from tgdrive.web.app import create_app


def cgroup_value(name):
    path = Path("/sys/fs/cgroup") / name
    return path.read_text().strip() if path.exists() else None


async def until(predicate, seconds=5):
    async with asyncio.timeout(seconds):
        while not predicate():  # noqa: ASYNC110 - bounded probe-state observation.
            await asyncio.sleep(0.01)


class ProbeRuntime(FakeRuntime):
    bandwidth: FileBandwidth


async def scenario(
    directory: Path, *, adaptive: bool, smoke: bool, pressure: bool = False
):
    size = (2 if smoke else 16) * BLOCK
    payload = b"x" * size
    monitor = Counter()
    patches = ExitStack()
    clients = [account(f"probe-bot-{index}", payload) for index in range(2)]
    for client in clients:
        client._media_scheduler = MediaScheduler(limit=8, rate=48)
        native_download = client.native.iter_download
        patches.enter_context(
            patch.object(
                client.native,
                "iter_download",
                lambda *args, original=native_download, **kwargs: DelayedIterator(
                    original(*args, **kwargs), 0.01, monitor
                ),
            )
        )
        client.native.payloads = {1: payload, 2: payload}

        def bound_source(_channel, message_ids, owner=client):
            native = message(1 if message_ids == 77 else 2, size)
            native.id = message_ids
            return TelegramMessage(owner, native)

        client.get_messages = AsyncMock(side_effect=bound_source)
    settings = make_settings(directory)
    settings.cache_disk_bytes = settings.cache_memory_bytes = 0
    settings.stream_memory_limit_bytes = None if adaptive else 32 * BLOCK
    settings.resolved_cache_dir.mkdir(parents=True)
    settings.resolved_data_dir.mkdir(parents=True)
    store = new_store(directory / "store")
    first = store.add_file("/", "ranges.bin", 77, size)["id"]
    second = store.add_file("/", "viewer.bin", 78, size)["id"]
    multipart = store.add_file(
        "/",
        "multipart.bin",
        77,
        2 * size,
        parts=[
            {
                "index": index,
                "offset": index * size,
                "size": size,
                "message_id": 77 + index,
                "document_id": index + 1,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for index in range(2)
        ],
    )["id"]
    runtime = ProbeRuntime(store)
    runtime.bandwidth = FileBandwidth()
    runtime.pool = pool = ClientPool(
        clients, media_cache_dir=directory / "media", media_per_bot=3
    )
    pool.bandwidth = runtime.bandwidth
    policy = MediaCachePolicy(
        memory_max_bytes=0,
        disk_max_bytes=0,
        min_free_bytes=0,
        scratch_max_bytes=(256 if adaptive else 32) * BLOCK,
    )
    cache = pool._media_read_cache = MediaReadCache(directory / "media", policy)
    runtime.preloads = PreloadManager(pool, policy=policy)
    app = create_app(settings, runtime=runtime)
    owner_a, owner_b = app.state.sessions.create(), app.state.sessions.create()
    await pool.start()
    stop, held = asyncio.Event(), asyncio.Event()
    peaks, counts = Counter(), Counter()
    results, samples, tasks = [], [], []
    sample_error = []
    pressure_active = False
    pressure_result = None
    head_gate = asyncio.Event()

    async def sample():
        next_policy = time.monotonic() + 5
        while not stop.is_set():
            now = time.monotonic()
            current = cgroup_value("memory.current")
            maximum = cgroup_value("memory.max")
            if (
                current is not None
                and maximum not in (None, "max")
                and int(current) > int(maximum) * (0.93 if pressure_active else 0.85)
            ):
                sample_error.append(
                    "Probe memory guard stopped the case before further allocation"
                )
                head_gate.set()
                for task in tasks:
                    task.cancel()
                return
            peaks["scratch_reserved_bytes"] = max(
                peaks["scratch_reserved_bytes"], cache.scratch.used * BLOCK
            )
            peaks["fill_reserved_bytes_overlapping"] = max(
                peaks["fill_reserved_bytes_overlapping"], cache.fill_slots.used * BLOCK
            )
            peaks["scratch_ceiling_bytes"] = max(
                peaks["scratch_ceiling_bytes"], cache.scratch.limit * BLOCK
            )
            peaks["head_waiters"] = max(peaks["head_waiters"], cache.scratch.waiters)
            peaks["retiring_reservations"] = max(
                peaks["retiring_reservations"],
                sum(len(stream.retiring) for stream in cache.scratch._streams),
            )
            if now >= next_policy:
                memory = await asyncio.to_thread(memory_snapshot)
                sample = {
                    "memory_available": memory["available_bytes"],
                    "memory_limit": memory["limit_bytes"],
                    "sampled_at": time.time(),
                }
                cache.update_stream_capacity(sample)
                samples.append(
                    {"memory": memory, "scratch": cache.activity_snapshot()["scratch"]}
                )
                next_policy = now + 5
            await asyncio.sleep(0.02)

    async def request(
        label,
        identity,
        cookie,
        *,
        start=0,
        length=BLOCK,
        gate=None,
        expect_cancel=False,
        expect_revocation=False,
        full=False,
    ):
        started = time.monotonic()
        digest = hashlib.sha256()
        delivered = 0
        status = None
        first_at = None
        received = False
        disconnected = asyncio.Event()
        query = urlencode({"path": "/" + identity, "request_id": label})
        scope = {
            "type": "http",
            "http_version": "1.1",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "GET",
            "scheme": "https",
            "path": "/file",
            "raw_path": b"/file",
            "query_string": query.encode(),
            "headers": [
                (b"host", b"probe.invalid"),
                (b"cookie", f"{SESSION_COOKIE}={cookie}".encode()),
                (b"range", f"bytes={start}-{start + length - 1}".encode()),
            ],
            "server": ("probe.invalid", 443),
            "client": ("synthetic", 1),
        }

        if full:
            scope["headers"] = [
                (key, value) for key, value in scope["headers"] if key != b"range"
            ]

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status, first_at, delivered
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body" and message.get("body"):
                if first_at is None:
                    first_at = time.monotonic() - started
                    counts["held"] += int(gate is not None)
                    if label == "pressure-new-head":
                        counts["pressure_new_head_delivered"] += 1
                    held.set()
                    if label == "finite-b":
                        counts["finite_a_completed_at_b_first"] = counts[
                            "finite_a_completed"
                        ]
                    if gate is not None:
                        await gate.wait()
                digest.update(message["body"])
                delivered += len(message["body"])

        aborted = None
        try:
            await app(scope, receive, send)
        except asyncio.CancelledError:
            if not expect_cancel and not sample_error:
                raise
            aborted = "cancelled"
        except (HTTPException, RuntimeError) as error:
            cause = error if isinstance(error, HTTPException) else error.__cause__
            if (
                not expect_revocation
                or not isinstance(cause, HTTPException)
                or cause.status_code not in (401, 403)
            ):
                raise
            aborted = "authorization_revoked"
        if aborted is None:
            assert status in (200, 206), (label, status)
            assert delivered == length, (label, delivered, length)
            expected = hashlib.sha256()
            for cursor in range(0, length, BLOCK):
                expected.update(payload[: min(BLOCK, length - cursor)])
            assert digest.hexdigest() == expected.hexdigest()
            counts["verified_responses"] += 1
            if label.startswith("finite-a-"):
                counts["finite_a_completed"] += 1
        row = {
            "label": label,
            "status": status,
            "first_body_seconds": first_at,
            "elapsed_seconds": time.monotonic() - started,
            "delivered_bytes": delivered,
            "sha256": digest.hexdigest(),
            "abort": aborted,
        }
        results.append(row)
        return row

    sampler = asyncio.create_task(sample())
    try:
        full_response = await request(
            "full-200", first, owner_a, length=size, full=True
        )
        assert full_response["status"] == 200
        multipart_response = await request(
            "multipart-cross-boundary",
            multipart,
            owner_b,
            start=size - BLOCK // 2,
            length=BLOCK,
        )
        assert multipart_response["status"] == 206
        initial_limit = cache.scratch.limit
        parallel = 4 if smoke else 40
        for index in range(parallel):
            tasks.append(
                asyncio.create_task(
                    request(
                        f"held-{index}",
                        first,
                        owner_a,
                        start=(index % (size // BLOCK)) * BLOCK,
                        gate=head_gate,
                        expect_cancel=True,
                    )
                )
            )
        await until(lambda: counts["held"] >= min(initial_limit, parallel), 15)
        viewer = asyncio.create_task(request("competing-viewer", second, owner_b))
        tasks.append(viewer)
        await asyncio.sleep(0.1 if smoke else 8)
        head_gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 20)
        assert not sample_error, sample_error
        assert cache.scratch.used == cache._active_streams == cache.fill_slots.used == 0
        await runtime.bandwidth.configure(512 * 1024, None)
        counts["finite_a_completed"] = 0
        tasks = [
            asyncio.create_task(
                request(f"finite-a-{index}", first, owner_a, length=64 * 1024)
            )
            for index in range(4 if smoke else 16)
        ]
        await asyncio.sleep(0.02)
        tasks.append(
            asyncio.create_task(request("finite-b", second, owner_b, length=64 * 1024))
        )
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert counts["finite_a_completed_at_b_first"] < len(tasks) - 1
        await runtime.bandwidth.configure(None, None)
        cancel_gate = asyncio.Event()
        before = counts["held"]
        cancelled = asyncio.create_task(
            request(
                "cancelled-http",
                first,
                owner_a,
                length=2 * BLOCK,
                gate=cancel_gate,
                expect_cancel=True,
            )
        )
        tasks = [cancelled]
        await until(lambda: counts["held"] > before)
        cancelled.cancel()
        await asyncio.wait_for(cancelled, 3)
        await until(
            lambda: (
                cache.scratch.used
                == cache.fill_slots.used
                == cache._active_streams
                == 0
            )
        )
        revoke_gate = asyncio.Event()
        before = counts["held"]
        revocable = app.state.sessions.create()
        revoked = asyncio.create_task(
            request(
                "revoked-session",
                second,
                revocable,
                length=2 * BLOCK,
                gate=revoke_gate,
                expect_revocation=True,
            )
        )
        tasks = [revoked]
        await until(lambda: counts["held"] > before)
        app.state.sessions.revoke(revocable)
        revoke_gate.set()
        revoked_row = await asyncio.wait_for(revoked, 5)
        assert revoked_row["abort"] == "authorization_revoked"
        assert cache.scratch.used == cache.fill_slots.used == cache._active_streams == 0
        assert not cache.memory.flights
        if pressure:
            pressure_active = True
            padding = []
            padding_bytes = 0
            pressure_gate = asyncio.Event()
            before_held = counts["held"]
            pressure_tasks = [
                asyncio.create_task(
                    request(
                        f"pressure-held-{index}",
                        first,
                        owner_a,
                        gate=pressure_gate,
                        expect_cancel=True,
                    )
                )
                for index in range(12)
            ]
            tasks = pressure_tasks
            new_head = None
            try:
                await until(lambda: counts["held"] >= before_held + 12, 15)
                owned = cache.scratch.used * BLOCK
                memory = await asyncio.to_thread(memory_snapshot)
                limit = memory["limit_bytes"]
                if limit is None or limit > 1024**3:
                    raise ValueError(
                        "Pressure mode requires its finite <=1GiB disposable container"
                    )
                goal = limit // 10 - BLOCK
                padding_limit = (96 if limit <= 256 * BLOCK else 700) * BLOCK
                padding_increment = (1 if limit <= 256 * BLOCK else 4) * BLOCK
                reason = None
                while (
                    memory["available_bytes"] is not None
                    and memory["available_bytes"] > goal
                ):
                    host = await asyncio.to_thread(
                        memory_snapshot, cgroup=Path("/nonexistent-probe-cgroup")
                    )
                    current_raw = cgroup_value("memory.current")
                    if current_raw is None:
                        reason = "cgroup measurement unavailable"
                        break
                    current = int(current_raw)
                    if (
                        host["available_bytes"] is None
                        or host["available_bytes"] < 2 * 1024**3
                    ):
                        reason = "host/VM available-memory guard"
                        break
                    if current >= limit * 0.93:
                        reason = "93% cgroup guard"
                        break
                    amount = min(padding_increment, memory["available_bytes"] - goal)
                    if padding_bytes + amount > padding_limit:
                        reason = "approved padding bound"
                        break
                    block = bytearray(amount)
                    for cursor in range(0, amount, 4096):
                        block[cursor] = 1
                    padding.append(block)
                    padding_bytes += amount
                    del block
                    await asyncio.sleep(0)
                    memory = await asyncio.to_thread(memory_snapshot)
                crossed = (
                    not sample_error
                    and memory["available_bytes"] is not None
                    and memory["available_bytes"] < limit // 10
                )
                pressure_result = {
                    "exercised": crossed,
                    "not_exercised_reason": reason,
                    "padding_bytes": padding_bytes,
                    "padding_increment_max_bytes": padding_increment,
                    "padding_limit_bytes": padding_limit,
                    "abort_cgroup_fraction": 0.93,
                    "minimum_host_available_bytes": 2 * 1024**3,
                    "memory_at_crossing": memory,
                    "owned_before_bytes": owned,
                }
                if crossed:
                    crossed_at = time.time()
                    try:
                        await until(
                            lambda: (
                                cache.activity_snapshot()["scratch"]["reason"]
                                == "memory_pressure"
                            ),
                            7,
                        )
                    except TimeoutError:
                        diagnostic = {
                            "event": "pressure_wait_timeout",
                            "memory_at_crossing": pressure_result["memory_at_crossing"],
                            "memory_at_wait_end": await asyncio.to_thread(
                                memory_snapshot
                            ),
                            "last_samples": samples[-3:],
                            "sampler_done": sampler.done(),
                            "sampler_exception": str(sampler.exception())
                            if sampler.done() and not sampler.cancelled()
                            else None,
                            "sample_error": list(sample_error),
                            "cgroup": {
                                name: cgroup_value(name)
                                for name in (
                                    "memory.current",
                                    "memory.max",
                                    "memory.peak",
                                    "memory.events",
                                )
                            },
                        }
                        print(
                            "PRESSURE_DIAGNOSTIC "
                            + json.dumps(diagnostic, sort_keys=True),
                            file=sys.stderr,
                            flush=True,
                        )
                        pressure_result["wait_diagnostic"] = diagnostic
                        relevant = [
                            row
                            for row in samples
                            if (row["scratch"]["sampled_at"] or 0) >= crossed_at
                        ]
                        actual_pressure = [
                            row
                            for row in relevant
                            if row["memory"]["available_bytes"] is not None
                            and row["memory"]["available_bytes"]
                            < max(32 * BLOCK, (row["memory"]["limit_bytes"] or 0) // 10)
                        ]
                        assert not actual_pressure, (
                            "Shared pressure sample did not produce "
                            "its required policy response"
                        )
                        crossed = False
                        pressure_result["exercised"] = False
                        pressure_result["not_exercised_reason"] = (
                            "Pressure was not sustained until "
                            "the existing5s policy sample"
                        )
                        pressure_result["samples_after_crossing"] = relevant
                        pressure_result["memory_after_wait"] = await asyncio.to_thread(
                            memory_snapshot
                        )
                if crossed:
                    snapshot = cache.activity_snapshot()["scratch"]
                    assert snapshot["max_bytes"] < owned
                    assert snapshot["bytes"] == owned
                    new_head = asyncio.create_task(
                        request(
                            "pressure-new-head", second, owner_b, expect_cancel=True
                        )
                    )
                    tasks.append(new_head)
                    await asyncio.sleep(0.1)
                    assert not new_head.done()
                    assert counts["pressure_new_head_delivered"] == 0
                    assert cache.scratch.used * BLOCK == owned
                    pressure_result["held_snapshot"] = snapshot
                pressure_gate.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 10)
                assert (
                    cache.scratch.used
                    == cache.fill_slots.used
                    == cache._active_streams
                    == 0
                )
                assert not cache.memory.flights
                pressure_result["owned_after_cleanup_bytes"] = 0
            finally:
                pressure_gate.set()
                for task in pressure_tasks:
                    task.cancel()
                if new_head is not None:
                    new_head.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                padding.clear()
                pressure_active = False
            pressure_result["memory_after_padding_release"] = await asyncio.to_thread(
                memory_snapshot
            )
        return {
            "adaptive": adaptive,
            "pressure": pressure_result,
            "payload_bytes": size,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "parallel_same_file_connections": parallel,
            "synthetic_accounts": 2,
            "media_per_account": 3,
            "initial_scratch_bytes": initial_limit * BLOCK,
            "held_delivery_seconds": 0.1 if smoke else 8,
            "peaks": dict(peaks),
            "governor_samples": samples,
            "counts": dict(counts),
            "origin_bytes": monitor["origin_bytes"],
            "synthetic_half_mib_delay_seconds": 0.01,
            "native_request_limit": 8,
            "native_parts_per_second": 48,
            "responses": results,
            "peak_process_rss_bytes_cumulative": resource.getrusage(
                resource.RUSAGE_SELF
            ).ru_maxrss
            * 1024,
        }
    finally:
        head_gate.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stop.set()
        await sampler
        await runtime.preloads.close()
        await pool.stop()
        runtime.transfers.registry.close()
        store.close()
        patches.close()


async def main(smoke, pressure):
    output = []
    for adaptive in (True,) if pressure else (False, True):
        with tempfile.TemporaryDirectory(prefix="tgdrive-stream-probe-") as temporary:
            output.append(
                await scenario(
                    Path(temporary), adaptive=adaptive, smoke=smoke, pressure=pressure
                )
            )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--pressure", action="store_true")
    parser.add_argument("--git-revision", required=True)
    args = parser.parse_args()
    result = {
        "scope": (
            "Real ASGI /file/auth/range/body/cancellation pipeline; "
            "synthetic Telegram/catalog, no HTTP socket/provider network"
        ),
        "cases": asyncio.run(main(args.smoke, args.pressure)),
        "git_revision": args.git_revision,
    }
    result["source_sha256"] = {
        path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in (
            "tgdrive/media/media_reader.py",
            "tgdrive/media/media_scheduler.py",
            "tgdrive/telegram/client_pool.py",
            "tgdrive/web/app.py",
            "tgdrive/web/file_bandwidth.py",
            "tgdrive/transfers/bandwidth.py",
            __file__,
        )
    }
    result["cgroup"] = {
        name: cgroup_value(name)
        for name in (
            "memory.max",
            "memory.peak",
            "memory.events",
            "memory.swap.max",
            "memory.swap.peak",
        )
    }
    print(json.dumps(result, sort_keys=True))
