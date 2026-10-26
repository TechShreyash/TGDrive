"""Concurrent-only Telegram metadata coalescing, never an authorization cache."""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass, field
from typing import Any

from tgdrive.media.media_scheduler import (
    MediaPriorityScope,
    effective_priority,
    media_priority,
)


@dataclass
class _Flight:
    task: asyncio.Task[Any]
    priority: MediaPriorityScope
    waiters: int = 0
    abandoned: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)


_FLIGHTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def get_message(client: Any, channel: int, message_id: int) -> Any:
    loop = asyncio.get_running_loop()
    flights = _FLIGHTS.setdefault(loop, {})
    key = (id(client), channel, message_id)
    flight = flights.get(key)
    while flight is not None and flight.abandoned:
        await flight.done.wait()
        flight = flights.get(key)
    if flight is None:
        from tgdrive.telegram.client_pool import MediaQueueFull

        pool = getattr(client, "_identity_pool", None)
        maximum = getattr(pool, "_max_media_queue", 256)
        if len(flights) >= maximum:
            raise MediaQueueFull(retry_after=1)
        inherited = media_priority.get()
        priority = (
            inherited
            if isinstance(inherited, MediaPriorityScope)
            else MediaPriorityScope(inherited)
        )
        token = media_priority.set(priority)
        try:
            task = asyncio.create_task(
                client.get_messages(channel, message_ids=message_id)
            )
        finally:
            media_priority.reset(token)
        flight = _Flight(task, priority)
        flights[key] = flight

        def completed(_task):
            if flights.get(key) is flight:
                flights.pop(key, None)
            flight.done.set()

        task.add_done_callback(completed)
    flight.waiters += 1
    try:
        if effective_priority(media_priority.get()) == "demand":
            await flight.priority.promote()
        return await asyncio.shield(flight.task)
    finally:
        flight.waiters -= 1
        if not flight.waiters and not flight.task.done():
            flight.abandoned = True
            flight.task.cancel()
            while not flight.task.done():
                try:
                    await asyncio.shield(flight.task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
