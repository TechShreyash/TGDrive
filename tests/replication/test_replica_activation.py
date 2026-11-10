"""Crash boundaries for explicit catalog/channel activation, never live overwrite."""

from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from bson import Binary, Decimal128, Int64, json_util
from pydantic import SecretStr

from tests.support.mongo_fixtures import Collection as FixtureCollection
from tests.support.mongo_fixtures import new_store
from tgdrive.core.config import Settings
from tgdrive.replication.replica_activation import (
    MANIFEST,
    ActivationError,
    apply_activation,
    effective_settings,
    prepare_activation,
)
from tgdrive.replication.replica_settings import POLICIES
from tgdrive.replication.replica_state import read_private
from tgdrive.settings.managed_settings import (
    ManagedClient,
    ManagedDocument,
    SettingsProblem,
    credential_id,
    read_document,
    save_document,
)
from tgdrive.storage.mongo import DatabaseUnavailable, for_settings
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import SCHEMA_VERSION

OLD = -100111222
NEW = -100333444
OLD_BOT = "12345:" + "a" * 24
NEW_BOT = "54321:" + "b" * 24
BACKUP_BOT = "67890:" + "c" * 24


@pytest.fixture
def settings(tmp_path, monkeypatch):
    configured = Settings(
        _env_file=None,
        MONGODB_URI="mongodb://synthetic.invalid",
        API_ID=12345,
        API_HASH="a" * 32,
        BOT_TOKENS=OLD_BOT,
        STORAGE_CHANNEL=OLD,
        ADMIN_PASSWORD="test activation password",
        SESSION_SECRET="s" * 40,
        ENCRYPTION_KEY="e" * 64,
        DATA_DIR=tmp_path / "data",
        CACHE_DIR=tmp_path / "cache",
    )

    backend = Backend(_seed_catalog())
    import tgdrive.storage.mongo as mongo

    monkeypatch.setattr(mongo, "configure", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(mongo, "for_settings", lambda _settings: backend)
    monkeypatch.setattr(mongo, "for_directory", lambda _directory: backend)
    return configured


def _seed_catalog():
    store = new_store()
    store.bind_storage_channel(OLD)
    item = store.add_file("/", "original.bin", 123, 42)
    store.set_starred("/" + item["id"], True)
    store.create_share("/" + item["id"], label="Review")
    payload = store.export()
    store.close()
    return payload


def catalog(settings):
    return installed(settings)


def _apply(settings):
    return apply_activation(settings, for_settings(settings))


def candidate(payload):
    value = copy.deepcopy(payload)
    value["storage_channel_id"] = NEW
    for node in value["root"]["children"].values():
        if node["type"] == "file":
            node["message_id"] += 1000
    return {
        "catalog": value,
        "complete": True,
        "credentials": {
            "api_id": 9876,
            "api_hash": "b" * 32,
            "session_secret": "r" * 40,
            "bot_tokens": [NEW_BOT],
            "managed_document": json.loads(
                ManagedDocument(
                    revision=1,
                    api_id=9876,
                    api_hash=SecretStr("b" * 32),
                    clients=[
                        ManagedClient(
                            id="recovery-bot",
                            label="Recovery bot",
                            kind="bot",
                            credential=SecretStr(NEW_BOT),
                        )
                    ],
                ).private_bytes()
            ),
            "preferences": {
                filename: model.model_validate({}).model_dump()
                for filename, model in POLICIES.items()
            },
        },
        "token": BACKUP_BOT,
        "key": "d" * 64,
    }


def installed(settings):
    store = MongoStore(for_settings(settings))
    try:
        return store.export()
    finally:
        store.close()


def test_prepare_preserves_live_mongo_catalog_and_archives_replaced_state(settings):
    before = catalog(settings)
    backend = cast(Backend, for_settings(settings))
    original_jobs = copy.deepcopy(backend.data["jobs"])
    source = candidate(before)
    identity = prepare_activation(settings, source)
    assert installed(settings) == before
    assert (settings.resolved_data_dir / MANIFEST).stat().st_mode & 0o777 == 0o600
    next_settings = effective_settings(settings)
    assert next_settings.storage_channel == NEW
    assert next_settings.bot_tokens == (NEW_BOT,)
    assert next_settings.string_sessions == ()
    assert _apply(next_settings)
    result = installed(settings)
    node = next(iter(result["root"]["children"].values()))
    assert result["storage_channel_id"] == NEW
    assert result["drive_id"] == before["drive_id"]
    assert node["message_id"] == 1123 and node["starred"] is True
    assert result["shares"] == before["shares"]
    archive = settings.resolved_data_dir / "recovery-archive" / identity
    saved = json.loads(read_private(archive / "mongodb-before.json"))
    assert saved["jobs"] == original_jobs
    assert saved["catalog"][0]["storage_channel_id"] == OLD
    assert backend.data["jobs"] == [] and backend.data["receipts"] == []
    document = read_document(settings.resolved_data_dir)
    assert document is not None
    assert document.clients[0].credential.get_secret_value() == NEW_BOT
    assert not _apply(next_settings)


def test_apply_replays_after_catalog_swap_before_manifest_update(settings, monkeypatch):
    before = catalog(settings)
    prepare_activation(settings, candidate(before))
    import tgdrive.replication.replica_activation as module

    original = module._write
    once = True

    def crash(config, value):
        nonlocal once
        if once and value["phase"] == "catalog":
            once = False
            raise OSError("synthetic power loss")
        return original(config, value)

    monkeypatch.setattr(module, "_write", crash)
    with pytest.raises(OSError, match="synthetic"):
        _apply(settings)
    assert installed(settings)["storage_channel_id"] == NEW
    assert (
        json.loads(read_private(settings.resolved_data_dir / MANIFEST))["phase"]
        == "installing"
    )
    assert _apply(settings)
    assert not _apply(settings)


def test_changed_catalog_and_foreign_nonempty_drive_are_never_overwritten(settings):
    before = catalog(settings)
    other = copy.deepcopy(before)
    other["drive_id"] = "another-drive"
    with pytest.raises(ActivationError, match="different drive"):
        prepare_activation(settings, candidate(other))
    assert not (settings.resolved_data_dir / MANIFEST).exists()
    prepare_activation(settings, candidate(before))
    store = MongoStore(for_settings(settings))
    store.create_folder("/", "Added after inspection")
    store.close()
    with pytest.raises(ActivationError, match="metadata changed"):
        _apply(settings)
    assert installed(settings)["storage_channel_id"] == OLD
    assert not (settings.resolved_data_dir / "recovery-archive").exists()


def test_applied_settings_keep_newer_connections_and_never_reinstall_catalog(settings):
    before = catalog(settings)
    prepare_activation(settings, candidate(before))
    _apply(settings)
    current = read_document(settings.resolved_data_dir)
    assert current is not None
    newer = ManagedDocument(
        revision=current.revision + 1,
        api_id=55555,
        api_hash=SecretStr("e" * 32),
        clients=[
            ManagedClient(
                id="updated-bot",
                label="Updated",
                kind="bot",
                credential=SecretStr("22222:" + "f" * 24),
            )
        ],
    )
    save_document(settings.resolved_data_dir, newer, current.revision)
    active = effective_settings(settings)
    assert active.api_id == 55555
    assert active.api_hash.get_secret_value() == "e" * 32
    assert active.bot_tokens == ("22222:" + "f" * 24,)
    assert active.session_secret.get_secret_value() == "r" * 40
    assert active.storage_channel == NEW
    store = MongoStore(for_settings(settings))
    store.create_folder("/", "After activation")
    revision = store.revision
    store.close()
    assert not _apply(active)
    assert installed(settings)["revision"] == revision


@pytest.mark.parametrize(
    "change",
    [
        {"complete": "true"},
        {"key": "bad"},
        {"token": "bad"},
        {
            "credentials": {
                "api_id": True,
                "api_hash": "b" * 32,
                "session_secret": "r" * 40,
                "bot_tokens": [NEW_BOT],
            }
        },
        {
            "credentials": {
                "api_id": 123,
                "api_hash": "bad",
                "session_secret": "short",
                "bot_tokens": [NEW_BOT],
            }
        },
    ],
)
def test_invalid_activation_never_writes_or_leaks_credentials(settings, change):
    before = catalog(settings)
    value = candidate(before)
    for field, replacement in change.items():
        if field == "credentials":
            value[field].update(replacement)
        else:
            value[field] = replacement
    with pytest.raises(ActivationError) as error:
        prepare_activation(settings, value)
    assert NEW_BOT not in str(error.value)
    assert "r" * 40 not in str(error.value)
    assert installed(settings) == before
    assert not (settings.resolved_data_dir / MANIFEST).exists()


def test_pending_conflict_and_corrupt_manifest_preserve_original_intent(settings):
    before = catalog(settings)
    value = candidate(before)
    prepare_activation(settings, value)
    path = settings.resolved_data_dir / MANIFEST
    original = path.read_bytes()
    different = copy.deepcopy(value)
    different["catalog"]["storage_channel_id"] -= 1
    with pytest.raises(ActivationError, match="pending"):
        prepare_activation(settings, different)
    assert path.read_bytes() == original
    assert prepare_activation(settings, value)
    damaged = json.loads(original)
    damaged["candidate"]["credentials"]["api_id"] += 1
    path.write_text(json.dumps(damaged))
    with pytest.raises(ActivationError, match="private activation record"):
        effective_settings(settings)
    assert installed(settings) == before


class Collection(FixtureCollection):
    """Shared fixture queries over this backend's swappable ``data`` mapping."""

    def __init__(self, backend, name):
        self.backend, self.name = backend, name

    @property
    def rows(self):
        return self.backend.data.setdefault(self.name, [])

    @rows.setter
    def rows(self, value):
        self.backend.data[self.name] = value

    def delete_many(self, query, **kwargs):
        return super().delete_many(query or {}, **kwargs)

    def insert_many(self, documents, **kwargs):
        if self.backend.fail_insert and self.name == "nodes":
            self.backend.fail_insert = False
            raise RuntimeError("synthetic transaction interruption")
        self.rows.extend(copy.deepcopy(documents))


class Database(SimpleNamespace):
    def __getitem__(self, name):
        return getattr(self, name)


class Backend:
    def __init__(self, payload):
        from tgdrive.replication.replica_activation import _mongo_nodes

        self.data = {
            "nodes": _mongo_nodes(payload),
            "shares": [dict(row, _id=row["id"]) for row in payload.get("shares", [])],
            "catalog": [
                {
                    "_id": "catalog",
                    "schema_version": SCHEMA_VERSION,
                    "drive_id": payload["drive_id"],
                    "generation": 2,
                    "revision": payload["revision"],
                    "backed_up_revision": payload["revision"],
                    "storage_channel_id": OLD,
                }
            ],
            "jobs": [{"_id": "old-job", "message_id": 123}],
            "receipts": [{"_id": "old-receipt"}],
            "settings": [{"_id": "bootstrap", "document": {"STORAGE_CHANNEL": OLD}}],
        }
        self.db = Database(
            **{
                name: Collection(self, name)
                for name in (
                    "nodes",
                    "shares",
                    "catalog",
                    "operations",
                    "deletions",
                    "preparations",
                    "settings",
                    "sessions",
                    "identities",
                    "jobs",
                    "receipts",
                    "bot_folders",
                    "backups",
                )
            }
        )
        for name in vars(self.db):
            self.data.setdefault(name, [])
            self.db[name].database = self.db
        self._settings_lock = threading.RLock()
        self._settings_cached = False
        self._settings_document = None
        self.fail_insert = False
        self.lose_reply = False
        self.fences = 0

    def fence(self, _session):
        self.fences += 1

    def check_owner(self):
        pass

    def write(self, collection, method, *args, **kwargs):
        return getattr(collection, method)(*args, **kwargs)

    @contextmanager
    def session_transaction(self):
        before = copy.deepcopy(self.data)
        try:
            yield object()
        except BaseException:
            self.data = before
            raise

    def transaction(self, operation):
        before = copy.deepcopy(self.data)
        try:
            result = operation(object())
        except BaseException:
            self.data = before
            raise
        if self.lose_reply and self.data["catalog"][0].get("replica_activation_id"):
            self.lose_reply = False
            raise RuntimeError("synthetic lost commit reply")
        return result


def mongo_settings(settings, monkeypatch):
    payload = catalog(settings)
    backend = Backend(payload)
    import tgdrive.storage.mongo as mongo

    monkeypatch.setattr(mongo, "configure", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(mongo, "for_settings", lambda _settings: backend)
    monkeypatch.setattr(mongo, "for_directory", lambda _directory: backend)
    return (
        settings.model_copy(
            update={"mongodb_uri": SecretStr("mongodb://synthetic.invalid")}
        ),
        backend,
        candidate(payload),
    )


def test_mongo_activation_is_fenced_atomic_and_has_no_sqlite_fallback(
    settings, monkeypatch
):
    settings, backend, source = mongo_settings(settings, monkeypatch)
    identity = prepare_activation(settings, source)
    before = copy.deepcopy(backend.data)
    backend.fail_insert = True
    with pytest.raises(RuntimeError, match="transaction interruption"):
        apply_activation(settings, backend)
    assert backend.data == before
    assert (
        json.loads(read_private(settings.resolved_data_dir / MANIFEST))["phase"]
        == "installing"
    )
    assert apply_activation(settings, backend)
    metadata = backend.data["catalog"][0]
    assert metadata["storage_channel_id"] == NEW
    assert metadata["replica_activation_id"] == identity
    assert backend.data["jobs"] == [] and backend.data["receipts"] == []
    assert backend.data["settings"][0]["document"]["STORAGE_CHANNEL"] == NEW
    assert backend.fences >= 3
    saved = json.loads(
        read_private(
            settings.resolved_data_dir
            / "recovery-archive"
            / identity
            / "mongodb-before.json"
        )
    )
    assert saved["jobs"][0]["_id"] == "old-job"
    assert not apply_activation(settings, backend)


def test_mongo_commit_response_loss_replays_without_replacing_committed_state(
    settings, monkeypatch
):
    settings, backend, source = mongo_settings(settings, monkeypatch)
    prepare_activation(settings, source)
    backend.lose_reply = True
    with pytest.raises(RuntimeError, match="lost commit reply"):
        apply_activation(settings, backend)
    assert backend.data["catalog"][0]["storage_channel_id"] == NEW
    generation = backend.data["catalog"][0]["generation"]
    assert apply_activation(settings, backend)
    assert backend.data["catalog"][0]["generation"] == generation
    assert effective_settings(settings).storage_channel == NEW


def test_mongo_pending_activation_never_uses_local_sqlite(settings, monkeypatch):
    settings, _backend, source = mongo_settings(settings, monkeypatch)
    local = settings.resolved_data_dir / "drive.sqlite3"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"legacy local catalog")
    before = local.read_bytes()
    prepare_activation(settings, source)
    with pytest.raises(ActivationError, match="no fallback"):
        apply_activation(settings)
    assert (settings.resolved_data_dir / "drive.sqlite3").read_bytes() == before


def with_saved_settings(value):
    from tgdrive.settings.performance_preferences import PerformancePreferences

    old = ManagedDocument(
        revision=7,
        api_id=12345,
        api_hash=SecretStr("a" * 32),
        clients=[
            ManagedClient(
                id="old-bot",
                label="Former bot",
                kind="bot",
                credential=SecretStr(OLD_BOT),
            ),
            ManagedClient(
                id="old-premium",
                label="Former Premium",
                kind="user",
                credential=SecretStr("synthetic-premium-session"),
            ),
        ],
    )
    value["credentials"].update(
        {
            "managed_document": json.loads(old.private_bytes()),
            "preferences": value["credentials"]["preferences"]
            | {
                "performance-preferences.json": PerformancePreferences(
                    timezone="UTC", cache_memory_bytes=0
                ).model_dump(),
            },
        }
    )
    return value


def test_recovery_restores_mongo_policies_and_keeps_previous_connections_disabled(
    settings,
):
    backend = cast(Backend, for_settings(settings))
    backend.db.settings.replace_one(
        {"_id": "performance-preferences"},
        {"_id": "performance-preferences", "document": {"timezone": "Asia/Tokyo"}},
        upsert=True,
    )
    value = with_saved_settings(candidate(catalog(settings)))
    identity = prepare_activation(settings, value)
    pending = effective_settings(settings)
    assert pending.timezone == "UTC"
    assert pending.cache_memory_bytes == 0
    assert pending.string_sessions == ()
    assert _apply(pending)
    document = read_document(settings.resolved_data_dir)
    assert document is not None
    assert [row.id for row in document.clients if row.enabled] == [
        credential_id("bot", NEW_BOT)
    ]
    assert {row.label for row in document.clients if not row.enabled} == {
        "Former bot",
        "Former Premium",
    }
    saved = backend.db.settings.find_one({"_id": "performance-preferences"})
    assert saved is not None
    assert saved["document"]["timezone"] == "UTC"
    archived = json.loads(
        read_private(
            settings.resolved_data_dir
            / "recovery-archive"
            / identity
            / "mongodb-before.json"
        )
    )
    old_policy = next(
        row for row in archived["settings"] if row["_id"] == "performance-preferences"
    )
    assert old_policy["document"]["timezone"] == "Asia/Tokyo"
    assert not _apply(settings)


def test_mongo_activation_restores_only_saved_domain_preferences(settings, monkeypatch):
    settings, backend, value = mongo_settings(settings, monkeypatch)
    with_saved_settings(value)
    prepare_activation(settings, value)
    assert apply_activation(settings, backend)
    assert (
        backend.db.settings.find_one({"_id": "performance-preferences"})["document"][
            "timezone"
        ]
        == "UTC"
    )
    assert backend.data["jobs"] == [] and backend.data["receipts"] == []


def test_applied_manifest_rejects_missing_baseline_without_changes(
    settings, monkeypatch
):
    import tgdrive.replication.replica_activation as module

    settings, backend, value = mongo_settings(settings, monkeypatch)
    with_saved_settings(value)
    prepare_activation(settings, value)
    assert apply_activation(settings, backend)
    path = settings.resolved_data_dir / MANIFEST
    manifest = json.loads(read_private(path))
    manifest.pop("mongodb_key_before_sha256")
    module.atomic_private(path, module._raw(manifest))
    before = copy.deepcopy(backend.data)
    private_files = {
        file.relative_to(settings.resolved_data_dir): file.read_bytes()
        for file in settings.resolved_data_dir.rglob("*")
        if file.is_file()
    }
    with pytest.raises(ActivationError, match="record is invalid"):
        effective_settings(settings)
    with pytest.raises(ActivationError, match="record is invalid"):
        apply_activation(settings, backend)
    assert backend.data == before
    assert {
        file.relative_to(settings.resolved_data_dir): file.read_bytes()
        for file in settings.resolved_data_dir.rglob("*")
        if file.is_file()
    } == private_files


def test_applied_key_baseline_is_bound_to_activation_identity(settings):
    import tgdrive.replication.replica_activation as module

    value = with_saved_settings(candidate(catalog(settings)))
    prepare_activation(settings, value)
    path = settings.resolved_data_dir / MANIFEST
    manifest = json.loads(read_private(path))
    manifest["mongodb_key_before_sha256"] = "f" * 64
    module.atomic_private(path, module._raw(manifest))
    with pytest.raises(ActivationError, match="record is invalid"):
        effective_settings(settings)


def test_applied_mongo_settings_outage_rejects_startup_without_writes(
    settings, monkeypatch
):
    import tgdrive.replication.replica_activation as module

    settings, backend, value = mongo_settings(settings, monkeypatch)
    with_saved_settings(value)
    prepare_activation(settings, value)
    assert apply_activation(settings, backend)
    before = copy.deepcopy(backend.data)

    def unavailable(_directory):
        raise DatabaseUnavailable("MongoDB is unavailable")

    monkeypatch.setattr(module, "read_document", unavailable)
    with pytest.raises(DatabaseUnavailable, match="MongoDB is unavailable"):
        effective_settings(settings)
    assert backend.data == before


def test_applied_mongo_invalid_connection_document_still_fails_closed(
    settings, monkeypatch
):
    import tgdrive.replication.replica_activation as module

    prepare_activation(settings, candidate(catalog(settings)))
    assert _apply(settings)

    def invalid(_directory):
        raise SettingsProblem("Private Telegram settings are invalid.", status=503)

    monkeypatch.setattr(module, "read_document", invalid)
    with pytest.raises(ActivationError, match="connection settings are invalid"):
        effective_settings(settings)


def test_recovery_policy_path_injection_is_rejected_before_preparation(settings):
    value = candidate(catalog(settings))
    value["credentials"]["preferences"] = {"../escaped.json": {"uploads_per_bot": 2}}
    with pytest.raises(ActivationError):
        prepare_activation(settings, value)
    assert not (settings.resolved_data_dir / MANIFEST).exists()


@pytest.mark.parametrize("missing", ["managed_document", "preferences", "bot-policy"])
def test_incomplete_recovery_settings_are_rejected_before_preparation(
    settings, missing
):
    before = catalog(settings)
    value = candidate(before)
    if missing == "bot-policy":
        value["credentials"]["preferences"].pop("bot-preferences.json")
    else:
        value["credentials"].pop(missing)
    with pytest.raises(ActivationError):
        prepare_activation(settings, value)
    assert installed(settings) == before
    assert not (settings.resolved_data_dir / MANIFEST).exists()


def test_archive_symlink_leaves_catalog_untouched(settings):
    before = catalog(settings)
    value = candidate(before)
    prepare_activation(settings, value)
    outside = settings.resolved_data_dir.parent / "outside"
    outside.mkdir()
    (settings.resolved_data_dir / "recovery-archive").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(ActivationError, match="archive path is unsafe"):
        _apply(settings)
    assert list(outside.iterdir()) == []
    assert installed(settings) == before


def test_connection_edit_after_confirmation_stops_before_catalog_replacement(settings):
    before = catalog(settings)
    prepare_activation(settings, candidate(before))
    changed = ManagedDocument(
        revision=1,
        api_id=44444,
        api_hash=SecretStr("e" * 32),
        clients=[
            ManagedClient(
                id="changed-bot",
                label="Changed",
                kind="bot",
                credential=SecretStr(OLD_BOT),
            )
        ],
    )
    save_document(settings.resolved_data_dir, changed, 0)
    with pytest.raises(ActivationError, match="Connection settings changed"):
        _apply(settings)
    assert installed(settings) == before
    assert not (settings.resolved_data_dir / "recovery-archive").exists()


def test_full_historical_connection_list_is_archived_without_auto_enabling_it(settings):
    value = candidate(catalog(settings))
    historical = ManagedDocument(
        revision=1,
        api_id=12345,
        api_hash=SecretStr("a" * 32),
        clients=[
            ManagedClient(
                id=f"old-bot-{i}",
                label=f"Former bot {i}",
                kind="bot",
                credential=SecretStr(str(100000 + i) + ":" + "a" * 24),
            )
            for i in range(50)
        ],
    )
    value["credentials"]["managed_document"] = json.loads(historical.private_bytes())
    prepare_activation(settings, value)
    assert _apply(settings)
    saved = read_document(settings.resolved_data_dir)
    assert saved is not None
    # The recovery bot plus every former connection: no account count limit.
    assert len(saved.clients) == 51
    assert sum(row.enabled for row in saved.clients) == 1
    manifest = json.loads(read_private(settings.resolved_data_dir / MANIFEST))
    assert (
        len(manifest["candidate"]["credentials"]["managed_document"]["clients"]) == 50
    )


def database_snapshot(settings):
    from tgdrive.recovery.mongo_backup import COLLECTIONS

    backend = cast(Backend, for_settings(settings))
    records = {name: copy.deepcopy(backend.data.get(name, [])) for name in COLLECTIONS}
    from tgdrive.core.encryption import key_record

    records["settings"].append(key_record(settings.encryption_key))
    records["jobs"] = [
        {
            "_id": "interrupted-upload",
            "data": {
                "id": "interrupted-upload",
                "phase": "uploading",
                "status": "running",
                "current": 3,
                "total": 42,
                "error": None,
                "created_at": 1000.0,
                "updated_at": 1001.0,
                "context": {
                    "kind": "upload",
                    "name": "original.bin",
                    "destination": "/",
                    "local_path": "/old-host/private.upload",
                    "multipart_plan": {"parts": [{"message_id": 123}]},
                },
            },
        },
    ]
    records["sessions"] = [{"_id": "old-browser-cookie"}]
    records["identities"] = [{"_id": "old-login-identity"}]
    records["bot_folders"] = [{"_id": "saved-folder", "node_id": "root"}]
    records["settings"].append(
        {
            "_id": "extra-saved-policy",
            "document": {
                "at": datetime(2026, 10, 28, tzinfo=UTC),
                "value": Binary(b"saved", subtype=128),
            },
        }
    )
    return records


def test_wrong_deployment_key_is_rejected_before_preparing_activation(settings):
    from tgdrive.core.encryption import key_record

    value = candidate(catalog(settings))
    value["database"] = database_snapshot(settings)
    value["database"]["settings"] = [key_record("f" * 64)]
    before = copy.deepcopy(cast(Backend, for_settings(settings)).data)
    with pytest.raises(ActivationError, match="ENCRYPTION_KEY does not match"):
        prepare_activation(settings, value)
    assert cast(Backend, for_settings(settings)).data == before
    assert not (settings.resolved_data_dir / MANIFEST).exists()


@pytest.mark.parametrize("interrupted", [False, True])
def test_unified_file_restores_database_settings_and_history_safely(
    settings, interrupted
):
    from argon2 import PasswordHasher

    before = catalog(settings)
    value = candidate(before)
    value["database"] = database_snapshot(settings)
    value["backup_config"] = {"channel": OLD, "token": BACKUP_BOT}
    backend = cast(Backend, for_settings(settings))
    backend.data["sessions"] = [{"_id": "current-browser-cookie"}]
    prepare_activation(settings, value)
    manifest = json_util.loads(
        read_private(settings.resolved_data_dir / MANIFEST),
        json_options=json_util.JSONOptions(tz_aware=True),
    )
    normalized = manifest["candidate"]
    assert normalized["backup_config"] == value["backup_config"]
    assert normalized["database"]["settings"][-1] == value["database"]["settings"][-1]
    assert normalized["database"]["sessions"] == []
    pending = effective_settings(settings)
    if interrupted:
        backend.fail_insert = True
        with pytest.raises(RuntimeError, match="transaction interruption"):
            _apply(pending)
        assert installed(settings) == before
        assert not backend.db.settings.find_one({"_id": "extra-saved-policy"})
        assert backend.data["sessions"] == [{"_id": "current-browser-cookie"}]
    assert _apply(pending)
    assert installed(settings) == normalized["catalog"]
    assert (
        backend.db.settings.find_one({"_id": "extra-saved-policy"})
        == value["database"]["settings"][-1]
    )
    assert backend.data["bot_folders"] == value["database"]["bot_folders"]
    assert backend.data["sessions"] == [] and backend.data["identities"] == []
    assert backend.data["operations"] == [] and backend.data["receipts"] == []
    assert backend.data["deletions"] == [] and backend.data["preparations"] == []
    assert backend.data["backups"] == []
    history = backend.data["jobs"]
    assert len(history) == 1 and history[0]["archived"] is True
    assert history[0]["status"] == "cancelled"
    assert history[0]["summary"]["name"] == "original.bin"
    assert history[0]["summary"]["retained"] is False
    assert "data" not in history[0] and "context" not in history[0]["summary"]
    row = backend.db.settings.find_one({"_id": "bootstrap"})
    assert row is not None
    bootstrap = row["document"]
    assert PasswordHasher().verify(
        bootstrap["ADMIN_PASSWORD_HASH"], "test activation password"
    )
    assert bootstrap["STORAGE_CHANNEL"] == NEW
    assert bootstrap["BOT_TOKENS"] == NEW_BOT and bootstrap["STRING_SESSIONS"] == ""
    assert not _apply(pending)


def test_unified_file_recovers_a_missing_mongodb_catalog(settings):
    value = candidate(catalog(settings))
    value["database"] = database_snapshot(settings)
    backend = cast(Backend, for_settings(settings))
    backend.data = {name: [] for name in backend.data}
    prepare_activation(settings, value)
    assert _apply(effective_settings(settings))
    assert installed(settings)["storage_channel_id"] == NEW
    assert backend.db.settings.find_one({"_id": "extra-saved-policy"})


@pytest.mark.parametrize("change", ["revision", "file", "backup-channel"])
def test_unified_file_rejects_mismatched_database_or_backup_before_activation(
    settings, change
):
    before = catalog(settings)
    value = candidate(before)
    value["database"] = database_snapshot(settings)
    if change == "revision":
        value["database"]["catalog"][0]["revision"] += 1
    elif change == "file":
        next(row for row in value["database"]["nodes"] if row["kind"] == "file")[
            "size"
        ] += 1
    else:
        value["backup_config"] = {"channel": NEW, "token": BACKUP_BOT}
    with pytest.raises(ActivationError):
        prepare_activation(settings, value)
    assert installed(settings) == before
    assert not (settings.resolved_data_dir / MANIFEST).exists()


def staged_candidate(settings, *, jobs=()):
    from tgdrive.recovery.recovery_file import sanitize_database
    from tgdrive.recovery.staged_snapshot import stage_snapshot

    value = candidate(catalog(settings))
    database = sanitize_database(database_snapshot(settings))
    database["jobs"] = list(jobs)
    database["operations"] = [
        {
            "_id": "old-operation",
            "operation_id": "old-operation",
            "node_id": next(
                row["_id"] for row in database["nodes"] if row["kind"] == "file"
            ),
        }
    ]
    envelope = {
        "format": "tgdrive-backup",
        "version": 2,
        "complete": True,
        "created_at": "2026-10-28T10:00:00Z",
        "database": database,
        "replica": {"channel": NEW, "messages": {"123": 1123}},
        "credentials": value["credentials"],
    }
    directory = settings.resolved_data_dir / "recovery-staging" / "case"
    directory.mkdir(parents=True, mode=0o700)
    source = directory / "snapshot.json"
    source.write_text(json_util.dumps(envelope), encoding="utf-8")
    index = stage_snapshot(source, directory / "snapshot.sqlite")
    value["catalog"] = {
        name: value["catalog"][name]
        for name in (
            "schema_version",
            "drive_id",
            "revision",
            "backed_up_revision",
            "storage_channel_id",
        )
    }
    value["staged"] = {
        "path": index.path.relative_to(settings.resolved_data_dir).as_posix(),
        "sha256": index.digest,
    }
    return value, index


def test_staged_activation_streams_bounded_batches_and_retains_history(
    settings, monkeypatch
):
    before = catalog(settings)
    jobs = (
        {"_id": f"job-{index}", "archived": True, "summary": {"name": "Saved"}}
        for index in range(700)
    )
    value, _index = staged_candidate(settings, jobs=jobs)
    batches = []
    original = Collection.insert_many

    def insert(collection, rows, **kwargs):
        batches.append((collection.name, len(rows)))
        return original(collection, rows, **kwargs)

    monkeypatch.setattr(Collection, "insert_many", insert)
    prepare_activation(settings, value)
    assert installed(settings) == before
    manifest = read_private(settings.resolved_data_dir / MANIFEST)
    assert len(manifest) < 16 * 1024
    assert b'"database"' not in manifest and b'"root"' not in manifest
    assert _apply(effective_settings(settings))
    after = installed(settings)
    assert after["storage_channel_id"] == NEW
    assert next(iter(after["root"]["children"].values()))["message_id"] == 1123
    assert after["shares"] == before["shares"]
    backend = cast(Backend, for_settings(settings))
    assert len(backend.data["jobs"]) == 700
    assert backend.data["operations"] == [] and backend.data["deletions"] == []
    assert backend.data["preparations"] == [] and backend.data["sessions"] == []
    assert backend.data["identities"] == [] and backend.data["receipts"] == []
    assert max(count for _name, count in batches) <= 256
    assert not _apply(settings)


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "permissions",
        "symlink",
        "parent_symlink",
        "path",
        "credentials",
        "channel",
        "key",
    ],
)
def test_staged_activation_refuses_changed_or_unsafe_source_before_prepare(
    settings, change
):
    from tgdrive.core.encryption import key_record
    from tgdrive.recovery.staged_snapshot import stage_snapshot

    value, index = staged_candidate(settings)
    if change == "digest":
        value["staged"]["sha256"] = "0" * 64
    elif change == "permissions":
        index.path.chmod(0o644)
    elif change == "symlink":
        target = index.path.with_suffix(".saved")
        index.path.rename(target)
        index.path.symlink_to(target)
    elif change == "parent_symlink":
        parent = index.path.parent
        target = parent.with_name("saved")
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
    elif change == "path":
        value["staged"]["path"] = "recovery-staging/../snapshot.sqlite"
    elif change == "credentials":
        value["credentials"]["session_secret"] = "changed" * 8
    elif change == "channel":
        value["catalog"]["storage_channel_id"] = -100999999
    else:
        source = index.path.with_suffix(".json")
        raw = json_util.loads(source.read_bytes())
        raw["database"]["settings"] = [key_record("f" * 64)]
        source.write_text(json_util.dumps(raw), encoding="utf-8")
        index.path.unlink()
        value["staged"]["sha256"] = stage_snapshot(source, index.path).digest
    before = copy.deepcopy(cast(Backend, for_settings(settings)).data)
    with pytest.raises(ActivationError):
        prepare_activation(settings, value)
    assert cast(Backend, for_settings(settings)).data == before
    assert not (settings.resolved_data_dir / MANIFEST).exists()


