"""Explicit, replayable channel recovery activation at an owned startup boundary.

Preparation never replaces a running catalog. Apply must run after the data-dir
and Mongo owner locks, before opening catalog/jobs/upload handles. The existing
MongoDB state and the replica journal are preserved before replacement; restored
sessions are never activated.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import secrets
import stat
import unicodedata
from contextlib import closing
from pathlib import Path

from bson import BSON, json_util
from pydantic import SecretStr

from tgdrive.core.config import Settings
from tgdrive.recovery.staged_snapshot import RecoveryResourceLimit
from tgdrive.replication.replica_state import (
    PrivateRecoveryFileTooLarge,
    atomic_private,
    private_reader,
    read_legacy_value,
)
from tgdrive.settings.managed_settings import (
    ManagedClient,
    ManagedDocument,
    SettingsProblem,
    credential_id,
    read_document,
)
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import ROOT_ID, SCHEMA_VERSION

MANIFEST = "channel-activation.json"
_MAX_STAGED_PATH_BYTES = 4096
_PHASES = {"prepared", "preserved", "installing", "catalog", "configured", "applied"}
_MONGO_DOMAIN = ("nodes", "shares", "operations", "deletions", "preparations")
_MONGO_ARCHIVE = (
    *_MONGO_DOMAIN,
    "catalog",
    "settings",
    "jobs",
    "receipts",
    "bot_folders",
    "backups",
)


class ActivationError(SettingsProblem):
    def __init__(self, detail, *, code="RECOVERY_ACTIVATION_FAILED"):
        super().__init__(detail, code=code, status=409)


def _resource_error(reason):
    return ActivationError(
        reason + "; this resource rejection does not establish corruption. "
        "The record is preserved. Stop and preserve the "
        "old deployment. Separate fresh recovery requires an intact compatible "
        "archive. If none is available, no fallback is established; preserve this "
        "record. See docs/backup-recovery.md.",
        code="ACTIVATION_RESOURCE_LIMIT",
    )


def _raw(value):
    return json_util.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate activation field")
        result[key] = value
    return json_util.object_hook(
        result, json_options=json_util.JSONOptions(tz_aware=True)
    )


def _digest(value):
    return hashlib.sha256(_raw(value)).hexdigest()


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _document(credentials, revision):
    clients = [
        ManagedClient(
            id=credential_id("bot", token),
            label=f"Storage bot {index}",
            kind="bot",
            credential=SecretStr(token),
        )
        for index, token in enumerate(credentials["bot_tokens"], 1)
    ]
    # A restored channel has different access requirements. Old credentials are
    # retained for owner review, but no old bot/Premium session is auto-enabled.
    original = ManagedDocument.model_validate(credentials["managed_document"])
    identifiers = {row.id for row in clients}
    active = {row.credential.get_secret_value() for row in clients}
    for row in original.clients:
        if row.kind == "bot" and row.credential.get_secret_value() in active:
            continue
        identity = row.id
        if identity in identifiers:
            identity = (
                "recovered-"
                + hashlib.sha256(
                    (row.kind + row.credential.get_secret_value()).encode()
                ).hexdigest()[:24]
            )
        identifiers.add(identity)
        clients.append(
            row.model_copy(
                update={
                    "id": identity,
                    "enabled": False,
                    "runtime_name": None,
                }
            )
        )
    return ManagedDocument(
        revision=revision,
        api_id=credentials["api_id"],
        api_hash=SecretStr(credentials["api_hash"]),
        clients=clients,
    )


def _checked_settings(settings, catalog, credentials, document=None):
    restoring = document is None
    if document is None:
        document = _document(credentials, 1)
    rows = [
        row
        for kind in ("bot", "user")
        for row in document.clients
        if row.kind == kind and row.enabled
    ]
    value = settings.model_dump(by_alias=True)
    value.update(
        {
            "STORAGE_CHANNEL": catalog["storage_channel_id"],
            "API_ID": document.api_id,
            "API_HASH": document.api_hash,
            "BOT_TOKENS": ",".join(
                row.credential.get_secret_value() for row in rows if row.kind == "bot"
            ),
            "STRING_SESSIONS": ",".join(
                row.credential.get_secret_value() for row in rows if row.kind == "user"
            ),
            "SESSION_SECRET": credentials["session_secret"],
            "MAIN_BOT_TOKEN": None,  # nosec B105 # None clears the credential.
            "managed_revision": document.revision if document is not None else 0,
            "managed_client_ids": tuple(row.id for row in rows),
            "managed_client_labels": tuple(row.label for row in rows),
        }
    )
    if restoring:
        for filename, policy in credentials["preferences"].items():
            if filename == "performance-preferences.json":
                value.update({name.upper(): item for name, item in policy.items()})
    # Init values outrank managed/environment sources. This applies every Settings
    # validator and prevents model_copy from smuggling unvalidated credentials.
    return Settings(_env_file=None, **value)


def _staged_source(settings, descriptor):
    """Bind a private, completed validation index to its immutable file digest."""
    from tgdrive.recovery.staged_snapshot import StagedSnapshot

    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != {"path", "sha256"}
        or not isinstance(descriptor["path"], str)
        or not isinstance(descriptor["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", descriptor["sha256"]) is None
    ):
        raise ValueError("Invalid staged recovery descriptor")
    relative = descriptor["path"]
    if (
        len(relative) > _MAX_STAGED_PATH_BYTES
        or len(relative.encode("utf-8")) > _MAX_STAGED_PATH_BYTES
    ):
        raise _resource_error("Staged recovery path exceeds its 4096 UTF-8 byte limit")
    if (
        not relative
        or "\\" in relative
        or "\x00" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or Path(relative).is_absolute()
    ):
        raise ValueError("Unsafe staged recovery path")
    path = settings.resolved_data_dir
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError("Unsafe staged recovery path")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077:
            raise ValueError("Staged recovery index must be a private regular file")
        if hashlib.file_digest(source, "sha256").hexdigest() != descriptor["sha256"]:
            raise ValueError("Staged recovery index changed")
        snapshot = StagedSnapshot(path)
        after = path.stat(follow_symlinks=False)
        if snapshot.digest != descriptor["sha256"] or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("Staged recovery index changed")
    return snapshot


def _staged_catalog(snapshot, channel):
    if channel != snapshot.catalog["storage_channel_id"] and snapshot.header[
        "replica"
    ] != {"channel": channel}:
        raise ValueError("Staged recovery has no mapping for the requested channel")
    return {
        "schema_version": SCHEMA_VERSION,
        "drive_id": snapshot.catalog["drive_id"],
        "revision": snapshot.catalog["revision"],
        "backed_up_revision": snapshot.catalog["revision"],
        "storage_channel_id": channel,
    }


def _normalized_credentials(settings, catalog, credentials):
    from tgdrive.replication.replica_settings import (
        POLICIES,
        validate_recovery_settings,
    )

    if not isinstance(credentials, dict) or set(credentials) != {
        "api_id",
        "api_hash",
        "session_secret",
        "bot_tokens",
        "managed_document",
        "preferences",
    }:
        raise ValueError("Invalid recovery credentials")
    validate_recovery_settings(credentials)
    if type(credentials["api_id"]) is not int:
        raise ValueError("Invalid recovery API identity")
    if not isinstance(credentials["bot_tokens"], list) or not all(
        isinstance(token, str) for token in credentials["bot_tokens"]
    ):
        raise ValueError("Invalid recovery bot credentials")
    checked = _checked_settings(settings, catalog, credentials)
    return {
        "api_id": checked.api_id,
        "api_hash": checked.api_hash.get_secret_value(),
        "session_secret": checked.session_secret.get_secret_value(),
        "bot_tokens": list(checked.bot_tokens),
        "managed_document": copy.deepcopy(credentials["managed_document"]),
        "preferences": {
            name: POLICIES[name].model_validate(policy).model_dump()
            for name, policy in credentials["preferences"].items()
        },
    }


def _candidate(settings, candidate):
    try:
        if not isinstance(candidate, dict) or candidate.get("complete") is not True:
            raise ValueError
        catalog = candidate["catalog"]
        if (
            not isinstance(catalog, dict)
            or catalog.get("schema_version") != SCHEMA_VERSION
        ):
            raise ValueError
        if (
            type(catalog.get("storage_channel_id")) is not int
            or catalog["storage_channel_id"] >= 0
        ):
            raise ValueError
        staged = None
        if "staged" in candidate:
            if "database" in candidate:
                raise ValueError("Recovery candidate contains two database sources")
            staged = _staged_source(settings, candidate["staged"])
            if catalog != _staged_catalog(staged, catalog["storage_channel_id"]):
                raise ValueError("Staged recovery metadata does not match its index")
            with closing(staged.records("settings")) as rows:
                check_recovery_key(settings, {"settings": rows})
            with closing(staged.records("jobs")) as rows:
                for row in rows:
                    _history([row])
        else:
            MongoStore._validate_payload(catalog)
        catalog = copy.deepcopy(catalog)
        # Never restore source-channel mutation receipts, including preparation
        # request IDs which could accidentally replay from an old browser tab.
        if staged is None:
            catalog["operations"] = []
            catalog["deletions"] = []
            catalog.pop("folder_preparations", None)
        credentials = _normalized_credentials(
            settings, catalog, candidate["credentials"]
        )
        if staged is not None and credentials != _normalized_credentials(
            settings, catalog, staged.header["credentials"]
        ):
            raise ValueError("Staged recovery credentials do not match its index")
        token, key = candidate["token"], candidate["key"]
        _document(dict(credentials, bot_tokens=[token]), 1)
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise ValueError
        result = {
            "catalog": catalog,
            "credentials": credentials,
            "complete": True,
            "token": token,
            "key": key.lower(),
        }
        if staged is not None:
            result["staged"] = copy.deepcopy(candidate["staged"])
        if "database" in candidate:
            result["database"] = _database(candidate["database"], catalog)
            check_recovery_key(settings, result["database"])
        if "backup_config" in candidate:
            config = candidate["backup_config"]
            if (
                not isinstance(config, dict)
                or set(config) != {"channel", "token"}
                or type(config["channel"]) is not int
                or not -(1 << 63) <= config["channel"] < 0
                or config["channel"] == catalog["storage_channel_id"]
                or not isinstance(config["token"], str)
                or re.fullmatch(r"[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}", config["token"])
                is None
            ):
                raise ValueError
            result["backup_config"] = copy.deepcopy(config)
        return result
    except ActivationError:
        raise
    except MemoryError:
        raise _resource_error(
            "Pending activation could not fit available memory"
        ) from None
    except Exception:
        raise ActivationError(
            "Recovery activation data is invalid; nothing changed."
        ) from None


def check_recovery_key(settings, database):
    """A capsule key never rotates the deployment's encrypted-file key."""
    if database is None:
        return  # Version-1 catalog-only activations contain legacy plaintext parts.
    from tgdrive.core.encryption import fingerprint

    expected = next(
        (
            row.get("document", {}).get("fingerprint")
            for row in database.get("settings", [])
            if row.get("_id") == "encryption"
        ),
        None,
    )
    if expected != fingerprint(settings.encryption_key):
        raise ActivationError(
            "ENCRYPTION_KEY does not match the recovered drive. Restore the original "
            "deployment key before recovery; no catalog or settings were replaced."
        )


