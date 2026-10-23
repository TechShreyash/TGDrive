"""Encrypted full MongoDB snapshots, independent of normal transfer completion."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path

from bson import json_util
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tgdrive.backup import BackupError, RecoveryConflict
from tgdrive.mongo_store import MongoStore

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
    """Stream an encrypted consistent snapshot; never stage plaintext secrets."""
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    compressor = zlib.compressobj(level=6)
    total = 0
    counts = {}
    with path.open("xb") as target:
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
                    count += 1
                counts[name] = count
        write({"complete": True, "counts": counts})
        target.write(encryptor.update(compressor.flush()))
        target.write(encryptor.finalize())
        target.write(encryptor.tag)
        target.flush()
        os.fsync(target.fileno())
    return counts


def decode_snapshot(path: Path, key: bytes):
    """Validate authentication, framing, bounds and completeness before restoring."""
    if path.stat().st_size > MAX_EXPANDED:
        raise BackupError("Encrypted backup is too large")
    data = path.read_bytes()
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
        return result
    except Exception:
        raise BackupError(
            "Backup authentication or validation failed; nothing restored"
        ) from None


def restore_snapshot(backend, path: Path, key: bytes):
    data = decode_snapshot(path, key)
    backend.ping()
    if backend.db.catalog.find_one({}):
        raise FileExistsError("Restore requires an empty destination MongoDB database")
    # Revocation is explicit: restored browser cookies must never reactivate.
    data["sessions"] = []

    def restore(session):
        for name in COLLECTIONS:
            if backend.db[name].find_one({}, session=session):
                raise FileExistsError("The restore destination is not empty")
        for name, values in data.items():
            if values:
                backend.db[name].insert_many(values, session=session)

    backend.transaction(restore)
    MongoStore._indexes(backend)
    store = MongoStore(backend)
    store.close()


class MongoProtection:
    """Cloud commits protect normal work; Telegram copies run independently."""

    def __init__(self, settings, pool, backend):
        self.settings, self.pool, self.backend = settings, pool, backend
        self._stop = asyncio.Event()
        self._task = None
        self._lock = asyncio.Lock()
        self._last_error = None
        self._last_success_at = None
        self.recovery_required = False
        self.recovery_error = None

    async def restore(self):
        return await asyncio.to_thread(MongoStore, self.backend)

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
        if error:
            result["error"] = error
        return result

    def history(self, *, limit=None):
        rows = self.backend.db.backups.find({}, {"_id": 0, "local_path": 0}).sort(
            "created_at", -1
        )
        result = list(rows.limit(limit or 100))
        if result and result[0].get("status") == "confirmed":
            self._last_success_at = result[0].get("confirmed_at")
        return result

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
            key = backup_key(self.settings)
            pending = await asyncio.to_thread(
                backend.db.backups.find_one,
                {"status": "pending"},
                sort=[("created_at", 1)],
            )
            if pending is None:
                identity = secrets.token_urlsafe(16)
                directory = self.settings.resolved_cache_dir / "mongo-backups"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                path = directory / (identity + ".tgdb")
                counts = await asyncio.to_thread(write_snapshot, backend, path, key)
                row = {
                    "_id": identity,
                    "id": identity,
                    "status": "pending",
                    "created_at": datetime.now(UTC).isoformat(),
                    "revision": store.revision,
                    "local_path": str(path),
                    "sha256": await asyncio.to_thread(file_checksum, path),
                    "random_id": secrets.randbits(63),
                    "sender": None,
                    "message_id": None,
                    "channel_id": self.settings.storage_channel,
                    "retry_at": 0,
                    "counts": counts,
                    "format": "mongodb-encrypted-v1",
                }
                await asyncio.to_thread(backend.db.backups.insert_one, row)
                pending = row
            if pending["channel_id"] != self.settings.storage_channel:
                raise BackupError("Pending backup belongs to another channel")
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
                    message = await client.send_document(
                        self.settings.storage_channel,
                        str(path),
                        file_name="tgdrive-" + pending["id"] + ".tgdb",
                        random_id=pending["random_id"],
                        caption="TGDrive encrypted MongoDB backup · "
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
                await asyncio.to_thread(
                    backend.db.backups.update_one,
                    {"_id": pending["_id"]},
                    {
                        "$set": {
                            "status": "confirmed",
                            "message_id": identity,
                            "confirmed_at": datetime.now(UTC).isoformat(),
                        }
                    },
                )
                path.unlink(missing_ok=True)
                self._last_error = None
                self._last_success_at = datetime.now(UTC).isoformat()
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
                    await self.pool.defer_channel(deadline)
                self._last_error = (
                    "The encrypted Telegram backup will retry automatically."
                )
                raise

    def start(self, store):
        if self._task is None:
            self._task = asyncio.create_task(self._loop(store), name="mongodb-backup")
        return self._task

    async def _loop(self, store):
        delay = 30
        try:
            pending = await asyncio.to_thread(
                self.backend.db.backups.find_one, {"status": "pending"}
            )
            latest = await asyncio.to_thread(
                self.backend.db.backups.find_one,
                {"status": "confirmed"},
                sort=[("created_at", -1)],
            )
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
