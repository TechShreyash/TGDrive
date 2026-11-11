"""One authenticated recovery file: one database snapshot and channel mappings."""

from __future__ import annotations

import copy
import json
import math
import re
import secrets
import zlib
from functools import partial
from pathlib import Path

from bson import BSON, json_util
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tgdrive.storage.mongo_store import MongoStore, catalog_counters

MAGIC = b"TGDRIVE-BACKUP-2\n"
LEGACY_MAGICS = (b"TGDRIVE-MONGO-1\n", b"TGDRIVE-REPLICA-1\n")
MAX_EXPANDED_BYTES = 16 * 1024**2
MAX_RECOVERY_BYTES = MAX_EXPANDED_BYTES + 64 * 1024
MAX_STAGED_RECOVERY_BYTES = 2000 * 1024**2
MAX_DATABASE_BYTES = MAX_EXPANDED_BYTES // 2
MAX_DATABASE_RECORDS = 10_000
_TOKEN = re.compile(r"[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}")


class LegacyRecoveryFile(ValueError):
    """An older drive needs its version-matched restore tool."""

    def __init__(self, detail=None):
        super().__init__(
            detail
            or "This recovery format requires tagged v2.2 tooling. Preserve "
            "the original archive and key; restore into isolated MongoDB, then upgrade."
        )


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
    return result


def snapshot_catalog(records):
    """Derive file locations from the captured database, never a later live read."""
    from tgdrive.recovery.mongo_backup import COLLECTIONS

    if not isinstance(records, dict) or set(records) != set(COLLECTIONS):
        raise ValueError("Invalid recovery database collections")
    size = count = 0
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
            count += 1
            size += len(BSON.encode(row))
            if count > MAX_DATABASE_RECORDS or size > MAX_DATABASE_BYTES:
                raise ValueError(
                    "The recovery database exceeds its record or memory budget"
                )
    return MongoStore.validate_snapshot_records(records)


def capture_database(backend):
    """Read all supported collections from one MongoDB snapshot transaction."""
    from tgdrive.recovery.mongo_backup import COLLECTIONS

    with backend.client.start_session(snapshot=True) as session:
        records = {}
        size = count = 0
        for name in COLLECTIONS:
            records[name] = []
            if name == "sessions":
                continue
            for row in backend.db[name].find({}, session=session):
                size += len(BSON.encode(row))
                count += 1
                if size > MAX_DATABASE_BYTES or count > MAX_DATABASE_RECORDS:
                    raise ValueError(
                        "The recovery database exceeds its record or memory budget"
                    )
                records[name].append(row)
    records = sanitize_database(records)
    snapshot_catalog(records)
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
            **catalog_counters(catalog),
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
        or set(value) - required
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
    raw = bytearray()
    encoder = json.JSONEncoder(
        default=json_util.default, allow_nan=False, separators=(",", ":")
    )
    for part in encoder.iterencode(value):
        encoded = part.encode()
        if len(raw) + len(encoded) > MAX_EXPANDED_BYTES:
            raise ValueError(
                "The recovery file exceeds the 16 MiB expanded metadata limit"
            )
        raw.extend(encoded)
    nonce = secrets.token_bytes(12)
    encrypted = (
        MAGIC
        + nonce
        + AESGCM(bytes.fromhex(key.strip())).encrypt(
            nonce, zlib.compress(raw, level=6), MAGIC
        )
    )
    if len(encrypted) > MAX_RECOVERY_BYTES:
        raise ValueError("The encrypted recovery file exceeds its transport limit")
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

    if isinstance(content, bytes) and content.startswith(LEGACY_MAGICS):
        raise LegacyRecoveryFile
    try:
        if (
            not isinstance(content, bytes)
            or len(content) < len(MAGIC) + 28
            or len(content) > MAX_RECOVERY_BYTES
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
        raw = decoder.decompress(compressed, MAX_EXPANDED_BYTES + 1)
        if (
            len(raw) > MAX_EXPANDED_BYTES
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
        if (
            isinstance(database, dict)
            and isinstance(database.get("catalog"), list)
            and database["catalog"]
            and database["catalog"][0].get("schema_version") == 4
        ):
            raise LegacyRecoveryFile(
                "This is a TGDrive 2.2 recovery file. Preserve it and its original "
                "key; restore with tagged v2.2 into isolated MongoDB, then upgrade."
            )
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
    except LegacyRecoveryFile:
        raise
    except Exception:
        raise ValueError(
            "Recovery key or file validation failed; nothing changed"
        ) from None


def write_recovery_snapshot(backend, path, key, settings, *, outstanding_bytes=0):
    from tgdrive.recovery.snapshot_stream import write_recovery_snapshot as write_staged
    from tgdrive.recovery.staged_snapshot import validate_snapshot
    from tgdrive.storage.capacity import probe_for_directory

    return write_staged(
        backend,
        path,
        key,
        settings,
        validate_snapshot=partial(
            validate_snapshot,
            min_free_bytes=settings.min_free_bytes,
            probe_directory=probe_for_directory(settings, Path(path).parent),
            outstanding_bytes=outstanding_bytes,
        ),
        outstanding_bytes=outstanding_bytes,
        max_records=None,
        max_database_bytes=None,
        max_expanded_bytes=None,
        max_recovery_bytes=MAX_STAGED_RECOVERY_BYTES,
    )
