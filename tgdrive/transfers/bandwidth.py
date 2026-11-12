"""Drive-wide logical file-payload admission, separate from protocol pacing."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from tgdrive.media.media_scheduler import current_fair_group, fair_group_ranks
from tgdrive.transfers.tasks import _journalled

QUANTUM_BYTES = 64 * 1024
MIN_LIMIT_BYTES_PER_SECOND = 1024
MAX_LIMIT_BYTES_PER_SECOND = 2**53 - 1


@dataclass
class _Deadline:
    timeout: asyncio.Timeout
    seconds: float
    waiting: int = 0
    remaining: float = 0.0
    closed: bool = False

    @contextmanager
    def pause(self):
        if not self.waiting:
            end = self.timeout.when()
            self.remaining = max(0, end - time.monotonic()) if end is not None else 0
            if self.remaining <= 0:
                raise TimeoutError
            self.timeout.reschedule(None)
        self.waiting += 1
        try:
            yield
        finally:
            self.waiting -= 1
            if not self.waiting and not self.closed:
                self.timeout.reschedule(time.monotonic() + self.remaining)


_deadline: ContextVar[_Deadline | None] = ContextVar(
    "file_payload_deadline", default=None
)


@asynccontextmanager
async def payload_timeout(seconds: float):
    """Keep an existing operation timeout while excluding intentional admission wait."""
    async with asyncio.timeout(seconds) as timeout:
        deadline = _Deadline(timeout, seconds)
        token = _deadline.set(deadline)
        try:
            yield
        finally:
            deadline.closed = True
            _deadline.reset(token)


def provider_timeout():
    """An individual provider call must stay bounded while sibling tasks pace."""
    deadline = _deadline.get()
    return asyncio.timeout(deadline.seconds if deadline is not None else None)


@dataclass(eq=False)
class _Waiter:
    remaining: float
    fair_group: str | None
    at: float | None = None


class PayloadBudget:
    def __init__(self, *, clock=time.monotonic):
        self.rate: int | None = None
        self._condition = asyncio.Condition()
        self._queue: deque[_Waiter] = deque()
        self._last_group = object()
        self.admitted_bytes = 0
        self.wait_seconds = 0.0
        self._clock = clock

    def quantum(self):
        return (
            min(QUANTUM_BYTES, max(1, self.rate // 10)) if self.rate else QUANTUM_BYTES
        )

    def _advance(self):
        now = self._clock()
        if self._queue:
            head = self._queue[0]
            if head.at is not None and self.rate is not None:
                head.remaining = max(
                    0, head.remaining - max(0, now - head.at) * self.rate
                )
            head.at = now

    async def configure(self, rate: int | None):
        async with self._condition:
            self._advance()
            self.rate = rate
            self._condition.notify_all()

    async def acquire(self, count: int):
        if type(count) is not int or count < 0:
            raise ValueError("Payload size must be nonnegative integer bytes")
        while count:
            size = min(count, self.quantum())
            if self.rate is None:
                self.admitted_bytes += count
                return
            started = self._clock()
            waiter = _Waiter(size, current_fair_group())
            async with self._condition:
                self._queue.append(waiter)
                try:
                    while True:
                        if self._queue[0] is waiter:
                            self._advance()
                            if self.rate is None or waiter.remaining <= 0:
                                self.admitted_bytes += size
                                break
                            delay = waiter.remaining / self.rate
                        else:
                            delay = None
                        try:
                            deadline = _deadline.get()
                            if deadline is None or deadline.closed:
                                async with asyncio.timeout(delay):
                                    await self._condition.wait()
                            else:
                                with deadline.pause():
                                    async with asyncio.timeout(delay):
                                        await self._condition.wait()
                        except TimeoutError:
                            pass
                finally:
                    head = self._queue[0] is waiter
                    self._queue.remove(waiter)
                    if head:
                        self._last_group = waiter.fair_group
                        if self._queue:
                            ranks = fair_group_ranks(
                                (item.fair_group for item in self._queue),
                                self._last_group,
                            )
                            selected = min(
                                self._queue, key=lambda item: ranks[item.fair_group]
                            )
                            self._queue.remove(selected)
                            self._queue.appendleft(selected)
                    if not self._queue:
                        self._last_group = object()
                    self.wait_seconds += max(0, self._clock() - started)
                    self._condition.notify_all()
            count -= size

    def snapshot(self):
        return {
            "effective_bytes_per_second": self.rate,
            "waiting": len(self._queue),
            "admitted_bytes": self.admitted_bytes,
            "wait_seconds": self.wait_seconds,
        }


class BudgetedReader:
    """Telethon accepts asynchronous reads when the caller supplies file_size."""

    def __init__(self, source, budget: PayloadBudget):
        self.source, self.budget = source, budget
        self.name = source.name

    async def read(self, size: int):
        block = await _journalled(asyncio.to_thread)(self.source.read, size)
        await self.budget.acquire(len(block))
        return block


def _lower(left: int | None, right: int | None) -> int | None:
    return right if left is None else left if right is None else min(left, right)


class FileBandwidth:
    def __init__(self):
        self.upload = PayloadBudget()
        self.download = PayloadBudget()
        self._configured: tuple[int | None, int | None] = (None, None)
        self._engines: dict[Any, tuple[int | None, ...]] = {}
        self._lock = asyncio.Lock()

    @_journalled
    async def configure(self, upload: int | None, download: int | None):
        for rate in (upload, download):
            if rate is not None and (
                type(rate) is not int
                or not MIN_LIMIT_BYTES_PER_SECOND <= rate <= MAX_LIMIT_BYTES_PER_SECOND
            ):
                raise ValueError("Invalid aggregate file speed limit")
        async with self._lock:
            previous = self._configured
            self._configured = upload, download
            try:
                await self._rebalance()
            except BaseException:
                self._configured = previous
                raise

    async def _rebalance(self):
        while True:
            # Reserve one aggregate Python share. No per-account multiplication.
            shares = tuple(
                None if rate is None else rate // (len(self._engines) + 1)
                for rate in self._configured
            )
            python = tuple(
                None if rate is None else rate - (share or 0) * len(self._engines)
                for rate, share in zip(self._configured, shares, strict=True)
            )
            for budget, target in zip(
                (self.upload, self.download), python, strict=True
            ):
                await budget.configure(_lower(budget.rate, target))
            failed = False
            for final in (False, True):
                for engine, previous in list(self._engines.items()):
                    limits = (
                        shares
                        if final
                        else tuple(
                            _lower(old, new)
                            for old, new in zip(previous, shares, strict=True)
                        )
                    )
                    if limits == previous:
                        continue
                    try:
                        await engine.set_bandwidth(*limits)
                    except Exception:
                        # Never leave an owned engine running above a saved cap.
                        await engine.stop_for_bandwidth()
                        del self._engines[engine]
                        failed = True
                        break
                    self._engines[engine] = limits
                if failed:
                    break
            if not failed:
                for budget, target in zip(
                    (self.upload, self.download), python, strict=True
                ):
                    await budget.configure(target)
                return

    @asynccontextmanager
    async def native(self, engine):
        try:
            async with self._lock:
                # Zero is aria2's unlimited sentinel; never allocate a zero share.
                if len(self._engines) >= MIN_LIMIT_BYTES_PER_SECOND - 1:
                    raise RuntimeError("Too many native file transfer engines")
                self._engines[engine] = (None, None)
                await self._rebalance()
                if engine not in self._engines:
                    raise OSError("Could not apply torrent speed limits")
            yield
        finally:
            await self._finish(engine)

    @_journalled
    async def _finish(self, engine):
        # Stop payload before returning its allocation to other participants.
        await engine.stop_for_bandwidth()
        async with self._lock:
            self._engines.pop(engine, None)
            await self._rebalance()

    def snapshot(self):
        return {
            "upload": {
                "configured_bytes_per_second": self._configured[0],
                **self.upload.snapshot(),
            },
            "download": {
                "configured_bytes_per_second": self._configured[1],
                **self.download.snapshot(),
            },
            "native_engines": len(self._engines),
            "scope": (
                "Managed file payload admission and native torrent averages; "
                "excludes protocol overhead and provider retransmissions."
            ),
        }
