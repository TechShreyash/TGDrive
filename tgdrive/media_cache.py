"""Private bounded disk cache for immutable, already-authorized media blocks."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path


class DiskMediaCache:
    """Disposable payloads only; no credentials, access decisions or catalog data."""

    def __init__(
        self,
        directory: Path,
        *,
        max_bytes: int = 512 * 1024**2,
        ttl: float = 24 * 60 * 60,
        max_entries: int = 1024,
    ) -> None:
        self.directory = directory
        self.max_bytes, self.ttl = max_bytes, ttl
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[int, float]] | None = None
        self._size = 0

    @staticmethod
    def _key(identity: tuple) -> str:
        return hashlib.sha256(repr(identity).encode()).hexdigest()

    def _remove(self, key: str) -> None:
        if self._entries is not None:
            item = self._entries.pop(key, None)
            if item:
                self._size -= item[0]
        (self.directory / (key + ".block")).unlink(missing_ok=True)

    def _prepare(self) -> None:
        if self._entries is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        rows = []
        now = time.time()
        for path in self.directory.iterdir():
            if path.suffix == ".tmp":
                path.unlink(missing_ok=True)
            elif path.suffix == ".block":
                stat = path.stat()
                if path.is_symlink() or stat.st_mtime + self.ttl <= now:
                    path.unlink(missing_ok=True)
                else:
                    rows.append((path.stem, stat.st_size, stat.st_mtime))
        self._entries = OrderedDict(
            (key, (size, created))
            for key, size, created in sorted(rows, key=lambda row: row[2])
        )
        self._size = sum(item[0] for item in self._entries.values())
        self._trim()

    def _trim(self, incoming: int = 0) -> None:
        assert self._entries is not None
        now = time.time()
        for key, (_, created) in list(self._entries.items()):
            if created + self.ttl <= now:
                self._remove(key)
        while self._entries and (
            self._size + incoming > self.max_bytes
            or len(self._entries) >= self.max_entries
        ):
            self._remove(next(iter(self._entries)))

    def read(self, identity: tuple, expected: int) -> bytes | None:
        try:
            with self._lock:
                self._prepare()
                key = self._key(identity)
                assert self._entries is not None
                item = self._entries.get(key)
                if item is None:
                    return None
                path = self.directory / (key + ".block")
                if (
                    item[0] != expected + 32
                    or item[1] + self.ttl <= time.time()
                    or path.is_symlink()
                ):
                    self._remove(key)
                    return None
                with path.open("rb") as handle:
                    payload = handle.read(expected + 33)
                digest, data = payload[:32], payload[32:]
                if len(data) != expected or hashlib.sha256(data).digest() != digest:
                    self._remove(key)
                    return None
                self._entries.move_to_end(key)
                return data
        except OSError:
            return None  # Cache availability never determines media availability.

    def write(self, identity: tuple, data: bytes) -> None:
        if not data or len(data) + 32 > self.max_bytes:
            return
        temporary = None
        try:
            with self._lock:
                self._prepare()
                key = self._key(identity)
                self._remove(key)
                size = len(data) + 32
                self._trim(size)
                temporary = self.directory / (secrets.token_hex(16) + ".tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(hashlib.sha256(data).digest() + data)
                temporary.replace(self.directory / (key + ".block"))
                assert self._entries is not None
                self._entries[key] = (size, time.time())
                self._size += size
        except OSError:
            pass
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
