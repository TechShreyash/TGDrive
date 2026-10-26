from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from tests.media.test_multipart_streaming import MultipartClient, build_parts
from tests.media.test_pooled_media import account
from tests.media.test_streaming import make_request, response_body
from tests.media.test_telegram_cache import document, message
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import BLOCK
from tgdrive.media.preloading import PreloadManager
from tgdrive.media.streaming import _not_modified, media_etag, stream_telegram_file
from tgdrive.telegram.client_pool import ClientPool, MediaQueueFull
from tgdrive.telegram.clients import TelegramMessage

CHANNEL = -1000000000123


def request(header=None, **headers):
    result = make_request(header)
    result.scope["headers"].extend(
        (name.replace("_", "-").encode(), value.encode())
        for name, value in headers.items()
    )
    return result


async def response(pool, payload, req=None, **kwargs):
    return await stream_telegram_file(
        pool=pool,
        channel_id=CHANNEL,
        message_id=77,
        file_name="video.mp4",
        expected_size=len(payload),
        request=req or request(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_strong_etag_if_range_and_no_transform_preserve_exact_bytes(tmp_path):
    payload = b"abcdefghij"
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    try:
        first = await response(pool, payload)
        etag = first.headers["etag"]
        assert etag.startswith('"tg-')
        assert "no-transform" in first.headers["cache-control"]
        assert first.headers["content-encoding"] == "identity"
        assert await response_body(first) == payload
        partial = await response(pool, payload, request("bytes=2-5", if_range=etag))
        assert partial.status_code == 206
        assert partial.headers["content-length"] == "4"
        assert partial.headers["content-range"] == "bytes 2-5/10"
        assert await response_body(partial) == b"cdef"
        for validator in ('"different"', "W/" + etag, "Sat, 01 Jan 2000 00:00:00 GMT"):
            full = await response(
                pool, payload, request("bytes=2-5", if_range=validator)
            )
            assert full.status_code == 200
            assert "content-range" not in full.headers
            assert await response_body(full) == payload
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_conditional_request_revalidates_metadata_without_reading_payload(
    tmp_path,
):
    payload = b"abcdefghij"
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    try:
        first = await response(pool, payload)
        assert await response_body(first) == payload
        metadata = client.get_messages.call_count
        reads = sum(client.native.requests.values())
        conditional = await response(
            pool, payload, request(if_none_match=first.headers["etag"])
        )
        assert conditional.status_code == 304 and conditional.body == b""
        assert client.get_messages.call_count == metadata + 1
        assert sum(client.native.requests.values()) == reads
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_same_size_telegram_replacement_invalidates_resume_validator(tmp_path):
    original, replacement = b"abcdefghij", b"0123456789"
    client = account("bot", original)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    try:
        first = await response(pool, original)
        assert await response_body(first) == original
        client.native.payloads[2] = replacement
        client.get_messages = AsyncMock(
            return_value=TelegramMessage(client, message(2, len(replacement)))
        )
        changed = await response(
            pool, replacement, request("bytes=2-5", if_range=first.headers["etag"])
        )
        assert changed.status_code == 200
        assert changed.headers["etag"] != first.headers["etag"]
        assert await response_body(changed) == replacement
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_origin_overload_returns_retryable_503_before_stream_headers(tmp_path):
    class OverloadedPool(ClientPool):
        @asynccontextmanager
        async def lease(self, **kwargs):
            if kwargs.get("download"):
                raise MediaQueueFull(7)
            async with super().lease(**kwargs) as client:
                yield client

    client = account("bot", b"hello")
    pool = OverloadedPool([client], media_cache_dir=tmp_path)
    await pool.start()
    try:
        overloaded = await response(pool, b"hello")
        assert overloaded.status_code == 503
        assert overloaded.headers["retry-after"] == "7"
        assert json.loads(overloaded.body)["code"] == "TELEGRAM_QUEUE_FULL"
        assert not client.native.requests
        assert all(row.active_leases == 0 for row in await pool.status())
        assert not pool._media_read_cache.memory.flights
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_warm_cache_holds_no_origin_or_metadata_lease(tmp_path):
    payload = b"x" * (2 * BLOCK)
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    try:
        assert await response_body(await response(pool, payload)) == payload
        reads = sum(client.native.requests.values())
        warm = await response(pool, payload)
        assert all(row.active_leases == 0 for row in await pool.status())
        assert await response_body(warm) == payload
        assert sum(client.native.requests.values()) == reads
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_revocation_before_headers_closes_primed_iterator(tmp_path):
    payload = b"x" * (2 * BLOCK)
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    revoked = False

    async def authorize():
        if revoked:
            raise PermissionError("Share revoked")

    try:
        prepared = await response(pool, payload, authorize=authorize)
        revoked = True
        sent = []

        async def send(value):
            sent.append(value)

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(PermissionError, match="revoked"):
            await prepared(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
            )
        assert not sent
        assert not pool._media_read_cache.memory.flights
        assert all(row.active_leases == 0 for row in await pool.status())
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_request_id_reaches_origin_leases(tmp_path):
    client = account("bot", b"hello")
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    leased = pool.lease
    origins = []

    @asynccontextmanager
    async def tracked(**kwargs):
        if kwargs.get("download"):
            origins.append(kwargs.get("request_id"))
        async with leased(**kwargs) as owner:
            yield owner

    pool.lease = tracked
    try:
        result = await response(pool, b"hello", request_id="viewer-123")
        assert await response_body(result) == b"hello"
        assert origins and set(origins) == {"viewer-123"}
    finally:
        await pool.stop()


def test_catalog_only_head_tag_is_weak_until_document_identity_is_known():
    assert media_etag(CHANNEL, 77, 10).startswith("W/")
    assert not media_etag(
        CHANNEL, 77, 10, document_identity=("document", 1, 10)
    ).startswith("W/")


def test_if_none_match_uses_weak_comparison_for_catalog_head():
    tag = media_etag(CHANNEL, 77, 10)
    assert _not_modified(request(if_none_match=tag), tag)
    assert _not_modified(request(if_none_match=tag.removeprefix("W/")), tag)


@pytest.mark.asyncio
async def test_revocation_mid_response_stops_body_and_cancels_pending_reads(tmp_path):
    payload = b"x" * (8 * BLOCK)
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    revoked = False

    async def authorize():
        if revoked:
            raise HTTPException(403, "Share revoked")

    try:
        prepared = await response(pool, payload, authorize=authorize)
        delivered = bytearray()

        async def send(value):
            nonlocal revoked
            if value["type"] == "http.response.body" and value.get("body"):
                delivered.extend(value["body"])
                revoked = True

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(HTTPException, match="revoked"):
            await prepared(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
            )
        assert bytes(delivered) == payload[:BLOCK]
        assert not pool._media_read_cache.memory.flights
        assert all(row.active_leases == 0 for row in await pool.status())
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_multipart_if_range_uses_catalog_manifest_tag():
    contents = [b"abc", b"def"]
    parts = build_parts(contents)
    client = MultipartClient(contents)
    pool = ClientPool([client])
    await pool.start()
    try:
        tag = media_etag(CHANNEL, 101, 6, parts=parts)
        selected = await stream_telegram_file(
            pool=pool,
            channel_id=CHANNEL,
            message_id=101,
            file_name="movie.mp4",
            expected_size=6,
            parts=parts,
            request=request("bytes=2-3", if_range=tag),
        )
        assert selected.headers["etag"] == tag
        assert selected.headers["content-encoding"] == "identity"
        assert selected.status_code == 206
        assert await response_body(selected) == b"cd"
        full = await stream_telegram_file(
            pool=pool,
            channel_id=CHANNEL,
            message_id=101,
            file_name="movie.mp4",
            expected_size=6,
            parts=parts,
            request=request("bytes=2-3", if_range='"stale"'),
        )
        assert full.status_code == 200
        assert await response_body(full) == b"abcdef"
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_multipart_demand_cache_inventory_is_one_logical_file(tmp_path):
    contents = [b"a" * BLOCK, b"b" * BLOCK]
    parts = build_parts(contents)
    client = account("bot", b"")
    client.native.payloads = {9001: contents[0], 9002: contents[1]}

    async def fetch(_channel, *, message_ids):
        index = message_ids - 101
        native = message(9001 + index, len(contents[index]))
        native.id = message_ids
        return TelegramMessage(client, native)

    client.get_messages = AsyncMock(side_effect=fetch)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    try:
        selected = await stream_telegram_file(
            pool=pool,
            channel_id=CHANNEL,
            message_id=101,
            file_name="movie.mp4",
            expected_size=2 * BLOCK,
            parts=parts,
            request=request(f"bytes={BLOCK - 10}-{BLOCK + 9}"),
        )
        assert await response_body(selected) == b"a" * 10 + b"b" * 10
        files = (await pool._media_read_cache.snapshot())["files"]
        assert len(files) == 1
        assert files[0]["name"] == "movie.mp4" and files[0]["complete"]
        assert files[0]["ready_ranges"] == [[0, 2 * BLOCK]]
    finally:
        if pool._media_read_cache is not None:
            await pool._media_read_cache.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_tiny_http_range_probes_do_not_count_whole_telegram_blocks_as_playback(
    tmp_path,
):
    payload = b"x" * (8 * BLOCK)
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    manager = PreloadManager(
        pool, policy=MediaCachePolicy(auto_preload=True, min_free_bytes=0)
    )
    try:
        for index in range(5):
            offset = index * BLOCK
            probe = await response(
                pool,
                payload,
                request(f"bytes={offset}-{offset}"),
                request_id="same-video",
                preview=True,
            )
            assert await response_body(probe) == b"x"
        assert not manager._jobs and not manager._auto_tasks
        row = next(iter(manager._observed.values()))
        assert not row["runs"]
        assert all(not viewer["active"] for viewer in row["viewers"].values())
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_alternate_quality_does_not_schedule_original_file_preloads(tmp_path):
    original = b"o" * (8 * BLOCK)
    alternate = b"a" * (5 * BLOCK)
    client = account("bot", original)
    client.native.payloads[2] = alternate
    source = TelegramMessage(
        client, message(1, len(original), [document(2, len(alternate))])
    )
    client.get_messages = AsyncMock(return_value=source)
    quality = client.describe_media(source)["variants"][0]["id"]
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    manager = PreloadManager(
        pool, policy=MediaCachePolicy(auto_preload=True, min_free_bytes=0)
    )
    try:
        selected = await response(pool, original, quality=quality)
        assert await response_body(selected) == alternate
        assert not manager._observed and not manager._jobs and not manager._auto_tasks
        assert all(identity == 2 for identity, _offset in client.native.requests)
        files = (await manager.cache.snapshot())["files"]
        assert len(files) == 1 and files[0]["complete"]
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "preview,media_destination,download,classified",
    [
        (False, False, False, False),
        (True, False, False, True),
        (False, True, False, True),
        (True, True, True, False),
    ],
)
async def test_only_identified_playback_uses_media_duration_for_background_warming(
    tmp_path, preview, media_destination, download, classified
):
    from telethon import types

    payload = b"x" * (2 * BLOCK)
    client = account("bot", payload)
    source = message(1, len(payload))
    source.document.attributes.append(
        types.DocumentAttributeVideo(duration=3600, w=1920, h=1080)
    )
    client.get_messages = AsyncMock(return_value=TelegramMessage(client, source))
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    manager = PreloadManager(
        pool, policy=MediaCachePolicy(auto_preload=True, min_free_bytes=0)
    )
    req = request(
        f"bytes=0-{BLOCK - 1}",
        **({"sec_fetch_dest": "video"} if media_destination else {}),
    )
    try:
        selected = await response(
            pool, payload, req, preview=preview, download=download
        )
        assert await response_body(selected) == payload[:BLOCK]
        assert bool(manager._observed) is classified
        if classified:
            observed = next(iter(manager._observed.values()))
            viewer = next(iter(observed["viewers"].values()))
            assert viewer["media_rate"] == len(payload) / 3600
            assert not viewer["allow_full"]
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.parametrize(
    "duration", [0, -1, float("nan"), float("inf"), 1e-320, True, "60"]
)
def test_invalid_telegram_duration_cannot_create_unbounded_bitrate(duration):
    from types import SimpleNamespace

    from tgdrive.media.streaming import _media_byte_rate

    source = SimpleNamespace(
        document=SimpleNamespace(attributes=[SimpleNamespace(duration=duration)])
    )
    assert _media_byte_rate(source, BLOCK) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("multipart", [False, True])
async def test_completed_finite_playback_range_warms_next_chunk_for_reuse(
    tmp_path, multipart
):
    import asyncio

    contents = [b"a" * (6 * BLOCK), b"b" * (6 * BLOCK)]
    payload = b"".join(contents)
    client = account("bot", payload)
    parts = build_parts(contents) if multipart else None
    if parts:
        client.native.payloads = {9001: contents[0], 9002: contents[1]}

        async def fetch(_channel, *, message_ids):
            part = parts[message_ids - 101]
            source = message(part["document_id"], part["size"])
            source.id = message_ids
            return TelegramMessage(client, source)

        client.get_messages = AsyncMock(side_effect=fetch)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    manager = PreloadManager(
        pool, policy=MediaCachePolicy(auto_preload=True, min_free_bytes=0)
    )
    try:
        selected = await stream_telegram_file(
            pool=pool,
            channel_id=CHANNEL,
            message_id=101 if parts else 77,
            file_name="video.mov",
            expected_size=len(payload),
            parts=parts,
            request=request(f"bytes=0-{4 * BLOCK - 1}"),
            preview=True,
            request_id="finite-player",
        )
        assert await response_body(selected) == payload[: 4 * BLOCK]
        for _ in range(100):
            if manager._jobs:
                break
            await asyncio.sleep(0.001)
        job = next(iter(manager._jobs.values()))
        task = manager._tasks.get(job["id"])
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), 3)
        assert job["state"] == "ready"
        assert job["start"] == 4 * BLOCK and job["end"] == 5 * BLOCK
        observed = next(iter(manager._observed.values()))
        viewer = observed["viewers"]["finite-player"]
        assert viewer["active"] == 0 and viewer["warm_until"] > manager._clock()
        reads = client.native.requests.copy()
        following = await stream_telegram_file(
            pool=pool,
            channel_id=CHANNEL,
            message_id=101 if parts else 77,
            file_name="video.mov",
            expected_size=len(payload),
            parts=parts,
            request=request(f"bytes={4 * BLOCK}-{5 * BLOCK - 1}"),
            download=True,
        )
        assert await response_body(following) == payload[4 * BLOCK : 5 * BLOCK]
        assert client.native.requests == reads
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_asgi_abort_does_not_grant_completed_playback_range_grace(tmp_path):
    import asyncio

    from starlette.requests import ClientDisconnect

    payload = b"x" * (12 * BLOCK)
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    manager = PreloadManager(
        pool, policy=MediaCachePolicy(auto_preload=True, min_free_bytes=0)
    )
    delivered = 0

    async def send(value):
        nonlocal delivered
        if value["type"] == "http.response.body":
            delivered += len(value.get("body", b""))
            if delivered >= 4 * BLOCK:
                raise OSError("Client disconnected during playback")
        await asyncio.sleep(0)

    async def receive():
        return {"type": "http.disconnect"}

    try:
        selected = await response(
            pool, payload, preview=True, request_id="aborted-player"
        )
        with pytest.raises(ClientDisconnect):
            await selected(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
            )
        await asyncio.gather(
            *list(manager._auto_tasks.values()),
            *list(manager._tasks.values()),
            return_exceptions=True,
        )
        viewer = next(iter(manager._observed.values()))["viewers"]["aborted-player"]
        assert delivered == 4 * BLOCK
        assert viewer["active"] == 0 and viewer["warm_until"] == 0
        assert not manager._viewer_timers
        assert all(
            job["state"] not in {"queued", "preparing"}
            for job in manager._jobs.values()
        )
        assert not manager.cache.memory.flights
        assert all(row.active_leases == 0 for row in await pool.status())
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("damaged_length", [0, BLOCK // 2 + 13], ids=["empty", "short"])
async def test_incomplete_origin_first_block_fails_over_before_headers_and_reuses_cache(
    tmp_path, damaged_length
):
    payload = b"x" * (3 * BLOCK // 4)
    bot, premium = account("bot", payload), account("premium", payload)
    premium.native.payloads[1] = payload[:damaged_length]
    pool = ClientPool([bot], [premium], media_cache_dir=tmp_path)
    await pool.start()
    try:
        selected = await response(pool, payload, request("bytes=17-255"), download=True)
        assert selected.status_code == 206
        assert selected.headers["content-length"] == "239"
        assert await response_body(selected) == payload[17:256]
        assert premium.native.iterators and bot.native.requests
        reads = (premium.native.requests.copy(), bot.native.requests.copy())
        repeated = await response(pool, payload, request("bytes=7-199"), download=True)
        assert await response_body(repeated) == payload[7:200]
        assert (premium.native.requests, bot.native.requests) == reads
        cache = pool._media_read_cache
        assert not cache.memory.flights and cache.scratch.used == 0
        assert all(
            len(entry[1]) == len(payload) for entry in cache.memory.entries.values()
        )
        assert all(row.active_leases == 0 for row in await pool.status())
    finally:
        if pool._media_read_cache is not None:
            await pool._media_read_cache.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("damaged_length", [0, BLOCK // 2 + 13], ids=["empty", "short"])
async def test_incomplete_origin_exhaustion_leaves_no_cache_or_lease_and_retry_recovers(
    tmp_path, damaged_length
):
    payload = b"x" * (3 * BLOCK // 4)
    client = account("bot", payload)
    client.native.payloads[1] = payload[:damaged_length]
    pool = ClientPool([client], media_cache_dir=tmp_path)
    # The synthetic account remains immediately available between attempts.
    pool.defer_client = AsyncMock()
    await pool.start()
    try:
        with pytest.raises(OSError, match="declared block length"):
            await response(pool, payload, request("bytes=17-255"), download=True)
        cache = pool._media_read_cache
        assert not cache.memory.entries and not cache.memory.flights
        assert not list(tmp_path.glob("*.block"))
        assert cache.scratch.used == 0 and not client.native.active
        assert all(row.active_leases == 0 for row in await pool.status())
        assert len(client.native.iterators) == 3
        client.native.payloads[1] = payload
        retried = await response(pool, payload, request("bytes=17-255"), download=True)
        assert await response_body(retried) == payload[17:256]
    finally:
        if pool._media_read_cache is not None:
            await pool._media_read_cache.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("finish_old", [False, True], ids=["abort-old", "finish-old"])
async def test_old_response_cannot_change_reopened_viewer_after_cache_clear(
    tmp_path, multipart, finish_old
):
    contents = [b"a" * (6 * BLOCK), b"b" * (6 * BLOCK)]
    payload = b"".join(contents)
    client = account("bot", payload)
    parts = build_parts(contents) if multipart else None
    if parts:
        client.native.payloads = {9001: contents[0], 9002: contents[1]}

        async def fetch(_channel, *, message_ids):
            part = parts[message_ids - 101]
            source = message(part["document_id"], part["size"])
            source.id = message_ids
            return TelegramMessage(client, source)

        client.get_messages = AsyncMock(side_effect=fetch)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    manager = PreloadManager(
        pool, policy=MediaCachePolicy(auto_preload=True, min_free_bytes=0)
    )
    first = second = None

    async def selected(start):
        return await stream_telegram_file(
            pool=pool,
            channel_id=CHANNEL,
            message_id=101 if parts else 77,
            file_name="movie.mov",
            expected_size=len(payload),
            parts=parts,
            request=request(f"bytes={start}-{start + 4 * BLOCK - 1}"),
            preview=True,
            request_id="reopened-player",
        )

    try:
        first = await selected(0)
        identity = next(iter(manager._observed))
        await manager.clear(identity)
        second = await selected(4 * BLOCK)
        viewer = manager._observed[identity]["viewers"]["reopened-player"]
        assert viewer["active"] == 1
        if finish_old:
            assert await response_body(first) == payload[: 4 * BLOCK]
        else:
            await first.body_iterator.aclose()
        assert viewer["active"] == 1 and viewer["warm_until"] == 0
        assert viewer["last_end"] is None and viewer["sequential"] == 0
        assert not manager._jobs and not manager._auto_tasks
        assert await response_body(second) == payload[4 * BLOCK : 8 * BLOCK]
        assert viewer["active"] == 0 and viewer["warm_until"] > manager._clock()
        assert viewer["sequential"] == 4 * BLOCK
    finally:
        if first is not None:
            await first.body_iterator.aclose()
        if second is not None:
            await second.body_iterator.aclose()
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_repeated_cancel_before_headers_drains_owned_metadata_lease(tmp_path):
    import asyncio

    payload = b"exact payload"
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    metadata_started = asyncio.Event()
    release_started = asyncio.Event()
    release_allowed = asyncio.Event()
    metadata_allowed = asyncio.Event()
    original_lease = pool.lease

    async def metadata(*_args, **_kwargs):
        metadata_started.set()
        await metadata_allowed.wait()
        return TelegramMessage(client, message(1, len(payload)))

    @asynccontextmanager
    async def delayed_release(**options):
        async with original_lease(**options) as owner:
            try:
                yield owner
            finally:
                release_started.set()
                await release_allowed.wait()

    client.get_messages = AsyncMock(side_effect=metadata)
    pool.lease = delayed_release
    pending = asyncio.create_task(response(pool, payload))
    try:
        await asyncio.wait_for(metadata_started.wait(), 2)
        pending.cancel()
        await asyncio.wait_for(release_started.wait(), 2)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert all(row.active_leases == 0 for row in await pool.status())
    finally:
        release_allowed.set()
        metadata_allowed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await pool.stop()
