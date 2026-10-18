from __future__ import annotations

import asyncio

import pytest

from tgdrive.client_pool import ClientPool, MediaQueueFull
from tgdrive.media_scheduler import MediaScheduler, media_priority


class Account:
    name = "storage-bot-1"
    is_connected = True

    async def start(self):
        pass

    async def stop(self):
        pass


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110 - bounded scheduler-state observation.
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_account_requests_bound_concurrency_and_prioritize_demand():
    scheduler = MediaScheduler(limit=1, rate=512)
    release = asyncio.Event()
    order = []

    async def request(label, priority):
        token = media_priority.set(priority)
        try:
            async with scheduler.request():
                order.append(label)
                if label == "active":
                    await release.wait()
        finally:
            media_priority.reset(token)

    active = asyncio.create_task(request("active", "demand"))
    await wait_until(lambda: order == ["active"])
    preload = asyncio.create_task(request("preload", "preload"))
    await wait_until(lambda: len(scheduler.snapshot()["queue"]) == 1)
    demand = asyncio.create_task(request("demand", "demand"))
    await wait_until(lambda: len(scheduler.snapshot()["queue"]) == 2)
    assert scheduler.snapshot()["queue"][0]["priority"] == "demand"
    release.set()
    await asyncio.gather(active, preload, demand)
    assert order == ["active", "demand", "preload"]
    assert scheduler.active == 0
    assert scheduler.completed == 3


@pytest.mark.asyncio
async def test_cancelled_waiter_is_removed_and_flood_reduces_shared_budget():
    scheduler = MediaScheduler(limit=8, rate=24)
    async with scheduler.request():
        await scheduler.flood(60)
        waiter = asyncio.create_task(scheduler.request().__aenter__())
        await wait_until(lambda: len(scheduler.snapshot()["queue"]) == 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        snapshot = scheduler.snapshot()
        assert snapshot["queue"] == []
        assert snapshot["effective_limit"] == 4
        assert snapshot["effective_parts_per_second"] == 12
        assert snapshot["cooldown_seconds"] > 59
    assert scheduler.active == 0


@pytest.mark.asyncio
async def test_pool_combined_cap_demand_priority_and_metadata_independence():
    pool = ClientPool([Account()], media_per_bot=1)
    await pool.start()
    release = asyncio.Event()
    order = []

    async def work(label, *, download=False, priority="demand"):
        async with pool.lease(media=not download, download=download, priority=priority):
            order.append(label)
            if label == "upload":
                await release.wait()

    upload = asyncio.create_task(work("upload"))
    await wait_until(lambda: order == ["upload"])
    preload = asyncio.create_task(work("preload", download=True, priority="preload"))
    await wait_until(lambda: pool.performance_snapshot()["queued"] == 1)
    demand = asyncio.create_task(work("demand", download=True))
    await wait_until(lambda: pool.performance_snapshot()["queued"] == 2)
    async with pool.lease():
        assert pool.performance_snapshot()["active"] == 1
    assert pool.performance_snapshot()["queue"][0]["priority"] == "demand"
    release.set()
    await asyncio.gather(upload, preload, demand)
    assert order == ["upload", "demand", "preload"]
    assert pool.performance_snapshot()["active"] == 0
    assert pool.performance_snapshot()["mean_activity_seconds"] is not None
    await pool.stop()


@pytest.mark.asyncio
async def test_pool_cancelled_preload_does_not_leak_queue_or_context():
    pool = ClientPool([Account()], media_per_bot=1)
    await pool.start()

    async def queued():
        async with pool.lease(download=True, priority="preload"):
            pytest.fail("occupied origin slot must not be acquired")

    async with pool.lease(media=True):
        task = asyncio.create_task(queued())
        await wait_until(lambda: pool.performance_snapshot()["queued"] == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.performance_snapshot()["queue"] == []
        assert media_priority.get() == "demand"
    await pool.stop()


@pytest.mark.asyncio
async def test_bounded_origin_queue_is_retryable_and_does_not_block_metadata():
    pool = ClientPool([Account()], media_per_bot=1, max_media_queue=1)
    await pool.start()

    async def waiting():
        async with pool.lease(download=True, request_id="visitor-range"):
            pass

    async with pool.lease(media=True, request_id="active-upload"):
        task = asyncio.create_task(waiting())
        await wait_until(lambda: pool.performance_snapshot()["queued"] == 1)
        estimate = pool.queue_estimate("visitor-range")
        assert estimate["position"] == 1
        assert estimate["estimated_wait_seconds"] is None
        assert pool.queue_estimate("active-upload")["active_operations"] == 1
        with pytest.raises(MediaQueueFull) as error:
            async with pool.lease(download=True):
                pytest.fail("a full queue cannot admit another origin request")
        assert error.value.code == "TELEGRAM_QUEUE_FULL"
        assert error.value.retry_after >= 1
        async with pool.lease():
            pass
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert pool.queue_estimate("visitor-range") is None
    assert pool.performance_snapshot()["active_requests"] == []
    await pool.stop()
