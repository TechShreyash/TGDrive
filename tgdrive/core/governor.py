"""Tune performance values from measured server resources while running."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

GIB = 1024**3
INTERVAL_SECONDS = 5
COOLDOWN_SECONDS = 60
CONCURRENCY = (
    "telegram_upload_pipeline",
    "telegram_request_limit",
    "telegram_media_per_bot",
    "remote_download_concurrency",
    "preload_max_jobs",
)
CPU_BOUND = {"preload_max_jobs"}
BYTES = (
    "cache_memory_bytes",
    "cache_disk_bytes",
    "cache_min_free_bytes",
    "min_free_bytes",
    "staging_budget_bytes",
)


def _words(path) -> list[str] | None:
    try:
        return Path(path).read_text().split()
    except OSError:
        return None


def memory(
    cgroup: str = "/sys/fs/cgroup", meminfo: str = "/proc/meminfo"
) -> tuple[int, int]:
    """Return (limit, available) bytes; cgroup v2 first, then the host."""
    limit, current = _words(f"{cgroup}/memory.max"), _words(f"{cgroup}/memory.current")
    if limit and current and limit[0] != "max":
        return int(limit[0]), max(0, int(limit[0]) - int(current[0]))
    rows = {}
    for line in Path(meminfo).read_text().splitlines():
        name, _, rest = line.partition(":")
        if rest.split():
            rows[name] = int(rest.split()[0]) * 1024
    return rows["MemTotal"], rows["MemAvailable"]


def cpus(path: str = "/sys/fs/cgroup/cpu.max") -> int:
    row = _words(path)
    if row and row[0] != "max":
        return max(1, -(-int(row[0]) // int(row[1])))
    return os.cpu_count() or 1


def measure(directory) -> dict:
    limit, available = memory()
    disk = shutil.disk_usage(directory)
    return dict(
        memory_limit=limit,
        memory_available=available,
        cpus=cpus(),
        disk_total=disk.total,
        disk_free=disk.free,
    )


def _gib(value: int) -> str:
    return f"{value / GIB:.2f} GiB"


def decide(measurements: dict, previous: dict, signals: dict | None, now: float):
    """Return the next state: values, a reason per value, and AIMD bookkeeping."""
    values, reasons = dict(previous["values"]), dict(previous.get("reasons", {}))
    m = measurements
    reserve = max(GIB, m["disk_total"] // 20)
    # shortcut: free disk excludes space the cache and staging already use, so
    # budgets settle below the true maximum; add usage back if that matters.
    room = max(0, m["disk_free"] - reserve)
    targets = {
        # shortcut: cgroup memory.current counts page cache, so available memory
        # is understated under file I/O; subtract inactive_file if caches stay small.
        "cache_memory_bytes": (m["memory_available"] // 4, "25% of available memory"),
        "cache_disk_bytes": (room // 2, "50% of free disk after the reserve"),
        "cache_min_free_bytes": (reserve, "larger of 1 GiB and 5% of the disk"),
        "min_free_bytes": (reserve, "larger of 1 GiB and 5% of the disk"),
        "staging_budget_bytes": (max(1, room), "free disk after the reserve"),
    }
    for key, (target, why) in targets.items():
        old = values.get(key) or 0
        if old == 0 or abs(target - old) > old / 10:
            values[key], reasons[key] = target, f"{why} ({_gib(target)})"
    last, raised_at = previous.get("signals"), previous["raised_at"]
    throughput = previous.get("throughput", 0.0)
    if last is not None and signals is not None:
        elapsed = max(1e-9, signals["at"] - last["at"])
        current = max(0, signals["bytes"] - last["bytes"]) / elapsed
        if signals["floods"] > last["floods"] or signals["timeouts"] > last["timeouts"]:
            for key in CONCURRENCY:
                values[key] = max(1, values[key] // 2)
                reasons[key] = f"halved after FloodWait or timeouts ({values[key]})"
            raised_at = now
        elif (
            current > 0
            and current >= throughput * 0.9
            and now - raised_at >= COOLDOWN_SECONDS
        ):
            for key in CONCURRENCY:
                values[key] += 1
                reasons[key] = (
                    f"raised: clean window, steady throughput ({values[key]})"
                )
            raised_at = now
        throughput = current
    for key in CPU_BOUND:
        if values[key] > m["cpus"] * 4:
            values[key] = m["cpus"] * 4
            reasons[key] = f"capped at 4 per CPU ({values[key]})"
    return dict(
        values=values,
        reasons=reasons,
        signals=signals,
        throughput=throughput,
        raised_at=raised_at,
    )


class Governor:
    def __init__(self, settings, runtime):
        self.settings, self.runtime = settings, runtime
        self.state: dict | None = None
        self.measurements: dict = {}
        self._task: asyncio.Task | None = None

    def _service(self):
        from tgdrive.settings.performance_preferences import get_performance_settings

        return get_performance_settings(self.settings, self.runtime)

    def signals(self) -> dict | None:
        slots = getattr(getattr(self.runtime, "pool", None), "_slots", ())
        rows = [
            scheduler
            for slot in slots
            if (scheduler := getattr(slot.client, "_media_scheduler", None))
        ]
        if not rows:
            return None
        return dict(
            at=time.monotonic(),
            floods=sum(row.floods for row in rows),
            timeouts=sum(row.timeouts for row in rows),
            bytes=sum(row.bytes for row in rows),
        )

    async def tick(self, now: float | None = None) -> None:
        service = self._service()
        directory = self.settings.resolved_data_dir
        self.measurements = await asyncio.to_thread(measure, directory)
        now = time.monotonic() if now is None else now
        if self.state is None:
            self.state = dict(
                values={k: getattr(service.value, k) for k in CONCURRENCY + BYTES},
                raised_at=now,
            )
        self.state = decide(self.measurements, self.state, self.signals(), now)
        values = self.state["values"]
        # Compare with live settings so an owner save is overridden on the next tick.
        if any(getattr(self.settings, k, None) != v for k, v in values.items()):
            async with service._lock:
                await service._apply(service.value.model_copy(update=values))

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Automatic performance tuning failed")
            await asyncio.sleep(INTERVAL_SECONDS)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="resource-governor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def snapshot(self) -> dict:
        state = self.state or {}
        return dict(
            running=self._task is not None,
            interval_seconds=INTERVAL_SECONDS,
            values=state.get("values", {}),
            reasons=state.get("reasons", {}),
            measurements=self.measurements,
        )
