from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import ClientDisconnect, Request

from tgdrive.media.streaming import TelegramFileNotFound, stream_telegram_file
from tgdrive.telegram.client_pool import ClientPool


class FakeStreamClient:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.is_connected = False
        self.name = "stream-test"

    async def start(self) -> None:
        self.is_connected = True

    async def stop(self) -> None:
        self.is_connected = False

    async def get_messages(self, _channel: int, *, message_ids: int):
        assert message_ids == 7
        return SimpleNamespace(
            empty=False,
            document=SimpleNamespace(file_size=len(self.content)),
            id=message_ids,
        )

    async def stream_media(self, _message: object, *, limit: int, offset: int):
        chunk_size = 1024 * 1024
        end = offset + limit if limit else None
        chunks = [
            self.content[index : index + chunk_size]
            for index in range(0, len(self.content), chunk_size)
        ]
        for chunk in chunks[offset:end]:
            yield chunk


def make_request(range_header: str | None = None) -> Request:
    headers = [] if range_header is None else [(b"range", range_header.encode())]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/file",
            "headers": headers,
            "query_string": b"",
            "server": ("test", 443),
            "client": ("test", 123),
            "scheme": "https",
        }
    )


async def response_body(response: object) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("range_header", "expected", "status"),
    [
        (None, b"a" * (1024 * 1024) + b"bc", 200),
        ("bytes=0-0", b"a", 206),
        ("bytes=1048575-1048576", b"ab", 206),
        ("bytes=-2", b"bc", 206),
    ],
    ids=["full", "first-byte", "chunk-boundary", "suffix"],
)
async def test_streaming_emits_exact_ranges(
    range_header: str | None, expected: bytes, status: int
) -> None:
    content = b"a" * (1024 * 1024) + b"bc"
    pool = ClientPool([FakeStreamClient(content)])
    await pool.start()

    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="video.mp4",
        expected_size=len(content),
        request=make_request(range_header),
    )

    assert response.status_code == status
    assert int(response.headers["content-length"]) == len(expected)
    assert await response_body(response) == expected
    assert all(item.active_leases == 0 for item in await pool.status())
    await pool.stop()


@pytest.mark.asyncio
async def test_active_content_is_attachment_with_sandbox() -> None:
    pool = ClientPool([FakeStreamClient(b"<script>bad()</script>")])
    await pool.start()
    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="payload.html",
        expected_size=22,
        request=make_request(),
    )

    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["content-security-policy"].startswith("sandbox")
    assert response.headers["x-content-type-options"] == "nosniff"
    await response_body(response)
    await pool.stop()


@pytest.mark.asyncio
async def test_unsatisfiable_range_releases_lease() -> None:
    pool = ClientPool([FakeStreamClient(b"abc")])
    await pool.start()
    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="a.bin",
        expected_size=3,
        request=make_request("bytes=3-"),
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */3"
    assert all(item.active_leases == 0 for item in await pool.status())
    await pool.stop()


@pytest.mark.asyncio
async def test_cancellation_before_stream_response_releases_lease() -> None:
    entered = asyncio.Event()

    class WaitingClient(FakeStreamClient):
        async def get_messages(self, _channel: int, *, message_ids: int):
            entered.set()
            await asyncio.Event().wait()

    pool = ClientPool([WaitingClient(b"abc")])
    await pool.start()
    task = asyncio.create_task(
        stream_telegram_file(
            pool=pool,
            channel_id=-100,
            message_id=7,
            file_name="a.bin",
            expected_size=3,
            request=make_request(),
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await pool.health()).active_leases == 0
    await pool.stop(drain_timeout=1)


@pytest.mark.asyncio
async def test_response_header_failure_releases_unstarted_stream_lease() -> None:
    pool = ClientPool([FakeStreamClient(b"abc")])
    await pool.start()
    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="a.bin",
        expected_size=3,
        request=make_request(),
    )

    async def send(_message: object) -> None:
        raise OSError("browser disconnected before response headers")

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    assert (await pool.health()).active_leases == 0
    await pool.stop(drain_timeout=1)


