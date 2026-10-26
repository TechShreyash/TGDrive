from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from tgdrive.media.media_cache import DiskMediaCache
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import BLOCK, MediaReadCache, _PayloadCache

CHANNEL = -1000000000123


def key(index=0, size=3 * BLOCK, message=77):
    return (CHANNEL, message, "document", message + 100, size, index)


@pytest.mark.asyncio
async def test_download_during_inventory_write_is_persisted_before_close(
    tmp_path, monkeypatch
):
    cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(memory_max_bytes=8 * BLOCK, min_free_bytes=0),
    )
    first = cache.register_file(channel_id=CHANNEL, message_id=77, size=3, name="First")
    second = cache.register_file(
        channel_id=CHANNEL, message_id=88, size=3, name="Second"
    )
    started, release = threading.Event(), threading.Event()
    original = cache._write_index
    writes = 0

    def delayed_write(value):
        nonlocal writes
        writes += 1
        if writes == 1:
            # Freeze the state as a real writer can do before a later download
            # changes the cache while the disk operation is still in progress.
            value = json.loads(json.dumps(value))
            started.set()
            assert release.wait(5)
        original(value)

    monkeypatch.setattr(cache, "_write_index", delayed_write)
    try:
        await cache.get(key(size=3), 3, lambda: asyncio.sleep(0, result=b"one"))
        assert await asyncio.to_thread(started.wait, 2)
        await cache.get(
            key(size=3, message=88), 3, lambda: asyncio.sleep(0, result=b"two")
        )
        release.set()
        await cache.close()
        cache = MediaReadCache(tmp_path, cache.policy)
        files = {row["id"]: row for row in (await cache.snapshot())["files"]}
        assert set(files) == {first, second}
        assert all(row["complete"] for row in files.values())
        assert files[first]["name"] == "First"
        assert files[second]["name"] == "Second"
    finally:
        release.set()
        await cache.close()


@pytest.mark.parametrize(
    "change",
    [
        {"disk_max_bytes": -1},
        {"memory_max_bytes": True},
        {"ttl_seconds": float("nan")},
        {"max_background_jobs": 0},
        {"max_file_fraction": 0},
        {"protected_fraction": 1},
    ],
)
def test_policy_rejects_invalid_capacity(change):
    with pytest.raises(ValueError):
        MediaCachePolicy(**change)


def test_disk_protects_reused_blocks_from_sequential_scan(tmp_path):
    cache = DiskMediaCache(tmp_path, max_bytes=5 * 132)
    for index in range(3):
        cache.write((index,), bytes([index]) * 100)
        assert cache.read((index,), 100) is not None
    for index in range(3, 40):
        cache.write((index,), bytes([index]) * 100)
    assert all(cache.read((index,), 100) == bytes([index]) * 100 for index in range(3))
    assert cache.snapshot()["bytes"] <= 5 * 132


def test_disk_entry_bound_scales_and_restart_does_not_evict_at_exact_limit(tmp_path):
    cache = DiskMediaCache(tmp_path, max_bytes=2 * 1024**3)
    assert cache.max_entries > 2048
    cache = DiskMediaCache(tmp_path, max_bytes=100, max_entries=2)
    cache.write((1,), b"one")
    cache.write((2,), b"two")
    restarted = DiskMediaCache(tmp_path, max_bytes=100, max_entries=2)
    assert restarted.read((1,), 3) == b"one"
    assert restarted.read((2,), 3) == b"two"


def test_disk_reserves_free_space_and_never_recursively_removes_other_files(
    tmp_path, monkeypatch
):
    sentinel = tmp_path / "upload.pending"
    sentinel.write_text("keep")
    cache = DiskMediaCache(tmp_path, max_bytes=4096, min_free_bytes=1024)
    monkeypatch.setattr(
        "tgdrive.media.media_cache.shutil.disk_usage",
        lambda _: SimpleNamespace(free=100),
    )
    assert cache.write((1,), b"payload") is False
    cache.clear()
    assert sentinel.read_text() == "keep"


