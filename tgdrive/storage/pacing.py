"""Pace background MongoDB operations so owner requests keep their headroom.

The free Atlas tier allows 100 operations per second in total and throttles
the whole connection beyond that. Thumbnail rebuilds and preloads re-check
files in MongoDB as they read, so unpaced they could take most of that
budget while the owner browses. They wait here instead; owner requests never
do.
"""

from __future__ import annotations

import asyncio
import time

# shortcut: a fixed share of the free tier's 100 ops/s; tie it to measured
# MongoDB latency in the governor if paid or local databases need more.
BACKGROUND_OPERATIONS_PER_SECOND = 30


class Pacer:
    def __init__(self, per_second: float) -> None:
        self.interval = 1 / per_second
        self._next = 0.0

    async def wait(self, operations: int = 1) -> None:
        # One event loop and no await before the update, so no lock is needed.
        now = time.monotonic()
        start = max(now, self._next)
        self._next = start + operations * self.interval
        if start > now:
            await asyncio.sleep(start - now)


background = Pacer(BACKGROUND_OPERATIONS_PER_SECOND)
