"""Authorized short-window HLS; no persistent derivative or original rewrite."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import signal
import sys
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web
from fastapi import HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from tgdrive.media import file_range
from tgdrive.media.compatibility_worker import (
    ADDRESS_SPACE_BYTES,
    COMPATIBILITY_FORMATS,
    MAX_DURATION_SECONDS,
    MAX_SEGMENT_BYTES,
    SEGMENT_SECONDS,
    duration,
)
from tgdrive.media.generated_thumbnails import _finish_owned, _owned_task
from tgdrive.media.media_scheduler import media_priority
from tgdrive.web.file_bandwidth import paced_body

BLOCK_BYTES = 1024**2
MAX_SOURCE_BYTES = 256 * BLOCK_BYTES
MAX_MANIFEST_BYTES = BLOCK_BYTES
MIN_AVAILABLE_BYTES = ADDRESS_SPACE_BYTES + 128 * BLOCK_BYTES
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def file_version(data):
    identity = [data[key] for key in ("id", "message_id", "size")]
    identity += [data.get("parts")]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class PlaybackSource:
    """One native job's token-exact, bounded localhost range reader."""

    def __init__(self, data, fetch, authorize):
        self.data, self.fetch, self.authorize = data, fetch, authorize
        self.token = secrets.token_hex(16)
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.served_bytes = 0
        self.fetched_bytes = 0
        self.tasks = set()
        self.error = None

    async def block(self, offset):
        await self.authorize()
        async with self.lock:
            if offset not in self.cache:
                count = min(BLOCK_BYTES, self.data["size"] - offset)
                if self.fetched_bytes + count > MAX_SOURCE_BYTES:
                    raise HTTPException(
                        413, "Compatible playback exceeded its source window."
                    )
                self.fetched_bytes += count
                value = await self.fetch(offset, count)
                if len(value) != count:
                    raise HTTPException(409, "The video ended early. Reopen it.")
                self.cache[offset] = value
                while len(self.cache) > 4:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(offset)
            return self.cache[offset]

    async def handle(self, request):
        if request.path != "/" + self.token or request.query_string:
            raise web.HTTPNotFound()
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            await self.authorize()
            size = self.data["size"]
            start, end, status = 0, size - 1, 200
            raw = request.headers.get("Range")
            if raw:
                match = re.fullmatch(r"bytes=(\d{1,20})-(\d{1,20})?", raw)
                if not match:
                    raise web.HTTPRequestRangeNotSatisfiable()
                start = int(match[1])
                end = min(size - 1, int(match[2])) if match[2] else size - 1
                status = 206
            if not 0 <= start <= end < size:
                raise web.HTTPRequestRangeNotSatisfiable()
            headers = {"Content-Length": str(end - start + 1), "Accept-Ranges": "bytes"}
            if status == 206:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            response = web.StreamResponse(status=status, headers=headers)
            await response.prepare(request)
            if request.method == "HEAD":
                return response
            while start <= end:
                count = min(BLOCK_BYTES - start % BLOCK_BYTES, end - start + 1)
                if self.served_bytes + count > MAX_SOURCE_BYTES:
                    raise HTTPException(
                        413, "Compatible playback exceeded its source window."
                    )
                self.served_bytes += count
                block = await self.block(start - start % BLOCK_BYTES)
                first = start % BLOCK_BYTES
                await response.write(block[first : first + count])
                start += count
            return response
        except HTTPException as error:
            self.error = error
            if request.transport is not None:
                request.transport.close()
            raise web.HTTPForbidden() from None
        except ConnectionError:
            return web.Response(status=499)
        finally:
            self.tasks.discard(task)

    @asynccontextmanager
    async def open(self):
        app = web.Application()
        app.router.add_route("GET", "/{token}", self.handle)
        app.router.add_route("HEAD", "/{token}", self.handle)
        runner = web.AppRunner(app, access_log=None)
        try:
            await _owned_task(asyncio.create_task(runner.setup()))
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await _owned_task(asyncio.create_task(site.start()))
            port = runner.addresses[0][1]
            yield f"http://127.0.0.1:{port}/{self.token}"
        finally:

            async def cleanup():
                for task in list(self.tasks):
                    task.cancel()
                await asyncio.gather(*list(self.tasks), return_exceptions=True)
                await runner.cleanup()
                self.cache.clear()

            await _owned_task(asyncio.create_task(cleanup()))


