"""Authorized, selective book reads through a resource-limited parser process."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from contextlib import suppress
from pathlib import Path, PurePath

from fastapi import HTTPException

from tgdrive.file_parts import validate_parts
from tgdrive.logical_media import PartLease, part_ranges
from tgdrive.media_reader import stream_pooled_media
from tgdrive.previews import lease_media
from tgdrive.ranges import ByteRange, plan_telegram_chunks

BLOCK = 1024 * 1024
MAX_READ = 32 * BLOCK
MAX_OUTPUT = 24 * BLOCK
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


async def read_range(pool, channel, data, start, count, authorize):
    await authorize()
    selection = ByteRange(start, start + count - 1, data["size"], is_partial=True)
    result = bytearray()
    if data.get("parts") is not None:
        parts = validate_parts(
            data["parts"], size=data["size"], message_id=data["message_id"]
        )
        for part, interval in part_ranges(parts, selection):
            lease = await PartLease(pool, channel, part, authorize=authorize).open()
            stream = lease.read(interval)
            try:
                async for block in stream:
                    result.extend(block)
            finally:
                await stream.aclose()
                await lease.close()
    else:
        async with lease_media(pool, channel, data["message_id"]) as (
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


async def parse_book(data, fetch, entry=None):
    fmt = PurePath(data["name"]).suffix.lower().lstrip(".")
    if fmt not in {"epub", "cbz", "cbr"}:
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
        "tgdrive.book_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=environment,
        limit=MAX_OUTPUT,
    )
    total = 0
    try:
        async with asyncio.timeout(45):
            process.stdin.write(
                json.dumps(
                    {"size": data["size"], "format": fmt, "entry": entry}
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
                        or total + count > MAX_READ
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
    except (ValueError, KeyError, BrokenPipeError, ConnectionResetError) as exc:
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
