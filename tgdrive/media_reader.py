"""Ordered, bounded read-ahead across healthy Telegram accounts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from telethon.errors import FloodWaitError, RPCError

from tgdrive.media_cache import DiskMediaCache
from tgdrive.media_policy import MediaCachePolicy, merge_ranges
from tgdrive.telegram_clients import _block_identity, _BlockCache

BLOCK = 1024**2
READ_AHEAD = 32  # MiB per response, including currently requested windows.


class _ScratchBudget:
    """Global MiB reservations; acquire a response head before speculative work.

    Reservations include completed futures and the currently yielded block, and
    remain owned until the consumer resumes or closes. Cache retention is a
    separate budget. Waiting response heads take precedence over new read-ahead.
    """

    def __init__(self, maximum):
        self.limit = max(2, maximum // BLOCK)
        self.used = self.peak = self.waiters = 0
        self.changed = asyncio.Event()

    def try_acquire(self):
        if self.waiters or self.used >= self.limit:
            return False
        self.used += 1
        self.peak = max(self.peak, self.used)
        return True

    async def acquire(self):
        self.waiters += 1
        try:
            while self.used >= self.limit:
                self.changed.clear()
                await self.changed.wait()
            self.used += 1
            self.peak = max(self.peak, self.used)
        finally:
            self.waiters -= 1

    def release(self):
        if self.used <= 0:
            raise RuntimeError("Media scratch reservation underflow")
        self.used -= 1
        self.changed.set()

    def resize(self, maximum):
        self.limit = max(2, maximum // BLOCK)
        self.changed.set()


class _PayloadCache(_BlockCache):
    """Shared-flight cache whose protected segment survives one-time scans."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.protected = {}
        self.protected_fraction = 0.75

    def _hit(self, key):
        data = super()._hit(key)
        self.protected = {k: None for k in self.protected if k in self.entries}
        if data is not None:
            self.entries[key] = (self.clock() + self.ttl, data)
            self.protected.pop(key, None)
            self.protected[key] = None
            self._demote()
        return data

    def _demote(self):
        size = sum(
            len(self.entries[key][1]) for key in self.protected if key in self.entries
        )
        while self.protected and size > self.max_bytes * self.protected_fraction:
            key = next(iter(self.protected))
            self.protected.pop(key)
            size -= len(self.entries[key][1])

    def trim(self):
        self._demote()
        while self.entries and (
            self.bytes_used > self.max_bytes or len(self.entries) > self.max_entries
        ):
            key = next(
                (key for key in self.entries if key not in self.protected),
                next(iter(self.entries)),
            )
            _, data = self.entries.pop(key)
            self.bytes_used -= len(data)
            self.protected.pop(key, None)

    def _put(self, key, data):
        if not data or len(data) > self.max_bytes:
            return
        old = self.entries.pop(key, None)
        if old:
            self.bytes_used -= len(old[1])
        self.entries[key] = (self.clock() + self.ttl, data)
        self.bytes_used += len(data)
        # Evict old probation before the newly admitted payload, keeping hot bytes.
        self.trim()


