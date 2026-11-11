"""Shared admission accounting for receipt, multipart, URL and torrent staging.

Admissions run synchronously on the server event loop. Reservations live in the
existing receipt/job journals so restart and retries retain their storage claim.
Physical free space already excludes written bytes; subtract only future writes.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from tgdrive.storage.capacity import (
    CapacityUnavailable,
    measure_capacity,
    probe_for_directory,
    require_headroom,
)


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
    jobs = registry.staging_records() if registry is not None else ()
    owners = {
        job.id for job in jobs if job.context.get("kind") in {"torrent", "extract"}
    }
    for job in jobs:
        if job.id in seen or job.id == exclude:
            continue
        seen.add(job.id)
        context = job.context
        if (
            context.get("torrent_parent_id") in owners
            or context.get("archive_parent_id") in owners
        ):
            continue
        raw_path = context.get("local_path")
        written = 0
        if raw_path:
            with contextlib.suppress(FileNotFoundError):
                written = Path(raw_path).stat().st_size
        active = not job.terminal or (job.task is not None and not job.task.done())
        directory = context.get("staging_directory")
        if directory:
            if Path(directory).exists():
                written = max(written, context.get("staging_written", 0))
                # Retain the journaled claim through failed jobs and asynchronous
                # cleanup, without walking every staged file on every admission.
                size = max(written, context.get("staging_size", 0))
            else:
                written = 0
                size = context.get("staging_size", 0) if active else 0
        else:
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


def check_staging(
    settings, manager, size, *, receipts=None, exclude=None, written=0, directory=None
):
    reserved, remaining = staging_usage(manager, receipts=receipts, exclude=exclude)
    if reserved + size > settings.staging_budget_bytes:
        # Lowering a ceiling blocks new/growing claims, not work admitted before
        # the change. Written bytes still participate in the physical-space guard.
        total, _ = staging_usage(manager, receipts=receipts)
        existing = total - reserved if exclude is not None else 0
        if size > existing:
            raise StorageUnavailable(
                "Temporary upload storage is reserved by other transfers. "
                "Finish or cancel a transfer, or increase the upload budget."
            )
    check_staging_capacity(
        settings,
        settings.resolved_cache_dir if directory is None else directory,
        max(0, size - written),
        outstanding_bytes=remaining,
    )


def check_staging_capacity(settings, directory, required_bytes, *, outstanding_bytes=0):
    """Check an immutable event-loop reservation snapshot in a disk worker.

    Callers capture outstanding future writes with staging_usage before dispatch;
    this helper never reads live receipt or transfer journals from a worker.
    """
    try:
        require_headroom(
            measure_capacity(
                directory,
                probe_directory=probe_for_directory(settings, directory),
            ),
            required_bytes,
            reserve_bytes=settings.min_free_bytes,
            outstanding_bytes=outstanding_bytes,
        )
    except CapacityUnavailable:
        raise StorageUnavailable(
            "Cannot check free space on the upload drive or its configured backing "
            "storage. Check server storage and retry.",
            status=503,
        ) from None
    except OSError:
        raise StorageUnavailable(
            "Not enough free disk space for this upload after existing reservations "
            "and the free-space reserve. Free space or finish another transfer first."
        ) from None
