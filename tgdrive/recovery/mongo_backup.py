"""Encrypted full MongoDB snapshots, independent of normal transfer completion."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import math
import os
import re
import secrets
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

from tgdrive.recovery.backup import BackupError, RecoveryConflict
from tgdrive.recovery.recovery_file import write_recovery_snapshot
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import ROOT_ID, SCHEMA_VERSION
from tgdrive.transfers.tasks import _journalled

logger = logging.getLogger(__name__)

COLLECTIONS = (
    "catalog",
    "nodes",
    "shares",
    "operations",
    "deletions",
    "preparations",
    "settings",
    "identities",
    "sessions",
    "jobs",
    "receipts",
    "bot_folders",
    "backups",
)
_HISTORY_UNAVAILABLE = "Encrypted Telegram backup history is temporarily unavailable."
_PIN_UNVERIFIED = (
    "The previous Telegram backup pin could not be verified. Its recovery file "
    "is retained; a new backup will be scheduled."
)
_worker = _journalled(asyncio.to_thread)


def file_checksum(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _pristine_recovery_key(row, drive_id):
    document = row.get("document")
    return (
        set(row) == {"_id", "document"}
        and row["_id"] == "recovery-key"
        and isinstance(document, dict)
        and set(document) == {"key", "drive_id"}
        and isinstance(document["key"], str)
        and re.fullmatch(r"[a-fA-F0-9]{64}", document["key"]) is not None
        and (document["drive_id"] is None or document["drive_id"] == drive_id)
    )


def check_restore_destination(backend, *, allow_pristine=False, session=None):
    """Accept an empty database or an unused automatically initialized drive."""
    catalogs = list(backend.db.catalog.find({}, session=session))
    if not allow_pristine:
        if any(backend.db[name].find_one({}, session=session) for name in COLLECTIONS):
            raise FileExistsError("Restore requires an empty MongoDB database")
        return
    if not catalogs:
        # Signing into a recovery-only shell creates sessions and may refresh
        # account names; neither is an existing drive to overwrite.
        if any(
            backend.db[name].find_one({}, session=session)
            for name in COLLECTIONS
            if name not in {"sessions", "identities", "settings"}
        ):
            raise FileExistsError(
                "This database contains drive records. Restore into an empty database."
            )
        if any(
            not _pristine_recovery_key(row, None) and row.get("_id") != "encryption"
            for row in backend.db.settings.find({}, session=session)
        ):
            raise FileExistsError("The destination contains saved settings changes")
        return
    if len(catalogs) != 1:
        raise FileExistsError("The destination is not a new, unused drive")
    catalog = catalogs[0]
    # create() starts at revision 1; initial channel binding is the sole commit.
    if (
        catalog.get("_id") != "catalog"
        or catalog.get("schema_version") != SCHEMA_VERSION
        or catalog.get("revision") != 2
        or catalog.get("generation") != 1
    ):
        raise FileExistsError("Restore requires a new, unused drive")
    nodes = list(backend.db.nodes.find({}, session=session))
    if (
        len(nodes) != 1
        or nodes[0].get("_id") != ROOT_ID
        or nodes[0].get("parent_id") is not None
        or nodes[0].get("kind") != "folder"
        or nodes[0].get("name") != "/"
        or nodes[0].get("trashed") is not False
    ):
        raise FileExistsError("The destination already contains files or folders")
    for name in COLLECTIONS:
        if name in {
            "catalog",
            "nodes",
            "settings",
            "sessions",
            "identities",
            "backups",
        }:
            continue
        if backend.db[name].find_one({}, session=session):
            raise FileExistsError("The destination contains drive work or shared links")
    for row in backend.db.settings.find({}, session=session):
        if row.get("_id") in {"bootstrap", "encryption"}:
            continue
        if row.get("_id") == "telegram" and row.get("revision") == 1:
            continue
        if _pristine_recovery_key(row, catalog.get("drive_id")):
            continue
        raise FileExistsError("The destination contains saved settings changes")


class MongoProtection:
    """Cloud commits protect normal work; Telegram copies run independently."""

    def __init__(self, settings, pool, backend):
        self.settings, self.pool, self.backend = settings, pool, backend
        self._stop = asyncio.Event()
        self._task = None
        self._lock = asyncio.Lock()
        self._last_error = None
        self._last_success_at = None
        self._latest_backup = None
        self.recovery_required = False
        self.recovery_error = None

    async def restore(self):
        store = await asyncio.to_thread(MongoStore, self.backend)
        self._latest_backup = None
        self._last_success_at = None
        self._last_error = None
        try:
            latest = await asyncio.to_thread(self.history, limit=1)
            if latest:
                row = latest[0]
                if (
                    row.get("status") == "confirmed"
                    and row.get("format") == "tgdrive-backup-v2"
                    and not row.get("pin_verified_at")
                ):
                    await self._verify_historic_pin(row)
                self._observe_backup(row)
                if row.get("status") == "unverified":
                    self._last_error = _PIN_UNVERIFIED
        except Exception:
            self._last_error = _HISTORY_UNAVAILABLE
        return store

    async def _verify_historic_pin(self, row):
        """Recheck old publication claims without replacing any channel pin."""
        try:
            message = row.get("message_id")
            if (
                type(message) is not int
                or message <= 0
                or row.get("channel_id") != self.settings.storage_channel
            ):
                raise BackupError("The historical backup has no valid pin receipt")
            async with asyncio.timeout(10):
                async with self.pool.lease(client_name=row.get("sender")) as client:
                    if await client.get_pinned_message_id(row["channel_id"]) != message:
                        raise BackupError(
                            "The historical backup is not the current pin"
                        )
        except Exception:
            values = {"status": "unverified"}
        else:
            values = {"pin_verified_at": datetime.now(UTC).isoformat()}
        await _worker(
            self.backend.write,
            self.backend.db.backups,
            "update_one",
            {"_id": row["id"]},
            {"$set": values},
        )
        row.update(values)

    def _observe_backup(self, row):
        if self._latest_backup is None or row.get(
            "created_at", ""
        ) >= self._latest_backup.get("created_at", ""):
            self._latest_backup = {
                key: value
                for key, value in row.items()
                if key not in {"_id", "local_path"}
            }
        if row.get("status") == "confirmed" and (
            row.get("format") != "tgdrive-backup-v2" or row.get("pin_verified_at")
        ):
            confirmed_at = row.get("confirmed_at")
            if confirmed_at and (
                self._last_success_at is None or confirmed_at > self._last_success_at
            ):
                self._last_success_at = confirmed_at

    def latest_history(self):
        """Read the latest observed publication without querying MongoDB."""
        if self._latest_backup is None:
            return []
        return copy.deepcopy([self._latest_backup])

    def local_archives(self):
        """Iterate retained download candidates without hiding an older local file."""
        return self.backend.db.backups.find(
            {"status": {"$in": ["confirmed", "unverified"]}},
            projection={"_id": 0, "id": 1, "created_at": 1, "sha256": 1, "status": 1},
        ).sort("created_at", -1)

    def check_local_binding(self, store):
        if store.storage_channel_id not in (None, self.settings.storage_channel):
            raise RecoveryConflict(
                "This MongoDB drive belongs to a different Telegram channel"
            )

    def check_upload_allowed(self, store):
        self.backend.check_owner()
        self.check_local_binding(store)

    async def verify_lineage(self, store):
        self.check_upload_allowed(store)

    async def restore_publication_cooldown(self):
        return

    async def ensure_revision(self, store, required_revision):
        self.check_upload_allowed(store)
        if required_revision > store.revision:
            raise BackupError("The catalog revision is not committed")
        return store.revision

    def protection_status(self, store):
        try:
            self.check_upload_allowed(store)
            state, error = "protected", None
        except Exception:
            state, error = "error", "MongoDB connection or ownership needs attention"
        result = {
            "state": state,
            "backend": "mongodb",
            "metadata_revision": store.revision,
            "protected_revision": store.revision if state == "protected" else 0,
            "backup_error": self._last_error,
            "last_success_at": self._last_success_at,
        }
        journal = getattr(getattr(self, "replica", None), "journal", None)
        if journal is not None and journal.get("config", {}).get("enabled"):
            independent = journal.get("status", {})
            result["last_success_at"] = (
                independent.get("last_success_at") or self._last_success_at
            )
            result["backup_error"] = (independent.get("error") or {}).get("detail")
        if error:
            result["error"] = error
        return result

    def history(self, *, limit=None):
        rows = self.backend.db.backups.find(
            {}, projection={"_id": 0, "local_path": 0}
        ).sort("created_at", -1)
        return list(rows.limit(limit or 100))

    async def backup(self, store, *, force=False):
        self.check_upload_allowed(store)
        if store.storage_channel_id is None:
            await asyncio.to_thread(
                store.bind_storage_channel, self.settings.storage_channel
            )
        if force:
            return await self.snapshot_now(store)
        return False

    async def snapshot_now(self, store):
        async with self._lock:
            await self.verify_lineage(store)
            backend = self.backend
            pending = await asyncio.to_thread(
                backend.db.backups.find_one,
                {"status": "pending"},
                sort=[("created_at", 1)],
            )
            try:
                previous_files = await asyncio.to_thread(
                    lambda: list(
                        backend.db.backups.find(
                            {
                                "status": "confirmed",
                                "format": "tgdrive-backup-v2",
                            }
                        )
                    )
                )
            except Exception:
                previous_files = []
                logger.warning(
                    "Could not list older confirmed local recovery files for cleanup"
                )
            from tgdrive.core import encryption

            key = bytes.fromhex(encryption.backup_key())
            if (
                pending is not None
                and pending.get("key_sha256") != hashlib.sha256(key).hexdigest()
            ):
                # A pending file from a different key is abandoned, never resent.
                await asyncio.to_thread(
                    backend.db.backups.update_one,
                    {"_id": pending["_id"]},
                    {"$set": {"status": "superseded"}},
                )
                pending = None
            if pending is None:
                identity = secrets.token_urlsafe(16)
                directory = self.settings.resolved_cache_dir / "mongo-backups"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                path = directory / (identity + ".tgdb")
                export = asyncio.create_task(
                    asyncio.to_thread(
                        write_recovery_snapshot, backend, path, key, self.settings
                    )
                )
                try:
                    revision, counts, digest = await asyncio.shield(export)
                except asyncio.CancelledError:
                    # The unadmitted file still belongs to its worker until it drains.
                    while not export.done():
                        try:
                            await asyncio.shield(export)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if not export.cancelled() and export.exception() is None:
                        path.unlink(missing_ok=True)
                    raise
                row = {
                    "_id": identity,
                    "id": identity,
                    "status": "pending",
                    "created_at": datetime.now(UTC).isoformat(),
                    "revision": revision,
                    "local_path": str(path),
                    "sha256": digest,
                    "random_id": secrets.randbits(63),
                    "sender": None,
                    "message_id": None,
                    "channel_id": self.settings.storage_channel,
                    "retry_at": 0,
                    "counts": counts,
                    "format": "tgdrive-backup-v2",
                    "key_sha256": hashlib.sha256(key).hexdigest(),
                }
                await asyncio.to_thread(backend.db.backups.insert_one, row)
                pending = row
            self._observe_backup(pending)
            if pending["channel_id"] != self.settings.storage_channel:
                raise BackupError("Pending backup belongs to another channel")
            remaining = pending.get("retry_at", 0) - time.time()
            if remaining > 0:
                raise BackupError(
                    "Telegram backup is waiting before retry", retry_after=remaining
                )
            if pending.get("message_id") is None:
                path = Path(pending["local_path"])
                # A fresh host may lack the old temporary file. Never resend
                # changed bytes under an existing random_id.
                if not path.is_file():
                    await asyncio.to_thread(
                        backend.db.backups.update_one,
                        {"_id": pending["_id"]},
                        {"$set": {"status": "interrupted"}},
                    )
                    pending["status"] = "interrupted"
                    self._observe_backup(pending)
                    raise BackupError(
                        "The previous backup file is unavailable; "
                        "a new snapshot will be scheduled"
                    )
                digest = await asyncio.to_thread(file_checksum, path)
                if digest != pending["sha256"]:
                    raise BackupError("The staged backup checksum failed")
            try:
                if pending.get("message_id") is not None:
                    await self._publish(pending, previous_files)
                    return True
                async with self.pool.lease(client_name=pending["sender"]) as client:
                    if pending["sender"] is None:
                        pending["sender"] = client.name
                        await asyncio.to_thread(
                            backend.db.backups.update_one,
                            {"_id": pending["_id"]},
                            {"$set": {"sender": client.name}},
                        )
                        self._observe_backup(pending)
                    message = await client.send_document(
                        self.settings.storage_channel,
                        str(path),
                        file_name="tgdrive-backup.tgdb",
                        random_id=pending["random_id"],
                        caption="TGDrive encrypted recovery file · "
                        + pending["created_at"],
                    )
                    identity = getattr(message, "id", None)
                    if type(identity) is not int or identity <= 0:
                        raise BackupError("Telegram did not confirm the backup")
                    # Verify the exact remote encrypted bytes before recording success.
                    fetched = await client.get_messages(
                        self.settings.storage_channel, message_ids=identity
                    )
                    remote_size = getattr(
                        getattr(fetched, "document", None), "file_size", None
                    )
                    if remote_size != path.stat().st_size:
                        raise BackupError("Telegram backup size verification failed")
                    remote_hash = hashlib.sha256()
                    iterator = client.stream_media(
                        fetched, limit=(remote_size + 1024**2 - 1) // 1024**2, offset=0
                    )
                    try:
                        async for block in iterator:
                            remote_hash.update(block)
                    finally:
                        await iterator.aclose()
                    if remote_hash.hexdigest() != digest:
                        raise BackupError("Telegram backup verification failed")
                # Persist verified-send evidence before pinning. A retry on a new
                # host needs neither another upload nor the old temporary file.
                await asyncio.to_thread(
                    backend.db.backups.update_one,
                    {"_id": pending["_id"]},
                    {"$set": {"message_id": identity}},
                )
                pending["message_id"] = identity
                self._observe_backup(pending)
                await self._publish(pending, previous_files)
                return True
            except Exception as exc:
                seconds = getattr(exc, "retry_after", None)
                if seconds is None:
                    seconds = getattr(exc, "seconds", None)
                if (
                    type(seconds) in (int, float)
                    and math.isfinite(seconds)
                    and seconds > 0
                ):
                    deadline = time.time() + seconds + 0.25
                    await asyncio.to_thread(
                        backend.db.backups.update_one,
                        {"_id": pending["_id"]},
                        {"$set": {"retry_at": deadline}},
                    )
                    pending["retry_at"] = deadline
                    self._observe_backup(pending)
                    await self.pool.defer_channel(deadline)
                self._last_error = (
                    str(exc)
                    if isinstance(exc, BackupError)
                    else ("The encrypted Telegram backup will retry automatically.")
                )
                raise

    async def _publish(self, row, previous_files):
        """Confirm recovery only after the verified ciphertext is discoverable."""
        await self._pin(row, row["message_id"])
        verified_at = datetime.now(UTC).isoformat()
        confirmed = {
            "status": "confirmed",
            "confirmed_at": verified_at,
            "pin_verified_at": verified_at,
        }
        await asyncio.to_thread(
            self.backend.db.backups.update_one,
            {"_id": row["_id"]},
            {"$set": confirmed},
        )
        row.update(confirmed)
        self._observe_backup(row)
        await asyncio.to_thread(self._prune_confirmed_files, previous_files, row["id"])
        self._last_error = None
        logger.info("Drive backup saved and pinned: revision %s", row.get("revision"))

    async def _pin(self, row, message_id):
        """Pin and verify discovery; leave failed publication pending for retry."""
        try:
            async with self.pool.lease(client_name=row["sender"]) as client:
                await client.pin_message(self.settings.storage_channel, message_id)
                if await client.get_pinned_message_id(row["channel_id"]) != message_id:
                    raise BackupError("The backup pin could not be verified")
        except Exception as error:
            self._last_error = (
                "The backup is awaiting its recovery pin. Grant the storage bot "
                "Edit Messages permission; publication will retry automatically."
            )
            raise BackupError(
                self._last_error,
                retry_after=getattr(error, "retry_after", None)
                or getattr(error, "seconds", None),
            ) from error

    def _prune_confirmed_files(self, rows, current_id):
        """Remote copies and pending ciphertext outlive local download retention."""
        directory = self.settings.resolved_cache_dir / "mongo-backups"
        try:
            resolved = directory.resolve()
            descriptor = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                for row in rows:
                    identity = row.get("id")
                    if (
                        not isinstance(identity, str)
                        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity) is None
                        or identity == current_id
                        or row.get("status") != "confirmed"
                        or row.get("format") != "tgdrive-backup-v2"
                    ):
                        continue
                    filename = identity + ".tgdb"
                    path = directory / filename
                    if (
                        row.get("local_path") != str(path)
                        or path.resolve().parent != resolved
                    ):
                        continue
                    try:
                        info = os.stat(
                            filename, dir_fd=descriptor, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(info.st_mode):
                        os.unlink(filename, dir_fd=descriptor)
            finally:
                os.close(descriptor)
        except Exception:
            logger.warning(
                "Could not remove an older confirmed local recovery file; "
                "the new backup remains confirmed"
            )

    def start(self, store):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(store), name="mongodb-backup")
        return self._task

    async def _loop(self, store):
        delay = 30
        observed = self._latest_backup
        try:
            pending = await asyncio.to_thread(
                self.backend.db.backups.find_one, {"status": "pending"}
            )
            latest = await asyncio.to_thread(
                self.backend.db.backups.find_one,
                {"status": "confirmed"},
                sort=[("created_at", -1)],
            )
            if self._last_error == _HISTORY_UNAVAILABLE:
                self._last_error = None
            # Startup reads must not overwrite publication progress made meanwhile.
            if self._latest_backup is observed:
                if latest:
                    self._observe_backup(latest)
                if pending:
                    self._observe_backup(pending)
            if pending:
                delay = max(1, pending.get("retry_at", 0) - time.time())
            elif latest and latest.get("confirmed_at"):
                confirmed = datetime.fromisoformat(latest["confirmed_at"]).timestamp()
                delay = max(
                    1,
                    self.settings.mongodb_backup_seconds
                    - max(0, time.time() - confirmed),
                )
        except Exception:
            delay = 60
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                replica = getattr(self, "replica", None)
                if replica is not None and replica.status().get("enabled"):
                    delay = self.settings.mongodb_backup_seconds
                    continue
                await self.snapshot_now(store)
                delay = self.settings.mongodb_backup_seconds
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._last_error = (
                    str(error)
                    if isinstance(error, BackupError)
                    else ("The encrypted Telegram backup will retry automatically.")
                )
                logger.warning(
                    "Drive backup failed (%s); retrying in 60 seconds",
                    type(error).__name__,
                )
                delay = 60

    async def stop(self, store):
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
