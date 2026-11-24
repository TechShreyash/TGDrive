"""Read ZIP listings through a bounded parser process."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from pathlib import Path, PurePath

from fastapi import HTTPException

BLOCK = 1024 * 1024
MAX_READ = 32 * BLOCK
MAX_OUTPUT = 24 * BLOCK
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


async def parse_archive(data, fetch, entry=None):
    fmt = PurePath(data["name"]).suffix.lower().lstrip(".")
    if fmt != "zip":
        raise HTTPException(415, "This file has no ZIP preview.")
    # Deliberately do not pass Telegram credentials, owner secrets or inherited
    # application environment into a parser handling untrusted archive bytes.
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "PYTHONPATH": PROJECT_ROOT,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tgdrive.media.archive_preview_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=environment,
        limit=MAX_OUTPUT,
    )
    assert process.stdin is not None and process.stdout is not None
    total = 0
    try:
        async with asyncio.timeout(45):
            process.stdin.write(
                json.dumps(
                    {
                        "size": data["size"],
                        "entry": entry,
                    }
                ).encode()
                + b"\n"
            )
            await process.stdin.drain()
            while True:
                line = await process.stdout.readline()
                if not line or len(line) > MAX_OUTPUT:
                    raise HTTPException(
                        422,
                        "The ZIP exceeds the preview limits or is damaged. "
                        "Download it instead.",
                    )
                message = json.loads(line)
                if "read" in message:
                    start, count = message["read"]
                    if (
                        type(start) is not int
                        or type(count) is not int
                        or start < 0
                        or not 0 < count <= BLOCK
                        or start + count > data["size"]
                        or total + count > MAX_READ
                    ):
                        raise HTTPException(
                            422,
                            "This ZIP needs too much data for a selective preview. "
                            "Download it instead.",
                        )
                    total += count
                    content = await fetch(start, count)
                    process.stdin.write(
                        json.dumps(
                            {"data": base64.b64encode(content).decode()}
                        ).encode()
                        + b"\n"
                    )
                    await process.stdin.drain()
                elif "error" in message:
                    raise HTTPException(422, str(message["error"])[:500])
                elif isinstance(message.get("result"), dict):
                    return message["result"]
                else:
                    raise HTTPException(422, "The ZIP could not be read.")
    except TimeoutError as exc:
        raise HTTPException(
            422, "This ZIP took too long to read. Download the original instead."
        ) from exc
    except (
        ValueError,
        KeyError,
        BrokenPipeError,
        ConnectionResetError,
        asyncio.IncompleteReadError,
    ) as exc:
        raise HTTPException(
            422, "This ZIP could not be read. Download the original instead."
        ) from exc
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await asyncio.shield(process.wait())
        if process.stdin:
            process.stdin.close()


class ArchivePreviewCache:
    """Bounded ZIP listing reuse; authorization stays in the route."""

    def __init__(self, *, maximum=64 * BLOCK, entries=16, ttl=300):
        self.maximum, self.entries, self.ttl = maximum, entries, ttl
        self.bytes = 0
        self.items = OrderedDict()
        self.locks = {}

    @staticmethod
    def key(data, entry):
        source = {key: data.get(key) for key in ("message_id", "size", "parts", "name")}
        return hashlib.sha256(
            json.dumps([source, entry], sort_keys=True).encode()
        ).hexdigest()

    def get(self, key):
        now = time.monotonic()
        for stale, (expires, content, _etag) in list(self.items.items()):
            if expires <= now:
                self.bytes -= len(content)
                del self.items[stale]
        result = self.items.get(key)
        if result is not None:
            self.items.move_to_end(key)
            return result[1:]
        return None

    def put(self, key, result):
        content = json.dumps(result).encode()
        etag = '"' + hashlib.sha256(content).hexdigest() + '"'
        if len(content) <= self.maximum:
            previous = self.items.pop(key, None)
            if previous:
                self.bytes -= len(previous[1])
            while self.items and (
                self.bytes + len(content) > self.maximum
                or len(self.items) >= self.entries
            ):
                _, old = self.items.popitem(last=False)
                self.bytes -= len(old[1])
            self.items[key] = (time.monotonic() + self.ttl, content, etag)
            self.bytes += len(content)
        return content, etag

    @asynccontextmanager
    async def hold(self, key):
        lock, users = self.locks.get(key, (asyncio.Lock(), 0))
        self.locks[key] = lock, users + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self.locks[key][1] - 1
            if remaining:
                self.locks[key] = lock, remaining
            else:
                del self.locks[key]
