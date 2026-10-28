"""Recovery envelope integrity and durable replica journal fault contracts."""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

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
from tgdrive.storage.store import DriveStore

MAIN = -1001234567890
BACKUP = -1002234567890
KEY = "a1" * 32


def payload():
    store = DriveStore.create()
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


def test_journal_retains_exact_intent_and_confirmation_across_restarts(tmp_path):
    journal = ReplicaJournal(tmp_path)
    identity, receipt = journal.intent("drive:source:target:part", 123)
    assert type(identity) is int and identity > 0 and receipt is None
    journal.set("job", {"phase": "copying", "checkpoint": 17})
    journal.confirm("drive:source:target:part", 901)
    journal.close()
    journal = ReplicaJournal(tmp_path)
    try:
        assert journal.intent("drive:source:target:part", 123) == (identity, 901)
        assert journal.get("job") == {"phase": "copying", "checkpoint": 17}
        with pytest.raises(ValueError, match="size changed"):
            journal.intent("drive:source:target:part", 124)
        assert journal.intent("drive:source:different-target:part", 123)[0] != identity
        assert (tmp_path / "channel-backup.sqlite3").stat().st_mode & 0o777 == 0o600
    finally:
        journal.close()


def test_atomic_state_rejects_unserializable_value_without_changing_any_key(tmp_path):
    journal = ReplicaJournal(tmp_path)
    journal.set_many({"restore_point": {"revision": 17}, "restore_point_key": KEY})
    try:
        with pytest.raises(ValueError):
            journal.set_many(
                {"restore_point": {"revision": 18}, "restore_point_key": float("nan")}
            )
    finally:
        journal.close()
    reopened = ReplicaJournal(tmp_path)
    try:
        assert reopened.get("restore_point") == {"revision": 17}
        assert reopened.get("restore_point_key") == KEY
    finally:
        reopened.close()


def test_process_death_cannot_pair_new_restore_point_with_old_keys(tmp_path):
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
    journal = ReplicaJournal(tmp_path)
    journal.set_many(old)
    journal.close()
    script = textwrap.dedent(
        """
        import json
        import os
        import sys
        from pathlib import Path
        from tgdrive.replication.replica_state import ReplicaJournal

        journal = ReplicaJournal(Path(sys.argv[1]))
        journal.db.create_function("crash_before_key", 0, lambda: os._exit(73))
        journal.db.executescript('''
            CREATE TRIGGER crash_publication BEFORE INSERT ON state
            WHEN NEW.key = 'restore_point_key'
            BEGIN SELECT crash_before_key(); END;
        ''')
        journal.set_many(json.loads(sys.argv[2]))
        raise SystemExit("Crash injection did not execute")
        """
    )
    stopped = subprocess.run(  # noqa: S603 - fixed Python and crash-injection script
        [sys.executable, "-c", script, str(tmp_path), json.dumps(new)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert stopped.returncode == 73, stopped.stderr
    reopened = ReplicaJournal(tmp_path)
    try:
        assert {key: reopened.get(key) for key in old} == old
        reopened.db.execute("DROP TRIGGER crash_publication")
        reopened.db.commit()
        reopened.set_many(new)
    finally:
        reopened.close()
    committed = ReplicaJournal(tmp_path)
    try:
        assert {key: committed.get(key) for key in new} == new
        assert committed.get("restore_point_mongodb_key", "stale") is None
    finally:
        committed.close()


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
        replica.journal.db.executescript(
            """
            CREATE TRIGGER reject_publication BEFORE INSERT ON state
            WHEN NEW.key = 'restore_point_key'
            BEGIN SELECT RAISE(ABORT, 'injected key write failure'); END;
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected key write failure"):
            await replica._sync(config)
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
        replica.journal.db.execute("DROP TRIGGER reject_publication")
        replica.journal.db.commit()
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