def _database(records, catalog):
    from tgdrive.recovery.recovery_file import sanitize_database, snapshot_catalog
    from tgdrive.replication.replica_state import files_in, parts_in, remap_catalog

    records = sanitize_database(records)
    source = snapshot_catalog(records)
    if (source["drive_id"], source["revision"]) != (
        catalog["drive_id"],
        catalog["revision"],
    ):
        raise ValueError(
            "Recovery database and catalog describe different restore points"
        )
    originals = {node["id"]: node for node in files_in(source)}
    restored = {node["id"]: node for node in files_in(catalog)}
    if set(originals) != set(restored):
        raise ValueError("Recovery database and catalog contain different files")
    mapping = {}
    for identity, node in originals.items():
        for before, after in zip(
            parts_in(node), parts_in(restored[identity]), strict=True
        ):
            message, replacement = str(before["message_id"]), after["message_id"]
            if message in mapping and mapping[message] != replacement:
                raise ValueError("Conflicting recovery database location")
            mapping[message] = replacement
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Duplicate recovery database destination")
    expected = remap_catalog(source, catalog["storage_channel_id"], mapping)
    expected.pop("folder_preparations", None)
    if expected != catalog:
        raise ValueError("Recovery database and catalog contain different metadata")
    for name in (
        "sessions",
        "identities",
        "operations",
        "deletions",
        "preparations",
        "receipts",
        "backups",
    ):
        records[name] = []
    records["jobs"] = _history(records["jobs"])
    return records


