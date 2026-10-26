from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from tests.media.test_media_scheduler import wait_until
from tests.media.test_pooled_media import account
from tests.media.test_telegram_cache import message
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import (
    BLOCK,
    MediaReadCache,
    _AdaptiveReadAhead,
    stream_pooled_media,
)
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import TelegramMessage

CHANNEL = -1000000000123


@pytest.fixture
def sustained_reader(monkeypatch):
    """Stress reclamation after a reader has already demonstrated fast demand.

    Adaptation itself is exercised with real generator advancement and a
    deterministic clock in test_adaptive_read_ahead; these tests need a full
    speculative pipeline to preserve their cancellation/fairness edge cases.
    """

    class SustainedReadAhead(_AdaptiveReadAhead):
        def __init__(self, maximum):
            now = 0.0
            super().__init__(maximum, clock=lambda: now)
            self.observe_fetch(1)
            for _ in range(5):
                now += 0.25
                self.consumed(100 * BLOCK)
            self.clock = time.monotonic
            self._sample_at = self.clock()

    monkeypatch.setattr(
        "tgdrive.media.media_reader._AdaptiveReadAhead", SustainedReadAhead
    )


@pytest.mark.asyncio
async def test_twenty_slow_distinct_readers_share_bounded_scratch_and_release_on_cancel(
    tmp_path,
):
    payload = b"x" * (6 * BLOCK)
    accounts = [account(str(index), payload) for index in range(20)]
    for client in accounts:
        client.native.payloads = {identity: payload for identity in range(1, 21)}

        async def fetch(_channel, *, message_ids, owner=client):
            native = message(message_ids - 99, len(payload))
            native.id = message_ids
            return TelegramMessage(owner, native)

        client.get_messages = AsyncMock(side_effect=fetch)
    pool = ClientPool(accounts, media_cache_dir=tmp_path)
    pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=0,
            disk_max_bytes=0,
            scratch_max_bytes=4 * BLOCK,
            min_free_bytes=0,
        ),
    )
    await pool.start()
    iterators, readers = [], []
    try:
        for index, client in enumerate(accounts):
            source = await client.get_messages(CHANNEL, message_ids=index + 100)
            iterator = stream_pooled_media(
                pool, client, source, channel=CHANNEL, offset=0, limit=6
            )
            iterators.append(iterator)
            readers.append(asyncio.create_task(anext(iterator)))
        cache = pool._media_read_cache
        await wait_until(lambda: any(task.done() for task in readers))
        await wait_until(lambda: cache._active_streams == 20)
        await wait_until(lambda: sum(task.done() for task in readers) == 4)
        completed = sum(task.done() for task in readers)
        assert cache._active_streams == 20
        state = await cache.snapshot()
        assert state["memory"]["bytes"] == state["disk"]["bytes"] == 0
        assert state["scratch"]["bytes"] == state["scratch"]["peak_bytes"] == 4 * BLOCK
        assert state["scratch"]["waiting_heads"] == 20 - completed
        first = next(index for index, task in enumerate(readers) if task.done())
        assert readers[first].result() == payload[:BLOCK]
        await iterators[first].aclose()
        await wait_until(lambda: sum(task.done() for task in readers) > completed)
        assert cache.scratch.peak == 4
    finally:
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        for iterator in iterators:
            await iterator.aclose()
        assert pool._media_read_cache.scratch.used == 0
        assert pool._media_read_cache._active_streams == 0
        assert not pool._media_read_cache.memory.flights
        assert all(row.active_leases == 0 for row in await pool.status())
        await pool._media_read_cache.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_inflight_scratch_reservations(tmp_path):
    gate = asyncio.Event()
    client = account("bot", b"x" * (8 * BLOCK), gate)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=0,
            disk_max_bytes=0,
            scratch_max_bytes=8 * BLOCK,
            min_free_bytes=0,
        ),
    )
    await pool.start()
    source = await client.get_messages(CHANNEL, message_ids=77)
    iterator = stream_pooled_media(
        pool, client, source, channel=CHANNEL, offset=0, limit=8
    )
    reader = asyncio.create_task(anext(iterator))
    try:
        await wait_until(lambda: client.native.active > 0)
        reader.cancel()
        await asyncio.sleep(0)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert pool._media_read_cache.scratch.used == 0
        assert pool._media_read_cache._active_streams == 0
        assert not pool._media_read_cache.memory.flights
        assert all(row.active_leases == 0 for row in await pool.status())
    finally:
        gate.set()
        await iterator.aclose()
        await pool._media_read_cache.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_sustained_reader_can_use_full_scratch_and_many_origin_fills(
    tmp_path, monkeypatch, sustained_reader
):
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    gate = asyncio.Event()
    payload = b"x" * (64 * BLOCK)
    accounts = [account(str(index), payload, gate) for index in range(20)]
    pool = ClientPool(accounts, media_cache_dir=tmp_path)
    pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=0,
            disk_max_bytes=0,
            scratch_max_bytes=32 * BLOCK,
            min_free_bytes=0,
        ),
    )
    await pool.start()
    source = await accounts[0].get_messages(CHANNEL, message_ids=77)
    iterator = stream_pooled_media(
        pool, accounts[0], source, channel=CHANNEL, offset=0, limit=64
    )
    reader = asyncio.create_task(anext(iterator))
    try:
        await wait_until(lambda: sum(client.native.active for client in accounts) == 32)
        state = await pool._media_read_cache.snapshot()
        assert state["scratch"]["bytes"] == 32 * BLOCK
        assert state["memory"]["active_fills"] == 32
        assert state["memory"]["max_fills"] == 32
        assert state["scratch"]["active_streams"] == 1
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        gate.set()
        await iterator.aclose()
        assert pool._media_read_cache.scratch.used == 0
        assert all(client.native.active == 0 for client in accounts)
        await pool._media_read_cache.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["ram", "disk"])
