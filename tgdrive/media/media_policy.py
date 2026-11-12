"""Instance-wide limits for disposable media bytes and speculative work."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass

MIB = 1024**2
STREAM_MEMORY_BASELINE_BYTES = 32 * MIB
AUTO_STREAM_MEMORY_LIMIT_BYTES = 256 * MIB


@dataclass(frozen=True, slots=True)
class MediaCachePolicy:
    """Shared byte budgets and scheduling limits for reads and preparation."""

    disk_max_bytes: int = 512 * MIB
    memory_max_bytes: int = 64 * MIB
    min_free_bytes: int = 1024 * MIB
    ttl_seconds: float = 86400
    memory_ttl_seconds: float = 600
    protected_fraction: float = 0.75
    max_background_jobs: int = 2
    background_blocks: int = 2
    read_ahead_blocks: int = 32
    scratch_max_bytes: int = 32 * MIB
    auto_preload: bool = False

    def __post_init__(self) -> None:
        for name in (
            "disk_max_bytes",
            "memory_max_bytes",
            "min_free_bytes",
            "scratch_max_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("ttl_seconds", "memory_ttl_seconds"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 0 <= self.protected_fraction < 1:
            raise ValueError("protected_fraction must be between zero and one")
        for name, ceiling in (
            ("max_background_jobs", 8),
            ("background_blocks", 8),
            ("read_ahead_blocks", 64),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be between 1 and {ceiling}")

    def as_dict(self) -> dict[str, int | float | bool]:
        """Return an independent snapshot for preferences and status responses."""
        return asdict(self)


def merge_ranges(ranges: Iterable[Sequence[int]]) -> list[list[int]]:
    """Return sorted half-open intervals, joining overlaps and adjacent ranges.

    Empty or reversed intervals are omitted. The returned lists do not alias the
    input, allowing callers to update cache readiness without changing its source.
    """
    result: list[list[int]] = []
    for start, end in sorted(ranges, key=lambda interval: (interval[0], interval[1])):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
