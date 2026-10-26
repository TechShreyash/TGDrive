from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodPremiumWaitError, FloodWaitError

from tests.media.test_streaming import make_request, response_body
from tests.media.test_telegram_cache import MIB, Native, message, transport
from tgdrive.media.media_cache import DiskMediaCache
from tgdrive.media.media_reader import MediaReadCache, stream_pooled_media
from tgdrive.media.streaming import stream_telegram_file
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import TelegramMessage


def account(name, payload, gate=None, native_type=Native):
    native = native_type({1: payload}, gate)
    client = transport(native, name)
    client.start = AsyncMock()
    client.stop = AsyncMock()
    native.is_connected = lambda: True
    client.get_messages = AsyncMock(
        side_effect=lambda *_args, **_kwargs: TelegramMessage(
            client, message(1, len(payload))
        )
    )
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unavailable", [None, "busy", "cooling", "unhealthy", "disconnected"]
)
async def test_payload_prefers_ready_premium_with_bot_fallback(unavailable):
    payload = b"original payload"
    bot, user = account("bot", payload), account("premium", payload)
    pool = ClientPool([bot], [user], media_per_bot=1)
    await pool.start()
    occupied = None
    try:
        if unavailable == "busy":
            occupied = pool.lease(
                download=True, allow_users=True, client_name=user.name
            )
            await occupied.__aenter__()
        elif unavailable == "cooling":
            await pool.defer_client(user.name, time.time() + 60)
        elif unavailable == "unhealthy":
            await pool.mark_unhealthy(user)
        elif unavailable == "disconnected":
            user.native.is_connected = lambda: False
        response = await stream_telegram_file(
            pool=pool,
            channel_id=-100123,
            message_id=77,
            file_name="data.bin",
            expected_size=len(payload),
            request=make_request(),
        )
        assert await asyncio.wait_for(response_body(response), 2) == payload
        assert bool(user.native.requests) is (unavailable is None)
        assert bool(bot.native.requests) is (unavailable is not None)
    finally:
        if occupied is not None:
            await occupied.__aexit__(None, None, None)
        assert all(row.active_leases == 0 for row in await pool.status())
        await pool.stop()


@pytest.mark.asyncio
async def test_all_accounts_share_ordered_reads_and_repeated_ranges_hit_cache(tmp_path):
    payload = b"".join(bytes([i]) * MIB for i in range(12)) + b"tail"
    accounts = [account(str(i), payload) for i in range(3)]
    pool = ClientPool(accounts, media_cache_dir=tmp_path)
    await pool.start()
    try:

        async def request():
            return await stream_telegram_file(
                pool=pool,
                channel_id=-100123,
                message_id=77,
                file_name="video.mkv",
                expected_size=len(payload),
                request=make_request(),
            )

        assert await response_body(await request()) == payload
        assert all(client.native.requests for client in accounts)
        assert all(client.native.peak <= 3 for client in accounts)
        assert all(client.native.active == 0 for client in accounts)
        reads = sum(sum(client.native.requests.values()) for client in accounts)
        assert reads == 25  # One remote fetch per block, exact final tail.
        # Rotate the metadata account, then discard RAM; both caches are shared.
        assert await response_body(await request()) == payload
        pool._media_read_cache = MediaReadCache(tmp_path)
        assert await response_body(await request()) == payload
        assert sum(sum(client.native.requests.values()) for client in accounts) == reads
        assert len(list(tmp_path.glob("*.block"))) == 13
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_two_same_bot_responses_do_not_deadlock_or_duplicate_remote_reads():
    payload = b"x" * (6 * MIB)
    client = account("only", payload)
    pool = ClientPool([client])
    await pool.start()
    try:

        async def request():
            response = await stream_telegram_file(
                pool=pool,
                channel_id=-100123,
                message_id=77,
                file_name="video.mkv",
                expected_size=len(payload),
                request=make_request(),
            )
            return await response_body(response)

        a, b = await asyncio.wait_for(asyncio.gather(request(), request()), 2)
        assert a == b == payload
        assert sum(client.native.requests.values()) == 12
        assert client.native.peak <= 3
        assert all(row.active_leases == 0 for row in await pool.status())
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_read_ahead_is_bounded_and_disconnect_drains_workers():
    payload = b"x" * (64 * MIB)
    clients = [account(str(i), payload) for i in range(3)]
    pool = ClientPool(clients)
    await pool.start()
    iterator = stream_pooled_media(
        pool,
        clients[0],
        await clients[0].get_messages(1, message_ids=77),
        channel=1,
        offset=0,
        limit=64,
    )
    try:
        assert await anext(iterator) == payload[:MIB]
        await asyncio.sleep(0.03)
        fetched = sum(sum(c.native.requests.values()) for c in clients)
        # A new response probes one block ahead until the consumer demonstrates
        # sustained demand. Each MiB uses two native Telegram requests here.
        assert fetched == 4
        await iterator.aclose()
        assert all(c.native.active == 0 for c in clients)
        assert all(row.active_leases == 0 for row in await pool.status())
        assert not pool._media_read_cache.memory.flights
    finally:
        await iterator.aclose()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("flood_error", [FloodWaitError, FloodPremiumWaitError])
