"""Bounded process counters for delivered HTTP bytes, separate from origin traffic."""

import time
from collections import deque


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
        if now - self._last_sample >= 1:
            self._last_sample = now
            self.history.append(current)
        return current | {"started_at": self.started_at, "history": list(self.history)}


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