def test_memory_protected_segment_survives_scan():
    cache = _PayloadCache(max_bytes=500, max_entries=100, ttl=60, read_slots=2)
    for index in range(3):
        cache._put((index,), bytes([index]) * 100)
        cache._hit((index,))
    for index in range(3, 40):
        cache._put((index,), bytes([index]) * 100)
    assert all(cache._hit((index,)) is not None for index in range(3))
    assert cache.bytes_used <= 500


@pytest.mark.asyncio
async def test_disk_write_is_deferred_and_included_in_memory_budget(
    tmp_path, monkeypatch
):
    cache = MediaReadCache(
        tmp_path, MediaCachePolicy(memory_max_bytes=8 * BLOCK, min_free_bytes=0)
    )
    started, release = threading.Event(), threading.Event()
    original = cache.disk.write

    def slow_write(*args):
        started.set()
        release.wait(5)
        return original(*args)

    monkeypatch.setattr(cache.disk, "write", slow_write)
    try:
        payload = b"x" * BLOCK
        assert (
            await asyncio.wait_for(
                cache.get(
                    key(size=BLOCK), BLOCK, lambda: asyncio.sleep(0, result=payload)
                ),
                1,
            )
            == payload
        )
        await asyncio.to_thread(started.wait, 1)
        assert cache._pending_bytes == BLOCK
        assert (
            cache.memory.bytes_used + cache._pending_bytes
            <= cache.effective_memory_bytes
        )
    finally:
        release.set()
        await cache.close()
    assert cache.disk.read(key(size=BLOCK), BLOCK) == payload


@pytest.mark.asyncio
async def test_zero_cache_budget_still_serves_remote_without_retaining_bytes(tmp_path):
    cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(memory_max_bytes=0, disk_max_bytes=0, min_free_bytes=0),
    )
    count = 0

    async def remote():
        nonlocal count
        count += 1
        return b"abc"

    try:
        assert await cache.get(key(size=3), 3, remote) == b"abc"
        assert await cache.get(key(size=3), 3, remote) == b"abc"
        assert count == 2
        state = await cache.snapshot()
        assert state["memory"]["bytes"] == state["disk"]["bytes"] == 0
        assert state["files"] == []
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_inventory_survives_restart_and_tracks_demand_bytes(tmp_path):
    policy = MediaCachePolicy(memory_max_bytes=0, disk_max_bytes=4096, min_free_bytes=0)
    cache = MediaReadCache(tmp_path, policy)
    file_id = cache.register_file(
        channel_id=CHANNEL, message_id=77, size=3, name="friend.mp4"
    )
    await cache.get(key(size=3), 3, lambda: asyncio.sleep(0, result=b"abc"))
    await cache.close()
    restarted = MediaReadCache(tmp_path, policy)
    try:
        state = await restarted.snapshot()
        assert state["files"] == [state["files"][0]]
        row = state["files"][0]
        assert row["id"] == file_id and row["name"] == "friend.mp4"
        assert row["disk_ranges"] == row["ready_ranges"] == [[0, 3]]
        assert row["complete"] and row["ready_bytes"] == 3
        assert (tmp_path / "inventory.json").stat().st_mode & 0o777 == 0o600
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_inventory_exposes_gaps_and_clear_is_per_logical_file(tmp_path):
    cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=8 * BLOCK, disk_max_bytes=0, min_free_bytes=0
        ),
    )
    first = cache.register_file(
        channel_id=CHANNEL, message_id=77, size=3 * BLOCK, name="video"
    )
    second = cache.register_file(channel_id=CHANNEL, message_id=88, size=3, name="tiny")
    try:
        for index in (0, 2):
            await cache.get(
                key(index), BLOCK, lambda: asyncio.sleep(0, result=b"x" * BLOCK)
            )
        await cache.get(
            key(size=3, message=88), 3, lambda: asyncio.sleep(0, result=b"abc")
        )
        files = {row["id"]: row for row in (await cache.snapshot())["files"]}
        assert files[first]["ready_ranges"] == [[0, BLOCK], [2 * BLOCK, 3 * BLOCK]]
        assert not files[first]["complete"]
        await cache.clear(first)
        assert [row["id"] for row in (await cache.snapshot())["files"]] == [second]
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_clear_during_inflight_read_does_not_resurrect_payload(tmp_path):
    cache = MediaReadCache(tmp_path)
    started, gate = asyncio.Event(), asyncio.Event()

    async def remote():
        started.set()
        await gate.wait()
        return b"abc"

    task = asyncio.create_task(cache.get(key(size=3), 3, remote))
    await started.wait()
    await cache.clear()
    gate.set()
    assert await task == b"abc"
    assert not cache.memory.entries
    assert cache.disk.read(key(size=3), 3) is None
    await cache.close()


