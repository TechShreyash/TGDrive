from __future__ import annotations

import asyncio

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


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def consume(controller, clock, *, blocks_per_second, blocks):
    for _ in range(blocks):
        clock.advance(1 / blocks_per_second)
        controller.consumed(BLOCK)


def test_pipeline_grows_for_sustained_fast_delivery_and_shrinks_when_it_slows():
    clock = Clock()
    controller = _AdaptiveReadAhead(32, clock=clock)
    controller.observe_fetch(0.2)
    assert controller.window == 2
    consume(controller, clock, blocks_per_second=2, blocks=8)
    assert controller.window == 2
    consume(controller, clock, blocks_per_second=128, blocks=256)
    assert 16 <= controller.window <= 32
    consume(controller, clock, blocks_per_second=1, blocks=1)
    assert controller.window == 2


def test_source_latency_changes_pipeline_needed_for_the_same_delivery_rate():
    windows = []
    for latency in (0.01, 0.5):
        clock = Clock()
        controller = _AdaptiveReadAhead(32, clock=clock)
        controller.observe_fetch(latency)
        consume(controller, clock, blocks_per_second=8, blocks=24)
        windows.append(controller.window)
    assert windows[0] < windows[1] < 32


def test_immediate_cache_bursts_cannot_start_an_unproven_large_pipeline():
    clock = Clock()
    controller = _AdaptiveReadAhead(32, clock=clock)
    controller.observe_fetch(1)
    for _ in range(128):
        controller.consumed(BLOCK)
    assert controller.window == 2
    clock.advance(0.1)
    controller.consumed(BLOCK)
    assert controller.window == 4  # One measured sample permits one growth step.


def test_tail_bytes_and_new_responses_do_not_inherit_a_full_block_fast_rate():
    clock = Clock()
    controller = _AdaptiveReadAhead(32, clock=clock)
    controller.observe_fetch(0.2)
    clock.advance(0.2)
    controller.consumed(17)
    assert controller.window == 2
    consume(controller, clock, blocks_per_second=128, blocks=256)
    assert controller.window > 2
    assert _AdaptiveReadAhead(32, clock=clock).window == 2
    assert _AdaptiveReadAhead(1, clock=clock).window == 1


@pytest.mark.asyncio
async def test_small_range_has_no_remote_reads_outside_its_aligned_span(tmp_path):
    payload = b"".join(bytes([index]) * BLOCK for index in range(8))
    client = account("bot", payload)
    calls = []

    async def remote(_source, *, offset, limit, **_kwargs):
        calls.append(offset)
        assert limit == 1
        yield payload[offset * BLOCK : (offset + 1) * BLOCK]

    client.stream_media = remote
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    source = await client.get_messages(1, message_ids=77)
    iterator = stream_pooled_media(pool, client, source, channel=1, offset=5, limit=2)
    try:
        assert (
            b"".join([data async for data in iterator])
            == payload[5 * BLOCK : 7 * BLOCK]
        )
        assert sorted(calls) == [5, 6]
        assert pool._media_read_cache.scratch.used == 0
    finally:
        await iterator.aclose()
        await pool.stop()


@pytest.mark.asyncio
async def test_paused_consumer_drops_ahead_and_disconnect_keeps_only_owned_head(
    tmp_path, monkeypatch
):
    payload = b"x" * (16 * BLOCK)
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    cache = pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=0,
            disk_max_bytes=0,
            scratch_max_bytes=4 * BLOCK,
            min_free_bytes=0,
        ),
    )
    # Exercise the timer without a one-second real wait; its action must never
    # reclaim the already-yielded head or leave detached I/O on disconnect.
    monkeypatch.setattr(
        _AdaptiveReadAhead, "idle_seconds", property(lambda _self: 0.01)
    )
    await pool.start()
    source = await client.get_messages(1, message_ids=77)
    iterator = stream_pooled_media(pool, client, source, channel=1, offset=0, limit=16)
    try:
        assert await anext(iterator) == payload[:BLOCK]
        assert cache.scratch.used == 2
        await wait_until(lambda: cache.scratch.used == 1)
        assert cache._active_streams == 1
        assert await anext(iterator) == payload[BLOCK : 2 * BLOCK]
        await iterator.aclose()
        assert cache.scratch.used == cache._active_streams == 0
        assert not cache.memory.flights
        await asyncio.sleep(0.02)
        assert cache.scratch.used == 0
    finally:
        await iterator.aclose()
        await cache.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_actual_reader_ramps_then_shares_scratch_and_reduces_slow_pipeline(
    tmp_path, monkeypatch
):
    clock = Clock()

    class ClockedReadAhead(_AdaptiveReadAhead):
        def __init__(self, maximum):
            super().__init__(maximum, clock=clock)

    monkeypatch.setattr(
        "tgdrive.media.media_reader._AdaptiveReadAhead", ClockedReadAhead
    )
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    client = account("bot", b"x")
    payload = b"x" * BLOCK

    async def remote(_source, *, offset, limit, **_kwargs):
        assert 0 <= offset < 128 and limit == 1
        yield payload

    client.stream_media = remote
    pool = ClientPool([client], media_cache_dir=tmp_path, media_per_bot=16)
    cache = pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=0,
            disk_max_bytes=0,
            scratch_max_bytes=32 * BLOCK,
            min_free_bytes=0,
        ),
    )
    await pool.start()
    source = TelegramMessage(client, message(1, 128 * BLOCK))
    fast = stream_pooled_media(pool, client, source, channel=1, offset=0, limit=128)
    seek = stream_pooled_media(pool, client, source, channel=1, offset=127, limit=1)
    try:
        assert await anext(fast) == payload
        assert cache.scratch.used == 2
        for _ in range(85):
            clock.advance(0.005)
            assert await anext(fast) == payload
        assert cache.scratch.used == 32
        assert await asyncio.wait_for(anext(seek), 2) == payload
        assert cache.scratch.used == cache.scratch.peak == 32
        clock.advance(0.005)
        assert await anext(fast) == payload
        # The advanced reader trims to its fair share while another response
        # owns a head, even though its rate still supports the maximum window.
        # In-flight reads keep their reservations until cancellation cleanup.
        await wait_until(lambda: cache.scratch.used <= 17)
        assert cache.scratch.used <= 17
        await seek.aclose()
        clock.advance(1)
        assert await anext(fast) == payload
        await wait_until(lambda: cache.scratch.used <= 3)
        assert cache.scratch.used <= 3
    finally:
        await seek.aclose()
        await fast.aclose()
        assert cache.scratch.used == cache._active_streams == 0
        assert not cache.memory.flights
        await cache.close()
        await pool.stop()
