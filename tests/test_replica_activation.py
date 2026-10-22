"""Crash boundaries for explicit catalog/channel activation, never live overwrite."""

from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from tgdrive.config import Settings
from tgdrive.managed_settings import (
    ManagedClient,
    ManagedDocument,
    SettingsProblem,
    credential_id,
    read_document,
    save_document,
)
from tgdrive.replica_activation import (
    MANIFEST,
    ActivationError,
    apply_activation,
    effective_settings,
    prepare_activation,
)
from tgdrive.replica_state import read_private
from tgdrive.store import DriveStore

OLD = -100111222
NEW = -100333444
OLD_BOT = "12345:" + "a" * 24
NEW_BOT = "54321:" + "b" * 24
BACKUP_BOT = "67890:" + "c" * 24


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        API_ID=12345,
        API_HASH="a" * 32,
        BOT_TOKENS=OLD_BOT,
        STORAGE_CHANNEL=OLD,
        ADMIN_PASSWORD="test activation password",
        SESSION_SECRET="s" * 40,
        DATA_DIR=tmp_path / "data",
        CACHE_DIR=tmp_path / "cache",
    )


def catalog(settings):
    path = settings.resolved_data_dir / "drive.sqlite3"
    store = DriveStore.create(path)
    store.bind_storage_channel(OLD)
    item = store.add_file("/", "original.bin", 123, 42)
    store.set_starred("/" + item["id"], True)
    store.create_share("/" + item["id"], label="Review")
    payload = store.export()
    store.close()
    return payload


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
        },
        "token": BACKUP_BOT,
        "key": "d" * 64,
    }


def installed(settings):
    store = DriveStore.load(settings.resolved_data_dir / "drive.sqlite3")
    try:
        return store.export()
    finally:
        store.close()


def test_prepare_does_not_overwrite_live_catalog_and_apply_preserves_local_state(
    settings,
):
    before = catalog(settings)
    directory = settings.resolved_data_dir
    (directory / "jobs.sqlite3").write_bytes(b"old jobs bytes")
    history = directory / "backups"
    history.mkdir()
    (history / "checkpoint.json").write_text("old checkpoint")
    source = candidate(before)
    identity = prepare_activation(settings, source)
    assert installed(settings) == before
    assert (directory / MANIFEST).stat().st_mode & 0o777 == 0o600
    next_settings = effective_settings(settings)
    assert next_settings.storage_channel == NEW
    assert next_settings.bot_tokens == (NEW_BOT,)
    assert next_settings.string_sessions == ()
    assert next_settings.main_bot_token is None
    assert apply_activation(next_settings)
    result = installed(settings)
    node = next(iter(result["root"]["children"].values()))
    assert result["storage_channel_id"] == NEW
    assert result["drive_id"] == before["drive_id"]
    assert node["message_id"] == 1123 and node["starred"] is True
    assert result["shares"] == before["shares"]
    archive = directory / "recovery-archive" / identity
    assert (archive / "jobs.sqlite3").read_bytes() == b"old jobs bytes"
    assert (archive / "backups/checkpoint.json").read_text() == "old checkpoint"
    old = DriveStore.load(archive / "drive.sqlite3")
    assert old.storage_channel_id == OLD
    old.close()
    assert not (directory / "jobs.sqlite3").exists()
    assert not history.exists()
    assert read_document(directory).clients[0].credential.get_secret_value() == NEW_BOT
    assert not apply_activation(next_settings)


def test_apply_replays_after_catalog_swap_before_manifest_update(settings, monkeypatch):
    before = catalog(settings)
    prepare_activation(settings, candidate(before))
    import tgdrive.replica_activation as module

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
        apply_activation(settings)
    assert installed(settings)["storage_channel_id"] == NEW
    assert (
        json.loads(read_private(settings.resolved_data_dir / MANIFEST))["phase"]
        == "installing"
    )
    assert apply_activation(settings)
    assert not apply_activation(settings)


