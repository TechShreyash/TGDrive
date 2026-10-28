"""Isolated archive decoding; only numeric staging files are written here."""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import sys
import unicodedata
from contextlib import contextmanager
from pathlib import Path

BLOCK = 1024**2
MAX_ENTRIES = 1000
MAX_DEPTH = 64
MAX_MANIFEST_BYTES = 4 * BLOCK
MAX_MEMORY_BYTES = 512 * BLOCK
MAX_CPU_SECONDS = 600
TAR_METADATA_BYTES = 8 * BLOCK + MAX_ENTRIES * 1024
FORMATS = {
    ".tar.gz": ("tar", "gzip"),
    ".tgz": ("tar", "gzip"),
    ".tar.bz2": ("tar", "bzip2"),
    ".tbz2": ("tar", "bzip2"),
    ".tbz": ("tar", "bzip2"),
    ".tar.xz": ("tar", "xz"),
    ".txz": ("tar", "xz"),
    ".tar.zst": ("tar", "zstd"),
    ".tzst": ("tar", "zstd"),
    ".zip": ("zip", None),
    ".7z": ("7zip", None),
    ".rar": ("rar", None),
    ".tar": ("tar", None),
    ".gz": ("raw", "gzip"),
    ".bz2": ("raw", "bzip2"),
    ".xz": ("raw", "xz"),
    ".zst": ("raw", "zstd"),
}


def archive_format(name):
    for suffix, selected in FORMATS.items():
        if name.lower().endswith(suffix):
            return suffix, selected
    raise ValueError("Use ZIP, 7z, RAR, TAR or gzip/bzip2/xz/Zstandard archives.")


class WorkerError(ValueError):
    pass


@contextmanager
def strict_native_warnings():
    class FirstWarning(logging.Handler):
        warning = None

        def emit(self, record):
            if self.warning is None:
                self.warning = record.getMessage()[:300]

    logger = logging.getLogger("libarchive")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    handler = FirstWarning(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        yield
        if handler.warning is not None:
            raise WorkerError("Archive decoding reported a warning: " + handler.warning)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


class Manifest:
    def __init__(self, limit, budget):
        self.limit, self.budget = limit, budget
        self.files, self.folders, self.explicit, self.file_keys = [], {}, set(), set()
        self.size = self.metadata = 0

    def add(self, name, *, directory, size, index, container):
        if container != "zip":
            if container in {"rar", "7zip"}:
                name = name.replace("\\", "/")
            while name.startswith("./"):
                name = name[2:]
            if name in {"", "."} and directory and not size:
                return None
        if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            raise WorkerError("Archive contains an unsafe entry path.")
        parts = (
            name[:-1].split("/")
            if directory and name.endswith("/")
            else name.split("/")
        )
        if len(parts) > MAX_DEPTH or len(name) > 4096:
            raise WorkerError("Archive paths exceed 64 components or 4096 characters.")
        normalized = []
        for part in parts:
            value = unicodedata.normalize("NFC", part)
            if (
                not value
                or value in {".", ".."}
                or value != value.strip()
                or len(value) > 255
                or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)
            ):
                raise WorkerError("Archive contains an unsafe entry name.")
            normalized.append(value)
        parts = tuple(normalized)
        key = tuple(part.casefold() for part in parts)
        if (
            key in self.explicit
            or key in self.file_keys
            or (not directory and key in self.folders)
        ):
            raise WorkerError("Archive contains duplicate or conflicting entry paths.")
        self.explicit.add(key)
        for depth in range(1, len(parts) + int(directory)):
            parent, actual = key[:depth], parts[:depth]
            if parent in self.file_keys or (
                parent in self.folders and self.folders[parent] != actual
            ):
                raise WorkerError(
                    "Archive contains duplicate or conflicting folder names."
                )
            if parent not in self.folders:
                self.folders[parent] = actual
                self.metadata += (
                    len(json.dumps(actual, ensure_ascii=False).encode()) + 1
                )
        if directory:
            if size not in {0, None}:
                raise WorkerError("Archive directory entries must be empty.")
            item = None
        else:
            if size is not None and (type(size) is not int or size <= 0):
                raise WorkerError(
                    "TGDrive cannot extract archives containing empty files."
                )
            if size is None and container != "raw":
                raise WorkerError("An archive entry has no declared size.")
            if size is not None and size > self.limit:
                raise WorkerError(
                    "An extracted file exceeds the configured upload limit."
                )
            self.file_keys.add(key)
            item = {"name": "/".join(parts), "size": size, "index": index}
            self.files.append(item)
            self.metadata += len(json.dumps(item, ensure_ascii=False).encode()) + 1
            self.size += size or 0
        if (
            len(self.files) + len(self.folders) > MAX_ENTRIES
            or len(self.explicit) > MAX_ENTRIES
        ):
            raise WorkerError(
                "An archive supports at most 1000 files and folders "
                "including implicit folders."
            )
        if self.size > self.budget:
            raise WorkerError("Archive and extracted files exceed the staging budget.")
        if self.metadata > MAX_MANIFEST_BYTES:
            raise WorkerError(
                "The expanded archive manifest exceeds the 4 MiB job metadata limit."
            )
        return item

    def result(self):
        plan = {
            "files": self.files,
            "folders": sorted(self.folders.values(), key=lambda p: (len(p), p)),
            "size": self.size,
        }
        if (
            len(json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode())
            > MAX_MANIFEST_BYTES
        ):
            raise WorkerError(
                "The expanded archive manifest exceeds the 4 MiB job metadata limit."
            )
        return plan


