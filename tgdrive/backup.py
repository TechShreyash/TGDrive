"""Append-only checkpoints and explicit, fail-closed metadata restoration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tgdrive.client_pool import ClientPool
from tgdrive.config import Settings
from tgdrive.store import MAX_STORE_BYTES, DriveStore

logger = logging.getLogger(__name__)


class BackupError(RuntimeError):
    """A checkpoint or explicit restore failed; retry timing contains no credentials."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RecoveryConflict(BackupError):
    """Local metadata cannot safely mutate the configured remote drive."""

    code = "RECOVERY_REQUIRED"


_MARKER = re.compile(
    r"\ATGDrive checkpoint v1\nDrive: ([A-Za-z0-9_-]{1,128})"
    r"\nChannel: (-?[0-9]+)\nRevision: ([0-9]+)\nSHA256: ([0-9a-f]{64})\Z"
)


class TelegramBackupService:
    """Keep local SQLite authoritative and append recoverable Telegram checkpoints.

    A checkpoint's random ID and immutable source bytes are persisted before the
    Telegram call. Retrying a response lost in transit reuses both. Local history
    follows a bounded retention policy; remote checkpoint messages are retained.
    """

    def __init__(self, settings: Settings, pool: ClientPool) -> None:
        self._settings = settings
        self._pool = pool
        self._data_dir = settings.resolved_data_dir
        self._store_path = self._data_dir / "drive.sqlite3"
        self._history_dir = self._data_dir / "backups"
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._backup_lock = asyncio.Lock()
        self._protected_drive_id: str | None = None
        self._protected_revision = 0
        self._checkpoint_message_id: int | None = None
        self._last_success_at: str | None = None
        self._last_error: str | None = None
        self._recovery_error: str | None = None
        self._checked_binding: tuple[str, int] | None = None

    @property
    def recovery_required(self) -> bool:
        return self._recovery_error is not None

    @property
    def recovery_error(self) -> str | None:
        return self._recovery_error

    def _conflict(self, message: str) -> RecoveryConflict:
        self._recovery_error = message
        self._last_error = message
        return RecoveryConflict(message)

    def check_local_binding(self, store: DriveStore) -> None:
        """Check persisted channel affinity without requiring Telegram availability."""
        channel = self._settings.storage_channel
        if store.storage_channel_id not in {None, channel}:
            raise self._conflict(
                "this drive is bound to another storage channel; "
                "restore its configuration before continuing"
            )
        identity = (store.drive_id, channel)
        if self._checked_binding == identity:
            return
        with self._history() as connection:
            mismatch = connection.execute(
                "SELECT 1 FROM checkpoints WHERE channel_id!=? "
                "AND (drive_id=? OR drive_id IS NULL) LIMIT 1",
                (channel, store.drive_id),
            ).fetchone()
        if mismatch is not None:
            raise self._conflict(
                "checkpoint history belongs to another storage channel; "
                "preserve local state and restore its configuration"
            )
        self._checked_binding = identity

    def check_upload_allowed(self, store: DriveStore) -> None:
        """Gate appends locally; completed uploads require remote protection."""
        self.check_local_binding(store)
        if self._recovery_error:
            raise RecoveryConflict(self._recovery_error)

    async def _defer_publication(self, deadline: float) -> None:
        defer = getattr(self._pool, "defer_channel", None)
        if callable(defer):
            await defer(deadline)

    async def restore_publication_cooldown(self) -> None:
        """Restore provider waits before any startup writer can publish."""
        with self._history() as connection:
            rows = connection.execute(
                "SELECT retry_at FROM publication_state UNION ALL "
                "SELECT retry_at FROM checkpoints WHERE channel_id=?",
                (self._settings.storage_channel,),
            ).fetchall()
        now = time.time()
        deadlines = [
            row[0]
            for row in rows
            if type(row[0]) in (int, float) and math.isfinite(row[0]) and row[0] > now
        ]
        if deadlines:
            await self._defer_publication(max(deadlines))

    async def verify_lineage(self, store: DriveStore) -> None:
        """Freshly check channel and catalog lineage before destructive effects."""
        await self._ensure_revision(store, store.revision, verify=True, check_only=True)

    @property
    def store_path(self) -> Path:
        return self._store_path

    async def restore(self) -> DriveStore:
        """Open existing authoritative state; missing state requires explicit recovery.

        Startup never downloads a checkpoint, initializes an empty replacement,
        or overwrites an existing database, regardless of legacy init settings.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._store_path.exists():
            try:
                return DriveStore.load(self._store_path)
            except Exception as exc:
                raise BackupError(
                    "local metadata is invalid; preserve it and use explicit recovery"
                ) from exc
        raise BackupError(
            "local metadata is missing; explicitly recover from Telegram with "
            "'python -m tgdrive recover', or explicitly initialize a new drive"
        )

    @staticmethod
    def _decode_snapshot(content: bytes) -> object:
        if len(content) > MAX_STORE_BYTES:
            raise BackupError("metadata backup exceeds the safe size limit")

        def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise BackupError("metadata backup contains duplicate JSON keys")
                result[key] = value
            return result

        try:
            return json.loads(content.decode("utf-8"), object_pairs_hook=unique_pairs)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise BackupError(
                "metadata backup is not valid bounded UTF-8 JSON"
            ) from exc

    @staticmethod
    def _marker(message: Any, message_id: int) -> dict[str, Any] | None:
        text = getattr(message, "text", None) or getattr(message, "raw_text", "")
        match = _MARKER.fullmatch(text) if isinstance(text, str) else None
        document = getattr(message, "document", None) if message else None
        if match is None or getattr(document, "file_name", None) != "drive.json":
            return None
        drive_id, channel, revision, digest = match.groups()
        size = getattr(document, "file_size", None)
        if type(size) is not int or not 0 < size <= MAX_STORE_BYTES:
            raise BackupError("pinned checkpoint has an invalid document size")
        if not 0 < int(revision) < 2**63 or int(channel) == 0:
            raise BackupError("pinned checkpoint has invalid identity metadata")
        return dict(
            drive_id=drive_id,
            channel_id=int(channel),
            revision=int(revision),
            sha256=digest,
            message_id=message_id,
        )

    async def _remote_pin(self) -> tuple[dict[str, Any] | None, Any]:
        async with self._pool.lease() as client:
            message_id = await client.get_pinned_message_id(
                self._settings.storage_channel
            )
            if message_id is None:
                return None, None
            message = await client.get_messages(
                self._settings.storage_channel, message_ids=message_id
            )
        marker = self._marker(message, message_id)
        if (
            marker is not None
            and marker["channel_id"] != self._settings.storage_channel
        ):
            raise self._conflict(
                "pinned checkpoint belongs to a different storage channel"
            )
        return marker, message

    async def recover_latest(
        self, *, destination: str | os.PathLike[str] | None = None
    ) -> DriveStore:
        """Discover the last pin and restore only into an absent path."""
        target = Path(destination) if destination is not None else self._store_path
        if target.exists():
            raise FileExistsError(target)
        try:
            marker, message = await self._remote_pin()
            if marker is None:
                raise BackupError(
                    "the last pinned message is not a verified TGDrive checkpoint; "
                    "select a known checkpoint message ID explicitly"
                )
            return await self._restore_message(message, target, marker)
        except (BackupError, FileExistsError):
            raise
        except Exception as exc:
            raise BackupError(
                f"failed to discover recovery checkpoint: {type(exc).__name__}"
            ) from exc

    async def _restore_message(
        self, message: Any, target: Path, marker: dict[str, Any] | None
    ) -> DriveStore:
        document = getattr(message, "document", None) if message else None
        if document is None or getattr(document, "file_name", None) != "drive.json":
            raise BackupError("selected checkpoint must be a drive.json document")
        size = getattr(document, "file_size", None)
        if type(size) is not int or not 0 < size <= MAX_STORE_BYTES:
            raise BackupError("metadata backup has an invalid or excessive size")
        self._data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self._data_dir / f"restore-{secrets.token_urlsafe(16)}.json"
        try:
            async with self._pool.lease() as client:
                downloaded = await client.download_media(
                    message, file_name=str(temporary)
                )
            if downloaded is None or not temporary.is_file():
                raise BackupError("Telegram returned no metadata backup file")
            with temporary.open("rb") as handle:
                content = handle.read(MAX_STORE_BYTES + 1)
            if len(content) != size:
                raise BackupError("checkpoint document size does not match")
            payload = self._decode_snapshot(content)
            if marker is not None:
                if marker["channel_id"] != self._settings.storage_channel:
                    raise BackupError(
                        "checkpoint belongs to a different storage channel"
                    )
                if hashlib.sha256(content).hexdigest() != marker["sha256"]:
                    raise BackupError("checkpoint document checksum failed")
                if not isinstance(payload, dict) or (
                    payload.get("drive_id") != marker["drive_id"]
                    or type(payload.get("revision")) is not int
                    or payload["revision"] != marker["revision"]
                ):
                    raise BackupError("checkpoint identity does not match its document")
            if isinstance(payload, dict):
                binding = payload.get("storage_channel_id")
                if binding is not None and (
                    type(binding) is not int
                    or binding != self._settings.storage_channel
                ):
                    raise BackupError(
                        "checkpoint catalog is bound to another storage channel"
                    )
                if marker is not None and "storage_channel_id" not in payload:
                    payload["storage_channel_id"] = self._settings.storage_channel
                    payload["revision"] += 1
            return DriveStore._from_payload(
                payload, storage_path=target, persist=True, revoke_shares=True
            )
        finally:
            temporary.unlink(missing_ok=True)

    async def restore_from_telegram(
        self, message_id: int, *, destination: str | os.PathLike[str] | None = None
    ) -> DriveStore:
        """Explicit IDs retain legacy JSON compatibility and never overwrite."""
        if type(message_id) is not int or message_id <= 0:
            raise BackupError("restore message ID must be a positive integer")
        target = Path(destination) if destination is not None else self._store_path
        if target.exists():
            raise FileExistsError(target)
        try:
            async with self._pool.lease() as client:
                message = await client.get_messages(
                    self._settings.storage_channel, message_ids=message_id
                )
            return await self._restore_message(
                message, target, self._marker(message, message_id)
            )
        except (BackupError, FileExistsError):
            raise
        except Exception as exc:
            raise BackupError(
                f"failed to restore selected checkpoint: {type(exc).__name__}"
            ) from exc

    def restore_from_file(
        self,
        source: str | os.PathLike[str],
        *,
        destination: str | os.PathLike[str],
    ) -> DriveStore:
        """Validate an offline JSON checkpoint and create a new SQLite database."""
        target = Path(destination)
        if target.exists():
            raise FileExistsError(target)
        with Path(source).open("rb") as handle:
            payload = self._decode_snapshot(handle.read(MAX_STORE_BYTES + 1))
        return DriveStore._from_payload(
            payload, storage_path=target, persist=True, revoke_shares=True
        )

    @contextmanager
    def _history(self) -> Iterator[sqlite3.Connection]:
        """Close history connections on successful reads and exceptional exits."""
        self._history_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._history_dir / "index.sqlite3"
        connection = sqlite3.connect(path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL, sha256 TEXT NOT NULL,
                random_id INTEGER NOT NULL UNIQUE, channel_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, message_id INTEGER,
                status TEXT NOT NULL CHECK(status IN ('pending','completed'))
            )""")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(checkpoints)")
            }
            for name, kind in {
                "drive_id": "TEXT",
                "sender": "TEXT",
                "published_at": "TEXT",
                "caption": "TEXT",
                "retry_at": "REAL",
            }.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE checkpoints ADD COLUMN {name} {kind}"
                    )
            connection.execute("""CREATE TABLE IF NOT EXISTS publication_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), retry_at REAL
            )""")
            connection.commit()
            path.chmod(0o600)
            yield connection
        finally:
            connection.close()

    def history(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("history limit must be a positive integer")
        with self._history() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM checkpoints ORDER BY rowid DESC LIMIT ?",
                    (limit if limit is not None else -1,),
                )
            ]

    def prune_local_history(self, *, now: datetime | None = None) -> int:
        """Keep 96 newest confirmed files plus one per UTC day for 30 days.

        Pending checkpoints and unconfirmed records are never removed. The index
        and remote messages are retained so an older checkpoint remains available
        through explicit Telegram recovery even when its local JSON is pruned.
        """
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        first_day = instant.date() - timedelta(days=29)
        with self._history() as connection:
            confirmed = connection.execute(
                "SELECT id,created_at FROM checkpoints WHERE status='completed' "
                "AND message_id>0 ORDER BY created_at DESC,rowid DESC"
            ).fetchall()
        days = set()
        removed = 0
        for index, checkpoint in enumerate(confirmed):
            identity = checkpoint["id"]
            if not isinstance(identity, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{22}", identity
            ):
                continue
            try:
                created = datetime.fromisoformat(
                    checkpoint["created_at"].replace("Z", "+00:00")
                )
                if created.tzinfo is None:
                    continue
                created = created.astimezone(UTC)
            except (ValueError, TypeError, AttributeError):
                continue
            if created > instant:
                continue
            day = created.date()
            daily = first_day <= day <= instant.date() and day not in days
            days.add(day)
            if index < 96 or daily:
                continue
            path = self._history_dir / f"{identity}.json"
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed += 1
        if removed:
            DriveStore._fsync_directory(self._history_dir)
        return removed

    @staticmethod
    def _caption(checkpoint: dict[str, Any]) -> str:
        return (
            "TGDrive checkpoint v1\nDrive: "
            + checkpoint["drive_id"]
            + "\nChannel: "
            + str(checkpoint["channel_id"])
            + "\nRevision: "
            + str(checkpoint["revision"])
            + "\nSHA256: "
            + checkpoint["sha256"]
        )

    def _prepare_checkpoint(
        self, store: DriveStore, snapshot: tuple[int, bytes] | None = None
    ) -> dict[str, Any]:
        with self._history() as connection:
            pending = connection.execute(
                "SELECT * FROM checkpoints WHERE status='pending' AND drive_id=? "
                "ORDER BY rowid LIMIT 1",
                (store.drive_id,),
            ).fetchone()
            if pending is not None:
                return dict(pending)
            revision, content = snapshot or store.backup_snapshot()
            checkpoint = dict(
                id=secrets.token_urlsafe(16),
                revision=revision,
                sha256=hashlib.sha256(content).hexdigest(),
                random_id=secrets.randbits(63),
                channel_id=self._settings.storage_channel,
                drive_id=store.drive_id,
                created_at=datetime.now(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            )
            checkpoint["caption"] = self._caption(checkpoint)
            path = self._history_dir / f"{checkpoint['id']}.json"
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            DriveStore._fsync_directory(self._history_dir)
            connection.execute(
                "INSERT INTO checkpoints(id,revision,sha256,random_id,channel_id,"
                "created_at,message_id,status,drive_id,caption) "
                "VALUES(?,?,?,?,?,?,NULL,'pending',?,?)",
                tuple(
                    checkpoint[key]
                    for key in (
                        "id",
                        "revision",
                        "sha256",
                        "random_id",
                        "channel_id",
                        "created_at",
                        "drive_id",
                        "caption",
                    )
                ),
            )
            connection.commit()
            return dict(
                connection.execute(
                    "SELECT * FROM checkpoints WHERE id=?", (checkpoint["id"],)
                ).fetchone()
            )

    def protection_status(self, store: DriveStore) -> dict[str, Any]:
        revision = store.revision
        protected = (
            self._protected_revision
            if self._protected_drive_id == store.drive_id
            else 0
        )
        result = dict(
            state="error"
            if self._last_error
            else "protected"
            if protected >= revision
            else "pending",
            metadata_revision=revision,
            protected_revision=protected,
        )
        if self._checkpoint_message_id is not None:
            result["checkpoint_message_id"] = self._checkpoint_message_id
        if self._last_success_at is not None:
            result["last_success_at"] = self._last_success_at
        if self._last_error:
            result["error"] = self._last_error
        if self.recovery_required:
            result["recovery_required"] = True
            result["code"] = RecoveryConflict.code
        return result

    def _record_protected(self, store: DriveStore, marker: dict[str, Any]) -> None:
        self._protected_drive_id = store.drive_id
        self._protected_revision = marker["revision"]
        self._checkpoint_message_id = marker["message_id"]
        self._last_success_at = (
            datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        store.mark_backed_up(marker["revision"])
        with self._history() as connection:
            connection.execute(
                "UPDATE checkpoints SET status='completed',message_id=?,"
                "published_at=?,retry_at=NULL "
                "WHERE drive_id=? AND revision=? AND sha256=?",
                (
                    marker["message_id"],
                    self._last_success_at,
                    store.drive_id,
                    marker["revision"],
                    marker["sha256"],
                ),
            )
            connection.commit()
        self._last_error = None
        self._recovery_error = None

    async def ensure_revision(self, store: DriveStore, required_revision: int) -> int:
        """Wait until the requested catalog is discoverable on Telegram."""
        return await self._ensure_revision(store, required_revision)

    async def _ensure_revision(
        self,
        store: DriveStore,
        required_revision: int,
        *,
        verify: bool = False,
        force: bool = False,
        check_only: bool = False,
    ) -> int:
        if (
            type(required_revision) is not int
            or not 1 <= required_revision <= store.revision
        ):
            raise BackupError("requested protection revision is invalid")
        async with self._backup_lock:
            checkpoint = None
            try:
                self.check_local_binding(store)
                self.prune_local_history()
                if (
                    not verify
                    and not force
                    and not self._last_error
                    and self._protected_drive_id == store.drive_id
                    and self._protected_revision >= required_revision
                ):
                    return self._protected_revision
                with self._history() as connection:
                    row = connection.execute(
                        "SELECT * FROM checkpoints WHERE drive_id=? "
                        "AND status='pending' ORDER BY rowid LIMIT 1",
                        (store.drive_id,),
                    ).fetchone()
                    cooldown = connection.execute(
                        "SELECT retry_at FROM publication_state WHERE singleton=1"
                    ).fetchone()
                if row is not None:
                    checkpoint = dict(row)
                retry_at = max(
                    checkpoint["retry_at"] or 0 if checkpoint is not None else 0,
                    cooldown[0] or 0 if cooldown is not None else 0,
                )
                remaining = retry_at - time.time()
                if remaining > 0:
                    await self._defer_publication(retry_at)
                    raise BackupError(
                        "Telegram recovery publication is waiting before retry",
                        retry_after=remaining,
                    )
                # Concurrent commits can share one checkpoint during this brief window.
                await asyncio.sleep(0.05)
                while True:
                    remote, _ = await self._remote_pin()
                    snapshot = store.backup_snapshot()
                    digest = hashlib.sha256(snapshot[1]).hexdigest()
                    if remote is not None:
                        if remote["drive_id"] != store.drive_id:
                            raise self._conflict(
                                "this channel has a checkpoint for another drive; "
                                "recover it or select a new channel"
                            )
                        if remote["revision"] > snapshot[0]:
                            raise self._conflict(
                                "Telegram has a newer catalog; preserve local state "
                                "and explicitly recover it"
                            )
                        if (
                            remote["revision"] == snapshot[0]
                            and remote["sha256"] != digest
                        ):
                            raise self._conflict(
                                "local and Telegram catalogs conflict "
                                "at the same revision"
                            )
                    if self._recovery_error and not (
                        remote is not None
                        and remote["revision"] == snapshot[0]
                        and remote["sha256"] == digest
                    ):
                        # Failed workers can advance a stale local revision.
                        # That must never turn a detected fork into permission
                        # to overwrite the newer remote recovery catalog.
                        raise RecoveryConflict(self._recovery_error)
                    if check_only:
                        if self._recovery_error:
                            raise RecoveryConflict(self._recovery_error)
                        return self._protected_revision
                    was_unbound = store.storage_channel_id is None
                    store.bind_storage_channel(self._settings.storage_channel)
                    if was_unbound:
                        required_revision = max(required_revision, store.revision)
                        snapshot = store.backup_snapshot()
                    if (
                        remote is not None
                        and remote["revision"] == snapshot[0]
                        and not force
                    ):
                        self._record_protected(store, remote)
                        return self._protected_revision
                    checkpoint = self._prepare_checkpoint(store, snapshot)
                    if checkpoint["revision"] > store.revision:
                        raise BackupError(
                            "pending checkpoint is newer than this restored database"
                        )
                    if (
                        remote is not None
                        and checkpoint["revision"] < remote["revision"]
                    ):
                        raise BackupError(
                            "pending checkpoint would replace a newer recovery pin"
                        )
                    if checkpoint["channel_id"] != self._settings.storage_channel:
                        raise self._conflict(
                            "pending checkpoint belongs to another storage channel"
                        )
                    remaining = (checkpoint["retry_at"] or 0) - time.time()
                    if remaining > 0:
                        raise BackupError(
                            "Telegram recovery publication is waiting before retry",
                            retry_after=remaining,
                        )
                    async with self._pool.lease(
                        client_name=checkpoint["sender"]
                    ) as client:
                        if checkpoint["message_id"] is None:
                            source = self._history_dir / f"{checkpoint['id']}.json"
                            if (
                                not source.is_file()
                                or source.stat().st_size > MAX_STORE_BYTES
                                or hashlib.sha256(source.read_bytes()).hexdigest()
                                != checkpoint["sha256"]
                            ):
                                raise BackupError(
                                    "pending checkpoint source is missing "
                                    "or its checksum failed"
                                )
                            if checkpoint["sender"] is None:
                                checkpoint["sender"] = client.name
                                with self._history() as connection:
                                    connection.execute(
                                        "UPDATE checkpoints SET sender=? WHERE id=?",
                                        (client.name, checkpoint["id"]),
                                    )
                                    connection.commit()
                            message = await client.send_document(
                                checkpoint["channel_id"],
                                str(source),
                                file_name="drive.json",
                                random_id=checkpoint["random_id"],
                                caption=checkpoint["caption"],
                            )
                            message_id = getattr(message, "id", None)
                            if type(message_id) is not int or message_id <= 0:
                                raise BackupError(
                                    "Telegram did not confirm a checkpoint message"
                                )
                            checkpoint["message_id"] = message_id
                            with self._history() as connection:
                                connection.execute(
                                    "UPDATE checkpoints SET message_id=? WHERE id=?",
                                    (message_id, checkpoint["id"]),
                                )
                                connection.commit()
                        await client.pin_message(
                            checkpoint["channel_id"],
                            checkpoint["message_id"],
                            silent=True,
                        )
                        pinned = await client.get_pinned_message_id(
                            checkpoint["channel_id"]
                        )
                        if pinned != checkpoint["message_id"]:
                            raise BackupError(
                                "Telegram did not confirm the recovery pin"
                            )
                        confirmed = await client.get_messages(
                            checkpoint["channel_id"], message_ids=pinned
                        )
                        marker = self._marker(confirmed, pinned)
                        if marker is None or any(
                            marker[key] != checkpoint[key]
                            for key in ("drive_id", "channel_id", "revision", "sha256")
                        ):
                            raise BackupError(
                                "published checkpoint identity could not be verified"
                            )
                    self._record_protected(store, marker)
                    self.prune_local_history()
                    if self._protected_revision >= required_revision:
                        return self._protected_revision
                    force = False
            except Exception as exc:
                seconds = getattr(exc, "seconds", None)
                retry = (
                    float(seconds)
                    if isinstance(seconds, (int, float))
                    and math.isfinite(seconds)
                    and seconds > 0
                    else None
                )
                if retry is not None:
                    with self._history() as connection:
                        deadline = time.time() + retry
                        connection.execute(
                            "INSERT INTO publication_state VALUES(1,?) "
                            "ON CONFLICT(singleton) DO UPDATE SET retry_at="
                            "max(coalesce(retry_at,0),excluded.retry_at)",
                            (deadline,),
                        )
                        if checkpoint is not None:
                            connection.execute(
                                "UPDATE checkpoints SET retry_at=? WHERE id=?",
                                (deadline, checkpoint["id"]),
                            )
                        connection.commit()
                    await self._defer_publication(deadline)
                error = (
                    exc
                    if isinstance(exc, BackupError)
                    else BackupError(
                        f"recovery checkpoint publication failed: {type(exc).__name__}",
                        retry_after=retry,
                    )
                )
                self._last_error = str(error)
                raise error from (None if error is exc else exc)

    async def backup(self, store: DriveStore, *, force: bool = False) -> bool:
        """Publish a discoverable immutable checkpoint, or verify current protection."""
        before = (
            self._protected_drive_id,
            self._protected_revision,
            self._checkpoint_message_id,
        )
        await self._ensure_revision(store, store.revision, verify=True, force=force)
        return force or before != (
            self._protected_drive_id,
            self._protected_revision,
            self._checkpoint_message_id,
        )

    def start(self, store: DriveStore) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop.clear()
        self._task = asyncio.create_task(
            self._backup_loop(store), name="metadata-backup"
        )
        return self._task

    async def _backup_loop(self, store: DriveStore) -> None:
        last_verify = 0.0
        next_attempt = 0.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except TimeoutError:
                if time.monotonic() < next_attempt:
                    continue
                if (
                    not self._last_error
                    and not store.dirty
                    and self._protected_drive_id == store.drive_id
                    and self._protected_revision >= store.revision
                    and time.monotonic() - last_verify
                    < self._settings.database_backup_time
                ):
                    continue
                try:
                    await self.backup(store)
                    last_verify = time.monotonic()
                except BackupError as exc:
                    next_attempt = time.monotonic() + max(exc.retry_after or 5, 1)
                    logger.exception("Metadata recovery checkpoint publication failed")

    async def stop(self, store: DriveStore) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self.backup(store)
