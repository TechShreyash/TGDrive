"""Tune performance values from measured server resources while running."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from tgdrive.core.system_resources import cpu_snapshot, memory_snapshot
from tgdrive.storage.capacity import measure_capacity

logger = logging.getLogger(__name__)

GIB = 1024**3
INTERVAL_SECONDS = 5
AUTO_RESOURCE_LIMITS = {
    "cache_disk_limit_bytes": 10 * GIB,
    "cache_memory_limit_bytes": 256 * 1024**2,
    "staging_limit_bytes": 16 * GIB,
}
AUTO_FREE_SPACE_RESERVE = {"minimum_bytes": GIB, "fraction": 0.05}
RESOURCE_LIMITS = {
    "cache_disk_bytes": "cache_disk_limit_bytes",
    "cache_memory_bytes": "cache_memory_limit_bytes",
    "staging_budget_bytes": "staging_limit_bytes",
}
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
# size and request pacing keep their tested defaults.
MANAGED = frozenset(
    CONCURRENCY + BYTES + ("telegram_parts_per_second", "telegram_max_queue")
)


def memory(
    cgroup: str = "/sys/fs/cgroup", meminfo: str = "/proc/meminfo"
) -> tuple[int | None, int | None]:
    """Return effective (limit, available) bytes within this kernel namespace."""
    value = memory_snapshot(
        cgroup=Path(cgroup), proc=Path(meminfo).parent, meminfo=meminfo
    )
    return value["limit_bytes"], value["available_bytes"]


def cpus(path: str = "/sys/fs/cgroup/cpu.max") -> float | None:
    return cpu_snapshot(cgroup=Path(path).parent)["capacity_cores"]


def measure(
    directory,
    media_directory,
    *,
    cache_probe_directory=None,
    data_probe_directory=None,
) -> dict:
    limit, available = memory()
    disk = measure_capacity(directory, probe_directory=cache_probe_directory)
    media_directory = Path(media_directory)
    # The disposable media cache need not exist before the first read.
    while not media_directory.exists():
        media_directory = media_directory.parent
    media_disk = measure_capacity(media_directory, probe_directory=data_probe_directory)
    return dict(
        memory_limit=limit,
        memory_available=available,
        cpus=cpus(),
        disk_total=disk.effective_total_bytes or 0,
        disk_free=disk.filesystem.free if disk.filesystem else None,
        disk_effective_free=disk.effective_free_bytes,
        disk_capacity=disk.as_dict(),
        cache_disk_total=media_disk.effective_total_bytes or 0,
        cache_disk_free=media_disk.filesystem.free if media_disk.filesystem else None,
        cache_disk_effective_free=media_disk.effective_free_bytes,
        cache_disk_capacity=media_disk.as_dict(),
    )


def _gib(value: int) -> str:
    return f"{value / GIB:.2f} GiB"


def constrain(values: dict, policy: dict, measurements: dict | None = None) -> dict:
    """Apply owner ceilings independently of pressure estimates and hysteresis."""
    values = dict(values)
    for key, limit_key in RESOURCE_LIMITS.items():
        ceiling = policy.get(limit_key)
        if ceiling is None:
            ceiling = AUTO_RESOURCE_LIMITS[limit_key]
        values[key] = min(values.get(key, ceiling), ceiling)
    values["max_file_size"] = min(
        values.get("max_file_size", values["staging_budget_bytes"]),
        values["staging_budget_bytes"],
    )
    reserve = policy.get("protected_free_bytes")
    if reserve is not None:
        values["min_free_bytes"] = values["cache_min_free_bytes"] = reserve
    elif measurements is not None:
        values["min_free_bytes"] = max(GIB, measurements["disk_total"] // 20)
        values["cache_min_free_bytes"] = max(
            GIB, measurements.get("cache_disk_total", measurements["disk_total"]) // 20
        )
    return values


def decide(measurements: dict, previous: dict, policy: dict | None = None):
    """Reduce budgets under pressure, never exceeding saved owner ceilings."""
    policy = policy or {}
    values, reasons = dict(previous["values"]), dict(previous.get("reasons", {}))
    ceilings = previous.get("ceilings") or {key: values[key] for key in CONCURRENCY}
    m = measurements
    reserve = policy.get("protected_free_bytes")
    if reserve is None:
        reserve = max(GIB, m["disk_total"] // 20)
    free = m.get("disk_effective_free", m["disk_free"])
    room = max(0, free - reserve) if free is not None else 0
    staging_room = room + m.get("staging_allocated_bytes", 0) if free is not None else 0
    cache_reserve = policy.get("protected_free_bytes")
    if cache_reserve is None:
        cache_reserve = max(GIB, m.get("cache_disk_total", m["disk_total"]) // 20)
    cache_free = m.get(
        "cache_disk_effective_free", m.get("cache_disk_free", m["disk_free"])
    )
    cache_room = max(0, cache_free - cache_reserve) if cache_free is not None else 0
    targets = {
        "cache_disk_bytes": (
            cache_room // 2,
            "50% of free media-cache disk after reserve",
        ),
        "staging_budget_bytes": (
            max(1, staging_room),
            "free disk after reserve plus allocated staging",
        ),
        "max_file_size": (max(1, staging_room), "fits in the staging budget"),
    }
    if m["memory_available"] is not None:
        targets["cache_memory_bytes"] = (
            m["memory_available"] // 4,
            "25% of available memory",
        )
    for key, (target, why) in targets.items():
        old = values.get(key) or 0
        # Shrinking is immediate; hysteresis only suppresses small growth.
        if old == 0 or target < old or target - old > old / 10:
            values[key], reasons[key] = target, f"{why} ({_gib(target)})"
    uncapped = dict(values)
    values = constrain(values, policy, m)
    for key in RESOURCE_LIMITS:
        if values[key] < uncapped.get(key, values[key]):
            why = (
                "automatic ceiling"
                if policy.get(RESOURCE_LIMITS[key]) is None
                else "owner ceiling"
            )
            reasons[key] = f"{why} ({_gib(values[key])})"
    if values["max_file_size"] < uncapped.get("max_file_size", values["max_file_size"]):
        reasons["max_file_size"] = (
            f"fits in capped staging budget ({_gib(values['max_file_size'])})"
        )
    for key in ("min_free_bytes", "cache_min_free_bytes"):
        why = (
            "owner free-space reserve"
            if policy.get("protected_free_bytes") is not None
            else "larger of 1 GiB and 5% of the filesystem"
        )
        reasons[key] = f"{why} ({_gib(values[key])})"
    for key, ceiling in ceilings.items():
        values[key] = ceiling
        reasons[key] = f"startup ceiling ({ceiling})"
    for key in CPU_BOUND:
        target = (
            min(values[key], max(1, int(m["cpus"] * 4)), 8)
            if m["cpus"]
            else values[key]
        )
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
        probes = {
            name: value
            for name, value in {
                "cache_probe_directory": getattr(
                    self.settings, "cache_backing_capacity_dir", None
                ),
                "data_probe_directory": getattr(
                    self.settings, "data_backing_capacity_dir", None
                ),
            }.items()
            if value is not None
        }
        self.measurements = await asyncio.to_thread(
            measure, directory, media_directory, **probes
        )
        self.measurements["sampled_at"] = time.time()
        manager = getattr(self.runtime, "transfers", None)
        if manager is not None:
            from tgdrive.transfers.staging import staging_usage

            reserved, remaining = staging_usage(manager)
            self.measurements["staging_allocated_bytes"] = reserved - remaining
        async with service._lock:
            if self.state is None:
                self.state = dict(
                    values={
                        k: getattr(service.value, k, None) or getattr(self.settings, k)
                        for k in CONCURRENCY + BYTES
                    },
                )
            before = self.state["values"]
            policy = {
                key: getattr(service.value, key, None)
                for key in (*AUTO_RESOURCE_LIMITS, "protected_free_bytes")
            }
            self.state = decide(self.measurements, self.state, policy)
            values = dict(self.state["values"])
            changed = sorted(k for k, v in values.items() if before.get(k) != v)
            if changed:
                # Summarize disk/memory budgets and concurrency changes.
                parts = []
                for k in changed:
                    reason = str(self.state["reasons"].get(k, ""))
                    if k in BYTES and "(" in reason:
                        # Keep the measured amount, without the tooltip formula.
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
                getattr(self.settings, k, None) != v
                or getattr(service.value, k, None) != v
                for k, v in values.items()
            ):
                await service._apply(service.value.model_copy(update=values))
            if (
                pool is not None
                and uploads != self.settings.telegram_uploads_per_bot
                and hasattr(pool, "set_uploads_per_bot")
            ):
                await pool.set_uploads_per_bot(uploads)
                self.settings.telegram_uploads_per_bot = uploads
            cache = getattr(pool, "_media_read_cache", None)
            update = getattr(cache, "update_stream_capacity", None)
            if callable(update):
                update(self.measurements)

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