async def test_paused_warm_reader_relinquishes_ahead_to_new_heads_and_resumes_in_order(
    tmp_path, monkeypatch, tier, sustained_reader
):
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    payload = b"".join(bytes([index]) * BLOCK for index in range(24)) + b"tail"
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    cache = pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=64 * BLOCK if tier == "ram" else 0,
            disk_max_bytes=64 * BLOCK if tier == "disk" else 0,
            scratch_max_bytes=8 * BLOCK,
            min_free_bytes=0,
        ),
    )
    await pool.start()
    source = await client.get_messages(CHANNEL, message_ids=77)

    def stream(offset=0, limit=25):
        return stream_pooled_media(
            pool, client, source, channel=CHANNEL, offset=offset, limit=limit
        )

    paused, seeks, readers = stream(), [], []
    try:
        assert b"".join([data async for data in stream()]) == payload
        await cache.flush()
        requests = sum(client.native.requests.values())
        first = await anext(paused)
        assert first == payload[:BLOCK]
        await wait_until(lambda: cache.scratch.used == 8)
        # None of these consumers advance the original reader. Its suspended
        # read-ahead must make room for new beginning/end/seek probes itself.
        for index in (24, 10, 3, 20):
            seek = stream(index, 1)
            seeks.append(seek)
            readers.append(asyncio.create_task(anext(seek)))
        async with asyncio.timeout(2):
            results = await asyncio.gather(*readers)
        for index, result in zip((24, 10, 3, 20), results, strict=True):
            assert result == payload[index * BLOCK : (index + 1) * BLOCK]
        assert cache.scratch.used <= cache.scratch.peak == 8
        assert sum(client.native.requests.values()) == requests
        assert cache.scratch.waiters == 0
        for seek in seeks:
            await seek.aclose()
        # Reclaimed offsets are loaded again from shared retention, without
        # losing/duplicating any bytes or losing a newer frame's reservation.
        async with asyncio.timeout(3):
            assert first + b"".join([data async for data in paused]) == payload
        assert sum(client.native.requests.values()) == requests
    finally:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        for iterator in [paused, *seeks]:
            await iterator.aclose()
        assert cache.scratch.used == cache._active_streams == 0
        assert cache.scratch._streams == set()
        assert not cache.memory.flights
        assert all(row.active_leases == 0 for row in await pool.status())
        await cache.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_cold_ahead_reclamation_keeps_payload_reserved_until_remote_cleanup(
    tmp_path, monkeypatch, cancel_waiter, sustained_reader
):
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    payload = b"".join(bytes([index]) * BLOCK for index in range(16))
    client = account("bot", payload)
    release_data, release_cleanup, cancelling = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    active = set()

    async def remote(_source, *, offset, limit, **_kwargs):
        assert limit == 1
        active.add(offset)
        try:
            if 1 <= offset <= 7:
                try:
                    await release_data.wait()
                except asyncio.CancelledError:
                    cancelling.set()
                    await release_cleanup.wait()
                    raise
            yield payload[offset * BLOCK : (offset + 1) * BLOCK]
        finally:
            active.discard(offset)

    client.stream_media = remote
    pool = ClientPool([client], media_cache_dir=tmp_path, media_per_bot=16)
    cache = pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=0,
            disk_max_bytes=0,
            scratch_max_bytes=8 * BLOCK,
            min_free_bytes=0,
        ),
    )
    await pool.start()
    source = await client.get_messages(CHANNEL, message_ids=77)

    def stream(offset=0, limit=16):
        return stream_pooled_media(
            pool, client, source, channel=CHANNEL, offset=offset, limit=limit
        )

    paused, seek = stream(), stream(15, 1)
    reader = None
    try:
        first = await anext(paused)
        await wait_until(lambda: active == set(range(1, 8)))
        assert cache.scratch.used == 8
        reader = asyncio.create_task(anext(seek))
        await asyncio.wait_for(cancelling.wait(), 2)
        assert not reader.done()
        assert cache.scratch.used == 8  # cancelled I/O still owns its frame
        assert 15 not in active
        if cancel_waiter:
            reader.cancel()
            await asyncio.sleep(0)
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader
            await seek.aclose()
            seek = stream(15, 1)
            reader = asyncio.create_task(anext(seek))
            await wait_until(lambda: cache.scratch.waiters == 1)
        release_cleanup.set()
        assert await asyncio.wait_for(reader, 2) == payload[15 * BLOCK :]
        assert cache.scratch.peak == 8
        await seek.aclose()
        release_data.set()
        async with asyncio.timeout(3):
            assert first + b"".join([data async for data in paused]) == payload
    finally:
        release_cleanup.set()
        release_data.set()
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await seek.aclose()
        await paused.aclose()
        assert cache.scratch.used == cache._active_streams == 0
        assert not cache.memory.flights
        assert not active
        assert all(row.active_leases == 0 for row in await pool.status())
        await cache.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_reclaiming_shared_ahead_keeps_another_viewers_origin_fill_alive(
    tmp_path, monkeypatch, sustained_reader
):
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    payload = b"".join(bytes([index]) * BLOCK for index in range(16))
    client = account("bot", payload)
    release_data = asyncio.Event()
    calls, cancelled = [], []

    async def remote(_source, *, offset, limit, **_kwargs):
        assert limit == 1
        calls.append(offset)
        try:
            if offset in (1, 2):
                await release_data.wait()
            yield payload[offset * BLOCK : (offset + 1) * BLOCK]
        except asyncio.CancelledError:
            cancelled.append(offset)
            raise

    client.stream_media = remote
    pool = ClientPool([client], media_cache_dir=tmp_path, media_per_bot=16)
    cache = pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=64 * BLOCK,
            disk_max_bytes=0,
            scratch_max_bytes=6 * BLOCK,
            read_ahead_blocks=3,
            min_free_bytes=0,
        ),
    )
    await pool.start()
    source = await client.get_messages(CHANNEL, message_ids=77)

    def stream(offset=0, limit=16):
        return stream_pooled_media(
            pool, client, source, channel=CHANNEL, offset=offset, limit=limit
        )

    paused, follower, probe = stream(), stream(2, 1), stream(15, 1)
    holders = [stream(10, 1), stream(11, 1)]
    reader = None
    try:
        first = await anext(paused)
        reader = asyncio.create_task(anext(follower))
        await wait_until(
            lambda: any(flight.waiters == 2 for flight in cache.memory.flights.values())
        )
        for holder in holders:
            await anext(holder)
        assert cache.scratch.used == 6
        assert await asyncio.wait_for(anext(probe), 2) == payload[15 * BLOCK :]
        # The original reader's farthest ahead was reclaimed, but the shared
        # fill for block two remains owned by the still-waiting follower.
        assert calls.count(2) == 1
        assert 2 not in cancelled
        assert not reader.done()
        assert cache.scratch.peak == 6
        await probe.aclose()
        for holder in holders:
            await holder.aclose()
        release_data.set()
        assert await asyncio.wait_for(reader, 2) == payload[2 * BLOCK : 3 * BLOCK]
        await follower.aclose()
        async with asyncio.timeout(3):
            assert first + b"".join([data async for data in paused]) == payload
        assert calls.count(2) == 1
    finally:
        release_data.set()
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        for iterator in [paused, follower, probe, *holders]:
            await iterator.aclose()
        assert cache.scratch.used == cache._active_streams == 0
        assert not cache.memory.flights
        assert all(row.active_leases == 0 for row in await pool.status())
        await cache.close()
        await pool.stop()
