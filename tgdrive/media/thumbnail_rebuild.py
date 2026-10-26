"""Owner-started, bounded thumbnail regeneration, separate from transfer admission."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import re
import secrets
import tempfile
import time
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import Literal

from aiohttp import web
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from tgdrive.core.errors import (
    NotDirectoryError,
    NotFileError,
    NotFoundError,
    TrashedItemError,
)
from tgdrive.media import generated_thumbnails as covers
from tgdrive.media.books import parse_book, read_range
from tgdrive.media.media_scheduler import media_priority
from tgdrive.media.thumbnail_worker import (
    AUDIO_FORMATS,
    IMAGE_CODECS,
    OFFICE_FORMATS,
    VIDEO_FORMATS,
)

BLOCK = 1024**2
SOURCE_BUDGET = 64 * BLOCK
PDF_BUDGET = 256 * BLOCK
RATE = 2 * BLOCK
SUPPORTED = covers.SUPPORTED | {".cbr"}


def file_identity(data):
    return data["message_id"], data["size"], data.get("parts")


def write_state(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".thumbnail-job-")
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(data, output, allow_nan=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class SourceLimit(Exception):
    pass


class RebuildSource:
    """Four hot blocks and a cumulative per-file read limit, including repeat reads."""

    def __init__(self, owner, data):
        self.owner, self.data = owner, data
        self.blocks = OrderedDict()
        self.used = 0
        self.budget = (
            PDF_BUDGET if Path(data["name"]).suffix.lower() == ".pdf" else SOURCE_BUDGET
        )
        self.lock = asyncio.Lock()
        self.token = secrets.token_urlsafe(32)
        self.runner = None

    async def authorize(self):
        store = self.owner.runtime().store
        current = store.get_file(store.resolve_id(self.data["id"]))
        if store.drive_id != self.owner.drive_id or file_identity(
            current
        ) != file_identity(self.data):
            raise SourceLimit("File removed or changed")

    async def read(self, start, count):
        if start < 0 or count < 1 or start + count > self.data["size"]:
            raise SourceLimit("Invalid source range")
        output = bytearray()
        async with self.lock:
            await self.authorize()
            while len(output) < count:
                offset = start + len(output)
                index = offset // BLOCK
                if index not in self.blocks:
                    length = min(BLOCK, self.data["size"] - index * BLOCK)
                    if self.used + length > self.budget:
                        raise SourceLimit("Source read budget reached")
                    await self.owner.wait_turn()
                    began = time.monotonic()
                    data = await read_range(
                        self.owner.runtime().pool,
                        self.owner.settings.storage_channel,
                        self.data,
                        index * BLOCK,
                        length,
                        self.authorize,
                        background=True,
                    )
                    if len(data) != length:
                        raise SourceLimit("Source ended early")
                    self.used += len(data)
                    self.owner.state["source_bytes"] += len(data)
                    self.owner.state["current_bytes"] = self.used
                    self.blocks[index] = data
                    while len(self.blocks) > 4:
                        self.blocks.popitem(last=False)
                    await self.owner.persist_progress()
                    await asyncio.sleep(
                        max(0, length / RATE - (time.monotonic() - began))
                    )
                self.blocks.move_to_end(index)
                block = self.blocks[index]
                position = offset % BLOCK
                output.extend(
                    block[
                        position : position
                        + min(count - len(output), len(block) - position)
                    ]
                )
        return bytes(output)

    async def serve(self, request):
        if request.path != "/" + self.token or request.method not in {"GET", "HEAD"}:
            raise web.HTTPNotFound()
        size = self.data["size"]
        start, end, partial = 0, size - 1, False
        if value := request.headers.get("Range"):
            match = (
                re.fullmatch(r"bytes=(\d+)-(\d*)", value) if len(value) < 128 else None
            )
            if not match:
                raise web.HTTPRequestRangeNotSatisfiable()
            start, raw_end = match.groups()
            start, end = int(start), min(int(raw_end), end) if raw_end else end
            partial = True
        if not 0 <= start <= end < size:
            raise web.HTTPRequestRangeNotSatisfiable()
        await self.authorize()
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": "application/octet-stream",
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        response = web.StreamResponse(status=206 if partial else 200, headers=headers)
        await response.prepare(request)
        if request.method == "HEAD":
            return response
        try:
            while start <= end:
                # Keep at most one chunk queued to the local decoder; its seeks
                # close abandoned streams before another origin block is read.
                data = await self.read(start, min(65536, end - start + 1))
                await response.write(data)
                start += len(data)
                await asyncio.sleep(len(data) / RATE)
            await response.write_eof()
        except ConnectionError, SourceLimit, asyncio.CancelledError:
            response.force_close()
        return response

    async def open(self):
        app = web.Application(client_max_size=1024)
        app.router.add_route("*", "/{tail:.*}", self.serve)
        self.runner = web.AppRunner(
            app, access_log=None, shutdown_timeout=0.1, handler_cancellation=True
        )
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/{self.token}"

    async def close(self):
        if self.runner is not None:
            await self.runner.cleanup()


class ThumbnailRebuild:
    def __init__(self, settings, runtime, thumbnails):
        self.settings, self.runtime, self.thumbnails = settings, runtime, thumbnails
        self.path = settings.resolved_data_dir / "thumbnail-rebuild.json"
        self.directory = settings.resolved_cache_dir / "thumbnails"
        self.staging = covers.CachedThumbnailRepair(self.directory)
        self.state = {
            "status": "idle",
            "phase": "Ready",
            "total": 0,
            "processed": 0,
            "generated": 0,
            "skipped": 0,
            "failed": 0,
            "source_bytes": 0,
        }
        self.task = None
        self.drive_id = None
        self.last_saved = 0
        self.stopping = False
        self.start_lock = asyncio.Lock()

    def snapshot(self):
        return copy.deepcopy(self.state)

    async def restore(self):
        try:
            if await asyncio.to_thread(lambda: self.path.stat().st_size) > 16384:
                return
            value = json.loads(await asyncio.to_thread(self.path.read_text))
            if isinstance(value, dict) and value.get("status") in {
                "running",
                "completed",
                "cancelled",
                "interrupted",
                "failed",
            }:
                self.state = value
                if value["status"] == "running":
                    self.state.update(
                        status="interrupted",
                        phase="Server restarted; start a new rebuild",
                        finished_at=time.time(),
                    )
                    await self.persist()
        except OSError, ValueError:
            pass

    async def persist(self):
        try:
            await covers._owned_disk_call(write_state, self.path, self.snapshot())
            self.state.pop("persistence_warning", None)
        except OSError:
            self.state["persistence_warning"] = (
                "Progress could not be saved to disk; keep this server running."
            )
        self.last_saved = time.monotonic()

    async def persist_progress(self):
        if time.monotonic() - self.last_saved >= 1:
            await self.persist()

    def busy(self):
        runtime = self.runtime()
        registry = getattr(runtime, "registry", None)
        if registry is not None and any(
            str(row.status) == "running" for row in registry.list()
        ):
            return True
        pool = getattr(runtime, "pool", None)
        if callable(getattr(pool, "performance_snapshot", None)):
            return bool(pool.performance_snapshot().get("active", 0))
        return False

    async def wait_turn(self):
        while self.busy():
            self.state["phase"] = "Waiting for active transfers or playback"
            await self.persist_progress()
            await asyncio.sleep(0.5)
        self.state["phase"] = "Regenerating thumbnail"

    async def start(self):
        async with self.start_lock:
            return await self._start()

    async def _start(self):
        if self.task is not None and not self.task.done():
            return self.snapshot()
        if (
            getattr(self.runtime(), "store", None) is None
            or getattr(self.runtime(), "pool", None) is None
        ):
            raise HTTPException(503, "TGDrive is not ready")
        self.state = {
            "id": secrets.token_urlsafe(12),
            "status": "running",
            "phase": "Finding files",
            "total": 0,
            "processed": 0,
            "generated": 0,
            "skipped": 0,
            "failed": 0,
            "source_bytes": 0,
            "current_bytes": 0,
            "started_at": time.time(),
            "recent_issues": [],
        }
        self.stopping = False
        await self.persist()
        self.task = asyncio.create_task(self.run(), name="tgdrive-thumbnail-rebuild")
        return self.snapshot()

    async def render_file(self, source):
        data = source.data
        suffix = Path(data["name"]).suffix.lower()
        temporary = output = None
        try:
            if (
                suffix in IMAGE_CODECS
                or suffix in VIDEO_FORMATS
                or suffix in AUDIO_FORMATS
            ):
                url = await source.open()
                return await covers.generate(
                    Path("remote"),
                    data["name"],
                    background=True,
                    remote_url=url,
                    source_size=data["size"],
                )
            if suffix in {".epub", ".cbz", ".cbr"} | OFFICE_FORMATS:
                if suffix in {".epub", ".cbz"} | OFFICE_FORMATS:
                    result = await parse_book(data, source.read, thumbnail=True)
                else:
                    index = await parse_book(data, source.read)
                    entry = index["entries"][0]["name"]
                    result = await parse_book(data, source.read, entry)
                if result.get("kind") != "image":
                    return None
                content = base64.b64decode(result["data"], validate=True)
                suffix = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                    "image/avif": ".avif",
                }.get(result["mime"])
                if suffix is None:
                    return None
            elif suffix == ".pdf":
                if data["size"] > PDF_BUDGET:
                    raise SourceLimit("PDF exceeds 256 MiB rebuild limit")
                content = None
            else:
                return None
            required = len(content) if content is not None else data["size"]
            opening = asyncio.create_task(
                asyncio.to_thread(self.staging._open_source, required, 64 * BLOCK)
            )
            opened, interrupted = await covers._finish_owned(opening)
            if opened is not None:
                output, temporary = opened
            if interrupted:
                raise asyncio.CancelledError
            if content is not None:
                await covers._owned_disk_call(output.write, content)
            else:
                for start in range(0, data["size"], BLOCK):
                    block = await source.read(start, min(BLOCK, data["size"] - start))
                    await covers._owned_disk_call(output.write, block)
            await covers._owned_disk_call(output.flush)
            return await covers.generate(temporary, "cover" + suffix, background=True)
        finally:
            await source.close()
            if output is not None:
                await covers._owned_disk_call(
                    self.staging._cleanup_source, output, temporary
                )

    async def run(self):
        priority = media_priority.set("preload")
        try:
            self.drive_id = self.runtime().store.drive_id
            pending, visited, files = ["/"], set(), set()
            while pending:
                path = pending.pop()
                if path in visited:
                    continue
                visited.add(path)
                try:
                    rows = await asyncio.to_thread(
                        self.runtime().store.list_directory, path
                    )
                except NotFoundError, NotDirectoryError, TrashedItemError:
                    # A concurrent move/deletion can invalidate this directory.
                    continue
                for row in rows.values():
                    if row["type"] == "file":
                        files.add(row["id"])
                    else:
                        pending.append(path.rstrip("/") + "/" + row["id"])
                self.state["total"] = len(files)
                await self.persist_progress()
                await asyncio.sleep(0)
            await self.persist()
            for item_id in sorted(files):
                try:
                    store = self.runtime().store
                    data = store.get_file(store.resolve_id(item_id))
                except NotFoundError, NotFileError, TrashedItemError:
                    self.state["skipped"] += 1
                    self.state["processed"] += 1
                    await self.persist()
                    continue
                self.state.update(current_name=data["name"], current_bytes=0)
                if Path(data["name"]).suffix.lower() not in SUPPORTED:
                    self.state["skipped"] += 1
                else:
                    await self.wait_turn()
                    source = RebuildSource(self, data)
                    try:
                        async with asyncio.timeout(240):
                            cover = await self.render_file(source)
                        await source.authorize()
                        if cover:
                            await asyncio.to_thread(
                                covers.save,
                                self.directory,
                                self.settings.storage_channel,
                                data["message_id"],
                                cover,
                            )
                            stored = await asyncio.to_thread(
                                covers.read,
                                self.directory,
                                self.settings.storage_channel,
                                data["message_id"],
                            )
                            if stored != cover:
                                raise SourceLimit(
                                    "Thumbnail cache is full or unavailable"
                                )
                            await self.thumbnails.invalidate_file(
                                self.settings.storage_channel, data["message_id"]
                            )
                            self.state["generated"] += 1
                        else:
                            self.state["skipped"] += 1
                            self.issue(
                                data["name"],
                                "No cover within the rendering/read limits",
                            )
                    except SourceLimit as error:
                        self.state["skipped"] += 1
                        self.issue(data["name"], str(error))
                    except Exception:
                        self.state["failed"] += 1
                        self.issue(
                            data["name"],
                            "File unavailable or thumbnail generation failed",
                        )
                self.state["processed"] += 1
                await self.persist()
                await asyncio.sleep(0.1)
            self.state.update(
                status="completed",
                phase="Finished",
                current_name="",
                finished_at=time.time(),
            )
        except asyncio.CancelledError:
            self.state.update(
                status="interrupted" if self.stopping else "cancelled",
                phase="Stopped",
                finished_at=time.time(),
            )
        except Exception:
            self.state.update(
                status="failed",
                phase="Could not continue; start a new rebuild",
                finished_at=time.time(),
            )
        finally:
            media_priority.reset(priority)
            await self.persist()

    def issue(self, name, reason):
        self.state["recent_issues"] = [
            *self.state["recent_issues"],
            {"name": name, "reason": reason},
        ][-5:]

    async def cancel(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            if self.state["status"] == "running":
                self.state.update(
                    status="interrupted" if self.stopping else "cancelled",
                    phase="Stopped",
                    finished_at=time.time(),
                )
                await self.persist()
        return self.snapshot()

    async def close(self):
        self.stopping = True
        await self.cancel()


class RebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


def register_thumbnail_routes(app, settings, require_admin):
    manager = ThumbnailRebuild(
        settings, lambda: app.state.runtime, app.state.thumbnails
    )
    app.state.thumbnail_rebuild = manager
    dependencies = [Depends(require_admin)]

    @app.get("/api/v1/settings/thumbnails/rebuild", dependencies=dependencies)
    async def status():
        return manager.snapshot()

    @app.post(
        "/api/v1/settings/thumbnails/rebuild",
        dependencies=dependencies,
        status_code=202,
    )
    async def start(body: RebuildRequest):
        return await manager.start()

    @app.post("/api/v1/settings/thumbnails/rebuild/cancel", dependencies=dependencies)
    async def cancel():
        return await manager.cancel()
