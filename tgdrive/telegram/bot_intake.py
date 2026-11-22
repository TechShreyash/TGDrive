"""One durable bot file pipeline at a time, using the transfer journal."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING

from tgdrive.media.media_scheduler import media_priority
from tgdrive.transfers.tasks import TransferRecord, _bot_cleanup_pending, _journalled

if TYPE_CHECKING:
    from tgdrive.transfers.manager import TransferManager


class BotIntake:
    def __init__(
        self,
        manager: TransferManager,
        process: Callable[[TransferRecord], Awaitable[None]],
        cleanup: Callable[[TransferRecord], Awaitable[bool]],
    ) -> None:
        self.manager, self.process, self.cleanup = manager, process, cleanup
        self._changed = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._closing = False

    def pending(self) -> list[TransferRecord]:
        return sorted(
            (
                row
                for row in self.manager.registry.list()
                if row.context.get("bot_source") and not row.terminal
            ),
            key=lambda row: (row.created_at, row.id),
        )

    @_journalled
    async def submit(self, transfer_id: str, size: int, context: dict):
        record = await self.manager.registry.create(
            "queued", total=size, transfer_id=transfer_id, context=context
        )
        self.wake()
        return record

    def wake(self) -> None:
        self._changed.set()

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._closing = False
            self._worker = asyncio.create_task(self._run(), name="bot-intake")

    async def stop(self) -> None:
        self._closing = True
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    async def _run(self) -> None:
        while not self._closing and not self.manager.registry._shutting_down:
            self._changed.clear()
            cleanup = next(
                (
                    row
                    for row in reversed(self.manager.registry.list())
                    if _bot_cleanup_pending(row.committed.context)
                ),
                None,
            )
            if cleanup is not None:
                if not cleanup.context["bot_intake"].get("message_id"):
                    await self._changed.wait()
                    continue
                deadline = cleanup.context["bot_intake"].get("retry_at", 0)
                if type(deadline) not in (int, float) or not math.isfinite(deadline):
                    deadline = 0
                if deadline > time.time():
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._changed.wait(), deadline - time.time()
                        )
                    continue
                await self.cleanup(cleanup)
                continue
            pending = self.pending()
            if not pending:
                await self._changed.wait()
                continue
            record = pending[0]
            source = record.context["bot_source"]
            account = source.get("account") if isinstance(source, dict) else None
            retry_at = max(
                (
                    row.context.get("bot_retry_at", 0)
                    for row in self.manager.registry.list()
                    if isinstance(row.context.get("bot_source"), dict)
                    and row.context["bot_source"].get("account") == account
                    and type(row.context.get("bot_retry_at", 0)) in (int, float)
                    and math.isfinite(row.context.get("bot_retry_at", 0))
                ),
                default=0,
            )
            if retry_at > time.time():
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), retry_at - time.time())
                continue
            if record.task is None or record.task.done():
                token = media_priority.set("preload")
                try:
                    self.manager.registry.spawn(record.id, self.process)
                finally:
                    media_priority.reset(token)
            if record.task is not None:
                try:
                    # The registry, not a Telegram update handler, owns this job.
                    await asyncio.shield(record.task)
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise
