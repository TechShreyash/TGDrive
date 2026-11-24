import asyncio
import time

import pytest

from tests.media.test_adaptive_read_ahead import Clock
from tests.media.test_media_scheduler import Account, wait_until
from tgdrive.media.media_reader import BLOCK, _AdaptiveReadAhead
from tgdrive.media.media_scheduler import (
    MediaPriorityScope,
    MediaScheduler,
    media_priority,
)
from tgdrive.telegram.client_pool import ClientPool


def test_ordered_download_ramps_through_origin_waits_and_backs_off_on_slow_send():
    clock = Clock()
    controller = _AdaptiveReadAhead(160, clock=clock)
    controller.bulk = True
    controller.observe_fetch(0.1)
    for _ in range(10):
        clock.advance(1.5)
        controller.consumed(BLOCK, consumer_seconds=0.001)
    assert controller.window == 160
    clock.advance(1.5)
    controller.consumed(BLOCK, consumer_seconds=1.5)
    assert controller.window == 2
    controller.idle()
    assert controller.window == 1


@pytest.mark.parametrize("gate", ["origin", "rpc"])
async def test_playback_promotes_a_queued_fill_and_leaves_bulk_a_turn(gate):
    pool = ClientPool([Account()], media_per_bot=1)
    scheduler = MediaScheduler(limit=1, rate=10000)
    await pool.start()
    order = []

    def admission(priority):
        if gate == "origin":
            return pool.lease(download=True, priority=priority)
        return scheduler.request()

    async def read(label, priority):
        with media_priority.set(priority):
            async with admission(priority):
                order.append(label)

    promoted = MediaPriorityScope("preload")
    requests = [("bulk", "demand"), ("joined", promoted), ("warm", "preload")]
    requests.extend((f"seek-{i}", "playback") for i in range(10))
    tasks = []
    try:
        async with admission("demand"):
            for label, priority in requests:
                tasks.append(asyncio.create_task(read(label, priority)))
                await wait_until(
                    lambda: (
                        len(
                            pool._origin_waiters
                            if gate == "origin"
                            else scheduler._waiting
                        )
                        == len(tasks)
                    )
                )
            await promoted.promote("playback")
            await promoted.promote("demand")
            assert promoted.priority == "playback"
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert order.index("bulk") == 8
        assert order.index("joined") < order.index("warm")
        assert order[-1] == "warm"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.stop()


async def test_playback_prefers_recent_fast_account_and_obeys_cooldown():
    slow, fast = Account(), Account()
    slow.name, fast.name = "slow", "fast"
    pool = ClientPool([slow, fast], media_per_bot=2)
    await pool.start()
    try:
        now = time.monotonic()
        slow.download_part_sample = (now, 1.0)
        fast.download_part_sample = (now, 0.05)
        async with (
            pool.lease(download=True),
            pool.lease(
                download=True, priority="playback", preferred_download_client=slow
            ) as selected,
        ):
            assert selected is fast
        await pool.defer_client(fast.name, time.time() + 10)
        async with pool.lease(download=True, priority="playback") as selected:
            assert selected is slow
    finally:
        await pool.stop()


@pytest.mark.parametrize("multipart", [False, True])
async def test_player_joins_an_ordinary_attachment_fill_at_playback_priority(multipart):
    from tests.media.test_multipart_streaming import build_parts
    from tests.media.test_pooled_media import account
    from tests.media.test_streaming import make_request, response_body
    from tests.media.test_telegram_cache import message
    from tgdrive.media.streaming import stream_telegram_file
    from tgdrive.telegram.clients import TelegramMessage

    body = bytes(range(256)) * 4096
    client = account("only", body)
    mid = 101 if multipart else 77
    if multipart:
        client.native.payloads = {9001: body}
        client.get_messages.side_effect = lambda *args, **kwargs: TelegramMessage(
            client, message(9001, len(body), message_id=mid)
        )
    pool = ClientPool([client], media_per_bot=1)
    await pool.start()

    async def read(download):
        response = await stream_telegram_file(
            pool=pool,
            channel_id=-100123,
            message_id=mid,
            file_name="movie.mkv",
            parts=build_parts([body]) if multipart else None,
            expected_size=len(body),
            request=make_request("bytes=20-2499"),
            preview=True,
            download=download,
        )
        assert response.status_code == 206
        return await response_body(response)

    tasks = []
    try:
        async with pool.lease(download=True):
            tasks.append(asyncio.create_task(read(download=True)))
            await wait_until(lambda: len(pool._origin_waiters) == 1)
            assert pool._origin_waiters[0]["priority"] == "demand"
            tasks.append(asyncio.create_task(read(download=False)))
            await wait_until(lambda: pool._origin_waiters[0]["priority"] == "playback")
            assert len(pool._origin_waiters) == 1
        assert await asyncio.gather(*tasks) == [body[20:2500]] * 2
        assert sum(client.native.requests.values()) == 2
        assert pool._media_read_cache.scratch.used == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.stop()


