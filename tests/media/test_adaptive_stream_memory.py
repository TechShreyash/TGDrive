"""Adaptive stream-memory draft; execution requires fresh owner approval."""

import asyncio

import pytest

from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import BLOCK, MediaReadCache


@pytest.fixture
async def adaptive_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    now = [100.0]
    cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=0, disk_max_bytes=0, scratch_max_bytes=256 * BLOCK
        ),
        clock=lambda: now[0],
    )
    yield cache, now
    while cache.scratch.used:
        cache.scratch.release()
    await cache.close()


def measurement(
    *,
    available: int | None = 1024 * BLOCK,
    limit: int | None = 2 * 1024 * BLOCK,
    at=1000.0,
):
    return {"memory_available": available, "memory_limit": limit, "sampled_at": at}


async def waiting_head(cache, *, foreground=True):
    while cache.scratch.try_acquire():
        pass
    task = asyncio.create_task(cache.scratch.acquire(foreground=foreground))
    await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_actual_wait_grows_by_one_baseline_and_same_sample_is_consumed_once(
    adaptive_cache,
):
    cache, now = adaptive_cache
    task = await waiting_head(cache)
    sample = measurement()
    try:
        assert cache.scratch.limit == 32
        cache.update_stream_capacity(sample)
        await asyncio.wait_for(task, 1)
        assert cache.scratch.limit == cache.fill_slots.limit == 64
        assert cache.scratch.used == 33
        assert cache.fill_slots.used == 0
        now[0] += 5
        cache.update_stream_capacity(sample)
        assert cache.scratch.limit == 64
        snapshot = cache.activity_snapshot()["scratch"]
        assert snapshot["sampled_at"] == 1000
        assert snapshot["sample_age_seconds"] == 5
        assert snapshot["reason"] == "foreground_wait"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_free_ram_without_foreground_wait_does_not_expand(adaptive_cache):
    cache, _now = adaptive_cache
    cache.update_stream_capacity(measurement())
    assert cache.scratch.limit == cache.fill_slots.limit == 32
    task = await waiting_head(cache, foreground=False)
    try:
        cache.update_stream_capacity(measurement(at=1005))
        assert cache.scratch.limit == 32
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pressure_shrinks_ceiling_without_returning_owned_reservations(
    adaptive_cache,
):
    cache, _now = adaptive_cache
    while cache.scratch.try_acquire():
        pass
    cache.update_stream_capacity(measurement(available=12 * BLOCK))
    assert cache.scratch.limit == cache.fill_slots.limit == 2
    assert cache.scratch.used == 32
    assert not cache.scratch.try_acquire()
    assert cache.activity_snapshot()["scratch"]["reason"] == "memory_pressure"
    for _ in range(31):
        cache.scratch.release()
    assert cache.scratch.try_acquire()
    assert cache.scratch.used == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("available,limit", [(None, None), (None, 1024 * BLOCK)])
async def test_unknown_available_memory_never_grows_above_baseline(
    adaptive_cache, available, limit
):
    cache, _now = adaptive_cache
    cache.scratch.resize(64 * BLOCK)
    cache.fill_slots.resize(64 * BLOCK)
    cache.update_stream_capacity(measurement(available=available, limit=limit))
    assert cache.scratch.limit == cache.fill_slots.limit == 32
    assert cache.activity_snapshot()["scratch"]["reason"] == "memory_unknown"


@pytest.mark.asyncio
async def test_known_available_memory_with_unknown_limit_can_grow(adaptive_cache):
    cache, _now = adaptive_cache
    task = await waiting_head(cache)
    try:
        cache.update_stream_capacity(measurement(limit=None))
        await asyncio.wait_for(task, 1)
        assert cache.scratch.limit == 64
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_owner_lowering_and_idle_shrink_do_not_reset_or_overshoot(adaptive_cache):
    cache, now = adaptive_cache
    cache.scratch.resize(64 * BLOCK)
    cache.fill_slots.resize(64 * BLOCK)
    now[0] += 31
    cache.update_stream_capacity(measurement())
    assert cache.scratch.limit == 32
    assert cache.activity_snapshot()["scratch"]["reason"] == "idle_baseline"
    await cache.reconfigure(
        MediaCachePolicy(
            memory_max_bytes=0, disk_max_bytes=0, scratch_max_bytes=2 * BLOCK
        )
    )
    assert cache.scratch.limit == cache.fill_slots.limit == 2
    task = await waiting_head(cache)
    try:
        cache.update_stream_capacity(measurement(at=1035))
        assert cache.scratch.limit == 2
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
