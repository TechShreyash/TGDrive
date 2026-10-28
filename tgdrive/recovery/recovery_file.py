"""One authenticated recovery file: one database snapshot and channel mappings."""

from __future__ import annotations

import copy
import json
import math
import re
import secrets
import shutil
import zlib
from datetime import UTC, datetime

from bson import json_util
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tgdrive.storage.mongo_store import MongoStore

MAGIC = b"TGDRIVE-BACKUP-2\n"
MAX_FILE_BYTES = 64 * 1024**2
MAX_EXPANDED = 1024**3
_TOKEN = re.compile(r"[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}")


def sanitize_database(records):
    """Keep durable records, never logged-in browser or Telegram user sessions."""
    result = copy.deepcopy(records)
    result["sessions"] = []
    for row in result.get("settings", []):
        document = row.get("document", {})
        if row.get("_id") == "telegram":
            document["clients"] = [
                client
                for client in document.get("clients", [])
                if client.get("kind") != "user"
            ]
        if row.get("_id") == "bootstrap":
            document.pop("STRING_SESSIONS", None)
            document.pop("MONGODB_BACKUP_KEY", None)
    return result


def snapshot_catalog(records):
    """Derive file locations from the captured database, never a later live read."""
    from tgdrive.recovery.mongo_backup import COLLECTIONS

    if not isinstance(records, dict) or set(records) != set(COLLECTIONS):
        raise ValueError("Invalid recovery database collections")
    for rows in records.values():
        if not isinstance(rows, list):
            raise ValueError("Invalid recovery database records")
        identities = set()
        for row in rows:
            if not isinstance(row, dict) or "_id" not in row:
                raise ValueError("Invalid recovery database document")
            if row["_id"] in identities:
                raise ValueError("Duplicate recovery database identity")
            identities.add(row["_id"])
    return MongoStore.validate_snapshot_records(records)


def capture_database(backend):
    """Read all supported collections from one MongoDB snapshot transaction."""
    from tgdrive.recovery.mongo_backup import COLLECTIONS

    with backend.client.start_session(snapshot=True) as session:
        records = {
            name: list(backend.db[name].find({}, session=session))
            for name in COLLECTIONS
        }
    records = sanitize_database(records)
    snapshot_catalog(records)
    if (
        len(json.dumps(records, default=json_util.default, allow_nan=False).encode())
        > MAX_EXPANDED
    ):
        raise ValueError("Recovery database exceeds the 1 GiB expanded limit")
    return records


def rebase_database(records, catalog):
    """Rebind safe captured data to verified recovered file locations."""
    from tgdrive.replication.replica_activation import _database, _mongo_nodes

    result = _database(records, catalog)
    result["nodes"] = _mongo_nodes(catalog)
    result["shares"] = [dict(row, _id=row["id"]) for row in catalog.get("shares", [])]
    metadata = result["catalog"][0]
    metadata.update(
        {
            "drive_id": catalog["drive_id"],
            "revision": catalog["revision"],
            "backed_up_revision": catalog["revision"],
            "storage_channel_id": catalog["storage_channel_id"],
        }
    )
    metadata.pop("replica_activation_id", None)
    if snapshot_catalog(result) != catalog:
        raise ValueError("Rebased database and recovery catalog do not match")
    return result


def _credentials(value):
    from tgdrive.replication.replica_settings import validate_recovery_settings

    required = {
        "api_id",
        "api_hash",
        "session_secret",
        "bot_tokens",
        "managed_document",
        "preferences",
    }
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - {"mongodb_backup_key"}
        or type(value["api_id"]) is not int
        or not 0 < value["api_id"] <= 2147483647
        or not isinstance(value["api_hash"], str)
        or re.fullmatch(r"[a-fA-F0-9]{32}", value["api_hash"]) is None
    ):
        raise ValueError("Invalid recovery credentials")
    validate_recovery_settings(value)
    secret = value["session_secret"]
    if (
        not isinstance(secret, str)
        or not 32 <= len(secret) <= 4096
        or secret != secret.strip()
        or secret.startswith("<")
        or any(ord(char) < 32 or ord(char) == 127 for char in secret)
    ):
        raise ValueError("Invalid recovery signing secret")
    tokens = value["bot_tokens"]
    if (
        not isinstance(tokens, list)
        or not 1 <= len(tokens) <= 256
        or any(
            not isinstance(token, str) or not _TOKEN.fullmatch(token)
            for token in tokens
        )
        or len({token.split(":", 1)[0] for token in tokens}) != len(tokens)
        or any(
            client["kind"] == "user" for client in value["managed_document"]["clients"]
        )
    ):
        raise ValueError("Invalid recovery bot credentials")


