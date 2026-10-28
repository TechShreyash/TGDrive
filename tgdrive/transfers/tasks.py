"""Bounded transfer state and background-task supervision."""

from __future__ import annotations

import asyncio
import copy
import errno
import logging
import math
import re
import secrets
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any

from aiohttp import ClientConnectionError, ClientPayloadError
from pymongo.errors import PyMongoError
from telethon.errors import RpcCallFailError, ServerError, TimedOutError

from tgdrive.storage.mongo import DatabaseUnavailable

logger = logging.getLogger(__name__)
_DEFAULT_CANCEL_GRACE_SECONDS = 10.0
_SHUTDOWN_DRAIN_SECONDS = 5.0
_PROGRESS_JOURNAL_SECONDS = 0.5
_PROGRESS_STALE_SECONDS = 15.0
_BYTE_PHASES = frozenset(
    {
        "saving",
        "downloading",
        "uploading",
        "torrent_uploading",
        "archiving",
        "extracting",
    }
)
_ERROR_UNSET = object()


def upload_effects(plan):
    """Original messages and optional poster share upload retry/cleanup ownership."""
    if not isinstance(plan, dict) or not isinstance(plan.get("parts"), list):
        return []
    poster = plan.get("poster")
    return plan["parts"] + ([poster] if isinstance(poster, dict) else [])


def _journalled(operation):
    """A disconnected caller must drain its commit before releasing the journal."""

    @wraps(operation)
    async def owned(*args, **kwargs):
        task = asyncio.create_task(operation(*args, **kwargs))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    return owned


def _retry_after_restart(error: Exception) -> bool:
    """Only known temporary transport failures may restart without owner input."""
    from tgdrive.recovery.backup import BackupError, RecoveryConflict
    from tgdrive.transfers.remote import (
        RemoteHTTPError,
        RemoteResolutionError,
        RemoteTimeout,
    )

    if isinstance(error, RecoveryConflict):
        return False
    if isinstance(error, BackupError) and isinstance(error.__cause__, Exception):
        # Checkpoint publication wraps provider errors in a safe domain exception.
        error = error.__cause__
    if isinstance(
        error,
        (
            ConnectionError,
            TimeoutError,
            ClientConnectionError,
            ClientPayloadError,
            ServerError,
            RpcCallFailError,
            TimedOutError,
            RemoteResolutionError,
            RemoteTimeout,
        ),
    ):
        return True
    if isinstance(error, RemoteHTTPError):
        return error.status in {408, 429} or 500 <= error.status <= 599
    return isinstance(error, OSError) and error.errno in {
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.ENETRESET,
        errno.ECONNABORTED,
        errno.ECONNRESET,
        errno.ETIMEDOUT,
        errno.EHOSTUNREACH,
    }


def _failure_diagnostic(error: Exception) -> tuple[str, str]:
    """Stable categories/actions without formatting private exception messages."""
    from tgdrive.recovery.backup import BackupError, RecoveryConflict
    from tgdrive.telegram.connection_errors import connection_problem
    from tgdrive.transfers.remote import (
        RemoteHTTPError,
        RemoteRetriesExhausted,
        RemoteTooLarge,
        UnsafeRemoteURL,
    )

    if isinstance(error, RecoveryConflict):
        return (
            "RECOVERY_REQUIRED",
            "check Storage & Backups and follow recovery guidance",
        )
    if isinstance(error, (DatabaseUnavailable, PyMongoError)):
        return "DATABASE_UNAVAILABLE", "restore MongoDB access and retry the transfer"
    if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
        return "STAGING_FULL", "free unrelated disk space; preserve unfinished uploads"
    problem = connection_problem(error, "bot")
    if problem is not None and problem.code in {
        "TELEGRAM_CREDENTIALS_REVOKED",
        "TELEGRAM_CHANNEL_ACCESS",
        "TELEGRAM_ACCOUNT_REMOVED",
    }:
        return problem.code, "repair the account or channel in Connections and retry"
    if isinstance(error, RemoteHTTPError):
        code = (
            f"REMOTE_HTTP_{error.status}"
            if type(error.status) is int and 100 <= error.status <= 599
            else "REMOTE_HTTP_ERROR"
        )
        return code, "check source availability and access before retrying"
    if isinstance(error, RemoteRetriesExhausted):
        return (
            "REMOTE_RETRIES_EXHAUSTED",
            "check source availability or use another link",
        )
    if isinstance(error, UnsafeRemoteURL):
        return "REMOTE_URL_BLOCKED", "check the source URL and outbound network policy"
    if isinstance(error, RemoteTooLarge):
        return "REMOTE_FILE_TOO_LARGE", "check the configured upload limit"
    if isinstance(error, BackupError):
        return "CHECKPOINT_FAILED", "check Storage & Backups before retrying"
    if _retry_after_restart(error):
        return "TRANSPORT_INTERRUPTED", "restore the connection and retry the transfer"
    return "TRANSFER_FAILED", "inspect the transfer error in Transfers"


class TransferStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


_TERMINAL = {
    TransferStatus.COMPLETED,
    TransferStatus.ERROR,
    TransferStatus.CANCELLED,
}


