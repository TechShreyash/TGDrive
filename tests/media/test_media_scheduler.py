from __future__ import annotations

import asyncio
import time

import pytest

from tgdrive.media.media_scheduler import MediaScheduler, media_priority
from tgdrive.telegram.client_pool import ClientPool, MediaQueueFull


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


@pytest.mark.asyncio
async def test_batch_queue_estimates_keep_priority_counts_and_active_only_fields():
    pool = ClientPool([Account()], media_per_bot=2)
    await pool.start()
    tasks = []

    async def waiting(identity, priority):
        async with pool.lease(download=True, request_id=identity, priority=priority):
            pass

    try:
        async with (
            pool.lease(media=True, request_id="shared"),
            pool.lease(download=True, request_id="active-only"),
        ):
            try:
                for identity, priority in (
                    ("preload", "preload"),
                    ("shared", "demand"),
                    ("shared", "demand"),
                    ("other", "demand"),
                ):
                    tasks.append(asyncio.create_task(waiting(identity, priority)))
                    await wait_until(
                        lambda: pool.performance_snapshot()["queued"] == len(tasks)
                    )
                pool._origin_durations.append(2)
                before = pool.performance_snapshot()
                estimates = pool.queue_estimates(
                    ["shared", "other", "preload", "active-only", "missing", "shared"]
                )
                assert set(estimates) == {"shared", "other", "preload", "active-only"}
                shared = estimates["shared"]
                assert shared["position"] == 1
                assert shared["priority"] == "demand"
                assert shared["active_operations"] == 1
                assert shared["queued_operations"] == 2
                assert shared["estimated_wait_seconds"] == 2
                assert shared["waiting_seconds"] >= 0
                assert estimates["other"]["position"] == 3
                assert estimates["other"]["estimated_wait_seconds"] == 4
                assert estimates["preload"]["position"] == 4
                assert estimates["preload"]["priority"] == "preload"
                assert estimates["active-only"] == {
                    "request_id": "active-only",
                    "position": 0,
                    "estimated_wait_seconds": 0.0,
                    "active_operations": 1,
                    "queued_operations": 0,
                    "estimates": True,
                }
                assert set(pool.queue_estimates(["shared"])) == {"shared"}
                assert pool.queue_estimate("missing") is None
                assert pool.queue_estimates([]) == {}
                after = pool.performance_snapshot()
                assert after["active"] == before["active"]
                assert [row["request_id"] for row in after["queue"]] == [
                    row["request_id"] for row in before["queue"]
                ]
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_configured_media_capacity_allows_four_reads_and_bounds_fifth():
    pool = ClientPool([Account()], media_per_bot=4)
    await pool.start()
    release = asyncio.Event()
    acquired = []

    async def read(index):
        async with pool.lease(download=True):
            acquired.append(index)
            await release.wait()

    tasks = [asyncio.create_task(read(index)) for index in range(5)]
    try:
        await wait_until(lambda: len(acquired) == 4)
        snapshot = pool.performance_snapshot()
        assert await pool.download_capacity() == 4
        assert snapshot["accounts"][0]["downloads"] == 4
        assert snapshot["active"] == 4 and snapshot["queued"] == 1
    finally:
        release.set()
        await asyncio.gather(*tasks)
        await pool.stop()


@pytest.mark.asyncio
async def test_waiting_upload_gets_released_combined_slot_before_later_read():
    pool = ClientPool([Account()], media_per_bot=4)
    await pool.start()
    gates = [asyncio.Event() for _ in range(5)]
    order = []

    async def work(label, gate=None, *, upload=False):
        async with pool.lease(media=upload, download=not upload):
            order.append(label)
            if gate is not None:
                await gate.wait()

    readers = [
        asyncio.create_task(work(str(index), gates[index])) for index in range(4)
    ]
    upload = later = None
    try:
        await wait_until(lambda: len(order) == 4)
        upload = asyncio.create_task(work("upload", gates[4], upload=True))
        await wait_until(lambda: pool.performance_snapshot()["queued"] == 1)
        later = asyncio.create_task(work("later-read"))
        await wait_until(lambda: pool.performance_snapshot()["queued"] == 2)
        gates[0].set()
        await wait_until(lambda: "upload" in order)
        assert "later-read" not in order
        account = pool.performance_snapshot()["accounts"][0]
        assert account["downloads"] == 3 and account["uploads"] == 1
        gates[4].set()
        await wait_until(lambda: "later-read" in order)
        assert order.index("upload") < order.index("later-read")
    finally:
        for gate in gates:
            gate.set()
        await asyncio.gather(
            *readers, *[task for task in (upload, later) if task is not None]
        )
        await pool.stop()


@pytest.mark.asyncio
async def test_demand_upload_overtakes_preload_upload_without_fifo_deadlock():
    pool = ClientPool([Account()], media_per_bot=1)
    await pool.start()
    release = asyncio.Event()
    order = []

    async def work(label, priority="demand"):
        async with pool.lease(media=True, priority=priority):
            order.append(label)
            if label == "active":
                await release.wait()

    active = asyncio.create_task(work("active"))
    await wait_until(lambda: order == ["active"])
    preload = asyncio.create_task(work("preload", "preload"))
    await wait_until(lambda: pool.performance_snapshot()["queued"] == 1)
    demand = asyncio.create_task(work("demand"))
    await wait_until(lambda: pool.performance_snapshot()["queued"] == 2)
    try:
        release.set()
        await asyncio.wait_for(asyncio.gather(active, preload, demand), 2)
        assert order == ["active", "demand", "preload"]
    finally:
        release.set()
        for task in (active, preload, demand):
            task.cancel()
        await asyncio.gather(active, preload, demand, return_exceptions=True)
        await pool.stop()


@pytest.mark.asyncio
async def test_channel_upload_cooldown_does_not_strand_ready_read_lane():
    pool = ClientPool([Account()], media_per_bot=4)
    await pool.start()
    await pool.defer_channel(time.time() + 60)

    async def upload():
        async with pool.lease(media=True):
            pytest.fail("Channel cooldown must still gate uploads")

    pending = asyncio.create_task(upload())
    try:
        await wait_until(lambda: pool.performance_snapshot()["queued"] == 1)
        async with asyncio.timeout(1):
            async with pool.lease(download=True):
                assert pool.performance_snapshot()["active"] == 1
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await pool.stop()


@pytest.mark.asyncio
async def test_upload_queue_estimate_includes_known_channel_cooldown():
    pool = ClientPool([Account()], media_per_bot=3)
    await pool.start()
    pool._origin_durations.append(2.0)
    await pool.defer_channel(time.time() + 60)

    async def upload():
        async with pool.lease(media=True, request_id="cooling-upload"):
            pytest.fail("Channel cooldown must still gate this upload")

    pending = asyncio.create_task(upload())
    try:
        await wait_until(lambda: pool.performance_snapshot()["queued"] == 1)
        estimate = pool.queue_estimate("cooling-upload")
        assert 59 <= estimate["estimated_wait_seconds"] <= 60
        owner_queue = pool.performance_snapshot()["queue"][0]
        assert owner_queue["request_id"] == "cooling-upload"
        assert 59 <= owner_queue["estimated_wait_seconds"] <= 60
        # Upload-only cooldowns cannot add latency to a ready download lane.
        async with pool.lease(download=True, request_id="ready-read"):
            assert pool.queue_estimate("ready-read")["estimated_wait_seconds"] == 0
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await pool.stop()
