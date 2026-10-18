from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_media_scheduler import wait_until
from test_pooled_media import account
from test_telegram_cache import message

from tgdrive.client_pool import ClientPool
from tgdrive.media_policy import MediaCachePolicy
from tgdrive.media_reader import BLOCK, MediaReadCache, stream_pooled_media
from tgdrive.telegram_clients import TelegramMessage

CHANNEL = -1000000000123


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
        completed = sum(task.done() for task in readers)
        assert 1 <= completed <= 4
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
async def test_single_reader_uses_full_scratch_and_more_than_twelve_origin_fills(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("tgdrive.media_reader._effective_memory", lambda value: value)
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
