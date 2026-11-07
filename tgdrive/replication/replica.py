"""Optional independent channel copies with crash-resumable, explicit recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import secrets
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing, suppress
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from bson import json_util
from pydantic import SecretStr

from tgdrive.recovery.recovery_file import (
    MAX_STAGED_RECOVERY_BYTES,
    LegacyRecoveryFile,
)
from tgdrive.replication.replica_state import (
    ReplicaJournal,
    atomic_private,
    decode_bundle,
    encode_bundle,
    files_in,
    parts_in,
    read_private,
    remap_catalog,
    validate_catalog,
)
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.transfers.tasks import _journalled

_worker = _journalled(asyncio.to_thread)

HEALTH_CHECK_INTERVAL_SECONDS = 60 * 60
BACKUP_BATCH_SECONDS = 60
logger = logging.getLogger(__name__)


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


class _CopyMapping:
    """Disposable copy lookup; the MongoDB journal remains authoritative."""

    def __init__(self, directory, reserve):
        import os

        descriptor, path = tempfile.mkstemp(prefix=".replica-mapping-", dir=directory)
        os.close(descriptor)
        self.path, self.reserve = Path(path), reserve
        try:
            with closing(sqlite3.connect(self.path)) as db:
                db.execute(
                    "CREATE TABLE copies(source INTEGER PRIMARY KEY,"
                    "destination INTEGER UNIQUE)"
                )
                db.commit()
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise

    def put(self, source, destination):
        if shutil.disk_usage(self.path.parent).free < self.reserve + 65536:
            raise SettingsProblem(
                "Free local disk space before retrying recovery.", status=507
            )
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "INSERT INTO copies VALUES(?,?) ON CONFLICT(source) "
                "DO UPDATE SET destination=excluded.destination",
                (source, destination),
            )
            db.commit()

    def lookup(self, source):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT destination FROM copies WHERE source=?", (int(source),)
            ).fetchone()
        if row is None:
            raise KeyError(source)
        return row[0]

    def pairs(self, *, reverse=False):
        with closing(sqlite3.connect(self.path)) as db:
            for source, destination in db.execute(
                "SELECT source,destination FROM copies ORDER BY source"
            ):
                yield (destination, source) if reverse else (source, destination)

    def digest(self):
        result = hashlib.sha256()
        for pair in self.pairs():
            result.update(json.dumps(pair, separators=(",", ":")).encode())
        return result.hexdigest()

    def close(self):
        self.path.unlink(missing_ok=True)


class ChannelReplica:
    def __init__(self, settings, runtime, *, transport_factory=None):
        from tgdrive.replication.replica_transport import (
            ReplicaRequestPacer,
            ReplicaTransport,
        )

        self.settings, self.runtime = settings, runtime
        self._request_pacer = ReplicaRequestPacer()
        self.factory = transport_factory or partial(
            ReplicaTransport, pacer=self._request_pacer
        )
        self.directory = settings.resolved_data_dir
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.journal = ReplicaJournal(self.directory, getattr(runtime, "mongo", None))
        self._task = None
        self._wake = asyncio.Event()
        self._operation = asyncio.Lock()
        self.control = asyncio.Lock()
        self._closed = False

    @property
    def blocked(self):
        try:
            self._check_saved_state()
            state = self.journal.get_many({"recovery": None, "activation": None})
        except Exception:
            if self._resource_error():
                return True
            raise
        return bool(state["recovery"] or state["activation"])

    def _resource_error(self):
        return getattr(self.journal, "resource_error", lambda: None)()

    def _check_resources(self):
        problem = self._resource_error()
        if problem:
            raise SettingsProblem(problem["detail"], code=problem["code"], status=409)

    def _check_saved_state(self):
        self._check_resources()
        with self.journal.lock:
            for key in (
                "config",
                "status",
                "job",
                "candidate",
                "recovery",
                "activation",
                "restore_point",
            ):
                self.journal.get(key)

    def status(self):
        problem = self._resource_error()
        saved = {
            "config": {},
            "status": {},
            "report": [],
            "restore_point": None,
            "candidate": None,
            "job": None,
        }
        if not problem:
            try:
                saved = self.journal.get_many(saved)
            except Exception:
                problem = self._resource_error()
                if not problem:
                    raise
        config, state = saved["config"], saved["status"]
        store = getattr(self.runtime, "store", None)
        revision = store.revision if store else 0
        result = {
            "enabled": bool(config.get("enabled")),
            "configured": bool(config),
            "recovery_only": bool(getattr(self.runtime, "recovery_only", False)),
            "phase": "idle" if config else "disabled",
            "source_channel": self.settings.storage_channel,
            "backup_channel": config.get("channel"),
            "revision": revision,
            "protected_revision": 0,
            "last_success_at": None,
            "current_file": "",
            "total_files": 0,
            "done_files": 0,
            "total_parts": 0,
            "done_parts": 0,
            "total_bytes": 0,
            "done_bytes": 0,
            "error": None,
            **state,
            "report": saved["report"],
            "restore_point": saved["restore_point"],
        }
        result["pending_revision"] = max(0, revision - result["protected_revision"])
        try:
            result["backup_file_available"] = self.backup_file() is not None
        except Exception:
            if not self._resource_error():
                raise
            result["backup_file_available"] = False
            problem = self._resource_error()
        result["file_restore_allowed"] = not self.settings.demo_mode
        if problem:
            result.update(
                phase="error",
                error={
                    "code": problem["code"],
                    "detail": problem["detail"],
                    "action": (
                        "Stop and preserve the old deployment. Follow the separate "
                        "recovery deployment procedure only with an intact compatible "
                        "recovery file; without it, preserve the pending record for "
                        "review."
                    ),
                    "retry_at": 0,
                },
                file_restore_allowed=False,
            )
            return result
        candidate = saved["candidate"]
        if candidate:
            catalog = candidate["catalog"]
            files = list(files_in(catalog)) if "staged" not in candidate else None
            result["recovery_candidate"] = {
                "revision": catalog["revision"],
                "channel": catalog["storage_channel_id"],
                "files": len(files) if files is not None else candidate["file_count"],
                "bytes": sum(f["size"] for f in files)
                if files is not None
                else candidate["total_bytes"],
                "complete": bool(candidate.get("complete")),
                "has_database": "database" in candidate or "staged" in candidate,
                "source_is_backup": bool(candidate.get("source_is_backup")),
                "discovered": bool(candidate.get("discovered")),
                "created_at": (candidate.get("inspection") or {}).get("created_at"),
            }
        return result

    def update(self, **values):
        with self.journal.lock:
            self.journal.set("status", self.journal.get("status", {}) | values)

    def report(self, level, message):
        with self.journal.lock:
            rows = self.journal.get("report", [])
            self.journal.set(
                "report",
                [*rows, {"at": now(), "level": level, "message": message}][-100:],
            )

    def key(self):
        """Backup files use a key derived from ENCRYPTION_KEY; nothing to save."""
        from tgdrive.core import encryption

        return encryption.backup_key()

    def _backup_selection(self):
        """Select one retained archive and its own integrity record."""
        import re

        choices = []
        saved = self.journal.get("backup_file")
        if saved and re.fullmatch(r"[a-f0-9]{64}", saved.get("digest", "")):
            path = self.directory / "replica-bundles" / (saved["digest"] + ".tgdb")
            if path.exists() or path.is_symlink():
                choices.append(
                    (
                        saved.get("created_at", "1970-01-01T00:00:00+00:00"),
                        path,
                        saved.get("sha256"),
                    )
                )
        service = getattr(self.runtime, "backup", None)
        archives = getattr(service, "local_archives", None)
        latest = (
            archives()
            if callable(archives)
            else getattr(service, "latest_history", lambda: [])()
        )
        for row in latest:
            if row.get("status") not in {"confirmed", "unverified"}:
                continue
            identity = row.get("id", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
                path = (
                    self.settings.resolved_cache_dir
                    / "mongo-backups"
                    / (identity + ".tgdb")
                )
                if path.exists() or path.is_symlink():
                    choice = (row["created_at"], path, row.get("sha256"))
                    if not choices or datetime.fromisoformat(
                        choice[0]
                    ) > datetime.fromisoformat(choices[0][0]):
                        choices[:] = [choice]
        return (
            max(choices, key=lambda choice: datetime.fromisoformat(choice[0]))[1:]
            if choices
            else None
        )

    def backup_file(self):
        """Offer retained confirmed or unverified archives without downloading."""
        selected = self._backup_selection()
        return selected[0] if selected else None

    def recovery_file(self):
        path, source = self.open_recovery_file()
        source.close()
        return path

    def open_recovery_file(self):
        """Return the verified open file; its caller owns closing that descriptor."""
        selected = self._backup_selection()
        if selected is None:
            raise SettingsProblem(
                "Create and confirm a backup before downloading its recovery file.",
                status=404,
            )
        path, expected = selected
        import os
        import stat

        descriptor = None
        source = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise SettingsProblem(
                    "The saved recovery file has unsafe permissions or file type.",
                    status=409,
                )
            source = os.fdopen(descriptor, "rb")
            descriptor = None
            digest = hashlib.file_digest(source, "sha256").hexdigest()
            if not expected or digest != expected:
                raise SettingsProblem(
                    "The saved recovery file changed. "
                    "Create a fresh confirmed backup before downloading.",
                    status=409,
                )
            source.seek(0)
            return path, source
        except BaseException as error:
            if source is not None:
                source.close()
            if isinstance(error, FileNotFoundError):
                raise SettingsProblem(
                    "The saved recovery file is no longer available. "
                    "Refresh and retry.",
                    status=404,
                ) from None
            if isinstance(error, OSError):
                raise SettingsProblem(
                    "The saved recovery file cannot be opened safely. "
                    "Check its permissions and retry.",
                    status=409,
                ) from None
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    async def configure(self, channel, token, independent):
        await _worker(self._check_saved_state)
        if not independent or channel == self.settings.storage_channel or channel >= 0:
            raise SettingsProblem(
                "Use two different private channels and confirm independent "
                "owner accounts."
            )
        if await _worker(lambda: self.blocked):
            raise SettingsProblem(
                "Pause the current operation before changing backup settings.",
                status=409,
            )
        transport = self.factory(self.settings, token)
        try:
            await transport.start()
            await transport.inspect(self.settings.storage_channel)
            target = await transport.inspect(channel)
            pinned = await transport.pinned_message(channel)
            point = await _worker(self.journal.get, "restore_point", {})
            if pinned is not None and not (
                point.get("backup_channel") == channel
                and point.get("backup_message_id") == pinned
            ):
                raise SettingsProblem(
                    "The destination already has a pinned message. Use a new dedicated "
                    "private channel, or inspect its existing recovery point instead "
                    "of overwriting it.",
                    status=409,
                )
            config = {
                "channel": channel,
                "token": token,
                "enabled": True,
                "title": target["title"],
            }
            await _worker(self.journal.set, "config", config)
            await _worker(self.journal.set, "job", None)
            await _worker(
                self.update,
                phase="idle",
                error=None,
                protected_revision=0,
                last_success_at=None,
            )
            await _worker(
                self.report,
                "info",
                "Private channels and backup bot verified. "
                "Initial synchronization scheduled.",
            )
            self.start()
            self._wake.set()
        finally:
            await transport.close()
        return await _worker(self.status)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._loop(), name="independent-channel-backup"
            )

    async def close(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await _worker(self.journal.close)

    async def action(self, action):
        await _worker(self._check_saved_state)
        config = await _worker(self.journal.get, "config", {})
        if action in {"pause", "disable"}:
            if await _worker(self.journal.get, "activation"):
                raise SettingsProblem(
                    "Restart to apply the confirmed recovery first.", status=409
                )
            await _worker(
                self.update, phase="paused" if action == "pause" else "disabled"
            )
            if action == "disable":
                candidate = await _worker(self.journal.get, "candidate")
                config["enabled"] = False
                await _worker(self.journal.set, "config", config)
                await _worker(self.journal.set, "recovery", None)
                await _worker(self.journal.set, "candidate", None)
                if not self._operation.locked():
                    await _worker(self._discard_staged, candidate)
            await _worker(
                self.report,
                "info",
                "Operation paused. Confirmed source and backup messages are retained.",
            )
        elif action in {"resume", "retry", "sync"}:
            error = (await _worker(self.journal.get, "status", {})).get("error")
            if error and error.get("retry_at", 0) > time.time():
                await _worker(self.update, phase="waiting")
            else:
                await _worker(self.update, phase="idle", error=None)
                if action == "sync" or error:
                    await _worker(self.update, checked_at=0)
            if config:
                config["enabled"] = True
                await _worker(self.journal.set, "config", config)
            self.start()
            self._wake.set()
        else:
            raise SettingsProblem("Unknown backup action")
        return await _worker(self.status)

    async def _loop(self):
        try:
            await _worker(self._check_saved_state)
            while not self._closed:
                if self._resource_error():
                    return
                saved = await _worker(
                    self.journal.get_many,
                    {"status": {}, "config": {}, "recovery": None},
                )
                state, config, recovery = (
                    saved["status"],
                    saved["config"],
                    saved["recovery"],
                )
                retry_at = (state.get("error") or {}).get("retry_at", 0)
                runnable = state.get("phase") not in {
                    "paused",
                    "disabled",
                    "error",
                    "ready_to_activate",
                    "restart_required",
                }
                if (
                    runnable
                    and retry_at <= time.time()
                    and (recovery or config.get("enabled"))
                ):
                    async with self._operation:
                        # A control request may arrive while another operation owns
                        # the lock. Do not execute the earlier eligibility snapshot.
                        saved = await _worker(
                            self.journal.get_many,
                            {"status": {}, "config": {}, "recovery": None, "job": None},
                        )
                        state, config, recovery = (
                            saved["status"],
                            saved["config"],
                            saved["recovery"],
                        )
                        if (
                            state.get("phase")
                            in {
                                "paused",
                                "disabled",
                                "error",
                                "ready_to_activate",
                                "restart_required",
                            }
                            or (state.get("error") or {}).get("retry_at", 0)
                            > time.time()
                            or not (recovery or config.get("enabled"))
                        ):
                            continue
                        try:
                            if recovery:
                                await self._recover_copy(recovery)
                            elif getattr(self.runtime, "store", None) is not None:
                                # Coalesce busy uploads without starving protection.
                                changed = (
                                    state.get("protected_revision")
                                    != self.runtime.store.revision
                                )
                                if (
                                    saved["job"] is not None
                                    or state.get("error")
                                    or not changed
                                    or time.time()
                                    >= state.get("checked_at", 0) + BACKUP_BATCH_SECONDS
                                ):
                                    await self._sync(config)
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            await _worker(self._record_failure, error)
                            if self._resource_error():
                                return
                self._wake.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=10)
        except Exception:
            if not self._resource_error():
                raise

    def _record_failure(self, error):
        if self._resource_error():
            return error
        from pymongo.errors import ConnectionFailure, PyMongoError

        from tgdrive.replication.replica_transport import ReplicaTransportError
        from tgdrive.storage.mongo import DatabaseUnavailable

        failure_type = type(error).__name__
        database_error = isinstance(error, (PyMongoError, DatabaseUnavailable))
        if database_error:
            # Ownership/fencing failures must remain stopped until explicitly repaired.
            retryable = isinstance(error, ConnectionFailure) or (
                isinstance(error, PyMongoError) and error.timeout is True
            )
            error = ReplicaTransportError(
                "MongoDB could not confirm the backup settings or snapshot."
                if retryable
                else "MongoDB access or ownership could not be confirmed for backup.",
                "REPLICA_MONGODB_UNAVAILABLE"
                if retryable
                else "REPLICA_MONGODB_FAILED",
                retryable=retryable,
            )
        retryable = bool(
            getattr(error, "retryable", False) or getattr(error, "retry_after", None)
        )
        delay = max(5, float(getattr(error, "retry_after", None) or 30))
        safe_error = isinstance(
            error, (SettingsProblem, ReplicaTransportError, ValueError)
        )
        code = (
            getattr(error, "code", "REPLICA_FAILED") if safe_error else "REPLICA_FAILED"
        )
        detail = (
            str(error) if safe_error else "Backup operation could not be confirmed."
        )
        if (
            code == "REPLICA_FLOOD_WAIT"
            and not self.journal.get("recovery")
            and self.journal.get("job") is None
            and self.journal.get("restore_point")
        ):
            detail = "Routine backup health check delayed. " + detail
        operation = getattr(error, "operation", None) if safe_error else None
        if operation:
            detail += f" Operation: {operation}."
        if retryable:
            detail += f" Retry in {math.ceil(delay)} seconds."
        if database_error:
            action = (
                "Leave TGDrive running. Backup will retry after the displayed deadline."
                if retryable
                else "Check MongoDB connectivity, ownership and permissions. "
                "Restore access and restart TGDrive before retrying."
            ) + " The last confirmed restore point is retained."
        elif code == "REPLICA_FLOOD_WAIT":
            action = (
                "Leave TGDrive running. Backup will retry after the displayed "
                "deadline; channel permissions do not need changing for this cooldown."
            )
        elif code == "ACTIVATION_RESOURCE_LIMIT":
            action = (
                "Stop and preserve the old deployment. Follow the separate recovery "
                "deployment procedure only with an intact compatible recovery file; "
                "without it, preserve the pending record for review."
            )
        elif code == "LEGACY_BACKUP_REQUIRES_UPGRADE":
            action = (
                "Preserve the original archive and key. Restore with tagged v2.2 "
                "into isolated MongoDB, then upgrade that restored database."
            )
        elif code == "REPLICA_FAILED":
            action = (
                "Preserve the backup journal and recovery keys. Review the server "
                "logs and retry after correcting the problem. "
                "The last confirmed restore point is retained."
            )
        else:
            action = (
                "Check channel access, bot permissions and the source file. "
                "Retry after correcting the problem. "
                "The last confirmed restore point is retained."
            )
        # Log the exception class, never arbitrary provider text or credentials.
        logger.warning("Independent backup failed: type=%s code=%s", failure_type, code)
        phase = self.journal.get("status", {}).get("phase")
        self.update(
            phase=(
                phase
                if phase in {"paused", "disabled"}
                else "waiting"
                if retryable
                else "error"
            ),
            error={
                "code": code,
                "detail": detail[:400],
                "action": action,
                "retry_at": time.time() + delay if retryable else 0,
            },
        )
        self.report("error", detail[:400])
        return error

    def _credentials(self):
        from tgdrive.replication.replica_settings import collect_credentials

        return collect_credentials(self.settings, self.runtime)

    def _capture(self):
        from tgdrive.recovery.snapshot_stream import write_recovery_snapshot
        from tgdrive.recovery.staged_snapshot import validate_snapshot

        path = self.directory / (".replica-capture-" + secrets.token_hex(12))
        try:
            write_recovery_snapshot(
                self.runtime.mongo,
                path,
                bytes.fromhex(self.key()),
                self.settings,
                validate_snapshot=partial(
                    validate_snapshot, min_free_bytes=self.settings.min_free_bytes
                ),
                max_records=None,
                max_database_bytes=None,
                max_expanded_bytes=None,
                max_recovery_bytes=MAX_STAGED_RECOVERY_BYTES,
            )
            return self._read_bundle(path, self.key())
        finally:
            path.unlink(missing_ok=True)

    def _read_bundle(self, path, key):
        from tgdrive.recovery.recovery_file import LEGACY_MAGICS, MAGIC
        from tgdrive.recovery.snapshot_stream import decrypt_snapshot
        from tgdrive.recovery.staged_snapshot import stage_snapshot
        from tgdrive.replication.replica_activation import (
            _staged_catalog,
            check_recovery_key,
        )

        with Path(path).open("rb") as source:
            prefix = source.read(max(map(len, (MAGIC, *LEGACY_MAGICS))))
        if prefix.startswith(LEGACY_MAGICS):
            raise LegacyRecoveryFile
        modern = prefix.startswith(MAGIC)
        if not modern:
            return decode_bundle(read_private(path), key)
        directory = self.directory / "recovery-staging"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = secrets.token_hex(16)
        plaintext, index = (
            directory / (identity + ".json"),
            directory / (identity + ".sqlite"),
        )
        try:
            decrypt_snapshot(
                path,
                plaintext,
                key,
                self.settings,
                max_recovery_bytes=MAX_STAGED_RECOVERY_BYTES,
                max_expanded_bytes=None,
            )
            staged = stage_snapshot(
                plaintext, index, min_free_bytes=self.settings.min_free_bytes
            )
            with closing(staged.records("settings")) as rows:
                check_recovery_key(self.settings, {"settings": rows})
            result = {
                "staged": {
                    "path": str(index.relative_to(self.directory)),
                    "sha256": staged.digest,
                },
                "catalog": _staged_catalog(
                    staged, staged.catalog["storage_channel_id"]
                ),
                "replica_catalog": None,
                "credentials": staged.header["credentials"],
                "created_at": staged.header["created_at"],
                "file_count": staged.file_count,
                "total_bytes": staged.total_bytes,
            }
            if staged.header["replica"]:
                result["replica_catalog"] = _staged_catalog(
                    staged, staged.header["replica"]["channel"]
                )
            if staged.header.get("recovery_token"):
                result["recovery_token"] = staged.header["recovery_token"]
            return result
        except LegacyRecoveryFile, SettingsProblem, OSError:
            index.unlink(missing_ok=True)
            raise
        except Exception:
            index.unlink(missing_ok=True)
            raise ValueError(
                "Recovery key or file validation failed; nothing changed"
            ) from None
        finally:
            plaintext.unlink(missing_ok=True)

    def _open_staged(self, value):
        from tgdrive.replication.replica_activation import _staged_source

        return _staged_source(self.settings, value["staged"])

    def _discard_staged(self, value):
        if not value or "staged" not in value:
            return
        references = self.journal.get_many(
            {"job": None, "candidate": None, "activation": None}
        )
        if references.pop("activation"):
            return
        if any(
            row and row.get("staged") == value["staged"] for row in references.values()
        ):
            return
        from tgdrive.recovery.snapshot_stream import _sync_directory

        try:
            staged = self._open_staged(value)
        except FileNotFoundError:
            return
        staged.path.unlink()
        _sync_directory(staged.path.parent)

    def _save_candidate(self, candidate, *, completed_recovery=None):
        with self.journal.lock:
            state = self.journal.get_many(
                {"candidate": None, "recovery": None, "status": {}}
            )
            values = {"candidate": candidate}
            if completed_recovery is not None:
                if state["recovery"] != completed_recovery or state["status"].get(
                    "phase"
                ) in {"paused", "disabled"}:
                    return False
                values["status"] = state["status"] | {
                    "phase": "ready_to_activate",
                    "current_file": "",
                    "error": None,
                }
            self.journal.set_many(values)
            self._discard_staged(state["candidate"])
            return True

    async def _complete_recovery(self, candidate, recovery):
        if not await _worker(
            self._save_candidate, candidate, completed_recovery=recovery
        ):
            return False
        # Never queue behind a control waiting for our operation lock. Even an
        # unlocked asyncio.Lock can have queued waiters entitled to acquire first.
        if candidate.get("discovered"):
            try:
                async with asyncio.timeout(0):
                    await self.control.acquire()
            except TimeoutError:
                return True
            try:
                if await self._stopped(True):
                    return True
                await _worker(self.activate)
            finally:
                self.control.release()
        return True

    def _restart_missing_job(self, job):
        with self.journal.lock:
            saved = self.journal.get_many(
                {
                    "job": None,
                    "candidate": None,
                    "recovery": None,
                    "activation": None,
                    "status": {},
                }
            )
            if any(saved[key] for key in ("candidate", "recovery", "activation")):
                raise SettingsProblem(
                    "The saved recovery index is missing. Preserve recovery state "
                    "and inspect the original file again.",
                    status=409,
                )
            if saved["job"] != job or saved["status"].get("phase") in {
                "paused",
                "disabled",
            }:
                return False
            store = getattr(self.runtime, "store", None)
            self.runtime.mongo.check_owner()
            if (
                store is None
                or store.drive_id != job["catalog"]["drive_id"]
                or store.storage_channel_id != self.settings.storage_channel
            ):
                raise SettingsProblem(
                    "The source drive cannot be confirmed. Preserve the saved backup "
                    "state before retrying.",
                    status=409,
                )
            self.journal.set("job", None)
            return True

    async def _staged_worker(self, function, *args, **kwargs):
        result = {}

        @_journalled
        async def perform():
            result["value"] = await asyncio.to_thread(function, *args, **kwargs)
            return result["value"]

        try:
            return await perform()
        except BaseException:
            if "value" in result:
                await _worker(self._discard_staged, result["value"])
            raise

    @staticmethod
    def _staged_stats(staged, channel):
        files = parts = size = 0
        branch = channel if channel != staged.catalog["storage_channel_id"] else None
        with closing(staged.nodes(branch)) as nodes:
            for node in nodes:
                if node["kind"] == "file":
                    files += 1
                    for part in parts_in(node):
                        parts += 1
                        size += part["size"]
        return files, parts, size

    async def _staged_files(self, staged, channel):
        sequence = 0
        branch = channel if channel != staged.catalog["storage_channel_id"] else None
        while row := await _worker(staged.node_after, sequence, branch):
            sequence, node = row
            if node["kind"] == "file":
                yield node

    async def _stopped(self, recovery=False):
        state = await _worker(self.journal.get_many, {"status": {}, "recovery": None})
        return state["status"].get("phase") in {"paused", "disabled"} or (
            recovery and state["recovery"] is None
        )

    async def _copy_staged(self, transport, value, destination, *, recovery=False):
        from tgdrive.replication.replica_transport import MAX_VERIFY_BATCH

        staged = await _worker(self._open_staged, value)
        source = value["catalog"]["storage_channel_id"]
        files, parts, size = await _worker(self._staged_stats, staged, source)
        await _worker(
            self.update,
            total_files=files,
            total_parts=parts,
            total_bytes=size,
            done_files=0,
            done_parts=0,
            done_bytes=0,
        )
        created = []
        complete = False
        try:

            def allocate():
                value = _CopyMapping(self.directory, self.settings.min_free_bytes)
                created.append(value)
                return value

            mapping = await _worker(allocate)
            batch = []
            verify_many = getattr(transport, "verify_many", None)
            if callable(verify_many):

                async def verify_batch():
                    if batch:
                        await verify_many(
                            destination,
                            [(message, length) for _, message, length in batch],
                        )
                        for original, message, _length in batch:
                            await _worker(mapping.put, original, message)
                        batch.clear()

                async for node in self._staged_files(staged, source):
                    for part in parts_in(node):
                        if await self._stopped(recovery):
                            return None
                        identity = (
                            f"{value['catalog']['drive_id']}:{source}:{destination}:"
                            f"{part['message_id']}:{part['size']}"
                        )
                        _, message = await _worker(
                            self.journal.intent, identity, part["size"]
                        )
                        if message is not None:
                            batch.append((part["message_id"], message, part["size"]))
                            if len(batch) == MAX_VERIFY_BATCH:
                                await verify_batch()
                await verify_batch()
            done_files = done_parts = done_bytes = 0
            next_progress = 0
            async for node in self._staged_files(staged, source):
                for part in parts_in(node):
                    if await self._stopped(recovery):
                        return None
                    identity = (
                        f"{value['catalog']['drive_id']}:{source}:{destination}:"
                        f"{part['message_id']}:{part['size']}"
                    )
                    random_id, message = await _worker(
                        self.journal.intent, identity, part["size"]
                    )
                    if message is None:
                        message = await transport.copy(
                            source,
                            part["message_id"],
                            destination,
                            random_id=random_id,
                            expected_size=part["size"],
                        )
                        await _worker(self.journal.confirm, identity, message)
                    else:
                        try:
                            verified = await _worker(mapping.lookup, part["message_id"])
                        except KeyError:
                            verified = None
                        if verified != message:
                            await transport.verify(destination, message, part["size"])
                    await _worker(mapping.put, part["message_id"], message)
                    done_parts += 1
                    done_bytes += part["size"]
                    if time.monotonic() >= next_progress:
                        await _worker(
                            self.update,
                            current_file=node["name"],
                            done_files=done_files,
                            done_parts=done_parts,
                            done_bytes=done_bytes,
                        )
                        next_progress = time.monotonic() + 1
                    await asyncio.sleep(0)
                done_files += 1
            await _worker(
                self.update,
                done_files=done_files,
                done_parts=done_parts,
                done_bytes=done_bytes,
            )
            complete = True
            return mapping
        finally:
            if not complete and created:
                await _worker(created[0].close)

    async def _copy_catalog(self, transport, catalog, destination):
        from tgdrive.replication.replica_transport import MAX_VERIFY_BATCH

        source = catalog["storage_channel_id"]
        files = list(files_in(catalog))
        total_parts = sum(len(parts_in(f)) for f in files)
        await _worker(
            self.update,
            total_files=len(files),
            total_parts=total_parts,
            total_bytes=sum(part["size"] for f in files for part in parts_in(f)),
            done_files=0,
            done_parts=0,
            done_bytes=0,
        )
        mapping = {}
        done_files = done_parts = done_bytes = 0
        current_file = ""
        next_progress_at = time.monotonic() + 1

        async def progress(*, complete=False):
            nonlocal next_progress_at
            if not complete and time.monotonic() < next_progress_at:
                return
            await _worker(
                self.update,
                current_file=current_file,
                done_files=done_files,
                done_parts=done_parts,
                done_bytes=done_bytes,
            )
            next_progress_at = time.monotonic() + 1

        try:
            # A resumed job must still recheck every journaled document. Batch those
            # reads so each cooldown does not trigger two requests per old part.
            verified = {}
            verify_many = getattr(transport, "verify_many", None)
            if callable(verify_many):
                batch = []

                async def check_batch():
                    nonlocal current_file
                    if not batch:
                        return
                    current_file = "Checking previously copied parts"
                    await progress()
                    await verify_many(
                        destination,
                        [(message, size) for _identity, message, size in batch],
                    )
                    verified.update(
                        (identity, message) for identity, message, _size in batch
                    )
                    batch.clear()

                for node in files:
                    for part in parts_in(node):
                        if (await _worker(self.journal.get, "status", {})).get(
                            "phase"
                        ) in {
                            "paused",
                            "disabled",
                        }:
                            return None
                        identity = (
                            f"{catalog['drive_id']}:{source}:{destination}:"
                            f"{part['message_id']}:{part['size']}"
                        )
                        _random_id, message = await _worker(
                            self.journal.intent, identity, part["size"]
                        )
                        if message is not None:
                            batch.append((identity, message, part["size"]))
                            if len(batch) == MAX_VERIFY_BATCH:
                                await check_batch()
                await check_batch()
            for count, node in enumerate(files):
                for part in parts_in(node):
                    if (await _worker(self.journal.get, "status", {})).get("phase") in {
                        "paused",
                        "disabled",
                    }:
                        return None
                    current_file = node["name"]
                    identity = (
                        f"{catalog['drive_id']}:{source}:{destination}:"
                        f"{part['message_id']}:{part['size']}"
                    )
                    random_id, message = await _worker(
                        self.journal.intent, identity, part["size"]
                    )
                    if message is None:
                        message = await transport.copy(
                            source,
                            part["message_id"],
                            destination,
                            random_id=random_id,
                            expected_size=part["size"],
                        )
                        await _worker(self.journal.confirm, identity, message)
                    elif verified.get(identity) != message:
                        await transport.verify(destination, message, part["size"])
                    mapping[str(part["message_id"])] = message
                    done_parts += 1
                    done_bytes += part["size"]
                    await progress()
                    await asyncio.sleep(0)
                done_files = count + 1
                await progress()
            return remap_catalog(catalog, destination, mapping)
        finally:
            await progress(complete=True)

    def _stage_bundle(
        self, catalog, replica, credentials, key, database, recovery_token
    ):
        """Prepare and verify one immutable bundle away from the event loop."""
        identity = [
            catalog,
            replica,
            credentials,
            hashlib.sha256(key.encode()).hexdigest(),
        ]
        if database is not None:
            identity.extend([database, recovery_token])
        digest = hashlib.sha256(
            json_util.dumps(
                identity,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        path = self.directory / "replica-bundles" / (digest + ".tgdb")
        if not path.exists():
            payload = {
                "format": "tgdrive-replica",
                "version": 1,
                "complete": True,
                "created_at": now(),
                "catalog": catalog,
                "replica_catalog": replica,
                "credentials": credentials,
            }
            if database is not None:
                payload["database"] = database
                if recovery_token:
                    payload["recovery_token"] = recovery_token
            content = encode_bundle(payload, key)
            if (
                shutil.disk_usage(self.directory).free
                < len(content) + self.settings.min_free_bytes
            ):
                raise SettingsProblem(
                    "Not enough local disk space to stage a recovery bundle. "
                    "Free space and retry.",
                    status=507,
                )
            atomic_private(path, content)
        staged = decode_bundle(read_private(path), key)
        if (
            staged["catalog"] != catalog
            or staged["replica_catalog"] != replica
            or staged["credentials"] != credentials
            or staged.get("database") != database
        ):
            raise ValueError(
                "The staged recovery bundle does not match this restore point"
            )
        with path.open("rb") as content:
            ciphertext_digest = hashlib.file_digest(content, "sha256").hexdigest()
        credentials_digest = hashlib.sha256(
            json.dumps(credentials, sort_keys=True).encode()
        ).hexdigest()
        return (
            digest,
            path,
            staged["version"],
            ciphertext_digest,
            path.stat().st_size,
            credentials_digest,
        )

    async def _publish(
        self,
        transport,
        source,
        target,
        catalog,
        replica,
        credentials,
        key,
        *,
        pin_source=False,
        database=None,
        recovery_token=None,
        staged_value=None,
        staged_options=None,
    ):
        previous_file = await _worker(self.journal.get, "backup_file")
        (
            digest,
            path,
            version,
            ciphertext_digest,
            size,
            credentials_digest,
        ) = (
            await _worker(
                self._stage_staged_bundle,
                staged_value,
                credentials,
                key,
                recovery_token,
                staged_options or {},
            )
            if staged_value is not None
            else await _worker(
                self._stage_bundle,
                catalog,
                replica,
                credentials,
                key,
                database,
                recovery_token,
            )
        )
        ids = []
        for channel, pin in [(target, True), (source, pin_source)]:
            if (await _worker(self.journal.get, "status", {})).get("phase") in {
                "paused",
                "disabled",
            }:
                return None
            identity = f"bundle:{channel}:{ciphertext_digest}"
            random_id, message = await _worker(self.journal.intent, identity, size)
            if message is None:
                message = await transport.publish(
                    channel,
                    path,
                    random_id=random_id,
                    caption="TGDrive encrypted recovery backup"
                    if database is not None or staged_value is not None
                    else "TGDrive independent recovery bundle v1",
                    pin=pin,
                )
                await _worker(self.journal.confirm, identity, message)
            else:
                await transport.verify(channel, message, size)
            if pin and await transport.pinned_message(channel) != message:
                raise SettingsProblem(
                    "The recovery checkpoint pin changed; "
                    "inspect the channel before retrying.",
                    code="REPLICA_PIN_CHANGED",
                    status=409,
                )
            ids.append(message)
        await _worker(
            self.journal.set_many,
            {
                "restore_point": {
                    "source_channel": source,
                    "source_message_id": ids[1],
                    "backup_channel": target,
                    "backup_message_id": ids[0],
                    "revision": catalog["revision"],
                    "created_at": now(),
                    "bundle_size": size,
                    "format_version": version,
                },
                "credentials_digest": credentials_digest,
                "backup_file": {
                    "digest": digest,
                    "key": key,
                    "sha256": ciphertext_digest,
                    "created_at": now(),
                }
                if database is not None or staged_value is not None
                else None,
            },
        )
        if (database is not None or staged_value is not None) and isinstance(
            previous_file, dict
        ):
            import re

            old_digest = previous_file.get("digest")
            if (
                isinstance(old_digest, str)
                and old_digest != digest
                and re.fullmatch(r"[a-f0-9]{64}", old_digest)
            ):
                root = self.directory.resolve()
                previous_path = root / "replica-bundles" / (old_digest + ".tgdb")
                if (
                    not previous_path.is_symlink()
                    and previous_path.parent.resolve() == root / "replica-bundles"
                ):
                    try:
                        previous_path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning(
                            "Could not remove an older confirmed local recovery file"
                        )
        return ids

    def _stage_staged_bundle(self, value, credentials, key, recovery_token, options):
        from tgdrive.recovery.snapshot_stream import write_staged_snapshot
        from tgdrive.recovery.staged_snapshot import validate_snapshot

        staged = self._open_staged(value)
        mapping = options.get("copy_mapping")
        identity = {
            "staged": staged.digest,
            "credentials": credentials,
            "key": hashlib.sha256(key.encode()).hexdigest(),
            "token": recovery_token,
            "options": {
                name: item for name, item in options.items() if name != "copy_mapping"
            },
            "mapping": mapping.digest() if mapping is not None else None,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        path = self.directory / "replica-bundles" / (digest + ".tgdb")
        saved = self.journal.get("staged_publication", {})
        credentials_digest = hashlib.sha256(
            json.dumps(credentials, sort_keys=True).encode()
        ).hexdigest()
        if saved.get("digest") == digest:
            with path.open("rb") as source:
                ciphertext_digest = hashlib.file_digest(source, "sha256").hexdigest()
            if ciphertext_digest != saved["sha256"]:
                raise ValueError(
                    "The staged recovery file changed; nothing was published"
                )
        else:
            writer_options = self._staged_writer_options(staged, mapping, options)
            _, _, ciphertext_digest = write_staged_snapshot(
                staged,
                path,
                bytes.fromhex(key.strip()),
                self.settings,
                validate_snapshot=partial(
                    validate_snapshot, min_free_bytes=self.settings.min_free_bytes
                ),
                credentials=credentials,
                recovery_token=recovery_token,
                **writer_options,
                max_expanded_bytes=None,
                max_recovery_bytes=MAX_STAGED_RECOVERY_BYTES,
            )
            self.journal.set(
                "staged_publication", {"digest": digest, "sha256": ciphertext_digest}
            )
        return (
            digest,
            path,
            2,
            ciphertext_digest,
            path.stat().st_size,
            credentials_digest,
        )

    @staticmethod
    def _staged_writer_options(staged, mapping, options):
        result = {
            name: item for name, item in options.items() if name != "copy_mapping"
        }
        reverse = result.pop("reverse", False)
        if mapping is not None:
            if result.get("target_channel") is not None:
                result["mapping"] = mapping.lookup
            result["replica_mapping"] = mapping.pairs(reverse=reverse)
        elif result.get("replica_channel") is not None:

            def pairs():
                with closing(staged._connect()) as db:
                    yield from db.execute(
                        "SELECT source,destination FROM mappings ORDER BY source"
                    )

            result["replica_mapping"] = pairs()
        return result

    async def _sync(self, config, *, force_check=False, force_snapshot=False):
        self._check_resources()
        store = self.runtime.store
        state = await _worker(self.journal.get, "status", {})
        error = state.get("error") or {}
        if error.get("retry_at", 0) > time.time():
            from tgdrive.recovery.backup import BackupError

            raise BackupError(
                "Telegram requested a wait before backup resumes.",
                retry_after=error["retry_at"] - time.time(),
            )
        current_credentials = await asyncio.to_thread(self._credentials)
        if (await _worker(self.journal.get, "status", {})).get("phase") in {
            "paused",
            "disabled",
        }:
            return
        credentials_digest = hashlib.sha256(
            json.dumps(current_credentials, sort_keys=True).encode()
        ).hexdigest()
        same_credentials = (
            await _worker(self.journal.get, "credentials_digest")
        ) == credentials_digest
        point = await _worker(self.journal.get, "restore_point", {})
        check_only = (
            same_credentials
            and state.get("protected_revision") == store.revision
            and bool(point.get("backup_message_id"))
            and not force_snapshot
        )
        if (
            check_only
            and not error
            and not force_check
            and state.get("checked_at", 0) > time.time() - HEALTH_CHECK_INTERVAL_SECONDS
        ):
            if state.get("phase") in {"idle", "waiting", "syncing"}:
                await _worker(
                    self.update, phase="protected", current_file="", error=None
                )
            return
        job = await _worker(self.journal.get, "job")
        if job is not None and "staged" in job and not check_only:
            try:
                await _worker(self._open_staged, job)
            except FileNotFoundError:
                if not await _worker(self._restart_missing_job, job):
                    return
                job = None
        if job is None and not check_only:
            job = await self._staged_worker(self._capture)
            try:
                catalog = job["catalog"]
                if await self._stopped():
                    return
                if catalog.get("storage_channel_id") not in {
                    None,
                    self.settings.storage_channel,
                }:
                    raise ValueError("Catalog and source channel do not match")
                catalog["storage_channel_id"] = self.settings.storage_channel
                if "staged" not in job:
                    validate_catalog(catalog)
                await _worker(self.journal.set, "job", job)
            finally:
                await _worker(self._discard_staged, job)
        if check_only:
            # The recorded checkpoint is still confirmed while its health is
            # checked; do not present an unchanged drive as copying files again.
            await _worker(self.journal.set, "job", None)
            await _worker(self._discard_staged, job)
            job = None
            await _worker(self.update, phase="protected", current_file="", error=None)
        else:
            await _worker(self.update, phase="syncing", error=None)
        transport = self.factory(self.settings, config["token"])
        copy_mapping = None
        try:
            await transport.start()
            await transport.inspect(self.settings.storage_channel)
            await transport.inspect(config["channel"])
            if check_only:
                await transport.verify(
                    config["channel"], point["backup_message_id"], point["bundle_size"]
                )
                current_pin = await transport.pinned_message(config["channel"])
                if current_pin != point["backup_message_id"]:
                    raise SettingsProblem(
                        "The latest recovery checkpoint is no longer pinned. The "
                        "confirmed restore point remains available by its recorded "
                        "message ID. Inspect the backup channel before resuming.",
                        code="REPLICA_PIN_CHANGED",
                        status=409,
                    )
                phase = (await _worker(self.journal.get, "status", {})).get("phase")
                await _worker(
                    self.update,
                    phase=phase if phase in {"paused", "disabled"} else "protected",
                    error=None,
                    checked_at=time.time(),
                )
                self._request_pacer.reset()
                await _worker(self.journal.set, "job", None)
                return
            catalog = job["catalog"]
            if "staged" in job:
                copy_mapping = await self._copy_staged(
                    transport, job, config["channel"]
                )
                replica = dict(catalog, storage_channel_id=config["channel"])
                if copy_mapping is None:
                    return
            else:
                replica = await self._copy_catalog(
                    transport, catalog, config["channel"]
                )
            if replica is None:
                return
            if (await _worker(self.journal.get, "status", {})).get("phase") in {
                "paused",
                "disabled",
            }:
                return
            credentials = dict(job["credentials"])
            published = await self._publish(
                transport,
                self.settings.storage_channel,
                config["channel"],
                catalog,
                replica,
                credentials,
                self.key(),
                database=job.get("database"),
                recovery_token=config["token"],
                # Pin in main too, so a restore needs only the env's bot token.
                pin_source=True,
                staged_value=job if "staged" in job else None,
                staged_options={
                    "copy_mapping": copy_mapping,
                    "replica_channel": config["channel"],
                },
            )
            if published is None:
                return
            await _worker(
                self.journal.set,
                "credentials_digest",
                hashlib.sha256(
                    json.dumps(job["credentials"], sort_keys=True).encode()
                ).hexdigest(),
            )
            phase = (await _worker(self.journal.get, "status", {})).get("phase")
            await _worker(
                self.update,
                phase=phase if phase in {"paused", "disabled"} else "protected",
                protected_revision=catalog["revision"],
                last_success_at=now(),
                current_file="",
                error=None,
                checked_at=time.time(),
            )
            await _worker(
                self.report,
                "info",
                "Restore point confirmed in both channels "
                f"at catalog revision {catalog['revision']}.",
            )
            self._request_pacer.reset()
            await _worker(self.journal.set, "job", None)
            await _worker(self._discard_staged, job)
        finally:
            if copy_mapping is not None:
                await _worker(copy_mapping.close)
            await transport.close()

    async def inspect_recovery(self, source, token, key, message_id=None):
        transport = self.factory(self.settings, token)
        path = self.directory / (".replica-inspect-" + secrets.token_hex(12))
        bundle = None
        try:
            await transport.start()
            await transport.inspect(source)
            inspected_id = await transport.download(source, message_id, path)
            key = key or self.key()
            bundle = await self._staged_worker(self._read_bundle, path, key)
            from tgdrive.replication.replica_activation import check_recovery_key

            check_recovery_key(self.settings, bundle.get("database"))
            catalog = next(
                (
                    bundle[name]
                    for name in ("replica_catalog", "catalog")
                    if bundle.get(name) is not None
                    and bundle[name].get("storage_channel_id") == source
                ),
                None,
            )
            if catalog is None:
                raise ValueError(
                    "This restore point does not belong to the selected channel"
                )
            await self._check_restore_target(catalog["drive_id"])
            await _worker(
                self._save_candidate,
                {
                    "catalog": catalog,
                    "credentials": bundle["credentials"],
                    "token": token,
                    "key": key.strip(),
                    "complete": False,
                    **self._database_fields(bundle),
                    "source_is_backup": source
                    != bundle["catalog"]["storage_channel_id"],
                    "inspection": {
                        "source_channel": source,
                        "source_message_id": inspected_id,
                        "revision": catalog["revision"],
                        "created_at": now(),
                    },
                },
            )
            await _worker(self.update, phase="paused", error=None)
            await _worker(
                self.report,
                "info",
                "Authenticated restore point inspected. "
                "No live files or configuration changed.",
            )
        finally:
            path.unlink(missing_ok=True)
            await _worker(self._discard_staged, bundle)
            await transport.close()
        return await _worker(self.status)

    def _recovery_transport(self, token, credentials=None):
        updates = {"bot_tokens_csv": SecretStr("")}
        if credentials:
            updates.update(
                api_id=credentials["api_id"],
                api_hash=SecretStr(credentials["api_hash"]),
            )
        return self.factory(self.settings.model_copy(update=updates), token)

    async def discover_backup(self):
        """Find the pinned backup in the main channel using the env's bot tokens.

        Returns True when a candidate was saved for one-click restore, False when
        the channel has no TGDrive backup pinned. A pinned backup that cannot be
        opened raises, so a new empty drive is never created over it.
        """
        from tgdrive.replication.replica_transport import ReplicaTransportError

        channel = self.settings.storage_channel
        key = self.key()
        last = None
        for token in self.settings.bot_tokens:
            transport = self._recovery_transport(token)
            path = self.directory / (".replica-discover-" + secrets.token_hex(12))
            bundle = None
            try:
                await transport.start()
                await transport.inspect(channel, writable=False)
                if await transport.pinned_message(channel) is None:
                    return False
                try:
                    await transport.download(channel, None, path)
                except ReplicaTransportError as error:
                    if error.code == "REPLICA_DOCUMENT_TYPE":
                        return False
                    raise
                try:
                    bundle = await self._staged_worker(self._read_bundle, path, key)
                except LegacyRecoveryFile as error:
                    raise SettingsProblem(
                        str(error), code="LEGACY_BACKUP_REQUIRES_UPGRADE", status=409
                    ) from None
                except ValueError:
                    raise SettingsProblem(
                        "A TGDrive backup is pinned in the storage channel, but "
                        "ENCRYPTION_KEY cannot open it. Start with the key that "
                        "created this drive; nothing was changed.",
                        code="BACKUP_KEY_MISMATCH",
                        status=409,
                    ) from None
                return await self._save_discovery(bundle, channel, token, key)
            except ReplicaTransportError as error:
                last = error
                continue
            finally:
                path.unlink(missing_ok=True)
                await _worker(self._discard_staged, bundle)
                await transport.close()
        if last is not None:
            raise (await _worker(self._record_failure, last))
        return False

    async def _save_discovery(self, bundle, channel, token, key):
        catalog = next(
            (
                bundle[name]
                for name in ("catalog", "replica_catalog")
                if bundle.get(name) is not None
                and bundle[name]["storage_channel_id"] == channel
            ),
            None,
        )
        if catalog is None:
            return False
        await _worker(
            self._save_candidate,
            {
                "catalog": catalog,
                "credentials": bundle["credentials"],
                "token": token,
                "key": key,
                "complete": False,
                **self._database_fields(bundle),
                "source_is_backup": channel != bundle["catalog"]["storage_channel_id"],
                "discovered": True,
                "inspection": {
                    "source_channel": channel,
                    "revision": catalog["revision"],
                    "created_at": bundle["created_at"],
                },
            },
        )
        await _worker(self.update, phase="paused", error=None)
        await _worker(
            self.report,
            "info",
            "Found the latest backup in the storage channel. "
            "Choose Restore drive to bring it back.",
        )
        return True

    async def restore_discovered(self):
        """One-click restore of a discovered backup onto its own channel."""
        candidate = await _worker(self.journal.get, "candidate")
        if not candidate or not candidate.get("discovered"):
            raise SettingsProblem("No discovered backup is waiting.", status=409)
        channel = candidate["catalog"]["storage_channel_id"]
        return await self.recover_to(
            channel, candidate["token"], str(channel), catalog_only=True
        )

    async def inspect_file(self, path, key, source=None, token=None):
        """Authenticate a saved file before probing its recorded channels."""
        await _worker(self._check_saved_state)
        retry_at = (
            (await _worker(self.journal.get, "status", {})).get("error") or {}
        ).get("retry_at", 0)
        if retry_at > time.time():
            raise SettingsProblem(
                "Telegram is waiting before recovery can retry. "
                "Wait for the displayed retry time.",
                status=409,
            )
        bundle = await self._staged_worker(self._read_bundle, path, key)
        try:
            return await self._inspect_file_bundle(bundle, key, source, token)
        finally:
            await _worker(self._discard_staged, bundle)

    async def _inspect_file_bundle(self, bundle, key, source, token):
        from tgdrive.replication.replica_activation import check_recovery_key
        from tgdrive.replication.replica_transport import ReplicaTransportError

        check_recovery_key(self.settings, bundle.get("database"))
        catalogs = [bundle["catalog"]]
        if bundle.get("replica_catalog") is not None:
            catalogs.append(bundle["replica_catalog"])
        if source is not None:
            catalogs = [
                catalog
                for catalog in catalogs
                if catalog["storage_channel_id"] == source
            ]
            if not catalogs:
                raise SettingsProblem(
                    "The selected channel is not recorded in this recovery file."
                )
        await self._check_restore_target(bundle["catalog"]["drive_id"])
        tokens = (
            [token]
            if token
            else list(
                dict.fromkeys(
                    [bundle.get("recovery_token"), *bundle["credentials"]["bot_tokens"]]
                )
            )
        )
        selected = None
        for catalog in catalogs:
            for access in tokens:
                if not access:
                    continue
                transport = self._recovery_transport(access, bundle["credentials"])
                try:
                    await transport.start()
                    await transport.inspect(catalog["storage_channel_id"])
                    selected = catalog, access
                except ReplicaTransportError as error:
                    if (
                        getattr(error, "retry_after", None)
                        or getattr(error, "code", None) == "REPLICA_FLOOD_WAIT"
                    ):
                        raise (await _worker(self._record_failure, error)) from None
                finally:
                    await transport.close()
                if selected:
                    break
            if selected:
                break
        if selected is None:
            raise SettingsProblem(
                "No recorded channel could be accessed. Supply a surviving channel "
                "and a working bot token with administrator access.",
                code="RECOVERY_ACCESS_REQUIRED",
            )
        catalog, access = selected
        candidate = {
            "catalog": catalog,
            "credentials": bundle["credentials"],
            "token": access,
            "key": key.strip(),
            "complete": False,
            "source_is_backup": catalog["storage_channel_id"]
            != bundle["catalog"]["storage_channel_id"],
            "inspection": {
                "source_channel": catalog["storage_channel_id"],
                "revision": catalog["revision"],
                "created_at": now(),
            },
        }
        self._attach_database(candidate, bundle)
        backup = bundle.get("replica_catalog")
        backup_token = bundle.get("recovery_token")
        if (
            backup is not None
            and backup_token
            and backup["storage_channel_id"] != catalog["storage_channel_id"]
        ):
            transport = self._recovery_transport(backup_token, bundle["credentials"])
            try:
                await transport.start()
                await transport.inspect(backup["storage_channel_id"])
                candidate["backup_config"] = {
                    "channel": backup["storage_channel_id"],
                    "token": backup_token,
                }
                if "staged" in bundle:
                    await _worker(
                        self._remember_staged_mapping, bundle, catalog, backup
                    )
                else:
                    await _worker(self._remember_mapping, catalog, backup)
            except ReplicaTransportError as error:
                if error.retry_after:
                    raise (await _worker(self._record_failure, error)) from None
            finally:
                await transport.close()
        await _worker(self._save_candidate, candidate)
        await _worker(self.update, phase="paused", error=None)
        await _worker(
            self.report,
            "info",
            "Recovery file opened. Review the restore destination; "
            "live data is unchanged.",
        )
        return await _worker(self.status)

    @staticmethod
    def _database_fields(bundle):
        if "staged" in bundle:
            return {
                name: bundle[name] for name in ("staged", "file_count", "total_bytes")
            }
        return {"database": bundle["database"]} if "database" in bundle else {}

    @classmethod
    def _attach_database(cls, candidate, bundle):
        candidate.update(cls._database_fields(bundle))

    def _remember_staged_mapping(self, value, source, destination):
        staged = self._open_staged(value)
        with closing(staged.nodes()) as nodes:
            for node in nodes:
                if node["kind"] != "file":
                    continue
                for part in parts_in(node):
                    if self.journal.get("status", {}).get("phase") in {
                        "paused",
                        "disabled",
                    }:
                        return
                    identity = (
                        f"{source['drive_id']}:{source['storage_channel_id']}:"
                        f"{destination['storage_channel_id']}:"
                        f"{part['message_id']}:{part['size']}"
                    )
                    self.journal.intent(identity, part["size"])
                    self.journal.confirm(identity, staged.mapping(part["message_id"]))

    async def _check_restore_target(self, drive_id):
        """Restore only onto the same drive or a new, unused one."""
        current = getattr(self.runtime, "store", None)
        if current is None or current.drive_id == drive_id:
            return
        from tgdrive.recovery.mongo_backup import check_restore_destination

        try:
            await _worker(
                check_restore_destination, self.runtime.mongo, allow_pristine=True
            )
        except FileExistsError:
            raise SettingsProblem(
                "This backup belongs to another drive. "
                "Restore into an empty installation.",
                status=409,
            ) from None

    def _remember_mapping(self, source, destination):
        originals = {node["id"]: node for node in files_in(source)}
        for node in files_in(destination):
            for old, new in zip(
                parts_in(originals[node["id"]]), parts_in(node), strict=True
            ):
                identity = (
                    f"{source['drive_id']}:{source['storage_channel_id']}:"
                    f"{destination['storage_channel_id']}:"
                    f"{old['message_id']}:{old['size']}"
                )
                self.journal.intent(identity, old["size"])
                self.journal.confirm(identity, new["message_id"])

    async def recover_to(
        self, target, target_token, confirmation, *, catalog_only=False
    ):
        await _worker(self._check_saved_state)
        candidate = await _worker(self.journal.get, "candidate")
        if not candidate or confirmation != str(target) or target >= 0:
            raise SettingsProblem(
                "Inspect a restore point, then type the new destination "
                "channel ID exactly."
            )
        source = candidate["catalog"]["storage_channel_id"]
        if source == target and not catalog_only:
            raise SettingsProblem("Recovery requires a different destination channel.")
        if catalog_only and target != source:
            raise SettingsProblem(
                "Database-only recovery must use the inspected surviving channel."
            )
        if catalog_only and not target_token:
            from tgdrive.replication.replica_transport import ReplicaTransportError

            for saved_token in dict.fromkeys(
                [*candidate["credentials"]["bot_tokens"], candidate["token"]]
            ):
                backup = candidate.get("backup_config")
                if (
                    backup
                    and saved_token.split(":")[0] == backup["token"].split(":")[0]
                ):
                    continue
                probe = self._recovery_transport(saved_token, candidate["credentials"])
                try:
                    await probe.start()
                    await probe.inspect(source)
                    target_token = saved_token
                    break
                except ReplicaTransportError as error:
                    if error.retry_after:
                        raise (await _worker(self._record_failure, error)) from None
                finally:
                    await probe.close()
        if not target_token or (
            not catalog_only
            and target_token.split(":")[0] == candidate["token"].split(":")[0]
        ):
            raise SettingsProblem("Supply a new main bot distinct from the backup bot.")
        saved_backup = candidate.get("backup_config")
        if (
            saved_backup
            and saved_backup["token"].split(":")[0] == target_token.split(":")[0]
        ):
            raise SettingsProblem(
                "Enter a separate main bot token to keep "
                "the surviving backup independent."
            )
        registry = getattr(self.runtime, "registry", None)
        if registry and any(
            r.committed.status in {"queued", "running", "cancelling"}
            for r in registry.list()
        ):
            raise SettingsProblem(
                "Cancel or finish all transfers before recovery. Their existing "
                "channel references must not replay after recovery.",
                status=409,
            )
        await _worker(
            self.journal.set,
            "recovery",
            {
                "target": target,
                "target_token": target_token,
                "catalog_only": catalog_only,
            },
        )
        await _worker(self.update, phase="recovering", error=None)
        self.start()
        self._wake.set()
        return await _worker(self.status)

    async def _recover_copy(self, recovery):
        if (await _worker(self.journal.get, "status", {})).get("phase") in {
            "paused",
            "disabled",
        } or (await _worker(self.journal.get, "recovery")) is None:
            return
        candidate = await _worker(self.journal.get, "candidate")
        if (
            candidate.get("complete")
            and candidate["catalog"]["storage_channel_id"] == recovery["target"]
        ):
            await self._complete_recovery(candidate, recovery)
            return
        source = candidate["catalog"]["storage_channel_id"]
        transport = self._recovery_transport(
            candidate["token"], candidate["credentials"]
        )
        try:
            await transport.start()
            await transport.inspect(source)
            await transport.inspect(recovery["target"])
            target_pin = await transport.pinned_message(recovery["target"])
            if (await _worker(self.journal.get, "status", {})).get("phase") in {
                "paused",
                "disabled",
            } or (await _worker(self.journal.get, "recovery")) is None:
                return
            reservation = {
                "drive_id": candidate["catalog"]["drive_id"],
                "source": source,
                "target": recovery["target"],
            }
            if (
                not recovery.get("catalog_only")
                and (await _worker(self.journal.get, "recovery_destination"))
                != reservation
            ):
                if target_pin is not None:
                    raise SettingsProblem(
                        "The replacement channel already has a pinned message. "
                        "Use a new dedicated private channel.",
                        status=409,
                    )
                await _worker(self.journal.set, "recovery_destination", reservation)
            # Separately authenticate the replacement main bot before copying.
            probe = self._recovery_transport(
                recovery["target_token"], candidate["credentials"]
            )
            try:
                await probe.start()
                await probe.inspect(recovery["target"])
            finally:
                await probe.close()
            if (await _worker(self.journal.get, "status", {})).get("phase") in {
                "paused",
                "disabled",
            } or (await _worker(self.journal.get, "recovery")) is None:
                return
            await _worker(self.update, phase="recovering", error=None)
            if "staged" in candidate:
                await self._recover_staged(transport, candidate, recovery, target_pin)
                return
            if recovery.get("catalog_only"):
                for node in files_in(candidate["catalog"]):
                    for part in parts_in(node):
                        if (await _worker(self.journal.get, "status", {})).get(
                            "phase"
                        ) in {"paused", "disabled"} or (
                            await _worker(self.journal.get, "recovery")
                        ) is None:
                            return
                        await transport.verify(source, part["message_id"], part["size"])
                restored = remap_catalog(
                    candidate["catalog"],
                    source,
                    {
                        str(p["message_id"]): p["message_id"]
                        for n in files_in(candidate["catalog"])
                        for p in parts_in(n)
                    },
                )
            else:
                restored = await self._copy_catalog(
                    transport, candidate["catalog"], recovery["target"]
                )
            if restored is None:
                return
            if (await _worker(self.journal.get, "status", {})).get("phase") in {
                "paused",
                "disabled",
            } or (await _worker(self.journal.get, "recovery")) is None:
                return
            if (
                target_pin is not None
                and not recovery.get("catalog_only")
                and not (
                    await _worker(
                        self.journal.owns_publication, recovery["target"], target_pin
                    )
                )
            ):
                await self._verify_recovery_pin(
                    transport, recovery["target"], target_pin, candidate, restored
                )
            credentials = dict(
                candidate["credentials"], bot_tokens=[recovery["target_token"]]
            )
            database = candidate.get("database")
            if (await _worker(self.journal.get, "status", {})).get("phase") in {
                "paused",
                "disabled",
            } or (await _worker(self.journal.get, "recovery")) is None:
                return
            if database is not None and not recovery.get("catalog_only"):
                from tgdrive.recovery.recovery_file import rebase_database

                rebased = await asyncio.to_thread(rebase_database, database, restored)
                await self._publish(
                    transport,
                    recovery["target"],
                    source,
                    restored,
                    candidate["catalog"],
                    credentials,
                    candidate["key"],
                    database=rebased,
                    recovery_token=candidate["token"],
                    pin_source=True,
                )
                # Reuse the surviving backup's verified documents after activation.
                await _worker(self._remember_mapping, restored, candidate["catalog"])
                candidate["backup_config"] = {
                    "channel": source,
                    "token": candidate["token"],
                }
            elif not recovery.get("catalog_only"):
                await self._publish(
                    transport,
                    source,
                    recovery["target"],
                    candidate["catalog"],
                    restored,
                    credentials,
                    candidate["key"],
                )
            if (await _worker(self.journal.get, "status", {})).get("phase") in {
                "paused",
                "disabled",
            } or (await _worker(self.journal.get, "recovery")) is None:
                return
            if recovery.get("catalog_only"):
                await _worker(
                    self.journal.set_many,
                    {
                        "restore_point": candidate["inspection"],
                        "restore_point_key": candidate["key"],
                        "credentials_digest": hashlib.sha256(
                            json.dumps(credentials, sort_keys=True).encode()
                        ).hexdigest(),
                    },
                )
            completed = dict(
                candidate, catalog=restored, credentials=credentials, complete=True
            )
            if (
                completed.get("backup_config", {}).get("channel")
                == restored["storage_channel_id"]
            ):
                completed.pop("backup_config")
            if not await self._complete_recovery(completed, recovery):
                return
            await _worker(
                self.report,
                "info",
                "All destination files and recovery metadata confirmed. "
                "Review and activate to switch TGDrive.",
            )
        finally:
            await transport.close()
            await _worker(self._discard_staged, candidate)

    def _derive_staged(self, candidate, credentials, options):
        from tgdrive.recovery.snapshot_stream import write_staged_json
        from tgdrive.recovery.staged_snapshot import stage_snapshot
        from tgdrive.replication.replica_activation import _staged_catalog

        staged = self._open_staged(candidate)
        directory = self.directory / "recovery-staging"
        identity = secrets.token_hex(16)
        plaintext, index = (
            directory / (identity + ".json"),
            directory / (identity + ".sqlite"),
        )
        try:
            write_staged_json(
                staged,
                plaintext,
                self.settings,
                credentials=credentials,
                max_expanded_bytes=None,
                **self._staged_writer_options(
                    staged, options.get("copy_mapping"), options
                ),
            )
            result = stage_snapshot(
                plaintext, index, min_free_bytes=self.settings.min_free_bytes
            )
            return {
                "staged": {
                    "path": str(index.relative_to(self.directory)),
                    "sha256": result.digest,
                },
                "catalog": _staged_catalog(
                    result, result.catalog["storage_channel_id"]
                ),
                "credentials": result.header["credentials"],
                "file_count": result.file_count,
                "total_bytes": result.total_bytes,
            }
        except BaseException:
            index.unlink(missing_ok=True)
            raise
        finally:
            plaintext.unlink(missing_ok=True)

    async def _recover_staged(self, transport, candidate, recovery, target_pin):
        source, target = candidate["catalog"]["storage_channel_id"], recovery["target"]
        staged = await _worker(self._open_staged, candidate)
        mapping = None
        derived = None
        try:
            if recovery.get("catalog_only"):
                async for node in self._staged_files(staged, source):
                    for part in parts_in(node):
                        if await self._stopped(True):
                            return
                        await transport.verify(source, part["message_id"], part["size"])
            else:
                mapping = await self._copy_staged(
                    transport, candidate, target, recovery=True
                )
                if mapping is None:
                    return
            if await self._stopped(True):
                return
            if (
                target_pin is not None
                and not recovery.get("catalog_only")
                and not (
                    await _worker(self.journal.owns_publication, target, target_pin)
                )
            ):
                await self._verify_staged_pin(
                    transport, target, target_pin, candidate, mapping
                )
            credentials = dict(
                candidate["credentials"], bot_tokens=[recovery["target_token"]]
            )
            options = {
                "node_channel": source
                if source != staged.catalog["storage_channel_id"]
                else None,
                "target_channel": target,
            }
            if mapping is not None:
                options.update(
                    copy_mapping=mapping, replica_channel=source, reverse=True
                )
                published = await self._publish(
                    transport,
                    target,
                    source,
                    dict(candidate["catalog"], storage_channel_id=target),
                    candidate["catalog"],
                    credentials,
                    candidate["key"],
                    recovery_token=candidate["token"],
                    pin_source=True,
                    staged_value=candidate,
                    staged_options=options,
                )
                if published is None:
                    return
                candidate["backup_config"] = {
                    "channel": source,
                    "token": candidate["token"],
                }
            elif candidate.get("backup_config"):
                options["replica_channel"] = candidate["backup_config"]["channel"]
            derived = await self._staged_worker(
                self._derive_staged, candidate, credentials, options
            )
            if await self._stopped(True):
                return
            if mapping is not None:
                await _worker(
                    self._remember_staged_mapping,
                    derived,
                    derived["catalog"],
                    candidate["catalog"],
                )
                if await self._stopped(True):
                    return
            if recovery.get("catalog_only"):
                await _worker(
                    self.journal.set_many,
                    {
                        "restore_point": candidate["inspection"],
                        "restore_point_key": candidate["key"],
                        "credentials_digest": hashlib.sha256(
                            json.dumps(credentials, sort_keys=True).encode()
                        ).hexdigest(),
                    },
                )
            completed = (
                candidate | derived | {"complete": True, "source_is_backup": False}
            )
            if completed.get("backup_config", {}).get("channel") == target:
                completed.pop("backup_config")
            if not await self._complete_recovery(completed, recovery):
                return
            await _worker(
                self.report,
                "info",
                "All destination files and recovery metadata confirmed. "
                "Review and activate to switch TGDrive.",
            )
        finally:
            if mapping is not None:
                await _worker(mapping.close)
            if derived is not None:
                await _worker(self._discard_staged, derived)

    def _staged_pin_matches(self, pinned, candidate, mapping, target):
        if "staged" not in pinned:
            return False
        expected, actual = self._open_staged(candidate), self._open_staged(pinned)
        if (
            expected.catalog["drive_id"] != actual.catalog["drive_id"]
            or expected.catalog["revision"] != actual.catalog["revision"]
        ):
            return False
        source = candidate["catalog"]["storage_channel_id"]
        source_branch = (
            source if source != expected.catalog["storage_channel_id"] else None
        )
        target_branch = (
            target if target != actual.catalog["storage_channel_id"] else None
        )
        if target_branch is not None and actual.header["replica"] != {
            "channel": target
        }:
            return False
        sentinel = object()
        from itertools import zip_longest

        with (
            closing(expected.nodes(source_branch)) as originals,
            closing(actual.nodes(target_branch)) as replacements,
        ):
            for old, new in zip_longest(originals, replacements, fillvalue=sentinel):
                if old is sentinel or new is sentinel:
                    return False
                if old["kind"] == "file":
                    old["message_id"] = mapping.lookup(old["message_id"])
                    for part in old.get("parts") or []:
                        part["message_id"] = mapping.lookup(part["message_id"])
                    if old.get("poster"):
                        old["poster"]["message_id"] = mapping.lookup(
                            old["poster"]["message_id"]
                        )
                if old != new:
                    return False
        return True

    async def _verify_staged_pin(self, transport, channel, message, candidate, mapping):
        path = self.directory / (".replica-pin-" + secrets.token_hex(12))
        pinned = None
        try:
            await transport.download(channel, message, path)
            pinned = await self._staged_worker(
                self._read_bundle, path, candidate["key"]
            )
            if not await _worker(
                self._staged_pin_matches, pinned, candidate, mapping, channel
            ):
                raise ValueError(
                    "The replacement channel pin changed outside this recovery. "
                    "Nothing was activated."
                )
        finally:
            path.unlink(missing_ok=True)
            if pinned is not None and "staged" in pinned:
                await _worker(self._discard_staged, pinned)

    def activate(self):
        self._check_saved_state()
        candidate = self.journal.get("candidate")
        if not candidate or not candidate.get("complete"):
            raise SettingsProblem(
                "Complete and verify recovery before activation.", status=409
            )
        from tgdrive.replication.replica_activation import prepare_activation

        prepare_activation(self.settings, candidate)
        self.journal.set("activation", True)
        self.update(phase="restart_required")
        self.report(
            "info",
            "Recovery activation saved. Restart TGDrive; "
            "the former catalog is preserved.",
        )
        return self.status()

    async def _verify_recovery_pin(
        self, transport, channel, message_id, candidate, restored
    ):
        path = self.directory / (".replica-pin-" + secrets.token_hex(12))
        try:
            await transport.download(channel, message_id, path)
            bundle = await asyncio.to_thread(
                lambda: decode_bundle(read_private(path), candidate["key"])
            )
            matched = (
                bundle["catalog"] == candidate["catalog"]
                and bundle["replica_catalog"] == restored
            )
            if "database" in candidate:
                matched = matched or (
                    bundle["catalog"] == restored
                    and bundle["replica_catalog"] == candidate["catalog"]
                )
            if not matched:
                raise ValueError(
                    "The replacement channel pin changed outside this recovery. "
                    "Nothing was activated."
                )
        finally:
            path.unlink(missing_ok=True)

    def _mutation_config(self):
        self._check_resources()
        saved = self.journal.get_many(
            {"recovery": None, "activation": None, "config": {}, "status": {}}
        )
        if saved["recovery"] or saved["activation"]:
            raise SettingsProblem(
                "Channel recovery is in progress. Resume it or disable the "
                "unfinished recovery before changing files.",
                status=409,
            )
        config = saved["config"]
        if not config.get("enabled"):
            return None
        if saved["status"].get("phase") in {"paused", "disabled"}:
            raise SettingsProblem(
                "Resume independent backup before Telegram mutations, "
                "or explicitly disable backup protection.",
                status=409,
            )
        return config

    async def before_mutation(self):
        if await _worker(self._mutation_config) is None:
            return
        async with self._operation:
            config = await _worker(self._mutation_config)
            if config is None:
                return
            required = self.runtime.store.revision
            for _ in range(2):
                saved_retry_at = (
                    (await _worker(self.journal.get, "status", {})).get("error") or {}
                ).get("retry_at", 0)
                remaining_wait = saved_retry_at - time.time()
                if remaining_wait > 0:
                    from tgdrive.recovery.backup import BackupError

                    # Reject outside the provider-error handler: a later check
                    # must not turn the saved cooldown into a new deadline.
                    raise BackupError(
                        "Telegram requested a wait before backup resumes.",
                        retry_after=remaining_wait,
                    )
                try:
                    await self._sync(config, force_check=True)
                except Exception as error:
                    raise (await _worker(self._record_failure, error)) from None
                if await _worker(self._mutation_config) is None:
                    return
                if (await _worker(self.journal.get, "status", {})).get(
                    "protected_revision", 0
                ) >= required:
                    return
        raise SettingsProblem(
            "The required backup restore point is not confirmed. "
            "Check backup status and retry.",
            status=503,
        )
