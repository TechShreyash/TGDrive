"""Safe, range-aware streaming from Telegram's supported client API."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import mimetypes
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send
from telethon.errors import FloodWaitError, RPCError

from tgdrive.core.security import safe_name
from tgdrive.media.media_metadata import get_message
from tgdrive.media.ranges import (
    MalformedRange,
    RangeNotSatisfiable,
    parse_byte_range,
    plan_telegram_chunks,
)
from tgdrive.telegram.client_pool import ClientPool, MediaQueueFull

logger = logging.getLogger(__name__)

PDF_PREVIEW_BYTES = 256 * 1024**2

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
    public_detail = (
        "The stored file is missing or changed in Telegram. Restore it from a "
        "configured channel backup, or upload the original file again. "
        "Do not edit or delete messages in TGDrive's storage channel."
    )


def _record_request_error(status, error):
    if status is None:
        return
    status.update(phase="error", code=None, retry_at=None)
    if isinstance(error, asyncio.CancelledError):
        status["phase"] = None
    elif isinstance(error, MediaQueueFull):
        status.update(
            phase="retry_wait",
            code=error.code,
            retry_at=time.monotonic() + error.retry_after,
        )


class _LeasedStreamingResponse(StreamingResponse):
    def __init__(
        self,
        *args: Any,
        release: Callable[[], Awaitable[None]],
        authorize=None,
        request_status=None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._release = release
        self._authorize = authorize
        self._request_status = request_status

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            if self._authorize is not None:
                await self._authorize()
            await super().__call__(scope, receive, send)
        except BaseException as error:
            _record_request_error(self._request_status, error)
            raise
        finally:
            # ASGI send can fail while the body is suspended at a yield.
            # Explicitly close it so bounded prefetch work is not left to GC.
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()
            finally:
                try:
                    await self._release()
                finally:
                    if (
                        self._request_status is not None
                        and self._request_status["phase"] == "receiving"
                    ):
                        self._request_status["phase"] = None


class _PrimedBody:
    """Own a started iterator even if ASGI fails before its first body send."""

    def __init__(self, iterator, first, authorize=None):
        self.iterator, self.first, self.authorize = iterator, first, authorize

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            if self.authorize is not None:
                await self.authorize()
            if self.first is not None:
                first, self.first = self.first, None
                return first
            return await anext(self.iterator)
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        self.first = None
        await self.iterator.aclose()


class _ActivityBody:
    """Track one HTTP consumer across a logical multipart response."""

    def __init__(self, iterator, activity):
        self.iterator, self.activity = iterator, activity
        self.observation = None
        self.started = self.closed = self.completed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.started:
            self.started = True
            if self.activity is not None:
                manager, identity, viewer = self.activity
                self.observation = manager.begin(identity, viewer)
        try:
            return await anext(self.iterator)
        except StopAsyncIteration:
            self.completed = True
            await self.aclose()
            raise
        except BaseException:
            await self.aclose()
            raise

    def delivered(self, **values):
        if self.activity is not None and self.observation is not None:
            manager, _identity, viewer = self.activity
            manager.delivered(**values, request_id=viewer, observation=self.observation)

    async def aclose(self):
        try:
            await self.iterator.aclose()
        finally:
            if self.started and not self.closed:
                self.closed = True
                if self.activity is not None and self.observation is not None:
                    manager, identity, viewer = self.activity
                    manager.finished(
                        identity,
                        viewer,
                        completed=self.completed,
                        observation=self.observation,
                    )


def _media_etag(identity):
    return '"tg-' + hashlib.sha256(repr(identity).encode()).hexdigest() + '"'


def media_etag(
    channel_id, message_id, total, parts=None, quality=None, document_identity=None
):
    quality = quality if quality not in (None, "") else "original"
    if parts is not None:
        manifest = tuple(
            (
                p["index"],
                p["offset"],
                p["size"],
                p["message_id"],
                p["sha256"],
                p.get("document_id"),
            )
            for p in parts
        )
        return _media_etag((channel_id, message_id, total, quality, manifest))
    tag = _media_etag((channel_id, message_id, total, quality, document_identity))
    # A catalog-only HEAD cannot prove the immutable Telegram document identity.
    return tag if document_identity is not None else "W/" + tag


def _request_range(request, etag):
    value = request.headers.get("if-range")
    # A weak tag or an unverifiable date cannot prove byte identity.
    if value is not None and (value.strip().startswith("W/") or value.strip() != etag):
        return None
    return request.headers.get("range")


def _not_modified(request, etag):
    value = request.headers.get("if-none-match")
    return value is not None and any(
        tag.strip() == "*" or tag.strip().removeprefix("W/") == etag.removeprefix("W/")
        for tag in value.split(",")
    )


def _is_playback_request(request, mime_type, *, download=False, preview=False):
    """Only explicit player requests may warm beyond their HTTP byte range."""
    return (
        not download
        and mime_type.startswith(("audio/", "video/"))
        and (preview or request.headers.get("sec-fetch-dest") in {"audio", "video"})
    )


def _media_byte_rate(message, size):
    """Use media duration for average bitrate instead of network burst speed."""
    native = getattr(message, "native", message)
    document = getattr(native, "document", None)
    for attribute in getattr(document, "attributes", ()):
        duration = getattr(attribute, "duration", None)
        if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
            rate = size / duration
            return rate if math.isfinite(rate) and rate > 0 else None
    return None


def _observe_media_request(
    pool,
    *,
    channel_id,
    message_id,
    size,
    name,
    request,
    start,
    end,
    parts=None,
    authorize=None,
    request_id=None,
    allow_preload=True,
    media_rate=None,
):
    manager = getattr(pool, "_preload_manager", None)
    if manager is not None:
        if not allow_preload:
            # Downloads and unclassified clients retain their requested blocks.
            # Only identified playback may schedule work outside its HTTP range.
            manager.register_file(
                channel_id=channel_id,
                message_id=message_id,
                size=size,
                name=name,
                parts=parts,
            )
            return None
        viewer = request_id or secrets.token_urlsafe(12)
        identity = manager.observe(
            channel_id=channel_id,
            message_id=message_id,
            size=size,
            name=name,
            parts=parts,
            start=start,
            end=end,
            method=request.method,
            authorize=authorize,
            request_id=viewer,
            media_rate=media_rate,
            allow_full=request_id is not None,
        )
        return (manager, identity, viewer) if request.method == "GET" else None
    elif callable(getattr(pool, "download_capacity", None)):
        from tgdrive.media.media_reader import MediaReadCache

        if pool._media_read_cache is None:
            pool._media_read_cache = MediaReadCache(pool.media_cache_dir)
        pool._media_read_cache.register_file(
            channel_id=channel_id,
            message_id=message_id,
            size=size,
            name=name,
            parts=parts,
        )


async def _prime_response(
    iterator,
    *,
    release,
    status,
    mime_type,
    headers,
    authorize=None,
    request_status=None,
):
    try:
        try:
            first = await anext(iterator)
        except StopAsyncIteration:
            first = None
    except BaseException as error:
        _record_request_error(request_status, error)
        try:
            await iterator.aclose()
        finally:
            await release()
        if isinstance(error, MediaQueueFull):
            return JSONResponse(
                {
                    "detail": str(error),
                    "code": error.code,
                    "retry_after": error.retry_after,
                },
                status_code=503,
                headers={
                    "Retry-After": str(error.retry_after),
                    "Cache-Control": "no-store",
                },
            )
        raise
    if request_status is not None:
        request_status["phase"] = "receiving"
    return _LeasedStreamingResponse(
        _PrimedBody(iterator, first, authorize),
        release=release,
        authorize=authorize,
        request_status=request_status,
        status_code=status,
        media_type=mime_type,
        headers=headers,
    )


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
    authorize=None,
    request_id: str | None = None,
    request_status=None,
) -> Response:
    """Stream with an optional Premium user and a balanced client lease."""
    if authorize is not None:
        await authorize()
    if parts is not None:
        from tgdrive.media.multipart_streaming import stream_multipart_file

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
            authorize=authorize,
            request_id=request_id,
            request_status=request_status,
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
        interrupted = False
        while not release_task.done():
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                # Keep lease cleanup owned until it drains, even if disconnect
                # and shutdown cancel this response more than once.
                interrupted = True
        release_task.result()
        if interrupted:
            raise asyncio.CancelledError

    try:

        async def fetch_media() -> tuple[Any, Any]:
            message = await get_message(client, channel_id, message_id)
            media = _media_from_message(message)
            if message is None or getattr(message, "empty", False) or media is None:
                raise TelegramFileNotFound(
                    f"Telegram message {message_id} has no media"
                )
            actual_size = getattr(media, "file_size", None)
            if type(actual_size) is int and actual_size != expected_size:
                raise TelegramFileNotFound(
                    "Stored Telegram media size differs from the catalog"
                )
            return message, media

        for attempt in range(3):
            try:
                message, media = await fetch_media()
                break
            except (OSError, RPCError, ValueError) as error:
                problem = await pool.report_failure(client, error)
                if isinstance(error, FloodWaitError):
                    await pool.defer_client(
                        client.name, time.time() + max(1, error.seconds)
                    )
                elif problem is None and pool.kind_of(client) != "user":
                    raise
                if attempt == 2:
                    if problem is not None:
                        raise problem from None
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

        from tgdrive.telegram.clients import _block_identity

        immutable = _block_identity(message, 0)
        selected_quality = quality if not download else "original"
        etag = media_etag(
            channel_id,
            message_id,
            total,
            quality=selected_quality,
            document_identity=immutable[0][2:-1] if immutable else None,
        )
        if _not_modified(request, etag):
            if authorize is not None:
                await authorize()
            await release()
            return Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Cache-Control": "private, no-store, no-transform",
                },
            )

        try:
            byte_range = parse_byte_range(_request_range(request, etag), total)
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
            and total > PDF_PREVIEW_BYTES
        ):
            await release()
            return Response(status_code=413, content="PDF preview limit is 256 MiB")

        activity = _observe_media_request(
            pool,
            channel_id=channel_id,
            message_id=message_id,
            size=total,
            name=file_name,
            request=request,
            start=byte_range.start,
            end=byte_range.end + 1,
            allow_preload=_is_playback_request(
                request, mime_type, download=download, preview=preview
            )
            and quality in (None, "", "original"),
            media_rate=_media_byte_rate(message, total),
            authorize=authorize,
            request_id=request_id,
        )
        # Pooled reads own their origin leases. A long warm response must not
        # retain the account used for its short, fresh metadata validation.
        if immutable is not None:
            await release()

        async def body() -> AsyncIterator[bytes]:
            emitted = 0
            from tgdrive.media.media_reader import stream_pooled_media

            stream = stream_pooled_media(
                pool,
                client,
                message,
                channel=channel_id,
                limit=plan.part_count,
                offset=plan.offset // plan.chunk_size,
                authorize=authorize,
                request_id=request_id,
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
                        if activity is not None:
                            tracked_body.delivered(
                                channel_id=channel_id,
                                message_id=message_id,
                                offset=byte_range.start + emitted - len(selected),
                                length=len(selected),
                            )
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
            "Content-Encoding": "identity",
            "Content-Disposition": _content_disposition(
                file_name, mime_type, download=download, preview=preview
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, no-store, no-transform",
            "ETag": etag,
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
            if authorize is not None:
                await authorize()
            await release()
            return Response(
                status_code=206 if byte_range.is_partial else 200,
                media_type=mime_type,
                headers=headers,
            )

        tracked_body = _ActivityBody(body(), activity)
        return await _prime_response(
            tracked_body,
            release=release,
            status=206 if byte_range.is_partial else 200,
            mime_type=mime_type,
            headers=headers,
            authorize=authorize,
            request_status=request_status,
        )
    except BaseException:
        await release()
        raise