def _history(rows):
    from tgdrive.transfers.tasks import TransferRegistry, TransferStatus

    history = []
    registry = TransferRegistry()
    for row in rows:
        if row.get("archived") is True:
            if not isinstance(row.get("summary"), dict):
                raise ValueError("Invalid archived transfer history")
            history.append(copy.deepcopy(row))
            continue
        registry._load_record(row["_id"], copy.deepcopy(row["data"]))
        record = registry.get(row["_id"])
        if record.history_hidden or record.context.get("kind") == "delete":
            continue
        if record.status not in {TransferStatus.COMPLETED, TransferStatus.CANCELLED}:
            record.status = TransferStatus.CANCELLED
            record.phase = "cancelled"
            record.finished_at = record.updated_at
        summary = registry.history_snapshot(record, retained=False)
        history.append(
            {
                "_id": record.id,
                "archived": True,
                "created_at": record.created_at,
                "status": record.status.value,
                "search_name": unicodedata.normalize("NFC", summary["name"]).casefold(),
                "summary": summary,
            }
        )
    return history


def _mongo_empty(backend, session):
    root = backend.db.nodes.find_one({"_id": ROOT_ID}, session=session)
    return (
        bool(root and root.get("kind") == "folder" and root.get("parent_id") is None)
        and backend.db.nodes.count_documents({}, session=session) == 1
        and not any(
            backend.db[name].find_one({}, session=session)
            for name in _MONGO_DOMAIN
            if name != "nodes"
        )
    )