@pytest.mark.asyncio
async def test_reconfigure_shrinks_both_tiers_immediately(tmp_path):
    cache = MediaReadCache(tmp_path)
    await cache.get(key(size=3), 3, lambda: asyncio.sleep(0, result=b"abc"))
    await cache.reconfigure(
        MediaCachePolicy(memory_max_bytes=0, disk_max_bytes=0, min_free_bytes=0)
    )
    state = await cache.snapshot()
    assert state["memory"]["bytes"] == state["disk"]["bytes"] == 0
    assert state["files"] == []
    await cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [[], None, {"files": [{"id": "bad", "parts": [{"bogus": True}]}]}]
)
async def test_corrupt_inventory_never_breaks_remote_availability(tmp_path, value):
    (tmp_path / "inventory.json").write_text(json.dumps(value))
    cache = MediaReadCache(tmp_path)
    assert (
        await cache.get(key(size=3), 3, lambda: asyncio.sleep(0, result=b"abc"))
        == b"abc"
    )
    assert len((await cache.snapshot())["files"]) == 1
    await cache.close()


@pytest.mark.asyncio
async def test_alternate_representations_do_not_combine_ready_ranges(tmp_path):
    cache = MediaReadCache(tmp_path, MediaCachePolicy(disk_max_bytes=0))
    cache.register_file(channel_id=CHANNEL, message_id=77, size=2 * BLOCK, name="movie")
    original = key(size=2 * BLOCK)
    alternate = list(key(index=1, size=2 * BLOCK))
    alternate[3] += 1
    try:
        await cache.get(original, BLOCK, lambda: asyncio.sleep(0, result=b"a" * BLOCK))
        await cache.get(
            tuple(alternate), BLOCK, lambda: asyncio.sleep(0, result=b"b" * BLOCK)
        )
        files = (await cache.snapshot())["files"]
        assert len(files) == 2
        assert all(not row["complete"] for row in files)
        assert sorted(row["ready_ranges"] for row in files) == [
            [[0, BLOCK]],
            [[BLOCK, 2 * BLOCK]],
        ]
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_many_waiting_preloads_leave_demand_fill_capacity(tmp_path):
    cache = MediaReadCache(tmp_path)
    gate = asyncio.Event()
    foreground_started = asyncio.Event()

    async def held_foreground():
        foreground_started.set()
        await gate.wait()
        return b"abc"

    foreground = asyncio.create_task(cache.get(key(size=3), 3, held_foreground))
    await foreground_started.wait()
    speculative = [
        asyncio.create_task(
            cache.get(
                key(size=3, message=index),
                3,
                lambda: asyncio.sleep(0, result=b"xyz"),
                background=True,
            )
        )
        for index in range(100, 130)
    ]
    try:
        assert (
            await asyncio.wait_for(
                cache.get(
                    key(size=3, message=88), 3, lambda: asyncio.sleep(0, result=b"new")
                ),
                1,
            )
            == b"new"
        )
        gate.set()
        assert await foreground == b"abc"
        assert all(value == b"xyz" for value in await asyncio.gather(*speculative))
    finally:
        gate.set()
        await asyncio.gather(foreground, *speculative, return_exceptions=True)
        await cache.close()


@pytest.mark.asyncio
async def test_inventory_poll_during_queued_fills_preserves_later_readiness(tmp_path):
    cache = MediaReadCache(tmp_path, MediaCachePolicy(disk_max_bytes=0))
    gate = asyncio.Event()

    async def remote():
        await gate.wait()
        return b"abc"

    tasks = [
        asyncio.create_task(cache.get(key(size=3, message=index), 3, remote))
        for index in range(100, 125)
    ]
    try:
        for _ in range(100):
            if (
                len(cache.memory.flights) == min(25, cache.scratch.limit)
                and len(cache._active_keys) == 25
            ):
                break
            await asyncio.sleep(0.001)
        assert (await cache.snapshot())["files"] == []
        gate.set()
        await asyncio.gather(*tasks)
        assert len((await cache.snapshot())["files"]) == 25
    finally:
        gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await cache.close()