def _mapping(original, replica):
    from tgdrive.replication.replica_state import files_in, parts_in, remap_catalog

    if replica is None:
        return None
    source_channel = original["storage_channel_id"]
    target_channel = replica["storage_channel_id"]
    if (
        type(target_channel) is not int
        or not -(1 << 63) <= target_channel < 0
        or target_channel == source_channel
    ):
        raise ValueError("Invalid backup channel")
    originals = {node["id"]: node for node in files_in(original)}
    replicas = {node["id"]: node for node in files_in(replica)}
    if set(originals) != set(replicas):
        raise ValueError("Recovery catalogs contain different files")
    mapping = {}
    for identity, node in originals.items():
        before, after = parts_in(node), parts_in(replicas[identity])
        for old, new in zip(before, after, strict=True):
            key, destination = str(old["message_id"]), new["message_id"]
            if key in mapping and mapping[key] != destination:
                raise ValueError("Conflicting recovery file location")
            mapping[key] = destination
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Duplicate recovery destination")
    if remap_catalog(original, target_channel, mapping) != replica:
        raise ValueError("Recovery catalogs describe different restore points")
    return {"channel": target_channel, "messages": mapping}


def encode_recovery_file(payload, key):
    """Encode a normalized bundle; the database contains the sole catalog copy."""
    from tgdrive.storage.store import _validate_timestamp

    if (
        not isinstance(key, str)
        or re.fullmatch(r"[a-fA-F0-9]{64}", key.strip()) is None
    ):
        raise ValueError("Invalid recovery key")
    _validate_timestamp(payload["created_at"])
    database = sanitize_database(payload["database"])
    original = snapshot_catalog(database)
    channel = original.get("storage_channel_id")
    if type(channel) is not int or not -(1 << 63) <= channel < 0:
        raise ValueError("Invalid source channel")
    if original != payload["catalog"]:
        raise ValueError(
            "Database and recovery catalog describe different restore points"
        )
    _credentials(payload["credentials"])
    value = {
        "format": "tgdrive-backup",
        "version": 2,
        "created_at": payload["created_at"],
        "complete": True,
        "database": database,
        "replica": _mapping(original, payload.get("replica_catalog")),
        "credentials": payload["credentials"],
    }
    if payload.get("recovery_token") is not None:
        if (
            not isinstance(payload["recovery_token"], str)
            or len(payload["recovery_token"]) > 256
            or not _TOKEN.fullmatch(payload["recovery_token"])
        ):
            raise ValueError("Invalid recovery bot token")
        value["recovery_token"] = payload["recovery_token"]
    raw = json.dumps(
        value, default=json_util.default, allow_nan=False, separators=(",", ":")
    ).encode()
    if len(raw) > MAX_EXPANDED:
        raise ValueError("Recovery file exceeds the 1 GiB expanded limit")
    nonce = secrets.token_bytes(12)
    encrypted = (
        MAGIC
        + nonce
        + AESGCM(bytes.fromhex(key.strip())).encrypt(
            nonce, zlib.compress(raw, level=6), MAGIC
        )
    )
    if len(encrypted) > MAX_FILE_BYTES:
        raise ValueError("Encrypted recovery file exceeds the 64 MiB limit")
    return encrypted


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate recovery field")
        result[key] = value
    value = json_util.object_hook(
        result, json_options=json_util.JSONOptions(tz_aware=True)
    )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite recovery number")
    return value