def _effective_memory(configured):
    """Clamp retained bytes to a quarter of available host/container memory."""
    limits = [configured]
    try:
        fields = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text().splitlines()
        )
        limits.append(int(fields["MemAvailable"].split()[0]) * 1024 // 4)
    except (OSError, ValueError, KeyError):
        pass
    try:
        maximum = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if maximum != "max":
            current = int(Path("/sys/fs/cgroup/memory.current").read_text())
            limits.append(max(0, int(maximum) - current) // 4)
    except (OSError, ValueError):
        pass
    return max(0, min(limits))


class MediaReadCache:
    def __init__(self, directory=None, policy=None, *, clock=None):
        self.policy = policy or MediaCachePolicy()
        self._capacity_clock = clock or time.monotonic
        self._last_capacity_check = self._capacity_clock()
        self.deferred_writes = policy is not None
        self.effective_memory_bytes = _effective_memory(self.policy.memory_max_bytes)
        self.scratch = _ScratchBudget(_effective_memory(self.policy.scratch_max_bytes))
        self._active_streams = 0
        self.memory = _PayloadCache(
            max_bytes=max(1, self.effective_memory_bytes),
            max_entries=max(256, self.effective_memory_bytes // BLOCK + 32),
            ttl=self.policy.memory_ttl_seconds,
            read_slots=self.scratch.limit,
        )
        # This resizable gate is independent of retained payloads; every real
        # pooled load also owns one global scratch frame. Reconfigure the same
        # gate object so old waiters/releases cannot inflate a new semaphore.
        self.memory._slots = _ScratchBudget(self.scratch.limit * BLOCK)
        self.memory.max_bytes = self.effective_memory_bytes
        self.memory.protected_fraction = self.policy.protected_fraction
        self.directory = Path(directory) if directory is not None else None
        self.disk = (
            DiskMediaCache(
                self.directory,
                max_bytes=self.policy.disk_max_bytes,
                ttl=self.policy.ttl_seconds,
                min_free_bytes=self.policy.min_free_bytes,
                protected_fraction=self.policy.protected_fraction,
            )
            if directory is not None
            else None
        )
        self._demand_idle = asyncio.Event()
        self._demand_idle.set()
        self._background_slots = asyncio.Semaphore(2)
        self._foreground = {}
        self._active_keys = {}
        self._pending = {}
        self._hot_keys = set()
        self._pending_bytes = 0
        self._writer = None
        self._flush_lock = asyncio.Lock()
        self._index_lock = asyncio.Lock()
        self._generation = 0
        self._closed = False
        self._files = {}
        self._blocks = {}
        self._file_lookup = {}
        self._prune_lock = asyncio.Lock()
        self._hits = self._misses = 0
        self._index_dirty = False
        self._load_index()

    def _refresh_capacity(self, *, force=False):
        now = self._capacity_clock()
        if not force and now - self._last_capacity_check < 5:
            return
        self._last_capacity_check = now
        self.effective_memory_bytes = _effective_memory(self.policy.memory_max_bytes)
        maximum = _effective_memory(self.policy.scratch_max_bytes)
        self.scratch.resize(maximum)
        self.memory._slots.resize(maximum)
        self.memory.max_entries = max(256, self.effective_memory_bytes // BLOCK + 32)
        self._adjust_memory()

    def _load_index(self):
        if self.directory is None:
            return
        try:
            path = self.directory / "inventory.json"
            index_limit = max(16 * BLOCK, min(128 * BLOCK, self.disk.max_entries * 512))
            if path.is_symlink() or path.stat().st_size > index_limit:
                return
            value = json.loads(path.read_text())
            for row in value.get("files", []):
                if not (
                    isinstance(row, dict)
                    and isinstance(row.get("id"), str)
                    and isinstance(row.get("name"), str)
                    and type(row.get("message_id")) is int
                    and type(row.get("size")) is int
                    and row["size"] >= 0
                    and isinstance(row.get("channel_id"), (int, type(None)))
                    and isinstance(row.get("parts"), list)
                    and isinstance(row.get("_representations", {}), dict)
                ):
                    raise ValueError("Invalid disposable cache index")
                if row["parts"]:
                    from tgdrive.file_parts import validate_parts

                    row["parts"] = validate_parts(
                        row["parts"], size=row["size"], message_id=row["message_id"]
                    )
                self._files[row["id"]] = row
            for row in value.get("blocks", []):
                key = tuple(row["key"])
                if not (
                    len(key) in (6, 7)
                    and all(isinstance(v, (str, int, type(None))) for v in key)
                    and row["file_id"] in self._files
                    and type(row["start"]) is int
                    and type(row["end"]) is int
                    and 0
                    <= row["start"]
                    < row["end"]
                    <= self._files[row["file_id"]]["size"]
                ):
                    raise ValueError("Invalid disposable cache block index")
                self._blocks[key] = {
                    "file_id": row["file_id"],
                    "start": row["start"],
                    "end": row["end"],
                }
            for row in self._files.values():
                self._bind_file(row)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            RecursionError,
        ):
            self._files, self._blocks = {}, {}
            self._file_lookup = {}

    def _bind_file(self, row):
        parts = row["parts"] or [
            {"message_id": row["message_id"], "offset": 0, "size": row["size"]}
        ]
        for part in parts:
            self._file_lookup[(row["channel_id"], part["message_id"], part["size"])] = (
                row["id"],
                part["offset"],
            )

    def register_file(
        self, *, channel_id, message_id, size, name="", parts=None, representation=None
    ):
        value = (
            channel_id,
            message_id,
            size,
            [(p["message_id"], p["offset"], p["size"]) for p in (parts or [])],
        )
        if representation is not None:
            value += (representation,)
        identity = hashlib.sha256(repr(value).encode()).hexdigest()[:24]
        row = self._files.get(identity, {})
        self._files[identity] = {
            "id": identity,
            "name": name or row.get("name", ""),
            "channel_id": channel_id,
            "message_id": message_id,
            "size": size,
            "parts": parts or [],
            "last_access": time.time(),
            "_representations": row.get("_representations", {}),
        }
        self._bind_file(self._files[identity])
        if len(self._files) > max(1024, len(self._blocks)) + 64:
            owners = {block["file_id"] for block in self._blocks.values()}
            keep = owners | set(list(self._files)[-1024:])
            self._files = {key: row for key, row in self._files.items() if key in keep}
            self._file_lookup = {
                key: value
                for key, value in self._file_lookup.items()
                if value[0] in keep
            }
        self._index_dirty = True
        return identity

    def register_block(self, key, expected):
        channel, message_id = key[0], key[1]
        owner = None
        logical_offset = 0
        binding = self._file_lookup.get((channel, message_id, key[-2]))
        if binding is not None:
            owner = self._files.get(binding[0])
            logical_offset = binding[1]
        representation = list(key[2:-1])
        if owner is not None:
            previous = owner.get("_representations", {}).get(str(message_id))
            if previous is not None and previous != representation:
                identity = self.register_file(
                    channel_id=channel,
                    message_id=message_id,
                    size=key[-2],
                    name=owner["name"],
                    representation=representation,
                )
                owner, logical_offset = self._files[identity], 0
        if owner is None:
            identity = self.register_file(
                channel_id=channel, message_id=message_id, size=key[-2]
            )
            owner = self._files[identity]
        owner.setdefault("_representations", {})[str(message_id)] = representation
        self._blocks[key] = {
            "file_id": owner["id"],
            "start": logical_offset + key[-1] * BLOCK,
            "end": logical_offset + key[-1] * BLOCK + expected,
        }
        self._index_dirty = True

    async def get(self, key, expected, remote, *, background=False, retain_hot=False):
        self._refresh_capacity()
        bound = (
            self.memory.max_entries
            + (self.disk.max_entries if self.disk is not None else 0)
            + 128
        )
        if len(self._blocks) > bound:
            async with self._prune_lock:
                memory_keys = (
                    set(self.memory.entries)
                    | set(self._pending)
                    | set(self.memory.flights)
                    | set(self._active_keys)
                )
                disk_keys = (
                    set(await asyncio.to_thread(self.disk.ready, list(self._blocks)))
                    if self.disk is not None
                    else set()
                )
                self._blocks = {
                    key: value
                    for key, value in self._blocks.items()
                    if key in memory_keys or key in disk_keys
                }
        self.register_block(key, expected)
        self._active_keys[key] = self._active_keys.get(key, 0) + 1
        if retain_hot:
            self._hot_keys.add(key)
        if not background:
            self._foreground[key] = self._foreground.get(key, 0) + 1
            self._demand_idle.clear()

        async def load():
            if key in self._pending:
                return self._pending[key]
            if self.disk is not None:
                payload = await asyncio.to_thread(self.disk.read, key, expected)
                if payload is not None:
                    return payload
            generation = self._generation
            payload = await remote()
            if len(payload) != expected:
                raise OSError("Telegram read ended before the declared block length")
            if (
                generation == self._generation
                and self.disk is not None
                and self.policy.disk_max_bytes > 0
                and not self._closed
            ):
                budget = min(16 * BLOCK, self.effective_memory_bytes // 4)
                if (
                    self.deferred_writes
                    and self._pending_bytes + len(payload) <= budget
                ):
                    self._pending[key] = payload
                    self._pending_bytes += len(payload)
                    self._adjust_memory()
                    if self._writer is None or self._writer.done():
                        self._writer = asyncio.create_task(self._drain_writes())
                else:
                    await asyncio.to_thread(self.disk.write, key, payload)
                    if key in self._hot_keys:
                        await asyncio.to_thread(self.disk.promote, key)
                        self._hot_keys.discard(key)
                    self._index_dirty = True
            return payload

        generation = self._generation
        try:
            if self.memory._hit(key) is not None or key in self._pending:
                self._hits += 1
            else:
                self._misses += 1
            if background:
                # Reserve most shared fill slots for demand. Waiting speculative
                # work must never occupy all slots needed to unblock foreground.
                async with self._background_slots:
                    await self._demand_idle.wait()
                    payload = await self.memory.get(key, expected, load)
            else:
                payload = await self.memory.get(key, expected, load)
            if generation != self._generation:
                old = self.memory.entries.pop(key, None)
                if old:
                    self.memory.bytes_used -= len(old[1])
                self.memory.protected.pop(key, None)
            elif retain_hot:
                self.memory._hit(key)
                if key not in self._pending:
                    if self.disk is not None:
                        await asyncio.to_thread(self.disk.promote, key)
                    self._hot_keys.discard(key)
            return payload
        finally:
            active = self._active_keys.get(key, 1) - 1
            if active:
                self._active_keys[key] = active
            else:
                self._active_keys.pop(key, None)
            if key not in self._pending:
                self._hot_keys.discard(key)
            if not background:
                count = self._foreground.get(key, 1) - 1
                if count:
                    self._foreground[key] = count
                else:
                    self._foreground.pop(key, None)
                if not self._foreground:
                    self._demand_idle.set()

    def _adjust_memory(self):
        self.memory.max_bytes = max(
            0, self.effective_memory_bytes - self._pending_bytes
        )
        self.memory.trim()

    async def _drain_writes(self):
        while True:
            while self._pending:
                key = next(iter(self._pending))
                payload = self._pending[key]
                await asyncio.to_thread(self.disk.write, key, payload)
                if key in self._hot_keys:
                    await asyncio.to_thread(self.disk.promote, key)
                    self._hot_keys.discard(key)
                self._pending.pop(key, None)
                self._pending_bytes -= len(payload)
                self._adjust_memory()
            await self._persist_index()
            if not self._pending:
                return

    async def flush(self):
        async with self._flush_lock:
            await self._flush()

    async def _flush(self):
        if self._writer is not None:
            await asyncio.shield(self._writer)
        await self._persist_index()

    async def _persist_index(self):
        async with self._index_lock:
            await self._persist_index_locked()

    async def _persist_index_locked(self):
        if self.disk is not None and self._index_dirty:
            disk_keys = set(
                await asyncio.to_thread(self.disk.ready, list(self._blocks))
            )
            blocks = [
                {"key": key, **row}
                for key, row in self._blocks.items()
                if key in disk_keys
            ]
            owners = {row["file_id"] for row in blocks}
            value = {
                "files": [row for key, row in self._files.items() if key in owners],
                "blocks": blocks,
            }
            try:
                await asyncio.to_thread(self._write_index, value)
                self._index_dirty = False
            except OSError:
                pass

    def _write_index(self, value):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / "inventory.json"
        temporary = self.directory / (secrets.token_hex(16) + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, separators=(",", ":"))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    async def reconfigure(self, policy):
        await self.flush()
        self.policy = policy
        self.memory.ttl = policy.memory_ttl_seconds
        self.memory.protected_fraction = policy.protected_fraction
        self._refresh_capacity(force=True)
        if self.disk is not None:
            await asyncio.to_thread(
                self.disk.reconfigure,
                max_bytes=policy.disk_max_bytes,
                ttl=policy.ttl_seconds,
                min_free_bytes=policy.min_free_bytes,
                protected_fraction=policy.protected_fraction,
            )

    async def clear(self, file_id=None):
        self._generation += 1
        await self.flush()
        keys = [
            key
            for key, row in self._blocks.items()
            if file_id is None or row["file_id"] == file_id
        ]
        if self.disk is not None:
            await asyncio.to_thread(self.disk.clear, None if file_id is None else keys)
        for key in keys:
            old = self.memory.entries.pop(key, None)
            if old:
                self.memory.bytes_used -= len(old[1])
            self.memory.protected.pop(key, None)
            self._hot_keys.discard(key)
            self._blocks.pop(key, None)
        if file_id is None:
            self._files.clear()
            self._file_lookup.clear()
        else:
            self._files.pop(file_id, None)
            self._file_lookup = {
                key: value
                for key, value in self._file_lookup.items()
                if value[0] != file_id
            }
        self._index_dirty = True
        await self.flush()

    async def snapshot(self):
        self._refresh_capacity()
        self.memory._hit(())  # Expire idle RAM entries before reporting readiness.
        disk = (
            await asyncio.to_thread(self.disk.snapshot)
            if self.disk is not None
            else {"available": False, "bytes": 0, "max_bytes": 0}
        )
        disk_keys = (
            set(await asyncio.to_thread(self.disk.ready, list(self._blocks)))
            if self.disk is not None
            else set()
        )
        memory_keys = set(self.memory.entries) | set(self._pending)
        grouped = {}
        for key, block in self._blocks.items():
            memory_ranges, disk_ranges = grouped.setdefault(block["file_id"], ([], []))
            interval = (block["start"], block["end"])
            if key in memory_keys:
                memory_ranges.append(interval)
            if key in disk_keys:
                disk_ranges.append(interval)
        files = []
        for identity, row in self._files.items():
            memory_ranges, disk_ranges = grouped.get(identity, ([], []))
            ready = merge_ranges(memory_ranges + disk_ranges)
            if ready:
                files.append(
                    {
                        k: v
                        for k, v in row.items()
                        if k != "parts" and not k.startswith("_")
                    }
                    | {
                        "ready_ranges": ready,
                        "memory_ranges": merge_ranges(memory_ranges),
                        "disk_ranges": merge_ranges(disk_ranges),
                        "ready_bytes": sum(end - start for start, end in ready),
                        "complete": ready == [[0, row["size"]]],
                    }
                )
        # Missing/evicted blocks must not grow an unbounded metadata registry.
        live = (
            memory_keys | disk_keys | set(self.memory.flights) | set(self._active_keys)
        )
        self._blocks = {key: row for key, row in self._blocks.items() if key in live}
        return {
            "policy": self.policy.as_dict(),
            "memory": {
                "bytes": self.memory.bytes_used + self._pending_bytes,
                "max_bytes": self.policy.memory_max_bytes,
                "effective_max_bytes": self.effective_memory_bytes,
                "entries": len(memory_keys),
                "hits": self._hits,
                "misses": self._misses,
                "active_fills": self.memory._slots.used,
                "max_fills": self.memory._slots.limit,
            },
            "disk": disk,
            "files": files,
            "pending_writes": len(self._pending),
            "scratch": {
                "bytes": self.scratch.used * BLOCK,
                "max_bytes": self.scratch.limit * BLOCK,
                "configured_max_bytes": self.policy.scratch_max_bytes,
                "peak_bytes": self.scratch.peak * BLOCK,
                "waiting_heads": self.scratch.waiters,
                "active_streams": self._active_streams,
            },
        }

    async def close(self):
        self._closed = True
        await self.flush()


async def stream_pooled_media(
    pool: Any,
    client: Any,
    message: Any,
    *,
    channel: int,
    limit: int,
    offset: int,
    background: bool = False,
    authorize=None,
    request_id: str | None = None,
    retain_hot: bool = False,
) -> AsyncIterator[bytes]:
    """Authorization and initial message validation precede every cache read.

    Each worker binds the same document through its own account. Content keys
    include channel, message, immutable document/rendition, size and block index.
    Cached payloads are shared across clients; Telegram access hashes are not.
    """
    if limit <= 0:
        return
    if authorize is not None:
        await authorize()
    identity = _block_identity(message, offset)
    if identity is None or not callable(getattr(pool, "download_capacity", None)):
        iterator = client.stream_media(message, limit=limit, offset=offset)
        try:
            async for data in iterator:
                if authorize is not None:
                    await authorize()
                yield data
        finally:
            await iterator.aclose()
        return

    if pool._media_read_cache is None:
        pool._media_read_cache = MediaReadCache(pool.media_cache_dir)
    cache = pool._media_read_cache
    cache._refresh_capacity()
    source = getattr(message, "native", message)
    message_id = source.id
    quality = getattr(message, "variant_id", None)
    bindings = {id(client): message}
    locks: dict[int, asyncio.Lock] = {}

    async def bind(account):
        key = id(account)
        async with locks.setdefault(key, asyncio.Lock()):
            if key not in bindings:
                from tgdrive.media_metadata import get_message

                fresh = await get_message(account, channel, message_id)
                if fresh is None:
                    raise FileNotFoundError("Stored Telegram message was removed")
                if quality is not None:
                    fresh = account.select_media_variant(fresh, quality)
                if _block_identity(fresh, offset) != identity:
                    raise FileNotFoundError("Stored Telegram media changed")
                bindings[key] = fresh
            return bindings[key]

    async def fetch_remote(index):
        for attempt in range(3):
            kwargs = {"download": True, "allow_users": True}
            if background:
                kwargs["priority"] = "preload"
            if request_id is not None:
                kwargs["request_id"] = request_id
            async with pool.lease(**kwargs) as account:
                try:
                    bound = await bind(account)
                    options = {"limit": 1, "offset": index}
                    if getattr(account, "supports_uncached_media", False):
                        options["use_cache"] = False
                    iterator = account.stream_media(bound, **options)
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

    async def remote(index):
        from tgdrive.media_scheduler import media_priority

        token = media_priority.set("preload" if background else "demand")
        try:
            return await fetch_remote(index)
        finally:
            media_priority.reset(token)

    async def read(index):
        block = _block_identity(message, index)
        if block is None:
            raise OSError("Telegram block lies outside the validated media")
        key, expected = block
        return await cache.get(
            key,
            expected,
            lambda: remote(index),
            background=background,
            retain_hot=retain_hot,
        )

    pending: deque[asyncio.Task[bytes]] = deque()
    tasks: set[asyncio.Task[bytes]] = set()
    next_index, end = offset, offset + limit
    scratch = cache.scratch

    def schedule():
        nonlocal next_index
        try:
            task = asyncio.create_task(read(next_index))
        except BaseException:
            scratch.release()
            raise
        pending.append(task)
        tasks.add(task)
        next_index += 1

    maximum = (
        cache.policy.background_blocks if background else cache.policy.read_ahead_blocks
    )
    window = min(
        maximum,
        max(4, await pool.download_capacity() * 4),
        limit,
    )

    def refill():
        quota = min(window, max(1, scratch.limit // max(1, cache._active_streams)))
        while next_index < end and len(tasks) < quota and scratch.try_acquire():
            schedule()

    cache._active_streams += 1
    try:
        await scratch.acquire()
        schedule()
        refill()
        while pending:
            task = pending.popleft()
            data = await task
            if authorize is not None:
                await authorize()
            yield data
            tasks.discard(task)
            scratch.release()
            refill()
            if not pending and next_index < end:
                await scratch.acquire()
                schedule()
                refill()
    finally:
        for task in tasks:
            task.cancel()
        interrupted = False
        try:
            if tasks:
                cleanup = asyncio.gather(*tasks, return_exceptions=True)
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        interrupted = True
                cleanup.result()
        finally:
            for _ in tasks:
                scratch.release()
            cache._active_streams -= 1
        if interrupted:
            raise asyncio.CancelledError
