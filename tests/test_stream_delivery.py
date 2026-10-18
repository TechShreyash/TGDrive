from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from test_multipart_streaming import MultipartClient, build_parts
from test_pooled_media import account
from test_streaming import make_request, response_body
from test_telegram_cache import message

from tgdrive.client_pool import ClientPool, MediaQueueFull
from tgdrive.media_policy import MediaCachePolicy
from tgdrive.media_reader import BLOCK
from tgdrive.preloading import PreloadManager
from tgdrive.streaming import _not_modified, media_etag, stream_telegram_file
from tgdrive.telegram_clients import TelegramMessage

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
            )
            assert await response_body(probe) == b"x"
        assert not manager._jobs and not manager._auto_tasks
        row = next(iter(manager._observed.values()))
        assert not row["runs"]
        assert all(not viewer["active"] for viewer in row["viewers"].values())
    finally:
        await manager.close()
        await pool.stop()
