from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from unittest.mock import AsyncMock

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect

from tests.media.test_media_scheduler import wait_until
from tests.media.test_multipart_streaming import build_parts
from tests.media.test_pooled_media import account
from tests.media.test_stream_delivery import request
from tests.media.test_streaming import response_body
from tests.media.test_telegram_cache import message
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import BLOCK, MediaReadCache
from tgdrive.media.streaming import stream_telegram_file
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import TelegramMessage

CHANNEL = -1000000000123
ASGI_SCOPE = {"type": "http", "asgi": {"spec_version": "2.4"}}


async def receive():
    return {"type": "http.disconnect"}


async def stream(pool, payload, header=None, *, method="GET", **options):
    req = request(header, accept_encoding="gzip, br, zstd")
    req.scope["method"] = method
    for name, value in options.pop("headers", {}).items():
        req.scope["headers"].append((name.encode(), value.encode()))
    return await stream_telegram_file(
        pool=pool,
        channel_id=CHANNEL,
        message_id=77,
        file_name="movie_टेस्ट.mkv",
        expected_size=len(payload),
        request=req,
        **options,
    )


async def assert_clean(pool):
    cache = pool._media_read_cache
    state = await cache.snapshot()
    assert state["scratch"]["bytes"] == 0
    assert state["scratch"]["active_streams"] == 0
    assert state["scratch"]["waiting_heads"] == 0
    assert state["scratch"]["peak_bytes"] <= 32 * BLOCK
    assert not cache.memory.flights
    assert not cache._active_keys
    assert not cache._foreground
    assert all(row.active_leases == 0 for row in await pool.status())


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["ram", "persisted-disk"])
async def test_fully_cached_http_seeks_tail_and_download_pass_a_stalled_full_response(
    tier, tmp_path, monkeypatch
):
    """ASGI backpressure must not let one viewer pin all speculative frames."""
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    payload = b"".join(bytes([index]) * BLOCK for index in range(40)) + b"exact-tail"
    clients = [account(str(index), payload) for index in range(3)]
    policy = MediaCachePolicy(
        memory_max_bytes=128 * BLOCK if tier == "ram" else 0,
        disk_max_bytes=128 * BLOCK if tier == "persisted-disk" else 0,
        min_free_bytes=0,
        scratch_max_bytes=32 * BLOCK,
    )
    pool = ClientPool(clients, media_cache_dir=tmp_path)
    pool._media_read_cache = MediaReadCache(tmp_path, policy)
    await pool.start()
    stalled = None
    release_send = asyncio.Event()
    entered_send = asyncio.Event()
    sent = bytearray()
    try:
        initial = await stream(pool, payload)
        etag = initial.headers["etag"]
        assert await response_body(initial) == payload
        await pool._media_read_cache.flush()
        if tier == "persisted-disk":
            await pool._media_read_cache.close()
            pool._media_read_cache = MediaReadCache(tmp_path, policy)
        cache = pool._media_read_cache
        ready = (await cache.snapshot())["files"]
        assert len(ready) == 1 and ready[0]["complete"]
        reads = sum(sum(client.native.requests.values()) for client in clients)

        head = await stream(pool, payload, method="HEAD")
        assert head.body == b"" and head.status_code == 200
        assert head.headers["content-length"] == str(len(payload))
        assert head.headers["accept-ranges"] == "bytes"
        assert head.headers["content-encoding"] == "identity"
        assert cache.scratch.used == 0

        slow = await stream(pool, payload, request_id="stalled-viewer")

        async def slow_send(value):
            if value["type"] == "http.response.body" and value.get("body"):
                sent.extend(value["body"])
                if not entered_send.is_set():
                    entered_send.set()
                    await release_send.wait()

        stalled = asyncio.create_task(slow(ASGI_SCOPE, receive, slow_send))
        await entered_send.wait()
        # A consumer blocked on its first send must shed speculative frames;
        # the yielded head remains owned while other requests pass it.
        await wait_until(lambda: cache.scratch.used == 1)
        assert not stalled.done()

        selections = [
            ("bytes=0-0", 0, 1),
            (
                f"bytes={31 * BLOCK - 7}-{31 * BLOCK + 8}",
                31 * BLOCK - 7,
                31 * BLOCK + 9,
            ),
            (
                f"bytes={35 * BLOCK + 13}-{36 * BLOCK + 22}",
                35 * BLOCK + 13,
                36 * BLOCK + 23,
            ),
            ("bytes=-257", len(payload) - 257, len(payload)),
            (f"bytes={39 * BLOCK}-", 39 * BLOCK, len(payload)),
        ]

        async def selected(header, start, end):
            result = await stream(
                pool,
                payload,
                header,
                headers={"if-range": etag},
                request_id=f"seek-{start}",
            )
            assert result.status_code == 206
            assert (
                result.headers["content-range"]
                == f"bytes {start}-{end - 1}/{len(payload)}"
            )
            assert result.headers["content-length"] == str(end - start)
            assert result.headers["content-encoding"] == "identity"
            assert result.headers["etag"] == etag
            assert await response_body(result) == payload[start:end]

        async def full_download():
            result = await stream(
                pool, payload, download=True, request_id="another-download"
            )
            assert result.status_code == 200
            assert result.headers["content-length"] == str(len(payload))
            assert result.headers["content-disposition"].startswith("attachment;")
            assert "filename*=UTF-8''movie_" in result.headers["content-disposition"]
            assert result.headers["content-encoding"] == "identity"
            assert await response_body(result) == payload

        # The deadline guards liveness rather than asserting a throughput target.
        # Every byte is local; a blocked reader has no dependency on the slow send.
        async with asyncio.timeout(10):
            await asyncio.gather(
                *(selected(*selection) for selection in selections), full_download()
            )
        assert not stalled.done() and sent == payload[:BLOCK]
        assert sum(sum(client.native.requests.values()) for client in clients) == reads
        assert all(row.active_leases == 0 for row in await pool.status())

        release_send.set()
        await asyncio.wait_for(stalled, 10)
        assert bytes(sent) == payload
        assert sum(sum(client.native.requests.values()) for client in clients) == reads
        await assert_clean(pool)
    finally:
        if stalled is not None and not stalled.done():
            stalled.cancel()
            await asyncio.gather(stalled, return_exceptions=True)
        release_send.set()
        await pool._media_read_cache.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_cached_http_disconnect_resumes_exactly_with_strong_validator(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    payload = b"".join(bytes([index]) * BLOCK for index in range(6)) + b"last"
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=16 * BLOCK, disk_max_bytes=0, min_free_bytes=0
        ),
    )
    await pool.start()
    received = bytearray()
    try:
        assert await response_body(await stream(pool, payload)) == payload
        reads = sum(client.native.requests.values())
        interrupted = await stream(pool, payload, download=True)
        etag = interrupted.headers["etag"]

        async def failed_send(value):
            if value["type"] == "http.response.body" and value.get("body"):
                if received:
                    raise OSError("client disconnected")
                received.extend(value["body"])

        with pytest.raises(ClientDisconnect):
            await interrupted(ASGI_SCOPE, receive, failed_send)
        assert received == payload[:BLOCK]
        await assert_clean(pool)

        resumed = await stream(
            pool,
            payload,
            f"bytes={len(received)}-",
            headers={"if-range": etag},
            download=True,
        )
        assert resumed.status_code == 206
        assert resumed.headers["content-length"] == str(len(payload) - len(received))
        received.extend(await response_body(resumed))
        assert bytes(received) == payload
        assert sum(client.native.requests.values()) == reads
        await assert_clean(pool)
    finally:
        await pool._media_read_cache.close()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["ram", "persisted-disk"])