def _mongo_state(backend):
    def read(session):
        metadata = backend.db.catalog.find_one({"_id": "catalog"}, session=session)
        if metadata is None:
            if any(
                backend.db[name].find_one({}, session=session) for name in _MONGO_DOMAIN
            ):
                raise ActivationError(
                    "MongoDB contains orphaned catalog state; preserve and repair "
                    "it first."
                )
            return {"kind": "missing"}
        return {
            "kind": "mongodb",
            "drive_id": metadata.get("drive_id"),
            "revision": metadata.get("revision"),
            "channel": metadata.get("storage_channel_id"),
            "digest": _digest(metadata),
            "empty": _mongo_empty(backend, session),
        }

    return backend.transaction(read)


def _source(settings, backend=None):
    if backend is None:
        from tgdrive.storage.mongo import for_settings

        backend = for_settings(settings)
    return _mongo_state(backend)


def _compatible(expected, catalog):
    if (
        expected["kind"] == "mongodb"
        and not expected["empty"]
        and expected["drive_id"] != catalog["drive_id"]
    ):
        raise ActivationError(
            "This installation contains a different drive. "
            "Use a separate data directory."
        )


def _manifest_path(settings):
    return settings.resolved_data_dir / MANIFEST


def _activation_identity(value):
    fields = (
        "candidate",
        "expected",
        "settings_revision",
        "settings_sha256",
        "backend",
        "mongodb_key_before_sha256",
    )
    return _digest({name: value[name] for name in fields})


