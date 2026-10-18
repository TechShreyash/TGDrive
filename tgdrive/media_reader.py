"""Ordered, bounded read-ahead across healthy Telegram accounts."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from telethon.errors import FloodWaitError, RPCError

from tgdrive.media_cache import DiskMediaCache
from tgdrive.telegram_clients import _block_identity, _BlockCache

BLOCK = 1024**2
READ_AHEAD = 32  # MiB per response, including currently requested windows.


class MediaReadCache:
    def __init__(self, directory=None):
        self.memory = _BlockCache(
            max_bytes=64 * BLOCK,
            max_entries=256,
            ttl=600,
            read_slots=12,
        )
        self.disk = DiskMediaCache(directory) if directory is not None else None

    async def get(self, key, expected, remote):
        async def load():
            if self.disk is not None:
                payload = await asyncio.to_thread(self.disk.read, key, expected)
                if payload is not None:
                    return payload
            payload = await remote()
            if len(payload) != expected:
                raise OSError("Telegram read ended before the declared block length")
            if self.disk is not None:
                await asyncio.to_thread(self.disk.write, key, payload)
            return payload

        return await self.memory.get(key, expected, load)


async def stream_pooled_media(
    pool: Any,
    client: Any,
    message: Any,
    *,
    channel: int,
    limit: int,
    offset: int,
) -> AsyncIterator[bytes]:
    """Authorization and initial message validation precede every cache read.

    Each worker binds the same document through its own account. Content keys
    include channel, message, immutable document/rendition, size and block index.
    Cached payloads are shared across clients; Telegram access hashes are not.
    """
    if limit <= 0:
        return
    identity = _block_identity(message, offset)
    if identity is None or not callable(getattr(pool, "download_capacity", None)):
        iterator = client.stream_media(message, limit=limit, offset=offset)
        try:
            async for data in iterator:
                yield data
        finally:
            await iterator.aclose()
        return

    if pool._media_read_cache is None:
        pool._media_read_cache = MediaReadCache(pool.media_cache_dir)
    cache = pool._media_read_cache
    source = getattr(message, "native", message)
    message_id = source.id
    quality = getattr(message, "variant_id", None)
    bindings = {id(client): message}
    locks: dict[int, asyncio.Lock] = {}

    async def bind(account):
        key = id(account)
        async with locks.setdefault(key, asyncio.Lock()):
            if key not in bindings:
                fresh = await account.get_messages(channel, message_ids=message_id)
                if fresh is None:
                    raise FileNotFoundError("Stored Telegram message was removed")
                if quality is not None:
                    fresh = account.select_media_variant(fresh, quality)
                if _block_identity(fresh, offset) != identity:
                    raise FileNotFoundError("Stored Telegram media changed")
                bindings[key] = fresh
            return bindings[key]

    async def remote(index):
        for attempt in range(3):
            async with pool.lease(download=True, allow_users=True) as account:
                try:
                    bound = await bind(account)
                    iterator = account.stream_media(bound, limit=1, offset=index)
                    try:
                        data = await anext(iterator)
                        return data
                    finally:
                        await iterator.aclose()
                except FloodWaitError as error:
                    await pool.defer_client(
                        account.name, time.time() + max(1, error.seconds)
                    )
                    if attempt == 2:
                        raise
                except (OSError, RPCError):
                    if attempt == 2:
                        raise
                    # A temporary failure should not monopolize the next window.
                    bindings.pop(id(account), None)
                    await pool.defer_client(account.name, time.time() + 2)
        raise OSError("No Telegram account could read this block")

    async def read(index):
        block = _block_identity(message, index)
        if block is None:
            raise OSError("Telegram block lies outside the validated media")
        key, expected = block
        return await cache.get(key, expected, lambda: remote(index))

    pending: deque[asyncio.Task[bytes]] = deque()
    tasks: set[asyncio.Task[bytes]] = set()
    next_index, end = offset, offset + limit

    def schedule():
        nonlocal next_index
        task = asyncio.create_task(read(next_index))
        pending.append(task)
        tasks.add(task)
        next_index += 1

    window = min(READ_AHEAD, max(4, await pool.download_capacity() * 4), limit)
    try:
        for _ in range(window):
            schedule()
        while pending:
            task = pending.popleft()
            data = await task
            tasks.discard(task)
            # Refill before yielding so read-ahead progresses while ASGI sends.
            if next_index < end:
                schedule()
            yield data
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
