"""Crash boundaries for explicit catalog/channel activation, never live overwrite."""

from __future__ import annotations

import copy
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from bson import Binary, json_util
from pydantic import SecretStr

from tests.support.mongo_fixtures import Collection as MongoCollection
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
from tgdrive.storage.store import DriveStore

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
    store = DriveStore.create()
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
                filename: model.model_validate(
                    {"uploads_per_bot": 1}
                    if filename == "transfer-preferences.json"
                    else {}
                ).model_dump()
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
    backend = for_settings(settings)
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
    assert (
        read_document(settings.resolved_data_dir)
        .clients[0]
        .credential.get_secret_value()
        == NEW_BOT
    )
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


class Collection(MongoCollection):
    def __init__(self, backend, name):
        self.backend, self.name = backend, name

    @property
    def rows(self):
        return self.backend.data.setdefault(self.name, [])

    def find(self, _query, **_kwargs):
        return copy.deepcopy(self.rows)

    def find_one(self, query, **_kwargs):
        return next(
            (
                copy.deepcopy(row)
                for row in self.rows
                if all(row.get(key) == value for key, value in query.items())
            ),
            None,
        )

    def count_documents(self, _query, **_kwargs):
        return len(self.rows)

    def delete_many(self, _query, **_kwargs):
        self.backend.data[self.name] = []

    def insert_many(self, rows, **_kwargs):
        if self.backend.fail_insert and self.name == "nodes":
            self.backend.fail_insert = False
            raise RuntimeError("synthetic transaction interruption")
        self.rows.extend(copy.deepcopy(rows))

    def replace_one(self, query, row, *, upsert=False, **_kwargs):
        for i, existing in enumerate(self.rows):
            if all(existing.get(key) == value for key, value in query.items()):
                self.rows[i] = copy.deepcopy(row)
                return SimpleNamespace(matched_count=1)
        if upsert:
            self.rows.append(copy.deepcopy(row))
        return SimpleNamespace(matched_count=0)


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
                    "schema_version": 4,
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
                "transfer-preferences.json": {"uploads_per_bot": 3},
            },
            "mongodb_backup_key": "e" * 64,
        }
    )
    return value


def test_recovery_restores_mongo_policies_and_keeps_previous_connections_disabled(
    settings,
):
    backend = for_settings(settings)
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
    assert pending.telegram_uploads_per_bot == 3
    assert pending.string_sessions == ()
    assert pending.mongodb_backup_key.get_secret_value() == "e" * 64
    assert _apply(pending)
    document = read_document(settings.resolved_data_dir)
    assert [row.id for row in document.clients if row.enabled] == [
        credential_id("bot", NEW_BOT)
    ]
    assert {row.label for row in document.clients if not row.enabled} == {
        "Former bot",
        "Former Premium",
    }
    saved = backend.db.settings.find_one({"_id": "performance-preferences"})
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
    backend.db.settings.replace_one(
        {"_id": "transfer-preferences"},
        {"_id": "transfer-preferences", "document": {"uploads_per_bot": 2}},
    )
    assert not _apply(settings)
    assert backend.db.settings.find_one({"_id": "transfer-preferences"})[
        "document"
    ] == {"uploads_per_bot": 2}


def test_mongo_activation_restores_only_saved_domain_preferences(settings, monkeypatch):
    settings, backend, value = mongo_settings(settings, monkeypatch)
    with_saved_settings(value)
    value["credentials"]["mongodb_archive"] = {
        "format": "mongodb-encrypted-v1",
        "id": "archive-id",
        "revision": 42,
        "source_channel": OLD,
        "source_message_id": 800,
        "replica_channel": NEW,
        "replica_message_id": 900,
        "size": 1200,
        "sha256": "a" * 64,
        "created_at": "2026-10-22T00:00:00Z",
    }
    prepare_activation(settings, value)
    assert apply_activation(settings, backend)
    assert backend.db.settings.find_one({"_id": "transfer-preferences"})[
        "document"
    ] == {"uploads_per_bot": 3}
    assert (
        backend.db.settings.find_one({"_id": "performance-preferences"})["document"][
            "timezone"
        ]
        == "UTC"
    )
    assert backend.data["jobs"] == [] and backend.data["receipts"] == []
    retained = json.loads(read_private(settings.resolved_data_dir / MANIFEST))
    assert (
        retained["candidate"]["credentials"]["mongodb_archive"]
        == value["credentials"]["mongodb_archive"]
    )
    assert (
        effective_settings(settings).mongodb_backup_key.get_secret_value() == "e" * 64
    )


