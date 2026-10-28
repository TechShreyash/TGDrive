"""Private durable replica journal and bounded, authenticated recovery bundles."""

from __future__ import annotations

import copy
import json
import os
import secrets
import sqlite3
import threading
from pathlib import Path

from bson import json_util
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tgdrive.recovery.backup import decode_catalog_snapshot
from tgdrive.storage.store import DriveStore

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
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "channel-backup.sqlite3"
        if path.is_symlink():
            raise ValueError("Unsafe backup journal path")
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS copies(
                key TEXT PRIMARY KEY, random_id INTEGER NOT NULL,
                message_id INTEGER, size INTEGER NOT NULL
            );
        """)
        self.db.commit()

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM state WHERE key=?", (key,)
            ).fetchone()
            return (
                json_util.loads(
                    row[0], json_options=json_util.JSONOptions(tz_aware=True)
                )
                if row
                else copy.deepcopy(default)
            )

    def set(self, key, value):
        self.set_many({key: value})

    def set_many(self, values):
        """Commit related state together, including explicit null values."""
        rows = [
            (
                key,
                json.dumps(
                    value,
                    default=json_util.default,
                    allow_nan=False,
                    ensure_ascii=False,
                ),
            )
            for key, value in values.items()
        ]
        with self.lock, self.db:
            self.db.executemany(
                "INSERT INTO state VALUES(?,?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                rows,
            )

    def intent(self, key, size):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO copies VALUES(?,?,NULL,?)",
                (key, secrets.randbelow(2**63 - 1) + 1, size),
            )
            row = self.db.execute(
                "SELECT random_id,message_id,size FROM copies WHERE key=?", (key,)
            ).fetchone()
            if row[2] != size:
                raise ValueError("Replica intent size changed")
            return row[0], row[1]

    def confirm(self, key, message_id):
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("Invalid replica confirmation")
        with self.lock, self.db:
            self.db.execute(
                "UPDATE copies SET message_id=? WHERE key=?", (message_id, key)
            )

    def owns_publication(self, channel, message_id):
        with self.lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM copies WHERE message_id=? "
                    "AND (key LIKE ? OR key LIKE ?) LIMIT 1",
                    (message_id, f"bundle:{channel}:%", f"primary-catalog:{channel}:%"),
                ).fetchone()
                is not None
            )

    def close(self):
        with self.lock:
            self.db.close()


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
    store = DriveStore._from_payload(catalog)
    store.close()


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
