"""Authorized, selective book reads through a resource-limited parser process."""

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

from tgdrive.media.logical_media import PartLease, part_ranges
from tgdrive.media.media_reader import stream_pooled_media
from tgdrive.media.previews import lease_media
from tgdrive.media.ranges import ByteRange, plan_telegram_chunks
from tgdrive.storage.file_parts import validate_parts

BLOCK = 1024 * 1024
MAX_READ = 32 * BLOCK
MAX_OUTPUT = 24 * BLOCK
MAX_PAGE = 64 * BLOCK
MAX_PAGE_READ = 96 * BLOCK
RASTER_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


async def read_range(pool, channel, data, start, count, authorize, *, background=False):
    await authorize()
    selection = ByteRange(start, start + count - 1, data["size"], is_partial=True)
    result = bytearray()
    if data.get("parts") is not None:
        parts = validate_parts(
            data["parts"], size=data["size"], message_id=data["message_id"]
        )
        for part, interval in part_ranges(parts, selection):
            lease = await PartLease(
                pool, channel, part, authorize=authorize, background=background
            ).open()
            stream = lease.read(interval)
            try:
                async for block in stream:
                    result.extend(block)
            finally:
                await stream.aclose()
                await lease.close()
    else:
        async with lease_media(
            pool, channel, data["message_id"], background=background
        ) as (
            client,
            message,
            media,
        ):
            if media.file_size != data["size"]:
                raise HTTPException(409, "The stored book changed. Reopen it.")
            plan = plan_telegram_chunks(selection)
            stream = stream_pooled_media(
                pool,
                client,
                message,
                channel=channel,
                offset=plan.offset // plan.chunk_size,
                limit=plan.part_count,
                authorize=authorize,
                background=background,
            )
            try:
                index = 0
                async for block in stream:
                    first = plan.first_part_cut if index == 0 else 0
                    last = (
                        plan.last_part_cut
                        if index == plan.part_count - 1
                        else len(block)
                    )
                    result.extend(block[first:last])
                    index += 1
                    if index >= plan.part_count:
                        break
            finally:
                await stream.aclose()
    if len(result) != count:
        raise HTTPException(
            409, "The book ended early. Reopen it or download the original."
        )
    await authorize()
    return bytes(result)


async def parse_book(data, fetch, entry=None, *, thumbnail=False, raw_images=False):
    fmt = PurePath(data["name"]).suffix.lower().lstrip(".")
    if fmt not in (
        {"epub", "cbz", "cbr", "docx", "pptx", "xlsx", "odt", "odp", "ods"}
        if thumbnail
        else {"epub", "cbz", "cbr"}
    ):
        raise HTTPException(415, "This file is not an EPUB, CBZ or CBR book.")
    # Deliberately do not pass Telegram credentials, owner secrets or inherited
    # application environment into a parser handling untrusted archive bytes.
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "PYTHONPATH": PROJECT_ROOT,
    }
    if os.environ.get("LIBARCHIVE"):
        environment["LIBARCHIVE"] = os.environ["LIBARCHIVE"]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tgdrive.media.book_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=environment,
        limit=MAX_OUTPUT,
    )
    raster = (
        raw_images
        and not thumbnail
        and PurePath(entry or "").suffix.lower()
        in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}
    )
    maximum = MAX_PAGE_READ if raster else MAX_READ
    total = 0
    try:
        async with asyncio.timeout(45):
            process.stdin.write(
                json.dumps(
                    {
                        "size": data["size"],
                        "format": fmt,
                        "entry": entry,
                        "thumbnail": thumbnail,
                        "raw_images": raw_images,
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
                        "The book exceeds the reader limits or is damaged. "
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
                        or total + count > maximum
                    ):
                        raise HTTPException(
                            422,
                            "This book needs too much data for a selective preview. "
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
                elif isinstance(message.get("image"), dict):
                    image = message["image"]
                    length, mime = image.get("length"), image.get("mime")
                    if (
                        not raster
                        or type(length) is not int
                        or not 0 < length <= MAX_PAGE
                        or mime not in RASTER_TYPES
                    ):
                        raise HTTPException(422, "The book returned an invalid image.")
                    content = await process.stdout.readexactly(length)
                    return {"kind": "image", "mime": mime, "content": content}
                elif "error" in message:
                    raise HTTPException(422, str(message["error"])[:500])
                elif isinstance(message.get("result"), dict):
                    return message["result"]
                else:
                    raise HTTPException(422, "The book could not be read.")
    except TimeoutError as exc:
        raise HTTPException(
            422, "This book took too long to read. Download it in a dedicated reader."
        ) from exc
    except (
        ValueError,
        KeyError,
        BrokenPipeError,
        ConnectionResetError,
        asyncio.IncompleteReadError,
    ) as exc:
        raise HTTPException(
            422, "This book could not be read. Download the original instead."
        ) from exc
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await asyncio.shield(process.wait())
        if process.stdin:
            process.stdin.close()


class BookCache:
    """Bounded original-byte/metadata reuse; authorization stays in the route."""

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
        for stale, (expires, content, _mime, _etag) in list(self.items.items()):
            if expires <= now:
                self.bytes -= len(content)
                del self.items[stale]
        result = self.items.get(key)
        if result is not None:
            self.items.move_to_end(key)
            return result[1:]
        return None

    def put(self, key, result):
        mime = (
            result.get("mime") if result.get("kind") == "image" else "application/json"
        )
        content = (
            result["content"]
            if result.get("kind") == "image"
            else json.dumps(result).encode()
        )
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
            self.items[key] = (time.monotonic() + self.ttl, content, mime, etag)
            self.bytes += len(content)
        return content, mime, etag

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