def _constant(_value):
    raise ValueError("Non-finite recovery number")


def decode_recovery_file(content, key):
    """Authenticate, bound and validate before exposing any recovery state."""
    from tgdrive.replication.replica_state import remap_catalog
    from tgdrive.storage.store import _validate_timestamp

    try:
        if (
            not isinstance(content, bytes)
            or not len(MAGIC) + 28 <= len(content) <= MAX_FILE_BYTES
            or not content.startswith(MAGIC)
            or not isinstance(key, str)
            or re.fullmatch(r"[a-fA-F0-9]{64}", key.strip()) is None
        ):
            raise ValueError
        offset = len(MAGIC)
        compressed = AESGCM(bytes.fromhex(key.strip())).decrypt(
            content[offset : offset + 12], content[offset + 12 :], MAGIC
        )
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_EXPANDED + 1)
        if (
            len(raw) > MAX_EXPANDED
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise ValueError
        value = json_util.loads(
            raw, object_pairs_hook=_object, parse_constant=_constant
        )
        required = {
            "format",
            "version",
            "created_at",
            "complete",
            "database",
            "replica",
            "credentials",
        }
        if (
            not isinstance(value, dict)
            or not required <= set(value)
            or set(value) - required - {"recovery_token"}
            or value["format"] != "tgdrive-backup"
            or type(value["version"]) is not int
            or value["version"] != 2
            or value["complete"] is not True
        ):
            raise ValueError
        _validate_timestamp(value["created_at"])
        database = value["database"]
        if sanitize_database(database) != database:
            raise ValueError
        original = snapshot_catalog(database)
        channel = original.get("storage_channel_id")
        if type(channel) is not int or not -(1 << 63) <= channel < 0:
            raise ValueError
        _credentials(value["credentials"])
        replica = value["replica"]
        if replica is not None:
            if not isinstance(replica, dict) or set(replica) != {"channel", "messages"}:
                raise ValueError
            mapping = replica["messages"]
            if not isinstance(mapping, dict) or any(
                not isinstance(source, str)
                or re.fullmatch(r"[1-9][0-9]*", source) is None
                or type(target) is not int
                or not 0 < target < 2**31
                for source, target in mapping.items()
            ):
                raise ValueError
            restored = remap_catalog(original, replica["channel"], mapping)
            if _mapping(original, restored) != replica:
                raise ValueError
            replica = restored
        result = {
            "format": "tgdrive-replica",
            "version": 2,
            "complete": True,
            "created_at": value["created_at"],
            "catalog": original,
            "replica_catalog": replica,
            "credentials": value["credentials"],
            "database": database,
        }
        if "recovery_token" in value:
            token = value["recovery_token"]
            if (
                not isinstance(token, str)
                or len(token) > 256
                or not _TOKEN.fullmatch(token)
            ):
                raise ValueError
            result["recovery_token"] = token
        return result
    except Exception:
        raise ValueError(
            "Recovery key or file validation failed; nothing changed"
        ) from None


def write_recovery_snapshot(backend, path, key, settings):
    from tgdrive.recovery.backup import BackupError
    from tgdrive.recovery.mongo_backup import file_checksum
    from tgdrive.replication.replica_settings import credentials_from_snapshot
    from tgdrive.replication.replica_state import atomic_private

    database = capture_database(backend)
    catalog = snapshot_catalog(database)
    payload = {
        "catalog": catalog,
        "replica_catalog": None,
        "database": database,
        "credentials": credentials_from_snapshot(settings, database),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    content = encode_recovery_file(payload, key.hex())
    if shutil.disk_usage(path.parent).free < len(content) + settings.min_free_bytes:
        raise BackupError(
            "Not enough temporary space to stage a recovery file; free space and retry"
        )
    atomic_private(path, content)
    return (
        catalog["revision"],
        {name: len(rows) for name, rows in database.items()},
        file_checksum(path),
    )