async def test_declined_read_ahead_retries_in_order_and_leaves_room_for_a_seek():
    from tests.media.test_pooled_media import account
    from tgdrive.media.media_reader import stream_pooled_media
    from tgdrive.telegram.client_pool import MediaQueueFull

    data = b"a" * BLOCK + b"b" * BLOCK + b"c" * BLOCK
    client = account("only", data)
    pool = ClientPool([client], media_per_bot=1, max_media_queue=2)
    await pool.start()
    message = await client.get_messages(-100123, message_ids=77)

    async def read(offset, count, transfer):
        iterator = stream_pooled_media(
            pool,
            client,
            message,
            channel=-100123,
            offset=offset,
            limit=count,
            transfer=transfer,
        )
        try:
            return b"".join([part async for part in iterator])
        finally:
            await iterator.aclose()

    tasks = []
    try:
        async with pool.lease(download=True):
            tasks.append(asyncio.create_task(read(0, 3, "download")))
            await wait_until(lambda: len(pool._origin_waiters) == 1)
            cache = pool._media_read_cache
            frames = next(iter(cache.scratch._streams)).frames
            await wait_until(lambda: frames[1].task.done())
            assert isinstance(frames[1].task.exception(), MediaQueueFull)
            tasks.append(asyncio.create_task(read(2, 1, "playback")))
            await wait_until(lambda: len(pool._origin_waiters) == 2)
            assert pool.performance_snapshot()["queue"][0]["priority"] == "playback"
        assert await asyncio.gather(*tasks) == [data, data[2 * BLOCK :]]
        assert not cache.memory.flights
        assert cache.scratch.used == cache.fill_slots.used == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.stop()


@pytest.mark.parametrize(
    "file_name", ["clip.m2ts", "clip.TS", "clip.VOB", "audio.weba"]
)
def test_player_formats_without_a_platform_media_mime_get_playback_turns(file_name):
    from tgdrive.media.streaming import _response_transfer

    assert (
        _response_transfer(
            file_name, "application/octet-stream", download=False, preview=True
        )
        == "playback"
    )
    assert (
        _response_transfer(
            file_name, "application/octet-stream", download=True, preview=True
        )
        == "download"
    )


async def test_normal_download_can_fill_fifty_healthy_accounts_with_headroom(
    monkeypatch,
):
    from tests.media.test_pooled_media import account
    from tests.media.test_streaming import make_request
    from tests.media.test_telegram_cache import message
    from tgdrive.media.media_policy import MediaCachePolicy
    from tgdrive.media.media_reader import MediaReadCache
    from tgdrive.media.streaming import stream_telegram_file
    from tgdrive.telegram.clients import TelegramMessage

    clock = Clock()
    cache = MediaReadCache(
        policy=MediaCachePolicy(
            scratch_max_bytes=160 * BLOCK, memory_max_bytes=0, disk_max_bytes=0
        ),
        clock=clock,
    )

    class SampledWindow(_AdaptiveReadAhead):
        def __init__(self, maximum):
            super().__init__(maximum, clock=clock)

        def consumed(self, size, **timing):
            clock.advance(5)
            super().consumed(size, **timing)
            cache.update_stream_capacity(
                {
                    "memory_available": 1024 * BLOCK,
                    "memory_limit": 2048 * BLOCK,
                    "sampled_at": clock(),
                }
            )

    monkeypatch.setattr("tgdrive.media.media_reader._AdaptiveReadAhead", SampledWindow)
    clients = [account(f"bot-{i}", b"") for i in range(50)]
    pool = ClientPool(clients, media_per_bot=3)
    pool._media_read_cache = cache
    payload = b"x" * BLOCK
    blocks = 256
    release = asyncio.Event()
    active = 0
    calls = {}

    def configure(client):
        async def read(source, *, offset, limit, **kwargs):
            nonlocal active
            assert limit == 1 and offset not in calls
            calls[offset] = client
            active += 1
            try:
                if offset >= 64:
                    await release.wait()
                yield payload
            finally:
                active -= 1

        client.stream_media = read
        client.get_messages.side_effect = None
        client.get_messages.return_value = TelegramMessage(
            client, message(1, blocks * BLOCK)
        )

    for client in clients:
        configure(client)

    async def download():
        response = await stream_telegram_file(
            pool=pool,
            channel_id=-100123,
            message_id=77,
            file_name="file.bin",
            expected_size=blocks * BLOCK,
            request=make_request(),
            download=True,
        )
        total = 0
        async for part in response.body_iterator:
            total += len(part)
        return total

    await pool.start()
    task = asyncio.create_task(download())
    try:
        await wait_until(lambda: active == 150)
        assert set(calls.values()) == set(clients)
        assert cache.scratch.limit == 160
        release.set()
        assert await asyncio.wait_for(task, 3) == blocks * BLOCK
        assert len(calls) == blocks
        assert active == cache.scratch.used == cache.fill_slots.used == 0
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await cache.close()
        await pool.stop()


@pytest.mark.parametrize("spare", [True, False])
async def test_overdue_head_moves_only_when_a_proven_spare_bot_can_read(
    spare, monkeypatch
):
    from tests.media.test_pooled_media import account
    from tests.media.test_streaming import make_request, response_body
    from tgdrive.media.streaming import stream_telegram_file
    from tgdrive.telegram.clients import Client

    monkeypatch.setattr(Client, "_download_reassignment_interval", 0.03)
    release = asyncio.Event()
    data = b"x" * BLOCK
    slow, fast = account("slow", data, gate=release), account("fast", data)
    pool = ClientPool([slow, fast] if spare else [slow], media_per_bot=1)
    await pool.start()
    slow.download_part_sample = fast.download_part_sample = (time.monotonic(), 0.001)
    task = asyncio.create_task(
        stream_telegram_file(
            pool=pool,
            channel_id=-100123,
            message_id=77,
            file_name="video.mkv",
            expected_size=BLOCK,
            request=make_request(),
            preview=True,
        )
    )
    try:
        await wait_until(lambda: slow.native.active == 1)
        if not spare:
            await asyncio.sleep(0.1)
            assert not task.done()
            release.set()
        response = await asyncio.wait_for(task, 1)
        assert await response_body(response) == data
        assert bool(fast.native.requests) is spare
        assert slow.native.active == fast.native.active == 0
        assert await pool.account_retry_at(slow.name) <= time.time()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await pool.stop()
