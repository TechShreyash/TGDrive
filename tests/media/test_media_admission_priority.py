"""Demand FIFO and priority inheritance through real shared payload fills."""

from __future__ import annotations

import asyncio

import pytest

from tests.media.test_media_scheduler import wait_until
from tests.media.test_pooled_media import account
from tests.media.test_preloading import CHANNEL, finish, manager_for
from tests.media.test_telegram_cache import Native
from tgdrive.media.media_metadata import get_message
from tgdrive.media.media_reader import BLOCK, stream_pooled_media
from tgdrive.media.media_scheduler import (
    MediaPriorityScope,
    MediaScheduler,
    media_priority,
)
from tgdrive.telegram.client_pool import ClientPool


class OrderedNative(Native):
    def __init__(self, *args):
        super().__init__(*args)
        self.order = []

    def iter_download(self, target, *, offset, request_size, limit):
        self.order.append(offset)
        return super().iter_download(
            target, offset=offset, request_size=request_size, limit=limit
        )


@pytest.mark.asyncio
async def test_shared_background_fill_inherits_demand_without_overtaking_older_demand():
    payload = b"".join(bytes([index]) * BLOCK for index in range(4))
    client = account("bot", payload, native_type=OrderedNative)
    pool = ClientPool([client], media_per_bot=1)
    await pool.start()
    message = await client.get_messages(CHANNEL, message_ids=77)
    tasks = []

    async def read(index, request_id, *, background=False):
        stream = stream_pooled_media(
            pool,
            client,
            message,
            channel=CHANNEL,
            offset=index,
            limit=1,
            background=background,
            request_id=request_id,
        )
        return b"".join([data async for data in stream])

    try:
        async with pool.lease(download=True, request_id="occupied"):
            tasks.append(asyncio.create_task(read(0, "shared", background=True)))
            await wait_until(lambda: len(pool._origin_waiters) == 1)
            tasks.append(asyncio.create_task(read(1, "background", background=True)))
            await wait_until(lambda: len(pool._origin_waiters) == 2)
            tasks.append(asyncio.create_task(read(2, "first-demand")))
            await wait_until(lambda: len(pool._origin_waiters) == 3)
            tasks.append(asyncio.create_task(read(0, "joined-demand")))
            await wait_until(lambda: pool._origin_waiters[-1]["priority"] == "demand")
            tasks.append(asyncio.create_task(read(3, "later-demand")))
            await wait_until(lambda: len(pool._origin_waiters) == 4)
            queue = pool.performance_snapshot()["queue"]
            assert [row["request_id"] for row in queue] == [
                "first-demand",
                "shared",
                "later-demand",
                "background",
            ]
        results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert results == [
            payload[:BLOCK],
            payload[BLOCK : 2 * BLOCK],
            payload[2 * BLOCK : 3 * BLOCK],
            payload[:BLOCK],
            payload[3 * BLOCK :],
        ]
        assert [offset // BLOCK for offset in client.native.order] == [2, 0, 3, 1]
        assert sum(client.native.requests.values()) == 8  # Two RPCs/MiB, no duplicate.
        cache = pool._media_read_cache
        assert not cache.memory.flights and not cache._fill_priorities
        assert not cache._foreground and not cache._active_keys
        assert pool.performance_snapshot()["active"] == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.stop()


@pytest.mark.asyncio
async def test_account_priority_promotion_respects_demand_arrival_order():
    scheduler = MediaScheduler(limit=1, rate=512)
    scope = MediaPriorityScope("preload")
    order = []
    tasks = []

    async def request(label, priority):
        token = media_priority.set(priority)
        try:
            async with scheduler.request():
                order.append(label)
        finally:
            media_priority.reset(token)

    try:
        async with scheduler.request():
            tasks.append(asyncio.create_task(request("shared", scope)))
            await wait_until(lambda: len(scheduler._waiting) == 1)
            tasks.append(asyncio.create_task(request("earlier-demand", "demand")))
            await wait_until(lambda: len(scheduler._waiting) == 2)
            await scope.promote()
            tasks.append(asyncio.create_task(request("later-demand", "demand")))
            await wait_until(lambda: len(scheduler._waiting) == 3)
            assert all(
                row["priority"] == "demand" for row in scheduler.snapshot()["queue"]
            )
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert order == ["earlier-demand", "shared", "later-demand"]
        assert not scope._conditions and not scope._updates
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_manual_preload_yields_next_origin_slot_to_new_demand(tmp_path):
    gate = asyncio.Event()
    payload = b"x" * (8 * BLOCK)
    manager, pool, client = await manager_for(tmp_path, payload, gate)
    await pool.set_media_per_bot(1)
    order = []
    original = client.native.iter_download

    def tracked(target, *, offset, request_size, limit):
        order.append(offset)
        return original(target, offset=offset, request_size=request_size, limit=limit)

    client.native.iter_download = tracked
    demand = None
    try:
        job = await manager.prepare(
            channel_id=CHANNEL, message_id=77, size=len(payload)
        )
        await wait_until(
            lambda: client.native.active > 0 and len(pool._origin_waiters) > 0
        )
        message = await client.get_messages(CHANNEL, message_ids=77)

        async def read():
            stream = stream_pooled_media(
                pool,
                client,
                message,
                channel=CHANNEL,
                offset=5,
                limit=1,
                request_id="playback-seek",
            )
            return b"".join([data async for data in stream])

        demand = asyncio.create_task(read())
        await wait_until(
            lambda: any(
                row["request_id"] == "playback-seek" for row in pool._origin_waiters
            )
        )
        gate.set()
        assert await asyncio.wait_for(demand, 3) == payload[5 * BLOCK : 6 * BLOCK]
        assert (await finish(manager, job))["state"] == "ready"
        assert order[0] // BLOCK in (0, 1)  # One existing background block can finish.
        assert order[1] // BLOCK == 5  # The next free slot belongs to demand.
        assert sorted(offset // BLOCK for offset in order) == list(range(8))
        assert sum(client.native.requests.values()) == 16
    finally:
        gate.set()
        if demand is not None:
            demand.cancel()
            await asyncio.gather(demand, return_exceptions=True)
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_shared_metadata_validation_inherits_demand_without_duplicate_rpc():
    scheduler = MediaScheduler(limit=1, rate=512)
    calls = []

    class MetadataClient:
        async def get_messages(self, _channel, *, message_ids):
            async with scheduler.request():
                calls.append(message_ids)
                return message_ids

    client = MetadataClient()
    tasks = []

    async def read(message_id, priority="demand"):
        token = media_priority.set(priority)
        try:
            return await get_message(client, CHANNEL, message_id)
        finally:
            media_priority.reset(token)

    try:
        async with scheduler.request():
            tasks.append(asyncio.create_task(read(77, "preload")))
            await wait_until(lambda: len(scheduler._waiting) == 1)
            assert scheduler.snapshot()["queue"][0]["priority"] == "preload"
            tasks.append(asyncio.create_task(read(88)))
            await wait_until(lambda: len(scheduler._waiting) == 2)
            tasks.append(asyncio.create_task(read(77)))
            await wait_until(
                lambda: all(
                    row["priority"] == "demand" for row in scheduler.snapshot()["queue"]
                )
            )
        assert await asyncio.wait_for(asyncio.gather(*tasks), 2) == [77, 88, 77]
        assert calls == [88, 77]
        assert media_priority.get() == "demand"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_promoted_pool_waiter_detaches_priority_notifications():
    from tests.media.test_media_scheduler import Account

    pool = ClientPool([Account()], media_per_bot=1)
    scope = MediaPriorityScope("preload")
    await pool.start()
    task = None

    async def wait():
        async with pool.lease(download=True, priority=scope):
            pytest.fail("Occupied slot cannot be acquired")

    try:
        async with pool.lease(download=True):
            task = asyncio.create_task(wait())
            await wait_until(lambda: len(pool._origin_waiters) == 1)
            await scope.promote()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not pool._origin_waiters
            assert not scope._conditions and not scope._updates
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await pool.stop()
