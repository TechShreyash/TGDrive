"""Optional independent channel copies with crash-resumable, explicit recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import secrets
import shutil
import time
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial

from bson import json_util
from pydantic import SecretStr

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
from tgdrive.storage.store import DriveStore

HEALTH_CHECK_INTERVAL_SECONDS = 60 * 60
BACKUP_BATCH_SECONDS = 60
logger = logging.getLogger(__name__)


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


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
        self.journal = ReplicaJournal(self.directory)
        saved_mongo_key = self.journal.get("mongodb_key")
        if settings.mongodb_backup_key is None and saved_mongo_key:
            settings.mongodb_backup_key = SecretStr(saved_mongo_key)
            protection = getattr(runtime, "backup", None)
            if protection is not None and hasattr(protection, "settings"):
                protection.settings.mongodb_backup_key = settings.mongodb_backup_key
        self._task = None
        self._wake = asyncio.Event()
        self._operation = asyncio.Lock()
        self.control = asyncio.Lock()
        self._closed = False

    @property
    def blocked(self):
        return bool(self.journal.get("recovery") or self.journal.get("activation"))

    def status(self):
        config = self.journal.get("config", {})
        state = self.journal.get("status", {})
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
            "report": self.journal.get("report", []),
            "restore_point": self.journal.get("restore_point"),
        }
        result["pending_revision"] = max(0, revision - result["protected_revision"])
        result["backup_file_available"] = self.backup_file() is not None
        result["file_restore_allowed"] = not self.settings.demo_mode
        candidate = self.journal.get("candidate")
        if candidate:
            catalog = candidate["catalog"]
            files = list(files_in(catalog))
            result["recovery_candidate"] = {
                "revision": catalog["revision"],
                "channel": catalog["storage_channel_id"],
                "files": len(files),
                "bytes": sum(f["size"] for f in files),
                "complete": bool(candidate.get("complete")),
                "has_database": "database" in candidate,
                "source_is_backup": bool(candidate.get("source_is_backup")),
            }
        return result

    def update(self, **values):
        self.journal.set("status", self.journal.get("status", {}) | values)

    def report(self, level, message):
        rows = self.journal.get("report", [])
        self.journal.set(
            "report",
            [*rows, {"at": now(), "level": level, "message": message}][-100:],
        )

    def key(self):
        import re

        from pymongo.errors import PyMongoError

        from tgdrive.storage.mongo import DatabaseUnavailable

        with self.journal.lock:
            value = self.journal.get("key")
            if value is not None and (
                not isinstance(value, str)
                or re.fullmatch(r"[a-fA-F0-9]{64}", value) is None
            ):
                raise SettingsProblem(
                    "The local recovery key is invalid. "
                    "Preserve it and repair recovery settings.",
                    status=409,
                )
            persisted = getattr(self, "_persisted_key", None)
            if (
                getattr(self, "_key_persisted", False)
                and value is not None
                and value.lower() == persisted
            ):
                return persisted
            backend = getattr(self.runtime, "mongo", None)
            if backend is None:
                if value is None:
                    configured = self.settings.mongodb_backup_key
                    value = (
                        configured.get_secret_value()
                        if configured
                        else secrets.token_hex(32)
                    )
                    self.journal.set("key", value)
                return value
            try:
                row = backend.db.settings.find_one({"_id": "recovery-key"})
                if row is None:
                    configured = self.settings.mongodb_backup_key
                    selected = value or (
                        configured.get_secret_value()
                        if configured
                        else secrets.token_hex(32)
                    )
                    if value is None:
                        latest = backend.db.backups.find_one(
                            {"status": "confirmed", "format": "tgdrive-backup-v2"},
                            sort=[("created_at", -1)],
                        )
                        if (
                            latest
                            and latest.get("key_sha256")
                            != hashlib.sha256(bytes.fromhex(selected)).hexdigest()
                        ):
                            raise SettingsProblem(
                                "The unified recovery key is missing "
                                "from this installation. "
                                "Restore its saved key before creating another backup; "
                                "existing files were preserved.",
                                code="RECOVERY_KEY_MISSING",
                                status=409,
                            )
                    store = getattr(self.runtime, "store", None)
                    backend.write(
                        backend.db.settings,
                        "update_one",
                        {"_id": "recovery-key"},
                        {
                            "$setOnInsert": {
                                "document": {
                                    "key": selected,
                                    "drive_id": store.drive_id if store else None,
                                }
                            }
                        },
                        upsert=True,
                    )
                    row = backend.db.settings.find_one({"_id": "recovery-key"})
                document = row.get("document") if isinstance(row, dict) else None
                if (
                    not isinstance(document, dict)
                    or set(document) != {"key", "drive_id"}
                    or not isinstance(document["key"], str)
                    or re.fullmatch(r"[a-fA-F0-9]{64}", document["key"]) is None
                    or (
                        document["drive_id"] is not None
                        and not isinstance(document["drive_id"], str)
                    )
                ):
                    raise SettingsProblem(
                        "The saved recovery key is invalid. "
                        "Preserve it and repair recovery settings.",
                        status=409,
                    )
                canonical = document["key"].lower()
                store = getattr(self.runtime, "store", None)
                if (value is not None and value.lower() != canonical) or (
                    store is not None
                    and document["drive_id"] not in {None, store.drive_id}
                ):
                    raise SettingsProblem(
                        "The local and database recovery keys disagree. "
                        "Preserve both and restore the matching recovery key "
                        "before continuing.",
                        code="RECOVERY_KEY_CONFLICT",
                        status=409,
                    )
                self.journal.set("key", canonical)
                self._key_persisted = True
                self._persisted_key = canonical
                return canonical
            except DatabaseUnavailable, PyMongoError:
                if value is not None and (
                    persisted is None or value.lower() == persisted
                ):
                    return value
                raise

    def backup_file(self):
        """Return only a confirmed local file; status never initiates a download."""
        import re

        choices = []
        saved = self.journal.get("backup_file")
        if saved and re.fullmatch(r"[a-f0-9]{64}", saved.get("digest", "")):
            path = self.directory / "replica-bundles" / (saved["digest"] + ".tgdb")
            if path.is_file():
                choices.append(
                    (saved.get("created_at", "1970-01-01T00:00:00+00:00"), path)
                )
        service = getattr(self.runtime, "backup", None)
        latest = getattr(service, "latest_history", lambda: [])()
        if latest and latest[0].get("status") == "confirmed":
            identity = latest[0].get("id", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
                path = (
                    self.settings.resolved_cache_dir
                    / "mongo-backups"
                    / (identity + ".tgdb")
                )
                if path.is_file():
                    choices.append((latest[0]["created_at"], path))
        return (
            max(choices, key=lambda choice: datetime.fromisoformat(choice[0]))[1]
            if choices
            else None
        )

    def recovery_file(self):
        path = self.backup_file()
        if path is None:
            raise SettingsProblem(
                "Create and confirm a backup before downloading its recovery file.",
                status=404,
            )
        content = read_private(path)
        saved = self.journal.get("backup_file", {})
        if path.parent == self.directory / "replica-bundles":
            expected = saved.get("sha256")
        else:
            expected = self.runtime.backup.latest_history()[0].get("sha256")
        if not expected or hashlib.sha256(content).hexdigest() != expected:
            raise SettingsProblem(
                "The saved recovery file changed. "
                "Create a fresh confirmed backup before downloading.",
                status=409,
            )
        return content

    async def configure(self, channel, token, independent):
        if not independent or channel == self.settings.storage_channel or channel >= 0:
            raise SettingsProblem(
                "Use two different private channels and confirm independent "
                "owner accounts."
            )
        if self.blocked:
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
            point = self.journal.get("restore_point", {})
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
            self.journal.set("config", config)
            from tgdrive.replication.replica_bootstrap import save_bootstrap

            await asyncio.to_thread(save_bootstrap, self.settings)
            if (
                getattr(self.runtime, "mongo", None) is not None
                and self.settings.mongodb_backup_key is None
            ):
                mongo_key = secrets.token_hex(32)
                self.journal.set("mongodb_key", mongo_key)
                self.settings.mongodb_backup_key = SecretStr(mongo_key)
                self.runtime.backup.settings.mongodb_backup_key = SecretStr(mongo_key)
            self.journal.set("job", None)
            self.key()
            self.update(
                phase="idle", error=None, protected_revision=0, last_success_at=None
            )
            self.report(
                "info",
                "Private channels and backup bot verified. "
                "Initial synchronization scheduled.",
            )
            self.start()
            self._wake.set()
        finally:
            await transport.close()
        return self.status()

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
        self.journal.close()

    async def action(self, action):
        config = self.journal.get("config", {})
        if action in {"pause", "disable"}:
            if self.journal.get("activation"):
                raise SettingsProblem(
                    "Restart to apply the confirmed recovery first.", status=409
                )
            self.update(phase="paused" if action == "pause" else "disabled")
            if action == "disable":
                config["enabled"] = False
                self.journal.set("config", config)
                self.journal.set("recovery", None)
                self.journal.set("candidate", None)
            self.report(
                "info",
                "Operation paused. Confirmed source and backup messages are retained.",
            )
        elif action in {"resume", "retry", "sync"}:
            error = self.journal.get("status", {}).get("error")
            if error and error.get("retry_at", 0) > time.time():
                self.update(phase="waiting")
            else:
                self.update(phase="idle", error=None)
                if action == "sync" or error:
                    self.update(checked_at=0)
            if config:
                config["enabled"] = True
                self.journal.set("config", config)
            self.start()
            self._wake.set()
        else:
            raise SettingsProblem("Unknown backup action")
        return self.status()

    async def _loop(self):
        while not self._closed:
            state = self.journal.get("status", {})
            config = self.journal.get("config", {})
            retry_at = (state.get("error") or {}).get("retry_at", 0)
            recovery = self.journal.get("recovery")
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
                    state = self.journal.get("status", {})
                    config = self.journal.get("config", {})
                    recovery = self.journal.get("recovery")
                    if (
                        state.get("phase")
                        in {
                            "paused",
                            "disabled",
                            "error",
                            "ready_to_activate",
                            "restart_required",
                        }
                        or (state.get("error") or {}).get("retry_at", 0) > time.time()
                        or not (recovery or config.get("enabled"))
                    ):
                        continue
                    try:
                        if recovery:
                            await self._recover_copy(recovery)
                        elif getattr(self.runtime, "store", None) is not None:
                            # Coalesce busy upload queues without starving protection.
                            changed = (
                                state.get("protected_revision")
                                != self.runtime.store.revision
                            )
                            if (
                                self.journal.get("job") is not None
                                or state.get("error")
                                or not changed
                                or time.time()
                                >= state.get("checked_at", 0) + BACKUP_BATCH_SECONDS
                            ):
                                await self._sync(config)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        self._record_failure(error)
            self._wake.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=10)

    def _record_failure(self, error):
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
        from tgdrive.recovery.recovery_file import capture_database, snapshot_catalog
        from tgdrive.replication.replica_settings import credentials_from_snapshot

        database = capture_database(self.runtime.mongo)
        credentials = credentials_from_snapshot(self.settings, database)
        return {
            "catalog": snapshot_catalog(database),
            "credentials": credentials,
            "database": database,
        }

    async def _copy_catalog(self, transport, catalog, destination):
        from tgdrive.replication.replica_transport import MAX_VERIFY_BATCH

        source = catalog["storage_channel_id"]
        files = list(files_in(catalog))
        total_parts = sum(len(parts_in(f)) for f in files)
        self.update(
            total_files=len(files),
            total_parts=total_parts,
            total_bytes=sum(part["size"] for f in files for part in parts_in(f)),
            done_files=0,
            done_parts=0,
            done_bytes=0,
        )
        mapping = {}
        done_parts = done_bytes = 0
        # A resumed job must still recheck every journaled document. Batch those
        # reads so each cooldown does not trigger two requests per old part.
        verified = {}
        verify_many = getattr(transport, "verify_many", None)
        if callable(verify_many):
            batch = []

            async def check_batch():
                if not batch:
                    return
                self.update(current_file="Checking previously copied parts")
                await verify_many(
                    destination, [(message, size) for _identity, message, size in batch]
                )
                verified.update(
                    (identity, message) for identity, message, _size in batch
                )
                batch.clear()

            for node in files:
                for part in parts_in(node):
                    if self.journal.get("status", {}).get("phase") in {
                        "paused",
                        "disabled",
                    }:
                        return None
                    identity = (
                        f"{catalog['drive_id']}:{source}:{destination}:"
                        f"{part['message_id']}:{part['size']}"
                    )
                    _random_id, message = self.journal.intent(identity, part["size"])
                    if message is not None:
                        batch.append((identity, message, part["size"]))
                        if len(batch) == MAX_VERIFY_BATCH:
                            await check_batch()
            await check_batch()
        for count, node in enumerate(files):
            for part in parts_in(node):
                if self.journal.get("status", {}).get("phase") in {
                    "paused",
                    "disabled",
                }:
                    return None
                self.update(current_file=node["name"])
                identity = (
                    f"{catalog['drive_id']}:{source}:{destination}:"
                    f"{part['message_id']}:{part['size']}"
                )
                random_id, message = self.journal.intent(identity, part["size"])
                if message is None:
                    message = await transport.copy(
                        source,
                        part["message_id"],
                        destination,
                        random_id=random_id,
                        expected_size=part["size"],
                    )
                    self.journal.confirm(identity, message)
                elif verified.get(identity) != message:
                    await transport.verify(destination, message, part["size"])
                mapping[str(part["message_id"])] = message
                done_parts += 1
                done_bytes += part["size"]
                self.update(done_parts=done_parts, done_bytes=done_bytes)
                await asyncio.sleep(0)
            self.update(done_files=count + 1)
        return remap_catalog(catalog, destination, mapping)

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
    ):
        previous_file = self.journal.get("backup_file")
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
        ids = []
        for channel, pin in [(target, True), (source, pin_source)]:
            identity = f"bundle:{channel}:{ciphertext_digest}"
            random_id, message = self.journal.intent(identity, path.stat().st_size)
            if message is None:
                message = await transport.publish(
                    channel,
                    path,
                    random_id=random_id,
                    caption="TGDrive encrypted recovery backup"
                    if database is not None
                    else "TGDrive independent recovery bundle v1",
                    pin=pin,
                )
                self.journal.confirm(identity, message)
            else:
                await transport.verify(channel, message, path.stat().st_size)
            ids.append(message)
        self.journal.set_many(
            {
                "restore_point": {
                    "source_channel": source,
                    "source_message_id": ids[1],
                    "backup_channel": target,
                    "backup_message_id": ids[0],
                    "revision": catalog["revision"],
                    "created_at": now(),
                    "bundle_size": path.stat().st_size,
                    "format_version": staged["version"],
                },
                "credentials_digest": hashlib.sha256(
                    json.dumps(credentials, sort_keys=True).encode()
                ).hexdigest(),
                "restore_point_key": key,
                "restore_point_mongodb_key": credentials.get("mongodb_backup_key"),
                "backup_file": {
                    "digest": digest,
                    "key": key,
                    "sha256": ciphertext_digest,
                    "created_at": now(),
                }
                if database is not None
                else None,
            }
        )
        if database is not None and isinstance(previous_file, dict):
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

    async def _sync(self, config, *, force_check=False, force_snapshot=False):
        store = self.runtime.store
        state = self.journal.get("status", {})
        error = state.get("error") or {}
        if error.get("retry_at", 0) > time.time():
            from tgdrive.recovery.backup import BackupError

            raise BackupError(
                "Telegram requested a wait before backup resumes.",
                retry_after=error["retry_at"] - time.time(),
            )
        current_credentials = await asyncio.to_thread(self._credentials)
        if self.journal.get("status", {}).get("phase") in {"paused", "disabled"}:
            return
        credentials_digest = hashlib.sha256(
            json.dumps(current_credentials, sort_keys=True).encode()
        ).hexdigest()
        same_credentials = self.journal.get("credentials_digest") == credentials_digest
        point = self.journal.get("restore_point", {})
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
                self.update(phase="protected", current_file="", error=None)
            return
        job = self.journal.get("job")
        if (
            job is not None
            and getattr(self.runtime, "mongo", None) is not None
            and job["credentials"].get("mongodb_backup_key")
            != current_credentials.get("mongodb_backup_key")
        ):
            raise SettingsProblem(
                "The pending backup captured a different MongoDB recovery key. "
                "Restore that key to resume, or verify Backup settings again "
                "to start a new snapshot with the current key. Existing restore "
                "points and confirmed file copies are retained.",
                code="REPLICA_MONGODB_KEY_CHANGED",
                status=409,
            )
        if job is None and not check_only:
            job = await asyncio.to_thread(self._capture)
            catalog = job["catalog"]
            if self.journal.get("status", {}).get("phase") in {"paused", "disabled"}:
                return
            if catalog.get("storage_channel_id") not in {
                None,
                self.settings.storage_channel,
            }:
                raise ValueError("Catalog and source channel do not match")
            catalog["storage_channel_id"] = self.settings.storage_channel
            validate_catalog(catalog)
            self.journal.set("job", job)
        if check_only:
            # The recorded checkpoint is still confirmed while its health is
            # checked; do not present an unchanged drive as copying files again.
            self.journal.set("job", None)
            self.update(phase="protected", current_file="", error=None)
        else:
            self.update(phase="syncing", error=None)
        transport = self.factory(self.settings, config["token"])
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
                phase = self.journal.get("status", {}).get("phase")
                self.update(
                    phase=phase if phase in {"paused", "disabled"} else "protected",
                    error=None,
                    checked_at=time.time(),
                )
                self._request_pacer.reset()
                self.journal.set("job", None)
                return
            catalog = job["catalog"]
            replica = await self._copy_catalog(transport, catalog, config["channel"])
            if replica is None:
                return
            if self.journal.get("status", {}).get("phase") in {"paused", "disabled"}:
                return
            archive = None
            if "database" not in job:
                # Finish already journaled v1 work with its original format and key.
                from tgdrive.replication.replica_mongo import mirror_mongodb

                archive = await mirror_mongodb(
                    self,
                    transport,
                    source_channel=self.settings.storage_channel,
                    destination_channel=config["channel"],
                    revision=catalog["revision"],
                    settings_fingerprint=hashlib.sha256(
                        json.dumps(job["credentials"], sort_keys=True).encode()
                    ).hexdigest(),
                )
            if self.journal.get("status", {}).get("phase") in {"paused", "disabled"}:
                return
            credentials = dict(job["credentials"])
            if archive is not None:
                credentials["mongodb_archive"] = archive
                self.update(database_archive=archive)
            await self._publish(
                transport,
                self.settings.storage_channel,
                config["channel"],
                catalog,
                replica,
                credentials,
                self.key(),
                database=job.get("database"),
                recovery_token=config["token"],
            )
            self.journal.set(
                "credentials_digest",
                hashlib.sha256(
                    json.dumps(job["credentials"], sort_keys=True).encode()
                ).hexdigest(),
            )
            phase = self.journal.get("status", {}).get("phase")
            self.update(
                phase=phase if phase in {"paused", "disabled"} else "protected",
                protected_revision=catalog["revision"],
                last_success_at=now(),
                current_file="",
                error=None,
                checked_at=time.time(),
            )
            self.report(
                "info",
                "Restore point confirmed in both channels "
                f"at catalog revision {catalog['revision']}.",
            )
            self._request_pacer.reset()
            self.journal.set("job", None)
        finally:
            await transport.close()

    async def inspect_recovery(self, source, token, key, message_id=None):
        transport = self.factory(self.settings, token)
        path = self.directory / (".replica-inspect-" + secrets.token_hex(12))
        try:
            await transport.start()
            await transport.inspect(source)
            try:
                inspected_id = await transport.download(source, message_id, path)
            except Exception as error:
                if getattr(error, "code", None) != "REPLICA_DOCUMENT_TYPE":
                    raise
                path.unlink(missing_ok=True)
                inspected_id = await transport.download_catalog(
                    source, path, message_id
                )
                from tgdrive.recovery.backup import decode_catalog_snapshot

                catalog = decode_catalog_snapshot(read_private(path))
                validate_catalog(catalog)
                if catalog.get("storage_channel_id") != source:
                    raise ValueError(
                        "Catalog checkpoint belongs to another channel"
                    ) from None
                key = key or self.key()
                if len(bytes.fromhex(key.strip())) != 32:
                    raise ValueError(
                        "Supply a 64-character hexadecimal recovery key"
                    ) from None
                bundle = {"credentials": await asyncio.to_thread(self._credentials)}
                self.report(
                    "warning",
                    "A catalog-only checkpoint was selected. Keep the original "
                    "SESSION_SECRET to preserve public share links. Application "
                    "preferences and unfinished transfers are not included "
                    "in this catalog.",
                )
            else:
                if not key:
                    raise ValueError(
                        "Supply the offline key for this encrypted recovery bundle"
                    )
                bundle = decode_bundle(read_private(path), key)
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
            current_store = getattr(self.runtime, "store", None)
            if (
                current_store is not None
                and current_store.drive_id != catalog["drive_id"]
            ):
                raise ValueError(
                    "The recovery point belongs to another drive. Preserve this "
                    "installation and recover that drive separately."
                )
            self.journal.set(
                "candidate",
                {
                    "catalog": catalog,
                    "credentials": bundle["credentials"],
                    "token": token,
                    "key": key.strip(),
                    "complete": False,
                    "inspection": {
                        "source_channel": source,
                        "source_message_id": inspected_id,
                        "revision": catalog["revision"],
                        "created_at": now(),
                    },
                },
            )
            if "database" in bundle:
                candidate = self.journal.get("candidate")
                candidate["database"] = bundle["database"]
                candidate["source_is_backup"] = (
                    source != bundle["catalog"]["storage_channel_id"]
                )
                self.journal.set("candidate", candidate)
            self.update(phase="paused", error=None)
            self.report(
                "info",
                "Authenticated restore point inspected. "
                "No live files or configuration changed.",
            )
        finally:
            path.unlink(missing_ok=True)
            await transport.close()
        return self.status()

    def _recovery_transport(self, token, credentials=None):
        updates = {"bot_tokens_csv": SecretStr("")}
        if credentials:
            updates.update(
                api_id=credentials["api_id"],
                api_hash=SecretStr(credentials["api_hash"]),
            )
        return self.factory(self.settings.model_copy(update=updates), token)

    async def inspect_file(self, path, key, source=None, token=None):
        """Authenticate a saved file before probing its recorded channels."""
        from tgdrive.replication.replica_transport import ReplicaTransportError

        retry_at = (self.journal.get("status", {}).get("error") or {}).get(
            "retry_at", 0
        )
        if retry_at > time.time():
            raise SettingsProblem(
                "Telegram is waiting before recovery can retry. "
                "Wait for the displayed retry time.",
                status=409,
            )
        bundle = await asyncio.to_thread(lambda: decode_bundle(read_private(path), key))
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
        current = getattr(self.runtime, "store", None)
        if current is not None and current.drive_id != bundle["catalog"]["drive_id"]:
            if current.export()["root"]["children"]:
                raise SettingsProblem(
                    "This file belongs to another drive. "
                    "Restore into an empty installation.",
                    status=409,
                )
            from tgdrive.recovery.mongo_backup import check_restore_destination

            await asyncio.to_thread(
                check_restore_destination, self.runtime.mongo, allow_pristine=True
            )
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
                        raise self._record_failure(error) from None
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
        if "database" in bundle:
            candidate["database"] = bundle["database"]
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
                self._remember_mapping(catalog, backup)
            except ReplicaTransportError as error:
                if error.retry_after:
                    raise self._record_failure(error) from None
            finally:
                await transport.close()
        self.journal.set("candidate", candidate)
        self.update(phase="paused", error=None)
        self.report(
            "info",
            "Recovery file opened. Review the restore destination; "
            "live data is unchanged.",
        )
        return self.status()

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
        candidate = self.journal.get("candidate")
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
                        raise self._record_failure(error) from None
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
        self.journal.set(
            "recovery",
            {
                "target": target,
                "target_token": target_token,
                "catalog_only": catalog_only,
            },
        )
        self.update(phase="recovering", error=None)
        self.start()
        self._wake.set()
        return self.status()

    async def _recover_copy(self, recovery):
        if (
            self.journal.get("status", {}).get("phase") in {"paused", "disabled"}
            or self.journal.get("recovery") is None
        ):
            return
        candidate = self.journal.get("candidate")
        source = candidate["catalog"]["storage_channel_id"]
        transport = self._recovery_transport(
            candidate["token"], candidate["credentials"]
        )
        try:
            await transport.start()
            await transport.inspect(source)
            await transport.inspect(recovery["target"])
            target_pin = await transport.pinned_message(recovery["target"])
            if (
                self.journal.get("status", {}).get("phase") in {"paused", "disabled"}
                or self.journal.get("recovery") is None
            ):
                return
            reservation = {
                "drive_id": candidate["catalog"]["drive_id"],
                "source": source,
                "target": recovery["target"],
            }
            if (
                not recovery.get("catalog_only")
                and self.journal.get("recovery_destination") != reservation
            ):
                if target_pin is not None:
                    raise SettingsProblem(
                        "The replacement channel already has a pinned message. "
                        "Use a new dedicated private channel.",
                        status=409,
                    )
                self.journal.set("recovery_destination", reservation)
            # Separately authenticate the replacement main bot before copying.
            probe = self._recovery_transport(
                recovery["target_token"], candidate["credentials"]
            )
            try:
                await probe.start()
                await probe.inspect(recovery["target"])
            finally:
                await probe.close()
            if (
                self.journal.get("status", {}).get("phase") in {"paused", "disabled"}
                or self.journal.get("recovery") is None
            ):
                return
            self.update(phase="recovering", error=None)
            if recovery.get("catalog_only"):
                for node in files_in(candidate["catalog"]):
                    for part in parts_in(node):
                        if (
                            self.journal.get("status", {}).get("phase")
                            in {"paused", "disabled"}
                            or self.journal.get("recovery") is None
                        ):
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
            if (
                self.journal.get("status", {}).get("phase") in {"paused", "disabled"}
                or self.journal.get("recovery") is None
            ):
                return
            if (
                target_pin is not None
                and not recovery.get("catalog_only")
                and not self.journal.owns_publication(recovery["target"], target_pin)
            ):
                await self._verify_recovery_pin(
                    transport, recovery["target"], target_pin, candidate, restored
                )
            credentials = dict(
                candidate["credentials"], bot_tokens=[recovery["target_token"]]
            )
            database = candidate.get("database")
            if database is not None:
                credentials["mongodb_backup_key"] = candidate["key"]
            archive = credentials.get("mongodb_archive")
            if archive and not recovery.get("catalog_only"):
                source_id = (
                    archive["replica_message_id"]
                    if archive["replica_channel"] == source
                    else archive["source_message_id"]
                )
                if source not in {
                    archive["replica_channel"],
                    archive["source_channel"],
                }:
                    raise ValueError(
                        "The database archive is not bound to the surviving channel"
                    )
                identity = (
                    f"recover-mongo:{source}:{source_id}:{recovery['target']}:"
                    f"{archive['sha256']}"
                )
                random_id, receipt = self.journal.intent(identity, archive["size"])
                if receipt is None:
                    receipt = await transport.copy(
                        source,
                        source_id,
                        recovery["target"],
                        random_id=random_id,
                        expected_size=archive["size"],
                    )
                    self.journal.confirm(identity, receipt)
                else:
                    await transport.verify(recovery["target"], receipt, archive["size"])
                credentials["mongodb_archive"] = dict(
                    archive,
                    source_channel=source,
                    source_message_id=source_id,
                    replica_channel=recovery["target"],
                    replica_message_id=receipt,
                )
            if (
                self.journal.get("status", {}).get("phase") in {"paused", "disabled"}
                or self.journal.get("recovery") is None
            ):
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
                self._remember_mapping(restored, candidate["catalog"])
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
            if (
                self.journal.get("status", {}).get("phase") in {"paused", "disabled"}
                or self.journal.get("recovery") is None
            ):
                return
            if database is None:
                await self._publish_primary_catalog(transport, restored)
            if (
                self.journal.get("status", {}).get("phase") in {"paused", "disabled"}
                or self.journal.get("recovery") is None
            ):
                return
            if recovery.get("catalog_only"):
                self.journal.set_many(
                    {
                        "restore_point": candidate["inspection"],
                        "restore_point_key": candidate["key"],
                        "restore_point_mongodb_key": credentials.get(
                            "mongodb_backup_key"
                        ),
                        "credentials_digest": hashlib.sha256(
                            json.dumps(credentials, sort_keys=True).encode()
                        ).hexdigest(),
                    }
                )
            candidate.update(catalog=restored, credentials=credentials, complete=True)
            if (
                candidate.get("backup_config", {}).get("channel")
                == restored["storage_channel_id"]
            ):
                candidate.pop("backup_config")
            self.journal.set("candidate", candidate)
            self.update(phase="ready_to_activate", current_file="", error=None)
            self.report(
                "info",
                "All destination files and recovery metadata confirmed. "
                "Review and activate to switch TGDrive.",
            )
        finally:
            await transport.close()

    def activate(self):
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

    def recovery_kit(self):
        path = self.backup_file()
        if path is not None:
            saved = self.journal.get("backup_file", {})
            if path.parent == self.directory / "replica-bundles":
                key = saved["key"]
            else:
                latest = self.runtime.backup.latest_history()[0]
                key = (
                    self.key()
                    if latest.get("format") == "tgdrive-backup-v2"
                    else (
                        self.settings.mongodb_backup_key.get_secret_value()
                        if self.settings.mongodb_backup_key
                        else None
                    )
                )
                if (
                    key is None
                    or latest.get("key_sha256")
                    != hashlib.sha256(bytes.fromhex(key)).hexdigest()
                ):
                    raise SettingsProblem(
                        "The saved file uses an older recovery key. "
                        "Preserve its matching key or create a new confirmed backup.",
                        status=409,
                    )
            return {
                "format": "tgdrive-recovery-key-v2",
                "key": key,
                "instructions": (
                    "Keep this recovery key outside the server, separately "
                    "from your recovery file. Your login password does not replace it."
                ),
            }
        candidate = self.journal.get("candidate")
        if candidate and not candidate.get("complete"):
            key, point = candidate["key"], candidate.get("inspection")
            mongodb_key = candidate["credentials"].get("mongodb_backup_key")
        else:
            key = self.journal.get("restore_point_key") or self.key()
            point = self.journal.get("restore_point")
            mongodb_key = self.journal.get("restore_point_mongodb_key")
        return {
            "format": "tgdrive-replica-key-v1",
            "key": key,
            "restore_point": point,
            "next_backup_key": self.key(),
            "mongodb_backup_key": mongodb_key,
            "next_mongodb_backup_key": (
                self.settings.mongodb_backup_key.get_secret_value()
                if self.settings.mongodb_backup_key
                else None
            ),
            "instructions": (
                "Keep this file outside Telegram and the TGDrive server. "
                "Paste key into "
                "web recovery for the recorded restore point. Save an updated file "
                "after a new confirmed backup. Never share it."
            ),
        }

    async def _verify_recovery_pin(
        self, transport, channel, message_id, candidate, restored
    ):
        path = self.directory / (".replica-pin-" + secrets.token_hex(12))
        try:
            try:
                await transport.download(channel, message_id, path)
            except Exception as error:
                if getattr(error, "code", None) != "REPLICA_DOCUMENT_TYPE":
                    raise
                path.unlink(missing_ok=True)
                await transport.download_catalog(channel, path, message_id)
                from tgdrive.recovery.backup import decode_catalog_snapshot

                matched = decode_catalog_snapshot(read_private(path)) == restored
            else:
                bundle = decode_bundle(read_private(path), candidate["key"])
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

    async def _publish_primary_catalog(self, transport, catalog):
        raw = DriveStore._json_bytes(catalog)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.directory / "replica-bundles" / (digest + ".json")
        if not path.exists():
            atomic_private(path, raw)
        identity = f"primary-catalog:{catalog['storage_channel_id']}:{digest}"
        random_id, message = self.journal.intent(identity, len(raw))
        if message is None:
            caption = (
                f"TGDrive checkpoint v1\nDrive: {catalog['drive_id']}"
                f"\nChannel: {catalog['storage_channel_id']}"
                f"\nRevision: {catalog['revision']}\nSHA256: {digest}"
            )
            message = await transport.publish_catalog(
                catalog["storage_channel_id"],
                path,
                random_id=random_id,
                caption=caption,
                pin=True,
            )
            self.journal.confirm(identity, message)
        else:
            await transport.verify(catalog["storage_channel_id"], message, len(raw))

    def _mutation_config(self):
        if self.blocked:
            raise SettingsProblem(
                "Channel recovery is in progress. Resume it or disable the "
                "unfinished recovery before changing files.",
                status=409,
            )
        config = self.journal.get("config", {})
        if not config.get("enabled"):
            return None
        if self.journal.get("status", {}).get("phase") in {"paused", "disabled"}:
            raise SettingsProblem(
                "Resume independent backup before Telegram mutations, "
                "or explicitly disable backup protection.",
                status=409,
            )
        return config

    async def before_mutation(self):
        if self._mutation_config() is None:
            return
        async with self._operation:
            config = self._mutation_config()
            if config is None:
                return
            required = self.runtime.store.revision
            for _ in range(2):
                saved_retry_at = (
                    self.journal.get("status", {}).get("error") or {}
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
                    raise self._record_failure(error) from None
                if self._mutation_config() is None:
                    return
                if (
                    self.journal.get("status", {}).get("protected_revision", 0)
                    >= required
                ):
                    return
        raise SettingsProblem(
            "The required backup restore point is not confirmed. "
            "Check backup status and retry.",
            status=503,
        )
