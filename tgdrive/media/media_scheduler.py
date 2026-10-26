"""Bounded account-wide admission for Telegram media RPCs.

Priority applies to work which has not started. Running requests are never
cancelled to admit newer work. Estimates describe recent observations only.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

MediaPriority = Literal["demand", "preload"]


class MediaPriorityScope:
    """Let foreground subscribers promote one shared fill without restarting it."""

    def __init__(self, priority: MediaPriority):
        self.priority = priority
        self._conditions: list[asyncio.Condition] = []
        self._updates: list[Callable[[], None]] = []

    def subscribe(self, condition: asyncio.Condition, on_promote=None):
        self._conditions.append(condition)
        if on_promote is not None:
            self._updates.append(on_promote)

        def unsubscribe():
            self._conditions.remove(condition)
            if on_promote is not None:
                self._updates.remove(on_promote)

        return unsubscribe

    async def promote(self):
        if self.priority == "demand":
            return
        self.priority = "demand"
        for update in self._updates:
            update()
        for condition in set(self._conditions):
            async with condition:
                condition.notify_all()


def effective_priority(value: MediaPriority | MediaPriorityScope) -> MediaPriority:
    return value.priority if isinstance(value, MediaPriorityScope) else value


media_priority: ContextVar[MediaPriority | MediaPriorityScope] = ContextVar(
    "media_priority", default="demand"
)


@dataclass(slots=True)
class _Request:
    number: int
    priority: MediaPriority | MediaPriorityScope
    queued_at: float


class MediaScheduler:
    """Share concurrency, rate and flood feedback across one account's transfers."""

    def __init__(self, *, limit: int = 8, rate: float = 48) -> None:
        self.configure(limit=limit, rate=rate)
        self.active = 0
        self.completed = 0
        self.failed = 0
        self.floods = 0
        self.bytes = 0
        self.upload_bytes = 0
        self.download_bytes = 0
        self._sequence = 0
        self._condition = asyncio.Condition()
        self._waiting: list[_Request] = []
        self._next_request = 0.0
        self._cooldown = 0.0
        self._durations: deque[float] = deque(maxlen=128)
        self._history: deque[dict] = deque(maxlen=120)
        self._last_sample = 0.0
        self._successes = 0

    def configure(self, *, limit: int, rate: float) -> None:
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("Telegram request limit must be between 1 and 32")
        if not 1 <= rate <= 512:
            raise ValueError("Telegram request rate must be between 1 and 512")
        self.limit = limit
        self.rate = float(rate)
        self.effective_limit = limit
        self.effective_rate = float(rate)

    async def flood(self, seconds: float) -> None:
        async with self._condition:
            self.floods += 1
            self._successes = 0
            self.effective_limit = max(1, self.effective_limit // 2)
            self.effective_rate = max(1.0, self.effective_rate / 2)
            self._cooldown = max(self._cooldown, time.monotonic() + seconds)
            self._condition.notify_all()

    def record_bytes(self, amount: int, *, upload: bool = False) -> None:
        self.bytes += amount
        if upload:
            self.upload_bytes += amount
        else:
            self.download_bytes += amount

    @asynccontextmanager
    async def request(self):
        queued_at = time.monotonic()
        async with self._condition:
            self._sequence += 1
            ticket = _Request(self._sequence, media_priority.get(), queued_at)
            self._waiting.append(ticket)

            def promote_waiter():
                self._sequence += 1
                ticket.number = self._sequence

            unsubscribe = (
                ticket.priority.subscribe(self._condition, promote_waiter)
                if isinstance(ticket.priority, MediaPriorityScope)
                else None
            )
            try:
                while True:
                    now = time.monotonic()
                    first = min(
                        self._waiting,
                        key=lambda item: (
                            effective_priority(item.priority) == "preload",
                            item.number,
                        ),
                    )
                    deadline = max(self._cooldown, self._next_request)
                    if (
                        first is ticket
                        and self.active < self.effective_limit
                        and deadline <= now
                    ):
                        self._waiting.remove(ticket)
                        self.active += 1
                        self._next_request = now + 1 / self.effective_rate
                        self._condition.notify_all()
                        break
                    delay = max(0.001, deadline - now) if deadline > now else None
                    try:
                        if delay is None:
                            await self._condition.wait()
                        else:
                            await asyncio.wait_for(self._condition.wait(), delay)
                    except TimeoutError:
                        pass
            except BaseException:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                raise
            finally:
                if unsubscribe is not None:
                    unsubscribe()
        started = time.monotonic()
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            async with self._condition:
                self.active -= 1
                if succeeded:
                    self.completed += 1
                    self._successes += 1
                    self._durations.append(time.monotonic() - started)
                    if self._successes >= 32:
                        self._successes = 0
                        self.effective_limit = min(self.limit, self.effective_limit + 1)
                        self.effective_rate = min(self.rate, self.effective_rate + 1)
                else:
                    self.failed += 1
                self._condition.notify_all()

    def snapshot(self) -> dict:
        now = time.monotonic()
        mean = sum(self._durations) / len(self._durations) if self._durations else None
        waiting = sorted(
            self._waiting,
            key=lambda item: (
                effective_priority(item.priority) == "preload",
                item.number,
            ),
        )
        queue = []
        for position, ticket in enumerate(waiting, 1):
            estimated = None
            if mean is not None:
                estimated = max(0.0, self._cooldown - now) + max(
                    (position - 1) / self.effective_rate,
                    ((self.active + position - 1) // self.effective_limit) * mean,
                )
            queue.append(
                dict(
                    position=position,
                    priority=effective_priority(ticket.priority),
                    waiting_seconds=round(now - ticket.queued_at, 3),
                    estimated_wait_seconds=estimated,
                )
            )
        sample = dict(
            at=time.time(),
            active=self.active,
            queued=len(queue),
            completed=self.completed,
            failed=self.failed,
            floods=self.floods,
            bytes=self.bytes,
            upload_bytes=self.upload_bytes,
            download_bytes=self.download_bytes,
        )
        if now - self._last_sample >= 1:
            self._history.append(sample)
            self._last_sample = now
        return dict(
            **sample,
            configured_limit=self.limit,
            effective_limit=self.effective_limit,
            configured_parts_per_second=self.rate,
            effective_parts_per_second=self.effective_rate,
            cooldown_seconds=max(0.0, self._cooldown - now),
            mean_request_seconds=mean,
            estimates=True,
            queue=queue[:128],
            history=list(self._history),
        )
