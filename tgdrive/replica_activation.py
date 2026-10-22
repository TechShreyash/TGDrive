"""Explicit, replayable channel recovery activation at an owned startup boundary.

Preparation never replaces a running catalog. Apply must run after the data-dir
and Mongo owner locks, before opening catalog/jobs/upload handles. The existing
auth.sqlite3 and replica journal are retained; browser sessions are never imported.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from pydantic import SecretStr

from tgdrive.backup import TelegramBackupService
from tgdrive.config import Settings
from tgdrive.managed_settings import (
    MAX_CLIENTS,
    ManagedClient,
    ManagedDocument,
    SettingsProblem,
    credential_id,
    read_document,
    save_document,
)
from tgdrive.replica_state import MAX_BUNDLE, atomic_private, read_private
from tgdrive.store import ROOT_ID, SCHEMA_VERSION, DriveStore

MANIFEST = "channel-activation.json"
_PHASES = {"prepared", "preserved", "installing", "catalog", "configured", "applied"}
_LOCAL_JOURNALS = ("jobs.sqlite3", "uploads.sqlite3", "bot-settings.sqlite3")
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
    def __init__(self, detail):
        super().__init__(detail, code="RECOVERY_ACTIVATION_FAILED", status=409)


def _raw(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


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
    historical = credentials.get("managed_document")
    if historical:
        original = ManagedDocument.model_validate(historical)
        identifiers = {row.id for row in clients}
        active = {row.credential.get_secret_value() for row in clients}
        for row in original.clients:
            if row.kind == "bot" and row.credential.get_secret_value() in active:
                continue
            if len(clients) >= MAX_CLIENTS:
                # The full original remains in the private manifest/capsule.
                break
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


def _checked_settings(
    settings, catalog, credentials, document=None, *, restore_mongodb_key=True
):
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
            "MAIN_BOT_TOKEN": None,  # nosec B105 # clear old optional bot
            "DATABASE_BACKUP_MSG_ID": None,
            "managed_revision": document.revision if document is not None else 0,
            "managed_client_ids": tuple(row.id for row in rows),
            "managed_client_labels": tuple(row.label for row in rows),
        }
    )
    if restore_mongodb_key and "mongodb_backup_key" in credentials:
        value["MONGODB_BACKUP_KEY"] = credentials["mongodb_backup_key"]
    if restoring:
        for filename, policy in credentials.get("preferences", {}).items():
            if filename == "transfer-preferences.json":
                value["TELEGRAM_UPLOADS_PER_BOT"] = policy["uploads_per_bot"]
            else:
                value.update({name.upper(): item for name, item in policy.items()})
    # Init values outrank managed/environment sources. This applies every Settings
    # validator and prevents model_copy from smuggling unvalidated credentials.
    return Settings(_env_file=None, **value)


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
        validated = DriveStore._from_payload(catalog)
        try:
            catalog = validated.export()
        finally:
            validated.close()
        # Never restore source-channel mutation receipts, including preparation
        # request IDs which could accidentally replay from an old browser tab.
        catalog["operations"] = []
        catalog["deletions"] = []
        catalog.pop("folder_preparations", None)
        credentials = candidate["credentials"]
        required = {"api_id", "api_hash", "session_secret", "bot_tokens"}
        optional = {
            "managed_document",
            "preferences",
            "mongodb_backup_key",
            "mongodb_archive",
        }
        if (
            not isinstance(credentials, dict)
            or not required <= set(credentials)
            or set(credentials) - required - optional
        ):
            raise ValueError
        from tgdrive.replica_settings import POLICIES, validate_optional_credentials

        validate_optional_credentials(credentials)
        extras = {
            name: copy.deepcopy(credentials[name])
            for name in optional
            if name in credentials
        }
        if "preferences" in extras:
            extras["preferences"] = {
                name: POLICIES[name].model_validate(policy).model_dump()
                for name, policy in extras["preferences"].items()
            }
        if "mongodb_backup_key" in extras:
            extras["mongodb_backup_key"] = extras["mongodb_backup_key"].lower()
        if type(credentials["api_id"]) is not int:
            raise ValueError
        if not isinstance(credentials["bot_tokens"], list) or not all(
            isinstance(token, str) for token in credentials["bot_tokens"]
        ):
            raise ValueError
        checked = _checked_settings(settings, catalog, credentials)
        credentials = {
            "api_id": checked.api_id,
            "api_hash": checked.api_hash.get_secret_value(),
            "session_secret": checked.session_secret.get_secret_value(),
            "bot_tokens": list(checked.bot_tokens),
            **extras,
        }
        token, key = candidate["token"], candidate["key"]
        _document(dict(credentials, bot_tokens=[token]), 1)
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise ValueError
        return {
            "catalog": catalog,
            "credentials": credentials,
            "complete": True,
            "token": token,
            "key": key.lower(),
        }
    except Exception:
        raise ActivationError(
            "Recovery activation data is invalid; nothing changed."
        ) from None


def _file_hash(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ActivationError("Recovery requires regular local state files.")
        with os.fdopen(fd, "rb", closefd=False) as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    finally:
        os.close(fd)


def _sqlite_state(directory):
    path = directory / "drive.sqlite3"
    if path.is_symlink():
        raise ActivationError("The local catalog path is unsafe; nothing changed.")
    files = [Path(str(path) + suffix) for suffix in ("", "-wal", "-shm")]
    if not any(p.exists() for p in files):
        return {"kind": "missing"}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if (
                db.execute("PRAGMA user_version").fetchone()[0]
                != DriveStore.DATABASE_VERSION
            ):
                raise ValueError
            store = DriveStore(db, None)
            catalog = store.export()
            validated = DriveStore._from_payload(catalog)
            validated.close()
            return {
                "kind": "sqlite",
                "drive_id": catalog["drive_id"],
                "revision": catalog["revision"],
                "channel": catalog.get("storage_channel_id"),
                "digest": _digest(catalog),
                "empty": not catalog["root"]["children"]
                and not any(
                    catalog.get(name)
                    for name in (
                        "shares",
                        "operations",
                        "deletions",
                        "folder_preparations",
                    )
                ),
            }
    except Exception:
        return {
            "kind": "corrupt",
            "files": {p.name: _file_hash(p) for p in files if p.exists()},
        }


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
    if settings.mongodb_uri:
        if backend is None:
            from tgdrive.mongo import for_settings

            backend = for_settings(settings)
        if backend is None:
            raise ActivationError(
                "MongoDB recovery requires the configured database; SQLite is not a "
                "fallback."
            )
        return _mongo_state(backend)
    if backend is not None:
        raise ActivationError("Recovery backend does not match the installation.")
    return _sqlite_state(settings.resolved_data_dir)


def _compatible(expected, catalog):
    if (
        expected["kind"] in {"sqlite", "mongodb"}
        and not expected["empty"]
        and expected["drive_id"] != catalog["drive_id"]
    ):
        raise ActivationError(
            "This installation contains a different drive. "
            "Use a separate data directory."
        )


def _manifest_path(settings):
    return settings.resolved_data_dir / MANIFEST


def _mongodb_key_fingerprint(settings):
    key = settings.mongodb_backup_key
    return (
        hashlib.sha256(bytes.fromhex(key.get_secret_value())).hexdigest()
        if key is not None
        else None
    )


def _activation_identity(value):
    fields = (
        "candidate",
        "expected",
        "settings_revision",
        "settings_sha256",
        "backend",
    )
    if "mongodb_key_before_sha256" in value:
        fields += ("mongodb_key_before_sha256",)
    return _digest({name: value[name] for name in fields})


def _load(settings):
    path = _manifest_path(settings)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        value = TelegramBackupService._decode_snapshot(read_private(path))
        if (
            not isinstance(value, dict)
            or set(value)
            not in (
                {
                    "version",
                    "id",
                    "candidate",
                    "expected",
                    "phase",
                    "settings_revision",
                    "settings_sha256",
                    "backend",
                },
                {
                    "version",
                    "id",
                    "candidate",
                    "expected",
                    "phase",
                    "settings_revision",
                    "settings_sha256",
                    "backend",
                    "mongodb_key_before_sha256",
                },
            )
            or value["version"] != 1
            or value["phase"] not in _PHASES
        ):
            raise ValueError
        checked = _candidate(settings, value["candidate"])
        if checked != value["candidate"] or value["backend"] not in {
            "sqlite",
            "mongodb",
        }:
            raise ValueError
        if "mongodb_key_before_sha256" in value:
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
        if (value["backend"] == "mongodb") != bool(settings.mongodb_uri):
            raise ValueError
        return value
    except Exception:
        raise ActivationError(
            "The private activation record is invalid. Preserve it and repeat "
            "recovery inspection."
        ) from None


def _write(settings, value):
    content = _raw(value)
    if len(content) > MAX_BUNDLE:
        raise ActivationError("Recovery activation exceeds the 64 MiB limit.")
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
        "backend": "mongodb" if settings.mongodb_uri else "sqlite",
        "mongodb_key_before_sha256": _mongodb_key_fingerprint(settings),
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
            try:
                document = read_document(settings.resolved_data_dir)
            except SettingsProblem:
                if not settings.mongodb_uri:
                    raise
                # The immutable, validated candidate supplies connection data
                # while MongoDB is unavailable. Runtime still owns admission;
                # this does not install a catalog or select a local backend.
        fingerprint = _mongodb_key_fingerprint(settings)
        rotated = (
            applied
            and "mongodb_key_before_sha256" in value
            and fingerprint is not None
            and fingerprint != value["mongodb_key_before_sha256"]
        )
        return _checked_settings(
            settings,
            candidate["catalog"],
            candidate["credentials"],
            document,
            # Before activation, the capsule key always wins. Afterwards, a
            # changed deployment key is an intentional rotation; an unchanged
            # old key or missing key must not undo recovery. Legacy manifests
            # lack this baseline and conservatively retain the capsule key.
            restore_mongodb_key=not rotated,
        )
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
    from bson import json_util

    destination = archive / "mongodb-before.json"
    if destination.exists() or destination.is_symlink():
        read_private(destination)
        return
    temporary = archive / (".mongodb-preserve-" + secrets.token_hex(12))

    def snapshot(session):
        backend.fence(session)
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        total = 0
        with os.fdopen(fd, "wb") as target:

            def write(content):
                nonlocal total
                total += len(content)
                if total > MAX_BUNDLE:
                    raise ActivationError(
                        "MongoDB preservation exceeds the 64 MiB recovery limit."
                    )
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
    for filename in (
        "drive.sqlite3",
        *_LOCAL_JOURNALS,
        "auth.sqlite3",
        "telegram-settings.json",
        "performance-preferences.json",
        "transfer-preferences.json",
    ):
        for suffix in ("", "-wal", "-shm") if filename.endswith(".sqlite3") else ("",):
            path = directory / (filename + suffix)
            if path.exists() or path.is_symlink():
                _copy_file(path, archive / path.name)
    if backend is not None:
        _preserve_mongo(backend, archive)
    old_history = directory / "backups"
    if old_history.exists():
        if old_history.is_symlink() or (archive / "backups").exists():
            raise ActivationError(
                "Checkpoint history preservation needs attention; nothing was "
                "discarded."
            )
        os.replace(old_history, archive / "backups")
        _sync(directory)
        _sync(archive)
    return archive


def _sqlite_marker(directory):
    path = directory / "drive.sqlite3"
    if not path.exists() or path.is_symlink():
        return None
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute(
                "SELECT value FROM drive_settings WHERE key='replica_activation_id'"
            ).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def _apply_sqlite(settings, value):
    directory = settings.resolved_data_dir
    if _sqlite_marker(directory) != value["id"]:
        staged = directory / (".recovered-" + value["id"] + ".sqlite3")
        if staged.exists():
            for suffix in ("", "-wal", "-shm"):
                Path(str(staged) + suffix).unlink(missing_ok=True)
        store = DriveStore._from_payload(
            value["candidate"]["catalog"], storage_path=staged, persist=True
        )
        try:
            with store._transaction(write=True) as db:
                db.execute(
                    "INSERT INTO drive_settings VALUES('replica_activation_id',?)",
                    (value["id"],),
                )
        finally:
            store.close()
        fd = os.open(staged, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # All old catalog/journal bytes were preserved before removing sidecars.
        for filename in ("drive.sqlite3", *_LOCAL_JOURNALS):
            for suffix in (
                ("-wal", "-shm")
                if filename == "drive.sqlite3"
                else ("", "-wal", "-shm")
            ):
                path = directory / (filename + suffix)
                if path.is_symlink():
                    raise ActivationError(
                        "Recovery cannot replace a symbolic state path."
                    )
                path.unlink(missing_ok=True)
        os.replace(staged, directory / "drive.sqlite3")
        _sync(directory)


def _mongo_nodes(catalog):
    from tgdrive.mongo_store import MongoStore

    stack = [(catalog["root"], None)]
    rows = []
    while stack:
        node, parent = stack.pop()
        values = {
            key: copy.deepcopy(node[key])
            for key in (
                "upload_date",
                "trashed",
                "starred",
                "message_id",
                "size",
                "parts",
                "sha256",
            )
            if key in node
        }
        values["part_count"] = len(node.get("parts") or [None])
        rows.append(
            MongoStore._node(node["id"], parent, node["name"], node["type"], **values)
        )
        if node["type"] == "folder":
            stack.extend((child, node["id"]) for child in node["children"].values())
    return rows


def _apply_mongo(settings, value, backend):
    candidate = value["candidate"]
    desired = _document(candidate["credentials"], value["settings_revision"] + 1)
    connection_document = json.loads(desired.private_bytes())

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
        for name in (*_MONGO_DOMAIN, "jobs", "receipts", "bot_folders", "backups"):
            backend.db[name].delete_many({}, session=session)
        nodes = _mongo_nodes(candidate["catalog"])
        backend.db.nodes.insert_many(nodes, session=session)
        shares = [
            dict(row, _id=row["id"]) for row in candidate["catalog"].get("shares", [])
        ]
        if shares:
            backend.db.shares.insert_many(shares, session=session)
        metadata = {
            "_id": "catalog",
            "schema_version": SCHEMA_VERSION,
            "drive_id": candidate["catalog"]["drive_id"],
            "revision": candidate["catalog"]["revision"],
            "backed_up_revision": candidate["catalog"]["revision"],
            "storage_channel_id": candidate["catalog"]["storage_channel_id"],
            "generation": (before or {}).get("generation", 0) + 1,
            "replica_activation_id": value["id"],
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
        for filename, policy in candidate["credentials"].get("preferences", {}).items():
            identity = filename.removesuffix(".json")
            backend.db.settings.replace_one(
                {"_id": identity},
                {"_id": identity, "document": policy},
                upsert=True,
                session=session,
            )
        bootstrap = backend.db.settings.find_one({"_id": "bootstrap"}, session=session)
        if bootstrap:
            bootstrap["document"].update(
                {
                    "STORAGE_CHANNEL": metadata["storage_channel_id"],
                    "SESSION_SECRET": candidate["credentials"]["session_secret"],
                }
            )
            if "mongodb_backup_key" in candidate["credentials"]:
                bootstrap["document"]["MONGODB_BACKUP_KEY"] = candidate["credentials"][
                    "mongodb_backup_key"
                ]
            backend.db.settings.replace_one(
                {"_id": "bootstrap"}, bootstrap, session=session
            )

    backend.transaction(replace)
    with backend._settings_lock:
        backend._settings_cached = False
        backend._settings_document = None


def _configure_sqlite(settings, value):
    desired = _document(
        value["candidate"]["credentials"], value["settings_revision"] + 1
    )
    current = read_document(settings.resolved_data_dir)
    if not current or current.private_bytes() != desired.private_bytes():
        if (current.revision if current else 0) != value["settings_revision"]:
            raise ActivationError(
                "Connection settings changed during recovery; "
                "preserve them and review activation."
            )
        save_document(settings.resolved_data_dir, desired, value["settings_revision"])
    for filename, policy in (
        value["candidate"]["credentials"].get("preferences", {}).items()
    ):
        atomic_private(settings.resolved_data_dir / filename, _raw(policy))


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
    if bool(settings.mongodb_uri) != (mongo_backend is not None):
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
        # This durable boundary authorizes replay even if the process dies after
        # removing an old SQLite WAL but before swapping its replacement file.
        value["phase"] = "installing"
        _write(settings, value)
    if value["phase"] == "installing":
        if mongo_backend is None:
            _apply_sqlite(settings, value)
        else:
            _apply_mongo(settings, value, mongo_backend)
        value["phase"] = "catalog"
        _write(settings, value)
    if value["phase"] == "catalog":
        if mongo_backend is None:
            _configure_sqlite(settings, value)
        value["phase"] = "configured"
        _write(settings, value)
    value["phase"] = "applied"
    _write(settings, value)
    return True
