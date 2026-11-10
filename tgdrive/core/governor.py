"""Tune performance values from measured server resources while running."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

GIB = 1024**3
INTERVAL_SECONDS = 5
CONCURRENCY = (
    "telegram_uploads_per_bot",
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
    "max_file_size",
)
# Values the owner no longer edits; saves that include them are refused. Queue
# size, request pacing and cache retention keep their tested defaults.
MANAGED = frozenset(
    CONCURRENCY
    + BYTES
    + ("telegram_parts_per_second", "telegram_max_queue", "cache_ttl_seconds")
)


def _words(path) -> list[str] | None:
    try:
        return Path(path).read_text().split()
    except OSError:
        return None


def memory(
    cgroup: str = "/sys/fs/cgroup", meminfo: str = "/proc/meminfo"
) -> tuple[int | None, int | None]:
    """Return (limit, available) bytes; cgroup v2 first, then the host."""
    limit, current = _words(f"{cgroup}/memory.max"), _words(f"{cgroup}/memory.current")
    if limit and current and limit[0] != "max":
        return int(limit[0]), max(0, int(limit[0]) - int(current[0]))
    rows = {}
    try:
        lines = Path(meminfo).read_text().splitlines()
    except OSError:
        return None, None
    for line in lines:
        name, _, rest = line.partition(":")
        if rest.split():
            rows[name] = int(rest.split()[0]) * 1024
    return rows.get("MemTotal"), rows.get("MemAvailable")


def cpus(path: str = "/sys/fs/cgroup/cpu.max") -> int:
    row = _words(path)
    if row and row[0] != "max":
        return max(1, -(-int(row[0]) // int(row[1])))
    return os.cpu_count() or 1


def measure(directory, media_directory) -> dict:
    limit, available = memory()
    disk = shutil.disk_usage(directory)
    media_directory = Path(media_directory)
    # The disposable media cache need not exist before the first read.
    while not media_directory.exists():
        media_directory = media_directory.parent
    media_disk = shutil.disk_usage(media_directory)
    return dict(
        memory_limit=limit,
        memory_available=available,
        cpus=cpus(),
        disk_total=disk.total,
        disk_free=disk.free,
        cache_disk_total=media_disk.total,
        cache_disk_free=media_disk.free,
    )


def _gib(value: int) -> str:
    return f"{value / GIB:.2f} GiB"


def decide(measurements: dict, previous: dict):
    """Size resource budgets while preserving the configured concurrency ceilings."""
    values, reasons = dict(previous["values"]), dict(previous.get("reasons", {}))
    ceilings = previous.get("ceilings") or {key: values[key] for key in CONCURRENCY}
    m = measurements
    reserve = max(GIB, m["disk_total"] // 20)
    room = max(0, m["disk_free"] - reserve)
    staging_room = room + m.get("staging_allocated_bytes", 0)
    cache_reserve = max(GIB, m.get("cache_disk_total", m["disk_total"]) // 20)
    cache_room = max(0, m.get("cache_disk_free", m["disk_free"]) - cache_reserve)
    targets = {
        "cache_disk_bytes": (
            cache_room // 2,
            "50% of free media-cache disk after reserve",
        ),
        "cache_min_free_bytes": (
            cache_reserve,
            "larger of 1 GiB and 5% of the media-cache disk",
        ),
        "min_free_bytes": (reserve, "larger of 1 GiB and 5% of the disk"),
        "staging_budget_bytes": (
            max(1, staging_room),
            "free disk after reserve plus allocated staging",
        ),
        # The largest file is whatever staging can hold; TGDrive adds no cap.
        "max_file_size": (max(1, staging_room), "fits in the staging budget"),
    }
    if m["memory_available"] is not None:
        targets["cache_memory_bytes"] = (
            m["memory_available"] // 4,
            "25% of available memory",
        )
    for key, (target, why) in targets.items():
        old = values.get(key) or 0
        if old == 0 or abs(target - old) > old / 10:
            values[key], reasons[key] = target, f"{why} ({_gib(target)})"
    for key, ceiling in ceilings.items():
        values[key] = ceiling
        reasons[key] = f"startup ceiling ({ceiling})"
    for key in CPU_BOUND:
        target = min(values[key], m["cpus"] * 4, 8)
        if target != values[key]:
            values[key] = target
            reasons[key] = f"capped at 4 per CPU and 8 jobs ({target})"
    return dict(values=values, reasons=reasons, ceilings=ceilings)


class Governor:
    def __init__(self, settings, runtime):
        self.settings, self.runtime = settings, runtime
        self.state: dict | None = None
        self.measurements: dict = {}
        self._task: asyncio.Task | None = None

    def _service(self):
        from tgdrive.settings.performance_preferences import get_performance_settings

        return get_performance_settings(self.settings, self.runtime)

    async def tick(self) -> None:
        service = self._service()
        directory = self.settings.resolved_cache_dir
        pool = getattr(self.runtime, "pool", None)
        media_directory = getattr(pool, "media_cache_dir", None) or (
            Path(self.settings.resolved_data_dir) / "media-cache"
        )
        self.measurements = await asyncio.to_thread(measure, directory, media_directory)
        manager = getattr(self.runtime, "transfers", None)
        if manager is not None:
            from tgdrive.transfers.staging import staging_usage

            reserved, remaining = staging_usage(manager)
            self.measurements["staging_allocated_bytes"] = reserved - remaining
        if self.state is None:
            self.state = dict(
                values={
                    k: getattr(service.value, k, None) or getattr(self.settings, k)
                    for k in CONCURRENCY + BYTES
                },
            )
        before = self.state["values"]
        self.state = decide(self.measurements, self.state)
        values = dict(self.state["values"])
        changed = sorted(k for k, v in values.items() if before.get(k) != v)
        if changed:
            # Format concise summaries for disk/memory budgets and concurrency changes
            parts = []
            for k in changed:
                reason = str(self.state["reasons"].get(k, ""))
                if k in BYTES and "(" in reason:
                    # e.g. "cache_disk_bytes: 16.62 GiB" instead of full tooltip formula
                    val_str = reason.rsplit("(", 1)[-1].rstrip(")")
                    parts.append(f"{k}: {val_str}")
                else:
                    parts.append(f"{k}: {reason}")
            logger.log(
                logging.INFO,
                "Automatic tuning: %s",
                "; ".join(parts),
            )
        uploads = values.pop("telegram_uploads_per_bot")
        if any(
            getattr(self.settings, k, None) != v or getattr(service.value, k, None) != v
            for k, v in values.items()
        ):
            async with service._lock:
                await service._apply(service.value.model_copy(update=values))
        if (
            pool is not None
            and uploads != self.settings.telegram_uploads_per_bot
            and hasattr(pool, "set_uploads_per_bot")
        ):
            await pool.set_uploads_per_bot(uploads)
            self.settings.telegram_uploads_per_bot = uploads

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