def test_apply_replays_after_old_sidecars_removed_before_catalog_swap(
    settings, monkeypatch
):
    before = catalog(settings)
    prepare_activation(settings, candidate(before))
    import tgdrive.replica_activation as module

    original = module.os.replace
    once = True

    def crash(source, target):
        nonlocal once
        if once and str(source).endswith(".sqlite3") and target.name == "drive.sqlite3":
            once = False
            raise OSError("synthetic swap interruption")
        return original(source, target)

    monkeypatch.setattr(module.os, "replace", crash)
    with pytest.raises(OSError, match="synthetic"):
        apply_activation(settings)
    assert (
        json.loads(read_private(settings.resolved_data_dir / MANIFEST))["phase"]
        == "installing"
    )
    assert apply_activation(settings)
    assert installed(settings)["storage_channel_id"] == NEW


def test_changed_catalog_and_foreign_nonempty_drive_are_never_overwritten(settings):
    before = catalog(settings)
    other = copy.deepcopy(before)
    other["drive_id"] = "another-drive"
    with pytest.raises(ActivationError, match="different drive"):
        prepare_activation(settings, candidate(other))
    assert not (settings.resolved_data_dir / MANIFEST).exists()
    prepare_activation(settings, candidate(before))
    store = DriveStore.load(settings.resolved_data_dir / "drive.sqlite3")
    store.create_folder("/", "Added after inspection")
    store.close()
    with pytest.raises(ActivationError, match="metadata changed"):
        apply_activation(settings)
    assert installed(settings)["storage_channel_id"] == OLD
    assert not (settings.resolved_data_dir / "recovery-archive").exists()


def test_corrupt_local_catalog_is_preserved_byte_for_byte_on_explicit_recovery(
    settings,
):
    before = catalog(settings)
    path = settings.resolved_data_dir / "drive.sqlite3"
    path.write_bytes(b"broken local metadata")
    identity = prepare_activation(settings, candidate(before))
    assert apply_activation(settings)
    archive = settings.resolved_data_dir / "recovery-archive" / identity
    assert (archive / "drive.sqlite3").read_bytes() == b"broken local metadata"
    assert installed(settings)["storage_channel_id"] == NEW


def test_applied_settings_keep_newer_connections_and_never_reinstall_catalog(settings):
    before = catalog(settings)
    prepare_activation(settings, candidate(before))
    apply_activation(settings)
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
    store = DriveStore.load(settings.resolved_data_dir / "drive.sqlite3")
    store.create_folder("/", "After activation")
    revision = store.revision
    store.close()
    assert not apply_activation(active)
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
    value = candidate(before) | change
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


class Collection:
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
        from tgdrive.replica_activation import _mongo_nodes

        self.data = {
            "nodes": _mongo_nodes(payload),
            "shares": [],
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
    import tgdrive.mongo as mongo

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
    before = (settings.resolved_data_dir / "drive.sqlite3").read_bytes()
    prepare_activation(settings, source)
    with pytest.raises(ActivationError, match="no fallback"):
        apply_activation(settings)
    assert (settings.resolved_data_dir / "drive.sqlite3").read_bytes() == before


def with_saved_settings(value):
    from tgdrive.performance_preferences import PerformancePreferences

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
            "preferences": {
                "performance-preferences.json": PerformancePreferences(
                    timezone="UTC", cache_memory_bytes=0
                ).model_dump(),
                "transfer-preferences.json": {"uploads_per_bot": 3},
            },
            "mongodb_backup_key": "e" * 64,
        }
    )
    return value


def test_recovery_restores_saved_policies_and_keeps_previous_connections_disabled(
    settings,
):
    before = catalog(settings)
    policy = settings.resolved_data_dir / "performance-preferences.json"
    policy.write_text(json.dumps({"timezone": "Asia/Tokyo"}))
    value = with_saved_settings(candidate(before))
    identity = prepare_activation(settings, value)
    pending = effective_settings(settings)
    assert pending.timezone == "UTC"
    assert pending.cache_memory_bytes == 0
    assert pending.telegram_uploads_per_bot == 3
    assert pending.string_sessions == ()
    assert pending.mongodb_backup_key.get_secret_value() == "e" * 64
    assert apply_activation(pending)
    document = read_document(settings.resolved_data_dir)
    assert [row.id for row in document.clients if row.enabled] == [
        credential_id("bot", NEW_BOT)
    ]
    assert {row.label for row in document.clients if not row.enabled} == {
        "Former bot",
        "Former Premium",
    }
    assert json.loads(policy.read_bytes())["timezone"] == "UTC"
    archived = settings.resolved_data_dir / "recovery-archive" / identity / policy.name
    assert json.loads(archived.read_bytes())["timezone"] == "Asia/Tokyo"
    transfer = settings.resolved_data_dir / "transfer-preferences.json"
    assert json.loads(transfer.read_bytes()) == {"uploads_per_bot": 3}
    transfer.write_text(json.dumps({"uploads_per_bot": 2}))
    assert not apply_activation(settings)
    assert json.loads(transfer.read_bytes()) == {"uploads_per_bot": 2}


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


