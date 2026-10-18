"""Safe, range-aware streaming from Telegram's supported client API."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send
from telethon.errors import FloodWaitError, RPCError

from tgdrive.client_pool import ClientPool
from tgdrive.ranges import (
    MalformedRange,
    RangeNotSatisfiable,
    parse_byte_range,
    plan_telegram_chunks,
)
from tgdrive.security import safe_name

logger = logging.getLogger(__name__)

_MEDIA_ATTRIBUTES = (
    "audio",
    "document",
    "photo",
    "sticker",
    "animation",
    "video",
    "voice",
    "video_note",
)
_SAFE_INLINE_IMAGE_TYPES = {
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


class TelegramVariantNotFound(ValueError):
    """The requested rendition does not belong to this message."""


class TelegramFileNotFound(FileNotFoundError):
    pass


class _LeasedStreamingResponse(StreamingResponse):
    def __init__(
        self, *args: Any, release: Callable[[], Awaitable[None]], **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # ASGI send can fail while the body is suspended at a yield.
            # Explicitly close it so bounded prefetch work is not left to GC.
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()
            finally:
                await self._release()


def _media_from_message(message: Any) -> Any | None:
    for attribute in _MEDIA_ATTRIBUTES:
        media = getattr(message, attribute, None)
        if media is not None:
            return media
    return None


def _content_disposition(
    file_name: str, mime_type: str, *, download: bool = False, preview: bool = False
) -> str:
    normalized = safe_name(file_name)
    disposition = (
        "inline"
        if not download
        and (
            mime_type.startswith(("audio/", "video/"))
            or mime_type in _SAFE_INLINE_IMAGE_TYPES
            or (preview and mime_type == "application/pdf")
        )
        else "attachment"
    )
    encoded = quote(normalized, safe="")
    ascii_fallback = normalized.encode("ascii", "replace").decode().replace('"', "'")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


async def stream_telegram_file(
    *,
    pool: ClientPool,
    channel_id: int,
    message_id: int,
    file_name: str,
    expected_size: int,
    request: Request,
    download: bool = False,
    preview: bool = False,
    quality: str | None = None,
    parts: list[dict[str, Any]] | None = None,
) -> Response:
    """Stream with an optional Premium user and a balanced client lease."""
    if parts is not None:
        from tgdrive.multipart_streaming import stream_multipart_file

        return await stream_multipart_file(
            pool=pool,
            channel_id=channel_id,
            message_id=message_id,
            file_name=file_name,
            expected_size=expected_size,
            parts=parts,
            request=request,
            download=download,
            preview=preview,
            quality=quality,
        )

    lease: AbstractAsyncContextManager[Any] = pool.lease(
        prefer_premium=True, respect_cooldown=True
    )
    client = await lease.__aenter__()
    release_task: asyncio.Task[Any] | None = None

    async def release() -> None:
        nonlocal release_task
        if release_task is None:
            release_task = asyncio.create_task(lease.__aexit__(None, None, None))
        await asyncio.shield(release_task)

    try:

        async def fetch_media() -> tuple[Any, Any]:
            message = await client.get_messages(channel_id, message_ids=message_id)
            media = _media_from_message(message)
            if message is None or getattr(message, "empty", False) or media is None:
                raise TelegramFileNotFound(
                    f"Telegram message {message_id} has no media"
                )
            return message, media

        for attempt in range(3):
            try:
                message, media = await fetch_media()
                break
            except (OSError, RPCError, ValueError) as error:
                if isinstance(error, FloodWaitError):
                    await pool.defer_client(
                        client.name, time.time() + max(1, error.seconds)
                    )
                elif pool.kind_of(client) != "user":
                    raise
                if attempt == 2:
                    raise
                # Switch before headers, preserving optional-user fallback.
                await release()
                lease = pool.lease(respect_cooldown=True)
                client = await lease.__aenter__()
                release_task = None

        if quality not in (None, "", "original") and not download:
            select = getattr(client, "select_media_variant", None)
            if not callable(select):
                raise TelegramVariantNotFound("This media quality is unavailable")
            try:
                message = select(message, quality)
            except (ValueError, KeyError) as exc:
                raise TelegramVariantNotFound(
                    "This media quality is unavailable"
                ) from exc
            media = _media_from_message(message)
            if media is None:
                raise TelegramVariantNotFound("This media quality is unavailable")
        telegram_size = getattr(media, "file_size", None)
        total = telegram_size if isinstance(telegram_size, int) else expected_size
        if total < 0:
            raise TelegramFileNotFound("Telegram media has an invalid size")

        try:
            byte_range = parse_byte_range(request.headers.get("range"), total)
        except MalformedRange as exc:
            await release()
            return Response(
                status_code=400,
                content=str(exc),
                headers={"Accept-Ranges": "bytes"},
            )
        except RangeNotSatisfiable as exc:
            await release()
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": exc.content_range,
                },
            )

        plan = plan_telegram_chunks(byte_range)
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        selected_mime = getattr(media, "mime_type", None)
        if (
            quality not in (None, "", "original")
            and not download
            and isinstance(selected_mime, str)
            and re.fullmatch(r"(video|audio|image)/[a-zA-Z0-9.+-]+", selected_mime)
        ):
            mime_type = selected_mime
        if (
            preview
            and not download
            and mime_type == "application/pdf"
            and total > 20 * 1024**2
        ):
            await release()
            return Response(status_code=413, content="PDF preview limit is 20 MiB")

        async def body() -> AsyncIterator[bytes]:
            emitted = 0
            from tgdrive.media_reader import stream_pooled_media

            stream = stream_pooled_media(
                pool,
                client,
                message,
                channel=channel_id,
                limit=plan.part_count,
                offset=plan.offset // plan.chunk_size,
            )
            try:
                chunk_index = 0
                async for chunk in stream:
                    if chunk_index >= plan.part_count:
                        break
                    cut_start = plan.first_part_cut if chunk_index == 0 else 0
                    cut_end = (
                        plan.last_part_cut
                        if chunk_index == plan.part_count - 1
                        else len(chunk)
                    )
                    selected = chunk[cut_start:cut_end]
                    emitted += len(selected)
                    chunk_index += 1
                    if selected:
                        yield selected
                if emitted != plan.requested_length:
                    raise OSError(
                        "Telegram stream ended early "
                        f"({emitted}/{plan.requested_length} bytes)"
                    )
            except Exception:
                logger.exception("Telegram response stream failed")
                raise
            finally:
                try:
                    await stream.aclose()
                finally:
                    await release()

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(byte_range.length),
            "Content-Disposition": _content_disposition(
                file_name, mime_type, download=download, preview=preview
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, no-store",
        }
        if (
            mime_type.startswith(("audio/", "video/"))
            or mime_type in _SAFE_INLINE_IMAGE_TYPES
        ) and not download:
            # Passive media may also be opened directly in the browser's native
            # media document. A default-src none sandbox prevents that document
            # from loading its own source in Chromium.
            headers["Content-Security-Policy"] = (
                "default-src 'none'; media-src 'self' blob:; img-src 'self' data:; "
                "style-src 'unsafe-inline'; frame-ancestors 'self'"
            )
        elif preview and mime_type == "application/pdf" and not download:
            headers["X-Frame-Options"] = "SAMEORIGIN"
            headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; frame-ancestors 'self'"
            )
        if byte_range.content_range is not None:
            headers["Content-Range"] = byte_range.content_range

        if request.method == "HEAD":
            await release()
            return Response(
                status_code=206 if byte_range.is_partial else 200,
                media_type=mime_type,
                headers=headers,
            )

        return _LeasedStreamingResponse(
            body(),
            release=release,
            status_code=206 if byte_range.is_partial else 200,
            media_type=mime_type,
            headers=headers,
        )
    except BaseException:
        await release()
        raise
