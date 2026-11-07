"""Private durable replica journal and bounded, authenticated recovery bundles."""

from __future__ import annotations

import copy
import json
import os
import secrets
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

from bson import json_util

from tgdrive.storage.mongo_store import MongoStore


class PrivateRecoveryFileTooLarge(ValueError):
    """A preserved legacy record exceeds bounded in-memory compatibility reads."""

    def __init__(self, maximum_bytes):
        self.maximum_bytes = maximum_bytes
        super().__init__(
            "The private recovery file exceeds its compatibility admission limit"
        )


def _private_maximum_bytes():
    from tgdrive.recovery.recovery_file import MAX_EXPANDED_BYTES

    return 4 * MAX_EXPANDED_BYTES


class BoundedPrivateReader:
    """Bound serialized bytes, including input that grows after its initial stat."""

    def __init__(self, source, maximum_bytes):
        self.source, self.maximum_bytes = source, maximum_bytes
        self.consumed_bytes = 0

    def read(self, size=-1):
        if size == 0:
            return b""
        available = self.maximum_bytes - self.consumed_bytes
        if available == 0:
            if self.source.read(1):
                raise PrivateRecoveryFileTooLarge(self.maximum_bytes)
            return b""
        requested = available if size < 0 else min(size, available)
        data = self.source.read(requested)
        self.consumed_bytes += len(data)
        if (
            len(data) == available
            and (size < 0 or size > available)
            and self.source.read(1)
        ):
            raise PrivateRecoveryFileTooLarge(self.maximum_bytes)
        return data


@contextmanager
def private_reader(path):
    """Own one checked private regular-file descriptor throughout a bounded read."""
    maximum = _private_maximum_bytes()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Invalid private recovery file")
        if info.st_mode & 0o077:
            raise ValueError("Recovery files must have mode 0600")
        if info.st_size > maximum:
            raise PrivateRecoveryFileTooLarge(maximum)
        with os.fdopen(fd, "rb", closefd=False) as source:
            yield BoundedPrivateReader(source, maximum)
    finally:
        os.close(fd)


def read_legacy_value(source):
    """Keep legacy BSON interpretation while bounding a whole decoded value."""
    import ijson

    from tgdrive.recovery.staged_snapshot import (
        MAX_VALUE_BYTES,
        RecoveryResourceLimit,
        _Parser,
    )

    options = json_util.JSONOptions(tz_aware=True)
    parser = _Parser(
        BoundedPrivateReader(source, MAX_VALUE_BYTES),
        object_hook=lambda value: json_util.object_hook(value, json_options=options),
    )
    try:
        value = parser.value()
        if next(parser.events, None) is not None:
            raise ValueError("Trailing private recovery JSON data")
        return value
    except PrivateRecoveryFileTooLarge as error:
        if error.maximum_bytes == MAX_VALUE_BYTES:
            raise RecoveryResourceLimit(
                "Legacy recovery JSON exceeds the serialized UTF-8 byte limit"
            ) from None
        raise
    except ijson.JSONError:
        raise ValueError("Invalid private recovery JSON") from None


class _JournalChunks:
    """Read ordered MongoDB string chunks without joining the complete value."""

    def __init__(self, chunks, characters):
        if type(characters) is not int or characters < 0:
            raise ValueError("Backup journal state is incomplete")
        if characters > _private_maximum_bytes():
            raise PrivateRecoveryFileTooLarge(_private_maximum_bytes())
        self.chunks, self.characters = iter(chunks), characters
        self.seen_characters = self.index = self.offset = 0
        self.buffer = b""

    def read(self, size):
        output = bytearray()
        while len(output) < size:
            if self.offset == len(self.buffer):
                row = next(self.chunks, None)
                if row is None:
                    if self.seen_characters != self.characters:
                        raise ValueError("Backup journal state is incomplete")
                    break
                data = row.get("data")
                if (
                    type(row.get("index")) is not int
                    or row["index"] != self.index
                    or not isinstance(data, str)
                    or not data
                ):
                    raise ValueError("Backup journal state is incomplete")
                self.index += 1
                self.seen_characters += len(data)
                if self.seen_characters > self.characters:
                    raise ValueError("Backup journal state is incomplete")
                self.buffer, self.offset = data.encode("utf-8"), 0
            count = min(size - len(output), len(self.buffer) - self.offset)
            output.extend(self.buffer[self.offset : self.offset + count])
            self.offset += count
        return bytes(output)


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


def read_private(path: Path):
    with private_reader(path) as source:
        return source.read()


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
        self._resource_errors = {}

    def resource_error(self):
        """Return a memoized rejection without reading the preserved value again."""
        with self.lock:
            if not self._resource_errors:
                return None
            key, reason = next(iter(self._resource_errors.items()))
            from tgdrive.replication.replica_activation import _resource_error

            return {
                "key": key,
                "code": "ACTIVATION_RESOURCE_LIMIT",
                "detail": str(_resource_error(reason)),
            }

    def get(self, key, default=None):
        from tgdrive.recovery.staged_snapshot import RecoveryResourceLimit

        with self.lock:
            if key in self._resource_errors:
                from tgdrive.replication.replica_activation import _resource_error

                raise _resource_error(self._resource_errors[key]) from None
            db = self.backend.db
            row = db.replica_state.find_one({"_id": key})
            if row is None:
                return copy.deepcopy(default)
            chunks = db.replica_chunks.find({"key": key}).sort("index", 1).batch_size(1)
            try:
                source = _JournalChunks(chunks, row["length"])
                return read_legacy_value(
                    BoundedPrivateReader(source, _private_maximum_bytes())
                )
            except PrivateRecoveryFileTooLarge, RecoveryResourceLimit, MemoryError:
                reason = "Saved recovery journal exceeds provisional decoding limits"
            finally:
                close = getattr(chunks, "close", None)
                if close is not None:
                    close()
            # Store only a reason, never the exception/traceback with its partial
            # decoded private tree. The same owned journal can now report status
            # without rebuilding a value already rejected by admission.
            self._resource_errors[key] = reason
            from tgdrive.replication.replica_activation import _resource_error

            raise _resource_error(reason) from None

    def get_many(self, defaults):
        """Read related keys from one committed journal version."""
        with self.lock:
            return {key: self.get(key, default) for key, default in defaults.items()}

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
        from tgdrive.recovery.staged_snapshot import RecoveryResourceLimit

        for raw in encoded.values():
            chunks = (
                {"index": index, "data": raw[start : start + self.CHUNK]}
                for index, start in enumerate(range(0, len(raw), self.CHUNK))
            )
            try:
                read_legacy_value(_JournalChunks(chunks, len(raw)))
            except PrivateRecoveryFileTooLarge, RecoveryResourceLimit, MemoryError:
                from tgdrive.replication.replica_activation import _resource_error

                raise _resource_error(
                    "New recovery journal value exceeds provisional decoding limits"
                ) from None

        def write(session):
            self.backend.fence(session)
            self._write_state(encoded, session)

        with self.lock:
            try:
                self.backend.transaction(write)
            finally:
                # An uncertain commit may have installed the readable replacement.
                # Re-evaluate affected keys once after an actual write attempt.
                for key in values:
                    self._resource_errors.pop(key, None)

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
