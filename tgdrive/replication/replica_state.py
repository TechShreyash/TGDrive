"""Private durable replica journal and bounded, authenticated recovery bundles."""

from __future__ import annotations

import copy
import json
import os
import secrets
import threading
from pathlib import Path

from bson import json_util
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tgdrive.recovery.backup import decode_catalog_snapshot
from tgdrive.storage.mongo_store import MongoStore

MAGIC = b"TGDRIVE-REPLICA-1\n"
MAX_BUNDLE = 64 * 1024**2


def atomic_private(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name("." + path.name + "-" + secrets.token_hex(12))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def read_private(path: Path, *, maximum=MAX_BUNDLE):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        import stat

        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("Invalid private recovery file")
        if info.st_mode & 0o077:
            raise ValueError("Recovery files must have mode 0600")
        with os.fdopen(fd, "rb", closefd=False) as source:
            return source.read(maximum + 1)
    finally:
        os.close(fd)


class ReplicaJournal:
    """Backup-channel state and send intents, stored in MongoDB with the drive."""

    # Characters per chunk document; UTF-8 stays well below MongoDB's 16 MiB limit.
    CHUNK = 1_000_000

    def __init__(self, directory: Path, backend=None):
        from tgdrive.storage.mongo import DatabaseUnavailable, for_directory

        self.backend = backend or for_directory(directory)
        if self.backend is None:
            raise DatabaseUnavailable("MongoDB is required for the backup journal.")
        self.lock = threading.RLock()
        self._import_sqlite(directory / "channel-backup.sqlite3")

    def _import_sqlite(self, path: Path):
        # shortcut: one-time import of the v2.0-v2.2 local journal; remove in 3.0.
        if not path.is_file() or path.is_symlink():
            return
        import sqlite3

        db = self.backend.db
        if db.replica_state.find_one({}) or db.replica_copies.find_one({}):
            raise ValueError(
                "Both channel-backup.sqlite3 and MongoDB backup state exist. "
                "Keep the MongoDB state and move the old file out of DATA_DIR."
            )
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            state = {
                key: json_util.loads(
                    value, json_options=json_util.JSONOptions(tz_aware=True)
                )
                for key, value in source.execute("SELECT key, value FROM state")
            }
            copies = source.execute(
                "SELECT key, random_id, message_id, size FROM copies"
            ).fetchall()
        finally:
            source.close()

        def migrate(session):
            self.backend.fence(session)
            self._write_state(state, session)
            if copies:
                db.replica_copies.insert_many(
                    [
                        {
                            "_id": key,
                            "random_id": random_id,
                            "message_id": message,
                            "size": size,
                        }
                        for key, random_id, message, size in copies
                    ],
                    session=session,
                )

        self.backend.transaction(migrate)
        path.rename(path.with_name(path.name + ".migrated"))

    def get(self, key, default=None):
        db = self.backend.db
        row = db.replica_state.find_one({"_id": key})
        if row is None:
            return copy.deepcopy(default)
        chunks = db.replica_chunks.find({"key": key}).sort("index", 1)
        raw = "".join(chunk["data"] for chunk in chunks)
        if len(raw) != row["length"]:
            raise ValueError("Backup journal state is incomplete")
        return json_util.loads(raw, json_options=json_util.JSONOptions(tz_aware=True))

    def set(self, key, value):
        self.set_many({key: value})

    def _write_state(self, values, session):
        db = self.backend.db
        for key, raw in values.items():
            if not isinstance(raw, str):
                raw = json.dumps(
                    raw, default=json_util.default, allow_nan=False, ensure_ascii=False
                )
            db.replica_chunks.delete_many({"key": key}, session=session)
            pieces = [raw[i : i + self.CHUNK] for i in range(0, len(raw), self.CHUNK)]
            if pieces:
                db.replica_chunks.insert_many(
                    [
                        {
                            "_id": f"{key}\x00{index}",
                            "key": key,
                            "index": index,
                            "data": data,
                        }
                        for index, data in enumerate(pieces)
                    ],
                    session=session,
                )
            db.replica_state.replace_one(
                {"_id": key},
                {"_id": key, "length": len(raw)},
                upsert=True,
                session=session,
            )

    def set_many(self, values):
        """Commit related state together, including explicit null values."""
        encoded = {
            key: json.dumps(
                value, default=json_util.default, allow_nan=False, ensure_ascii=False
            )
            for key, value in values.items()
        }

        def write(session):
            self.backend.fence(session)
            self._write_state(encoded, session)

        with self.lock:
            self.backend.transaction(write)

    def intent(self, key, size):
        db = self.backend.db
        with self.lock:
            row = db.replica_copies.find_one({"_id": key})
            if row is None:
                row = {
                    "_id": key,
                    "random_id": secrets.randbelow(2**63 - 1) + 1,
                    "message_id": None,
                    "size": size,
                }
                self.backend.write(db.replica_copies, "insert_one", row)
            if row["size"] != size:
                raise ValueError("Replica intent size changed")
            return row["random_id"], row["message_id"]

    def confirm(self, key, message_id):
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("Invalid replica confirmation")
        db = self.backend.db
        with self.lock:
            self.backend.write(
                db.replica_copies,
                "update_one",
                {"_id": key},
                {"$set": {"message_id": message_id}},
            )

    def owns_publication(self, channel, message_id):
        return any(
            row["_id"].startswith((f"bundle:{channel}:", f"primary-catalog:{channel}:"))
            for row in self.backend.db.replica_copies.find({"message_id": message_id})
        )

    def close(self):
        pass


def files_in(catalog):
    stack = [catalog["root"]]
    while stack:
        node = stack.pop()
        if node["type"] == "folder":
            stack.extend(reversed(list(node["children"].values())))
        else:
            yield node


def parts_in(node):
    parts = node.get("parts") or [
        {"message_id": node["message_id"], "size": node["size"]}
    ]
    return [*parts, node["poster"]] if node.get("poster") else parts


def validate_catalog(catalog):
    MongoStore._validate_payload(catalog)


def remap_catalog(catalog, channel, mapping):
    result = copy.deepcopy(catalog)
    for node in files_in(result):
        old_anchor = node["message_id"]
        node["message_id"] = mapping[str(old_anchor)]
        for part in node.get("parts", []):
            part["message_id"] = mapping[str(part["message_id"])]
        if node.get("poster"):
            node["poster"]["message_id"] = mapping[str(node["poster"]["message_id"])]
    # Old jobs/deletion receipts are tied to their old channel. They must never
    # replay into a recovered channel with coincidentally matching message IDs.
    result["operations"] = []
    result["deletions"] = []
    result.pop("folder_preparations", None)
    result["storage_channel_id"] = channel
    result["backed_up_revision"] = result["revision"]
    validate_catalog(result)
    return result


def encode_bundle(payload, key):
    if "database" in payload:
        from tgdrive.recovery.recovery_file import encode_recovery_file

        return encode_recovery_file(payload, key)
    raw = json.dumps(payload, allow_nan=False, ensure_ascii=False).encode()
    if len(raw) > MAX_BUNDLE - 1024:
        raise ValueError("The recovery bundle exceeds the 64 MiB limit")
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + AESGCM(bytes.fromhex(key)).encrypt(nonce, raw, MAGIC)


def decode_bundle(content, key):
    """Authenticate and validate a complete, internally equivalent channel pair."""
    from tgdrive.recovery.recovery_file import MAGIC as RECOVERY_MAGIC
    from tgdrive.recovery.recovery_file import decode_recovery_file

    if isinstance(content, bytes) and content.startswith(RECOVERY_MAGIC):
        return decode_recovery_file(content, key)
    import re

    from tgdrive.storage.store import _validate_timestamp

    try:
        if (
            not isinstance(content, bytes)
            or len(content) > MAX_BUNDLE
            or not content.startswith(MAGIC)
            or not isinstance(key, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", key.strip()) is None
        ):
            raise ValueError
        offset = len(MAGIC)
        raw = AESGCM(bytes.fromhex(key.strip())).decrypt(
            content[offset : offset + 12], content[offset + 12 :], MAGIC
        )
        result = decode_catalog_snapshot(raw)
        required = {
            "format",
            "version",
            "complete",
            "created_at",
            "catalog",
            "replica_catalog",
            "credentials",
        }
        if not isinstance(result, dict) or set(result) != required:
            raise ValueError
        if (
            result["format"] != "tgdrive-replica"
            or type(result["version"]) is not int
            or result["version"] != 1
            or result["complete"] is not True
        ):
            raise ValueError
        _validate_timestamp(result["created_at"])
        credentials = result["credentials"]
        base_fields = {
            "api_id",
            "api_hash",
            "session_secret",
            "bot_tokens",
            "managed_document",
            "preferences",
        }
        optional_fields = {
            "mongodb_backup_key",
            "mongodb_archive",
        }
        if (
            not isinstance(credentials, dict)
            or not base_fields <= set(credentials)
            or set(credentials) - base_fields - optional_fields
        ):
            raise ValueError
        from tgdrive.replication.replica_settings import validate_recovery_settings

        validate_recovery_settings(credentials)
        if (
            type(credentials["api_id"]) is not int
            or not 0 < credentials["api_id"] <= 2147483647
            or not isinstance(credentials["api_hash"], str)
            or re.fullmatch(r"[0-9a-fA-F]{32}", credentials["api_hash"]) is None
        ):
            raise ValueError
        secret = credentials["session_secret"]
        if (
            not isinstance(secret, str)
            or not 32 <= len(secret) <= 4096
            or secret != secret.strip()
            or secret.startswith("<")
            or any(ord(char) < 32 or ord(char) == 127 for char in secret)
        ):
            raise ValueError
        tokens = credentials["bot_tokens"]
        if (
            not isinstance(tokens, list)
            or not 1 <= len(tokens) <= 256
            or any(
                not isinstance(token, str)
                or len(token) > 512
                or re.fullmatch(r"[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}", token) is None
                for token in tokens
            )
            or len({token.split(":", 1)[0] for token in tokens}) != len(tokens)
        ):
            raise ValueError
        original, replica = result["catalog"], result["replica_catalog"]
        for catalog in (original, replica):
            validate_catalog(catalog)
            channel = catalog.get("storage_channel_id")
            if type(channel) is not int or not -(1 << 63) <= channel < 0:
                raise ValueError
        if original["storage_channel_id"] == replica["storage_channel_id"]:
            raise ValueError
        archive = credentials.get("mongodb_archive")
        if archive is not None and (
            archive["source_channel"] != original["storage_channel_id"]
            or archive["replica_channel"] != replica["storage_channel_id"]
            or archive["revision"] < original["revision"]
        ):
            raise ValueError
        old_files = {node["id"]: node for node in files_in(original)}
        new_files = {node["id"]: node for node in files_in(replica)}
        if set(old_files) != set(new_files):
            raise ValueError
        mapping = {}
        destinations = {}
        for node_id, old in old_files.items():
            before, after = parts_in(old), parts_in(new_files[node_id])
            if len(before) != len(after):
                raise ValueError
            for old_part, new_part in zip(before, after, strict=True):
                source, target = old_part["message_id"], new_part["message_id"]
                if source in mapping and mapping[source] != target:
                    raise ValueError
                if target in destinations and destinations[target] != source:
                    raise ValueError
                mapping[source] = target
                destinations[target] = source
        expected = remap_catalog(
            original,
            replica["storage_channel_id"],
            {str(source): target for source, target in mapping.items()},
        )
        if expected != replica:
            raise ValueError
        return result
    except Exception:
        raise ValueError(
            "Recovery key or bundle validation failed; nothing changed"
        ) from None
