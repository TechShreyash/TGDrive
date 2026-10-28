"""Bounded, authorized reads of uploaded poster documents; never read the video."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress

from telethon.errors import RPCError

from tgdrive.media import books
from tgdrive.media.generated_thumbnails import read, save
from tgdrive.media.streaming import TelegramFileNotFound
from tgdrive.media.thumbnails import Thumbnail, ThumbnailBusy
from tgdrive.storage.file_parts import MAX_POSTER_BYTES
from tgdrive.telegram.client_pool import ClientPoolError


async def load_poster(*, pool, channel_id, poster, directory, authorize):
    def valid(content):
        return (
            isinstance(content, bytes)
            and 0 < len(content) == poster["size"] <= MAX_POSTER_BYTES
            and content.startswith(b"\xff\xd8\xff")
            and content.endswith(b"\xff\xd9")
            and hashlib.sha256(content).hexdigest() == poster["sha256"]
        )

    await authorize()
    content = await asyncio.to_thread(
        read, directory, channel_id, poster["message_id"], maximum=MAX_POSTER_BYTES
    )
    await authorize()
    if valid(content):
        return Thumbnail(content, "image/jpeg")
    # A virtual single part retains checksum and immutable document-id checks.
    source = {
        "message_id": poster["message_id"],
        "size": poster["size"],
        "parts": [{"index": 0, "offset": 0, **poster}],
    }
    try:
        content = await books.read_range(
            pool,
            channel_id,
            source,
            0,
            poster["size"],
            authorize,
            background=True,
        )
    except TelegramFileNotFound:
        return None
    except (OSError, RPCError, ClientPoolError, ValueError) as exc:
        raise ThumbnailBusy from exc
    await authorize()
    if not valid(content):
        return None
    with suppress(OSError):
        await asyncio.to_thread(
            save,
            directory,
            channel_id,
            poster["message_id"],
            content,
            maximum=MAX_POSTER_BYTES,
        )
    return Thumbnail(content, "image/jpeg")
