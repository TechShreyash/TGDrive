"""Ordered, bounded read-ahead across healthy Telegram accounts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon.errors import FloodPremiumWaitError, FloodWaitError, RPCError

from tgdrive.media.media_cache import DiskMediaCache
from tgdrive.media.media_policy import MediaCachePolicy, merge_ranges
from tgdrive.media.media_scheduler import MediaPriorityScope
from tgdrive.telegram.clients import _block_identity, _BlockCache

BLOCK = 1024**2
READ_AHEAD = 32  # MiB per response, including currently requested windows.


class CachedSourceUnavailable(Exception):
    """A cache-only derivative must stop rather than fetch missing payloads."""


@dataclass(frozen=True)
class CachedSourceVersion:
    file_id: str
    revision: int
    generation: int

    @property
    def data_revision(self):
        # Registering metadata/HEAD alone must not invalidate a cached miss.
        return (self.file_id, self.revision) if self.revision else None


class _AdaptiveReadAhead:
    """Size a response pipeline from demonstrated delivery, not codec bitrate.

    Consumer advancement includes HTTP backpressure and waits for the next
    source block. Sampling over time avoids treating an immediate cache burst as
    an unlimited consumer. Source latency supplies the horizon needed to overlap
    Telegram reads; policy, account capacity and scratch remain separate caps.
    """

    def __init__(self, maximum, *, clock=None):
        self.maximum = max(1, maximum)
        self.window = min(2, self.maximum)
        self.clock = clock or time.monotonic
        self._sample_at = self.clock()
        self._last_consumed_at = self._sample_at
        self._sample_bytes = 0
        self._rate = None
        self._latency = None

    def observe_fetch(self, elapsed):
        elapsed = max(0.001, elapsed)
        self._latency = (
            elapsed if self._latency is None else self._latency * 0.75 + elapsed * 0.25
        )

    def consumed(self, size, *, consumer_seconds=None):
        self._sample_bytes += size
        now = self.clock()
        if consumer_seconds is None:
            consumer_seconds = now - self._last_consumed_at
        self._last_consumed_at = now
        elapsed = now - self._sample_at
        if elapsed < 0.1:
            return
        rate = self._sample_bytes / elapsed
        if consumer_seconds >= 0.1:
            # A paused send must not inherit the fast bytes accumulated just
            # before it. Source waits are measured separately by the caller.
            rate = min(rate, size / consumer_seconds)
        # Slow consumers reduce speculation promptly. Sustained faster delivery
        # must demonstrate itself over successive samples before using more RAM.
        self._rate = (
            rate
            if self._rate is None or rate < self._rate
            else min(self._rate * 2, self._rate * 0.5 + rate * 0.5)
        )
        horizon = max(0.25, 2 * (self._latency or 0))
        target = min(self.maximum, max(2, 1 + math.ceil(self._rate * horizon / BLOCK)))
        self.window = min(target, max(2, self.window * 2))
        self._sample_bytes = 0
        self._sample_at = now

    @property
    def idle_seconds(self):
        return max(1.0, 3 * (self._latency or 0))

    def idle(self):
        # No consumer progress: retain only the yielded head. Its speculative
        # neighbours are disposable; a later advancement starts a fresh probe.
        self.window = 1
        self._rate = None
        self._sample_at = self.clock()
        self._last_consumed_at = self._sample_at
        self._sample_bytes = 0


class _ScratchBudget:
    """Global MiB reservations with FIFO heads and reclaimable read-ahead.

    An awaited or yielded head stays owned until its consumer advances or closes.
    Speculative frames can be reclaimed from a suspended reader when a new head
    needs space. In-flight frames release only after their cancellation cleanup.
    """

    def __init__(self, maximum):
        self.limit = max(2, maximum // BLOCK)
        self.used = self.peak = self.waiters = 0
        self.changed = asyncio.Event()
        self._heads = []
        self._streams = set()

    def try_acquire(self):
        if self.waiters or self.used >= self.limit:
            return False
        self.used += 1
        self.peak = max(self.peak, self.used)
        return True

    async def acquire(self):
        ticket = object()
        self._heads.append(ticket)
        self.waiters += 1
        try:
            while True:
                self.changed.clear()
                if self._heads[0] is ticket:
                    if self.used >= self.limit:
                        # Prefer the largest speculative window and reclaim its
                        # farthest block. Never revoke its awaited/yielded head.
                        streams = sorted(
                            self._streams,
                            key=lambda stream: stream.speculative_count,
                            reverse=True,
                        )
                        for stream in streams:
                            if stream.reclaim_one():
                                break
                    if self.used < self.limit:
                        self.used += 1
                        self.peak = max(self.peak, self.used)
                        return
                await self.changed.wait()
        finally:
            self._heads.remove(ticket)
            self.waiters -= 1
            self.changed.set()

    def release(self):
        if self.used <= 0:
            raise RuntimeError("Media scratch reservation underflow")
        self.used -= 1
        self.changed.set()

    def resize(self, maximum):
        self.limit = max(2, maximum // BLOCK)
        self.changed.set()


class _StreamFrames:
    """Own stream tasks independently of a suspended generator's consumer."""

    def __init__(self, scratch, read):
        self.scratch, self.read = scratch, read
        self.frames = {}
        self.retiring = set()
        scratch._streams.add(self)

    @property
    def speculative_count(self):
        return sum(not frame.head for frame in self.frames.values())

    def schedule(self, index, *, head=False):
        try:
            task = asyncio.create_task(self.read(index))
        except BaseException:
            self.scratch.release()
            raise
        frame = _StreamFrame(self, index, task, head)
        self.frames[index] = frame
        return frame

    def reclaim_one(self):
        for index in sorted(self.frames, reverse=True):
            frame = self.frames[index]
            if not frame.head:
                self.retire(frame)
                return True
        return False

    def trim(self, index, quota):
        for following in sorted(self.frames, reverse=True):
            frame = self.frames[following]
            if not frame.head and (
                following >= index + quota or len(self.frames) > quota
            ):
                self.retire(frame)

    def release(self, frame):
        if self.frames.get(frame.index) is frame:
            del self.frames[frame.index]
        frame.task = None
        if not frame.released:
            frame.released = True
            self.scratch.release()

    def retire(self, frame):
        self.frames.pop(frame.index, None)
        task = frame.task
        if task.done():
            frame.finish(task)
        else:
            self.retiring.add(frame)
            task.add_done_callback(frame.finish)
            task.cancel()

    async def close(self):
        self.scratch._streams.discard(self)
        for frame in list(self.frames.values()):
            self.retire(frame)
        interrupted = False
        if self.retiring:
            cleanup = asyncio.gather(
                *(frame.task for frame in self.retiring), return_exceptions=True
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            cleanup.result()
        if interrupted:
            raise asyncio.CancelledError


class _StreamFrame:
    def __init__(self, owner, index, task, head):
        self.owner, self.index, self.task = owner, index, task
        self.head, self.released = head, False

    def finish(self, task):
        # Consume speculative errors without making them escape a different
        # request. The original reader retries that offset if it needs it later.
        with suppress(BaseException):
            task.result()
        self.owner.retiring.discard(self)
        self.owner.release(self)


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
    except OSError, ValueError, KeyError:
        pass
    try:
        maximum = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if maximum != "max":
            current = int(Path("/sys/fs/cgroup/memory.current").read_text())
            limits.append(max(0, int(maximum) - current) // 4)
    except OSError, ValueError:
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
        self._fill_priorities = {}
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
        self._data_revision = max(
            (row.get("_data_revision", 0) for row in self._files.values()), default=0
        )

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
                    from tgdrive.storage.file_parts import validate_parts

                    row["parts"] = validate_parts(
                        row["parts"], size=row["size"], message_id=row["message_id"]
                    )
                downloaded = row.get("last_download", 0)
                row["last_download"] = (
                    downloaded
                    if type(downloaded) in (int, float)
                    and math.isfinite(downloaded)
                    and downloaded >= 0
                    else 0
                )
                revision = row.get("_data_revision", 0)
                row["_data_revision"] = (
                    revision if type(revision) is int and revision >= 0 else 0
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

    @staticmethod
    def _file_identity(channel_id, message_id, size, parts, representation=None):
        value = (
            channel_id,
            message_id,
            size,
            [(p["message_id"], p["offset"], p["size"]) for p in (parts or [])],
        )
        if representation is not None:
            value += (representation,)
        return hashlib.sha256(repr(value).encode()).hexdigest()[:24]

    def register_file(
        self, *, channel_id, message_id, size, name="", parts=None, representation=None
    ):
        identity = self._file_identity(
            channel_id, message_id, size, parts, representation
        )
        row = self._files.get(identity, {})
        self._files[identity] = {
            "id": identity,
            "name": name or row.get("name", ""),
            "channel_id": channel_id,
            "message_id": message_id,
            "size": size,
            "parts": parts or [],
            "last_access": time.time(),
            "last_download": row.get("last_download", 0),
            "_data_revision": row.get("_data_revision", 0),
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

    def cached_source_version(self, *, channel_id, message_id, size, parts=None):
        identity = self._file_identity(channel_id, message_id, size, parts)
        row = self._files.get(identity)
        if row is None:
            return None
        return CachedSourceVersion(
            identity, row.get("_data_revision", 0), self._generation
        )

    async def cached_file_blocks(
        self,
        *,
        channel_id,
        message_id,
        size,
        parts=None,
        version,
        max_bytes,
        authorize=None,
    ):
        """Yield a complete original from retained blocks; never start/join origin I/O.

        Readiness is checked before the first yield, then actual disk reads verify
        every digest. Eviction, clearing or source replacement aborts the copy.
        """
        from tgdrive.storage.file_parts import validate_parts

        def current():
            if authorize is not None:
                authorize()
            if (
                self._closed
                or version is None
                or self.cached_source_version(
                    channel_id=channel_id,
                    message_id=message_id,
                    size=size,
                    parts=parts,
                )
                != version
            ):
                raise CachedSourceUnavailable

        current()
        if not 0 < size <= max_bytes:
            raise CachedSourceUnavailable
        documents = (
            validate_parts(parts, size=size, message_id=message_id)
            if parts
            else [{"message_id": message_id, "offset": 0, "size": size}]
        )
        row = self._files[version.file_id]
        plan = []
        for part in documents:
            representation = row.get("_representations", {}).get(
                str(part["message_id"])
            )
            if (
                not isinstance(representation, (list, tuple))
                or len(representation) not in (3, 4)
                or not all(
                    isinstance(value, (str, int, type(None)))
                    for value in representation
                )
                or representation[-1] != part["size"]
            ):
                raise CachedSourceUnavailable
            blocks = []
            for index in range(math.ceil(part["size"] / BLOCK)):
                expected = min(BLOCK, part["size"] - index * BLOCK)
                key = (channel_id, part["message_id"], *representation, index)
                start = part["offset"] + index * BLOCK
                if self._blocks.get(key) != {
                    "file_id": version.file_id,
                    "start": start,
                    "end": start + expected,
                }:
                    raise CachedSourceUnavailable
                blocks.append((key, expected))
            plan.append((part, blocks))
        keys = [key for _, blocks in plan for key, _ in blocks]
        disk_keys = (
            set(await asyncio.to_thread(self.disk.ready, keys))
            if self.disk is not None
            else set()
        )
        current()
        # Expire RAM hits here as well; an old inventory entry is not proof that
        # the complete original is still retained.
        if any(
            key not in self._pending
            and self.memory._hit(key) is None
            and key not in disk_keys
            for key in keys
        ):
            raise CachedSourceUnavailable
        for part, blocks in plan:
            checksum = hashlib.sha256()
            for key, expected in blocks:
                current()
                payload = self._pending.get(key)
                if payload is None:
                    payload = self.memory._hit(key)
                if payload is None and self.disk is not None:
                    payload = await asyncio.to_thread(self.disk.read, key, expected)
                current()
                if payload is None or len(payload) != expected:
                    raise CachedSourceUnavailable
                checksum.update(payload)
                yield payload
                current()
            if part.get("sha256") and checksum.hexdigest() != part["sha256"]:
                raise CachedSourceUnavailable

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
        priority_scope = self._fill_priorities.setdefault(
            key, MediaPriorityScope("preload" if background else "demand")
        )
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
            if generation == self._generation:
                block = self._blocks.get(key)
                owner = self._files.get(block["file_id"]) if block else None
                if owner is not None:
                    owner["last_download"] = time.time()
                    self._data_revision += 1
                    owner["_data_revision"] = self._data_revision
                    self._index_dirty = True
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
            if not background:
                await priority_scope.promote()
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
                self._fill_priorities.pop(key, None)
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
            # Clear before yielding so registrations/downloads arriving during
            # the write keep their own dirty flag and get a subsequent snapshot.
            self._index_dirty = False
            written = False
            try:
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
                await asyncio.to_thread(self._write_index, value)
                written = True
            except OSError:
                pass
            finally:
                if not written:
                    self._index_dirty = True

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
        files.sort(key=lambda row: (-row.get("last_download", 0), row["id"]))
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

    request_id = request_id or f"read-{secrets.token_urlsafe(12)}"
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
                from tgdrive.media.media_metadata import get_message

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
        from tgdrive.media.media_scheduler import media_priority

        for attempt in range(3):
            kwargs = {
                "download": True,
                "allow_users": True,
                "prefer_premium": True,
                "priority": media_priority.get(),
            }
            if request_id is not None:
                kwargs["request_id"] = request_id
            async with pool.lease(**kwargs) as account:
                try:
                    bound = await bind(account)
                    options = {"limit": 1, "offset": index}
                    if getattr(account, "supports_uncached_media", False):
                        options["use_cache"] = False
                    iterator = account.stream_media(bound, **options)
                    started = controller.clock()
                    try:
                        data = await anext(iterator, b"")
                        block = _block_identity(bound, index)
                        if block is None or len(data) != block[1]:
                            # Validate inside account failover, before the cache
                            # sees any bytes. Empty EOF is a failed read too.
                            raise OSError(
                                "Telegram read ended before the declared block length"
                            )
                        controller.observe_fetch(controller.clock() - started)
                        return data
                    finally:
                        await iterator.aclose()
                except (FloodWaitError, FloodPremiumWaitError) as error:
                    await pool.defer_client(
                        account.name, time.time() + max(1, error.seconds)
                    )
                    if attempt == 2:
                        raise
                except (OSError, RPCError) as error:
                    problem = await pool.report_failure(account, error)
                    if attempt == 2:
                        if problem is not None:
                            raise problem from None
                        raise
                    # A temporary failure should not monopolize the next window.
                    bindings.pop(id(account), None)
                    await pool.defer_client(account.name, time.time() + 2)
        raise OSError("No Telegram account could read this block")

    async def remote(index):
        from tgdrive.media.media_scheduler import media_priority

        block = _block_identity(message, index)
        scope = cache._fill_priorities.get(block[0]) if block is not None else None
        token = media_priority.set(scope or ("preload" if background else "demand"))
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

    end = offset + limit
    scratch = cache.scratch
    maximum = (
        cache.policy.background_blocks if background else cache.policy.read_ahead_blocks
    )
    controller = _AdaptiveReadAhead(
        min(
            maximum,
            max(4, await pool.download_capacity() * 4),
            limit,
        )
    )
    frames = _StreamFrames(scratch, read)

    def refill(index):
        quota = min(
            controller.window, max(1, scratch.limit // max(1, cache._active_streams))
        )
        frames.trim(index, quota)
        for following in range(index + 1, min(end, index + quota)):
            if len(frames.frames) >= quota:
                break
            if following in frames.frames:
                continue
            if not scratch.try_acquire():
                break
            frames.schedule(following)

    cache._active_streams += 1
    data = frame = None
    idle_timer = None

    def idle(index):
        controller.idle()
        frames.trim(index, 1)

    try:
        for index in range(offset, end):
            frame = frames.frames.get(index)
            if frame is None:
                await scratch.acquire()
                frame = frames.schedule(index, head=True)
            else:
                frame.head = True
            refill(index)
            data = await frame.task
            if authorize is not None:
                await authorize()
            idle_timer = asyncio.get_running_loop().call_later(
                controller.idle_seconds, idle, index
            )
            yielded_at = controller.clock()
            yield data
            idle_timer.cancel()
            idle_timer = None
            controller.consumed(
                len(data), consumer_seconds=controller.clock() - yielded_at
            )
            # A suspended consumer owns exactly its yielded head, plus only
            # reclaimable read-ahead. Drop our payload references before freeing
            # the reservation so a new head cannot exceed the aggregate bound.
            data = None
            frames.release(frame)
            frame = None
    finally:
        if idle_timer is not None:
            idle_timer.cancel()
        del data, frame
        try:
            await frames.close()
        finally:
            cache._active_streams -= 1
