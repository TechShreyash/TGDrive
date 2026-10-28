"""Encrypted full MongoDB snapshots, independent of normal transfer completion."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import os
import re
import secrets
import stat
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path

from bson import json_util
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tgdrive.recovery.backup import BackupError, RecoveryConflict
from tgdrive.recovery.recovery_file import write_recovery_snapshot
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import ROOT_ID, SCHEMA_VERSION

logger = logging.getLogger(__name__)

MAGIC = b"TGDRIVE-MONGO-1\n"
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
MAX_EXPANDED = 1024**3
_HISTORY_UNAVAILABLE = "Encrypted Telegram backup history is temporarily unavailable."


def backup_key(settings):
    value = settings.mongodb_backup_key
    if value is None:
        raise BackupError(
            "Configure MONGODB_BACKUP_KEY before enabling encrypted backups"
        )
    try:
        key = bytes.fromhex(value.get_secret_value())
    except ValueError:
        raise BackupError(
            "MONGODB_BACKUP_KEY must contain 64 hexadecimal characters"
        ) from None
    if len(key) != 32:
        raise BackupError("MONGODB_BACKUP_KEY must contain 64 hexadecimal characters")
    return key


def file_checksum(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_snapshot(backend, path: Path, key: bytes):
    """Return the encrypted snapshot's revision, record counts and checksum."""
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    compressor = zlib.compressobj(level=6)
    total = 0
    counts = {}
    revision = None
    target = path.open("xb")
    try:
        with target:
            path.chmod(0o600)
            target.write(MAGIC + nonce)

            def write(value):
                nonlocal total
                data = (json_util.dumps(value, separators=(",", ":")) + "\n").encode()
                total += len(data)
                if total > MAX_EXPANDED:
                    raise BackupError("MongoDB backup exceeds the 1 GiB expanded limit")
                target.write(encryptor.update(compressor.compress(data)))

            with backend.client.start_session(snapshot=True) as session:
                write(
                    {
                        "format": "tgdrive-mongodb",
                        "version": 1,
                        "created_at": datetime.now(UTC),
                    }
                )
                for name in COLLECTIONS:
                    count = 0
                    for document in backend.db[name].find({}, session=session):
                        write({"collection": name, "document": document})
                        if name == "catalog" and document["_id"] == "catalog":
                            revision = document["revision"]
                        count += 1
                    counts[name] = count
            if counts["catalog"] != 1 or type(revision) is not int or revision < 1:
                raise BackupError("MongoDB backup catalog revision is invalid")
            write({"complete": True, "counts": counts})
            target.write(encryptor.update(compressor.flush()))
            target.write(encryptor.finalize())
            target.write(encryptor.tag)
            target.flush()
            os.fsync(target.fileno())
        return revision, counts, file_checksum(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def decode_snapshot(path: Path, key: bytes):
    """Validate authentication, framing, bounds and completeness before restoring."""
    if path.stat().st_size > MAX_EXPANDED:
        raise BackupError("Encrypted backup is too large")
    data = path.read_bytes()
    from tgdrive.recovery.recovery_file import MAGIC as RECOVERY_MAGIC
    from tgdrive.recovery.recovery_file import decode_recovery_file

    if data.startswith(RECOVERY_MAGIC):
        try:
            return decode_recovery_file(data, key.hex())["database"]
        except ValueError:
            raise BackupError(
                "Backup authentication or validation failed; nothing restored"
            ) from None
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 12 + 16:
        raise BackupError("This is not a TGDrive MongoDB backup")
    nonce = data[len(MAGIC) : len(MAGIC) + 12]
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, data[-16:])).decryptor()
    decryptor.authenticate_additional_data(MAGIC)
    try:
        compressed = (
            decryptor.update(data[len(MAGIC) + 12 : -16]) + decryptor.finalize()
        )
        decoder = zlib.decompressobj()
        content = decoder.decompress(compressed, MAX_EXPANDED + 1)
        if (
            len(content) > MAX_EXPANDED
            or not decoder.eof
            or decoder.unconsumed_tail
            or decoder.unused_data
        ):
            raise ValueError
        records = [json_util.loads(line) for line in content.splitlines()]
        if not records or records[0].get("format") != "tgdrive-mongodb":
            raise ValueError
        if records[0].get("version") != 1 or not records[-1].get("complete"):
            raise ValueError
        result = {name: [] for name in COLLECTIONS}
        seen = {name: set() for name in COLLECTIONS}
        for record in records[1:-1]:
            name = record["collection"]
            document = record["document"]
            if (
                name not in result
                or not isinstance(document, dict)
                or "_id" not in document
            ):
                raise ValueError
            if document["_id"] in seen[name]:
                raise ValueError
            seen[name].add(document["_id"])
            result[name].append(document)
        if records[-1]["counts"] != {
            name: len(values) for name, values in result.items()
        }:
            raise ValueError
        if len(result["catalog"]) != 1 or not result["nodes"]:
            raise ValueError
        MongoStore.validate_snapshot_records(result)
        return result
    except Exception:
        raise BackupError(
            "Backup authentication or validation failed; nothing restored"
        ) from None


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
            not _pristine_recovery_key(row, None)
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
        if row.get("_id") == "bootstrap":
            continue
        if row.get("_id") == "telegram" and row.get("revision") == 1:
            continue
        if _pristine_recovery_key(row, catalog.get("drive_id")):
            continue
        raise FileExistsError("The destination contains saved settings changes")


