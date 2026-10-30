"""Private durable replica journal and bounded, authenticated recovery bundles."""

from __future__ import annotations

import copy
import json
import os
import secrets
import threading
from pathlib import Path

from bson import json_util

from tgdrive.storage.mongo_store import MongoStore

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
    from tgdrive.recovery.recovery_file import encode_recovery_file

    return encode_recovery_file(payload, key)


def decode_bundle(content, key):
    from tgdrive.recovery.recovery_file import decode_recovery_file

    return decode_recovery_file(content, key)