@dataclass(slots=True)
class TransferRecord:
    id: str
    phase: str
    status: TransferStatus = TransferStatus.QUEUED
    current: int = 0
    total: int = 0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress_updated_at: float | None = None
    _progress_samples: deque[tuple[float, int]] = field(
        default_factory=lambda: deque(maxlen=64), repr=False
    )
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    cancel_handle: asyncio.TimerHandle | None = field(default=None, repr=False)

    context: dict[str, Any] = field(default_factory=dict)
    _accepted: bool = field(default=True, repr=False)
    _committed: TransferRecord | None = field(default=None, repr=False)

    @property
    def committed(self) -> TransferRecord:
        return self._committed or self

    @property
    def terminal(self) -> bool:
        return self.committed.status in _TERMINAL

    @property
    def history_hidden(self) -> bool:
        return bool(
            self.committed.context.get("torrent_parent_id")
            or self.committed.context.get("archive_parent_id")
        ) or (self.terminal and bool(self.committed.context.get("history_cleared")))

    def as_dict(self) -> dict[str, Any]:
        if self._committed is not None:
            return self._committed.as_dict()
        now = time.time()
        elapsed = (
            max(0.0, (self.finished_at or now) - self.started_at)
            if self.started_at is not None
            else 0.0
        )
        speed = None
        eta = None
        if (
            self.status is TransferStatus.RUNNING
            and self.phase in _BYTE_PHASES
            and self.context.get("kind") != "delete"
            and self.progress_updated_at is not None
            and len(self._progress_samples) >= 2
            and time.monotonic() - self._progress_samples[-1][0]
            <= _PROGRESS_STALE_SECONDS
        ):
            first_at, first_bytes = self._progress_samples[0]
            last_at, last_bytes = self._progress_samples[-1]
            duration = last_at - first_at
            if duration > 0 and last_bytes > first_bytes:
                speed = (last_bytes - first_bytes) / duration
                if self.total > self.current:
                    eta = (self.total - self.current) / speed
        download_mode = (
            self.context.get("download_mode")
            if self.phase in {"checking_source", "downloading"}
            else None
        )
        if download_mode in {"warm", "queued", "waiting_storage"}:
            eta = None
        deadline_keys = ["retry_at", "backup_retry_at", "lineage_retry_at"]
        if self.phase == "source_retry_wait":
            deadline_keys.append("source_retry_at")
        deadlines = [
            value
            for key in deadline_keys
            if type(value := self.context.get(key)) in (int, float)
            and math.isfinite(value)
        ]
        result = {
            "retry_at": max(deadlines, default=None),
            "id": self.id,
            "status": self.status.value,
            "phase": self.phase,
            "current": self.current,
            "total": self.total,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress_updated_at": self.progress_updated_at,
            "elapsed_seconds": elapsed,
            "speed_bytes_per_second": speed,
            "eta_seconds": eta,
        }
        if self.phase in {"checking_source", "source_retry_wait", "downloading"}:
            from tgdrive.transfers.remote import REMOTE_MAX_ATTEMPTS

            result["source_attempt"] = self.context.get("source_attempt", 1)
            result["source_max_attempts"] = REMOTE_MAX_ATTEMPTS
        plan = self.context.get("multipart_plan")
        if download_mode in {"full", "warm", "queued", "waiting_storage"}:
            result["download_mode"] = download_mode
        parts = plan.get("parts") if isinstance(plan, dict) else None
        if isinstance(parts, list) and len(parts) > 1:
            result["telegram_parts"] = {
                "total": len(parts),
                "stored": sum(bool(part.get("confirmed")) for part in parts),
            }
        folder_upload = self.context.get("folder_upload")
        folder_fields = ("id", "name", "root_path", "created_at")
        if isinstance(folder_upload, dict) and all(
            key in folder_upload for key in folder_fields
        ):
            result["folder_upload"] = {
                key: copy.deepcopy(folder_upload[key]) for key in folder_fields
            }
        if self.context.get("source_kind") == "torrent":
            result["name"] = self.context.get("name", "Torrent import")
            manifest = self.context.get("torrent_manifest")
            if isinstance(manifest, dict):
                result["torrent_files"] = {
                    "total": len(manifest["files"]),
                    "completed": len(self.context.get("torrent_completed", [])),
                }
        return result


class TransferNotFound(KeyError):
    pass


