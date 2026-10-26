"""Bounded, owner/share-authorized preview helpers; never execute file content."""

from __future__ import annotations

import codecs
import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePath
from typing import Any
from urllib.parse import urlencode

from telethon.errors import RPCError

from tgdrive.media.logical_media import read_logical_prefix
from tgdrive.media.media_metadata import get_message
from tgdrive.media.media_reader import MediaReadCache, stream_pooled_media
from tgdrive.media.media_scheduler import media_priority
from tgdrive.media.streaming import (
    PDF_PREVIEW_BYTES,
    TelegramFileNotFound,
    _media_from_message,
)
from tgdrive.storage.file_parts import validate_parts

TEXT_PREVIEW_BYTES = 256 * 1024
_TEXT_LANGUAGES = {
    "txt": "text",
    "md": "markdown",
    "markdown": "markdown",
    "rst": "text",
    "log": "text",
    "csv": "csv",
    "tsv": "tsv",
    "json": "json",
    "jsonl": "json",
    "ipynb": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "ini": "ini",
    "cfg": "ini",
    "conf": "text",
    "xml": "xml",
    "svg": "xml",
    "html": "html",
    "htm": "html",
    "css": "css",
    "scss": "css",
    "less": "css",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "typescript",
    "mts": "typescript",
    "cts": "typescript",
    "tsx": "typescript",
    "py": "python",
    "pyi": "python",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "ps1": "powershell",
    "bat": "text",
    "sql": "sql",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "hpp": "cpp",
    "cc": "cpp",
    "cs": "csharp",
    "java": "java",
    "kt": "kotlin",
    "kts": "kotlin",
    "rs": "rust",
    "go": "go",
    "rb": "ruby",
    "php": "php",
    "swift": "swift",
    "dart": "dart",
    "lua": "lua",
    "r": "r",
    "srt": "text",
    "vtt": "text",
    "tex": "latex",
    "bib": "text",
    "env": "text",
    "gitignore": "text",
    "dockerfile": "text",
    "makefile": "text",
    "license": "text",
    "readme": "text",
}


_TS_PROBE_BYTES = 8192
_TS_MIME_TYPES = {
    "video/mp2t",
    "video/mpegts",
    "video/x-mpegts",
    "application/mp2t",
}


def _decode_preview_text(data: bytes, *, truncated: bool) -> str | None:
    """Do not turn binary/invalid encodings into plausible-looking source text."""
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = codecs.getincrementaldecoder(encoding)().decode(
            data, final=not truncated
        )
    except UnicodeError:
        return None
    # Logs can contain backspace/ANSI escape; both stay inert in the text viewer.
    if any(ord(char) < 32 and char not in "\t\n\r\f\b\x1b" for char in text):
        return None
    return text


def _is_mpeg_transport_stream(data: bytes, mime_type: str | None) -> bool:
    """Require repeated packet structure; neither a .ts suffix nor MIME is proof."""
    sample = data[:_TS_PROBE_BYTES]
    if _decode_preview_text(sample, truncated=True) is not None:
        return False
    normalized_mime = (
        mime_type.partition(";")[0].strip().lower()
        if isinstance(mime_type, str)
        else ""
    )
    required = 3 if normalized_mime in _TS_MIME_TYPES else 5
    # Account for ordinary TS, timestamp-prefixed M2TS, and FEC-appended packets.
    for stride in (188, 192, 204):
        last_start = min(1024, len(sample) - (required - 1) * stride - 188)
        for start in range(last_start + 1):
            for packet in range(required):
                offset = start + packet * stride
                if sample[offset] != 0x47:
                    break
                flags = sample[offset + 3]
                adaptation = (flags >> 4) & 3
                if not adaptation or (flags >> 6) == 1:
                    break
                if adaptation & 2 and sample[offset + 4] > (
                    182 if adaptation == 3 else 183
                ):
                    break
            else:
                return True
    return False


@asynccontextmanager
async def lease_media(
    pool: Any, channel_id: int, message_id: int, *, background: bool = False
) -> AsyncIterator[tuple[Any, Any, Any]]:
    """Release even when metadata fetch fails before response construction."""

    # Metadata must not hold the payload slot that its pooled reader needs.
    priority = {"priority": "preload", "respect_cooldown": True} if background else {}
    lease = pool.lease(prefer_premium=True, **priority)
    client = await lease.__aenter__()

    async def metadata():
        token = media_priority.set("preload") if background else None
        try:
            return await get_message(client, channel_id, message_id)
        finally:
            if token is not None:
                media_priority.reset(token)

    try:
        try:
            message = await metadata()
            media = _media_from_message(message)
            if message is None or getattr(message, "empty", False) or media is None:
                raise TelegramFileNotFound(
                    "Stored Telegram message has no media"
                ) from None
        except OSError, RPCError, ValueError:
            if pool.kind_of(client) != "user":
                raise
            await lease.__aexit__(None, None, None)
            lease = None
            replacement = pool.lease(**priority)
            client = await replacement.__aenter__()
            lease = replacement
            message = await metadata()
            media = _media_from_message(message)
            if message is None or getattr(message, "empty", False) or media is None:
                raise TelegramFileNotFound(
                    "Stored Telegram message has no media"
                ) from None
        yield client, message, media
    finally:
        if lease is not None:
            await lease.__aexit__(None, None, None)


