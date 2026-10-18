"""Instance-wide limits for disposable media bytes and speculative work."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

MIB = 1024**2


@dataclass(frozen=True, slots=True)
class MediaCachePolicy:
    disk_max_bytes: int = 512 * MIB
    memory_max_bytes: int = 64 * MIB
    min_free_bytes: int = 1024 * MIB
    ttl_seconds: float = 86400
    memory_ttl_seconds: float = 600
    protected_fraction: float = 0.75
    max_background_jobs: int = 2
    background_blocks: int = 2
    max_file_fraction: float = 0.5
    read_ahead_blocks: int = 32
    auto_preload: bool = False

    def __post_init__(self):
        for name in ("disk_max_bytes", "memory_max_bytes", "min_free_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("ttl_seconds", "memory_ttl_seconds"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 0 <= self.protected_fraction < 1:
            raise ValueError("protected_fraction must be between zero and one")
        if not 0 < self.max_file_fraction <= 1:
            raise ValueError("max_file_fraction must be positive and at most one")
        for name, ceiling in (
            ("max_background_jobs", 8),
            ("background_blocks", 8),
            ("read_ahead_blocks", 64),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be between 1 and {ceiling}")

    def as_dict(self):
        return asdict(self)


def merge_ranges(ranges):
    """Canonical half-open ready intervals, independent of HTTP range syntax."""
    result = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