def send(message, *, approval=False):
    print(json.dumps(message, ensure_ascii=False, separators=(",", ":")), flush=True)
    if approval and sys.stdin.buffer.readline(32) != b"ok\n":
        raise WorkerError("Archive processing was interrupted.")


class BoundedStream:
    def __init__(self, stream, maximum):
        self.stream, self.maximum = stream, maximum
        self.read_bytes = 0
        self.failure = None

    def seekable(self):
        return False

    def readinto(self, target):
        try:
            count = self.stream.readinto(target)
            self.read_bytes += count
            if self.read_bytes > self.maximum:
                raise WorkerError(
                    "Decoded archive data exceeds its file and metadata budget."
                )
            return count
        except Exception as error:
            self.failure = error
            return 0

    def drain(self):
        if self.failure:
            raise self.failure
        buffer = bytearray(BLOCK)
        while self.readinto(buffer):
            pass
        if self.failure:
            raise self.failure


@contextmanager
def compressed_source(path, compression, maximum):
    # Stdlib wrappers and the EOF drain verify compression trailers; native gzip
    # filtering alone does not check its CRC or uncompressed length.
    import bz2
    import gzip
    import lzma

    if compression == "zstd":
        from compression.zstd import ZstdFile

        opener = ZstdFile
    else:
        opener = {None: open, "gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}[
            compression
        ]
    with opener(path, "rb") as stream:
        bounded = BoundedStream(stream, maximum)
        try:
            yield bounded
        except Exception as error:
            if bounded.failure:
                raise bounded.failure from error
            raise
        bounded.drain()


def rar_header(path):
    with path.open("rb") as source:
        header = source.read(65536)
    if header.startswith(b"Rar!\x1a\x07\x00"):
        if len(header) < 14 or header[9] != 0x73:
            raise WorkerError("Invalid RAR archive header.")
        flags = struct.unpack_from("<H", header, 10)[0]
        if flags & (1 | 0x80):
            raise WorkerError("Encrypted and split-volume archives are unsupported.")
    elif header.startswith(b"Rar!\x1a\x07\x01\x00"):
        position = 12

        def integer():
            nonlocal position
            result = 0
            for shift in range(0, 64, 7):
                if position >= len(header):
                    break
                byte = header[position]
                position += 1
                result |= (byte & 127) << shift
                if byte < 128:
                    return result
            raise WorkerError("Invalid RAR5 archive header.")

        integer()  # Header size.
        kind, flags = integer(), integer()
        if kind == 4:
            raise WorkerError("Encrypted archives are unsupported.")
        if kind != 1:
            raise WorkerError("Invalid RAR5 archive header.")
        if flags & 1:
            integer()  # Extra area size.
        if flags & 2:
            integer()  # Data area size.
        if integer() & 1:
            raise WorkerError("Split-volume archives are unsupported.")
    else:
        raise WorkerError("This is not a readable RAR archive.")


