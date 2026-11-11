"""Resource-limited torrent decoding; raw metadata never leaves this child."""

from __future__ import annotations

import json
import mmap
import os
import resource
import stat
import sys
from dataclasses import asdict
from pathlib import Path

MAX_MEMORY_BYTES = 512 * 1024 * 1024


def main():
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    from tgdrive.transfers.torrent import (
        MAX_MANIFEST_BYTES,
        MAX_METADATA_BYTES,
        MAX_SANITIZED_BYTES,
        TorrentError,
        _decode,
        _encode,
        _manifest,
        _tracker_urls,
    )

    resource.setrlimit(
        resource.RLIMIT_FSIZE, (MAX_SANITIZED_BYTES, MAX_SANITIZED_BYTES)
    )
    try:
        # mmap avoids copying the full serialized admission into the Python heap.
        descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            details = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(details.st_mode)
                or not 0 < details.st_size <= MAX_METADATA_BYTES
            ):
                raise TorrentError("Torrent metadata exceeds its 256 MiB input limit.")
            with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
                metadata = _decode(data)
        manifest = _manifest(metadata)
        sanitized = _encode({b"info": metadata[b"info"]})
        if len(sanitized) > MAX_SANITIZED_BYTES:
            raise TorrentError("Sanitized torrent metadata exceeds its 12 MiB limit.")
        plan = {
            **asdict(manifest),
            "trackers": _tracker_urls(metadata),
            "private": bool(metadata[b"info"].get(b"private")),
        }
        response = json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode()
        if len(response) + 1 > MAX_MANIFEST_BYTES:
            raise TorrentError("Torrent manifest exceeds its 4 MiB response limit.")
        with Path(sys.argv[2]).open("xb") as output:
            os.chmod(output.fileno(), 0o600)
            output.write(sanitized)
        sys.stdout.buffer.write(response + b"\n")
    except (TorrentError, OSError, MemoryError, RecursionError) as error:
        message = (
            str(error)
            if isinstance(error, TorrentError)
            else ("Torrent metadata could not be processed within its resource limits.")
        )
        print(json.dumps({"error": message}), flush=True)


if __name__ == "__main__":
    main()
