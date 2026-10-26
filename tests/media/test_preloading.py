from __future__ import annotations

import asyncio

import pytest

from tests.media.test_pooled_media import account
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import BLOCK, stream_pooled_media
from tgdrive.media.preloading import PreloadCapacityError, PreloadManager
from tgdrive.telegram.client_pool import ClientPool

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
async def test_one_viewer_replaying_a_segment_does_not_promote_whole_file(tmp_path):
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
        assert job["mode"] == "auto" and job["target_bytes"] == BLOCK
        assert (await finish(manager, job))["state"] == "ready"
        for index in range(4):
            manager.delivered(
                channel_id=CHANNEL, message_id=77, offset=index * BLOCK, length=BLOCK
            )
        await asyncio.sleep(0)
        assert len(manager._jobs) == 1
        assert manager._observed[identity]["hot_until"] == 0
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
        assert row["target_bytes"] == BLOCK
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
        assert BLOCK <= row["target_bytes"] <= 128 * BLOCK
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
    "interval,fill,window", [(1, 2, 8), (1, 0.5, 16), (0.05, 40, 60)]
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
    try:
        identity = manager.observe(channel_id=CHANNEL, message_id=77, size=64 * BLOCK)
        manager._observed[identity]["fill_rate"] = fill * BLOCK
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


@pytest.mark.asyncio
async def test_finished_viewer_history_is_bounded_without_forgetting_active_playback(
    tmp_path,
):
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (8 * BLOCK), auto_preload=True
    )
    try:
        identity = manager.observe(
            channel_id=CHANNEL,
            message_id=77,
            size=8 * BLOCK,
            request_id="still-playing",
        )
        manager.begin(identity, "still-playing")
        for index in range(1024):
            viewer = f"finished-{index}"
            manager.observe(
                channel_id=CHANNEL, message_id=77, size=8 * BLOCK, request_id=viewer
            )
            manager.begin(identity, viewer)
            manager.finished(identity, viewer)
        viewers = manager._observed[identity]["viewers"]
        assert len(viewers) <= 128
        assert viewers["still-playing"]["active"] == 1
        manager.finished(identity, "still-playing")
        assert not any(viewer["active"] for viewer in viewers.values())
        assert not manager._jobs and not manager._auto_tasks
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_disabled_automatic_preloading_keeps_inventory_without_viewer_history(
    tmp_path,
):
    manager, pool, _client = await manager_for(tmp_path, b"x" * (8 * BLOCK))
    try:
        for index in range(1024):
            identity = manager.observe(
                channel_id=CHANNEL, message_id=77, size=8 * BLOCK, request_id=str(index)
            )
            manager.begin(identity, str(index))
            manager.finished(identity, str(index))
        assert not manager._observed
        assert len(manager.cache._files) == 1
        assert not manager._jobs and not manager._auto_tasks
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_concurrent_manual_prepare_admits_one_shared_job(tmp_path, monkeypatch):
    gate = asyncio.Event()
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (4 * BLOCK), gate, disk_max_bytes=64 * BLOCK
    )
    original_capacity = manager._capacity

    async def capacity():
        await asyncio.sleep(0)
        return await original_capacity()

    monkeypatch.setattr(manager, "_capacity", capacity)
    try:
        first, second = await asyncio.gather(
            *(
                manager.prepare(channel_id=CHANNEL, message_id=77, size=4 * BLOCK)
                for _ in range(2)
            )
        )
        assert first["id"] == second["id"]
        assert len(manager._jobs) == 1
        assert len(manager._tasks) == 1
    finally:
        gate.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_ahead_preload_renews_when_playback_reaches_previous_window(tmp_path):
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (20 * BLOCK), auto_preload=True
    )
    manager._clock = lambda: 1.0
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=20 * BLOCK, media_rate=BLOCK / 2
        )
        manager.begin(identity, None)
        for index in range(4):
            manager.delivered(
                channel_id=CHANNEL, message_id=77, offset=index * BLOCK, length=BLOCK
            )
        for _ in range(100):
            if manager._jobs:
                break
            await asyncio.sleep(0.001)
        first = next(iter(manager._jobs.values()))
        assert first["start"] == 4 * BLOCK and first["end"] == 8 * BLOCK
        assert (await finish(manager, first))["state"] == "ready"
        for index in range(4, 8):
            manager.delivered(
                channel_id=CHANNEL, message_id=77, offset=index * BLOCK, length=BLOCK
            )
        for _ in range(100):
            if len(manager._jobs) > 1:
                break
            await asyncio.sleep(0.001)
        assert len(manager._jobs) == 2
        second = list(manager._jobs.values())[-1]
        assert second["start"] == 8 * BLOCK and second["end"] == 12 * BLOCK
        assert (await finish(manager, second))["state"] == "ready"
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_partial_preload_reserves_aligned_blocks_before_admission(tmp_path):
    manager, pool, client = await manager_for(
        tmp_path, b"x" * (2 * BLOCK), memory_max_bytes=BLOCK, disk_max_bytes=0
    )
    try:
        with pytest.raises(PreloadCapacityError, match="budget"):
            await manager.prepare(
                channel_id=CHANNEL,
                message_id=77,
                size=2 * BLOCK,
                start=BLOCK - 1,
                end=BLOCK + 1,
            )
        assert not manager._jobs and not client.native.requests
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_multipart_preload_reserves_a_digest_for_each_telegram_part(tmp_path):
    from tests.media.test_multipart_streaming import build_parts

    contents = [b"one", b"two"]
    parts = build_parts(contents)
    manager, pool, client = await manager_for(
        tmp_path, b"", memory_max_bytes=0, disk_max_bytes=6 + 32
    )
    try:
        with pytest.raises(PreloadCapacityError, match="budget"):
            await manager.prepare(
                channel_id=CHANNEL,
                message_id=parts[0]["message_id"],
                size=6,
                parts=parts,
            )
        assert not manager._jobs and not client.native.requests
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_manual_preload_replaces_full_automatic_job_and_its_authorization(
    tmp_path,
):
    gate = asyncio.Event()
    manager, pool, _client = await manager_for(tmp_path, b"x" * (4 * BLOCK), gate)
    revoked = False

    async def authorize_auto():
        if revoked:
            raise PermissionError("The original viewer's share was revoked")

    try:
        automatic = await manager.prepare(
            channel_id=CHANNEL,
            message_id=77,
            size=4 * BLOCK,
            mode="auto",
            authorize=authorize_auto,
        )
        manual = await manager.prepare(
            channel_id=CHANNEL, message_id=77, size=4 * BLOCK
        )
        assert manual["id"] != automatic["id"] and manual["mode"] == "manual"
        assert manager._jobs[automatic["id"]]["state"] == "cancelled"
        revoked = True
        gate.set()
        assert (await finish(manager, manual))["state"] == "ready"
        assert (await manager.snapshot())["cache"]["files"][0]["complete"]
    finally:
        gate.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocks,complete", [(20, False), (8, True)])