async def test_cached_multipart_http_head_boundaries_and_resume_are_byte_exact(
    tier, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    contents = [b"a" * BLOCK + b"first-seam", b"b" * BLOCK + b"second-seam", b"tail"]
    payload = b"".join(contents)
    parts = build_parts(contents)
    client = account("bot", b"")
    client.native.payloads = {
        9001 + index: content for index, content in enumerate(contents)
    }

    async def fetch(_channel, *, message_ids):
        index = message_ids - 101
        native = message(9001 + index, len(contents[index]))
        native.id = message_ids
        return TelegramMessage(client, native)

    client.get_messages = AsyncMock(side_effect=fetch)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    policy = MediaCachePolicy(
        memory_max_bytes=16 * BLOCK if tier == "ram" else 0,
        disk_max_bytes=16 * BLOCK if tier == "persisted-disk" else 0,
        min_free_bytes=0,
    )
    pool._media_read_cache = MediaReadCache(tmp_path, policy)
    await pool.start()

    async def get(header=None, *, method="GET", **headers):
        req = request(header, accept_encoding="gzip", **headers)
        req.scope["method"] = method
        return await stream_telegram_file(
            pool=pool,
            channel_id=CHANNEL,
            message_id=101,
            file_name="split-movie.mkv",
            expected_size=len(payload),
            parts=parts,
            request=req,
        )

    try:
        first = await get()
        etag = first.headers["etag"]
        assert await response_body(first) == payload
        await pool._media_read_cache.flush()
        if tier == "persisted-disk":
            await pool._media_read_cache.close()
            pool._media_read_cache = MediaReadCache(tmp_path, policy)
        reads = sum(client.native.requests.values())
        metadata = client.get_messages.call_count
        head = await get(method="HEAD")
        assert head.status_code == 200 and head.body == b""
        assert head.headers["content-length"] == str(len(payload))
        assert head.headers["etag"] == etag
        assert (
            client.get_messages.call_count == metadata + 1
        )  # Fresh metadata, no payload.
        assert sum(client.native.requests.values()) == reads

        for boundary in (len(contents[0]), len(contents[0]) + len(contents[1])):
            start, end = boundary - 3, min(len(payload), boundary + 3)
            partial = await get(f"bytes={start}-{end - 1}", if_range=etag)
            assert partial.status_code == 206
            assert (
                partial.headers["content-range"]
                == f"bytes {start}-{end - 1}/{len(payload)}"
            )
            assert partial.headers["content-length"] == str(end - start)
            assert partial.headers["content-encoding"] == "identity"
            assert await response_body(partial) == payload[start:end]
        stale = await get("bytes=2-3", if_range='"old-file"')
        assert stale.status_code == 200
        assert await response_body(stale) == payload
        conditional = await get(if_none_match=etag)
        assert conditional.status_code == 304 and conditional.body == b""
        invalid = await get(f"bytes={len(payload)}-")
        assert invalid.status_code == 416
        assert invalid.headers["content-range"] == f"bytes */{len(payload)}"
        assert sum(client.native.requests.values()) == reads
        await assert_clean(pool)
    finally:
        await pool._media_read_cache.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_tcp_stalled_cached_download_does_not_block_a_different_video(
    tmp_path, monkeypatch
):
    """Exercise Uvicorn/TCP backpressure, not an eagerly collected ASGI client."""
    monkeypatch.setattr(
        "tgdrive.media.media_reader._effective_memory", lambda value: value
    )
    binary = b"".join(bytes([index]) * BLOCK for index in range(128))
    video = (
        Path(__file__).resolve().parents[1] / "fixtures" / "multitrack.mkv"
    ).read_bytes()
    clients = [account(str(index), binary) for index in range(3)]
    for client in clients:
        client.native.payloads[2] = video

        def fetch(_channel, *, message_ids, owner=client):
            identity, payload = (1, binary) if message_ids == 77 else (2, video)
            native = message(identity, len(payload))
            native.id = message_ids
            return TelegramMessage(owner, native)

        client.get_messages = AsyncMock(side_effect=fetch)
    pool = ClientPool(clients, media_cache_dir=tmp_path)
    pool._media_read_cache = MediaReadCache(
        tmp_path,
        MediaCachePolicy(
            memory_max_bytes=256 * BLOCK,
            disk_max_bytes=0,
            min_free_bytes=0,
            scratch_max_bytes=32 * BLOCK,
        ),
    )
    await pool.start()
    cache = pool._media_read_cache
    app = FastAPI()

    @app.api_route("/media/{kind}", methods=["GET", "HEAD"])
    async def media(kind: str, request: Request):
        is_binary = kind == "binary"
        return await stream_telegram_file(
            pool=pool,
            channel_id=CHANNEL,
            message_id=77 if is_binary else 78,
            file_name="large.bin" if is_binary else "multitrack.mkv",
            expected_size=len(binary if is_binary else video),
            download=is_binary,
            request=request,
        )

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
        )
    )
    serving = None
    slow_writer = None
    try:
        assert await response_body(await stream(pool, binary)) == binary
        warm_video = await media("video", request())
        assert await response_body(warm_video) == video
        reads = sum(sum(client.native.requests.values()) for client in clients)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        await wait_until(lambda: server.started)
        # StreamReader pauses socket reads at a small bounded buffer. The 128MiB
        # response quickly exceeds TCP/ASGI buffers and suspends its body send.
        slow_reader, slow_writer = await asyncio.open_connection(
            "127.0.0.1", port, limit=4096
        )
        slow_writer.write(
            b"GET /media/binary HTTP/1.1\r\nHost: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )
        await slow_writer.drain()
        headers = await asyncio.wait_for(slow_reader.readuntil(b"\r\n\r\n"), 5)
        assert headers.startswith(b"HTTP/1.1 200")
        assert b"content-length: 134217728" in headers.lower()
        # TCP backpressure leaves only the response head reserved after the
        # adaptive pipeline retires unused read-ahead.
        await wait_until(lambda: cache.scratch.used == 1)

        async with httpx2.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=10
        ) as http:
            head = await http.head("/media/video")
            assert head.status_code == 200 and head.headers["content-length"] == str(
                len(video)
            )
            results = await asyncio.gather(
                http.get("/media/video", headers={"Range": "bytes=0-4095"}),
                http.get("/media/video", headers={"Range": "bytes=-4096"}),
                http.get("/media/video"),
                http.get(
                    "/media/binary", headers={"Range": f"bytes={127 * BLOCK + 11}-"}
                ),
            )
            assert [result.status_code for result in results] == [206, 206, 200, 206]
            assert results[0].content == video[:4096]
            assert results[1].content == video[-4096:]
            assert results[2].content == video
            assert results[3].content == binary[127 * BLOCK + 11 :]
            assert all(
                result.headers["content-encoding"] == "identity" for result in results
            )
        assert (
            cache._active_streams == 1
        )  # The deliberately unread download remains live.
        assert sum(sum(client.native.requests.values()) for client in clients) == reads
        assert all(row.active_leases == 0 for row in await pool.status())
        slow_writer.close()
        await slow_writer.wait_closed()
        slow_writer = None
        await wait_until(lambda: cache._active_streams == 0)
        await assert_clean(pool)
    finally:
        if slow_writer is not None:
            slow_writer.close()
            await slow_writer.wait_closed()
        if serving is not None:
            server.should_exit = True
            await asyncio.wait_for(serving, 10)
        listener.close()
        await cache.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_browser_fixture_uses_the_real_pooled_cache_and_closes_it():
    """Browser decoding tests must not silently bypass cache/backpressure code."""
    from tests.support.browser_transfer_app import FixtureRuntime, settings
    from tgdrive.telegram.clients import _block_identity

    runtime = FixtureRuntime()
    await runtime.start()
    cache = runtime.pool._media_read_cache
    try:
        async with runtime.pool.lease() as client:
            source = await client.get_messages(
                settings.storage_channel, message_ids=101
            )
        assert _block_identity(source, 0) is not None
        selected = await stream_telegram_file(
            pool=runtime.pool,
            channel_id=settings.storage_channel,
            message_id=101,
            file_name="résumé.txt",
            expected_size=14,
            request=request("bytes=0-3"),
        )
        assert await response_body(selected) == b"publ"
        files = (await cache.snapshot())["files"]
        assert len(files) == 1 and files[0]["ready_ranges"] == [[0, 14]]
        assert runtime.preloads.cache is cache
        # Match production's wrapper metadata as well as its immutable native
        # document. Size-only native documents break logical multipart reads.
        assert source.document.file_size == 14
        import hashlib

        manifest = [
            {
                "index": index,
                "offset": 0 if index == 0 else 14,
                "size": len(runtime.payloads[message_id]),
                "message_id": message_id,
                "sha256": hashlib.sha256(runtime.payloads[message_id]).hexdigest(),
            }
            for index, message_id in enumerate((101, 102))
        ]
        boundary = await stream_telegram_file(
            pool=runtime.pool,
            channel_id=settings.storage_channel,
            message_id=101,
            file_name="logical.bin",
            expected_size=29,
            parts=manifest,
            request=request("bytes=12-16"),
        )
        assert boundary.status_code == 206
        assert boundary.headers["content-range"] == "bytes 12-16/29"
        assert boundary.headers["content-length"] == "5"
        assert await response_body(boundary) == b"ntpri"
        await assert_clean(runtime.pool)
    finally:
        await runtime.stop()
    assert cache._closed and runtime.preloads._closed
