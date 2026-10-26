from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from telethon import types, utils
from telethon.errors import FileReferenceExpiredError

from tgdrive.telegram.clients import Client, TelegramMessage

MIB = 1024**2
CHANNEL = -1001234567890


def client(native):
    value = Client.__new__(Client)
    value.native = native
    return value


class PartIterator:
    def __init__(self, owner, offset, limit):
        self.owner, self.offset, self.limit = owner, offset, limit
        self.count = 0
        self.closed = False
        owner.iterators.append(self)
        owner.active += 1
        owner.peak = max(owner.peak, owner.active)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.count >= self.limit or self.offset >= len(self.owner.payload):
            raise StopAsyncIteration
        self.count += 1
        self.owner.requests += 1
        if self.owner.gate:
            await self.owner.gate.wait()
        else:
            await asyncio.sleep(0.001 * (4 - self.offset // MIB % 4))
        part = self.owner.payload[self.offset : self.offset + MIB // 2]
        self.offset += len(part)
        return part

    async def close(self):
        if not self.closed:
            self.closed = True
            self.owner.active -= 1


class Native:
    def __init__(self, payload, gate=None):
        self.payload, self.gate = payload, gate
        self.requests = self.active = self.peak = 0
        self.iterators = []

    def iter_download(self, message, *, offset, request_size, limit):
        assert request_size == MIB // 2
        return PartIterator(self, offset, limit)


@pytest.mark.asyncio
async def test_prefetch_reorders_completed_windows_and_preserves_exact_tail():
    payload = b"".join(bytes([i]) * MIB for i in range(7)) + b"tail"
    native = Native(payload)
    parts = [
        part
        async for part in client(native).stream_media(
            SimpleNamespace(), limit=6, offset=2
        )
    ]
    assert b"".join(parts) == payload[2 * MIB :]
    assert native.peak == 4 and native.active == 0
    assert all(item.closed for item in native.iterators)


@pytest.mark.asyncio
async def test_slow_consumer_has_bounded_read_ahead_and_close_drains_every_iterator():
    native = Native(b"x" * (32 * MIB))
    stream = client(native).stream_media(SimpleNamespace(), limit=32, offset=0)
    assert len(await anext(stream)) == MIB
    await asyncio.sleep(0.03)
    assert native.requests == 8
    assert len(native.iterators) == 4
    await stream.aclose()
    assert native.active == 0 and all(item.closed for item in native.iterators)


@pytest.mark.asyncio
async def test_request_cancellation_drains_blocked_prefetch_workers():
    native = Native(b"x" * (8 * MIB), asyncio.Event())

    async def consume():
        return [
            part
            async for part in client(native).stream_media(
                SimpleNamespace(), limit=8, offset=0
            )
        ]

    task = asyncio.create_task(consume())
    for _ in range(20):
        await asyncio.sleep(0)
        if native.active == 4:
            break
    assert native.active == 4
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert native.active == 0 and all(item.closed for item in native.iterators)


def document(identity, size=2 * MIB, reference=b"current"):
    return types.Document(
        id=identity,
        access_hash=123,
        file_reference=reference,
        date=datetime.now(UTC),
        mime_type="video/mp4",
        size=size,
        dc_id=2,
        attributes=[
            types.DocumentAttributeVideo(
                duration=3.0, w=1280, h=720, supports_streaming=True
            )
        ],
    )


def message(original, alternatives=(), message_id=77):
    return SimpleNamespace(
        id=message_id,
        chat_id=CHANNEL,
        document=original,
        photo=None,
        media=types.MessageMediaDocument(
            document=original, alt_documents=list(alternatives)
        ),
    )


def test_variant_descriptors_and_selection_are_bound_to_the_source_message():
    original, alternate = document(1, 3 * MIB), document(2)
    transport = client(SimpleNamespace())
    source = message(original, [alternate])
    info = transport.describe_media(source)
    assert info["original"]["id"] == "original"
    assert info["original"]["is_original"] is True
    descriptor = info["variants"][0]
    assert descriptor["height"] == 720 and descriptor["label"] == "720p"
    assert set(descriptor) == {
        "id",
        "label",
        "width",
        "height",
        "duration",
        "size",
        "mime_type",
        "is_original",
    }
    selected = transport.select_media_variant(source, descriptor["id"])
    assert selected.download_media is alternate
    assert selected.document.file_size == 2 * MIB
    assert transport.select_media_variant(source, "original") is source
    assert transport.select_media_variant(selected, "original") is source
    with pytest.raises(ValueError, match="unavailable"):
        transport.select_media_variant(
            message(original, [alternate], message_id=78), descriptor["id"]
        )
    assert transport.describe_media(message(original))["variants"] == []


@pytest.mark.asyncio
async def test_selected_document_is_downloaded_and_reselected_after_reference_refresh():
    original = document(1)
    stale, fresh = document(2, reference=b"stale"), document(2, reference=b"fresh")
    source, refreshed = message(original, [stale]), message(original, [fresh])
    closed = []

    class Iterator:
        def __init__(self, media):
            self.media = media

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.media.file_reference == b"stale":
                raise FileReferenceExpiredError(request=None)
            if getattr(self, "sent", False):
                raise StopAsyncIteration
            self.sent = True
            return b"video"

        async def close(self):
            closed.append(self.media)

    native = SimpleNamespace(
        iter_download=Mock(side_effect=lambda media, **_: Iterator(media)),
        get_messages=AsyncMock(return_value=refreshed),
    )
    transport = client(native)
    # Avoid wrapping synthetic messages: refresh still exercises bound reselection.
    transport.get_messages = AsyncMock(return_value=refreshed)
    quality = transport.describe_media(source)["variants"][0]["id"]
    selected = transport.select_media_variant(source, quality)
    assert (
        b"".join(
            [part async for part in transport.stream_media(selected, limit=1, offset=0)]
        )
        == b"video"
    )
    assert [call.args[0] for call in native.iter_download.call_args_list] == [
        stale,
        fresh,
    ]
    transport.get_messages.assert_awaited_once_with(CHANNEL, message_ids=77)
    assert closed == [stale, fresh]


def test_photo_sizes_are_discovered_without_claiming_document_image_variants():
    sizes = [
        types.PhotoSize(type="m", w=320, h=240, size=100),
        types.PhotoSizeProgressive(type="x", w=1280, h=960, sizes=[200, 400]),
    ]
    photo = types.Photo(
        id=7,
        access_hash=8,
        file_reference=b"ref",
        date=datetime.now(UTC),
        sizes=sizes,
        dc_id=2,
    )
    source = SimpleNamespace(id=77, chat_id=CHANNEL, document=None, photo=photo)
    transport = client(SimpleNamespace())
    info = transport.describe_media(source)
    assert info["original"]["size"] == 400
    assert info["variants"][0]["width"] == 320
    selection = transport.select_media_variant(source, info["variants"][0]["id"])
    assert selection.download_media.sizes == [sizes[0]]
    assert source.photo.sizes == sizes
    assert transport.describe_media(
        SimpleNamespace(id=77, chat_id=CHANNEL, document=None, photo=None)
    ) == {"original": None, "variants": []}


@pytest.mark.asyncio
async def test_scheduled_processing_identity_is_never_committed_as_channel_message():
    native = SimpleNamespace(get_messages=AsyncMock())
    result = SimpleNamespace(
        updates=[
            types.UpdateNewScheduledMessage(
                message=SimpleNamespace(id=77, video_processing_pending=True)
            )
        ]
    )
    with pytest.raises(RuntimeError, match="processing scheduled"):
        await client(native)._sent(CHANNEL, result)
    native.get_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_refresh_refuses_to_switch_to_replaced_original_media():
    source = message(document(1))
    replacement = message(document(99))

    class BrokenIterator:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise FileReferenceExpiredError(request=None)

        async def close(self):
            self.closed = True

    iterator = BrokenIterator()
    transport = client(SimpleNamespace(iter_download=Mock(return_value=iterator)))
    transport.get_messages = AsyncMock(return_value=replacement)
    with pytest.raises(FileNotFoundError, match="replaced"):
        _ = [part async for part in transport.stream_media(source, limit=1, offset=0)]
    assert iterator.closed


@pytest.mark.asyncio
async def test_parallel_reference_failures_share_one_message_refresh():
    source, refreshed = message(document(1)), message(document(1, reference=b"fresh"))
    base = Native(b"x" * (4 * MIB))
    stale_closed = []

    class ExpiredIterator:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0)
            raise FileReferenceExpiredError(request=None)

        async def close(self):
            stale_closed.append(True)

    def iterator(target, **options):
        if target is source:
            return ExpiredIterator()
        return base.iter_download(target, **options)

    transport = client(SimpleNamespace(iter_download=Mock(side_effect=iterator)))

    async def fetch(*_args, **_kwargs):
        await asyncio.sleep(0.005)
        return refreshed

    transport.get_messages = AsyncMock(side_effect=fetch)
    result = b"".join(
        [part async for part in transport.stream_media(source, limit=4, offset=0)]
    )
    assert result == base.payload
    transport.get_messages.assert_awaited_once_with(CHANNEL, message_ids=77)
    assert len(stale_closed) == 4 and base.active == 0


class ValidatingNative(Native):
    """Exercise Telethon's actual file-location coercion before yielding bytes."""

    def iter_download(self, target, **options):
        dc_id, location = utils.get_input_location(target)
        assert dc_id == 2 and isinstance(location, types.InputDocumentFileLocation)
        assert location.id == 1
        return super().iter_download(target, **options)


def real_message():
    return types.Message(
        id=77,
        peer_id=types.PeerChannel(123),
        date=datetime.now(UTC),
        message="",
        media=types.MessageMediaDocument(document=document(1, size=8)),
    )


@pytest.mark.asyncio
async def test_real_message_download_method_is_never_mistaken_for_media_target():
    native = ValidatingNative(b"original")
    transport = client(native)
    wrapped = TelegramMessage(transport, real_message())
    assert callable(wrapped.download_media)
    chunks = [part async for part in transport.stream_media(wrapped, limit=1, offset=0)]
    assert b"".join(chunks) == b"original"
    assert native.active == 0


def test_real_message_first_yield_survives_base_http_middleware():
    from tgdrive.media.streaming import stream_telegram_file

    native = ValidatingNative(b"original")
    native.get_messages = AsyncMock(return_value=real_message())
    transport = client(native)

    class Pool:
        @asynccontextmanager
        async def lease(self, **options):
            yield transport

        def kind_of(self, value):
            return "bot"

    pool = Pool()
    app = FastAPI()

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Test"] = "wrapped"
        return response

    @app.get("/file")
    async def file(request: Request):
        return await stream_telegram_file(
            pool=pool,
            channel_id=CHANNEL,
            message_id=77,
            file_name="video.mp4",
            expected_size=8,
            request=request,
        )

    with TestClient(app) as http:
        response = http.get("/file")
        repeated = http.get("/file")
    assert response.content == repeated.content
    assert native.get_messages.await_count == 2
    assert native.requests == 1  # The second HTTP request reuses bytes, not metadata.
    assert response.status_code == 200
    assert response.content == b"original"
    assert response.headers["Content-Length"] == "8"
    assert response.headers["X-Test"] == "wrapped"
    assert native.active == 0
