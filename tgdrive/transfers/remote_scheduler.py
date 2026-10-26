"""Bounded URL connections, FIFO full-speed slots and paced waiting sources."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

WARM_BYTES_PER_SECOND = 64 * 1024
WARM_READ_SIZE = 16 * 1024
WARM_STAGING_WINDOW = 32 * 1024 * 1024
EXTRA_SOURCE_CONNECTIONS = 8


@dataclass(eq=False, slots=True)
class DownloadTicket:
    can_promote: Callable[[], bool]
    changed: Callable[[str], Awaitable[None] | None]
    full_speed: bool = False
    next_read: float = 0
    promoted: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None

    async def notify(self, mode: str) -> None:
        result = self.changed(mode)
        if inspect.isawaitable(result):
            await result

    async def pace(self) -> None:
        if self.error is not None:
            raise self.error
        delay = self.next_read - time.monotonic()
        if not self.full_speed and delay > 0:
            with suppress(TimeoutError):
                await asyncio.wait_for(self.promoted.wait(), delay)

    def received(self, size: int) -> None:
        if not self.full_speed:
            self.next_read = time.monotonic() + size / WARM_BYTES_PER_SECOND


class URLDownloadScheduler:
    def __init__(self, full_speed_limit: int = 2) -> None:
        self._tickets: list[DownloadTicket] = []
        self._changed = asyncio.Event()
        self._monitor: asyncio.Task[None] | None = None
        self._refresh_lock = asyncio.Lock()
        if type(full_speed_limit) is not int or not 1 <= full_speed_limit <= 8:
            raise ValueError("URL download concurrency must be between 1 and 8")
        self.full_speed_limit = full_speed_limit

    async def configure(self, limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("URL download concurrency must be between 1 and 8")
        self.full_speed_limit = limit
        await self.refresh()
        self._notify()

    async def refresh(self) -> None:
        async with self._refresh_lock:
            running = sum(ticket.full_speed for ticket in self._tickets)
            for ticket in self._tickets:
                if running >= self.full_speed_limit:
                    break
                if ticket.full_speed or ticket.error is not None:
                    continue
                try:
                    ready = ticket.can_promote()
                except Exception as error:
                    ticket.error = error
                    ticket.promoted.set()
                    continue
                if ready:
                    ticket.full_speed = True
                    ticket.promoted.set()
                    await ticket.notify("full")
                    running += 1

    def _notify(self) -> None:
        previous, self._changed = self._changed, asyncio.Event()
        previous.set()

    async def wait(self) -> None:
        await self._changed.wait()

    async def _watch(self) -> None:
        try:
            while self._tickets:
                await asyncio.sleep(1)
                await self.refresh()
        finally:
            if self._monitor is asyncio.current_task():
                self._monitor = None

    @asynccontextmanager
    async def admit(self, wait, can_promote=None, changed=None):
        changed = changed or (lambda _mode: None)
        ticket = DownloadTicket(can_promote or (lambda: True), changed)
        await ticket.notify("queued")
        while len(self._tickets) >= self.full_speed_limit + EXTRA_SOURCE_CONNECTIONS:
            await wait(self.wait())
        self._tickets.append(ticket)
        try:
            await ticket.notify("warm")
            await self.refresh()
            if self._monitor is None:
                self._monitor = asyncio.create_task(self._watch())
            yield ticket
        finally:
            self._tickets.remove(ticket)
            try:
                await self.refresh()
            finally:
                self._notify()
                if not self._tickets and self._monitor is not None:
                    monitor, self._monitor = self._monitor, None
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