@pytest.mark.parametrize("relative", ["a" * 4097, "é" * 2049], ids=["ascii", "utf8"])
def test_staged_descriptor_path_budget_precedes_derived_allocations(
    settings, monkeypatch, relative
):
    import tgdrive.replication.replica_activation as module

    class UnexpandedPath(str):
        def split(self, *args, **kwargs):
            pytest.fail("Split an oversized staged descriptor path")

        def encode(self, *args, **kwargs):
            assert len(self) <= 4096, "Encoded an oversized staged path"
            return super().encode(*args, **kwargs)

    monkeypatch.setattr(
        module, "Path", lambda *_args: pytest.fail("Constructed an oversized Path")
    )
    descriptor = {"path": UnexpandedPath(relative), "sha256": "a" * 64}
    with pytest.raises(ActivationError) as failure:
        module._staged_source(settings, descriptor)
    assert failure.value.code == "ACTIVATION_RESOURCE_LIMIT"
    assert failure.value.status == 409
    assert "4096 UTF-8 byte limit" in str(failure.value)


@pytest.mark.parametrize("relative", ["a" * 4096, "é" * 2048], ids=["ascii", "utf8"])
def test_staged_descriptor_path_budget_accepts_exact_utf8_boundary(
    settings, monkeypatch, relative
):
    import tgdrive.replication.replica_activation as module

    class PathValidationReached(Exception):
        pass

    def check_path(value):
        assert value == relative
        raise PathValidationReached

    monkeypatch.setattr(module, "Path", check_path)
    with pytest.raises(PathValidationReached):
        module._staged_source(settings, {"path": relative, "sha256": "a" * 64})