def extract(request):
    import libarchive
    from libarchive import ffi

    source, destination = Path(request["source"]), Path(request["destination"])
    suffix, (container, compression) = archive_format(request["name"])
    budget = request["budget"] - source.stat().st_size
    plan = Manifest(request["limit"], budget)
    reserved = current = 0
    encrypted = ffi.ffi("entry_is_encrypted", [ffi.c_archive_entry_p], ffi.c_int)
    if container == "rar":
        rar_header(source)

    def receive(item, blocks):
        nonlocal reserved, current
        if item["size"] is not None:
            reserved = plan.size
            send({"reserve": reserved}, approval=True)
        received = 0
        with (destination / f"{len(plan.files) - 1}.file").open("wb") as output:
            for block in blocks:
                received += len(block)
                if received > request["limit"] or (
                    item["size"] is not None and received > item["size"]
                ):
                    raise WorkerError(
                        "An archive entry exceeded its declared size or upload limit."
                    )
                if current + len(block) > reserved:
                    reserved = min(
                        budget,
                        max(
                            BLOCK,
                            ((current + len(block) + 8 * BLOCK - 1) // (8 * BLOCK))
                            * 8
                            * BLOCK,
                        ),
                    )
                    if current + len(block) > reserved:
                        raise WorkerError(
                            "Archive and extracted files exceed the staging budget."
                        )
                    send({"reserve": reserved}, approval=True)
                send({"write": len(block)}, approval=True)
                output.write(block)
                current += len(block)
                send({"progress": current})
            output.flush()
            os.fsync(output.fileno())
        if not received:
            raise WorkerError("TGDrive cannot extract archives containing empty files.")
        if item["size"] is not None and received != item["size"]:
            raise WorkerError("An archive entry ended before its declared size.")
        if item["size"] is None:
            item["size"] = received
            plan.size += received

    @contextmanager
    def reader():
        if container in {"tar", "raw"}:
            with (
                compressed_source(
                    source,
                    compression,
                    budget + (TAR_METADATA_BYTES if container == "tar" else 0),
                ) as stream,
                libarchive.stream_reader(
                    stream, format_name=container, filter_name="none", block_size=BLOCK
                ) as archive,
            ):
                yield archive, stream
        else:
            with libarchive.file_reader(
                str(source),
                format_name="all" if container == "rar" else container,
                filter_name="none",
            ) as archive:
                yield archive, None

    with strict_native_warnings(), reader() as (archive, stream):
        for index, entry in enumerate(archive):
            if stream and stream.failure:
                raise stream.failure
            if index >= MAX_ENTRIES:
                raise WorkerError("An archive supports at most 1000 entries.")
            if encrypted(entry._entry_p):
                raise WorkerError("Encrypted archives are unsupported.")
            if (
                entry.linkpath
                or entry.islnk
                or entry.issym
                or not (entry.isdir or entry.isfile)
            ):
                raise WorkerError("Archive links and special files are unsupported.")
            name = (
                (request["name"][: -len(suffix)] or "Extracted file")
                if container == "raw"
                else entry.pathname
            )
            if not isinstance(name, str):
                raise WorkerError("Archive filenames must be valid Unicode.")
            item = plan.add(
                name,
                directory=entry.isdir,
                size=None if container == "raw" else entry.size,
                index=index,
                container=container,
            )
            if item is not None:
                receive(item, entry.get_blocks(BLOCK))
            if stream and stream.failure:
                raise stream.failure
    return plan.result()


def run():
    import resource

    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    request = json.loads(sys.stdin.buffer.readline(16384))
    resource.setrlimit(resource.RLIMIT_FSIZE, (request["limit"], request["limit"]))
    try:
        send({"result": extract(request)})
    except Exception as error:
        message = str(error)
        if not isinstance(error, WorkerError):
            message = (
                "Archive integrity/CRC verification failed, or the archive is "
                "damaged, encrypted or unsupported. " + message
            )
        send({"error": message[:500]})


if __name__ == "__main__":
    run()
