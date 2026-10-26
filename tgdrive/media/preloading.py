"""Bounded, cancellable preparation sharing the ordinary media block cache.

Authorization remains the caller's responsibility. A caller may supply a fresh
authorization callback, which is checked before work and before every block.
No HEAD request or metadata-only observation starts a bulk download.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
import time
from collections import OrderedDict

from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import BLOCK, MediaReadCache, stream_pooled_media
from tgdrive.storage.file_parts import validate_parts
from tgdrive.telegram.clients import _block_identity

PLAYBACK_RANGE_GRACE_SECONDS = 5.0


class PreloadCapacityError(ValueError):
    pass


def _cache_footprint(size, parts, start, end):
    """Count retained Telegram blocks, including partial boundary blocks."""
    payload_bytes = block_count = 0
    for part in parts or [{"offset": 0, "size": size}]:
        lo = max(0, start - part["offset"])
        hi = min(part["size"], end - part["offset"])
        if hi <= lo:
            continue
        first = lo // BLOCK
        final = (hi + BLOCK - 1) // BLOCK
        payload_bytes += min(part["size"], final * BLOCK) - first * BLOCK
        block_count += final - first
    return payload_bytes, block_count


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
        self._viewer_timers = {}
        self._closed = False
        self._condition = asyncio.Condition()
        self._admission_lock = asyncio.Lock()
        self._active = 0
        self._clock = clock or time.monotonic
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
        media_rate=None,
        allow_full=True,
    ):
        """Record a request; only delivered sequential bytes can trigger warming."""
        identity = self.register_file(
            channel_id=channel_id,
            message_id=message_id,
            size=size,
            name=name,
            parts=parts,
        )
        if method != "GET" or self._closed or not self.policy.auto_preload:
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
                "last_auto_range": None,
                "authorize": authorize,
                "requests": 0,
                "viewers": {},
                "runs": [],
                "hot_until": 0,
                "fill_rate": 0.0,
            }
            self._observed[identity] = row
        row["authorize"] = authorize
        row["requests"] += 1
        viewer = self._get_viewer(row, request_id)
        viewer["authorize"] = authorize
        viewer["allow_full"] = allow_full
        if (
            type(media_rate) in (int, float)
            and math.isfinite(media_rate)
            and media_rate > 0
        ):
            viewer["media_rate"] = media_rate
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
            "sample_at": None,
            "sample_bytes": 0,
            "rate": 0.0,
            "media_rate": None,
            "authorize": None,
            "allow_full": True,
            "active": 0,
            "warm_until": 0.0,
        }

    def _get_viewer(self, row, request_id):
        viewers = row["viewers"]
        key = request_id or "default"
        viewer = viewers.pop(key, None)
        if viewer is None:
            viewer = self._viewer()
        viewer["last_seen"] = self._clock()
        viewers[key] = viewer
        self._prune_viewers(row, keep=key)
        return viewer

    def _prune_viewers(self, row, *, keep=None):
        # Viewer IDs are request observations, not permanent analytics. Keep
        # recent range/replay continuity without retaining every past visitor.
        # Active HTTP consumers stay tracked even beyond the idle-history cap.
        viewers = row["viewers"]
        cutoff = self._clock() - 600
        for key, viewer in list(viewers.items()):
            if (
                key != keep
                and not self._viewer_active(viewer)
                and (viewer.get("last_seen", 0) < cutoff or len(viewers) > 128)
            ):
                viewers.pop(key)

    def _viewer_active(self, viewer):
        return bool(viewer["active"] or viewer["warm_until"] > self._clock())

    def _cancel_viewer_timer(self, file_id, request_id):
        timer = self._viewer_timers.pop((file_id, request_id), None)
        if timer is not None:
            timer.cancel()

    def _expire_viewer(self, file_id, request_id):
        self._viewer_timers.pop((file_id, request_id), None)
        row = self._observed.get(file_id)
        viewer = row["viewers"].get(request_id) if row is not None else None
        if viewer is not None and not viewer["active"]:
            viewer["warm_until"] = 0.0
            self.finished(file_id, request_id)

    def begin(self, file_id, request_id):
        row = self._observed.get(file_id)
        if row is not None:
            viewer = self._get_viewer(row, request_id)
            self._cancel_viewer_timer(file_id, request_id or "default")
            viewer["warm_until"] = 0.0
            viewer["active"] += 1
            return row
        return None

    def finished(self, file_id, request_id, *, completed=False, observation=None):
        row = self._observed.get(file_id)
        if row is None or (observation is not None and row is not observation):
            return
        viewer = row["viewers"].get(request_id or "default")
        if viewer is not None:
            viewer["active"] = max(0, viewer["active"] - 1)
            viewer["last_seen"] = self._clock()
            if not viewer["active"]:
                self._cancel_viewer_timer(file_id, request_id or "default")
                viewer["warm_until"] = (
                    self._clock() + PLAYBACK_RANGE_GRACE_SECONDS if completed else 0.0
                )
                if completed:
                    # A completed finite HTTP range is not a closed player.
                    # Retain only a short, bounded bridge to its next range.
                    key = (file_id, request_id or "default")
                    self._viewer_timers[key] = asyncio.get_running_loop().call_later(
                        PLAYBACK_RANGE_GRACE_SECONDS, self._expire_viewer, *key
                    )
        self._prune_viewers(row)
        any_active = any(self._viewer_active(item) for item in row["viewers"].values())
        existing = self._existing(file_id)
        if (
            existing is not None
            and existing["mode"] == "auto"
            and existing["target_bytes"] < existing["size"]
            and (
                not any_active
                or (
                    existing.get("viewer_id") == (request_id or "default")
                    and (viewer is None or not self._viewer_active(viewer))
                )
            )
        ):
            task = self._tasks.get(existing["id"])
            if task is not None:
                task.cancel()
        if not any_active and row["hot_until"] <= self._clock():
            task = self._auto_tasks.get(file_id)
            if task is not None:
                task.cancel()

    def delivered(
        self,
        *,
        channel_id,
        message_id,
        offset,
        length,
        request_id=None,
        observation=None,
    ):
        if not self.policy.auto_preload or self._closed:
            return
        for identity, row in list(self._observed.items()):
            if row["channel_id"] != channel_id or (
                observation is not None and row is not observation
            ):
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
            viewer = self._get_viewer(row, viewer_id)
            now = self._clock()
            if viewer["last_end"] == logical_offset:
                viewer["sequential"] += length
                if viewer["at"] is not None and now - viewer["at"] > 30:
                    viewer["rate"] = 0.0
                    viewer["sample_at"] = now
                    viewer["sample_bytes"] = 0
                else:
                    viewer["sample_bytes"] += length
                    elapsed = now - viewer["sample_at"]
                    # Aggregate short bursts over a real sample interval instead
                    # of mistaking each cached yield for the media's bitrate.
                    if elapsed >= 0.05:
                        rate = viewer["sample_bytes"] / elapsed
                        viewer["rate"] = (
                            rate
                            if not viewer["rate"]
                            else 0.75 * viewer["rate"] + 0.25 * rate
                        )
                        viewer["sample_at"] = now
                        viewer["sample_bytes"] = 0
            else:
                viewer["rate"] = 0.0
                viewer["sample_at"] = now
                viewer["sample_bytes"] = 0
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
            viewer["at"] = now
            viewer["last_end"] = logical_offset + length
            rate = viewer["media_rate"] or viewer["rate"]
            qualification = (
                max(BLOCK, int(min(BLOCK, viewer["media_rate"]) * 4))
                if viewer["media_rate"]
                else 4 * BLOCK
            )
            if viewer["sequential"] < qualification:
                continue
            row["runs"] = [run for run in row["runs"] if now - run["at"] <= 600]
            run = viewer["run"]
            if run is None:
                run = {
                    "viewer": viewer_id,
                    "start": viewer["run_start"],
                    "end": viewer["last_end"],
                    "at": now,
                    "allow_full": viewer["allow_full"],
                }
                row["runs"].append(run)
                row["runs"] = row["runs"][-32:]
                viewer["run"] = run
            else:
                run.update(end=viewer["last_end"], at=now)
            if viewer["allow_full"] and any(
                previous["allow_full"]
                and previous["viewer"] != viewer_id
                and min(previous["end"], run["end"])
                - max(previous["start"], run["start"])
                >= qualification
                for previous in row["runs"]
            ):
                row["hot_until"] = now + 600
            hot = row["hot_until"] > now
            promote = hot and row["last_auto_mode"] != "full"
            previous_range = row["last_auto_range"]
            renew = (
                row["last_auto_mode"] == "ahead"
                and previous_range is not None
                and (
                    viewer["last_end"] < previous_range[0]
                    or (
                        previous_range[1] < row["size"]
                        and viewer["last_end"]
                        >= previous_range[1]
                        - (previous_range[1] - previous_range[0]) // 2
                    )
                )
            )
            # Renew as playback consumes the warmed window. A fixed cooldown
            # alone leaves fast readers without ahead bytes for up to 30s.
            if now - row["last_auto"] < 30 and not (promote or renew):
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
            row["last_auto_range"] = None
            context = dict(row) | {
                "last_end": viewer["last_end"],
                "rate": rate,
                "viewer_id": viewer_id,
                "authorize": viewer["authorize"],
                "hot": hot,
            }
            task = asyncio.create_task(self._auto(identity, context))
            self._auto_tasks[identity] = task

            def completed(done, key=identity):
                if self._auto_tasks.get(key) is done:
                    self._auto_tasks.pop(key, None)

            task.add_done_callback(completed)

    def _ahead_window(self, capacity, consumption_rate, fill_rate=0):
        seconds = 16 if fill_rate and fill_rate < consumption_rate else 8
        bounded_rate = min(consumption_rate, 128 * BLOCK / seconds)
        desired = max(BLOCK, math.ceil(bounded_rate * seconds / BLOCK) * BLOCK)
        return min(128 * BLOCK, int(capacity * 0.25), desired)

    async def _auto(self, identity, row):
        try:
            capacity = await self._capacity()
            current = self._observed.get(identity)
            viewer = (
                current["viewers"].get(row["viewer_id"])
                if current is not None
                else None
            )
            if viewer is not None:
                # Capacity inspection yields; playback may have advanced since
                # the delivery that scheduled this work.
                row = row | {
                    "last_end": viewer["last_end"],
                    "rate": viewer["media_rate"] or viewer["rate"],
                    "authorize": viewer["authorize"],
                }
            full_bytes, full_blocks = _cache_footprint(
                row["size"], row["parts"], 0, row["size"]
            )
            full = (
                row["hot"]
                and full_bytes + full_blocks * 32
                <= capacity * self.policy.max_file_fraction
            )
            start = 0 if full else row["last_end"]
            ahead = self._ahead_window(
                capacity, row["rate"], current["fill_rate"] if current else 0
            )
            if not full and (
                ahead < BLOCK
                or current is None
                or viewer is None
                or not self._viewer_active(viewer)
            ):
                return
            end = row["size"] if full else min(row["size"], start + ahead)
            if end > start:
                job = await self.prepare(
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
                    current["last_auto_range"] = (job["start"], job["end"])
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
        # Capacity checks yield to disk I/O. Serialize job admission so two
        # simultaneous preparations cannot create duplicate jobs/reservations.
        async with self._admission_lock:
            identity = self.register_file(
                channel_id=channel_id,
                message_id=message_id,
                size=size,
                name=name,
                parts=parts,
            )
            existing = self._existing(identity)
            if existing is not None:
                if mode == "manual" and existing["mode"] == "auto":
                    await self.cancel(existing["id"])
                else:
                    return dict(existing)
            capacity = await self._capacity()
            if self._closed:
                raise ValueError("Media preparation is stopped")
            if mode == "auto" and end - start < size:
                observation = self._observed.get(identity)
                viewer = (
                    observation["viewers"].get(viewer_id or "default")
                    if observation is not None
                    else None
                )
                if viewer is None or not self._viewer_active(viewer):
                    # Its viewer can leave while admission waits for the lock,
                    # authorization or disk capacity. Hot whole-file work may
                    # outlive a viewer; speculative ahead work may not.
                    raise ValueError("Automatic read-ahead has no active viewer")
                if not start <= viewer["last_end"] < end:
                    raise ValueError("Playback moved beyond this read-ahead range")
            # RAM stores payloads directly; only disk entries need digest prefixes.
            retained_bytes, blocks = _cache_footprint(size, parts, start, end)
            overhead = (
                blocks * 32 if capacity > self.cache.effective_memory_bytes else 0
            )
            if retained_bytes + overhead > capacity:
                raise PreloadCapacityError(
                    "This preparation exceeds the available cache budget; "
                    "increase the budget or free space"
                )
            reserved = sum(
                job["reservation_bytes"] + job.get("reservation_overhead", 0)
                for job in self._jobs.values()
                if job["state"] in {"queued", "preparing"}
            )
            if reserved + retained_bytes + overhead > capacity:
                raise PreloadCapacityError(
                    "Active preparations have reserved the available cache budget; "
                    "try again later"
                )
            pending = sum(
                row["state"] in {"queued", "preparing"} for row in self._jobs.values()
            )
            if pending >= self.policy.max_background_jobs * 4:
                raise PreloadCapacityError(
                    "The preparation queue is full; try again later"
                )
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
                "reservation_bytes": retained_bytes,
                "reservation_overhead": overhead,
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
            fill_bytes = 0
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
                    from tgdrive.media.media_metadata import get_message
                    from tgdrive.media.media_scheduler import media_priority

                    token = media_priority.set("preload")
                    try:
                        message = await get_message(
                            client, channel_id, part["message_id"]
                        )
                    finally:
                        media_priority.reset(token)
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
                        fill_bytes += len(data)
                        if now - previous_fill >= 0.05:
                            rate = fill_bytes / (now - previous_fill)
                            observation = self._observed.get(row["file_id"])
                            if observation is not None:
                                previous_rate = observation["fill_rate"]
                                observation["fill_rate"] = (
                                    rate
                                    if not previous_rate
                                    else 0.75 * previous_rate + 0.25 * rate
                                )
                            previous_fill = now
                            fill_bytes = 0
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
        for identity, viewer in list(self._viewer_timers):
            if file_id is None or identity == file_id:
                self._cancel_viewer_timer(identity, viewer)
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
            for identity, viewer in list(self._viewer_timers):
                self._cancel_viewer_timer(identity, viewer)
            self._observed.clear()
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
        for identity, viewer in list(self._viewer_timers):
            self._cancel_viewer_timer(identity, viewer)
        tasks = list(self._auto_tasks.values()) + list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.cache.close()