class PlaybackJobs:
    def __init__(self):
        self.slot = asyncio.Semaphore(1)
        self.pending = 0

    async def native(self, source, suffix, index):
        from tgdrive.core.governor import memory

        if self.pending >= 5:
            raise HTTPException(
                503,
                "Compatible playback is busy. Try again shortly.",
                headers={"Retry-After": "3"},
            )
        self.pending += 1
        acquired = False
        try:
            try:
                async with asyncio.timeout(15):
                    await self.slot.acquire()
                    acquired = True
            except TimeoutError:
                raise HTTPException(
                    503,
                    "Compatible playback is busy. Try again shortly.",
                    headers={"Retry-After": "3"},
                ) from None
            _, available = await asyncio.to_thread(memory)
            if available is None or available < MIN_AVAILABLE_BYTES:
                raise HTTPException(
                    503, "Compatible playback needs more available server memory."
                )
            process = None
            try:
                async with asyncio.timeout(35), source.open() as url:
                    opening = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            sys.executable,
                            "-m",
                            "tgdrive.media.compatibility_worker",
                            url,
                            suffix,
                            "probe" if index is None else str(index),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                            start_new_session=True,
                            env={
                                "PATH": os.defpath,
                                "LANG": "C.UTF-8",
                                "PYTHONPATH": PROJECT_ROOT,
                            },
                        )
                    )
                    process, interrupted = await _finish_owned(opening)
                    if interrupted:
                        raise asyncio.CancelledError
                    assert process is not None and process.stdout is not None
                    maximum = 64 * 1024 if index is None else MAX_SEGMENT_BYTES
                    content = bytearray()
                    while block := await process.stdout.read(64 * 1024):
                        if len(content) + len(block) > maximum:
                            raise HTTPException(
                                413, "Compatible playback output is too large."
                            )
                        content.extend(block)
                    await process.wait()
                    if source.error:
                        raise source.error
                    if process.returncode or not content:
                        raise HTTPException(
                            422,
                            "This video could not be converted within playback limits. "
                            "Download the original.",
                        )
                    return bytes(content)
            except OSError, TimeoutError:
                raise HTTPException(
                    503,
                    "Compatible playback is unavailable or took too long. "
                    "Download the original.",
                ) from None
            finally:
                if process is not None:
                    with suppress(ProcessLookupError):
                        if process.returncode is None:
                            os.killpg(process.pid, signal.SIGKILL)
                    await _owned_task(asyncio.create_task(process.wait()))
        finally:
            self.pending -= 1
            if acquired:
                self.slot.release()


async def watch_request(request, authorize, operation):
    async def watch():
        while True:
            await authorize()
            if await request.is_disconnected():
                raise HTTPException(499, "Playback request disconnected")
            await asyncio.sleep(0.5)

    job, monitor = asyncio.create_task(operation), asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait(
            {job, monitor}, return_when=asyncio.FIRST_COMPLETED
        )
        return await (monitor if monitor in done else job)
    finally:
        job.cancel()
        monitor.cancel()
        await _owned_task(asyncio.create_task(_drain(job, monitor)))


async def _drain(*tasks):
    await asyncio.gather(*tasks, return_exceptions=True)


