"""Small provider thumbnails with bounded work, never an original-file preview.

Only payloads (including short-lived misses) are cached. Callers must authorize
both before entering this cache and after awaiting it; shares/sessions never
become part of a cache entry.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from telethon.errors import RPCError

from tgdrive.media.media_metadata import get_message
from tgdrive.media.media_scheduler import media_priority
from tgdrive.telegram.client_pool import ClientPoolError

THUMBNAIL_BYTES = 128 * 1024
THUMBNAIL_DIMENSION = 640


class ThumbnailBusy(Exception):
    """Optional thumbnail work is temporarily unavailable."""


@dataclass(frozen=True)
class Thumbnail:
    content: bytes
    media_type: str

    @property
    def etag(self) -> str:
        return '"' + hashlib.sha256(self.content).hexdigest() + '"'


def image_type(content: bytes) -> str | None:
    # Never serve an SVG/HTML/document body in an image-shaped endpoint.
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


async def load_thumbnail(*, pool: Any, channel_id: int, message_id: int):
    """Use an optional small-image transport; never fall back to stream_media."""
    priority = media_priority.set("preload")
    try:
        async with pool.lease(
            download=True,
            allow_users=True,
            respect_cooldown=True,
            priority="preload",
        ) as client:
            download = getattr(client, "download_thumbnail", None)
            if not callable(download):
                return None
            message = await get_message(client, channel_id, message_id)
            if message is None or getattr(message, "empty", False):
                return None
            content = await download(
                message,
                max_bytes=THUMBNAIL_BYTES,
                target_size=320,
                max_dimension=THUMBNAIL_DIMENSION,
            )
            if (
                not isinstance(content, bytes)
                or not 0 < len(content) <= THUMBNAIL_BYTES
            ):
                return None
            media_type = image_type(content)
            return Thumbnail(content, media_type) if media_type else None
    except (OSError, RPCError, ClientPoolError, ValueError) as exc:
        raise ThumbnailBusy from exc
    finally:
        media_priority.reset(priority)


@dataclass
class _Flight:
    task: asyncio.Task[Thumbnail | None]
    waiters: int = 0
    abandoned: bool = False


class ThumbnailCache:
    """Per-app LRU, two fills, bounded pending work, and concurrent coalescing."""

    def __init__(
        self,
        *,
        max_bytes: int = 16 * 1024**2,
        max_entries: int = 256,
        max_pending: int = 16,
        max_image_bytes: int = THUMBNAIL_BYTES,
        timeout: float = 10,
        ttl: float = 3600,
        missing_ttl: float = 300,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.max_pending = max_pending
        self.max_image_bytes = max_image_bytes
        self.timeout = timeout
        self.ttl = ttl
        self.missing_ttl = missing_ttl
        self.bytes = 0
        self._entries: OrderedDict[
            tuple[Any, ...], tuple[float, Thumbnail | None, Any]
        ] = OrderedDict()
        self._flights: dict[tuple[Any, ...], _Flight] = {}
        self._slots = asyncio.Semaphore(2)
        self._closed = False

    async def get(
        self,
        key: tuple[Any, ...],
        load: Callable[[], Awaitable[Thumbnail | None]],
        *,
        missing_revision: Any = None,
    ) -> Thumbnail | None:
        if self._closed:
            raise ThumbnailBusy
        self._expire()
        cached = self._entries.get(key)
        if cached is not None:
            if cached[1] is not None or cached[2] == missing_revision:
                self._entries.move_to_end(key)
                return cached[1]
            self._entries.pop(key)
        flight = self._flights.get(key)
        while flight is not None and flight.abandoned:
            await asyncio.gather(flight.task, return_exceptions=True)
            if self._flights.get(key) is flight:
                self._flights.pop(key, None)
            flight = self._flights.get(key)
        if self._closed:
            raise ThumbnailBusy
        if flight is None:
            if len(self._flights) >= self.max_pending:
                raise ThumbnailBusy
            flight = _Flight(
                asyncio.create_task(self._fill(key, load, missing_revision))
            )
            self._flights[key] = flight
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if not flight.waiters and not flight.task.done():
                flight.abandoned = True
                flight.task.cancel()
                await asyncio.gather(flight.task, return_exceptions=True)
            if flight.task.done() and self._flights.get(key) is flight:
                self._flights.pop(key, None)

    async def _fill(self, key, load, missing_revision):
        try:
            async with asyncio.timeout(self.timeout):
                async with self._slots:
                    result = await load()
        except TimeoutError as exc:
            raise ThumbnailBusy from exc
        # The transport and test/custom adapters both stay under the same cap.
        if result is not None and len(result.content) > self.max_image_bytes:
            result = None
        if result is not None and len(result.content) > self.max_bytes:
            return result
        self._expire()
        self._entries[key] = (
            time.monotonic() + (self.ttl if result is not None else self.missing_ttl),
            result,
            missing_revision,
        )
        self.bytes += len(result.content) if result is not None else 0
        while self.bytes > self.max_bytes or len(self._entries) > self.max_entries:
            _key, (_deadline, value, _revision) = self._entries.popitem(last=False)
            self.bytes -= len(value.content) if value is not None else 0
        return result

    def _expire(self):
        now = time.monotonic()
        for key, (deadline, value, _revision) in list(self._entries.items()):
            if deadline <= now:
                del self._entries[key]
                self.bytes -= len(value.content) if value is not None else 0

    async def invalidate_file(self, channel: int, message: int):
        keys = {
            key
            for key in (*self._entries, *self._flights)
            if key[:2] == (channel, message)
        }
        tasks = []
        for key in keys:
            flight = self._flights.get(key)
            if flight is not None:
                flight.task.cancel()
                tasks.append(flight.task)
            cached = self._entries.pop(key, None)
            if cached is not None and cached[1] is not None:
                self.bytes -= len(cached[1].content)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        self._closed = True
        tasks = [flight.task for flight in self._flights.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._flights.clear()
        self._entries.clear()
        self.bytes = 0