class TransferRegistry:
    """Own transfer records and ensure spawned tasks are observed and drained."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 3600,
        max_entries: int = 1000,
        cancel_grace_seconds: float = _DEFAULT_CANCEL_GRACE_SECONDS,
        database_path: Path | None = None,
        mongo=None,
        mongo_backend=None,
        demo_mode: bool = False,
    ) -> None:
        if cancel_grace_seconds < 0:
            raise ValueError("cancel_grace_seconds cannot be negative")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._cancel_grace = cancel_grace_seconds
        self.demo_mode = demo_mode
        self._records: dict[str, TransferRecord] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._finalizers: set[asyncio.Task[None]] = set()
        self._journal_lock = asyncio.Lock()
        self._shutting_down = False
        self._mongo = mongo
        self._mongo_backend = mongo_backend
        self._durable: dict[str, dict[str, Any]] = {}
        self._last_progress_commit: dict[str, float] = {}
        self._logged: dict[str, tuple[TransferStatus, str, bool, float]] = {}
        self._history: dict[str, dict[str, Any]] = {}
        if database_path is not None:
            raise ValueError(
                "Transfer persistence requires MongoDB, not a local database path"
            )
        if mongo is not None:
            for row in mongo.find({"archived": {"$ne": True}}):
                self._load_record(row["_id"], row["data"])
            if mongo_backend is not None:
                mongo.create_index([("archived", 1), ("created_at", -1), ("_id", -1)])
                mongo.create_index(
                    [("archived", 1), ("status", 1), ("created_at", -1), ("_id", -1)]
                )

    def _load_record(self, identity, item):
        if not isinstance(item, dict) or item.get("id") != identity:
            raise ValueError("job identity does not match its row")
        for field_name in ("current", "total"):
            if type(item.get(field_name)) is not int or item[field_name] < 0:
                raise ValueError("invalid byte count")
        for field_name in ("created_at", "updated_at"):
            if type(item.get(field_name)) not in (
                int,
                float,
            ) or not math.isfinite(item[field_name]):
                raise ValueError("invalid job timestamp")
        for field_name in (
            "started_at",
            "finished_at",
            "progress_updated_at",
        ):
            timestamp = item.get(field_name)
            if timestamp is not None and (
                type(timestamp) not in (int, float) or not math.isfinite(timestamp)
            ):
                raise ValueError("invalid optional job timestamp")
        if not isinstance(item.get("context"), dict) or not isinstance(
            item.get("phase"), str
        ):
            raise ValueError("invalid job context")
        item["status"] = TransferStatus(item["status"])
        item.setdefault("started_at", None)
        item.setdefault(
            "finished_at",
            item["updated_at"] if item["status"] in _TERMINAL else None,
        )
        item.setdefault("progress_updated_at", None)
        record = TransferRecord(**item)
        if record.context.get("cancel_requested"):
            record.cancel_event.set()
        self._records[item["id"]] = record
        self._durable[item["id"]] = copy.deepcopy(item)
        self._logged[record.id] = self._log_snapshot(record)

    async def _write(self, operation, *args, **kwargs):
        if self._mongo_backend is not None:
            return await asyncio.to_thread(
                self._mongo_backend.write, self._mongo, operation, *args, **kwargs
            )
        return await asyncio.to_thread(getattr(self._mongo, operation), *args, **kwargs)

    @_journalled
    async def persist(self, record: TransferRecord) -> None:
        context = record.context
        async with self._journal_lock:
            if record.context is not context:
                raise DatabaseUnavailable(
                    "Transfer state changed after a failed commit"
                )
            await self._persist(record)

    @_journalled
    async def prune(self, *, now: float | None = None) -> None:
        async with self._journal_lock:
            await self._prune(now=now)

    async def _persist(self, record: TransferRecord) -> None:
        # Lifecycle/context commits are immediate; byte counters are advisory.
        if record.status is TransferStatus.RUNNING and record.started_at is None:
            record.started_at = time.time()
        if record.terminal:
            if record.finished_at is None:
                record.finished_at = time.time()
            record._progress_samples.clear()
        else:
            record.finished_at = None
            record.context.pop("history_cleared", None)
        self._last_progress_commit[record.id] = time.monotonic()
        if self._mongo is None:
            record._accepted = True
            self._log_record(record)
            return
        item = {
            key: getattr(record, key)
            for key in (
                "id",
                "phase",
                "status",
                "current",
                "total",
                "error",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
                "progress_updated_at",
            )
        } | {"context": copy.deepcopy(record.context)}
        previous = self._durable.get(record.id)
        if previous is not None:
            record._committed = TransferRecord(
                **copy.deepcopy(previous),
                task=record.task,
                cancel_event=record.cancel_event,
            )
        try:
            await self._write(
                "replace_one",
                {"_id": record.id},
                {"_id": record.id, "data": item},
                upsert=True,
            )
        except (
            PyMongoError,
            DatabaseUnavailable,
            ValueError,
            TypeError,
        ):
            if previous is not None:
                for key, value in previous.items():
                    if key == "context":
                        record.context = copy.deepcopy(value)
                    else:
                        setattr(
                            record,
                            key,
                            TransferStatus(value) if key == "status" else value,
                        )
                if record.context.get("cancel_requested"):
                    record.cancel_event.set()
                else:
                    record.cancel_event.clear()
            raise DatabaseUnavailable(
                "Transfer state could not be saved to MongoDB"
            ) from None
        finally:
            record._committed = None
        self._durable[record.id] = copy.deepcopy(item)
        record._accepted = True
        self._log_record(record)

    @staticmethod
    def _log_snapshot(
        record: TransferRecord,
    ) -> tuple[TransferStatus, str, bool, float]:
        deadlines = [
            record.context.get(key)
            for key in (
                "retry_at",
                "channel_retry_at",
                "backup_retry_at",
                "lineage_retry_at",
            )
        ]
        plan = record.context.get("multipart_plan")
        if isinstance(plan, dict) and isinstance(plan.get("parts"), list):
            deadlines.extend(
                part.get("retry_at")
                for part in upload_effects(plan)
                if isinstance(part, dict)
            )
        retry_at = max(
            (
                value
                for value in deadlines
                if type(value) in (int, float) and math.isfinite(value)
            ),
            default=0.0,
        )
        return record.status, record.phase, record.started_at is not None, retry_at

    def _log_record(self, record: TransferRecord) -> None:
        """Log committed lifecycle changes, never byte callbacks or poll snapshots."""
        current = self._log_snapshot(record)
        previous = self._logged.get(record.id)
        self._logged[record.id] = current
        if previous is None:
            logger.debug("Transfer queued id=%s bytes=%s", record.id, record.total)
            return
        kind = record.context.get("kind")
        kind = (
            kind
            if kind in {"upload", "remote", "torrent", "delete", "receiving"}
            else "transfer"
        )
        now = time.time()
        if current[2] and not previous[2]:
            logger.info(
                "Transfer started id=%s kind=%s phase=%s bytes=%s queue_seconds=%.3f",
                record.id,
                kind,
                record.phase,
                record.total,
                max(0.0, (record.started_at or now) - record.created_at),
            )
        if record.status != previous[0] and record.status in {
            TransferStatus.COMPLETED,
            TransferStatus.CANCELLED,
        }:
            logger.info(
                "Transfer finished id=%s kind=%s status=%s bytes=%s/%s "
                "elapsed_seconds=%.3f",
                record.id,
                kind,
                record.status.value,
                record.current,
                record.total,
                max(0.0, (record.finished_at or now) - record.created_at),
            )
        if current[3] > max(now, previous[3]):
            reason = (
                "recovery_checkpoint"
                if record.context.get("backup_retry_at") == current[3]
                else "catalog_verification"
                if record.context.get("lineage_retry_at") == current[3]
                else "telegram_cooldown"
            )
            logger.warning(
                "Transfer retry scheduled id=%s reason=%s retry_after_seconds=%s; "
                "retry is automatic",
                record.id,
                reason,
                math.ceil(current[3] - now),
            )
        if record.phase == "waiting_for_telegram" and previous[1] != record.phase:
            logger.warning(
                "Transfer waiting id=%s reason=no_telegram_client; "
                "check Connections; retry is automatic",
                record.id,
            )
        if current[:2] != previous[:2]:
            logger.debug(
                "Transfer state id=%s status=%s phase=%s bytes=%s/%s",
                record.id,
                record.status.value,
                record.phase,
                record.current,
                record.total,
            )

    def _retained(self, record: TransferRecord) -> bool:
        record = record.committed
        context = record.context
        archive_parent = self._records.get(context.get("archive_parent_id"))
        if (
            archive_parent is not None
            and context.get("archive_file_index")
            not in archive_parent.committed.context.get("archive_completed", [])
            and archive_parent.status
            not in {TransferStatus.COMPLETED, TransferStatus.CANCELLED}
        ):
            return True
        parent = self._records.get(context.get("torrent_parent_id"))
        if (
            parent is not None
            and context.get("torrent_file_index")
            not in parent.committed.context.get("torrent_completed", [])
            and parent.status
            not in {
                TransferStatus.COMPLETED,
                TransferStatus.CANCELLED,
            }
        ):
            return True
        deadlines = [context.get("retry_at"), context.get("channel_retry_at")]
        plan = context.get("multipart_plan")
        if isinstance(plan, dict) and isinstance(plan.get("parts"), list):
            deadlines.extend(
                part.get("retry_at")
                for part in upload_effects(plan)
                if isinstance(part, dict)
            )
        if any(
            type(deadline) in (int, float)
            and math.isfinite(deadline)
            and deadline > time.time()
            for deadline in deadlines
        ):
            return True
        if record.context.get("kind") in {
            "upload",
            "remote",
            "torrent",
            "archive",
            "extract",
            "delete",
        } and record.status not in {TransferStatus.COMPLETED, TransferStatus.CANCELLED}:
            return True
        source = record.context.get("local_path")
        directory = record.context.get("staging_directory")
        return bool(
            (source and Path(source).exists())
            or (directory and Path(directory).exists())
        )

    @staticmethod
    def history_snapshot(
        record: TransferRecord, *, retained: bool = True
    ) -> dict[str, Any]:
        """Public summary without source URLs, credentials or payload context."""
        record = record.committed
        summary = record.as_dict() | {
            "name": record.context.get("name") or "Transfer",
            "kind": record.context.get(
                "source_kind", record.context.get("kind", "upload")
            ),
            "destination": record.context.get("destination", "/"),
            "retained": retained,
        }
        if not retained:
            summary["error"] = (
                "Transfer failed" if record.status is TransferStatus.ERROR else None
            )
        return summary

    async def _remove(self, transfer_id: str) -> None:
        record = self._records[transfer_id]
        summary = (
            self.history_snapshot(record, retained=False)
            if record.terminal
            and not record.history_hidden
            and record.context.get("kind") != "delete"
            and not record.context.get("torrent_parent_id")
            and not record.context.get("archive_parent_id")
            else None
        )
        if self._mongo is not None:
            if summary is not None:
                args = (
                    {"_id": transfer_id},
                    {
                        "_id": transfer_id,
                        "archived": True,
                        "created_at": record.created_at,
                        "status": record.status.value,
                        "search_name": unicodedata.normalize(
                            "NFC", summary["name"]
                        ).casefold(),
                        "summary": summary,
                    },
                )
                await self._write("replace_one", *args)
            else:
                await self._write("delete_one", {"_id": transfer_id})
        if self._mongo is None and summary is not None:
            self._history[transfer_id] = summary
            # Nonpersistent test runtimes stay bounded; MongoDB archives are paged.
            if len(self._history) > self._max_entries:
                oldest = min(
                    self._history, key=lambda key: self._history[key]["created_at"]
                )
                del self._history[oldest]
        self._records.pop(transfer_id, None)
        self._durable.pop(transfer_id, None)
        self._last_progress_commit.pop(transfer_id, None)
        self._logged.pop(transfer_id, None)

    @staticmethod
    def _history_matches(row, statuses, before, query=""):
        return (
            row.get("kind") != "delete"
            and (not statuses or row["status"] in statuses)
            and (before is None or (row["created_at"], row["id"]) < before)
            and query
            in unicodedata.normalize("NFC", str(row.get("name", ""))).casefold()
        )

    def _archived_history(self, statuses, before, limit, term=""):
        if self._mongo is not None:
            query: dict[str, Any] = {"archived": True}
            if statuses:
                query["status"] = {"$in": list(statuses)}
            if term:
                query["search_name"] = {"$regex": re.escape(term)}
            if before is not None:
                at, identity = before
                query["$or"] = [
                    {"created_at": {"$lt": at}},
                    {"created_at": at, "_id": {"$lt": identity}},
                ]
            cursor = self._mongo.find(query).sort([("created_at", -1), ("_id", -1)])
            return [row["summary"] for row in cursor.limit(limit)]
        return sorted(
            (
                row
                for row in list(self._history.values())
                if self._history_matches(row, statuses, before, term)
            ),
            key=lambda row: (row["created_at"], row["id"]),
            reverse=True,
        )[:limit]

    async def history(self, *, status="all", before=None, limit=50, query=""):
        statuses = {
            "all": (),
            "finished": ("completed", "error", "cancelled"),
            "queued": ("queued",),
            "running": ("running", "cancelling"),
            "completed": ("completed",),
            "error": ("error",),
            "cancelled": ("cancelled",),
        }
        if (
            status not in statuses
            or not 1 <= limit <= 200
            or not isinstance(query, str)
            or len(query) > 255
        ):
            raise ValueError("Invalid transfer history page")
        query = unicodedata.normalize("NFC", query.strip()).casefold()
        selected = statuses[status]
        # Snapshot live progress on the event loop; only the DB lookup runs in a thread.
        live = [
            row
            for record in self.list()
            if not record.history_hidden
            and self._history_matches(
                row := self.history_snapshot(record), selected, before, query
            )
        ]
        archived = await asyncio.to_thread(
            self._archived_history, selected, before, limit + 1, query
        )
        merged = {row["id"]: row for row in archived}
        merged.update({row["id"]: row for row in live})
        ordered = sorted(
            merged.values(),
            key=lambda row: (row["created_at"], row["id"]),
            reverse=True,
        )
        has_more = len(ordered) > limit
        page = ordered[:limit]
        return {
            "jobs": page,
            "has_more": has_more,
            "next_before": page[-1]["created_at"] if has_more else None,
            "next_before_id": page[-1]["id"] if has_more else None,
        }

    @staticmethod
    def _history_task_id(row):
        folder = row.get("folder_upload")
        if (
            row.get("kind") in {"upload", "receiving"}
            and isinstance(folder, dict)
            and isinstance(folder.get("id"), str)
            and folder["id"]
        ):
            return "folder:" + folder["id"]
        return "job:" + row["id"]

    def _task_archive_rows(self, selected=None):
        """Stream summaries; numbered pages never materialize the whole archive."""
        if self._mongo is not None:
            query: dict[str, Any] = {"archived": True}
            if selected is not None:
                query["$or"] = [
                    {
                        "summary.folder_upload.id": {
                            "$in": [
                                key[7:] for key in selected if key.startswith("folder:")
                            ]
                        }
                    },
                    {
                        "_id": {
                            "$in": [
                                key[4:] for key in selected if key.startswith("job:")
                            ]
                        }
                    },
                ]
            cursor = self._mongo.find(query)
            for document in cursor.batch_size(200):
                yield document["summary"]
            return
        for row in list(self._history.values()):
            if selected is None or self._history_task_id(row) in selected:
                yield copy.deepcopy(row)

    @staticmethod
    def _task_status(statuses):
        if statuses & {"running", "cancelling"}:
            return "running"
        if "queued" in statuses:
            return "queued"
        if "error" in statuses:
            return "error"
        return "completed" if statuses == {"completed"} else "cancelled"

    def _numbered_history(
        self, live, *, status, page, limit, query, offset, exclude_folders
    ):
        live_ids = {row["id"] for row in live}
        tasks = {}
        excluded = {"folder:" + identity for identity in exclude_folders}

        def observe(row):
            if row.get("kind") == "delete":
                return
            identity = self._history_task_id(row)
            if identity in excluded:
                return
            folder = row.get("folder_upload")
            created_at = (
                folder.get("created_at", row["created_at"])
                if identity.startswith("folder:")
                else row["created_at"]
            )
            entry = tasks.setdefault(
                identity,
                {"created_at": created_at, "statuses": set(), "matches": False},
            )
            entry["created_at"] = min(entry["created_at"], created_at)
            entry["statuses"].add(row["status"])
            names = [row.get("name", "")]
            if identity.startswith("folder:"):
                names.append(folder.get("name", ""))
            entry["matches"] |= any(
                query in unicodedata.normalize("NFC", str(name)).casefold()
                for name in names
            )

        for row in self._task_archive_rows():
            if row["id"] not in live_ids:
                observe(row)
        for row in live:
            observe(row)
        matching = [
            identity
            for identity, entry in tasks.items()
            if entry["matches"]
            and (
                status == "all"
                or (
                    status == "finished"
                    and self._task_status(entry["statuses"])
                    in {"completed", "cancelled", "error"}
                )
                or status == self._task_status(entry["statuses"])
            )
        ]
        matching.sort(
            key=lambda identity: (tasks[identity]["created_at"], identity), reverse=True
        )
        total = len(matching)
        pages = max(1, (total + limit - 1) // limit)
        if offset is None:
            page = min(page, pages)
            offset = (page - 1) * limit
        selected = matching[offset : offset + limit]
        selected_ids = set(selected)
        members = {}
        if selected:
            for row in self._task_archive_rows(selected_ids):
                if (
                    row["id"] not in live_ids
                    and self._history_task_id(row) in selected_ids
                ):
                    members[row["id"]] = row
            for row in live:
                if self._history_task_id(row) in selected_ids:
                    members[row["id"]] = row
        order = {identity: index for index, identity in enumerate(selected)}
        jobs = sorted(
            members.values(),
            key=lambda row: (
                order[self._history_task_id(row)],
                row["created_at"],
                row["id"],
            ),
        )
        return {
            "jobs": jobs,
            "page": page,
            "page_size": limit,
            "total_tasks": total,
            "total_pages": pages,
            "has_more": offset + limit < total,
            "offset": offset,
            "next_offset": min(total, offset + limit),
        }

    async def task_history(
        self,
        *,
        status="all",
        page=1,
        limit=20,
        query="",
        offset=None,
        exclude_folders=(),
    ):
        if (
            status
            not in {
                "all",
                "finished",
                "queued",
                "running",
                "completed",
                "error",
                "cancelled",
            }
            or type(page) is not int
            or page < 1
            or type(limit) is not int
            or not 1 <= limit <= 200
            or not isinstance(query, str)
            or len(query) > 255
            or (offset is not None and (type(offset) is not int or offset < 0))
        ):
            raise ValueError("Invalid transfer task page")
        # Progress and recovery contexts belong to the event loop. The worker
        # sees copied public snapshots and streams archived summaries in batches.
        live = [
            self.history_snapshot(record)
            for record in self.list()
            if not record.history_hidden
        ]
        return await asyncio.to_thread(
            self._numbered_history,
            live,
            status=status,
            page=page,
            limit=limit,
            query=unicodedata.normalize("NFC", query.strip()).casefold(),
            offset=offset,
            exclude_folders=tuple(exclude_folders),
        )

    @_journalled
    async def clear_history(self, *, exclude_folders=()) -> int:
        """Forget finished display history without discarding recovery contexts."""
        async with self._journal_lock:
            active_folders = set(exclude_folders)
            for record in self._records.values():
                folder = record.context.get("folder_upload")
                if not record.terminal and isinstance(folder, dict):
                    active_folders.add(folder["id"])

            if self._mongo is not None:
                query: dict[str, Any] = {
                    "archived": True,
                    "status": {"$in": [status.value for status in _TERMINAL]},
                    "summary.folder_upload.id": {"$nin": sorted(active_folders)},
                }
                result = await self._write("delete_many", query)
                cleared = result.deleted_count
            else:
                removed = [
                    identity
                    for identity, row in self._history.items()
                    if row.get("folder_upload", {}).get("id") not in active_folders
                ]
                for identity in removed:
                    del self._history[identity]
                cleared = len(removed)

            for record in self._records.values():
                folder = record.context.get("folder_upload", {})
                if (
                    record.terminal
                    and not record.history_hidden
                    and record.context.get("kind") != "delete"
                    and folder.get("id") not in active_folders
                ):
                    # Retained errors may still own staged bytes, send identities or
                    # backup barriers. Hide their history, keeping recovery intact.
                    record.context["history_cleared"] = True
                    await self._persist(record)
                    cleared += 1
            return cleared

    def close(self) -> None:
        if self._tasks or self._finalizers:
            raise RuntimeError("Drain transfer tasks before closing the journal")

    def list(self) -> list[TransferRecord]:
        return sorted(
            (record for record in self._records.values() if record._accepted),
            key=lambda item: item.created_at,
            reverse=True,
        )

    def staging_records(self) -> list[TransferRecord]:
        """Include unacknowledged reservations when admitting another upload."""
        return list(self._records.values())

    @_journalled
    async def retry(self, transfer_id: str) -> TransferRecord:
        async with self._journal_lock:
            record = self.get(transfer_id)
            if record.status not in {TransferStatus.ERROR, TransferStatus.QUEUED}:
                raise ValueError("Only failed or interrupted transfers can be retried")
            record.status = TransferStatus.QUEUED
            record.error = None
            record.context.pop("restart_retryable", None)
            record._progress_samples.clear()
            record.cancel_event.clear()
            await self._persist(record)
            logger.info("Transfer retry requested id=%s", record.id)
            return record

    @_journalled
    async def create(
        self,
        phase: str,
        *,
        total: int = 0,
        transfer_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> TransferRecord:
        async with self._journal_lock:
            await self._prune()
            if transfer_id is not None and transfer_id in self._records:
                return self._records[transfer_id]
            if self._shutting_down:
                raise RuntimeError("Transfer registry is shutting down")
            if len(self._records) >= self._max_entries:
                terminal = sorted(
                    (
                        r
                        for r in self._records.values()
                        if r.terminal
                        and not self._retained(r)
                        and (r.task is None or r.task.done())
                    ),
                    key=lambda record: record.updated_at,
                )
                if terminal:
                    await self._remove(terminal[0].id)
                else:
                    raise RuntimeError("too many active transfers")
            transfer_id = transfer_id or secrets.token_urlsafe(18)
            if transfer_id in self._records:
                return self._records[transfer_id]
            record = TransferRecord(
                id=transfer_id,
                phase=phase,
                total=max(0, total),
                context=copy.deepcopy(context) if context is not None else {},
                _accepted=False,
            )
            from tgdrive.storage.demo_files import restricted_writes

            if self.demo_mode and restricted_writes.get():
                record.context["demo_read_only"] = True
            self._records[transfer_id] = record
            try:
                await self._persist(record)
            except BaseException:
                self._records.pop(transfer_id, None)
                raise
            return record

    def get(self, transfer_id: str) -> TransferRecord:
        try:
            record = self._records[transfer_id]
        except KeyError as exc:
            raise TransferNotFound(transfer_id) from exc
        if not record._accepted:
            raise TransferNotFound(transfer_id)
        return record

    @_journalled
    async def update(
        self,
        transfer_id: str,
        *,
        status: TransferStatus | None = None,
        phase: str | None = None,
        current: int | None = None,
        total: int | None = None,
        error=_ERROR_UNSET,
        checkpoint_progress: bool = True,
    ) -> TransferRecord:
        async with self._journal_lock:
            record = self.get(transfer_id)
            # Terminal snapshots are immutable.  In particular, a late progress
            # callback from a transport that is winding down after cancellation
            # must not change the phase/bytes the API has already reported.
            if record.terminal:
                return record
            previous_phase = record.phase
            previous_status = record.status
            previous_current = record.current
            previous_total = record.total
            previous_error = record.error
            if status is not None:
                record.status = status
            if phase is not None:
                record.phase = phase
            if current is not None:
                record.current = max(0, current)
            if total is not None:
                record.total = max(0, total)
            if error is not _ERROR_UNSET:
                record.error = error[:500] if error is not None else None
            now = time.time()
            monotonic = time.monotonic()
            record.updated_at = now
            transition = (
                record.phase != previous_phase or record.status != previous_status
            )
            if (
                transition
                or record.current < previous_current
                or record.total != previous_total
                or record.phase not in _BYTE_PHASES
                or record.status is not TransferStatus.RUNNING
            ):
                record._progress_samples.clear()
            if (
                record.phase in _BYTE_PHASES
                and record.status is TransferStatus.RUNNING
                and current is not None
            ):
                record.progress_updated_at = now
                record._progress_samples.append((monotonic, record.current))
                while (
                    len(record._progress_samples) > 2
                    and record._progress_samples[1][0] < monotonic - 5
                ):
                    record._progress_samples.popleft()
            # Never throttle phase/status/error or recovery-context changes. Staging
            # write counts are advisory, like current; they are not resumable receipt
            # offsets or Telegram send identities. URL retrieval restarts from zero.
            durable = self._durable.get(record.id)
            context_changed = durable is not None and {
                key: value
                for key, value in durable["context"].items()
                if key != "staging_written"
            } != {
                key: value
                for key, value in record.context.items()
                if key != "staging_written"
            }
            if (
                transition
                or record.error != previous_error
                or context_changed
                or (
                    checkpoint_progress
                    and self._mongo is None
                    and monotonic - self._last_progress_commit.get(record.id, 0)
                    >= _PROGRESS_JOURNAL_SECONDS
                )
            ):
                await self._persist(record)
            return record

    @_journalled
    async def cancel(self, transfer_id: str) -> TransferRecord:
        async with self._journal_lock:
            record = self.get(transfer_id)
            if record.context.get("kind") == "delete" and not record.terminal:
                raise ValueError(
                    "Permanent deletion cannot be cancelled after it starts"
                )
            if not record.terminal or (
                record.status == TransferStatus.ERROR and self._retained(record)
            ):
                candidate = replace(
                    record,
                    context=dict(record.context, cancel_requested=True),
                    cancel_event=asyncio.Event(),
                    _progress_samples=record._progress_samples.copy(),
                )
                running = record.task is not None and not record.task.done()
                ambiguous = bool(record.context.get("send_started"))
                if ambiguous:
                    candidate.status = (
                        TransferStatus.CANCELLING if running else TransferStatus.QUEUED
                    )
                    candidate.phase = "cleanup_pending"
                    candidate.error = (
                        "Cancellation requested; Telegram cleanup is pending"
                    )
                else:
                    candidate.status = (
                        TransferStatus.CANCELLING
                        if running
                        else TransferStatus.CANCELLED
                    )
                    candidate.phase = "cancelling"
                    candidate.error = "Transfer cancelled"
                candidate.updated_at = time.time()
                await self._persist(candidate)
                for key in ("status", "phase", "error", "updated_at", "finished_at"):
                    setattr(record, key, getattr(candidate, key))
                record.context["cancel_requested"] = True
                record.cancel_event.set()
                if record.terminal:
                    record._progress_samples.clear()
                if running:
                    record.cancel_handle = asyncio.get_running_loop().call_later(
                        self._cancel_grace, self._force_cancel, record
                    )
            return record

    def spawn(
        self,
        transfer_id: str,
        work: Callable[[TransferRecord], Awaitable[None]],
    ) -> asyncio.Task[None]:
        record = self.get(transfer_id)
        if record.task is not None and not record.task.done():
            return record.task
        if record.terminal:
            raise ValueError("Terminal transfers cannot be started again without retry")

        started = False

        async def runner() -> None:
            nonlocal started
            started = True
            try:
                from tgdrive.storage.demo_files import write_policy

                with write_policy(
                    self.demo_mode and record.context.get("demo_read_only") is True
                ):
                    await work(record)
            except asyncio.CancelledError:
                if (
                    self._mongo is not None
                    and self._shutting_down
                    and not record.cancel_event.is_set()
                ):
                    record.status = TransferStatus.QUEUED
                    record.phase = "interrupted"
                    await self.persist(record)
                elif record.context.get("send_started"):
                    record.status = TransferStatus.QUEUED
                    record.phase = (
                        "cleanup_pending"
                        if record.cancel_event.is_set()
                        else "interrupted"
                    )
                    await self.persist(record)
                elif not record.terminal:
                    await self.update(
                        record.id,
                        status=TransferStatus.CANCELLED,
                        error="Transfer cancelled",
                    )
                raise
            except Exception as exc:
                retryable = _retry_after_restart(exc)
                code, action = _failure_diagnostic(exc)
                logger.error(
                    "Transfer failed id=%s phase=%s error_type=%s retryable=%s "
                    "code=%s; %s",
                    record.id,
                    record.phase,
                    type(exc).__name__,
                    retryable,
                    code,
                    action,
                )
                logger.debug("Transfer failure detail id=%s", record.id, exc_info=True)
                if record.cancel_event.is_set() and record.context.get("send_started"):
                    record.status = TransferStatus.QUEUED
                    record.phase = "cleanup_pending"
                    record.error = str(exc)[:500] or type(exc).__name__
                    await self.persist(record)
                else:
                    if retryable:
                        record.context["restart_retryable"] = True
                    else:
                        record.context.pop("restart_retryable", None)
                    await self.update(
                        record.id,
                        status=TransferStatus.ERROR,
                        error=(
                            "The connection was interrupted. Retry this transfer, "
                            "or restart TGDrive after restoring the connection."
                            if retryable
                            else str(exc) or type(exc).__name__
                        ),
                    )
            finally:
                await self._settle_cancelled(record)

        task = asyncio.create_task(runner(), name=f"transfer:{transfer_id}")
        record.task = task
        self._tasks.add(task)
        task.add_done_callback(
            lambda done: self._task_done(record, done, started=started)
        )
        return task

    def _force_cancel(self, record: TransferRecord) -> None:
        task = record.task
        if task is not None and not task.done() and record.cancel_event.is_set():
            logger.warning(
                "Transfer %s did not stop cooperatively; forcing task cancellation",
                record.id,
            )
            task.cancel()

    def _task_done(
        self,
        record: TransferRecord,
        task: asyncio.Task[None],
        *,
        started: bool,
    ) -> None:
        self._tasks.discard(task)
        if record.cancel_handle is not None:
            record.cancel_handle.cancel()
            record.cancel_handle = None
        if not started and record.cancel_event.is_set() and not record.terminal:
            finalizer = asyncio.create_task(self._settle_cancelled(record))
            self._finalizers.add(finalizer)
            finalizer.add_done_callback(self._finalizer_done)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error(
                    "Transfer task %s failed during supervision",
                    record.id,
                    exc_info=(type(error), error, error.__traceback__),
                )

    @_journalled
    async def _settle_cancelled(self, record: TransferRecord) -> None:
        async with self._journal_lock:
            if record.cancel_event.is_set() and not record.terminal:
                if record.context.get("send_started"):
                    record.status = TransferStatus.QUEUED
                    record.phase = "cleanup_pending"
                else:
                    record.status = TransferStatus.CANCELLED
                    record.phase = "cancelled"
                await self._persist(record)

    def _finalizer_done(self, task: asyncio.Task[None]) -> None:
        self._finalizers.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "Transfer cancellation journal failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _prune(self, *, now: float | None = None) -> None:
        current = time.time() if now is None else now
        expired = [
            transfer_id
            for transfer_id, record in self._records.items()
            if record.terminal
            and not self._retained(record)
            and (record.task is None or record.task.done())
            and record.updated_at <= current - self._ttl
        ]
        for transfer_id in expired:
            await self._remove(transfer_id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._mongo is None:
            for record in list(self._records.values()):
                if not record.terminal:
                    await self.cancel(record.id)
        pending = set(self._tasks)
        for _ in range(2):
            for task in pending:
                task.cancel()
            if not pending:
                break
            _, pending = await asyncio.wait(pending, timeout=_SHUTDOWN_DRAIN_SECONDS)
        if pending:
            logger.error("Shutdown left %s stalled transfer task(s)", len(pending))
        while self._finalizers:
            await asyncio.gather(*tuple(self._finalizers), return_exceptions=True)
