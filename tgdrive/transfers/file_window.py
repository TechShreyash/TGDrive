"""Bounded seekable staged-file views; never materialize multipart pieces."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path


class FileWindow(io.RawIOBase):
    def __init__(self, path: Path, offset: int, size: int, *, name: str | None = None):
        super().__init__()
        if type(offset) is not int or type(size) is not int or offset < 0 or size <= 0:
            raise ValueError("Invalid staged file window")
        self._source = path.open("rb")
        self._offset = offset
        self._size = size
        self._position = 0
        self._hashed = 0
        self._digest = hashlib.sha256()
        self._sequential = True
        self.name = name or path.name
        self._source.seek(offset)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._position

    def seek(self, offset, whence=io.SEEK_SET):
        position = (
            offset
            if whence == io.SEEK_SET
            else (
                self._position + offset
                if whence == io.SEEK_CUR
                else self._size + offset
            )
        )
        if (
            whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END)
            or not 0 <= position <= self._size
        ):
            raise ValueError("Seek outside staged file window")
        if position != self._position:
            if position == 0:
                self._digest = hashlib.sha256()
                self._hashed = 0
                self._sequential = True
            else:
                self._sequential = False
        self._position = position
        self._source.seek(self._offset + position)
        return position

    def read(self, size=-1):
        if self.closed:
            raise ValueError("I/O operation on closed file")
        remaining = self._size - self._position
        amount = remaining if size is None or size < 0 else min(size, remaining)
        data = self._source.read(amount)
        if self._position != self._hashed:
            self._sequential = False
        self._position += len(data)
        self._hashed += len(data)
        self._digest.update(data)
        return data

    def verify(self, expected_sha256):
        if not self._sequential or self._hashed != self._size:
            raise ValueError("Staged file window was not uploaded completely")
        if expected_sha256 is not None and self._digest.hexdigest() != expected_sha256:
            raise ValueError("Staged file changed before Telegram publication")

    def close(self):
        if not self.closed:
            self._source.close()
        super().close()
