"""Measured filesystem and optional operator-verified backing capacity.

This module owns no reservations. Callers supply outstanding future writes from
their existing journals; already-written bytes are included in filesystem use.
"""

from __future__ import annotations

import errno
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


class CapacityUnavailable(OSError):
    """A required filesystem measurement is unavailable; do not fall back."""


class _Usage(NamedTuple):
    total: int
    used: int
    free: int


@dataclass(frozen=True)
class DiskCapacity:
    filesystem: _Usage | None
    backing: _Usage | None
    probe_configured: bool
    filesystem_device: int | None = None
    backing_device: int | None = None
    probe_directory: Path | None = None

    @property
    def effective_free_bytes(self) -> int | None:
        if self.filesystem is None or (self.probe_configured and self.backing is None):
            return None
        return (
            min(self.filesystem.free, self.backing.free)
            if self.backing
            else (self.filesystem.free)
        )

    @property
    def constraint_ids(self) -> frozenset[int]:
        # Equality proves the same filesystem in this kernel namespace.
        # Inequality does not prove different physical backing volumes.
        return frozenset(
            value
            for value in (self.filesystem_device, self.backing_device)
            if value is not None
        )

    def as_dict(self, reserve_bytes=0, outstanding_bytes=0) -> dict:
        def usage(value):
            return {
                "available": value is not None,
                "total_bytes": value.total if value else None,
                "used_bytes": value.used if value else None,
                "free_bytes": value.free if value else None,
            }

        scope = (
            "filesystem_unavailable"
            if self.filesystem is None
            else "backing_unavailable"
            if self.probe_configured and self.backing is None
            else "backing_filesystem"
            if self.backing is not None
            else "filesystem_only"
        )
        return {
            "filesystem": usage(self.filesystem),
            "backing": {"configured": self.probe_configured, **usage(self.backing)},
            "effective": {
                "available": self.effective_free_bytes is not None,
                "free_bytes": self.effective_free_bytes,
                "headroom_bytes": usable_headroom(
                    self, reserve_bytes, outstanding_bytes
                ),
            },
            "scope": scope,
        }


def _measure(directory: Path) -> tuple[_Usage | None, int | None]:
    try:
        if not directory.is_dir():
            return None, None
        device = directory.stat().st_dev
        usage = shutil.disk_usage(directory)
        return _Usage(usage.total, usage.used, usage.free), device
    except OSError:
        return None, None


def measure_capacity(directory, *, probe_directory=None) -> DiskCapacity:
    """Measure the target and an explicitly configured empty backing probe.

    Probe identity is operator supplied, never guessed from host names or drives.
    A configured invalid probe has no fallback, including to its parent directory.
    """
    filesystem, device = _measure(Path(directory))
    backing = backing_device = canonical = None
    if probe_directory is not None:
        probe = Path(probe_directory)
        try:
            if not probe.is_symlink() and probe.is_dir():
                canonical = probe.resolve(strict=True)
                if next(canonical.iterdir(), None) is None:
                    backing, backing_device = _measure(canonical)
        except OSError:
            pass
    return DiskCapacity(
        filesystem,
        backing,
        probe_directory is not None,
        device,
        backing_device,
        canonical,
    )


def usable_headroom(capacity: DiskCapacity, reserve_bytes, outstanding_bytes=0):
    """Subtract one reserve and outstanding future writes from limiting free space."""
    if any(
        type(value) is not int or value < 0
        for value in (reserve_bytes, outstanding_bytes)
    ):
        raise ValueError("Capacity reservations must be nonnegative integer bytes")
    free = capacity.effective_free_bytes
    return None if free is None else max(0, free - reserve_bytes - outstanding_bytes)


def require_headroom(
    capacity: DiskCapacity, required_bytes, *, reserve_bytes=0, outstanding_bytes=0
) -> None:
    if type(required_bytes) is not int or required_bytes < 0:
        raise ValueError("Required capacity must be nonnegative integer bytes")
    room = usable_headroom(capacity, reserve_bytes, outstanding_bytes)
    if room is None:
        raise CapacityUnavailable("Cannot measure required storage capacity")
    # A zero-length request must still preserve the floor when free space has
    # already fallen below existing commitments.
    free = capacity.effective_free_bytes
    assert free is not None
    if required_bytes > free - reserve_bytes - outstanding_bytes:
        raise OSError(errno.ENOSPC, "Insufficient storage headroom after reservations")


def probe_for_directory(settings, directory) -> Path | None:
    """Select only from explicit DATA/CACHE roots, including future destinations."""
    target = Path(directory).resolve()
    matches = []
    configured = False
    for role in ("data", "cache"):
        probe = getattr(settings, role + "_backing_capacity_dir", None)
        configured = configured or probe is not None
        root = getattr(settings, "resolved_" + role + "_dir", None)
        if root is not None:
            root = Path(root).resolve()
            if target.is_relative_to(root):
                matches.append((len(root.parts), probe))
    if not matches:
        if configured:
            raise CapacityUnavailable(
                "Storage target is outside configured capacity roots"
            )
        return None
    depth = max(item[0] for item in matches)
    probes = [
        Path(probe) for size, probe in matches if size == depth and probe is not None
    ]
    if not probes:
        return None
    selected = probes[0]
    for probe in probes[1:]:
        if selected.resolve() == probe.resolve():
            continue
        left = measure_capacity(selected, probe_directory=selected)
        right = measure_capacity(probe, probe_directory=probe)
        if (
            left.backing is None
            or right.backing is None
            or left.backing_device != right.backing_device
        ):
            raise CapacityUnavailable(
                "Conflicting backing probes for the same storage root"
            )
    return selected