class TrackingStreamClient(FakeStreamClient):
    def __init__(
        self,
        content: bytes,
        *,
        name: str,
        error: Exception | None = None,
        missing: bool = False,
    ) -> None:
        super().__init__(content)
        self.name = name
        self.error = error
        self.missing = missing
        self.fetches = 0
        self.streams = 0

    async def get_messages(self, channel: int, *, message_ids: int):
        self.fetches += 1
        if self.error:
            raise self.error
        if self.missing:
            return None
        return await super().get_messages(channel, message_ids=message_ids)

    async def stream_media(self, message: object, *, limit: int, offset: int):
        self.streams += 1
        async for chunk in super().stream_media(message, limit=limit, offset=offset):
            yield chunk


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_streaming_prefers_premium_for_ranges_and_head(method: str) -> None:
    bot = TrackingStreamClient(b"bot", name="bot")
    premium = TrackingStreamClient(b"user", name="premium")
    pool = ClientPool([bot], [premium])
    await pool.start()

    request = make_request("bytes=1-2")
    request.scope["method"] = method
    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="video.mp4",
        expected_size=4,
        request=request,
    )
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 1-2/4"
    assert premium.fetches == 1
    assert bot.fetches == 0
    if method == "GET":
        assert (await pool.status())[1].active_leases == 1
        assert await response_body(response) == b"se"
        assert premium.streams == 1
    else:
        assert response.body == b""
        assert premium.streams == 0
    assert (await pool.health()).active_leases == 0
    await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unhealthy", "disconnected"])
async def test_streaming_uses_bot_when_premium_unavailable(failure: str) -> None:
    bot = TrackingStreamClient(b"bot", name="bot")
    premium = TrackingStreamClient(b"user", name="premium")
    pool = ClientPool([bot], [premium])
    await pool.start()
    if failure == "unhealthy":
        await pool.mark_unhealthy(premium)
    else:
        premium.is_connected = False

    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="a.bin",
        expected_size=3,
        request=make_request(),
    )
    assert await response_body(response) == b"bot"
    assert premium.fetches == 0
    assert bot.fetches == 1
    assert (await pool.health()).active_leases == 0
    await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("failure", ["permission", "connection", "missing", "rpc"])
async def test_premium_metadata_failure_releases_then_retries_bot(
    method: str, failure: str
) -> None:
    from telethon.errors import ChannelPrivateError

    errors = {
        "permission": PermissionError("synthetic access failure"),
        "connection": ConnectionError("synthetic connection failure"),
        "rpc": ChannelPrivateError(request=None),
    }
    bot = TrackingStreamClient(b"bot", name="bot")
    premium = TrackingStreamClient(
        b"user", name="premium", error=errors.get(failure), missing=failure == "missing"
    )
    pool = ClientPool([bot], [premium])
    await pool.start()
    request = make_request()
    request.scope["method"] = method

    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="a.bin",
        expected_size=3,
        request=request,
    )
    assert premium.fetches == 1
    assert bot.fetches == 1
    assert (await pool.status())[1].active_leases == 0
    if method == "GET":
        assert await response_body(response) == b"bot"
    assert (await pool.health()).active_leases == 0
    await pool.stop()


@pytest.mark.asyncio
async def test_failed_premium_fallback_with_no_bot_does_not_mask_error() -> None:
    from tgdrive.telegram.client_pool import NoClientAvailable

    premium = TrackingStreamClient(
        b"user", name="premium", error=PermissionError("synthetic access failure")
    )
    pool = ClientPool([], [premium])
    await pool.start()
    with pytest.raises(NoClientAvailable):
        await stream_telegram_file(
            pool=pool,
            channel_id=-100,
            message_id=7,
            file_name="a.bin",
            expected_size=3,
            request=make_request(),
        )
    assert (await pool.health()).active_leases == 0
    await pool.stop()


@pytest.mark.asyncio
async def test_bot_metadata_failure_is_not_retried() -> None:
    bot = TrackingStreamClient(
        b"bot", name="bot", error=PermissionError("synthetic access failure")
    )
    pool = ClientPool([bot])
    await pool.start()
    with pytest.raises(PermissionError):
        await stream_telegram_file(
            pool=pool,
            channel_id=-100,
            message_id=7,
            file_name="a.bin",
            expected_size=3,
            request=make_request(),
        )
    assert bot.fetches == 1
    assert (await pool.health()).active_leases == 0
    await pool.stop()


