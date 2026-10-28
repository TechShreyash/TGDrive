"""The unified key survives local-state loss without replacing older archive keys."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from pymongo.errors import ConnectionFailure

from tests.support.mongo_fixtures import FakeMongoBackend
from tests.support.recovery_fixtures import settings
from tgdrive.replication.replica import ChannelReplica
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.mongo import DatabaseUnavailable

KEY = "a1" * 32
LEGACY_KEY = "b2" * 32
DRIVE = "key-durability-drive"


@pytest.fixture
def replicas(tmp_path):
    backend = FakeMongoBackend()
    opened = []

    def create(name="first", *, local_key=None, drive_id=DRIVE):
        configured = settings(tmp_path / name)
        configured.mongodb_backup_key = SecretStr(LEGACY_KEY)
        runtime = SimpleNamespace(
            mongo=backend, store=SimpleNamespace(drive_id=drive_id)
        )
        replica = ChannelReplica(configured, runtime)
        if local_key is not None:
            replica.journal.set("key", local_key)
        opened.append(replica)
        return replica

    yield create, backend
    for replica in opened:
        replica.journal.close()


def saved_key(key=KEY, drive_id=DRIVE):
    return {"_id": "recovery-key", "document": {"key": key, "drive_id": drive_id}}


def test_migrated_unified_key_survives_losing_local_directory(replicas, monkeypatch):
    create, backend = replicas
    original = create(local_key=KEY)
    assert original.key() == KEY
    assert backend.db.settings.find_one({"_id": "recovery-key"}) == saved_key()
    assert original.settings.mongodb_backup_key.get_secret_value() == LEGACY_KEY

    restored = create("reinstalled")
    assert restored.journal.get("key") is None
    assert restored.key() == KEY
    assert restored.journal.get("key") == KEY
    assert restored.settings.mongodb_backup_key.get_secret_value() == LEGACY_KEY

    read = Mock(side_effect=AssertionError("A known key must not add MongoDB reads"))
    monkeypatch.setattr(backend.db.settings, "find_one", read)
    assert restored.key() == KEY
    read.assert_not_called()


def test_existing_canonical_key_wins_over_legacy_configuration(replicas):
    create, backend = replicas
    backend.db.settings.insert_one(saved_key())
    replica = create()
    assert replica.key() == KEY
    assert replica.settings.mongodb_backup_key.get_secret_value() == LEGACY_KEY


def test_cached_key_rechecks_a_changed_local_key(replicas):
    create, backend = replicas
    replica = create(local_key=KEY)
    assert replica.key() == KEY
    replica.journal.set("key", LEGACY_KEY)
    with pytest.raises(SettingsProblem) as caught:
        replica.key()
    assert caught.value.code == "RECOVERY_KEY_CONFLICT"
    assert backend.db.settings.find_one({"_id": "recovery-key"}) == saved_key()

    backend.db.settings.update_one(
        {"_id": "recovery-key"},
        {"$set": {"document": saved_key(LEGACY_KEY)["document"]}},
    )
    assert replica.key() == LEGACY_KEY


def test_changed_local_key_is_not_trusted_during_database_outage(replicas, monkeypatch):
    create, backend = replicas
    replica = create(local_key=KEY)
    assert replica.key() == KEY
    replica.journal.set("key", LEGACY_KEY)
    monkeypatch.setattr(
        backend.db.settings,
        "find_one",
        Mock(side_effect=DatabaseUnavailable("unavailable")),
    )
    with pytest.raises(DatabaseUnavailable):
        replica.key()


@pytest.mark.parametrize(
    "local_key,drive_id", [(LEGACY_KEY, DRIVE), (None, "different-drive")]
)
def test_conflicting_keys_or_drive_are_preserved_for_explicit_repair(
    replicas, local_key, drive_id
):
    create, backend = replicas
    backend.db.settings.insert_one(saved_key())
    replica = create(local_key=local_key, drive_id=drive_id)
    with pytest.raises(SettingsProblem) as caught:
        replica.key()
    assert caught.value.code == "RECOVERY_KEY_CONFLICT"
    assert replica.journal.get("key") == local_key
    assert backend.db.settings.find_one({"_id": "recovery-key"}) == saved_key()


@pytest.mark.parametrize("local_key", [None, LEGACY_KEY])
def test_atomic_creation_uses_winner_without_overwriting_existing_local_key(
    replicas, monkeypatch, local_key
):
    create, backend = replicas
    replica = create(local_key=local_key)
    write = backend.write

    def another_writer_wins(*args, **kwargs):
        backend.db.settings.insert_one(saved_key())
        return write(*args, **kwargs)

    monkeypatch.setattr(backend, "write", another_writer_wins)
    if local_key is None:
        assert replica.key() == KEY
        assert replica.journal.get("key") == KEY
    else:
        with pytest.raises(SettingsProblem) as caught:
            replica.key()
        assert caught.value.code == "RECOVERY_KEY_CONFLICT"
        assert replica.journal.get("key") == LEGACY_KEY
    assert backend.db.settings.find_one({"_id": "recovery-key"}) == saved_key()


@pytest.mark.parametrize(
    "error", [DatabaseUnavailable("unavailable"), ConnectionFailure("unavailable")]
)
def test_database_outage_keeps_local_key_but_never_generates_a_replacement(
    replicas, monkeypatch, error
):
    create, backend = replicas
    existing = create(local_key=KEY)
    missing = create("missing")
    monkeypatch.setattr(backend.db.settings, "find_one", Mock(side_effect=error))
    assert existing.key() == KEY
    assert not getattr(existing, "_key_persisted", False)
    with pytest.raises(type(error)):
        missing.key()
    assert missing.journal.get("key") is None


def test_missing_migrated_key_cannot_silently_change_confirmed_file_password(replicas):
    create, backend = replicas
    backend.db.backups.insert_one(
        {
            "_id": "confirmed",
            "status": "confirmed",
            "format": "tgdrive-backup-v2",
            "created_at": "2026-10-28T00:00:00+00:00",
            "key_sha256": hashlib.sha256(bytes.fromhex(KEY)).hexdigest(),
        }
    )
    replica = create()
    with pytest.raises(SettingsProblem) as caught:
        replica.key()
    assert caught.value.code == "RECOVERY_KEY_MISSING"
    assert replica.journal.get("key") is None
    assert backend.db.settings.find_one({"_id": "recovery-key"}) is None


@pytest.mark.parametrize(
    "document",
    [None, {"key": "invalid", "drive_id": DRIVE}, {"key": KEY, "drive_id": []}],
)
def test_malformed_canonical_key_is_not_replaced(replicas, document):
    create, backend = replicas
    backend.db.settings.insert_one({"_id": "recovery-key", "document": document})
    replica = create(local_key=KEY)
    with pytest.raises(SettingsProblem, match="saved recovery key is invalid"):
        replica.key()
    assert replica.journal.get("key") == KEY
    assert backend.db.settings.find_one({"_id": "recovery-key"})["document"] == document