@pytest.mark.parametrize(
    "current,expected",
    [
        ("a" * 64, "e" * 64),
        ("e" * 64, "e" * 64),
        ("f" * 64, "f" * 64),
        (None, "e" * 64),
    ],
)
def test_applied_mongo_key_accepts_rotation_but_retains_recovery_for_stale_key(
    settings, monkeypatch, current, expected
):
    settings, backend, value = mongo_settings(settings, monkeypatch)
    settings.mongodb_backup_key = SecretStr("a" * 64)
    with_saved_settings(value)
    prepare_activation(settings, value)
    pending = effective_settings(settings)
    assert pending.mongodb_backup_key.get_secret_value() == "e" * 64
    assert apply_activation(pending, backend)
    deployment = settings.model_copy(
        update={"mongodb_backup_key": SecretStr(current) if current else None}
    )
    active = effective_settings(deployment)
    assert active.mongodb_backup_key.get_secret_value() == expected
    assert active.storage_channel == NEW


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
    settings.mongodb_backup_key = SecretStr("f" * 64)
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
    assert settings.mongodb_backup_key.get_secret_value() == "f" * 64
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


def test_archive_symlink_and_manifest_size_limit_leave_catalog_untouched(
    settings, monkeypatch
):
    before = catalog(settings)
    value = candidate(before)
    import tgdrive.replication.replica_activation as module

    original_limit = module.MAX_BUNDLE
    monkeypatch.setattr(module, "MAX_BUNDLE", 100)
    with pytest.raises(ActivationError, match="64 MiB"):
        prepare_activation(settings, value)
    assert not (settings.resolved_data_dir / MANIFEST).exists()
    monkeypatch.setattr(module, "MAX_BUNDLE", original_limit)
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
    assert len(saved.clients) == 50
    assert sum(row.enabled for row in saved.clients) == 1
    manifest = json.loads(read_private(settings.resolved_data_dir / MANIFEST))
    assert (
        len(manifest["candidate"]["credentials"]["managed_document"]["clients"]) == 50
    )


def database_snapshot(settings):
    from tgdrive.recovery.mongo_backup import COLLECTIONS

    backend = for_settings(settings)
    records = {name: copy.deepcopy(backend.data.get(name, [])) for name in COLLECTIONS}
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


@pytest.mark.parametrize("interrupted", [False, True])
def test_unified_file_restores_database_settings_and_history_safely(
    settings, interrupted
):
    from argon2 import PasswordHasher

    before = catalog(settings)
    value = candidate(before)
    value["database"] = database_snapshot(settings)
    value["backup_config"] = {"channel": OLD, "token": BACKUP_BOT}
    backend = for_settings(settings)
    backend.data["sessions"] = [{"_id": "current-browser-cookie"}]
    prepare_activation(settings, value)
    manifest = json_util.loads(
        read_private(settings.resolved_data_dir / MANIFEST),
        json_options=json_util.JSONOptions(tz_aware=True),
    )
    normalized = manifest["candidate"]
    assert normalized["backup_config"] == value["backup_config"]
    assert normalized["credentials"]["mongodb_backup_key"] == value["key"]
    assert normalized["database"]["settings"][-1] == value["database"]["settings"][-1]
    assert normalized["database"]["sessions"] == []
    pending = effective_settings(settings)
    assert pending.mongodb_backup_key.get_secret_value() == value["key"]
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
    bootstrap = backend.db.settings.find_one({"_id": "bootstrap"})["document"]
    assert PasswordHasher().verify(
        bootstrap["ADMIN_PASSWORD_HASH"], "test activation password"
    )
    assert bootstrap["STORAGE_CHANNEL"] == NEW
    assert bootstrap["BOT_TOKENS"] == NEW_BOT and bootstrap["STRING_SESSIONS"] == ""
    assert bootstrap["MONGODB_BACKUP_KEY"] == value["key"]
    assert not _apply(pending)


def test_unified_file_recovers_a_missing_mongodb_catalog(settings):
    value = candidate(catalog(settings))
    value["database"] = database_snapshot(settings)
    backend = for_settings(settings)
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