async def test_hot_auto_admission_after_last_viewer_leaves_only_allows_full_file(
    tmp_path, monkeypatch, blocks, complete
):
    manager, pool, client = await manager_for(
        tmp_path, b"x" * (blocks * BLOCK), auto_preload=True
    )
    manager._clock = lambda: 1.0
    at_admission = asyncio.Event()
    release_admission = asyncio.Event()
    original_capacity = manager._capacity
    inspections = 0

    async def capacity():
        nonlocal inspections
        inspections += 1
        if inspections == 2:
            at_admission.set()
            await release_admission.wait()
        return await original_capacity()

    monkeypatch.setattr(manager, "_capacity", capacity)
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=blocks * BLOCK
        )
        # Independent viewers qualify overlapping runs as hot-file demand.
        for viewer in ("first", "second"):
            manager.observe(
                channel_id=CHANNEL,
                message_id=77,
                size=blocks * BLOCK,
                request_id=viewer,
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
        await asyncio.wait_for(at_admission.wait(), 3)
        assert not manager._jobs
        manager.finished(identity, "first")
        manager.finished(identity, "second")
        release_admission.set()
        await asyncio.wait_for(manager._auto_tasks[identity], 3)
        if complete:
            job = next(iter(manager._jobs.values()))
            assert job["target_bytes"] == blocks * BLOCK
            assert (await finish(manager, job))["state"] == "ready"
        else:
            assert not manager._jobs and not client.native.requests
            assert not manager.cache.memory.flights
    finally:
        release_admission.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media_rate,qualification,window",
    [
        (BLOCK / 64, 1, 1),
        (2 * BLOCK, 4, 16),
        (32 * BLOCK, 4, 128),
        (1e308, 4, 128),
    ],
)
async def test_metadata_bitrate_scales_qualification_and_ahead_without_network_bursts(
    tmp_path, media_rate, qualification, window
):
    from unittest.mock import AsyncMock

    from tests.media.test_telegram_cache import message
    from tgdrive.telegram.clients import TelegramMessage

    gate = asyncio.Event()
    size = 256 * BLOCK
    manager, pool, client = await manager_for(
        tmp_path, b"", gate, disk_max_bytes=512 * BLOCK, auto_preload=True
    )
    client.get_messages = AsyncMock(
        return_value=TelegramMessage(client, message(1, size))
    )
    manager._clock = lambda: 1.0
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=size, media_rate=media_rate
        )
        manager.begin(identity, None)
        for index in range(qualification - 1):
            manager.delivered(
                channel_id=CHANNEL, message_id=77, offset=index * BLOCK, length=BLOCK
            )
        await asyncio.sleep(0)
        assert not manager._jobs
        manager.delivered(
            channel_id=CHANNEL,
            message_id=77,
            offset=(qualification - 1) * BLOCK,
            length=BLOCK,
        )
        for _ in range(100):
            if manager._jobs:
                break
            await asyncio.sleep(0.001)
        job = next(iter(manager._jobs.values()))
        assert job["start"] == qualification * BLOCK
        assert job["target_bytes"] == window * BLOCK
        viewer = manager._observed[identity]["viewers"]["default"]
        assert viewer["rate"] == 0
        assert viewer["media_rate"] == media_rate
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "times,expected_rate",
    [
        ([index * 0.01 for index in range(11)], 100 * BLOCK),
        ([0, 1, 1.001, 1.002, 1.003, 2], 1.75 * BLOCK),
        ([index * 0.00001 for index in range(11)], 0),
        ([0, 0.1, 50], 0),
    ],
)
async def test_fallback_rate_aggregates_fast_chunks_and_bursts_but_rejects_idle_gaps(
    tmp_path, times, expected_rate
):
    manager, pool, _client = await manager_for(tmp_path, b"", auto_preload=True)
    now = [0.0]
    manager._clock = lambda: now[0]
    try:
        identity = manager.observe(channel_id=CHANNEL, message_id=77, size=64 * BLOCK)
        manager.begin(identity, None)
        for index, at in enumerate(times):
            now[0] = at
            manager.delivered(
                channel_id=CHANNEL, message_id=77, offset=index * BLOCK, length=BLOCK
            )
        viewer = manager._observed[identity]["viewers"]["default"]
        assert viewer["rate"] == pytest.approx(expected_rate)
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_playback_seek_resets_delivery_rate_without_affecting_other_viewer(
    tmp_path,
):
    manager, pool, _client = await manager_for(tmp_path, b"", auto_preload=True)
    now = [0.0]
    manager._clock = lambda: now[0]
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=64 * BLOCK, request_id="first"
        )
        manager.observe(
            channel_id=CHANNEL, message_id=77, size=64 * BLOCK, request_id="second"
        )
        manager.begin(identity, "first")
        manager.begin(identity, "second")
        for index in range(3):
            now[0] = index
            for viewer in ("first", "second"):
                manager.delivered(
                    channel_id=CHANNEL,
                    message_id=77,
                    offset=index * BLOCK,
                    length=BLOCK,
                    request_id=viewer,
                )
        now[0] += 0.000001
        manager.delivered(
            channel_id=CHANNEL,
            message_id=77,
            offset=40 * BLOCK,
            length=BLOCK,
            request_id="first",
        )
        viewers = manager._observed[identity]["viewers"]
        assert viewers["first"]["rate"] == 0
        assert viewers["second"]["rate"] == BLOCK
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_fill_rate_for_another_file_does_not_change_playback_ahead(tmp_path):
    gate = asyncio.Event()
    manager, pool, _client = await manager_for(
        tmp_path,
        b"x" * (64 * BLOCK),
        gate,
        disk_max_bytes=256 * BLOCK,
        auto_preload=True,
    )
    manager._clock = lambda: 1.0
    try:
        identities = []
        for message_id, fill in ((77, BLOCK / 2), (88, 2 * BLOCK)):
            identity = manager.observe(
                channel_id=CHANNEL,
                message_id=message_id,
                size=64 * BLOCK,
                media_rate=BLOCK,
            )
            identities.append(identity)
            manager._observed[identity]["fill_rate"] = fill
            manager.begin(identity, None)
            for index in range(4):
                manager.delivered(
                    channel_id=CHANNEL,
                    message_id=message_id,
                    offset=index * BLOCK,
                    length=BLOCK,
                )
        for _ in range(100):
            if len(manager._jobs) == 2:
                break
            await asyncio.sleep(0.001)
        targets = {
            job["file_id"]: job["target_bytes"] for job in manager._jobs.values()
        }
        assert targets == {identities[0]: 16 * BLOCK, identities[1]: 8 * BLOCK}
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_anonymous_player_requests_do_not_qualify_as_distinct_full_file_viewers(
    tmp_path,
):
    manager, pool, _client = await manager_for(tmp_path, b"", auto_preload=True)
    try:
        for index in range(3):
            viewer = f"anonymous-http-{index}"
            identity = manager.observe(
                channel_id=CHANNEL,
                message_id=77,
                size=8 * BLOCK,
                request_id=viewer,
                allow_full=False,
            )
            manager.begin(identity, viewer)
            for block in range(4):
                manager.delivered(
                    channel_id=CHANNEL,
                    message_id=77,
                    offset=block * BLOCK,
                    length=BLOCK,
                    request_id=viewer,
                )
        assert manager._observed[identity]["hot_until"] == 0
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_independent_playback_runs_can_qualify_after_growing_into_overlap(
    tmp_path,
):
    manager, pool, _client = await manager_for(tmp_path, b"", auto_preload=True)
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=16 * BLOCK, request_id="first"
        )
        for viewer, start in (("first", 0), ("second", 8)):
            manager.observe(
                channel_id=CHANNEL, message_id=77, size=16 * BLOCK, request_id=viewer
            )
            manager.begin(identity, viewer)
            for index in range(start, start + 4):
                manager.delivered(
                    channel_id=CHANNEL,
                    message_id=77,
                    offset=index * BLOCK,
                    length=BLOCK,
                    request_id=viewer,
                )
        assert manager._observed[identity]["hot_until"] == 0
        for index in range(4, 12):
            manager.delivered(
                channel_id=CHANNEL,
                message_id=77,
                offset=index * BLOCK,
                length=BLOCK,
                request_id="first",
            )
        assert manager._observed[identity]["hot_until"] > manager._clock()
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("media_rate", [0, -1, float("nan"), float("inf"), True, "60"])
async def test_invalid_media_rate_hint_is_ignored(tmp_path, media_rate):
    manager, pool, _client = await manager_for(tmp_path, b"", auto_preload=True)
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=8 * BLOCK, media_rate=media_rate
        )
        assert manager._observed[identity]["viewers"]["default"]["media_rate"] is None
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["seek", "owner_leaves"])
async def test_pending_ahead_admission_rechecks_its_own_viewer_after_capacity_wait(
    tmp_path, monkeypatch, change
):
    manager, pool, client = await manager_for(tmp_path, b"", auto_preload=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_capacity = manager._capacity
    inspections = 0

    async def capacity():
        nonlocal inspections
        inspections += 1
        if inspections == 2:
            entered.set()
            await release.wait()
        return await original_capacity()

    monkeypatch.setattr(manager, "_capacity", capacity)
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=64 * BLOCK, request_id="first"
        )
        manager.observe(
            channel_id=CHANNEL, message_id=77, size=64 * BLOCK, request_id="second"
        )
        manager.begin(identity, "first")
        manager.begin(identity, "second")
        for index in range(4):
            manager.delivered(
                channel_id=CHANNEL,
                message_id=77,
                offset=index * BLOCK,
                length=BLOCK,
                request_id="first",
            )
        await asyncio.wait_for(entered.wait(), 3)
        pending = manager._auto_tasks[identity]
        if change == "seek":
            manager.delivered(
                channel_id=CHANNEL,
                message_id=77,
                offset=40 * BLOCK,
                length=BLOCK,
                request_id="first",
            )
        else:
            manager.finished(identity, "first")
        release.set()
        await asyncio.wait_for(pending, 3)
        assert not manager._jobs and not client.native.requests
        assert manager._observed[identity]["viewers"]["second"]["active"] == 1
    finally:
        release.set()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_ahead_owner_leaving_cancels_work_while_another_viewer_remains(tmp_path):
    gate = asyncio.Event()
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (64 * BLOCK), gate, auto_preload=True
    )
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=64 * BLOCK, request_id="first"
        )
        manager.observe(
            channel_id=CHANNEL, message_id=77, size=64 * BLOCK, request_id="second"
        )
        manager.begin(identity, "first")
        manager.begin(identity, "second")
        for index in range(4):
            manager.delivered(
                channel_id=CHANNEL,
                message_id=77,
                offset=index * BLOCK,
                length=BLOCK,
                request_id="first",
            )
        for _ in range(100):
            if manager._jobs:
                break
            await asyncio.sleep(0.001)
        job = next(iter(manager._jobs.values()))
        manager.finished(identity, "first")
        task = manager._tasks.get(job["id"])
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        assert job["state"] == "cancelled"
        assert manager._observed[identity]["viewers"]["second"]["active"] == 1
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_completed_range_grace_expires_and_cancels_unfinished_ahead(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("tgdrive.media.preloading.PLAYBACK_RANGE_GRACE_SECONDS", 0.03)
    gate = asyncio.Event()
    manager, pool, client = await manager_for(
        tmp_path, b"x" * (16 * BLOCK), gate, auto_preload=True
    )
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=16 * BLOCK, request_id="player"
        )
        manager.begin(identity, "player")
        for index in range(4):
            manager.delivered(
                channel_id=CHANNEL,
                message_id=77,
                offset=index * BLOCK,
                length=BLOCK,
                request_id="player",
            )
        manager.finished(identity, "player", completed=True)
        for _ in range(100):
            if manager._jobs:
                break
            await asyncio.sleep(0.001)
        job = next(iter(manager._jobs.values()))
        assert manager._viewer_timers
        for _ in range(100):
            if job["state"] == "cancelled":
                break
            await asyncio.sleep(0.001)
        assert job["state"] == "cancelled"
        viewer = manager._observed[identity]["viewers"]["player"]
        assert viewer["active"] == 0 and viewer["warm_until"] == 0
        assert not manager._viewer_timers
        assert not manager.cache.memory.flights and not client.native.active
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_new_player_request_abort_revokes_completed_range_grace_immediately(
    tmp_path,
):
    gate = asyncio.Event()
    manager, pool, _client = await manager_for(
        tmp_path, b"x" * (16 * BLOCK), gate, auto_preload=True
    )
    try:
        identity = manager.observe(
            channel_id=CHANNEL, message_id=77, size=16 * BLOCK, request_id="player"
        )
        manager.begin(identity, "player")
        for index in range(4):
            manager.delivered(
                channel_id=CHANNEL,
                message_id=77,
                offset=index * BLOCK,
                length=BLOCK,
                request_id="player",
            )
        manager.finished(identity, "player", completed=True)
        for _ in range(100):
            if manager._jobs:
                break
            await asyncio.sleep(0.001)
        job = next(iter(manager._jobs.values()))
        manager.begin(identity, "player")
        manager.finished(identity, "player")
        task = manager._tasks.get(job["id"])
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        assert job["state"] == "cancelled"
        assert not manager._viewer_timers
        assert not manager._viewer_active(
            manager._observed[identity]["viewers"]["player"]
        )
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_cache_shutdown_disposes_completed_player_range_timers(tmp_path):
    manager, pool, _client = await manager_for(tmp_path, b"", auto_preload=True)
    try:
        identity = manager.observe(channel_id=CHANNEL, message_id=77, size=BLOCK)
        manager.begin(identity, None)
        manager.finished(identity, None, completed=True)
        timers = list(manager._viewer_timers.values())
        assert timers and all(not timer.cancelled() for timer in timers)
        await manager.close()
        assert not manager._viewer_timers
        assert all(timer.cancelled() for timer in timers)
    finally:
        await manager.close()
        await pool.stop()