@pytest.mark.asyncio
async def test_premium_cancellation_does_not_fall_back_or_leak() -> None:
    entered = asyncio.Event()

    class WaitingClient(TrackingStreamClient):
        async def get_messages(self, _channel: int, *, message_ids: int):
            entered.set()
            await asyncio.Event().wait()

    premium = WaitingClient(b"user", name="premium")
    bot = TrackingStreamClient(b"bot", name="bot")
    pool = ClientPool([bot], [premium])
    await pool.start()
    task = asyncio.create_task(
        stream_telegram_file(
            pool=pool,
            channel_id=-100,
            message_id=7,
            file_name="a.bin",
            expected_size=3,
            request=make_request(),
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bot.fetches == 0
    assert (await pool.health()).active_leases == 0
    await pool.stop()


@pytest.mark.asyncio
async def test_video_download_forces_attachment_and_original_even_with_quality():
    content = b"original-video-bytes"
    pool = ClientPool([FakeStreamClient(content)])
    await pool.start()
    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="movie.mp4",
        expected_size=len(content),
        request=make_request(),
        download=True,
        quality="unrelated-variant",
    )
    assert response.headers["content-disposition"].startswith("attachment;")
    assert await response_body(response) == content
    await pool.stop()


@pytest.mark.asyncio
async def test_pdf_preview_inline_only_with_explicit_preview_flag():
    content = b"%PDF-1.7 sample"
    pool = ClientPool([FakeStreamClient(content)])
    await pool.start()
    for preview, expected in ((False, "attachment;"), (True, "inline;")):
        response = await stream_telegram_file(
            pool=pool,
            channel_id=-100,
            message_id=7,
            file_name="manual.pdf",
            expected_size=len(content),
            request=make_request(),
            preview=preview,
        )
        assert response.headers["content-disposition"].startswith(expected)
        if preview:
            assert response.headers["x-frame-options"] == "SAMEORIGIN"
        assert await response_body(response) == content
    await pool.stop()


@pytest.mark.asyncio
async def test_video_inline_policy_allows_native_document_to_read_media():
    content = b"video"
    pool = ClientPool([FakeStreamClient(content)])
    await pool.start()
    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="movie.mp4",
        expected_size=len(content),
        request=make_request(),
    )
    assert "media-src 'self'" in response.headers["content-security-policy"]
    assert not response.headers["content-security-policy"].startswith("sandbox")
    assert await response_body(response) == content
    await pool.stop()


@pytest.mark.asyncio
async def test_response_body_send_failure_closes_iterator_before_lease_release() -> (
    None
):
    closed = asyncio.Event()
    produced = []

    class CancellableClient(FakeStreamClient):
        async def stream_media(self, message: object, *, limit: int, offset: int):
            try:
                async for chunk in super().stream_media(
                    message, limit=limit, offset=offset
                ):
                    produced.append(chunk)
                    yield chunk
            finally:
                closed.set()

    pool = ClientPool([CancellableClient(b"a" * (2 * 1024**2))])
    await pool.start()
    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="movie.mp4",
        expected_size=2 * 1024**2,
        request=make_request(),
    )

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("browser disconnected during playback")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert closed.is_set()
    assert len(produced) == 1
    assert (await pool.health()).active_leases == 0
    await pool.stop(drain_timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(("download", "status"), [(False, 413), (True, 200)])
async def test_large_pdf_download_overrides_preview_size_cap(download, status):
    from tgdrive.media.streaming import PDF_PREVIEW_BYTES

    size = PDF_PREVIEW_BYTES + 1

    class LargePdfClient(FakeStreamClient):
        async def get_messages(self, _channel: int, *, message_ids: int):
            return SimpleNamespace(
                empty=False, document=SimpleNamespace(file_size=size), id=message_ids
            )

    pool = ClientPool([LargePdfClient(b"")])
    await pool.start()
    request = make_request()
    request.scope["method"] = "HEAD"
    response = await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=7,
        file_name="large.pdf",
        expected_size=size,
        request=request,
        preview=True,
        download=download,
    )
    assert response.status_code == status
    if download:
        assert response.headers["content-disposition"].startswith("attachment;")
        assert int(response.headers["content-length"]) == size
    assert (await pool.health()).active_leases == 0
    await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_missing_or_changed_storage_message_fails_before_stream_headers(missing):
    class DamagedClient(FakeStreamClient):
        async def get_messages(self, channel, *, message_ids):
            if missing:
                return None
            return await super().get_messages(channel, message_ids=message_ids)

    pool = ClientPool([DamagedClient(b"changed payload")])
    await pool.start()
    try:
        with pytest.raises(TelegramFileNotFound) as error:
            await stream_telegram_file(
                pool=pool,
                channel_id=-100,
                message_id=7,
                file_name="original.bin",
                expected_size=3,
                request=make_request(),
            )
        assert "missing or changed" in error.value.public_detail
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()