def _load(settings):
    path = _manifest_path(settings)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        with private_reader(path) as source:
            value = read_legacy_value(source)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "version",
                "id",
                "candidate",
                "expected",
                "phase",
                "settings_revision",
                "settings_sha256",
                "backend",
                "mongodb_key_before_sha256",
            }
            or value["version"] != 1
            or value["phase"] not in _PHASES
        ):
            raise ValueError
        checked = _candidate(settings, value["candidate"])
        if checked != value["candidate"] or value["backend"] != "mongodb":
            raise ValueError
        fingerprint = value["mongodb_key_before_sha256"]
        if fingerprint is not None and (
            not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        ):
            raise ValueError
        identity = _activation_identity(value)
        if value["id"] != identity or not isinstance(value["expected"], dict):
            raise ValueError
        if (
            type(value["settings_revision"]) is not int
            or value["settings_revision"] < 0
        ):
            raise ValueError
        return value
    except PrivateRecoveryFileTooLarge as error:
        raise _resource_error(
            f"Pending activation exceeds the {error.maximum_bytes // 1024**2} MiB "
            "compatibility admission limit"
        ) from None
    except RecoveryResourceLimit:
        raise _resource_error(
            "Pending activation exceeds provisional decoding limits"
        ) from None
    except MemoryError:
        raise _resource_error(
            "Pending activation could not fit available memory"
        ) from None
    except Exception as error:
        if (
            isinstance(error, ActivationError)
            and error.code == "ACTIVATION_RESOURCE_LIMIT"
        ):
            raise
        raise ActivationError(
            "The private activation record is invalid. Preserve it and repeat "
            "recovery inspection."
        ) from None


def _write(settings, value):
    try:
        content = _raw(value)
        with io.BytesIO(content) as source:
            read_legacy_value(source)
    except PrivateRecoveryFileTooLarge, RecoveryResourceLimit, MemoryError:
        raise _resource_error(
            "New activation record exceeds provisional decoding limits"
        ) from None
    atomic_private(_manifest_path(settings), content)


def prepare_activation(settings, candidate):
    """Persist intent only; current catalog/configuration remain untouched."""
    checked = _candidate(settings, candidate)
    existing = _load(settings)
    if existing and existing["phase"] != "applied":
        if existing["candidate"] == checked:
            return existing["id"]
        raise ActivationError(
            "Another activation is pending. Restart and finish it before preparing "
            "another."
        )
    expected = _source(settings)
    _compatible(expected, checked["catalog"])
    document = read_document(settings.resolved_data_dir)
    value = {
        "version": 1,
        "candidate": checked,
        "expected": expected,
        "settings_revision": document.revision if document else 0,
        "settings_sha256": hashlib.sha256(document.private_bytes()).hexdigest()
        if document
        else None,
        "backend": "mongodb",
        "mongodb_key_before_sha256": None,
        "phase": "prepared",
    }
    value["id"] = _activation_identity(value)
    if existing:
        atomic_private(
            settings.resolved_data_dir / "recovery-archive" / existing["id"] / MANIFEST,
            _raw(existing),
        )
    _write(settings, value)
    return value["id"]


def effective_settings(settings):
    """Use recovery credentials before constructing pools and session managers."""
    value = _load(settings)
    if value is None:
        return settings
    candidate = value["candidate"]
    try:
        document = None
        applied = value["phase"] == "applied"
        if applied:
            document = read_document(settings.resolved_data_dir)
        return _checked_settings(
            settings,
            candidate["catalog"],
            candidate["credentials"],
            document,
        )
    except DatabaseUnavailable:
        raise
    except Exception:
        raise ActivationError(
            "The activated connection settings are invalid. Preserve local state "
            "and repair the connection."
        ) from None


