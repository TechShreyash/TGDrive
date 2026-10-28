"""Bounded logical reads across immutable Telegram document parts."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from typing import Any

from telethon.errors import FloodWaitError, RPCError

from tgdrive.media.media_metadata import get_message
from tgdrive.media.ranges import ByteRange, plan_telegram_chunks
from tgdrive.media.streaming import TelegramFileNotFound, _media_from_message


def part_ranges(
    parts: list[dict[str, Any]],
    selected: ByteRange,
) -> list[tuple[dict[str, Any], ByteRange]]:
    spans = []
    for part in parts:
        start = max(selected.start, part["offset"])
        end = min(selected.end, part["offset"] + part["size"] - 1)
        if start <= end:
            spans.append(
                (
                    part,
                    ByteRange(
                        start - part["offset"],
                        end - part["offset"],
                        part["size"],
                        is_partial=True,
                    ),
                )
            )
    if sum(interval.length for _, interval in spans) != selected.length:
        raise TelegramFileNotFound("Stored file parts do not cover this byte range")
    return spans


class PartLease:
    """One selected part, with owned release even if HTTP cancellation repeats."""

    def __init__(
        self,
        pool: Any,
        channel: int,
        part: dict[str, Any],
        *,
        authorize=None,
        request_id=None,
        background=False,
    ):
        self.pool, self.channel, self.part = pool, channel, part
        self.authorize, self.request_id = authorize, request_id
        self.background = background
        self.lease: Any = None
        self.client: Any = None
        self.message: Any = None
        self._release_task: asyncio.Task | None = None

    async def _fetch(self) -> None:
        if self.authorize is not None:
            await self.authorize()
        self.message = await get_message(
            self.client, self.channel, self.part["message_id"]
        )
        media = _media_from_message(self.message)
        if (
            self.message is None
            or getattr(self.message, "empty", False)
            or media is None
        ):
            raise TelegramFileNotFound("A stored file part is missing from Telegram")
        actual = getattr(media, "file_size", None)
        if type(actual) is not int or actual != self.part["size"]:
            raise TelegramFileNotFound("A stored file part has changed size")
        if "document_id" in self.part:
            native = getattr(self.message, "native", self.message)
            document_id = getattr(getattr(native, "document", None), "id", None)
            if document_id != self.part["document_id"]:
                raise TelegramFileNotFound(
                    "A stored file part was replaced in Telegram"
                )

    async def _acquire(self, *, prefer_premium: bool = False) -> None:
        priority = {"priority": "preload", "download": True} if self.background else {}
        lease = self.pool.lease(
            prefer_premium=prefer_premium, respect_cooldown=True, **priority
        )
        client = await lease.__aenter__()
        # A context that never yielded owns no client lease to release.
        self.lease, self.client = lease, client
        self._release_task = None

    async def open(self) -> PartLease:
        await self._acquire(prefer_premium=True)
        try:
            for attempt in range(3):
                try:
                    await self._fetch()
                    break
                except (OSError, RPCError, ValueError) as error:
                    problem = await self.pool.report_failure(self.client, error)
                    if isinstance(error, FloodWaitError):
                        await self.pool.defer_client(
                            self.client.name,
                            time.time() + max(1, error.seconds),
                        )
                    elif problem is None and self.pool.kind_of(self.client) != "user":
                        raise
                    if attempt == 2:
                        if problem is not None:
                            raise problem from None
                        raise
                    await self.close()
                    self.lease = None
                    await self._acquire()
            from tgdrive.telegram.clients import _block_identity

            if _block_identity(self.message, 0) is not None:
                await self.close()
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self.lease is None:
            return
        if self._release_task is None:
            self._release_task = asyncio.create_task(
                self.lease.__aexit__(None, None, None),
            )
        interrupted = False
        while not self._release_task.done():
            try:
                await asyncio.shield(self._release_task)
            except asyncio.CancelledError:
                interrupted = True
        self._release_task.result()
        if interrupted:
            raise asyncio.CancelledError

    async def read(
        self, selected: ByteRange, *, authorize=None, request_id=None
    ) -> AsyncIterator[bytes]:
        plan = plan_telegram_chunks(selected)
        from tgdrive.media.media_reader import stream_pooled_media

        iterator = stream_pooled_media(
            self.pool,
            self.client,
            self.message,
            channel=self.channel,
            limit=plan.part_count,
            offset=plan.offset // plan.chunk_size,
            authorize=authorize or self.authorize,
            request_id=request_id or self.request_id,
            background=self.background,
        )
        emitted = 0
        count = 0
        whole_part = selected.start == 0 and selected.length == self.part["size"]
        digest = hashlib.sha256() if whole_part and "sha256" in self.part else None
        try:
            async for chunk in iterator:
                if count >= plan.part_count:
                    break
                cut_start = plan.first_part_cut if count == 0 else 0
                cut_end = (
                    plan.last_part_cut if count == plan.part_count - 1 else len(chunk)
                )
                selected_bytes = chunk[cut_start:cut_end]
                count += 1
                emitted += len(selected_bytes)
                if digest is not None:
                    digest.update(selected_bytes)
                if count == plan.part_count:
                    if emitted != selected.length:
                        raise OSError(
                            "Telegram file part ended before its declared length"
                        )
                    if digest is not None and digest.hexdigest() != self.part["sha256"]:
                        # Withhold the last block so an incorrect full part can never
                        # satisfy Content-Length and appear to download successfully.
                        raise OSError("Telegram file part failed its checksum")
                if selected_bytes:
                    yield selected_bytes
            if emitted != selected.length:
                raise OSError("Telegram file part ended before its declared length")
        finally:
            await iterator.aclose()


async def read_logical_prefix(
    *,
    pool: Any,
    channel_id: int,
    parts: list[dict[str, Any]],
    size: int,
    limit: int,
) -> bytes:
    """Fetch only a bounded prefix, including tiny parts crossing that prefix."""
    if not 0 < limit <= 1024 * 1024:
        raise ValueError("Logical prefix limit must be between 1 byte and 1 MiB")
    selection = ByteRange(0, min(size, limit) - 1, size, is_partial=False)
    output = bytearray()
    for part, interval in part_ranges(parts, selection):
        lease = await PartLease(pool, channel_id, part).open()
        iterator = lease.read(interval)
        try:
            async for chunk in iterator:
                output.extend(chunk)
        finally:
            try:
                await iterator.aclose()
            finally:
                await lease.close()
    return bytes(output)
