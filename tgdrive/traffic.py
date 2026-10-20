"""Bounded process counters for delivered HTTP bytes, separate from origin traffic."""

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger(__name__)
HISTORY_SECONDS = 300
MAX_SAMPLE_GAP_SECONDS = 30


class TrafficCounters:
    def __init__(self):
        self.started_at = time.time()
        self.requests = 0
        self.active = 0
        self.download_bytes = 0
        self.upload_bytes = 0
        self.errors = 0
        self.history = deque(maxlen=120)
        self._last_sample = 0

    def snapshot(self):
        now = time.time()
        current = {
            "at": now,
            "requests": self.requests,
            "active": self.active,
            "download_bytes": self.download_bytes,
            "upload_bytes": self.upload_bytes,
            "errors": self.errors,
        }
        # Never draw a continuous rate across a suspended process or wall-clock jump.
        if (
            self.history
            and not 0 <= now - self.history[-1]["at"] <= MAX_SAMPLE_GAP_SECONDS
        ):
            self.history.clear()
            self._last_sample = 0
        while self.history and self.history[0]["at"] < now - HISTORY_SECONDS:
            self.history.popleft()
        previous = self.history[-1] if self.history else None
        if previous is not None and now - previous["at"] < 1:
            previous = self.history[-2] if len(self.history) > 1 else None
        seconds = now - previous["at"] if previous else 0
        rates = {
            name + "_per_second": (
                max(0, current[name] - previous[name]) / seconds
                if previous is not None and seconds > 0
                else None
            )
            for name in ("download_bytes", "upload_bytes")
        }
        if now - self._last_sample >= 1:
            self._last_sample = now
            self.history.append(current)
        return (
            current
            | rates
            | {
                "started_at": self.started_at,
                "history": list(self.history),
                "history_window_seconds": HISTORY_SECONDS,
            }
        )


async def sample_performance(counters, pool, *, interval=3):
    """Keep a small recent history even when no dashboard is open; no I/O or RPCs."""
    while True:
        counters.snapshot()
        snapshot = getattr(pool, "performance_snapshot", None)
        if callable(snapshot):
            try:
                snapshot()
            except Exception:
                # Diagnostics must not interrupt file service or terminate sampling.
                logger.exception("Could not sample Telegram activity")
        await asyncio.sleep(interval)


class TrafficMiddleware:
    def __init__(self, app, counters):
        self.app, self.counters = app, counters

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        tracked = scope["type"] == "http" and (
            path == "/file" or path.startswith("/api/v1/uploads")
        )
        if not tracked:
            return await self.app(scope, receive, send)
        self.counters.active += 1
        self.counters.requests += 1
        failed = False

        async def read():
            message = await receive()
            if message["type"] == "http.request":
                self.counters.upload_bytes += len(message.get("body", b""))
            return message

        async def write(message):
            nonlocal failed
            if message["type"] == "http.response.start" and message["status"] >= 400:
                failed = True
            await send(message)
            if message["type"] == "http.response.body":
                self.counters.download_bytes += len(message.get("body", b""))

        try:
            await self.app(scope, read, write)
        except BaseException:
            failed = True
            raise
        finally:
            self.counters.active -= 1
            self.counters.errors += int(failed)