@pytest.mark.asyncio
async def test_current_availability_refresh_shrinks_and_recovers_retention_and_scratch(
    tmp_path, monkeypatch
):
    available, now = [8 * BLOCK], [0.0]
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory",
        lambda value: min(value, available[0]),
    )
    cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=8 * BLOCK, disk_max_bytes=0, scratch_max_bytes=8 * BLOCK
        ),
        clock=lambda: now[0],
    )
    try:
        for index in range(4):
            await cache.get(
                key(index, size=4 * BLOCK),
                BLOCK,
                lambda: asyncio.sleep(0, result=b"x" * BLOCK),
            )
        assert (await cache.snapshot())["memory"]["bytes"] == 4 * BLOCK
        for _ in range(4):
            await cache.scratch.acquire()
        available[0] = 2 * BLOCK
        now[0] = 4
        assert (await cache.snapshot())["memory"]["effective_max_bytes"] == 8 * BLOCK
        now[0] = 5
        reduced = await cache.snapshot()
        assert reduced["memory"]["bytes"] <= 2 * BLOCK
        assert reduced["memory"]["effective_max_bytes"] == 2 * BLOCK
        assert reduced["memory"]["max_fills"] == 2
        assert reduced["scratch"]["max_bytes"] == 2 * BLOCK
        assert reduced["scratch"]["bytes"] == 4 * BLOCK  # Existing frames drain.
        for _ in range(4):
            cache.scratch.release()
        available[0] = 8 * BLOCK
        now[0] = 10
        recovered = await cache.snapshot()
        assert recovered["memory"]["effective_max_bytes"] == 8 * BLOCK
        assert recovered["memory"]["max_fills"] == 8
        assert recovered["scratch"]["max_bytes"] == 8 * BLOCK
        assert recovered["scratch"]["bytes"] == 0
    finally:
        await cache.close()


async def test_inventory_orders_actual_downloads_not_cache_hits_and_retains_order(
    tmp_path, monkeypatch
):
    cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=8 * BLOCK, disk_max_bytes=8 * BLOCK, min_free_bytes=0
        ),
    )
    clock = [1000.0]
    monkeypatch.setattr("tgdrive.media.media_reader.time.time", lambda: clock[0])
    first = cache.register_file(
        channel_id=CHANNEL, message_id=77, size=2 * BLOCK, name="First"
    )
    second = cache.register_file(
        channel_id=CHANNEL, message_id=88, size=3, name="Second"
    )
    try:
        await cache.get(
            key(0, 2 * BLOCK), BLOCK, lambda: asyncio.sleep(0, result=b"a" * BLOCK)
        )
        clock[0] = 1001.0
        await cache.get(
            key(size=3, message=88), 3, lambda: asyncio.sleep(0, result=b"abc")
        )
        assert [row["id"] for row in (await cache.snapshot())["files"]] == [
            second,
            first,
        ]
        clock[0] = 1002.0
        cache.register_file(
            channel_id=CHANNEL, message_id=77, size=2 * BLOCK, name="First"
        )
        await cache.get(
            key(0, 2 * BLOCK), BLOCK, lambda: pytest.fail("must reuse cached bytes")
        )
        assert [row["id"] for row in (await cache.snapshot())["files"]] == [
            second,
            first,
        ]
        clock[0] = 1003.0
        await cache.get(
            key(1, 2 * BLOCK), BLOCK, lambda: asyncio.sleep(0, result=b"b" * BLOCK)
        )
        rows = (await cache.snapshot())["files"]
        assert [row["id"] for row in rows] == [first, second]
        assert rows[0]["last_download"] == 1003.0
        await cache.close()
        cache = MediaReadCache(tmp_path, cache.policy)
        assert [row["id"] for row in (await cache.snapshot())["files"]] == [
            first,
            second,
        ]
    finally:
        await cache.close()
