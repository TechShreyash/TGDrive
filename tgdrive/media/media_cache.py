"""Private bounded disk cache for immutable, already-authorized media blocks."""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path


class DiskMediaCache:
    """Disposable bytes with scan-resistant probation/protected retention."""

    def __init__(
        self,
        directory: Path,
        *,
        max_bytes=512 * 1024**2,
        ttl=86400,
        max_entries=None,
        min_free_bytes=0,
        protected_fraction=0.75,
    ):
        self.directory = Path(directory)
        self.max_bytes, self.ttl = max_bytes, ttl
        self.min_free_bytes = min_free_bytes
        self.protected_fraction = protected_fraction
        self._explicit_max_entries = max_entries
        self.max_entries = max_entries or self._entry_limit(max_bytes)
        self._lock = threading.Lock()
        self._entries = None
        self._protected = OrderedDict()
        self._protected_bytes = 0
        self._probation = OrderedDict()
        self._size = 0
        self._hits = self._misses = self._evictions = self._write_failures = 0

    @staticmethod
    def _entry_limit(budget):
        return max(1024, math.ceil(budget / 1024**2) + 32)

    @staticmethod
    def _key(identity):
        return hashlib.sha256(repr(identity).encode()).hexdigest()

    def _remove(self, key):
        if self._entries is not None:
            item = self._entries.pop(key, None)
            if item:
                self._size -= item[0]
        if key in self._protected:
            self._protected.pop(key)
            self._protected_bytes -= item[0] if item else 0
        self._probation.pop(key, None)
        (self.directory / (key + ".block")).unlink(missing_ok=True)

    def _prepare(self):
        if self._entries is not None:
            return
        if self.directory.is_symlink():
            raise OSError("Media cache directory must not be a symbolic link")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        rows = []
        now = time.time()
        for path in self.directory.iterdir():
            if path.suffix == ".tmp":
                path.unlink(missing_ok=True)
            elif path.suffix == ".block":
                stat = path.lstat()
                if path.is_symlink() or stat.st_mtime + self.ttl <= now:
                    path.unlink(missing_ok=True)
                else:
                    rows.append((path.stem, stat.st_size, stat.st_mtime))
        self._entries = OrderedDict(
            (key, (size, touched))
            for key, size, touched in sorted(rows, key=lambda row: row[2])
        )
        self._probation = OrderedDict.fromkeys(self._entries)
        self._size = sum(size for size, _ in self._entries.values())
        self._trim()

    def _demote(self):
        ceiling = self.max_bytes * self.protected_fraction
        while self._protected and self._protected_bytes > ceiling:
            key, _ = self._protected.popitem(last=False)
            self._protected_bytes -= self._entries[key][0]
            self._probation[key] = None

    def _victim(self):
        return (
            next(iter(self._probation))
            if self._probation
            else next(iter(self._protected))
        )

    def _trim(self, incoming=0, incoming_entries=0):
        now = time.time()
        while self._entries:
            key = next(iter(self._entries))
            _, touched = self._entries[key]
            if touched + self.ttl <= now:
                self._remove(key)
            else:
                break
        self._demote()
        while self._entries and (
            self._size + incoming > self.max_bytes
            or len(self._entries) + incoming_entries > self.max_entries
        ):
            self._remove(self._victim())
            self._evictions += 1

    def read(self, identity, expected):
        try:
            with self._lock:
                self._prepare()
                key = self._key(identity)
                item = self._entries.get(key)
                if item is None:
                    self._misses += 1
                    return None
                path = self.directory / (key + ".block")
                if (
                    item[0] != expected + 32
                    or item[1] + self.ttl <= time.time()
                    or path.is_symlink()
                ):
                    self._remove(key)
                    self._misses += 1
                    return None
                with path.open("rb") as handle:
                    payload = handle.read(expected + 33)
                digest, data = payload[:32], payload[32:]
                if len(data) != expected or hashlib.sha256(data).digest() != digest:
                    self._remove(key)
                    self._misses += 1
                    return None
                now = time.time()
                self._entries[key] = (item[0], now)
                self._entries.move_to_end(key)
                if key not in self._protected:
                    self._protected_bytes += item[0]
                self._protected[key] = None
                self._probation.pop(key, None)
                self._protected.move_to_end(key)
                self._demote()
                os.utime(path, (now, now), follow_symlinks=False)
                self._hits += 1
                return data
        except OSError:
            return None

    def write(self, identity, data):
        if not data or len(data) + 32 > self.max_bytes:
            return False
        temporary = None
        try:
            with self._lock:
                self._prepare()
                key = self._key(identity)
                self._remove(key)
                size = len(data) + 32
                self._trim(size, 1)
                while (
                    self._entries
                    and shutil.disk_usage(self.directory).free
                    < self.min_free_bytes + size
                ):
                    self._remove(self._victim())
                    self._evictions += 1
                if shutil.disk_usage(self.directory).free < self.min_free_bytes + size:
                    return False
                temporary = self.directory / (secrets.token_hex(16) + ".tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(hashlib.sha256(data).digest() + data)
                temporary.replace(self.directory / (key + ".block"))
                self._entries[key] = (size, time.time())
                self._probation[key] = None
                self._size += size
                return True
        except OSError:
            self._write_failures += 1
            return False
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def reconfigure(self, *, max_bytes, ttl, min_free_bytes, protected_fraction):
        with self._lock:
            self.max_bytes, self.ttl = max_bytes, ttl
            self.min_free_bytes, self.protected_fraction = (
                min_free_bytes,
                protected_fraction,
            )
            self.max_entries = self._explicit_max_entries or self._entry_limit(
                max_bytes
            )
            try:
                self._prepare()
                self._trim()
            except OSError:
                pass

    def promote(self, identity):
        """Explicit owner preparation counts as reuse, after a verified fill."""
        with self._lock:
            try:
                self._prepare()
                key = self._key(identity)
                item = self._entries.get(key)
                if item is not None:
                    if key not in self._protected:
                        self._protected_bytes += item[0]
                    self._protected[key] = None
                    self._protected.move_to_end(key)
                    self._probation.pop(key, None)
                    self._demote()
            except OSError:
                pass

    def clear(self, identities=None):
        with self._lock:
            try:
                self._prepare()
                keys = (
                    list(self._entries)
                    if identities is None
                    else [self._key(identity) for identity in identities]
                )
                for key in keys:
                    self._remove(key)
            except OSError:
                pass

    def ready(self, identities):
        """Inventory only; actual reads still verify length and checksum."""
        with self._lock:
            try:
                self._prepare()
                self._trim()
                return [
                    identity
                    for identity in identities
                    if self._key(identity) in self._entries
                    and (self.directory / (self._key(identity) + ".block")).is_file()
                ]
            except OSError:
                return []

    def snapshot(self):
        with self._lock:
            available = True
            try:
                self._prepare()
                self._trim()
                free = shutil.disk_usage(self.directory).free
            except OSError:
                available, free = False, None
            return {
                "available": available,
                "bytes": self._size,
                "max_bytes": self.max_bytes,
                "entries": len(self._entries or ()),
                "max_entries": self.max_entries,
                "protected_entries": len(self._protected),
                "free_bytes": free,
                "min_free_bytes": self.min_free_bytes,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "write_failures": self._write_failures,
            }
