"""Recovery envelope integrity and durable replica journal fault contracts."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import SecretStr

from tests.support.mongo_fixtures import FakeMongoBackend, new_store
from tgdrive.replication.replica_settings import POLICIES
from tgdrive.replication.replica_state import (
    ReplicaJournal,
    atomic_private,
    files_in,
    parts_in,
    read_private,
    remap_catalog,
)
from tgdrive.settings.managed_settings import ManagedClient, ManagedDocument

MAIN = -1001234567890
BACKUP = -1002234567890
KEY = "a1" * 32


def payload():
    store = new_store()
    try:
        store.bind_storage_channel(MAIN)
        folder = store.create_folder("/", "Records")
        path = "/" + folder["id"]
        first = store.add_file(path, "Report.txt", 11, 7)
        store.add_file(path, "Second.txt", 12, 8)
        store.set_starred(path + "/" + first["id"], True)
        store.create_share(path, label="Team", expires_at=None)
        catalog = store.export()
    finally:
        store.close()
    mapping = {
        str(part["message_id"]): part["message_id"] + 100
        for node in files_in(catalog)
        for part in parts_in(node)
    }
    return {
        "format": "tgdrive-replica",
        "version": 1,
        "complete": True,
        "created_at": "2026-10-22T10:00:00Z",
        "catalog": catalog,
        "replica_catalog": remap_catalog(catalog, BACKUP, mapping),
        "credentials": {
            "api_id": 12345,
            "api_hash": "a" * 32,
            "session_secret": "s" * 48,
            "bot_tokens": ["123456:" + "x" * 30],
            "managed_document": json.loads(
                ManagedDocument(
                    revision=1,
                    api_id=12345,
                    api_hash=SecretStr("a" * 32),
                    clients=[
                        ManagedClient(
                            id="storage-bot",
                            label="Storage bot",
                            kind="bot",
                            credential=SecretStr("123456:" + "x" * 30),
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
    }


def test_remap_does_not_mutate_original_and_requires_every_part():
    original = payload()["catalog"]
    preserved = copy.deepcopy(original)
    with pytest.raises(KeyError):
        remap_catalog(original, BACKUP, {"11": 111})
    assert original == preserved


def journal_for(tmp_path, backend):
    return ReplicaJournal(tmp_path, backend)


def test_journal_retains_exact_intent_and_confirmation_across_restarts(tmp_path):
    backend = FakeMongoBackend()
    journal = journal_for(tmp_path, backend)
    identity, receipt = journal.intent("drive:source:target:part", 123)
    assert type(identity) is int and identity > 0 and receipt is None
    journal.set("job", {"phase": "copying", "checkpoint": 17})
    journal.confirm("drive:source:target:part", 901)
    # A new server with the same MongoDB sees the same journal.
    journal = journal_for(tmp_path / "another-server", backend)
    assert journal.intent("drive:source:target:part", 123) == (identity, 901)
    assert journal.get("job") == {"phase": "copying", "checkpoint": 17}
    with pytest.raises(ValueError, match="size changed"):
        journal.intent("drive:source:target:part", 124)
    assert journal.intent("drive:source:different-target:part", 123)[0] != identity
    assert not list(tmp_path.glob("*.sqlite3"))


def test_large_journal_values_are_chunked_and_round_trip(tmp_path):
    journal = journal_for(tmp_path, FakeMongoBackend())
    value = {"database": "x" * (ReplicaJournal.CHUNK * 2 + 5)}
    journal.set("job", value)
    assert journal.get("job") == value
    assert journal.backend.db.replica_chunks.count_documents({"key": "job"}) == 3
    journal.set("job", None)
    assert journal.get("job", "missing") is None
    assert journal.backend.db.replica_chunks.count_documents({"key": "job"}) == 1


def test_atomic_state_rejects_unserializable_value_without_changing_any_key(tmp_path):
    backend = FakeMongoBackend()
    journal = journal_for(tmp_path, backend)
    journal.set_many({"restore_point": {"revision": 17}, "restore_point_key": KEY})
    with pytest.raises(ValueError):
        journal.set_many(
            {"restore_point": {"revision": 18}, "restore_point_key": float("nan")}
        )
    reopened = journal_for(tmp_path, backend)
    assert reopened.get("restore_point") == {"revision": 17}
    assert reopened.get("restore_point_key") == KEY


def test_failed_write_cannot_pair_new_restore_point_with_old_keys(
    tmp_path, monkeypatch
):
    old = {
        "restore_point": {"revision": 17, "backup_message_id": 901},
        "restore_point_key": KEY,
        "restore_point_mongodb_key": "b2" * 32,
        "credentials_digest": "a" * 64,
    }
    new = {
        "restore_point": {"revision": 18, "backup_message_id": 902},
        "restore_point_key": "c3" * 32,
        "restore_point_mongodb_key": None,
        "credentials_digest": "d" * 64,
    }
    backend = FakeMongoBackend()
    journal = journal_for(tmp_path, backend)
    journal.set_many(old)
    state = backend.db.replica_state
    replace = state.replace_one

    def fail_on_key(query, document, **kwargs):
        if query["_id"] == "restore_point_key":
            raise RuntimeError("injected key write failure")
        return replace(query, document, **kwargs)

    monkeypatch.setattr(state, "replace_one", fail_on_key)
    with pytest.raises(RuntimeError, match="injected"):
        journal.set_many(new)
    monkeypatch.undo()
    reopened = journal_for(tmp_path, backend)
    assert {key: reopened.get(key) for key in old} == old
    reopened.set_many(new)
    assert {key: reopened.get(key) for key in new} == new
    assert reopened.get("restore_point_mongodb_key", "stale") is None


def test_private_archive_write_and_read_reject_world_readable_or_symlink(tmp_path):
    target = tmp_path / "checkpoint.tgdb"
    atomic_private(target, b"encrypted archive")
    assert read_private(target) == b"encrypted archive"
    target.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_private(target)
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        read_private(link)
    assert target.read_bytes() == b"encrypted archive"
