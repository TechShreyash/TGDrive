"""HTTP ranges over one logical file backed by multiple Telegram messages."""

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from tgdrive.media.logical_media import PartLease, part_ranges
from tgdrive.media.ranges import MalformedRange, RangeNotSatisfiable, parse_byte_range
from tgdrive.media.streaming import (
    PDF_PREVIEW_BYTES,
    TelegramVariantNotFound,
    _ActivityBody,
    _content_disposition,
    _is_playback_request,
    _not_modified,
    _observe_media_request,
    _prime_response,
    _request_range,
    media_etag,
)
from tgdrive.storage.file_parts import validate_parts


async def stream_multipart_file(
    *,
    pool: Any,
    channel_id: int,
    message_id: int,
    file_name: str,
    expected_size: int,
    parts: list[dict[str, Any]],
    request: Request,
    download: bool = False,
    preview: bool = False,
    quality: str | None = None,
    authorize=None,
    request_id: str | None = None,
    request_status=None,
) -> Response:
    if authorize is not None:
        await authorize()
    manifest = validate_parts(parts, size=expected_size, message_id=message_id)
    if quality not in (None, "", "original") and not download:
        raise TelegramVariantNotFound("Separate file parts have no alternate rendition")
    etag = media_etag(channel_id, message_id, expected_size, parts=manifest)
    try:
        selected = parse_byte_range(_request_range(request, etag), expected_size)
    except MalformedRange as error:
        return Response(
            status_code=400, content=str(error), headers={"Accept-Ranges": "bytes"}
        )
    except RangeNotSatisfiable as error:
        return Response(
            status_code=416,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": error.content_range,
            },
        )
    mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    if (
        preview
        and not download
        and mime_type == "application/pdf"
        and expected_size > PDF_PREVIEW_BYTES
    ):
        return Response(status_code=413, content="PDF preview limit is 256 MiB")
    spans = part_ranges(manifest, selected)
    first = await PartLease(
        pool, channel_id, spans[0][0], authorize=authorize, request_id=request_id
    ).open()
    if _not_modified(request, etag):
        try:
            if authorize is not None:
                await authorize()
        finally:
            await first.close()
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, no-store, no-transform"},
        )
    activity = _observe_media_request(
        pool,
        channel_id=channel_id,
        message_id=message_id,
        size=expected_size,
        name=file_name,
        parts=manifest,
        request=request,
        start=selected.start,
        end=selected.end + 1,
        allow_preload=_is_playback_request(
            request, mime_type, download=download, preview=preview
        ),
        authorize=authorize,
        request_id=request_id,
    )
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(selected.length),
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
    # Retain the original streamer's safe document policy for passive media.
    from tgdrive.media.streaming import _SAFE_INLINE_IMAGE_TYPES

    if (
        mime_type.startswith(("audio/", "video/"))
        or mime_type in _SAFE_INLINE_IMAGE_TYPES
    ) and not download:
        headers["Content-Security-Policy"] = (
            "default-src 'none'; media-src 'self' blob:; img-src 'self' data:; "
            "style-src 'unsafe-inline'; frame-ancestors 'self'"
        )
    elif preview and mime_type == "application/pdf" and not download:
        headers["X-Frame-Options"] = "SAMEORIGIN"
        headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; frame-ancestors 'self'"
        )
    if selected.content_range is not None:
        headers["Content-Range"] = selected.content_range
    status = 206 if selected.is_partial else 200
    if request.method == "HEAD":
        try:
            if authorize is not None:
                await authorize()
        finally:
            await first.close()
        return Response(status_code=status, media_type=mime_type, headers=headers)

    async def body() -> AsyncIterator[bytes]:
        emitted = 0
        try:
            for index, (part, interval) in enumerate(spans):
                lease = (
                    first
                    if index == 0
                    else await PartLease(
                        pool,
                        channel_id,
                        part,
                        authorize=authorize,
                        request_id=request_id,
                    ).open()
                )
                iterator = lease.read(interval)
                try:
                    async for chunk in iterator:
                        emitted += len(chunk)
                        yield chunk
                        if activity is not None:
                            tracked_body.delivered(
                                channel_id=channel_id,
                                message_id=message_id,
                                offset=selected.start + emitted - len(chunk),
                                length=len(chunk),
                            )
                finally:
                    try:
                        await iterator.aclose()
                    finally:
                        await lease.close()
            if emitted != selected.length:
                raise OSError(
                    "Logical Telegram stream ended before its declared length"
                )
        finally:
            await first.close()

    tracked_body = _ActivityBody(body(), activity)
    return await _prime_response(
        tracked_body,
        release=first.close,
        status=status,
        mime_type=mime_type,
        headers=headers,
        authorize=authorize,
        request_status=request_status,
    )