def restore_decoded_snapshot(backend, data, *, allow_pristine=False):
    """Recheck destination and atomically import a previously authenticated archive."""
    MongoStore.validate_snapshot_records(data)
    backend.ping()
    # Revocation is explicit: restored browser cookies must never reactivate.
    data = {**data, "sessions": []}

    def restore(session):
        backend.fence(session)
        check_restore_destination(
            backend, allow_pristine=allow_pristine, session=session
        )
        if allow_pristine:
            for name in COLLECTIONS:
                backend.db[name].delete_many({}, session=session)
        for name, values in data.items():
            if values:
                backend.db[name].insert_many(values, session=session)

    backend.transaction(restore, allow_paused=True)
    with backend._settings_lock:
        backend._settings_cached = False
        backend._settings_document = None
    MongoStore._indexes(backend)


def restore_snapshot(backend, path: Path, key: bytes):
    restore_decoded_snapshot(backend, decode_snapshot(path, key))


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
        except Exception:
            self._last_error = _HISTORY_UNAVAILABLE
        else:
            if latest:
                self._observe_backup(latest[0])
        return store

    def _observe_backup(self, row):
        if self._latest_backup is None or row.get(
            "created_at", ""
        ) >= self._latest_backup.get("created_at", ""):
            self._latest_backup = {
                key: value
                for key, value in row.items()
                if key not in {"_id", "local_path"}
            }
        if row.get("status") == "confirmed":
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
            "backup_key_configured": bool(self.settings.mongodb_backup_key),
            "last_success_at": self._last_success_at,
        }
        journal = getattr(getattr(self, "replica", None), "journal", None)
        if journal is not None and journal.get("config", {}).get("enabled"):
            independent = journal.get("status", {})
            result["last_success_at"] = (
                independent.get("last_success_at") or self._last_success_at
            )
            result["backup_error"] = (independent.get("error") or {}).get("detail")
            result["backup_key_configured"] = (
                bool(journal.get("key")) or result["backup_key_configured"]
            )
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

    async def snapshot_now(self, store, *, legacy=False):
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
            legacy_key = (
                pending.get("format") != "tgdrive-backup-v2"
                if pending is not None
                else legacy
            )
            replica = getattr(self, "replica", None)
            key = (
                bytes.fromhex(replica.key())
                if replica is not None and not legacy_key
                else backup_key(self.settings)
            )
            if pending is None:
                identity = secrets.token_urlsafe(16)
                directory = self.settings.resolved_cache_dir / "mongo-backups"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                path = directory / (identity + ".tgdb")
                writer = (
                    (write_snapshot, backend, path, key)
                    if legacy
                    else (write_recovery_snapshot, backend, path, key, self.settings)
                )
                export = asyncio.create_task(asyncio.to_thread(*writer))
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
                    "format": "mongodb-encrypted-v1" if legacy else "tgdrive-backup-v2",
                    "key_sha256": hashlib.sha256(key).hexdigest(),
                }
                await asyncio.to_thread(backend.db.backups.insert_one, row)
                pending = row
            self._observe_backup(pending)
            if pending["channel_id"] != self.settings.storage_channel:
                raise BackupError("Pending backup belongs to another channel")
            fingerprint = pending.get("key_sha256")
            if (
                fingerprint is not None
                and fingerprint != hashlib.sha256(key).hexdigest()
            ):
                raise BackupError(
                    "The pending recovery file uses a different key. Preserve the "
                    "file and restore its original recovery key before retrying."
                )
            remaining = pending.get("retry_at", 0) - time.time()
            if remaining > 0:
                raise BackupError(
                    "Telegram backup is waiting before retry", retry_after=remaining
                )
            path = Path(pending["local_path"])
            # A fresh host may have the database but not its previous temporary file.
            # Do not resend changed bytes under an existing random_id.
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
                        file_name=(
                            "tgdrive-backup.tgdb"
                            if pending["format"] == "tgdrive-backup-v2"
                            else "tgdrive-" + pending["id"] + ".tgdb"
                        ),
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
                confirmed_at = datetime.now(UTC).isoformat()
                confirmed = {
                    "status": "confirmed",
                    "message_id": identity,
                    "confirmed_at": confirmed_at,
                }
                await asyncio.to_thread(
                    backend.db.backups.update_one,
                    {"_id": pending["_id"]},
                    {"$set": confirmed},
                )
                pending.update(confirmed)
                self._observe_backup(pending)
                if pending["format"] != "tgdrive-backup-v2":
                    path.unlink(missing_ok=True)
                else:
                    await asyncio.to_thread(
                        self._prune_confirmed_files, previous_files, pending["id"]
                    )
                self._last_error = None
                return True
            except Exception as exc:
                seconds = getattr(exc, "seconds", None)
                if isinstance(seconds, (int, float)) and seconds > 0:
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
                    "The encrypted Telegram backup will retry automatically."
                )
                raise

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
            except Exception:
                self._last_error = (
                    "The encrypted Telegram backup will retry automatically."
                )
                delay = 60

    async def stop(self, store):
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
