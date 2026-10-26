"""Shared admission accounting for receipt, multipart and URL upload staging.

Admissions run synchronously on the server event loop. Reservations live in the
existing receipt/job journals so restart and retries retain their storage claim.
Physical free space already excludes written bytes; subtract only future writes.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path


class StorageUnavailable(ValueError):
    def __init__(self, message: str, status: int = 507):
        super().__init__(message)
        self.status = status


def staging_usage(manager, *, receipts=None, exclude=None):
    reserved = remaining = 0
    seen = set()
    receipts = receipts or getattr(manager, "uploads", None)
    if receipts is not None:
        for item in receipts._all():
            if item["state"] not in {"receiving", "queued", "cancelling"}:
                continue
            seen.add(item["id"])
            if item["id"] == exclude:
                continue
            reserved += item["total_size"]
            if item["state"] == "receiving":
                remaining += max(0, item["total_size"] - item["offset"])
    registry = getattr(manager, "registry", None)
    for job in registry.staging_records() if registry is not None else ():
        if job.id in seen or job.id == exclude:
            continue
        seen.add(job.id)
        context = job.context
        raw_path = context.get("local_path")
        written = 0
        if raw_path:
            with contextlib.suppress(FileNotFoundError):
                written = Path(raw_path).stat().st_size
        active = not job.terminal or (job.task is not None and not job.task.done())
        size = max(written, context.get("staging_size", 0)) if active else written
        reserved += size
        if active:
            received = max(written, context.get("staging_written", 0))
            remaining += max(0, size - received)
    for identity, size in getattr(manager, "_pending_staging", {}).items():
        if identity not in seen and identity != exclude:
            reserved += size
            remaining += size
    return reserved, remaining


def check_staging(settings, manager, size, *, receipts=None, exclude=None, written=0):
    reserved, remaining = staging_usage(manager, receipts=receipts, exclude=exclude)
    if reserved + size > settings.staging_budget_bytes:
        raise StorageUnavailable(
            "Temporary upload storage is reserved by other transfers. "
            "Finish or cancel a transfer, or increase the upload budget."
        )
    try:
        free = shutil.disk_usage(settings.resolved_cache_dir).free
    except OSError:
        raise StorageUnavailable(
            "Cannot check free space on the upload drive. "
            "Check server storage and retry.",
            status=503,
        ) from None
    required = remaining + max(0, size - written)
    if free - required < settings.min_free_bytes:
        raise StorageUnavailable(
            "Not enough free disk space for this upload after existing reservations "
            "and the free-space reserve. Free space or finish another transfer first."
        )
