"""Authenticated controls for disposable media storage and origin capacity."""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tgdrive.settings.performance_preferences import get_performance_settings


class PrepareFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class ClearCache(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str | None = Field(default=None, min_length=1, max_length=128)


def system_snapshot(settings):
    try:
        usage = shutil.disk_usage(settings.resolved_data_dir)
        disk = {
            "available": True,
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_bytes": usage.used,
        }
    except OSError:
        disk = {"available": False, "total_bytes": 0, "free_bytes": 0, "used_bytes": 0}
    memory = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, value = line.split(":", 1)
            if name in {"MemTotal", "MemAvailable"}:
                memory[name] = int(value.strip().split()[0]) * 1024
    except OSError, ValueError:
        pass
    # Container limits matter more than host-wide free memory.
    for name, filename in (
        ("cgroup_limit", "memory.max"),
        ("cgroup_current", "memory.current"),
    ):
        try:
            raw = (Path("/sys/fs/cgroup") / filename).read_text().strip()
            memory[name] = int(raw) if raw != "max" else None
        except OSError, ValueError:
            pass
    try:
        load = list(os.getloadavg())
    except OSError, AttributeError:
        load = []
    current_rss = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                current_rss = int(line.split()[1]) * 1024
                break
    except OSError, ValueError, IndexError:
        pass
    return {
        "at": time.time(),
        "memory": memory,
        "process_rss_bytes": current_rss,
        "cpu_count": os.cpu_count(),
        "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * 1024,
        "load_average": load,
        "disk": disk,
        "staging_budget_bytes": settings.staging_budget_bytes,
    }


def register_performance_routes(
    app, settings, *, require_admin, get_runtime, get_store
):
    admin = [Depends(require_admin)]

    async def service(request):
        value = get_performance_settings(settings, get_runtime(request))
        await value.initialize()
        return value

    def response(value, **headers):
        return JSONResponse(value, headers={"Cache-Control": "no-store", **headers})

    def media_manager(request):
        manager = getattr(get_runtime(request), "preloads", None)
        if manager is None:
            raise HTTPException(503, "Media storage is not available yet")
        return manager

    @app.get("/api/v1/settings/performance", dependencies=admin)
    async def preferences(request: Request):
        value = await service(request)
        return response(await value.snapshot(), ETag=value.etag())

    @app.patch("/api/v1/settings/performance", dependencies=admin)
    async def update_preferences(request: Request, changes: dict[str, Any]):
        value = await service(request)
        try:
            result = await value.save(changes, expected=request.headers.get("if-match"))
        except ValidationError:
            raise HTTPException(
                422, "Invalid performance settings. Check values and supported limits."
            ) from None
        return response(result, ETag=value.etag())

    @app.get("/api/v1/preloads", dependencies=admin)
    async def preloads(request: Request):
        await service(request)
        return response(await media_manager(request).snapshot())

    @app.post("/api/v1/preloads", dependencies=admin, status_code=202)
    async def prepare(payload: PrepareFile, request: Request):
        await service(request)
        store, runtime = get_store(request), get_runtime(request)
        manager = media_manager(request)
        path = store.resolve_id(payload.path.rstrip("/").rsplit("/", 1)[-1])
        data = store.get_file(path)

        async def authorize():
            # Deletion, trash and recovery must stop background work too.
            if getattr(getattr(runtime, "backup", None), "recovery_required", False):
                raise HTTPException(409, "Drive recovery is required")
            current = await asyncio.to_thread(
                store.get_file, store.resolve_id(data["id"]) if "id" in data else path
            )
            if any(
                current.get(key) != data.get(key)
                for key in ("message_id", "size", "parts")
            ):
                raise HTTPException(
                    409, "The file changed during preparation; request the new version"
                )

        try:
            job = await manager.prepare(
                channel_id=settings.storage_channel,
                message_id=data["message_id"],
                size=data["size"],
                name=data["name"],
                parts=data.get("parts"),
                authorize=authorize,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return JSONResponse(job, status_code=202, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/preloads/clear", dependencies=admin)
    async def clear(payload: ClearCache, request: Request):
        await service(request)
        await media_manager(request).clear(payload.file_id)
        return response({"status": "ok"})

    @app.post("/api/v1/preloads/{job_id}/cancel", dependencies=admin)
    async def cancel(job_id: str, request: Request):
        await service(request)
        try:
            await media_manager(request).cancel(job_id)
        except KeyError:
            raise HTTPException(404, "Preload job not found") from None
        return response({"status": "ok"})

    @app.get("/api/v1/performance", dependencies=admin)
    async def performance(request: Request):
        value = await service(request)
        runtime, store = get_runtime(request), get_store(request)
        pool = getattr(runtime, "pool", None)
        snapshot = getattr(pool, "performance_snapshot", None)
        cache = getattr(pool, "_media_read_cache", None)
        statistics = getattr(store, "statistics", None)
        drive = await asyncio.to_thread(statistics) if callable(statistics) else {}
        return response(
            {
                "telegram": snapshot() if callable(snapshot) else {},
                "cache": await cache.snapshot()
                if cache is not None
                else {
                    "policy": value.value.cache_policy().as_dict(),
                    "memory": {
                        "bytes": 0,
                        "max_bytes": value.value.cache_memory_bytes,
                        "effective_max_bytes": 0,
                        "entries": 0,
                        "hits": 0,
                        "misses": 0,
                    },
                    "disk": {
                        "available": False,
                        "bytes": 0,
                        "max_bytes": value.value.cache_disk_bytes,
                    },
                    "files": [],
                    "pending_writes": 0,
                },
                "server": (await asyncio.to_thread(system_snapshot, settings))
                | request.app.state.traffic.resources.value,
                "drive": drive,
                "traffic": request.app.state.traffic.snapshot(),
                "history_scope": (
                    "Last 5 minutes; sampled every 3 seconds; resets on restart."
                ),
            }
        )

    @app.get("/api/v1/performance/history", dependencies=admin)
    async def performance_history(request: Request, start: float, end: float):
        from tgdrive.observability.telemetry_history import validate_range

        try:
            validate_range(start, end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            result = await asyncio.to_thread(
                request.app.state.telemetry_history.query, start, end
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(
                503, "Performance history is temporarily unavailable. Try again."
            ) from None
        return response(result)
