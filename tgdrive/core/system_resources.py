"""Kernel-visible resources shared by admission, tuning and observability."""

from __future__ import annotations

import os
from pathlib import Path


def _text(path: Path) -> str:
    try:
        with path.open() as source:
            return source.read(65536)
    except OSError:
        return ""


def _integer(value: str) -> int | None:
    try:
        result = int(value)
        return result if result >= 0 else None
    except ValueError:
        return None


def _ancestors(root: Path, membership: str) -> list[Path]:
    relative = Path(membership.lstrip("/"))
    if ".." in relative.parts:
        return []
    leaf = root / relative if (root / relative).is_dir() else root
    directories = [leaf]
    while leaf != root and len(directories) < 64:
        leaf = leaf.parent
        directories.append(leaf)
    return directories


def cgroup_directories(cgroup: Path, proc: Path) -> list[Path]:
    """Include visible v2 ancestors; a namespace may hide outer limits."""
    for row in _text(proc / "self/cgroup").splitlines():
        if row.startswith("0::"):
            return _ancestors(cgroup, row[3:]) or [cgroup]
    return [cgroup]


def cgroup_v1_directories(
    cgroup: Path, proc: Path, controller: str
) -> list[Path] | None:
    """None means absent; empty means detected but not measurable here."""
    filename = {
        "memory": "memory.limit_in_bytes",
        "cpu": "cpu.cfs_quota_us",
        "cpuacct": "cpuacct.usage",
    }[controller]
    for row in _text(proc / "self/cgroup").splitlines():
        fields = row.split(":", 2)
        if len(fields) != 3 or controller not in fields[1].split(","):
            continue
        controllers = fields[1].split(",")
        for root in (
            cgroup / controller,
            cgroup / fields[1],
            cgroup / ",".join(sorted(controllers)),
            cgroup,
        ):
            if (root / filename).is_file():
                relative = Path(fields[2].lstrip("/"))
                if ".." in relative.parts or not (root / relative).is_dir():
                    return []  # Root measurements do not establish hidden child limits.
                return _ancestors(root, fields[2])
        # Nonstandard mounts are unknown; do not infer host-wide headroom.
        return []
    return None


def memory_snapshot(
    *, cgroup=Path("/sys/fs/cgroup"), proc=Path("/proc"), meminfo=None
) -> dict:
    host = {}
    for row in _text(
        Path(meminfo) if meminfo is not None else proc / "meminfo"
    ).splitlines():
        name, _, value = row.partition(":")
        fields = value.split()
        if name in {"MemTotal", "MemAvailable"} and len(fields) == 2:
            parsed = _integer(fields[0])
            if parsed is not None and fields[1] == "kB":
                host[name] = parsed * 1024
    total, available = host.get("MemTotal"), host.get("MemAvailable")
    if total is not None and available is not None:
        available = min(total, available)
    cgroup, proc = Path(cgroup), Path(proc)
    legacy = cgroup_v1_directories(cgroup, proc, "memory")
    directories = legacy if legacy is not None else cgroup_directories(cgroup, proc)
    source = "cgroup_v1" if legacy is not None else "cgroup_v2"
    limits = []
    leaf_current = None
    observed = legacy is not None
    unknown = legacy == []
    for directory in directories:
        raw_limit = _text(
            directory
            / ("memory.limit_in_bytes" if legacy is not None else "memory.max")
        ).strip()
        raw_current = _text(
            directory
            / ("memory.usage_in_bytes" if legacy is not None else "memory.current")
        ).strip()
        observed = observed or bool(raw_limit or raw_current)
        limit, current = _integer(raw_limit), _integer(raw_current)
        # V1's page-aligned LONG_MAX sentinel is not a finite memory allowance.
        unlimited = raw_limit == "max" or (
            legacy is not None and limit is not None and limit >= 2**60
        )
        if unlimited:
            limit = None
        elif limit is None and (legacy is not None or directory != cgroup or raw_limit):
            unknown = True
        if directory == directories[0]:
            leaf_current = current
        if limit is not None:
            limits.append((limit, current))
    group_limit = None
    used = leaf_current
    if limits:
        group_limit, used = min(limits, key=lambda pair: pair[0])
        headrooms = [max(0, cap - use) for cap, use in limits if use is not None]
        if unknown or any(use is None for _, use in limits):
            available = None
        else:
            available = min(headrooms + ([available] if available is not None else []))
    elif observed and (unknown or leaf_current is None):
        available = None
    limit = group_limit
    limit_source = source if limit is not None else "unavailable"
    if (
        total is not None
        and (not observed or not unknown)
        and (limit is None or total < limit)
    ):
        limit, limit_source = total, "proc_meminfo"
    scope = "cgroup" if observed else "host" if total is not None else "unavailable"
    if not observed:
        source = "proc_meminfo" if total is not None else "unavailable"
        if total is not None and available is not None:
            used = total - available
    return {
        "scope": scope,
        "source": source,
        "limit_bytes": limit,
        "used_bytes": used,
        "available_bytes": available,
        "cgroup_limit_bytes": group_limit,
        "environment_total_bytes": total,
        "limit_source": limit_source,
    }


def cpu_snapshot(*, cgroup=Path("/sys/fs/cgroup"), proc=Path("/proc")) -> dict:
    count = os.cpu_count()
    capacity = float(count) if count and count > 0 else None
    scope, source = ("host", "os_cpu_count") if capacity else ("unavailable",) * 2
    try:
        affinity = len(os.sched_getaffinity(0))
        if affinity > 0 and (capacity is None or affinity < capacity):
            capacity, scope, source = float(affinity), "affinity", "affinity"
    except AttributeError, OSError:
        pass
    cgroup, proc = Path(cgroup), Path(proc)
    legacy = cgroup_v1_directories(cgroup, proc, "cpu")
    if legacy == []:
        return {"scope": "unavailable", "source": "unavailable", "capacity_cores": None}
    directories = legacy if legacy is not None else cgroup_directories(cgroup, proc)
    for directory in directories:
        if legacy is not None:
            raw_quota = _text(directory / "cpu.cfs_quota_us").strip()
            raw_period = _text(directory / "cpu.cfs_period_us").strip()
            if raw_quota == "-1" and _integer(raw_period):
                continue
            fields = [raw_quota, raw_period]
        else:
            fields = _text(directory / "cpu.max").split()
        if len(fields) != 2:
            continue
        quota, period = (_integer(value) for value in fields)
        if legacy is not None and not (quota and period):
            return {
                "scope": "unavailable",
                "source": "unavailable",
                "capacity_cores": None,
            }
        if quota and period and (capacity is None or quota / period <= capacity):
            capacity, scope, source = (
                quota / period,
                "cgroup",
                ("cgroup_v1" if legacy is not None else "cgroup_v2"),
            )
    return {"scope": scope, "source": source, "capacity_cores": capacity}