async def test_flood_wait_defers_account_and_other_account_continues(flood_error):
    class Flooded(Native):
        def iter_download(self, *args, **kwargs):
            raise flood_error(request=None, capture=60)

    payload = b"x" * (4 * MIB)
    flooded = account("flooded", payload, native_type=Flooded)
    healthy = account("healthy", payload)
    pool = ClientPool([flooded, healthy])
    await pool.start()
    try:
        response = await stream_telegram_file(
            pool=pool,
            channel_id=-100123,
            message_id=77,
            file_name="video.mkv",
            expected_size=len(payload),
            request=make_request(),
        )
        assert await asyncio.wait_for(response_body(response), 2) == payload
        assert await pool.account_retry_at("flooded") > time.time() + 50
        assert healthy.native.requests and healthy.native.peak <= 3
    finally:
        await pool.stop()


def test_disk_cache_survives_restart_evicts_and_rejects_corruption(tmp_path):
    cache = DiskMediaCache(tmp_path, max_bytes=70, max_entries=2)
    cache.write(("a",), b"aaa")
    cache.write(("b",), b"bbb")
    assert cache.read(("a",), 3) == b"aaa"
    cache.write(("c",), b"ccc")
    assert cache.read(("b",), 3) is None
    restarted = DiskMediaCache(tmp_path, max_bytes=70, max_entries=2)
    assert restarted.read(("a",), 3) == b"aaa"
    path = tmp_path / (cache._key(("a",)) + ".block")
    path.write_bytes(b"z" * 35)
    assert restarted.read(("a",), 3) is None
    assert not path.exists()
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_disk_cache_expiry_and_unavailable_path_are_safe(tmp_path):
    cache = DiskMediaCache(tmp_path / "cache", ttl=0.001)
    cache.write(("expired",), b"abc")
    time.sleep(0.005)
    assert cache.read(("expired",), 3) is None
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("blocked")
    unavailable = DiskMediaCache(blocked)
    assert unavailable.read(("a",), 3) is None
    unavailable.write(("a",), b"abc")


@pytest.mark.asyncio
async def test_multipart_boundary_and_cached_reads_still_validate_message():
    from tests.media.test_multipart_streaming import build_parts
    from tgdrive.media.streaming import TelegramFileNotFound

    contents = [b"a" * (3 * MIB), b"b" * (3 * MIB)]
    clients = [account(str(i), b"") for i in range(3)]
    for client in clients:
        client.native.payloads = {9001: contents[0], 9002: contents[1]}

        def fetch(_channel, *, message_ids, owner=client):
            index = message_ids - 101
            native = message(9001 + index, len(contents[index]))
            native.id = message_ids
            return TelegramMessage(owner, native)

        client.get_messages = AsyncMock(side_effect=fetch)
    pool = ClientPool(clients)
    await pool.start()
    try:

        async def request():
            return await stream_telegram_file(
                pool=pool,
                channel_id=-100123,
                message_id=101,
                parts=build_parts(contents),
                file_name="movie.mkv",
                expected_size=6 * MIB,
                request=make_request(f"bytes={3 * MIB - 5}-{3 * MIB + 9}"),
            )

        assert await response_body(await request()) == b"a" * 5 + b"b" * 10
        before = sum(sum(c.native.requests.values()) for c in clients)
        assert await response_body(await request()) == b"a" * 5 + b"b" * 10
        assert sum(sum(c.native.requests.values()) for c in clients) == before
        for client in clients:
            client.get_messages = AsyncMock(return_value=None)
        with pytest.raises(TelegramFileNotFound):
            await request()
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_cooling_premium_metadata_uses_idle_bot():
    bot, user = account("bot", b"data"), account("premium", b"data")
    pool = ClientPool([bot], [user])
    await pool.start()
    try:
        await pool.defer_client("premium", time.time() + 60)

        async def acquire():
            async with pool.lease(prefer_premium=True, respect_cooldown=True) as chosen:
                assert chosen is bot

        await asyncio.wait_for(acquire(), 0.5)
    finally:
        await pool.stop()
