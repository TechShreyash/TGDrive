"""Offline contracts for the real Telethon request types used by the adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import functions, types
from telethon.errors import (
    FileReferenceExpiredError,
    FileReferenceInvalidError,
    FilerefUpgradeNeededError,
)

from tgdrive.telegram.clients import Client, TelegramMessage

MIB = 1024**2
CHANNEL = -1001234567890


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,media,handler",
    [
        ("/start", False, "_help"),
        ("Help", False, "_help"),
        ("Choose folder", False, "_set_folder"),
        ("/set_folder@mybot Projects", False, "_set_folder"),
        ("/folders", False, "_set_folder"),
        ("Current folder", False, "_current_folder"),
        ("/cancel", False, "_cancel"),
        ("Projects", False, "_folder_query"),
        ("   ", False, "_folder_query"),
        ("Choose folder", True, "_file"),
        ("Help", True, "_file"),
    ],
)
async def test_bot_transport_routes_menu_commands_and_media_captions(
    text, media, handler
):
    native = SimpleNamespace(add_event_handler=Mock())
    client = transport_client(native)
    handlers = {
        name: AsyncMock()
        for name in (
            "_help",
            "_set_folder",
            "_current_folder",
            "_cancel",
            "_folder_query",
            "_file",
        )
    }
    mode = SimpleNamespace(
        settings=SimpleNamespace(telegram_admin_ids=(1,)), **handlers
    )
    client.register_bot(mode)
    dispatch = native.add_event_handler.call_args_list[0].args[0]
    message = native_message()
    message.raw_text = text
    if not media:
        message.file = None
    event = SimpleNamespace(is_private=True, sender_id=1, message=message)
    await dispatch(event)
    handlers[handler].assert_awaited_once()
    assert sum(item.await_count for item in handlers.values()) == 1

    for private, sender in [(False, 1), (True, 999)]:
        event.is_private, event.sender_id = private, sender
        await dispatch(event)
    assert sum(item.await_count for item in handlers.values()) == 1


@pytest.mark.asyncio
async def test_bot_transport_returns_editable_replies_and_preserves_literal_text():
    sent = native_message()
    sent.edit = AsyncMock()
    original = native_message()
    original.reply = AsyncMock(return_value=sent)
    client = transport_client(SimpleNamespace())
    message = TelegramMessage(client, original)
    reply = await message.reply_text("Saving <file>…", reply_markup="menu")
    assert isinstance(reply, TelegramMessage)
    original.reply.assert_awaited_once_with(
        "Saving <file>…", buttons="menu", parse_mode=None
    )
    await reply.edit_text("Saved <file>", reply_markup="new buttons")
    sent.edit.assert_awaited_once_with(
        "Saved <file>", buttons="new buttons", parse_mode=None
    )


def native_message(message_id: int = 77) -> Any:
    return SimpleNamespace(
        id=message_id,
        chat_id=CHANNEL,
        sender_id=1,
        raw_text="caption",
        photo=None,
        file=SimpleNamespace(size=3, name="file.bin"),
    )


class NativeTransport:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.get_input_entity = AsyncMock(
            return_value=types.InputPeerChannel(channel_id=123, access_hash=456)
        )
        self.get_messages = AsyncMock(return_value=native_message())
        self.fail_first = False

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        if self.fail_first and len(self.requests) == 1:
            raise TimeoutError("synthetic ambiguous send")
        return SimpleNamespace(
            updates=[types.UpdateMessageID(id=77, random_id=request.random_id)]
        )


def transport_client(native: Any) -> Client:
    client = Client.__new__(Client)
    client.name = "storage-bot-123456"
    client.native = native
    client._upload_parts = AsyncMock(
        side_effect=[
            types.InputFileBig(id=1, parts=1, name="file.bin"),
            types.InputFileBig(id=2, parts=1, name="file.bin"),
        ]
    )
    return client


@pytest.mark.asyncio
async def test_send_replays_real_raw_request_with_the_persisted_random_id(
    tmp_path: Path,
) -> None:
    native = NativeTransport()
    native.fail_first = True
    client = transport_client(native)
    staged = tmp_path / "file.bin"
    staged.write_bytes(b"abc")
    operation_id = -(2**62) + 123
    progress = AsyncMock()
    with pytest.raises(TimeoutError, match="ambiguous"):
        await client.send_staged(
            CHANNEL,
            staged,
            file_name="file.bin",
            random_id=operation_id,
            progress=progress,
        )
    sent = await client.send_staged(
        CHANNEL, staged, file_name="file.bin", random_id=operation_id, progress=progress
    )

    assert sent.id == 77
    assert len(native.requests) == 2
    assert all(
        isinstance(value, functions.messages.SendMediaRequest)
        for value in native.requests
    )
    assert [value.random_id for value in native.requests] == [operation_id] * 2
    assert [value.media.file.id for value in native.requests] == [1, 2]
    assert all(value.silent for value in native.requests)
    assert all(
        isinstance(value.media, types.InputMediaUploadedDocument)
        and value.media.force_file
        for value in native.requests
    )
    assert native.requests[-1].media.attributes == [
        types.DocumentAttributeFilename(file_name="file.bin")
    ]
    assert all(
        call.kwargs["progress"] is progress
        for call in client._upload_parts.await_args_list
    )
    native.get_messages.assert_awaited_once_with(CHANNEL, ids=77)


@pytest.mark.asyncio
async def test_send_document_preserves_explicit_operation_id(tmp_path: Path) -> None:
    client = transport_client(SimpleNamespace())
    client.send_staged = AsyncMock(return_value="confirmed")
    staged = tmp_path / "checkpoint.json"
    result = await client.send_document(
        CHANNEL, staged, file_name="drive.json", random_id=999
    )
    assert result == "confirmed"
    client.send_staged.assert_awaited_once_with(
        CHANNEL, staged, file_name="drive.json", random_id=999, progress=None
    )


@pytest.mark.asyncio
async def test_copy_and_delete_keep_public_transport_contracts() -> None:
    native = NativeTransport()
    native.delete_messages = AsyncMock(return_value=2)
    client = transport_client(native)
    media = types.InputMediaDocument(
        id=types.InputDocument(id=12, access_hash=34, file_reference=b"fresh")
    )
    original = SimpleNamespace(media=media, raw_text="literal <caption>")
    copied = await client.copy_media(CHANNEL, original, random_id=456)
    request = native.requests[0]
    assert copied.id == 77
    assert isinstance(request, functions.messages.SendMediaRequest)
    assert request.random_id == 456 and request.media is media
    assert request.message == "literal <caption>"
    assert await client.delete_messages(CHANNEL, [77, 78]) == 2
    native.delete_messages.assert_awaited_once_with(CHANNEL, [77, 78], revoke=True)


class DownloadIterator:
    def __init__(self, steps: list[Any]) -> None:
        self.steps = iter(steps)
        self.closed = False

    def __aiter__(self) -> DownloadIterator:
        return self

    async def __anext__(self) -> bytes:
        value = next(self.steps, None)
        if value is None:
            raise StopAsyncIteration
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [FileReferenceExpiredError, FileReferenceInvalidError, FilerefUpgradeNeededError],
)
async def test_stream_refresh_resumes_after_partial_buffer_without_duplication(
    error_type: Any,
) -> None:
    stale, refreshed = native_message(), native_message()
    first = DownloadIterator([b"a" * (MIB // 2), error_type(request=None)])
    second = DownloadIterator([b"b" * (MIB // 2), b"unused"])
    native = SimpleNamespace(
        iter_download=Mock(side_effect=[first, second]),
        get_messages=AsyncMock(return_value=refreshed),
    )
    client = transport_client(native)
    chunks = [part async for part in client.stream_media(stale, limit=1, offset=3)]
    assert chunks == [b"a" * (MIB // 2) + b"b" * (MIB // 2)]
    calls = native.iter_download.call_args_list
    assert [call.kwargs["offset"] for call in calls] == [3 * MIB, 3 * MIB + MIB // 2]
    assert all(call.kwargs["request_size"] == 512 * 1024 for call in calls)
    assert calls[1].args[0] is refreshed
    native.get_messages.assert_awaited_once_with(CHANNEL, ids=77)
    assert first.closed and second.closed


@pytest.mark.asyncio
async def test_removed_message_is_a_visible_stream_failure() -> None:
    stream = DownloadIterator([FileReferenceExpiredError(request=None)])
    native = SimpleNamespace(
        iter_download=Mock(return_value=stream),
        get_messages=AsyncMock(return_value=None),
    )
    client = transport_client(native)
    with pytest.raises(FileNotFoundError, match="removed"):
        _ = [
            part
            async for part in client.stream_media(native_message(), limit=1, offset=0)
        ]
    assert stream.closed


@pytest.mark.asyncio
async def test_reference_refresh_retry_is_bounded_and_closes_each_iterator() -> None:
    streams = [
        DownloadIterator([FileReferenceExpiredError(request=None)]) for _ in range(3)
    ]
    native = SimpleNamespace(
        iter_download=Mock(side_effect=streams),
        get_messages=AsyncMock(return_value=native_message()),
    )
    client = transport_client(native)
    with pytest.raises(FileReferenceExpiredError):
        _ = [
            part
            async for part in client.stream_media(native_message(), limit=1, offset=0)
        ]
    assert native.get_messages.await_count == 2
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
async def test_large_download_offset_is_not_truncated_to_32_bits() -> None:
    stream = DownloadIterator([b"tail"])
    native = SimpleNamespace(iter_download=Mock(return_value=stream))
    chunks = [
        part
        async for part in transport_client(native).stream_media(
            native_message(), limit=1, offset=4096
        )
    ]
    assert chunks == [b"tail"]
    assert native.iter_download.call_args.kwargs["offset"] == 4 * 1024**3
    assert stream.closed
