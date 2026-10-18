"""Bounded, cancellable preparation sharing the ordinary media block cache.

Authorization remains the caller's responsibility. A caller may supply a fresh
authorization callback, which is checked before work and before every block.
No HEAD request or metadata-only observation starts a bulk download.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import OrderedDict

from tgdrive.file_parts import validate_parts
from tgdrive.media_policy import MediaCachePolicy
from tgdrive.media_reader import BLOCK, MediaReadCache, stream_pooled_media
from tgdrive.telegram_clients import _block_identity


class PreloadCapacityError(ValueError):
    pass


class PreloadManager:
    def __init__(self, pool, *, policy=None, clock=None):
        self.pool = pool
        self.policy = policy or MediaCachePolicy()
        if pool._media_read_cache is None:
            pool._media_read_cache = MediaReadCache(pool.media_cache_dir, self.policy)
        self.cache = pool._media_read_cache
        self._jobs = OrderedDict()
        self._tasks = {}
        self._observed = OrderedDict()
        self._auto_tasks = {}
        self._closed = False
        self._condition = asyncio.Condition()
        self._active = 0
        self._clock = clock or time.monotonic
        self._fill_rate = 0.0
        pool._preload_manager = self

    def register_file(self, **kwargs):
        return self.cache.register_file(**kwargs)

    def observe(
        self,
        *,
        channel_id,
        message_id,
        size,
        name="",
        parts=None,
        start=0,
        end=None,
        method="GET",
        authorize=None,
        request_id=None,
    ):
        """Record a request; only delivered sequential bytes can trigger warming."""
        identity = self.register_file(
            channel_id=channel_id,
            message_id=message_id,
            size=size,
            name=name,
            parts=parts,
        )
        if method != "GET" or self._closed:
            return identity
        row = self._observed.get(identity)
        if row is None:
            row = {
                "channel_id": channel_id,
                "message_id": message_id,
                "size": size,
                "name": name,
                "parts": parts,
                "last_auto": float("-inf"),
                "last_auto_mode": None,
                "authorize": authorize,
                "requests": 0,
                "viewers": {},
                "runs": [],
                "hot_until": 0,
            }
            self._observed[identity] = row
        row["authorize"] = authorize
        row["requests"] += 1
        row["viewers"].setdefault(request_id or "default", self._viewer())
        self._observed.move_to_end(identity)
        # Observations do not retain payloads; bound idle request metadata too.
        while len(self._observed) > 1024:
            self._observed.popitem(last=False)
        return identity

    @staticmethod
    def _viewer():
        return {
            "last_end": None,
            "sequential": 0,
            "run_start": 0,
            "run": None,
            "at": None,
            "rate": 0.0,
            "active": 0,
        }

    def begin(self, file_id, request_id):
        row = self._observed.get(file_id)
        if row is not None:
            viewer = row["viewers"].setdefault(request_id or "default", self._viewer())
            viewer["active"] += 1

    def finished(self, file_id, request_id):
        row = self._observed.get(file_id)
        if row is None:
            return
        viewer = row["viewers"].get(request_id or "default")
        if viewer is not None:
            viewer["active"] = max(0, viewer["active"] - 1)
        if not any(viewer["active"] for viewer in row["viewers"].values()):
            existing = self._existing(file_id)
            if (
                existing is not None
                and existing["mode"] == "auto"
                and existing["target_bytes"] < existing["size"]
            ):
                task = self._tasks.get(existing["id"])
                if task is not None:
                    task.cancel()
            if row["hot_until"] <= self._clock():
                task = self._auto_tasks.get(file_id)
                if task is not None:
                    task.cancel()

    def delivered(self, *, channel_id, message_id, offset, length, request_id=None):
        if not self.policy.auto_preload or self._closed:
            return
        for identity, row in list(self._observed.items()):
            if row["channel_id"] != channel_id:
                continue
            logical_offset = offset
            if row["parts"]:
                part = next(
                    (p for p in row["parts"] if p["message_id"] == message_id), None
                )
                if part is None:
                    continue
                logical_offset += part["offset"]
            elif row["message_id"] != message_id:
                continue
            viewer_id = request_id or "default"
            viewer = row["viewers"].setdefault(viewer_id, self._viewer())
            now = self._clock()
            if viewer["at"] is not None and now > viewer["at"]:
                rate = length / (now - viewer["at"])
                viewer["rate"] = (
                    rate if not viewer["rate"] else 0.75 * viewer["rate"] + 0.25 * rate
                )
            viewer["at"] = now
            if viewer["last_end"] == logical_offset:
                viewer["sequential"] += length
            else:
                viewer["sequential"] = length
                viewer["run_start"] = logical_offset
                viewer["run"] = None
                # A distant seek abandons only speculative ahead work; shared
                # fill subscribers belonging to other viewers remain alive.
                existing = self._existing(identity)
                if (
                    existing is not None
                    and existing["mode"] == "auto"
                    and existing["target_bytes"] < existing["size"]
                    and existing.get("viewer_id") == viewer_id
                    and not existing["start"] <= logical_offset < existing["end"]
                ):
                    task = self._tasks.get(existing["id"])
                    if task is not None:
                        task.cancel()
            viewer["last_end"] = logical_offset + length
            if viewer["sequential"] < 4 * BLOCK:
                continue
            row["runs"] = [run for run in row["runs"] if now - run["at"] <= 600]
            run = viewer["run"]
            if run is None:
                run = {
                    "viewer": viewer_id,
                    "start": viewer["run_start"],
                    "end": viewer["last_end"],
                    "at": now,
                }
                if any(
                    min(previous["end"], run["end"])
                    - max(previous["start"], run["start"])
                    >= 4 * BLOCK
                    for previous in row["runs"]
                ):
                    row["hot_until"] = now + 600
                row["runs"].append(run)
                row["runs"] = row["runs"][-32:]
                viewer["run"] = run
            else:
                run.update(end=viewer["last_end"], at=now)
            hot = row["hot_until"] > now
            promote = hot and row["last_auto_mode"] != "full"
            if now - row["last_auto"] < 30 and not promote:
                continue
            existing = self._existing(identity)
            if existing is not None:
                if (
                    not promote
                    or existing["mode"] != "auto"
                    or existing["target_bytes"] == existing["size"]
                ):
                    continue
                task = self._tasks.get(existing["id"])
                if task is not None:
                    existing["state"] = "cancelled"
                    task.cancel()
            if identity in self._auto_tasks:
                if not promote:
                    continue
                self._auto_tasks[identity].cancel()
            if len(self._auto_tasks) >= self.policy.max_background_jobs:
                continue
            row["last_auto"] = now
            context = dict(row) | {
                "last_end": viewer["last_end"],
                "rate": viewer["rate"],
                "viewer_id": viewer_id,
                "hot": hot,
            }
            task = asyncio.create_task(self._auto(identity, context))
            self._auto_tasks[identity] = task

            def completed(done, key=identity):
                if self._auto_tasks.get(key) is done:
                    self._auto_tasks.pop(key, None)

            task.add_done_callback(completed)

    def _ahead_window(self, capacity, consumption_rate):
        seconds = 16 if self._fill_rate and self._fill_rate < consumption_rate else 8
        desired = max(4 * BLOCK, int(consumption_rate * seconds))
        return min(32 * BLOCK, int(capacity * 0.25), desired)

    async def _auto(self, identity, row):
        try:
            capacity = await self._capacity()
            full = (
                row["hot"]
                and row["size"] + ((row["size"] + BLOCK - 1) // BLOCK) * 32
                <= capacity * self.policy.max_file_fraction
            )
            start = 0 if full else row["last_end"]
            ahead = self._ahead_window(capacity, row["rate"])
            current = self._observed.get(identity)
            if not full and (
                ahead < 4 * BLOCK
                or current is None
                or not any(viewer["active"] for viewer in current["viewers"].values())
            ):
                return
            end = row["size"] if full else min(row["size"], start + ahead)
            if end > start:
                await self.prepare(
                    channel_id=row["channel_id"],
                    message_id=row["message_id"],
                    size=row["size"],
                    name=row["name"],
                    parts=row["parts"],
                    authorize=row["authorize"],
                    mode="auto",
                    start=start,
                    end=end,
                    viewer_id=row["viewer_id"],
                )
                if current is not None:
                    current["last_auto_mode"] = "full" if full else "ahead"
        except Exception:
            # Speculation is best effort and cannot fail an active response.
            return

    def _existing(self, identity):
        return next(
            (
                row
                for row in self._jobs.values()
                if row["file_id"] == identity
                and row["state"] in {"queued", "preparing"}
            ),
            None,
        )

    async def _capacity(self):
        snapshot = await self.cache.snapshot()
        disk = snapshot["disk"]
        disk_capacity = 0
        if disk["available"] and disk.get("free_bytes") is not None:
            disk_capacity = min(
                self.policy.disk_max_bytes,
                max(0, disk["free_bytes"] + disk["bytes"] - self.policy.min_free_bytes),
            )
        # The layers duplicate hot bytes, so adding their limits would overpromise
        # whole-file retention when the object fits neither individual tier.
        return max(disk_capacity, self.cache.effective_memory_bytes)

    async def prepare(
        self,
        *,
        channel_id,
        message_id,
        size,
        name="",
        parts=None,
        authorize=None,
        mode="manual",
        start=0,
        end=None,
        viewer_id=None,
    ):
        if self._closed:
            raise ValueError("Media preparation is stopped")
        if type(size) is not int or size <= 0:
            raise ValueError("A nonempty file is required")
        if parts is not None:
            parts = validate_parts(parts, size=size, message_id=message_id)
        end = size if end is None else end
        if not 0 <= start < end <= size:
            raise ValueError("Preparation range is outside the file")
        if authorize is not None:
            await authorize()
        identity = self.register_file(
            channel_id=channel_id,
            message_id=message_id,
            size=size,
            name=name,
            parts=parts,
        )
        existing = self._existing(identity)
        if existing is not None:
            if (
                mode == "manual"
                and existing["mode"] == "auto"
                and existing["target_bytes"] < size
            ):
                await self.cancel(existing["id"])
            else:
                return dict(existing)
        capacity = await self._capacity()
        if self._closed:
            raise ValueError("Media preparation is stopped")
        overhead = ((end - start + BLOCK - 1) // BLOCK) * 32
        if end - start + overhead > capacity:
            raise PreloadCapacityError(
                "This preparation exceeds the available cache budget; "
                "increase the budget or free space"
            )
        reserved = sum(
            job["target_bytes"]
            for job in self._jobs.values()
            if job["state"] in {"queued", "preparing"}
        )
        if reserved + end - start + overhead > capacity:
            raise PreloadCapacityError(
                "Active preparations have reserved the available cache budget; "
                "try again later"
            )
        pending = sum(
            row["state"] in {"queued", "preparing"} for row in self._jobs.values()
        )
        if pending >= self.policy.max_background_jobs * 4:
            raise PreloadCapacityError("The preparation queue is full; try again later")
        job_id = secrets.token_urlsafe(12)
        row = {
            "id": job_id,
            "file_id": identity,
            "name": name,
            "state": "queued",
            "mode": mode,
            "viewer_id": viewer_id,
            "bytes_done": 0,
            "size": size,
            "target_bytes": end - start,
            "start": start,
            "end": end,
            "created_at": time.time(),
            "error": None,
        }
        self._jobs[job_id] = row
        # Keep bounded completed history without forgetting active work.
        for old in list(self._jobs):
            if len(self._jobs) <= 128:
                break
            if self._jobs[old]["state"] not in {"queued", "preparing"}:
                self._jobs.pop(old)
        task = asyncio.create_task(
            self._run(row, channel_id, message_id, size, parts, authorize)
        )
        self._tasks[job_id] = task

        def completed(done, key=job_id):
            if done.cancelled() and row["state"] in {"queued", "preparing"}:
                row["state"] = "cancelled"
                row["finished_at"] = time.time()
            self._tasks.pop(key, None)

        task.add_done_callback(completed)
        return dict(row)

    async def _run(self, row, channel_id, message_id, size, parts, authorize):
        acquired = False
        try:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: (
                        self._active < self.policy.max_background_jobs or self._closed
                    )
                )
                if self._closed:
                    raise asyncio.CancelledError
                self._active += 1
                acquired = True
            row["state"] = "preparing"
            previous_fill = self._clock()
            manifest = parts or [{"message_id": message_id, "offset": 0, "size": size}]
            for part in manifest:
                lo, hi = (
                    max(row["start"], part["offset"]),
                    min(row["end"], part["offset"] + part["size"]),
                )
                if hi <= lo:
                    continue
                if authorize is not None:
                    await authorize()
                async with self.pool.lease(
                    priority="preload", respect_cooldown=True
                ) as client:
                    from tgdrive.media_metadata import get_message

                    message = await get_message(client, channel_id, part["message_id"])
                identity = _block_identity(message, 0) if message is not None else None
                if identity is None or identity[0][-2] != part["size"]:
                    raise OSError("Stored Telegram media is missing or changed")
                if "document_id" in part and identity[0][-3] != part["document_id"]:
                    raise OSError("Stored Telegram media was replaced")
                offset = (lo - part["offset"]) // BLOCK
                final = (hi - part["offset"] + BLOCK - 1) // BLOCK
                stream = stream_pooled_media(
                    self.pool,
                    client,
                    message,
                    channel=channel_id,
                    offset=offset,
                    limit=final - offset,
                    background=True,
                    authorize=authorize,
                    retain_hot=row["mode"] == "manual" or row["target_bytes"] == size,
                )
                digest = (
                    hashlib.sha256()
                    if lo == part["offset"]
                    and hi == part["offset"] + part["size"]
                    and "sha256" in part
                    else None
                )
                try:
                    async for data in stream:
                        now = self._clock()
                        if now > previous_fill:
                            rate = len(data) / (now - previous_fill)
                            self._fill_rate = (
                                rate
                                if not self._fill_rate
                                else 0.75 * self._fill_rate + 0.25 * rate
                            )
                        previous_fill = now
                        if digest is not None:
                            digest.update(data)
                        row["bytes_done"] = min(
                            row["target_bytes"], row["bytes_done"] + len(data)
                        )
                finally:
                    await stream.aclose()
                if digest is not None and digest.hexdigest() != part["sha256"]:
                    await self.cache.clear(row["file_id"])
                    raise OSError("Prepared Telegram part failed its checksum")
            await self.cache.flush()
            files = {
                entry["id"]: entry for entry in (await self.cache.snapshot())["files"]
            }
            cached = files.get(row["file_id"], {})
            if not any(
                start <= row["start"] and end >= row["end"]
                for start, end in cached.get("ready_ranges", [])
            ):
                raise PreloadCapacityError(
                    "Cache capacity changed or blocks were evicted during preparation; "
                    "free space or increase the budget"
                )
            row["state"] = "ready"
        except asyncio.CancelledError:
            row["state"] = "cancelled"
            raise
        except Exception as error:
            row["state"] = "failed"
            row["error"] = (
                str(error)
                if isinstance(error, (ValueError, OSError, PermissionError))
                else "Telegram preparation failed; retry later"
            )
        finally:
            row["finished_at"] = time.time()
            if acquired:
                async with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

    async def cancel(self, job_id):
        row = self._jobs.get(job_id)
        if row is None:
            raise KeyError("Preparation job not found")
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            row["state"] = "cancelled"
            row.setdefault("finished_at", time.time())
        return dict(row)

    async def clear(self, file_id=None):
        if file_id is None:
            self._observed.clear()
        else:
            self._observed.pop(file_id, None)
        for identity, task in list(self._auto_tasks.items()):
            if file_id is None or identity == file_id:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for row in list(self._jobs.values()):
            if (file_id is None or row["file_id"] == file_id) and row[
                "id"
            ] in self._tasks:
                await self.cancel(row["id"])
        await self.cache.clear(file_id)

    async def reconfigure(self, policy):
        self.policy = policy
        if not policy.auto_preload:
            for task in list(self._auto_tasks.values()):
                task.cancel()
            await asyncio.gather(
                *list(self._auto_tasks.values()), return_exceptions=True
            )
            for row in list(self._jobs.values()):
                if row["mode"] == "auto" and row["id"] in self._tasks:
                    await self.cancel(row["id"])
        await self.cache.reconfigure(policy)
        async with self._condition:
            self._condition.notify_all()

    async def snapshot(self):
        cache = await self.cache.snapshot()
        files = {row["id"]: row for row in cache["files"]}
        jobs = []
        for row in reversed(self._jobs.values()):
            cached = files.get(row["file_id"], {})
            jobs.append(
                dict(row)
                | {
                    "ready_bytes": cached.get("ready_bytes", 0),
                    "complete": cached.get("complete", False),
                }
            )
        return {
            "jobs": jobs,
            "active_jobs": self._active,
            "max_background_jobs": self.policy.max_background_jobs,
            "cache": cache,
        }

    async def close(self):
        self._closed = True
        tasks = list(self._auto_tasks.values()) + list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.cache.close()