def test_applied_legacy_manifest_conservatively_retains_recovery_key(
    settings, monkeypatch
):
    import tgdrive.replica_activation as module

    settings, backend, value = mongo_settings(settings, monkeypatch)
    with_saved_settings(value)
    prepare_activation(settings, value)
    assert apply_activation(settings, backend)
    path = settings.resolved_data_dir / MANIFEST
    manifest = json.loads(read_private(path))
    manifest.pop("mongodb_key_before_sha256")
    manifest["id"] = module._activation_identity(manifest)
    module.atomic_private(path, module._raw(manifest))
    settings.mongodb_backup_key = SecretStr("f" * 64)
    assert (
        effective_settings(settings).mongodb_backup_key.get_secret_value() == "e" * 64
    )


def test_applied_key_baseline_is_bound_to_activation_identity(settings):
    import tgdrive.replica_activation as module

    value = with_saved_settings(candidate(catalog(settings)))
    prepare_activation(settings, value)
    path = settings.resolved_data_dir / MANIFEST
    manifest = json.loads(read_private(path))
    manifest["mongodb_key_before_sha256"] = "f" * 64
    module.atomic_private(path, module._raw(manifest))
    with pytest.raises(ActivationError, match="record is invalid"):
        effective_settings(settings)


def test_applied_mongo_settings_outage_uses_private_candidate_without_writes(
    settings, monkeypatch
):
    import tgdrive.replica_activation as module

    settings, backend, value = mongo_settings(settings, monkeypatch)
    with_saved_settings(value)
    prepare_activation(settings, value)
    assert apply_activation(settings, backend)
    before = copy.deepcopy(backend.data)
    local = settings.resolved_data_dir / "drive.sqlite3"
    local_before = local.read_bytes()

    def unavailable(_directory):
        raise SettingsProblem("Could not read MongoDB connection settings.", status=503)

    monkeypatch.setattr(module, "read_document", unavailable)
    active = effective_settings(settings)
    assert active.storage_channel == NEW
    assert active.bot_tokens == (NEW_BOT,)
    assert active.api_id == value["credentials"]["api_id"]
    assert (
        active.session_secret.get_secret_value()
        == value["credentials"]["session_secret"]
    )
    assert active.mongodb_backup_key.get_secret_value() == "e" * 64
    assert active.mongodb_uri == settings.mongodb_uri
    assert backend.data == before
    assert local.read_bytes() == local_before


def test_applied_sqlite_invalid_connection_document_still_fails_closed(
    settings, monkeypatch
):
    import tgdrive.replica_activation as module

    prepare_activation(settings, candidate(catalog(settings)))
    assert apply_activation(settings)

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


def test_archive_symlink_and_manifest_size_limit_leave_catalog_untouched(
    settings, monkeypatch
):
    before = catalog(settings)
    value = candidate(before)
    import tgdrive.replica_activation as module

    monkeypatch.setattr(module, "MAX_BUNDLE", 100)
    with pytest.raises(ActivationError, match="64 MiB"):
        prepare_activation(settings, value)
    assert not (settings.resolved_data_dir / MANIFEST).exists()
    monkeypatch.undo()
    prepare_activation(settings, value)
    outside = settings.resolved_data_dir.parent / "outside"
    outside.mkdir()
    (settings.resolved_data_dir / "recovery-archive").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(ActivationError, match="archive path is unsafe"):
        apply_activation(settings)
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
        apply_activation(settings)
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
    assert apply_activation(settings)
    saved = read_document(settings.resolved_data_dir)
    assert len(saved.clients) == 50
    assert sum(row.enabled for row in saved.clients) == 1
    manifest = json.loads(read_private(settings.resolved_data_dir / MANIFEST))
    assert (
        len(manifest["candidate"]["credentials"]["managed_document"]["clients"]) == 50
    )
