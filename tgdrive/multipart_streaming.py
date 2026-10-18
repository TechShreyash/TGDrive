"""HTTP ranges over one logical file backed by multiple Telegram messages."""

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from tgdrive.file_parts import validate_parts
from tgdrive.logical_media import PartLease, part_ranges
from tgdrive.ranges import MalformedRange, RangeNotSatisfiable, parse_byte_range
from tgdrive.streaming import (
    TelegramVariantNotFound,
    _content_disposition,
    _LeasedStreamingResponse,
)


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
) -> Response:
    manifest = validate_parts(parts, size=expected_size, message_id=message_id)
    if quality not in (None, "", "original") and not download:
        raise TelegramVariantNotFound("Separate file parts have no alternate rendition")
    try:
        selected = parse_byte_range(request.headers.get("range"), expected_size)
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
        and expected_size > 20 * 1024**2
    ):
        return Response(status_code=413, content="PDF preview limit is 20 MiB")
    spans = part_ranges(manifest, selected)
    first = await PartLease(pool, channel_id, spans[0][0]).open()
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(selected.length),
        "Content-Disposition": _content_disposition(
            file_name, mime_type, download=download, preview=preview
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cache-Control": "private, no-store",
    }
    # Retain the original streamer's safe document policy for passive media.
    from tgdrive.streaming import _SAFE_INLINE_IMAGE_TYPES

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
        await first.close()
        return Response(status_code=status, media_type=mime_type, headers=headers)

    async def body() -> AsyncIterator[bytes]:
        emitted = 0
        try:
            for index, (part, interval) in enumerate(spans):
                lease = (
                    first
                    if index == 0
                    else await PartLease(pool, channel_id, part).open()
                )
                iterator = lease.read(interval)
                try:
                    async for chunk in iterator:
                        emitted += len(chunk)
                        yield chunk
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

    return _LeasedStreamingResponse(
        body(),
        release=first.close,
        status_code=status,
        media_type=mime_type,
        headers=headers,
    )
