from __future__ import annotations

import asyncio

import pytest
from test_pooled_media import account

from tgdrive.client_pool import ClientPool
from tgdrive.media_policy import MediaCachePolicy
from tgdrive.media_reader import BLOCK, stream_pooled_media
from tgdrive.preloading import PreloadCapacityError, PreloadManager

CHANNEL = -1000000000123


async def manager_for(tmp_path, payload, gate=None, **changes):
    client = account("bot", payload, gate)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    policy = MediaCachePolicy(
        **(
            {
                "memory_max_bytes": 8 * BLOCK,
                "disk_max_bytes": 32 * BLOCK,
                "min_free_bytes": 0,
            }
            | changes
        )
    )
    return PreloadManager(pool, policy=policy), pool, client


async def finish(manager, job):
    task = manager._tasks.get(job["id"])
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 3)
    return manager._jobs[job["id"]]


@pytest.mark.asyncio
async def test_manual_prepare_ready_and_foreground_reuses_every_block(tmp_path):
    payload = b"a" * (6 * BLOCK) + b"tail"
    manager, pool, client = await manager_for(tmp_path, payload)
    try:
        job = await manager.prepare(
            channel_id=CHANNEL, message_id=77, size=len(payload), name="movie.mp4"
        )
        assert (await finish(manager, job))["state"] == "ready"
        state = await manager.snapshot()
        row = state["cache"]["files"][0]
        assert row["complete"] and row["ready_ranges"] == [[0, len(payload)]]
        requests = sum(client.native.requests.values())
        message = await client.get_messages(CHANNEL, message_ids=77)
        stream = stream_pooled_media(
            pool, client, message, channel=CHANNEL, offset=0, limit=7
        )
        assert b"".join([chunk async for chunk in stream]) == payload
        assert sum(client.native.requests.values()) == requests
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_cancel_preload_preserves_other_viewer_shared_fills(tmp_path):
    gate = asyncio.Event()
    payload = b"x" * (3 * BLOCK)
    manager, pool, client = await manager_for(tmp_path, payload, gate)
    try:
        job = await manager.prepare(
            channel_id=CHANNEL, message_id=77, size=len(payload)
        )
        for _ in range(100):
            if client.native.active:
                break
            await asyncio.sleep(0.001)
        message = await client.get_messages(CHANNEL, message_ids=77)

        async def consume():
            stream = stream_pooled_media(
                pool, client, message, channel=CHANNEL, offset=0, limit=3
            )
            return b"".join([chunk async for chunk in stream])

        reader = asyncio.create_task(consume())
        for _ in range(100):
            if any(
                flight.waiters > 1 for flight in manager.cache.memory.flights.values()
            ):
                break
            await asyncio.sleep(0.001)
        assert any(
            flight.waiters > 1 for flight in manager.cache.memory.flights.values()
        )
        assert (await manager.cancel(job["id"]))["state"] == "cancelled"
        gate.set()
        assert await asyncio.wait_for(reader, 3) == payload
        assert sum(client.native.requests.values()) == 6
        assert all(status.active_leases == 0 for status in await pool.status())
    finally:
        gate.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_head_and_metadata_observation_never_trigger_preload(tmp_path):
    manager, pool, client = await manager_for(
        tmp_path, b"x" * (8 * BLOCK), auto_preload=True
    )
    try:
        manager.observe(
            channel_id=CHANNEL, message_id=77, size=8 * BLOCK, method="HEAD"
        )
        for index in range(5):
            manager.delivered(
                channel_id=CHANNEL, message_id=77, offset=index * BLOCK, length=BLOCK
            )
        await asyncio.sleep(0)
        assert not manager._jobs and not manager._auto_tasks
        assert not client.native.requests
        manager.observe(channel_id=CHANNEL, message_id=77, size=8 * BLOCK, method="GET")
        await asyncio.sleep(0)
        assert not manager._jobs
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_auto_completes_hot_file_only_after_sequential_delivery(tmp_path):
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (8 * BLOCK), auto_preload=True
    )
    try:
        identity = manager.observe(channel_id=CHANNEL, message_id=77, size=8 * BLOCK)
        manager.begin(identity, None)
        for index in range(3):
            manager.delivered(
                channel_id=CHANNEL, message_id=77, offset=index * BLOCK, length=BLOCK
            )
        await asyncio.sleep(0)
        assert not manager._jobs
        manager.delivered(
            channel_id=CHANNEL, message_id=77, offset=3 * BLOCK, length=BLOCK
        )
        for _ in range(100):
            if manager._jobs:
                break
            await asyncio.sleep(0.001)
        job = next(iter(manager._jobs.values()))
        assert job["mode"] == "auto" and job["target_bytes"] == 4 * BLOCK
        assert (await finish(manager, job))["state"] == "ready"
        for index in range(4):
            manager.delivered(
                channel_id=CHANNEL, message_id=77, offset=index * BLOCK, length=BLOCK
            )
        for _ in range(100):
            if len(manager._jobs) == 2:
                break
            await asyncio.sleep(0.001)
        replay = list(manager._jobs.values())[-1]
        assert replay["target_bytes"] == 8 * BLOCK
        assert (await finish(manager, replay))["state"] == "ready"
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_manual_whole_file_refuses_capacity_and_combined_reservations(tmp_path):
    gate = asyncio.Event()
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (6 * BLOCK), gate, memory_max_bytes=0, disk_max_bytes=8 * BLOCK
    )
    try:
        with pytest.raises(PreloadCapacityError, match="budget"):
            await manager.prepare(channel_id=CHANNEL, message_id=77, size=9 * BLOCK)
        await manager.prepare(channel_id=CHANNEL, message_id=77, size=6 * BLOCK)
        with pytest.raises(PreloadCapacityError, match="reserved"):
            await manager.prepare(channel_id=CHANNEL, message_id=88, size=6 * BLOCK)
    finally:
        gate.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_disk_failure_never_claims_whole_file_ready(tmp_path, monkeypatch):
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (4 * BLOCK), memory_max_bytes=0
    )
    monkeypatch.setattr(manager.cache.disk, "write", lambda *args: False)
    try:
        job = await manager.prepare(channel_id=CHANNEL, message_id=77, size=4 * BLOCK)
        row = await finish(manager, job)
        assert row["state"] == "failed" and "capacity" in row["error"].lower()
        assert (await manager.snapshot())["cache"]["files"] == []
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_revoke_during_preparation_stops_with_no_leaked_leases(tmp_path):
    manager, pool, _client = await manager_for(tmp_path, b"x" * (8 * BLOCK))
    calls = 0

    async def authorize():
        nonlocal calls
        calls += 1
        if calls >= 5:
            raise PermissionError("Share was revoked")

    try:
        job = await manager.prepare(
            channel_id=CHANNEL, message_id=77, size=8 * BLOCK, authorize=authorize
        )
        row = await finish(manager, job)
        assert row["state"] == "failed" and "revoked" in row["error"]
        assert all(status.active_leases == 0 for status in await pool.status())
        assert not manager.cache.memory.flights
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_budget_shrink_during_preparation_is_not_false_ready(tmp_path):
    gate = asyncio.Event()
    manager, pool, client = await manager_for(tmp_path, b"x" * (4 * BLOCK), gate)
    try:
        job = await manager.prepare(channel_id=CHANNEL, message_id=77, size=4 * BLOCK)
        for _ in range(100):
            if client.native.active:
                break
            await asyncio.sleep(0.001)
        await manager.reconfigure(
            MediaCachePolicy(memory_max_bytes=0, disk_max_bytes=0, min_free_bytes=0)
        )
        gate.set()
        row = await finish(manager, job)
        assert row["state"] == "failed"
        state = await manager.snapshot()
        assert state["cache"]["files"] == []
    finally:
        gate.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_far_seek_cancels_obsolete_automatic_ahead_job(tmp_path):
    gate = asyncio.Event()
    manager, pool, _client = await manager_for(
        tmp_path,
        b"x" * (64 * BLOCK),
        gate,
        memory_max_bytes=4 * BLOCK,
        disk_max_bytes=16 * BLOCK,
        auto_preload=True,
    )
    try:
        identity = manager.observe(channel_id=CHANNEL, message_id=77, size=64 * BLOCK)
        manager.begin(identity, None)
        for index in range(4):
            manager.delivered(
                channel_id=CHANNEL, message_id=77, offset=index * BLOCK, length=BLOCK
            )
        for _ in range(100):
            if manager._jobs:
                break
            await asyncio.sleep(0.001)
        row = next(iter(manager._jobs.values()))
        assert row["target_bytes"] == 4 * BLOCK
        manager.delivered(
            channel_id=CHANNEL, message_id=77, offset=48 * BLOCK, length=BLOCK
        )
        task = manager._tasks.get(row["id"])
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        assert row["state"] == "cancelled"
        assert not manager.cache.memory.flights
    finally:
        gate.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_explicit_full_prepare_can_replace_old_protected_bytes(tmp_path):
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (8 * BLOCK), memory_max_bytes=0, disk_max_bytes=10 * BLOCK
    )
    try:
        for index in range(6):
            old = (CHANNEL, 88, "document", 999, 6 * BLOCK, index)
            manager.cache.disk.write(old, b"o" * BLOCK)
            manager.cache.disk.promote(old)
        job = await manager.prepare(channel_id=CHANNEL, message_id=77, size=8 * BLOCK)
        assert (await finish(manager, job))["state"] == "ready"
        assert (await manager.snapshot())["cache"]["files"][0]["complete"]
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_first_cold_view_only_preloads_ahead_and_stops_when_viewer_leaves(
    tmp_path,
):
    gate = asyncio.Event()
    manager, pool, _client = await manager_for(
        tmp_path,
        b"x" * (64 * BLOCK),
        gate,
        disk_max_bytes=256 * BLOCK,
        auto_preload=True,
    )
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=64 * BLOCK, request_id="first-view"
        )
        manager.begin(identity, "first-view")
        for index in range(4):
            manager.delivered(
                channel_id=CHANNEL,
                message_id=77,
                offset=index * BLOCK,
                length=BLOCK,
                request_id="first-view",
            )
        for _ in range(100):
            if manager._jobs:
                break
            await asyncio.sleep(0.001)
        row = next(iter(manager._jobs.values()))
        assert 4 * BLOCK <= row["target_bytes"] <= 32 * BLOCK
        assert row["target_bytes"] < row["size"]
        manager.finished(identity, "first-view")
        task = manager._tasks.get(row["id"])
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        assert row["state"] == "cancelled"
    finally:
        gate.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interval,fill,window", [(1, 2, 8), (1, 0.5, 16), (0.05, 40, 32)]
)
async def test_ahead_window_uses_consumption_and_fill_speed(
    tmp_path, interval, fill, window
):
    gate = asyncio.Event()
    manager, pool, _client = await manager_for(
        tmp_path,
        b"x" * (64 * BLOCK),
        gate,
        disk_max_bytes=256 * BLOCK,
        auto_preload=True,
    )
    now = [0.0]
    manager._clock = lambda: now[0]
    manager._fill_rate = fill * BLOCK
    try:
        identity = manager.observe(channel_id=CHANNEL, message_id=77, size=64 * BLOCK)
        manager.begin(identity, None)
        for index in range(4):
            now[0] = index * interval
            manager.delivered(
                channel_id=CHANNEL, message_id=77, offset=index * BLOCK, length=BLOCK
            )
        for _ in range(100):
            if manager._jobs:
                break
            await asyncio.sleep(0.001)
        row = next(iter(manager._jobs.values()))
        assert row["start"] == 4 * BLOCK
        assert row["target_bytes"] == window * BLOCK
    finally:
        gate.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_two_qualified_overlapping_viewers_earn_full_completion(tmp_path):
    gate = asyncio.Event()
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (8 * BLOCK), gate, auto_preload=True
    )
    try:
        identity = None
        for viewer in ("friend-one", "friend-two"):
            identity = manager.observe(
                channel_id=CHANNEL, message_id=77, size=8 * BLOCK, request_id=viewer
            )
            manager.begin(identity, viewer)
            for index in range(4):
                manager.delivered(
                    channel_id=CHANNEL,
                    message_id=77,
                    offset=index * BLOCK,
                    length=BLOCK,
                    request_id=viewer,
                )
            for _ in range(100):
                expected = 1 if viewer == "friend-one" else 2
                if len(manager._jobs) >= expected:
                    break
                await asyncio.sleep(0.001)
        full = list(manager._jobs.values())[-1]
        assert full["target_bytes"] == 8 * BLOCK
        manager.finished(identity, "friend-one")
        manager.finished(identity, "friend-two")
        gate.set()
        assert (await finish(manager, full))["state"] == "ready"
    finally:
        gate.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_manual_prepare_exact_ram_budget_does_not_charge_disk_digest_overhead(
    tmp_path,
):
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (4 * BLOCK), memory_max_bytes=4 * BLOCK, disk_max_bytes=0
    )
    try:
        job = await manager.prepare(channel_id=CHANNEL, message_id=77, size=4 * BLOCK)
        assert (await finish(manager, job))["state"] == "ready"
        state = await manager.snapshot()
        assert state["cache"]["files"][0]["complete"]
        assert state["cache"]["memory"]["bytes"] == 4 * BLOCK
    finally:
        await manager.close()
        await pool.stop()