async def describe_preview(
    *,
    pool: Any,
    channel_id: int,
    message_id: int,
    file_name: str,
    size: int,
    path: str,
    auth: str | None,
    parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    extension = PurePath(file_name).suffix.lower().lstrip(".")
    language = _TEXT_LANGUAGES.get(extension or file_name.lower().lstrip("."))
    base: dict[str, Any] = {"name": file_name, "size": size}
    if extension in {"epub", "cbz", "cbr"}:
        return base | {"kind": "book", "format": extension}
    if extension == "pdf":
        if size > PDF_PREVIEW_BYTES:
            return base | {
                "kind": "unsupported",
                "reason": (
                    "PDF previews support files up to 256 MiB "
                    "to protect browser memory. "
                    "Download the original to open this file."
                ),
            }
        query = {"path": path, "preview": "1"}
        if auth:
            query["auth"] = auth
        return base | {"kind": "pdf", "url": "/file?" + urlencode(query)}
    if language is None:
        return base | {
            "kind": "unsupported",
            "reason": (
                "This format does not have a browser preview. "
                "Download the original to open it."
            ),
        }
    if parts is not None:
        manifest = validate_parts(parts, size=size, message_id=message_id)
        data = await read_logical_prefix(
            pool=pool,
            channel_id=channel_id,
            parts=manifest,
            size=size,
            limit=TEXT_PREVIEW_BYTES,
        )
        mime_type = mimetypes.guess_type(file_name)[0]
    else:
        async with lease_media(pool, channel_id, message_id) as (
            client,
            message,
            media,
        ):
            actual_size = getattr(media, "file_size", size)
            if isinstance(actual_size, int):
                base["size"] = actual_size
            if callable(getattr(pool, "download_capacity", None)):
                if pool._media_read_cache is None:
                    pool._media_read_cache = MediaReadCache(pool.media_cache_dir)
                pool._media_read_cache.register_file(
                    channel_id=channel_id,
                    message_id=message_id,
                    size=base["size"],
                    name=file_name,
                )
            # A preview shares the bounded prefix cache, without qualifying as
            # playback or asking the preparation manager to fetch anything else.
            iterator = stream_pooled_media(
                pool, client, message, channel=channel_id, limit=1, offset=0
            )
            try:
                data = await anext(iterator, b"")
                mime_type = getattr(media, "mime_type", None)
                if mime_type is None:
                    native_document = getattr(
                        getattr(message, "native", message), "document", None
                    )
                    mime_type = getattr(native_document, "mime_type", None)
            finally:
                await iterator.aclose()
    if extension in {"ts", "mts"} and _is_mpeg_transport_stream(data, mime_type):
        return base | {"kind": "video"}
    clipped = data[:TEXT_PREVIEW_BYTES]
    truncated = base["size"] > len(clipped)
    text = _decode_preview_text(clipped, truncated=truncated)
    if text is None:
        return base | {
            "kind": "unsupported",
            "reason": (
                "This file is not readable text in a supported encoding. "
                "Download the original to open it."
            ),
        }
    return base | {
        "kind": "text",
        "text": text,
        "language": language,
        "truncated": truncated,
        "preview_bytes": len(clipped),
    }


async def describe_media(
    *,
    pool: Any,
    channel_id: int,
    message_id: int,
    file_name: str,
    size: int,
    parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if parts is not None:
        validate_parts(parts, size=size, message_id=message_id)
        return {
            "original": {
                "id": "original",
                "label": "Original",
                "is_original": True,
                "size": size,
                "mime_type": mimetypes.guess_type(file_name)[0]
                or "application/octet-stream",
                "width": None,
                "height": None,
                "duration": None,
            },
            "variants": [],
        }
    async with lease_media(pool, channel_id, message_id) as (client, message, media):
        describe = getattr(client, "describe_media", None)
        if callable(describe):
            return describe(message)
        return {
            "original": {
                "id": "original",
                "label": "Original",
                "is_original": True,
                "size": getattr(media, "file_size", size),
                "mime_type": mimetypes.guess_type(file_name)[0]
                or "application/octet-stream",
                "width": None,
                "height": None,
                "duration": None,
            },
            "variants": [],
        }
