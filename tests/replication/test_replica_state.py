"""Recovery envelope integrity and durable replica journal fault contracts."""

from __future__ import annotations

import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from tests.support.mongo_fixtures import FakeMongoBackend, new_store
from tgdrive.replication.replica_settings import POLICIES
from tgdrive.replication.replica_state import (
    MAGIC,
    ReplicaJournal,
    atomic_private,
    decode_bundle,
    encode_bundle,
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


def test_authenticated_roundtrip_retains_catalog_identity_shares_and_starred():
    original = payload()
    encrypted = encode_bundle(original, KEY)
    assert encrypted.startswith(MAGIC) and b"session_secret" not in encrypted
    assert b"Report.txt" not in encrypted
    assert decode_bundle(encrypted, KEY) == original
    assert decode_bundle(encrypted, "  " + KEY + "  ") == original


@pytest.mark.parametrize("damage", [None, "source", "destination", "old-revision"])
def test_full_database_archive_reference_binds_to_catalog_pair(damage):
    value = payload()
    revision = value["catalog"]["revision"]
    archive = {
        "format": "mongodb-encrypted-v1",
        "id": "full-database-snapshot",
        "revision": revision + 1,
        "source_channel": MAIN,
        "source_message_id": 90,
        "replica_channel": BACKUP,
        "replica_message_id": 190,
        "size": 1024,
        "sha256": "a" * 64,
        "created_at": value["created_at"],
    }
    value["credentials"].update(mongodb_archive=archive, mongodb_backup_key=KEY)
    if damage == "source":
        archive["source_channel"] = -100999
    elif damage == "destination":
        archive["replica_channel"] = -100999
    elif damage == "old-revision":
        archive["revision"] = revision - 1
    if damage is None:
        assert decode_bundle(encode_bundle(value, KEY), KEY) == value
    else:
        with pytest.raises(ValueError, match="nothing changed"):
            decode_bundle(encode_bundle(value, KEY), KEY)


@pytest.mark.parametrize("damage", ["wrong-key", "truncated", "tampered", "not-bundle"])
def test_key_or_archive_corruption_never_yields_a_catalog(damage):
    encrypted = encode_bundle(payload(), KEY)
    key = KEY
    if damage == "wrong-key":
        key = "b2" * 32
    elif damage == "truncated":
        encrypted = encrypted[:-1]
    elif damage == "tampered":
        encrypted = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    else:
        encrypted = b"not a backup"
    with pytest.raises(ValueError, match="nothing changed"):
        decode_bundle(encrypted, key)


@pytest.mark.parametrize(
    "damage",
    [
        "extra-field",
        "missing-field",
        "bool-version",
        "incomplete",
        "fake-complete",
        "created-at",
        "same-channel",
        "positive-channel",
        "bool-channel",
        "revision",
        "drive-id",
        "renamed",
        "starred",
        "share-label",
        "missing-file",
        "duplicate-destination",
        "credential-extra",
        "credential-missing",
        "managed-document-missing",
        "preferences-missing",
        "policy-missing",
        "bool-api-id",
        "hash",
        "short-secret",
        "long-secret",
        "placeholder-secret",
        "tokens-empty",
        "token-malformed",
        "token-duplicate-identity",
        "tokens-unbounded",
    ],
)
def test_authenticated_envelope_still_rejects_semantic_forgery(damage):
    value = payload()
    if damage == "extra-field":
        value["unexpected"] = True
    elif damage == "missing-field":
        value.pop("credentials")
    elif damage == "bool-version":
        value["version"] = True
    elif damage == "incomplete":
        value["complete"] = False
    elif damage == "fake-complete":
        value["complete"] = "true"
    elif damage == "created-at":
        value["created_at"] = "not a time"
    elif damage in {"same-channel", "positive-channel", "bool-channel"}:
        value["replica_catalog"]["storage_channel_id"] = {
            "same-channel": MAIN,
            "positive-channel": 123,
            "bool-channel": True,
        }[damage]
    elif damage == "revision":
        value["replica_catalog"]["revision"] += 1
    elif damage == "drive-id":
        value["replica_catalog"]["drive_id"] = "different-drive"
    elif damage in {"renamed", "starred"}:
        node = next(files_in(value["replica_catalog"]))
        if damage == "renamed":
            node["name"] = "Wrong file name"
        else:
            node["starred"] = not node.get("starred", False)
    elif damage == "share-label":
        value["replica_catalog"]["shares"][0]["label"] = "Different audience"
    elif damage == "missing-file":
        root = value["replica_catalog"]["root"]
        folder = next(iter(root["children"].values()))
        folder["children"].pop(next(iter(folder["children"])))
    elif damage == "duplicate-destination":
        nodes = list(files_in(value["replica_catalog"]))
        nodes[1]["message_id"] = nodes[0]["message_id"]
    elif damage == "credential-extra":
        value["credentials"]["remote_url"] = "https://invalid.example"
    elif damage == "credential-missing":
        value["credentials"].pop("session_secret")
    elif damage == "managed-document-missing":
        value["credentials"].pop("managed_document")
    elif damage == "preferences-missing":
        value["credentials"].pop("preferences")
    elif damage == "policy-missing":
        value["credentials"]["preferences"].pop("bot-preferences.json")
    elif damage == "bool-api-id":
        value["credentials"]["api_id"] = True
    elif damage == "hash":
        value["credentials"]["api_hash"] = "not a hash"
    elif damage == "short-secret":
        value["credentials"]["session_secret"] = "short"
    elif damage == "long-secret":
        value["credentials"]["session_secret"] = "x" * 4097
    elif damage == "placeholder-secret":
        value["credentials"]["session_secret"] = "<replace this example" + "x" * 40
    elif damage == "tokens-empty":
        value["credentials"]["bot_tokens"] = []
    elif damage == "token-malformed":
        value["credentials"]["bot_tokens"] = ["invalid"]
    elif damage == "token-duplicate-identity":
        value["credentials"]["bot_tokens"] += ["123456:" + "y" * 30]
    else:
        value["credentials"]["bot_tokens"] = [
            str(100000 + index) + ":" + "x" * 30 for index in range(257)
        ]
    with pytest.raises(ValueError, match="nothing changed"):
        decode_bundle(encode_bundle(value, KEY), KEY)


def test_authenticated_duplicate_json_keys_are_rejected():
    encoded = json.dumps(payload()).replace(
        '"version": 1', '"version": 1, "version": 1'
    )
    nonce = b"n" * 12
    content = (
        MAGIC
        + nonce
        + AESGCM(bytes.fromhex(KEY)).encrypt(nonce, encoded.encode(), MAGIC)
    )
    with pytest.raises(ValueError, match="nothing changed"):
        decode_bundle(content, KEY)


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


def test_legacy_sqlite_journal_is_imported_once(tmp_path):
    path = tmp_path / "channel-backup.sqlite3"
    with sqlite3.connect(path) as legacy:
        legacy.executescript(
            """
            CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE copies(
                key TEXT PRIMARY KEY, random_id INTEGER NOT NULL,
                message_id INTEGER, size INTEGER NOT NULL
            );
            """
        )
        legacy.execute(
            "INSERT INTO state VALUES(?,?)",
            ("config", json.dumps({"channel": BACKUP, "enabled": True})),
        )
        legacy.execute("INSERT INTO copies VALUES('part', 7, 901, 12)")
    legacy.close()
    backend = FakeMongoBackend()
    journal = journal_for(tmp_path, backend)
    assert journal.get("config") == {"channel": BACKUP, "enabled": True}
    assert journal.intent("part", 12) == (7, 901)
    assert not path.exists()
    assert (tmp_path / "channel-backup.sqlite3.migrated").exists()
    # Later starts use MongoDB only.
    assert journal_for(tmp_path, backend).get("config")["channel"] == BACKUP


def test_legacy_journal_never_overwrites_existing_mongodb_state(tmp_path):
    backend = FakeMongoBackend()
    journal_for(tmp_path, backend).set("config", {"channel": BACKUP})
    path = tmp_path / "channel-backup.sqlite3"
    with sqlite3.connect(path) as legacy:
        legacy.execute("CREATE TABLE state(key TEXT, value TEXT)")
        legacy.execute("CREATE TABLE copies(key TEXT, random_id, message_id, size)")
    legacy.close()
    with pytest.raises(ValueError, match=r"Both channel-backup\.sqlite3"):
        journal_for(tmp_path, backend)
    assert path.exists()


@pytest.mark.asyncio
async def test_capsule_publication_rolls_back_point_and_keys_as_one_generation(
    tmp_path,
    monkeypatch,
):
    from unittest.mock import AsyncMock

    from pydantic import SecretStr

    from tests.replication.test_replica import BACKUP, dispose, setup
    from tests.support.mongo_fixtures import register_test_backend
    from tests.support.recovery_fixtures import settings
    from tgdrive.replication.replica import ChannelReplica

    backend = register_test_backend(monkeypatch, settings(tmp_path))
    monkeypatch.setattr(
        "tgdrive.replication.replica_mongo.mirror_mongodb", AsyncMock(return_value=None)
    )
    configured, store, remote, replica, config = setup(tmp_path)
    try:
        configured.mongodb_backup_key = SecretStr("b2" * 32)
        await replica._sync(config)
        # Deliberate rotation must keep both durable copies of the key aligned.
        backend.write(
            backend.db.settings,
            "update_one",
            {"_id": "recovery-key"},
            {"$set": {"document": {"key": "c3" * 32, "drive_id": store.drive_id}}},
        )
        replica.journal.set("key", "c3" * 32)
        configured.mongodb_backup_key = SecretStr("d4" * 32)
        previous = replica.recovery_kit()
        store.create_folder("/", "Next generation")
        state = backend.db.replica_state
        replace = state.replace_one

        def reject_key(query, document, **kwargs):
            if query["_id"] == "restore_point_key":
                raise RuntimeError("injected key write failure")
            return replace(query, document, **kwargs)

        monkeypatch.setattr(state, "replace_one", reject_key)
        with pytest.raises(RuntimeError, match="injected key write failure"):
            await replica._sync(config)
        monkeypatch.setattr(state, "replace_one", replace)
        await replica.close()
        replica = ChannelReplica(
            configured,
            SimpleNamespace(store=store, mongo=backend),
            transport_factory=remote.factory,
        )
        assert replica.recovery_kit() == previous
        original = decode_bundle(
            remote.messages[BACKUP, previous["restore_point"]["backup_message_id"]],
            previous["key"],
        )
        assert (
            original["credentials"]["mongodb_backup_key"]
            == previous["mongodb_backup_key"]
        )
        await replica._sync(config)
        current = replica.recovery_kit()
        assert current["restore_point"] != previous["restore_point"]
        assert current["key"] == "c3" * 32
        assert current["mongodb_backup_key"] == "d4" * 32
        recovered = decode_bundle(
            remote.messages[BACKUP, current["restore_point"]["backup_message_id"]],
            current["key"],
        )
        assert (
            recovered["credentials"]["mongodb_backup_key"]
            == current["mongodb_backup_key"]
        )
    finally:
        await dispose(replica, store)


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
