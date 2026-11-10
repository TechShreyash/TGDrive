"""Stream an existing V2 recovery envelope without retaining the drive catalog."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import tempfile
import zlib
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from bson import BSON, json_util
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tgdrive.recovery.backup import BackupError
from tgdrive.recovery.recovery_file import (
    MAGIC,
    MAX_DATABASE_BYTES,
    MAX_DATABASE_RECORDS,
    MAX_EXPANDED_BYTES,
    MAX_RECOVERY_BYTES,
    sanitize_database,
)
from tgdrive.replication.replica_settings import POLICIES, credentials_from_snapshot

IO_CHUNK_BYTES = 64 * 1024
CURSOR_BATCH_RECORDS = 100


class _Writer:
    def __init__(self, stream, directory, reserve, maximum):
        self.stream = stream
        self.directory = directory
        self.reserve = reserve
        self.maximum = maximum
        self.size = 0
        self.checksum = hashlib.sha256()
        self.buffer = bytearray()

    def write(self, content):
        if self.maximum is not None and self.size + len(content) > self.maximum:
            raise ValueError("The recovery file exceeds its configured byte limit")
        self.checksum.update(content)
        self.size += len(content)
        offset = 0
        while offset < len(content):
            length = min(IO_CHUNK_BYTES - len(self.buffer), len(content) - offset)
            self.buffer.extend(content[offset : offset + length])
            offset += length
            if len(self.buffer) == IO_CHUNK_BYTES:
                self.flush()

    def flush(self):
        if not self.buffer:
            return
        if shutil.disk_usage(self.directory).free < self.reserve + len(self.buffer):
            raise BackupError(
                "Not enough temporary space to stage a recovery file; "
                "free space and retry"
            )
        if self.stream.write(self.buffer) != len(self.buffer):
            raise OSError("Incomplete recovery staging write")
        self.buffer.clear()

    def json(self, value):
        encoder = json.JSONEncoder(
            default=json_util.default, allow_nan=False, separators=(",", ":")
        )
        for part in encoder.iterencode(value):
            # A single MongoDB string may be large. Encode bounded slices rather
            # than allocating another complete UTF-8 copy of that string.
            for offset in range(0, len(part), IO_CHUNK_BYTES // 4):
                self.write(part[offset : offset + IO_CHUNK_BYTES // 4].encode())


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _capture(backend, stream, settings, max_records, max_database_bytes):
    from tgdrive.recovery.mongo_backup import COLLECTIONS

    counts = dict.fromkeys(COLLECTIONS, 0)
    credential_ids = {"telegram"} | {name.removesuffix(".json") for name in POLICIES}
    credential_rows = []
    metadata = None
    size = count = 0
    stream.write(b'{"format":"tgdrive-backup","version":2,"created_at":')
    stream.json(datetime.now(UTC).isoformat(timespec="seconds"))
    stream.write(b',"complete":true,"database":{')
    with backend.client.start_session(snapshot=True) as session:
        for index, name in enumerate(COLLECTIONS):
            if index:
                stream.write(b",")
            stream.json(name)
            stream.write(b":[")
            if name != "sessions":
                cursor = (
                    backend.db[name]
                    .find({}, session=session)
                    .batch_size(CURSOR_BATCH_RECORDS)
                )
                try:
                    for row in cursor:
                        size += len(BSON.encode(row))
                        count += 1
                        if (max_records is not None and count > max_records) or (
                            max_database_bytes is not None and size > max_database_bytes
                        ):
                            raise ValueError(
                                "The recovery database exceeds its configured "
                                "record or byte limit"
                            )
                        if name == "settings":
                            row = sanitize_database({name: [row]})[name][0]
                            if row.get("_id") in credential_ids:
                                credential_rows.append(row)
                        if name == "catalog":
                            if metadata is not None:
                                raise ValueError("Invalid recovery catalog metadata")
                            metadata = row
                        if counts[name]:
                            stream.write(b",")
                        stream.json(row)
                        counts[name] += 1
                finally:
                    close = getattr(cursor, "close", None)
                    if close is not None:
                        close()
            stream.write(b"]")
        if metadata is None or type(metadata.get("revision")) is not int:
            raise ValueError("Invalid recovery catalog metadata")
        stream.write(b'},"replica":null,"credentials":')
        stream.json(credentials_from_snapshot(settings, {"settings": credential_rows}))
        stream.write(b"}")
    return metadata["revision"], counts


def write_recovery_snapshot(
    backend,
    path,
    key,
    settings,
    *,
    validate_snapshot,
    max_records: int | None = MAX_DATABASE_RECORDS,
    max_database_bytes: int | None = MAX_DATABASE_BYTES,
    max_expanded_bytes: int | None = MAX_EXPANDED_BYTES,
    max_recovery_bytes=MAX_RECOVERY_BYTES,
):
    """Validate staged JSON, then atomically publish its existing V2 ciphertext.

    ``validate_snapshot`` must validate the entire envelope and catalog graph.
    It is deliberately required: streaming rows alone cannot establish a valid
    restore point. This synchronous worker must be drained before its caller
    handles cancellation or removes its staging directory.
    """
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("Invalid recovery key")
    if not callable(validate_snapshot):
        raise TypeError("A complete staged recovery validator is required")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporaries = []
    try:
        descriptor, filename = tempfile.mkstemp(
            prefix="." + path.name + "-json-", dir=path.parent
        )
        plaintext = Path(filename)
        temporaries.append(plaintext)
        with os.fdopen(descriptor, "wb", buffering=0) as destination:
            writer = _Writer(
                destination, path.parent, settings.min_free_bytes, max_expanded_bytes
            )
            revision, counts = _capture(
                backend, writer, settings, max_records, max_database_bytes
            )
            writer.flush()
            os.fsync(destination.fileno())
        validate_snapshot(plaintext)
        digest = _encrypt_snapshot(plaintext, path, key, settings, max_recovery_bytes)
        return revision, counts, digest
    finally:
        cleanup_error = None
        for temporary in temporaries:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                cleanup_error = cleanup_error or error
        if temporaries:
            try:
                _sync_directory(path.parent)
            except OSError as error:
                cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise cleanup_error


def _encrypt_snapshot(plaintext, path, key, settings, maximum):
    if isinstance(key, str):
        key = bytes.fromhex(key.strip())
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("Invalid recovery key")
    descriptor, filename = tempfile.mkstemp(
        prefix="." + path.name + "-encrypted-", dir=path.parent
    )
    encrypted = Path(filename)
    try:
        with (
            os.fdopen(descriptor, "wb", buffering=0) as destination,
            plaintext.open("rb", buffering=0) as source,
        ):
            writer = _Writer(destination, path.parent, settings.min_free_bytes, maximum)
            nonce = secrets.token_bytes(12)
            cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
            cipher.authenticate_additional_data(MAGIC)
            writer.write(MAGIC + nonce)
            compressor = zlib.compressobj(level=6)
            while chunk := source.read(IO_CHUNK_BYTES):
                writer.write(cipher.update(compressor.compress(chunk)))
            writer.write(cipher.update(compressor.flush()))
            writer.write(cipher.finalize())
            writer.write(cipher.tag)
            writer.flush()
            os.fsync(destination.fileno())
        os.replace(encrypted, path)
        _sync_directory(path.parent)
        return writer.checksum.hexdigest()
    finally:
        encrypted.unlink(missing_ok=True)


def write_staged_json(
    staged,
    path,
    settings,
    *,
    node_channel=None,
    target_channel=None,
    mapping=None,
    replica_channel=None,
    replica_mapping=None,
    credentials=None,
    recovery_token=None,
    max_expanded_bytes: int | None = MAX_EXPANDED_BYTES,
):
    """Render bounded staged rows, optionally rebasing verified message locations."""
    from tgdrive.recovery.mongo_backup import COLLECTIONS

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, filename = tempfile.mkstemp(prefix=".snapshot-json-", dir=path.parent)
    temporary = Path(filename)
    counts = dict.fromkeys(COLLECTIONS, 0)
    rebased = target_channel is not None or node_channel is not None
    resources = ExitStack()
    close_mapping = getattr(replica_mapping, "close", None)
    if close_mapping is not None:
        resources.callback(close_mapping)
    try:
        with os.fdopen(descriptor, "wb", buffering=0) as target:
            writer = _Writer(
                target, path.parent, settings.min_free_bytes, max_expanded_bytes
            )
            header = dict(
                staged.header, credentials=credentials or staged.header["credentials"]
            )
            header["created_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            header.pop("replica", None)
            if recovery_token is not None:
                header["recovery_token"] = recovery_token
            writer.write(b"{")
            for index, (name, value) in enumerate(header.items()):
                if index:
                    writer.write(b",")
                writer.json(name)
                writer.write(b":")
                writer.json(value)
            writer.write(b',"database":{')
            for index, name in enumerate(COLLECTIONS):
                if index:
                    writer.write(b",")
                writer.json(name)
                writer.write(b":[")
                rows = (
                    staged.nodes(node_channel)
                    if name == "nodes"
                    else staged.records(name)
                )
                close_rows = getattr(rows, "close", None)
                if close_rows is not None:
                    resources.callback(close_rows)
                if rebased and name in {
                    "sessions",
                    "identities",
                    "operations",
                    "deletions",
                    "preparations",
                    "receipts",
                    "backups",
                }:
                    rows = ()
                for row in rows:
                    if name == "catalog" and rebased:
                        row["storage_channel_id"] = target_channel or node_channel
                        row["backed_up_revision"] = row["revision"]
                        row.pop("replica_activation_id", None)
                    if (
                        name == "nodes"
                        and row["kind"] == "file"
                        and mapping is not None
                    ):
                        row["message_id"] = mapping(int(row["message_id"]))
                        for part in row.get("parts") or []:
                            part["message_id"] = mapping(int(part["message_id"]))
                        if row.get("poster"):
                            row["poster"]["message_id"] = mapping(
                                int(row["poster"]["message_id"])
                            )
                    if counts[name]:
                        writer.write(b",")
                    writer.json(row)
                    counts[name] += 1
                writer.write(b"]")
            writer.write(b'},"replica":')
            if replica_channel is None:
                if replica_mapping is not None:
                    raise ValueError("Recovery mapping requires a replica channel")
                writer.write(b"null")
            else:
                if replica_mapping is None:
                    raise ValueError("Replica channel requires a recovery mapping")
                writer.write(b'{"channel":')
                writer.json(replica_channel)
                writer.write(b',"messages":{')
                for index, (source, destination) in enumerate(replica_mapping):
                    if index:
                        writer.write(b",")
                    writer.json(str(source))
                    writer.write(b":")
                    writer.json(destination)
                writer.write(b"}}")
            writer.write(b"}")
            writer.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
        return staged.catalog["revision"], counts
    finally:
        try:
            resources.close()
        finally:
            temporary.unlink(missing_ok=True)


def write_staged_snapshot(
    staged,
    path,
    key,
    settings,
    *,
    validate_snapshot,
    max_recovery_bytes=MAX_RECOVERY_BYTES,
    **options,
):
    """Validate derived staged JSON before publishing compatible V2 ciphertext."""
    if not callable(validate_snapshot):
        raise TypeError("A complete staged recovery validator is required")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, filename = tempfile.mkstemp(prefix=".derived-json-", dir=path.parent)
    os.close(descriptor)
    plaintext = Path(filename)
    try:
        revision, counts = write_staged_json(staged, plaintext, settings, **options)
        validate_snapshot(plaintext)
        digest = _encrypt_snapshot(plaintext, path, key, settings, max_recovery_bytes)
        return revision, counts, digest
    finally:
        plaintext.unlink(missing_ok=True)


def decrypt_snapshot(
    path,
    destination,
    key,
    settings,
    *,
    max_recovery_bytes=MAX_RECOVERY_BYTES,
    max_expanded_bytes: int | None = MAX_EXPANDED_BYTES,
):
    """Authenticate all ciphertext before decompressing into private staged JSON.

    The caller owns ``destination`` and must drain this worker on cancellation.
    Parsing and semantic validation are separate and may only follow success.
    """
    if isinstance(key, str):
        try:
            key = bytes.fromhex(key.strip())
        except ValueError:
            raise ValueError("Invalid recovery key") from None
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("Invalid recovery key")
    path, destination = Path(path), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporaries = []
    try:
        source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(source_fd, "rb", buffering=0) as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size < len(MAGIC) + 28
                or (
                    max_recovery_bytes is not None and info.st_size > max_recovery_bytes
                )
                or source.read(len(MAGIC)) != MAGIC
            ):
                raise ValueError("Invalid recovery file")
            nonce = source.read(12)
            source.seek(-16, os.SEEK_END)
            tag = source.read(16)
            source.seek(len(MAGIC) + 12)
            cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            cipher.authenticate_additional_data(MAGIC)
            descriptor, filename = tempfile.mkstemp(
                prefix=".recovery-compressed-", dir=destination.parent
            )
            compressed = Path(filename)
            temporaries.append(compressed)
            with os.fdopen(descriptor, "wb", buffering=0) as target:
                writer = _Writer(
                    target,
                    destination.parent,
                    settings.min_free_bytes,
                    max_recovery_bytes,
                )
                remaining = info.st_size - len(MAGIC) - 28
                while remaining:
                    block = source.read(min(IO_CHUNK_BYTES, remaining))
                    if not block:
                        raise ValueError("Truncated recovery file")
                    remaining -= len(block)
                    writer.write(cipher.update(block))
                # GCM output is untrusted until finalize verifies the complete tag.
                writer.write(cipher.finalize())
                if source.read(16) != tag or source.read(1):
                    raise ValueError("Recovery file changed during authentication")
                writer.flush()
                os.fsync(target.fileno())
        descriptor, filename = tempfile.mkstemp(
            prefix=".recovery-json-", dir=destination.parent
        )
        plaintext = Path(filename)
        temporaries.append(plaintext)
        with (
            compressed.open("rb", buffering=0) as source,
            os.fdopen(descriptor, "wb", buffering=0) as target,
        ):
            writer = _Writer(
                target,
                destination.parent,
                settings.min_free_bytes,
                max_expanded_bytes,
            )
            decoder = zlib.decompressobj()
            while block := source.read(IO_CHUNK_BYTES):
                while block:
                    writer.write(decoder.decompress(block, IO_CHUNK_BYTES))
                    block = decoder.unconsumed_tail
                    if decoder.unused_data:
                        raise ValueError("Trailing recovery compression data")
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError("Incomplete recovery compression data")
            writer.flush()
            os.fsync(target.fileno())
        os.replace(plaintext, destination)
        _sync_directory(destination.parent)
        return writer.size
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)
