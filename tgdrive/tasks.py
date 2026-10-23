"""Bounded transfer state and background-task supervision."""

from __future__ import annotations

import asyncio
import copy
import errno
import json
import logging
import math
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import weakref
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from aiohttp import ClientConnectionError, ClientPayloadError
from pymongo import UpdateOne
from pymongo.errors import PyMongoError
from telethon.errors import RpcCallFailError, ServerError, TimedOutError

from tgdrive.mongo import DatabaseUnavailable

logger = logging.getLogger(__name__)
_DEFAULT_CANCEL_GRACE_SECONDS = 10.0
_SHUTDOWN_DRAIN_SECONDS = 5.0
_PROGRESS_JOURNAL_SECONDS = 0.5
_PROGRESS_STALE_SECONDS = 15.0
_BYTE_PHASES = frozenset({"saving", "downloading", "uploading"})


def _retry_after_restart(error: Exception) -> bool:
    """Only known temporary transport failures may restart without owner input."""
    from tgdrive.backup import BackupError, RecoveryConflict
    from tgdrive.remote import RemoteHTTPError, RemoteResolutionError, RemoteTimeout

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
    from tgdrive.backup import BackupError, RecoveryConflict
    from tgdrive.connection_errors import connection_problem
    from tgdrive.remote import RemoteHTTPError, RemoteTooLarge, UnsafeRemoteURL

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

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL

    def as_dict(self) -> dict[str, Any]:
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
        deadlines = [
            value
            for key in ("retry_at", "backup_retry_at", "lineage_retry_at")
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
        plan = self.context.get("multipart_plan")
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
        self._shutting_down = False
        self._mongo = mongo
        self._mongo_backend = mongo_backend
        self._db: sqlite3.Connection | None = None
        self._db_lock = threading.RLock()
        self._durable: dict[str, dict[str, Any]] = {}
        self._last_progress_commit: dict[str, float] = {}
        self._logged: dict[str, tuple[TransferStatus, str, bool, float]] = {}
        self._history: dict[str, dict[str, Any]] = {}
        self._finalizer: weakref.finalize | None = None
        if mongo is not None:
            for row in mongo.find({"archived": {"$ne": True}}):
                self._load_record(row["_id"], row["data"])
            if mongo_backend is not None:
                mongo.create_index([("archived", 1), ("created_at", -1), ("_id", -1)])
                mongo.create_index(
                    [("archived", 1), ("status", 1), ("created_at", -1), ("_id", -1)]
                )
        if database_path is not None and mongo is None:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(database_path, check_same_thread=False)
            self._db.create_function(
                "casefold",
                1,
                lambda value: unicodedata.normalize("NFC", str(value or "")).casefold(),
                deterministic=True,
            )
            self._finalizer = weakref.finalize(self, self._db.close)
            try:
                database_path.chmod(0o600)
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=FULL")
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data "
                    "TEXT NOT NULL)"
                )
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS transfer_history ("
                    "id TEXT PRIMARY KEY, data TEXT NOT NULL, "
                    "created_at REAL NOT NULL, status TEXT NOT NULL)"
                )
                self._db.execute(
                    "CREATE INDEX IF NOT EXISTS history_page "
                    "ON transfer_history(created_at DESC,id DESC)"
                )
                try:
                    for identity, data in self._db.execute("SELECT id,data FROM jobs"):
                        self._load_record(identity, json.loads(data))
                except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                    self._db.close()
                    self._db = None
                    raise RuntimeError(
                        "Transfer journal is invalid; preserve it for recovery"
                    ) from exc
            except BaseException:
                self._finalizer()
                self._db = None
                raise

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

    def persist(self, record: TransferRecord) -> None:
        # Lifecycle/context commits are immediate; byte counters are advisory.
        if record.status is TransferStatus.RUNNING and record.started_at is None:
            record.started_at = time.time()
        if record.terminal:
            if record.finished_at is None:
                record.finished_at = time.time()
            record._progress_samples.clear()
        else:
            record.finished_at = None
        self._last_progress_commit[record.id] = time.monotonic()
        if self._db is None and self._mongo is None:
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
        try:
            if self._mongo is not None:
                if self._mongo_backend is not None:
                    self._mongo_backend.write(
                        self._mongo,
                        "replace_one",
                        {"_id": record.id},
                        {"_id": record.id, "data": item},
                        upsert=True,
                    )
                else:
                    self._mongo.replace_one(
                        {"_id": record.id},
                        {"_id": record.id, "data": item},
                        upsert=True,
                    )
            else:
                with self._db_lock, self._db:
                    self._db.execute(
                        "INSERT INTO jobs VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET "
                        "data=excluded.data",
                        (record.id, json.dumps(item, allow_nan=False)),
                    )
        except (
            sqlite3.Error,
            PyMongoError,
            DatabaseUnavailable,
            ValueError,
            TypeError,
        ) as exc:
            previous = self._durable.get(record.id)
            if previous is not None:
                for key, value in previous.items():
                    if key == "context":
                        record.context.clear()
                        record.context.update(copy.deepcopy(value))
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
            if self._mongo is not None:
                raise DatabaseUnavailable(
                    "Transfer state could not be saved to MongoDB"
                ) from None
            raise RuntimeError("Transfer state could not be persisted") from exc
        self._durable[record.id] = copy.deepcopy(item)
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
                part.get("retry_at") for part in plan["parts"] if isinstance(part, dict)
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
            kind if kind in {"upload", "remote", "delete", "receiving"} else "transfer"
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

    @staticmethod
    def _retained(record: TransferRecord) -> bool:
        context = record.context
        deadlines = [context.get("retry_at"), context.get("channel_retry_at")]
        plan = context.get("multipart_plan")
        if isinstance(plan, dict) and isinstance(plan.get("parts"), list):
            deadlines.extend(
                part.get("retry_at") for part in plan["parts"] if isinstance(part, dict)
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
            "delete",
        } and record.status not in {TransferStatus.COMPLETED, TransferStatus.CANCELLED}:
            return True
        source = record.context.get("local_path")
        return bool(source and Path(source).exists())

    @staticmethod
    def history_snapshot(
        record: TransferRecord, *, retained: bool = True
    ) -> dict[str, Any]:
        """Public summary without source URLs, credentials or payload context."""
        summary = record.as_dict() | {
            "name": record.context.get("name", "Transfer"),
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

    def _remove(self, transfer_id: str) -> None:
        record = self._records[transfer_id]
        summary = (
            self.history_snapshot(record, retained=False)
            if record.terminal and record.context.get("kind") != "delete"
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
                if self._mongo_backend is not None:
                    self._mongo_backend.write(self._mongo, "replace_one", *args)
                else:
                    self._mongo.replace_one(*args)
            elif self._mongo_backend is not None:
                self._mongo_backend.write(
                    self._mongo, "delete_one", {"_id": transfer_id}
                )
            else:
                self._mongo.delete_one({"_id": transfer_id})
        if self._db is not None:
            with self._db_lock, self._db:
                if summary is not None:
                    self._db.execute(
                        "INSERT OR REPLACE INTO transfer_history VALUES(?,?,?,?)",
                        (
                            transfer_id,
                            json.dumps(summary, allow_nan=False),
                            record.created_at,
                            record.status.value,
                        ),
                    )
                self._db.execute("DELETE FROM jobs WHERE id=?", (transfer_id,))
        if self._mongo is None and self._db is None and summary is not None:
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
                query["$and"] = [
                    {
                        "$or": [
                            {"search_name": {"$regex": re.escape(term)}},
                            {"search_name": {"$exists": False}},
                        ]
                    }
                ]
            if before is not None:
                at, identity = before
                query["$or"] = [
                    {"created_at": {"$lt": at}},
                    {"created_at": at, "_id": {"$lt": identity}},
                ]
            cursor = self._mongo.find(query).sort([("created_at", -1), ("_id", -1)])
            if not term:
                return [row["summary"] for row in cursor.limit(limit)]
            # Old installations have no canonical search key. Stream bounded
            # batches and backfill keys automatically; never load the archive
            # into application memory or paginate before applying the search.
            rows, updates = [], []

            def backfill():
                if not updates:
                    return
                try:
                    if self._mongo_backend is not None:
                        self._mongo_backend.write(
                            self._mongo, "bulk_write", list(updates), ordered=False
                        )
                    else:
                        self._mongo.bulk_write(list(updates), ordered=False)
                except (PyMongoError, DatabaseUnavailable):
                    logger.warning(
                        "Could not update transfer search keys; retry on later search"
                    )
                updates.clear()

            for row in cursor.batch_size(200):
                name = row.get("search_name")
                if name is None:
                    name = unicodedata.normalize(
                        "NFC", str(row["summary"].get("name", ""))
                    ).casefold()
                    updates.append(
                        UpdateOne({"_id": row["_id"]}, {"$set": {"search_name": name}})
                    )
                if term in name:
                    rows.append(row["summary"])
                if len(updates) >= 200:
                    backfill()
                if len(rows) >= limit:
                    break
            backfill()
            return rows
        if self._db is not None:
            at, identity = before if before is not None else (None, "")
            with self._db_lock:
                return [
                    json.loads(row[0])
                    for row in self._db.execute(
                        "SELECT data FROM transfer_history "
                        "WHERE (? OR status IN (SELECT value FROM json_each(?))) "
                        "AND (? IS NULL OR created_at < ? "
                        "OR (created_at = ? AND id < ?)) "
                        "AND instr(casefold(json_extract(data,'$.name')),?) > 0 "
                        "ORDER BY created_at DESC,id DESC LIMIT ?",
                        (
                            not statuses,
                            json.dumps(statuses),
                            at,
                            at,
                            at,
                            identity,
                            term,
                            limit,
                        ),
                    ).fetchall()
                ]
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
            if self._history_matches(
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
                            "$in": [key[4:] for key in selected if key.startswith("job:")]
                        }
                    },
                ]
            cursor = self._mongo.find(query)
            for document in cursor.batch_size(200):
                yield document["summary"]
            return
        if self._db is not None:
            # Release the shared connection between small reads so lifecycle
            # commits do not wait behind a scan of a large transfer archive.
            after = ""
            while True:
                with self._db_lock:
                    rows = self._db.execute(
                        "SELECT id,data FROM transfer_history WHERE id>? "
                        "ORDER BY id LIMIT 200",
                        (after,),
                    ).fetchall()
                if not rows:
                    return
                for identity, data in rows:
                    row = json.loads(data)
                    if selected is None or self._history_task_id(row) in selected:
                        yield row
                after = rows[-1][0]
        else:
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
        self, live, *, status, page, limit, query, offset, folders, exclude_folders
    ):
        live_ids = {row["id"] for row in live}
        tasks = {}
        hints = {
            identity: hint["folder_upload"]
            for hint in folders
            for identity in hint["transfer_ids"]
        }
        excluded = {"folder:" + identity for identity in exclude_folders}

        def decorate(row):
            if (
                not isinstance(row.get("folder_upload"), dict)
                and row["id"] in hints
            ):
                return row | {"folder_upload": hints[row["id"]]}
            return row

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
                observe(decorate(row))
        for row in live:
            observe(decorate(row))
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
            for row in self._task_archive_rows(None if hints else selected_ids):
                row = decorate(row)
                if (
                    row["id"] not in live_ids
                    and self._history_task_id(row) in selected_ids
                ):
                    members[row["id"]] = row
            for row in live:
                row = decorate(row)
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
        folders=(),
        exclude_folders=(),
    ):
        if (
            status not in {
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
        live = [self.history_snapshot(record) for record in self.list()]
        return await asyncio.to_thread(
            self._numbered_history,
            live,
            status=status,
            page=page,
            limit=limit,
            query=unicodedata.normalize("NFC", query.strip()).casefold(),
            offset=offset,
            folders=copy.deepcopy(folders),
            exclude_folders=tuple(exclude_folders),
        )

    def close(self) -> None:
        if self._tasks:
            raise RuntimeError("Drain transfer tasks before closing the journal")
        with self._db_lock:
            if self._finalizer is not None:
                self._finalizer()
                self._db = None

    def list(self) -> list[TransferRecord]:
        return sorted(
            self._records.values(), key=lambda item: item.created_at, reverse=True
        )

    def retry(self, transfer_id: str) -> TransferRecord:
        record = self.get(transfer_id)
        if record.status not in {TransferStatus.ERROR, TransferStatus.QUEUED}:
            raise ValueError("Only failed or interrupted transfers can be retried")
        record.status = TransferStatus.QUEUED
        record.error = None
        record.context.pop("restart_retryable", None)
        record._progress_samples.clear()
        record.cancel_event.clear()
        self.persist(record)
        logger.info("Transfer retry requested id=%s", record.id)
        return record

    def create(
        self, phase: str, *, total: int = 0, transfer_id: str | None = None
    ) -> TransferRecord:
        self.prune()
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
                self._remove(terminal[0].id)
            else:
                raise RuntimeError("too many active transfers")
        transfer_id = transfer_id or secrets.token_urlsafe(18)
        if transfer_id in self._records:
            return self._records[transfer_id]
        record = TransferRecord(id=transfer_id, phase=phase, total=max(0, total))
        from tgdrive.demo_files import restricted_writes

        if self.demo_mode and restricted_writes.get():
            record.context["demo_read_only"] = True
        self._records[transfer_id] = record
        try:
            self.persist(record)
        except BaseException:
            self._records.pop(transfer_id, None)
            raise
        return record

    def get(self, transfer_id: str) -> TransferRecord:
        self.prune()
        try:
            return self._records[transfer_id]
        except KeyError as exc:
            raise TransferNotFound(transfer_id) from exc

    def update(
        self,
        transfer_id: str,
        *,
        status: TransferStatus | None = None,
        phase: str | None = None,
        current: int | None = None,
        total: int | None = None,
        error: str | None = None,
        checkpoint_progress: bool = True,
    ) -> TransferRecord:
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
        if status is not None:
            record.status = status
        if phase is not None:
            record.phase = phase
        if current is not None:
            record.current = max(0, current)
        if total is not None:
            record.total = max(0, total)
        if error is not None:
            record.error = error[:500]
        now = time.time()
        monotonic = time.monotonic()
        record.updated_at = now
        transition = record.phase != previous_phase or record.status != previous_status
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
            or error is not None
            or context_changed
            or (
                checkpoint_progress
                and self._mongo is None
                and monotonic - self._last_progress_commit.get(record.id, 0)
                >= _PROGRESS_JOURNAL_SECONDS
            )
        ):
            self.persist(record)
        return record

    def cancel(self, transfer_id: str) -> TransferRecord:
        record = self.get(transfer_id)
        if record.context.get("kind") == "delete" and not record.terminal:
            raise ValueError("Permanent deletion cannot be cancelled after it starts")
        if not record.terminal or (
            record.status == TransferStatus.ERROR and self._retained(record)
        ):
            record.cancel_event.set()
            record.context["cancel_requested"] = True
            running = record.task is not None and not record.task.done()
            ambiguous = bool(record.context.get("send_started"))
            if ambiguous:
                record.status = (
                    TransferStatus.CANCELLING if running else TransferStatus.QUEUED
                )
                record.phase = "cleanup_pending"
                record.error = "Cancellation requested; Telegram cleanup is pending"
            else:
                record.status = (
                    TransferStatus.CANCELLING if running else TransferStatus.CANCELLED
                )
                record.phase = "cancelling"
                record.error = "Transfer cancelled"
            record.updated_at = time.time()
            self.persist(record)
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

        async def runner() -> None:
            try:
                from tgdrive.demo_files import write_policy

                with write_policy(
                    self.demo_mode and record.context.get("demo_read_only") is True
                ):
                    await work(record)
            except asyncio.CancelledError:
                if (
                    (self._db is not None or self._mongo is not None)
                    and self._shutting_down
                    and not record.cancel_event.is_set()
                ):
                    record.status = TransferStatus.QUEUED
                    record.phase = "interrupted"
                    self.persist(record)
                elif record.context.get("send_started"):
                    record.status = TransferStatus.QUEUED
                    record.phase = (
                        "cleanup_pending"
                        if record.cancel_event.is_set()
                        else "interrupted"
                    )
                    self.persist(record)
                elif not record.terminal:
                    self.update(
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
                    self.persist(record)
                else:
                    if retryable:
                        record.context["restart_retryable"] = True
                    else:
                        record.context.pop("restart_retryable", None)
                    self.update(
                        record.id,
                        status=TransferStatus.ERROR,
                        error=(
                            "The connection was interrupted. Retry this transfer, "
                            "or restart TGDrive after restoring the connection."
                            if retryable
                            else str(exc) or type(exc).__name__
                        ),
                    )

        task = asyncio.create_task(runner(), name=f"transfer:{transfer_id}")
        record.task = task
        self._tasks.add(task)
        task.add_done_callback(lambda done: self._task_done(record, done))
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
    ) -> None:
        self._tasks.discard(task)
        if record.cancel_handle is not None:
            record.cancel_handle.cancel()
            record.cancel_handle = None
        if record.cancel_event.is_set() and not record.terminal:
            if record.context.get("send_started"):
                record.status = TransferStatus.QUEUED
                record.phase = "cleanup_pending"
            else:
                record.status = TransferStatus.CANCELLED
                record.phase = "cancelled"
            self.persist(record)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error(
                    "Transfer task %s failed during supervision",
                    record.id,
                    exc_info=(type(error), error, error.__traceback__),
                )

    def prune(self, *, now: float | None = None) -> None:
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
            self._remove(transfer_id)

    async def shutdown(self) -> None:
        # cancel() calls get(), which may prune expired terminal records.
        # Iterate a snapshot so that pruning cannot resize this dictionary.
        self._shutting_down = True
        if self._db is None and self._mongo is None:
            for record in list(self._records.values()):
                if not record.terminal:
                    self.cancel(record.id)
        pending = set(self._tasks)
        for _ in range(2):
            for task in pending:
                task.cancel()
            if not pending:
                return
            _, pending = await asyncio.wait(pending, timeout=_SHUTDOWN_DRAIN_SECONDS)
        if pending:
            logger.error("Shutdown left %s stalled transfer task(s)", len(pending))