def register_compatibility_routes(app, settings, runtime, authorized_file):
    jobs = PlaybackJobs()
    probe_key = secrets.token_bytes(32)

    def signature(version, seconds):
        # Attests the bounded probe, never substitutes for current file access.
        return hmac.new(
            probe_key, f"{version}:{seconds:.6f}".encode(), hashlib.sha256
        ).hexdigest()

    async def source_for(request, path, auth):
        path, data = await asyncio.to_thread(authorized_file, request, path, auth)
        suffix = Path(data["name"]).suffix.lower()
        if suffix not in COMPATIBILITY_FORMATS:
            raise HTTPException(
                415, "Compatible playback requires a supported video container."
            )
        version = file_version(data)

        async def authorize():
            _, current = await asyncio.to_thread(authorized_file, request, path, auth)
            if file_version(current) != version:
                raise HTTPException(409, "The video changed. Reopen it.")

        async def fetch(start, count):
            with media_priority.set("playback"):
                return await file_range.read_range(
                    runtime(request).pool,
                    settings.storage_channel,
                    data,
                    start,
                    count,
                    authorize,
                )

        return PlaybackSource(data, fetch, authorize), suffix, version

    @app.get("/api/v1/playback/compatibility.m3u8")
    async def manifest(
        request: Request,
        path: str = Query(max_length=4096),
        auth: str | None = Query(default=None, max_length=1024),
    ):
        source, suffix, version = await source_for(request, path, auth)
        content = await watch_request(
            request, source.authorize, jobs.native(source, suffix, None)
        )
        try:
            seconds = duration(json.loads(content))
        except ValueError, TypeError, KeyError, OverflowError:
            raise HTTPException(
                422,
                "Compatible playback requires a finite video up to 4K and 24 hours.",
            ) from None
        query = urlencode(
            {
                "path": path,
                "version": version,
                "seconds": f"{seconds:.6f}",
                "signature": signature(version, seconds),
                **({"auth": auth} if auth is not None else {}),
            }
        )
        lines = [
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        ]
        manifest_bytes = len(lines[0])
        for index in range(math.ceil(seconds / SEGMENT_SECONDS)):
            length = min(SEGMENT_SECONDS, seconds - index * SEGMENT_SECONDS)
            line = (
                f"#EXTINF:{length:.6f},\n"
                f"/api/v1/playback/segment.ts?{query}&index={index}\n"
            )
            manifest_bytes += len(line)
            if manifest_bytes > MAX_MANIFEST_BYTES:
                raise HTTPException(413, "Compatible playback manifest is too large.")
            lines.append(line)
        await source.authorize()
        return Response(
            "".join(lines) + "#EXT-X-ENDLIST\n",
            media_type="application/vnd.apple.mpegurl",
            headers=HEADERS,
        )

    @app.get("/api/v1/playback/segment.ts")
    async def segment(
        request: Request,
        path: str = Query(max_length=4096),
        version: str = Query(pattern=r"^[a-f0-9]{64}$"),
        index: int = Query(ge=0, lt=MAX_DURATION_SECONDS // SEGMENT_SECONDS),
        seconds: float = Query(gt=0, le=MAX_DURATION_SECONDS),
        signature_value: str = Query(alias="signature", pattern=r"^[a-f0-9]{64}$"),
        auth: str | None = Query(default=None, max_length=1024),
    ):
        source, suffix, current = await source_for(request, path, auth)
        if version != current:
            raise HTTPException(409, "The video changed. Reopen it.")
        if not math.isfinite(seconds) or not hmac.compare_digest(
            signature_value, signature(current, seconds)
        ):
            raise HTTPException(
                403, "Reopen compatible playback to verify its duration."
            )
        if index >= math.ceil(seconds / SEGMENT_SECONDS):
            raise HTTPException(416, "Playback segment is outside the video.")
        content = await watch_request(
            request, source.authorize, jobs.native(source, suffix, index)
        )
        await source.authorize()

        async def delivery():
            bandwidth = getattr(runtime(request), "bandwidth", None)
            async for block in paced_body(
                content, bandwidth.upload if bandwidth else None
            ):
                await source.authorize()
                yield block

        return StreamingResponse(
            delivery(),
            media_type="video/mp2t",
            headers={**HEADERS, "Content-Length": str(len(content))},
        )