def test_pending_staged_descriptor_overflow_preserves_input_before_path_or_identity(
    settings, monkeypatch
):
    import tgdrive.replication.replica_activation as module

    value, index = staged_candidate(settings)
    prepare_activation(settings, value)
    path = settings.resolved_data_dir / MANIFEST
    manifest = json_util.loads(read_private(path))
    manifest["candidate"]["staged"]["path"] = "a/" * (3 * 1024**2 // 2) + "index"
    original = module._raw(manifest)
    module.atomic_private(path, original)
    backend = cast(Backend, for_settings(settings))
    before = copy.deepcopy(backend.data)
    digest = index.digest
    monkeypatch.setattr(
        module, "Path", lambda *_args: pytest.fail("Constructed the oversized Path")
    )
    monkeypatch.setattr(
        module,
        "_activation_identity",
        lambda *_args: pytest.fail("Constructed the rejected activation identity"),
    )
    with pytest.raises(ActivationError) as failure:
        module._load(settings)
    assert failure.value.code == "ACTIVATION_RESOURCE_LIMIT"
    assert failure.value.status == 409
    assert "4096 UTF-8 byte limit" in str(failure.value)
    assert path.read_bytes() == original
    assert index.digest == digest
    assert backend.data == before
    assert not (path.parent / "recovery-archive").exists()


def test_staged_activation_rechecks_index_and_replays_atomic_failure(settings):
    value, index = staged_candidate(settings)
    backend = cast(Backend, for_settings(settings))
    before = copy.deepcopy(backend.data)
    prepare_activation(settings, value)
    original = index.path.read_bytes()
    with index.path.open("ab") as changed:
        changed.write(b"changed")
    with pytest.raises(ActivationError):
        _apply(settings)
    assert backend.data == before
    index.path.write_bytes(original)
    backend.fail_insert = True
    with pytest.raises(RuntimeError, match="transaction interruption"):
        _apply(settings)
    assert backend.data == before
    assert _apply(settings)
    assert installed(settings)["storage_channel_id"] == NEW
    assert not _apply(settings)


def test_existing_large_preservation_archive_is_checked_without_loading(
    settings, monkeypatch
):
    import tgdrive.replication.replica_activation as module

    archive = settings.resolved_data_dir / "recovery-archive" / "private"
    archive.mkdir(parents=True, mode=0o700)
    path = archive / "mongodb-before.json"
    with path.open("wb") as source:
        source.truncate(128 * 1024**2)
    path.chmod(0o600)
    monkeypatch.setattr(
        module, "private_reader", lambda _path: pytest.fail("read whole archive")
    )
    module._preserve_mongo(for_settings(settings), archive)


def test_staged_policy_normalization_remains_replayable(settings):
    from tgdrive.recovery.staged_snapshot import stage_snapshot

    value, index = staged_candidate(settings)
    source = index.path.with_suffix(".json")
    raw = json_util.loads(source.read_bytes())
    raw["credentials"]["preferences"]["performance-preferences.json"] = {
        "timezone": "UTC"
    }
    value["credentials"] = copy.deepcopy(raw["credentials"])
    source.write_text(json_util.dumps(raw), encoding="utf-8")
    index.path.unlink()
    value["staged"]["sha256"] = stage_snapshot(source, index.path).digest
    prepare_activation(settings, value)
    assert effective_settings(settings).timezone == "UTC"
    assert _apply(settings)
    assert not _apply(settings)


@pytest.mark.parametrize("failure", [None, "history", "insert"])
def test_staged_activation_closes_partial_iterators_in_their_worker(
    settings, monkeypatch, failure
):
    from tgdrive.recovery.staged_snapshot import StagedSnapshot, stage_snapshot

    jobs = (
        [{"_id": "bad", "archived": True, "summary": "invalid"}]
        if failure == "history"
        else []
    )
    value, index = staged_candidate(settings, jobs=jobs)
    if failure == "insert":
        source = index.path.with_suffix(".json")
        raw = json_util.loads(source.read_bytes())
        template = next(
            row for row in raw["database"]["nodes"] if row["kind"] == "file"
        )
        raw["database"]["nodes"].extend(
            dict(
                template,
                _id=f"bulk-{number}",
                name=f"bulk-{number}.bin",
                name_key=f"bulk-{number}.bin",
            )
            for number in range(600)
        )
        source.write_text(json_util.dumps(raw), encoding="utf-8")
        index.path.unlink()
        value["staged"]["sha256"] = stage_snapshot(source, index.path).digest
    observed, closed = [], []
    original = StagedSnapshot.records

    def tracked(snapshot, collection):
        def stream():
            origin = threading.get_ident()
            try:
                yield from original(snapshot, collection)
            finally:
                closed.append((origin, threading.get_ident()))

        iterator = stream()
        observed.append(iterator)  # Retain it as an exception traceback/GC could.
        return iterator

    monkeypatch.setattr(StagedSnapshot, "records", tracked)
    with ThreadPoolExecutor(max_workers=1) as worker:
        try:
            if failure == "history":
                with pytest.raises(ActivationError):
                    worker.submit(prepare_activation, settings, value).result()
            else:
                worker.submit(prepare_activation, settings, value).result()
                if failure == "insert":
                    cast(Backend, for_settings(settings)).fail_insert = True
                    with pytest.raises(RuntimeError, match="transaction interruption"):
                        worker.submit(_apply, settings).result()
            assert observed and all(iterator.gi_frame is None for iterator in observed)
            assert closed and all(origin == closer for origin, closer in closed)
        finally:
            # Keep a failing regression safe to collect on the test's thread.
            worker.submit(lambda: [iterator.close() for iterator in observed]).result()


def test_oversize_pending_activation_is_a_preserved_resource_limit(settings):
    path = settings.resolved_data_dir / MANIFEST
    path.parent.mkdir(parents=True)
    with path.open("wb") as record:
        record.write(b"preserve this activation")
        record.truncate(64 * 1024**2 + 1)
    path.chmod(0o600)
    before = path.stat()
    backend = cast(Backend, for_settings(settings))
    database = copy.deepcopy(backend.data)
    for load in (effective_settings, _apply):
        with pytest.raises(ActivationError) as failure:
            load(settings)
        assert failure.value.code == "ACTIVATION_RESOURCE_LIMIT"
        assert failure.value.status == 409
        detail = str(failure.value)
        assert len(detail) < 400
        assert "64 MiB compatibility admission limit" in detail
        assert (
            "does not establish corruption" in detail
            and "record is preserved" in detail
        )
        assert (
            "intact compatible archive" in detail
            and "no fallback is established" in detail
        )
        assert "docs/backup-recovery.md" in detail
    assert backend.data == database
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("step", ["decode", "catalog"])
def test_pending_activation_memory_exhaustion_is_not_misreported_as_corruption(
    settings, monkeypatch, step
):
    import tgdrive.replication.replica_activation as module

    prepare_activation(settings, candidate(catalog(settings)))
    path = settings.resolved_data_dir / MANIFEST
    original = path.read_bytes()

    def exhausted(*_args, **_kwargs):
        raise MemoryError("Synthetic allocation failure")

    if step == "decode":
        monkeypatch.setattr(module, "read_legacy_value", exhausted)
    else:
        monkeypatch.setattr(MongoStore, "_validate_payload", exhausted)
    with pytest.raises(ActivationError) as failure:
        effective_settings(settings)
    assert failure.value.code == "ACTIVATION_RESOURCE_LIMIT"
    assert "available memory" in str(failure.value)
    assert len(str(failure.value)) < 400
    assert path.read_bytes() == original
    assert not (path.parent / "recovery-archive").exists()


def test_legacy_whole_manifest_resource_limit_precedes_candidate_construction(
    settings, monkeypatch
):
    import tgdrive.recovery.staged_snapshot as staged
    import tgdrive.replication.replica_activation as module

    prepare_activation(settings, candidate(catalog(settings)))
    path = settings.resolved_data_dir / MANIFEST
    original = path.read_bytes()
    before = copy.deepcopy(cast(Backend, for_settings(settings)).data)
    monkeypatch.setattr(staged, "MAX_VALUE_ITEMS", 32)
    monkeypatch.setattr(
        module,
        "_candidate",
        lambda *_args: pytest.fail("Constructed excessive candidate"),
    )
    with pytest.raises(ActivationError) as failure:
        effective_settings(settings)
    assert failure.value.code == "ACTIVATION_RESOURCE_LIMIT"
    assert path.read_bytes() == original
    assert cast(Backend, for_settings(settings)).data == before
    assert not (path.parent / "recovery-archive").exists()


def test_bounded_legacy_loader_preserves_canonical_extended_json_and_identity(settings):
    import tgdrive.replication.replica_activation as module

    value = candidate(catalog(settings))
    value["database"] = database_snapshot(settings)
    value["database"]["jobs"] = [
        {
            "_id": "typed-history",
            "archived": True,
            "summary": {
                "name": "Typed history",
                "at": datetime(2026, 10, 28, tzinfo=UTC),
                "large": Int64(2**40),
                "binary": Binary(b"typed", 128),
                "decimal": Decimal128("NaN"),
                "floating": float("nan"),
            },
        }
    ]
    identity = prepare_activation(settings, value)
    path = settings.resolved_data_dir / MANIFEST
    loaded = module._load(settings)
    assert loaded is not None
    assert loaded["id"] == identity == module._activation_identity(loaded)
    assert module._raw(loaded) == path.read_bytes()
    assert effective_settings(settings).storage_channel == NEW


def test_activation_writer_rejects_unreadable_phase_before_replacing_the_record(
    settings, monkeypatch
):
    import tgdrive.recovery.staged_snapshot as staged
    import tgdrive.replication.replica_activation as module

    prepare_activation(settings, candidate(catalog(settings)))
    path = settings.resolved_data_dir / MANIFEST
    original = path.read_bytes()
    value = module._load(settings)
    assert value is not None
    value["phase"] = "preserved"
    monkeypatch.setattr(staged, "MAX_VALUE_ITEMS", 8)
    with pytest.raises(ActivationError) as failure:
        module._write(settings, value)
    assert failure.value.code == "ACTIVATION_RESOURCE_LIMIT"
    assert path.read_bytes() == original
    assert json.loads(original)["phase"] == "prepared"
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_activation_keeps_video_position_but_invalidates_writer(
    settings, staged, interrupted
):
    backend = cast(Backend, for_settings(settings))
    identity = next(
        row["_id"] for row in backend.data["nodes"] if row["kind"] == "file"
    )
    progress = {
        "position_seconds": 20.0,
        "duration_seconds": 200.0,
        "updated_at": 1000.0,
    }
    old_writer = {
        "id": "a" * 32,
        "sequence": 1,
        "started_at": 999.0,
        "saved_at": 1000.0,
        "closed": False,
    }
    backend.db.nodes.update_one(
        {"_id": identity},
        {"$set": {"video_progress": progress, "video_progress_session": old_writer}},
    )
    if staged:
        value, _index = staged_candidate(settings)
    else:
        value = candidate(catalog(settings))
        value["database"] = database_snapshot(settings)
    prepare_activation(settings, value)
    pending = effective_settings(settings)
    if interrupted:
        backend.fail_insert = True
        with pytest.raises(RuntimeError, match="transaction interruption"):
            _apply(pending)
        preserved = backend.db.nodes.find_one({"_id": identity})
        assert preserved is not None
        assert preserved["video_progress"] == progress
        assert preserved["video_progress_session"] == old_writer
    assert _apply(pending)
    restored = backend.db.nodes.find_one({"_id": identity})
    assert restored is not None
    assert restored["message_id"] == 1123
    assert restored["video_progress"] == progress
    assert "video_progress_session" not in restored
    assert "video_progress" not in json_util.dumps(installed(settings))