def _copy_file(source, destination):
    if source.is_symlink() or destination.is_symlink():
        raise ActivationError("Recovery preservation encountered an unsafe file path.")
    if destination.exists():
        return
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    temporary = destination.with_name(
        "." + destination.name + "-" + secrets.token_hex(12)
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise ActivationError("Recovery preservation requires regular files.")
        with (
            os.fdopen(source_fd, "rb", closefd=False) as original,
            os.fdopen(fd, "wb") as target,
        ):
            while chunk := original.read(1024**2):
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
        _sync(destination.parent)
    finally:
        os.close(source_fd)
        temporary.unlink(missing_ok=True)


def _archive_dir(directory, identity):
    archive = directory
    for name in ("recovery-archive", identity):
        archive = archive / name
        if archive.is_symlink():
            raise ActivationError(
                "The recovery archive path is unsafe; nothing changed."
            )
    return archive


def _preserve_mongo(backend, archive):
    destination = archive / "mongodb-before.json"
    if destination.exists() or destination.is_symlink():
        # This archive was published with fsync + replace. Replaying preservation
        # only needs the same private-file checks, never a whole-archive read.
        fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ActivationError("Recovery preservation requires private files.")
        finally:
            os.close(fd)
        return
    temporary = archive / (".mongodb-preserve-" + secrets.token_hex(12))

    def snapshot(session):
        backend.fence(session)
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as target:

            def write(content):
                target.write(content)

            write(b"{")
            for index, name in enumerate(_MONGO_ARCHIVE):
                if index:
                    write(b",")
                write(json.dumps(name).encode() + b":[")
                for row_index, row in enumerate(
                    backend.db[name].find({}, session=session)
                ):
                    if row_index:
                        write(b",")
                    write(json_util.dumps(row, separators=(",", ":")).encode())
                write(b"]")
            write(b"}")
            target.flush()
            os.fsync(target.fileno())

    try:
        backend.transaction(snapshot)
        os.replace(temporary, destination)
        _sync(archive)
    finally:
        temporary.unlink(missing_ok=True)


def _preserve(settings, value, backend):
    directory = settings.resolved_data_dir
    archive = _archive_dir(directory, value["id"])
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_private(archive / MANIFEST, _raw(value))
    _preserve_mongo(backend, archive)
    return archive


def _mongo_nodes(catalog):
    from tgdrive.storage.mongo_store import mongo_nodes

    return mongo_nodes(catalog)


def _inline_nodes(catalog, database):
    # Legacy inline candidates are admission-bounded; keep catalog ID rebasing.
    positions = {
        row["_id"]: row["video_progress"]
        for row in (database or {}).get("nodes", [])
        if "video_progress" in row
    }
    for node in _mongo_nodes(catalog):
        if node["_id"] in positions:
            node["video_progress"] = positions[node["_id"]]
        yield node


def _database_nodes(rows):
    from tgdrive.storage.video_progress import validate_video_state

    for row in rows:
        validate_video_state(row)
        # Rebuild derived indexes; saved writer sessions never survive activation.
        values = {key: row[key] for key in ("upload_date", "trashed")}
        if row.get("starred"):
            values["starred"] = True
        if row["kind"] == "file":
            values.update(message_id=row["message_id"], size=row["size"])
            for key in ("parts", "sha256"):
                if row.get(key):
                    values[key] = row[key]
            if "video_progress" in row:
                values["video_progress"] = row["video_progress"]
            if row.get("poster") is not None:
                values["poster"] = row["poster"]
        values["part_count"] = len(values.get("parts") or [None])
        yield MongoStore._node(
            row["_id"], row["parent_id"], row["name"], row["kind"], **values
        )


def _staged_nodes(snapshot, channel):
    remap = channel if channel != snapshot.catalog["storage_channel_id"] else None
    with closing(snapshot.nodes(remap)) as rows:
        yield from _database_nodes(rows)


def _insert_rows(collection, rows, session):
    """Bound each recovery write batch by document count and encoded bytes."""
    batch, size = [], 0
    iterator = iter(rows)
    try:
        for row in iterator:
            encoded_bytes = len(BSON.encode(row))
            if batch and (len(batch) >= 256 or size + encoded_bytes > 8 * 1024**2):
                collection.insert_many(batch, session=session)
                batch, size = [], 0
            batch.append(row)
            size += encoded_bytes
        if batch:
            collection.insert_many(batch, session=session)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


def _apply_mongo(settings, value, backend):
    candidate = value["candidate"]
    desired = _document(candidate["credentials"], value["settings_revision"] + 1)
    connection_document = json.loads(desired.private_bytes())
    database = candidate.get("database")
    staged = (
        _staged_source(settings, candidate["staged"]) if "staged" in candidate else None
    )
    full_database = database is not None or staged is not None

    def rows(name):
        if staged is not None:
            with closing(staged.records(name)) as source:
                for row in source:
                    if name == "jobs":
                        yield from _history([row])
                    elif name == "shares":
                        yield dict(row, _id=row["id"])
                    else:
                        yield row
        else:
            assert database is not None
            yield from database[name]

    owner_hash = None
    if full_database:
        from argon2 import PasswordHasher

        owner_hash = (
            settings.admin_password_hash.get_secret_value()
            if settings.admin_password_hash
            else PasswordHasher().hash(settings.admin_password.get_secret_value())
        )

    from tgdrive.storage.mongo_store import catalog_counters

    def replace(session):
        backend.fence(session)
        before = backend.db.catalog.find_one({"_id": "catalog"}, session=session)
        if before and before.get("replica_activation_id") == value["id"]:
            return
        # Read and compare inside the same fenced transaction as every replace.
        if before is None:
            current = {"kind": "missing"}
        else:
            current = {
                "kind": "mongodb",
                "drive_id": before.get("drive_id"),
                "revision": before.get("revision"),
                "channel": before.get("storage_channel_id"),
                "digest": _digest(before),
                "empty": _mongo_empty(backend, session),
            }
        if current != value["expected"]:
            raise ActivationError(
                "MongoDB metadata changed after recovery was prepared; nothing was "
                "replaced."
            )
        current_settings = backend.db.settings.find_one(
            {"_id": "telegram"}, session=session
        )
        if (current_settings["revision"] if current_settings else 0) != value[
            "settings_revision"
        ]:
            raise ActivationError(
                "Connection settings changed after recovery was prepared; nothing "
                "was replaced."
            )
        settings_digest = (
            hashlib.sha256(
                ManagedDocument.model_validate(
                    current_settings["document"]
                ).private_bytes()
            ).hexdigest()
            if current_settings
            else None
        )
        if settings_digest != value["settings_sha256"]:
            raise ActivationError("Connection settings changed; nothing was replaced.")
        if full_database:
            for name in ("settings", "sessions", "identities"):
                backend.db[name].delete_many({}, session=session)
            _insert_rows(backend.db.settings, rows("settings"), session)
        for name in (*_MONGO_DOMAIN, "jobs", "receipts", "bot_folders", "backups"):
            backend.db[name].delete_many({}, session=session)
        if full_database:
            for name in ("jobs", "bot_folders"):
                _insert_rows(backend.db[name], rows(name), session)
        nodes = (
            _staged_nodes(staged, candidate["catalog"]["storage_channel_id"])
            if staged is not None
            else _inline_nodes(candidate["catalog"], database)
        )
        _insert_rows(backend.db.nodes, nodes, session)
        shares = (
            rows("shares")
            if staged is not None
            else (
                dict(row, _id=row["id"])
                for row in candidate["catalog"].get("shares", [])
            )
        )
        _insert_rows(backend.db.shares, shares, session)
        metadata = {
            "_id": "catalog",
            "schema_version": SCHEMA_VERSION,
            "drive_id": candidate["catalog"]["drive_id"],
            "revision": candidate["catalog"]["revision"],
            "backed_up_revision": candidate["catalog"]["revision"],
            "storage_channel_id": candidate["catalog"]["storage_channel_id"],
            "generation": (before or {}).get("generation", 0) + 1,
            "replica_activation_id": value["id"],
            **(
                staged.counters
                if staged is not None
                else catalog_counters(candidate["catalog"])
            ),
        }
        backend.db.catalog.replace_one(
            {"_id": "catalog"}, metadata, upsert=True, session=session
        )
        backend.db.settings.replace_one(
            {"_id": "telegram"},
            {
                "_id": "telegram",
                "revision": desired.revision,
                "document": connection_document,
            },
            upsert=True,
            session=session,
        )
        for filename, policy in candidate["credentials"]["preferences"].items():
            identity = filename.removesuffix(".json")
            backend.db.settings.replace_one(
                {"_id": identity},
                {"_id": identity, "document": policy},
                upsert=True,
                session=session,
            )
        bootstrap = backend.db.settings.find_one({"_id": "bootstrap"}, session=session)
        if full_database and bootstrap is None:
            bootstrap = {"_id": "bootstrap", "document": {}}
        if bootstrap:
            bootstrap["document"].update(
                {
                    "STORAGE_CHANNEL": metadata["storage_channel_id"],
                    "SESSION_SECRET": candidate["credentials"]["session_secret"],
                }
            )
            if full_database:
                bootstrap["document"].update(
                    {
                        "API_ID": desired.api_id,
                        "API_HASH": desired.api_hash.get_secret_value(),
                        "BOT_TOKENS": ",".join(candidate["credentials"]["bot_tokens"]),
                        "STRING_SESSIONS": "",
                        "MAIN_BOT_TOKEN": None,  # nosec B105 - token reset
                        "ADMIN_PASSWORD_HASH": owner_hash,
                    }
                )
                bootstrap["document"].pop("ADMIN_PASSWORD", None)
            backend.db.settings.replace_one(
                {"_id": "bootstrap"}, bootstrap, upsert=True, session=session
            )

    backend.transaction(replace)
    with backend._settings_lock:
        backend._settings_cached = False
        backend._settings_document = None


def _check_connections(settings, value):
    current = read_document(settings.resolved_data_dir)
    revision = current.revision if current else 0
    digest = hashlib.sha256(current.private_bytes()).hexdigest() if current else None
    if revision != value["settings_revision"] or digest != value["settings_sha256"]:
        raise ActivationError(
            "Connection settings changed after recovery was prepared; "
            "nothing was replaced. Inspect recovery again."
        )


def apply_activation(settings, mongo_backend=None):
    """Apply once under startup ownership; any interrupted step is safe to replay."""
    value = _load(settings)
    if value is None or value["phase"] == "applied":
        return False
    if mongo_backend is None:
        raise ActivationError(
            "The configured recovery database is unavailable; no fallback was "
            "attempted."
        )
    if value["phase"] == "prepared":
        _check_connections(settings, value)
        if _source(settings, mongo_backend) != value["expected"]:
            raise ActivationError(
                "Local metadata changed after recovery was prepared; nothing was "
                "replaced."
            )
        _preserve(settings, value, mongo_backend)
        value["phase"] = "preserved"
        _write(settings, value)
    if value["phase"] == "preserved":
        _check_connections(settings, value)
        if _source(settings, mongo_backend) != value["expected"]:
            raise ActivationError(
                "Local metadata changed during recovery preparation; nothing was "
                "replaced."
            )
        # This durable boundary authorizes replay after an interrupted MongoDB
        # transaction, with the previous state preserved above.
        value["phase"] = "installing"
        _write(settings, value)
    if value["phase"] == "installing":
        _apply_mongo(settings, value, mongo_backend)
        value["phase"] = "catalog"
        _write(settings, value)
    if value["phase"] == "catalog":
        value["phase"] = "configured"
        _write(settings, value)
    value["phase"] = "applied"
    _write(settings, value)
    return True
