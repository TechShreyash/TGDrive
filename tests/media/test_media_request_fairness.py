from __future__ import annotations

import asyncio

import pytest

from tests.media.test_media_scheduler import Account, wait_until
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


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", ["demand", "preload"])
async def test_download_turn_rotates_read_ahead_behind_other_requests(priority):
    pool = ClientPool([Account()], media_per_bot=1)
    await pool.start()
    order, tasks = [], []

    async def read(request_id, block):
        async with pool.lease(download=True, priority=priority, request_id=request_id):
            order.append((request_id, block))

    try:
        async with pool.lease(download=True):
            for request_id, block in [("a", 0), ("a", 1), ("b", 0), ("c", 0)]:
                tasks.append(asyncio.create_task(read(request_id, block)))
                await wait_until(lambda: len(pool._origin_waiters) == len(tasks))
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert order == [("a", 0), ("b", 0), ("c", 0), ("a", 1)]
        assert pool.performance_snapshot()["active"] == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.stop()


@pytest.mark.asyncio
async def test_one_bot_serves_distinct_anonymous_files_and_drops_cancelled_waiter(
    tmp_path, monkeypatch
):
    class SustainedReadAhead(_AdaptiveReadAhead):
        def __init__(self, maximum):
            super().__init__(maximum)
            self.window = self.maximum

    monkeypatch.setattr(
        "tgdrive.media.media_reader._AdaptiveReadAhead", SustainedReadAhead
    )
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    client = account("only-bot", b"")
    pool = ClientPool([client])  # The default three media operations serve four files.
    cache = pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(memory_max_bytes=0, disk_max_bytes=0, min_free_bytes=0),
    )
    release = asyncio.Event()
    calls, tasks = [], []
    active = peak = 0

    async def remote(source, *, offset, limit, **_kwargs):
        nonlocal active, peak
        document = source.native.document.id
        calls.append((document, offset))
        active += 1
        peak = max(peak, active)
        try:
            assert limit == 1
            await release.wait()
            yield bytes([document]) * BLOCK
        finally:
            active -= 1

    client.stream_media = remote

    async def read(document, count):
        native = message(document, count * BLOCK)
        native.id = 100 + document
        stream = stream_pooled_media(
            pool,
            client,
            TelegramMessage(client, native),
            channel=1,
            offset=0,
            limit=count,
        )
        try:
            return b"".join([data async for data in stream])
        finally:
            await stream.aclose()

    await pool.start()
    try:
        tasks.append(asyncio.create_task(read(1, 6)))
        await wait_until(lambda: len(pool._origin_waiters) == 3 and active == 3)
        first_ids = {row["request_id"] for row in pool._origin_waiters}
        assert len(first_ids) == 1  # Every block of an anonymous stream shares a turn.
        for document in (2, 3, 4):
            tasks.append(asyncio.create_task(read(document, 2)))
            await wait_until(
                lambda: len(pool._origin_waiters) == 3 + 2 * (len(tasks) - 1)
            )
        queued_ids = {row["request_id"] for row in pool._origin_waiters}
        assert len(queued_ids) == 4  # Distinct streams never collapse into one turn.
        cancelled_ids = {row["request_id"] for row in pool._origin_waiters[-2:]}
        tasks[-1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[-1]
        assert not cancelled_ids.intersection(
            row["request_id"] for row in pool._origin_waiters
        )
        assert len(pool._origin_waiters) == 7
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks[:-1]), 3)
        assert results == [
            b"\x01" * (6 * BLOCK),
            b"\x02" * (2 * BLOCK),
            b"\x03" * (2 * BLOCK),
        ]
        assert [document for document, _offset in calls[3:6]] == [1, 2, 3]
        assert sorted(calls) == [
            (document, offset)
            for document, count in ((1, 6), (2, 2), (3, 2))
            for offset in range(count)
        ]
        assert peak == 3 and active == 0
        assert cache.scratch.used == cache._active_streams == 0
        assert not cache.memory.flights
        assert not pool._origin_waiters and not pool._origin_active
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await cache.close()
        await pool.stop()
