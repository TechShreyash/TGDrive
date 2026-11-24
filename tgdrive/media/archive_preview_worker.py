"""List ZIP contents in an isolated process without extracting files."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import struct
import sys
from collections import OrderedDict
from typing import Any
from zipfile import ZipFile

BLOCK = 1024 * 1024
MAX_READ = 32 * BLOCK
MAX_ENTRIES = 10000


class ZipPreviewError(ValueError):
    pass


def safe_name(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 1024
        or name.startswith("/")
        or "\\" in name
        or any(ord(c) < 32 for c in name)
    ):
        raise ZipPreviewError("This ZIP contains an invalid resource path.")
    if any(p in {"..", "."} for p in name.split("/")):
        raise ZipPreviewError("This ZIP contains an unsafe resource path.")
    return name


class RemoteFile(io.RawIOBase):
    def __init__(self, size, fetch, *, maximum=MAX_READ):
        self.size, self.fetch, self.pos = size, fetch, 0
        self.maximum = maximum
        self.cache = OrderedDict()
        self.used = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        value = (
            offset
            if whence == 0
            else self.pos + offset
            if whence == 1
            else self.size + offset
            if whence == 2
            else -1
        )
        if not 0 <= value <= self.size:
            raise ZipPreviewError("The ZIP requests an invalid byte range.")
        self.pos = value
        return value

    def read(self, amount=-1):
        if amount is None or amount < 0:
            amount = self.size - self.pos
        if amount > self.maximum:
            raise ZipPreviewError(
                "This ZIP needs too much data for a selective preview. "
                "Download it instead."
            )
        end = min(self.size, self.pos + amount)
        output = bytearray()
        while self.pos < end:
            block = self.pos // BLOCK
            if block not in self.cache:
                count = min(BLOCK, self.size - block * BLOCK)
                if self.used + count > self.maximum:
                    raise ZipPreviewError(
                        f"This archive needs more than {self.maximum // BLOCK} MiB "
                        "to list its contents. "
                        "Download it instead."
                    )
                data = self.fetch(block * BLOCK, count)
                if len(data) != count:
                    raise ZipPreviewError("The ZIP ended before its declared length.")
                self.used += count
                self.cache[block] = data
                while len(self.cache) > 4:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(block)
            chunk = self.cache[block][
                self.pos % BLOCK : min(BLOCK, end - block * BLOCK)
            ]
            output.extend(chunk)
            self.pos += len(chunk)
        return bytes(output)

    def readinto(self, target):
        data = self.read(len(target))
        target[: len(data)] = data
        return len(data)


def open_zip(source):
    # Bound the directory before ZipFile allocates one object per entry.
    tail_size = min(source.size, 65557)
    source.seek(source.size - tail_size)
    tail = source.read(tail_size)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        raise ZipPreviewError("This is not a readable ZIP archive.")
    _, disk, cd_disk, disk_count, count, length, offset, comment = struct.unpack_from(
        "<4s4H2LH", tail, at
    )
    if at + 22 + comment != len(tail) or disk or cd_disk or disk_count != count:
        raise ZipPreviewError("Split-volume ZIP archives are not supported.")
    end = source.size - tail_size + at
    if count == 65535 or length == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        position = source.size - tail_size + at
        if position < 20:
            raise ZipPreviewError("Invalid ZIP64 book.")
        source.seek(position - 20)
        locator = source.read(20)
        signature, disk, zip64_at, disks = struct.unpack("<4sLQL", locator)
        if signature != b"PK\x06\x07" or disk or disks != 1:
            raise ZipPreviewError("Split-volume ZIP archives are not supported.")
        source.seek(zip64_at)
        record = struct.unpack("<4sQ2H2L4Q", source.read(56))
        if (
            record[0] != b"PK\x06\x06"
            or record[4]
            or record[5]
            or record[6] != record[7]
        ):
            raise ZipPreviewError("Invalid ZIP64 directory.")
        count, length, offset = record[7:10]
        end = zip64_at
    if count > MAX_ENTRIES or length > 2 * BLOCK or offset + length > end:
        raise ZipPreviewError(
            "This ZIP has too many resources for the preview. Download it instead."
        )
    source.seek(offset)
    directory = source.read(length)
    position = actual = 0
    while position < len(directory):
        if (
            position + 46 > len(directory)
            or directory[position : position + 4] != b"PK\x01\x02"
        ):
            raise ZipPreviewError("Invalid ZIP archive directory.")
        name, extra, comment = struct.unpack_from("<3H", directory, position + 28)
        position += 46 + name + extra + comment
        actual += 1
        if actual > MAX_ENTRIES or position > len(directory):
            raise ZipPreviewError(
                "The ZIP archive directory exceeds its resource limits."
            )
    if actual != count:
        raise ZipPreviewError("The ZIP archive directory entry count is inconsistent.")
    archive = ZipFile(source)
    names = set()
    for entry in archive.infolist():
        safe_name(entry.filename.rstrip("/"))
        if "//" in entry.filename:
            archive.close()
            raise ZipPreviewError("This ZIP contains an invalid directory path.")
        if entry.is_dir():
            continue
        safe_name(entry.filename)
        if entry.filename in names:
            archive.close()
            raise ZipPreviewError("This ZIP contains duplicate file entries.")
        names.add(entry.filename)
    return archive


def list_zip(source, entry=None) -> dict[str, Any]:
    if entry is not None:
        raise ZipPreviewError("ZIP previews list contents without extracting files.")
    with open_zip(source) as archive:
        return {
            "kind": "archive",
            "entries": [
                {
                    "name": item.filename,
                    "directory": item.is_dir(),
                    "size": item.file_size,
                    "encrypted": bool(item.flag_bits & 1),
                }
                for item in archive.infolist()
            ],
        }


def run():
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (384 * BLOCK, 384 * BLOCK))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except ImportError:
        pass
    # Under memory pressure the kernel kills this worker, not TGDrive.
    with contextlib.suppress(OSError), open("/proc/self/oom_score_adj", "w") as oom:
        oom.write("1000")
    request = json.loads(sys.stdin.readline())

    def fetch(offset, length):
        print(json.dumps({"read": [offset, length]}), flush=True)
        reply = json.loads(sys.stdin.readline())
        return base64.b64decode(reply["data"], validate=True)

    try:
        result = list_zip(RemoteFile(request["size"], fetch), request.get("entry"))
        print(json.dumps({"result": result}), flush=True)
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, ZipPreviewError)
            else (
                "This ZIP is damaged, encrypted or uses an unsupported feature. "
                "Download it to open in an archive app."
            )
        )
        print(json.dumps({"error": message[:500]}), flush=True)


if __name__ == "__main__":
    run()
