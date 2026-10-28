"""Activation checks on an opt-in, disposable real MongoDB replica set."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from tgdrive.core.config import Settings
from tgdrive.replication.replica_activation import (
    MANIFEST,
    ActivationError,
    apply_activation,
    effective_settings,
    prepare_activation,
)
from tgdrive.replication.replica_settings import collect_credentials
from tgdrive.replication.replica_state import read_private, remap_catalog
from tgdrive.settings.managed_settings import (
    ManagedClient,
    ManagedDocument,
    read_document,
    save_document,
)
from tgdrive.settings.performance_preferences import PerformancePreferences
from tgdrive.storage.mongo import configure
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.web.share_links import token_factory

pytestmark = pytest.mark.skipif(
    os.environ.get("TGDRIVE_MONGO_TESTS") != "1",
    reason="Set TGDRIVE_MONGO_TESTS=1 for isolated live MongoDB checks",
)

OLD = -100111222
NEW = -100333444
OLD_BOT = "12345:" + "a" * 24
NEW_BOT = "54321:" + "b" * 24
BACKUP_BOT = "67890:" + "c" * 24


def node_path(row):
    return row["path"].rstrip("/") + "/" + row["id"]


@pytest.fixture
def deployment(tmp_path):
    uri = os.environ.get("TGDRIVE_TEST_MONGODB_URI")
    if not uri:
        raise pytest.UsageError(
            "Set TGDRIVE_TEST_MONGODB_URI to an isolated replica set"
        )
    name = "tgdrive_test_activation_" + secrets.token_hex(6)
    directory = tmp_path / "private-data"
    backend = configure(directory, uri, name)
    backend.acquire()
    try:
        settings = Settings(
            _env_file=None,
            MONGODB_URI=uri,
            MONGODB_DATABASE=name,
            DATA_DIR=directory,
            CACHE_DIR=tmp_path / "cache",
            API_ID=12345,
            API_HASH="a" * 32,
            BOT_TOKENS=OLD_BOT,
            STORAGE_CHANNEL=OLD,
            ADMIN_PASSWORD="synthetic recovery owner password",
            SESSION_SECRET="s" * 40,
            MONGODB_BACKUP_KEY="e" * 64,
        )
        store = MongoStore.create(backend)
        store.bind_storage_channel(OLD)
        folder = store.create_folder("/", "Projects")
        parts = [
            {
                "index": 0,
                "offset": 0,
                "size": 3,
                "message_id": 101,
                "sha256": hashlib.sha256(b"abc").hexdigest(),
            },
            {
                "index": 1,
                "offset": 3,
                "size": 3,
                "message_id": 102,
                "sha256": hashlib.sha256(b"def").hexdigest(),
            },
        ]
        item = store.add_file(
            node_path(folder),
            "split.bin",
            101,
            6,
            parts=parts,
            sha256=hashlib.sha256(b"abcdef").hexdigest(),
            operation_id="source-receipt",
        )
        store.set_starred(node_path(item), True)
        store.set_starred(node_path(folder), True)
        tokens = [
            store.create_share(
                node_path(item), label=label, token_factory=token_factory(settings)
            )
            for label in ("Review", "External")
        ]
        old = store.export()
        store.close()
        document = ManagedDocument(
            revision=1,
            api_id=12345,
            api_hash=SecretStr("a" * 32),
            clients=[
                ManagedClient(
                    id="original-bot",
                    label="Original bot",
                    kind="bot",
                    credential=SecretStr(OLD_BOT),
                )
            ],
        )
        save_document(directory, document, 0)
        backend.write(
            backend.db.settings,
            "replace_one",
            {"_id": "bootstrap"},
            {
                "_id": "bootstrap",
                "document": {"STORAGE_CHANNEL": OLD, "SESSION_SECRET": "s" * 40},
            },
            upsert=True,
        )
        for identity, value in (
            (
                "performance-preferences",
                PerformancePreferences(timezone="UTC").model_dump(),
            ),
            ("transfer-preferences", {"uploads_per_bot": 2}),
        ):
            backend.write(
                backend.db.settings,
                "replace_one",
                {"_id": identity},
                {"_id": identity, "document": value},
                upsert=True,
            )
        backend.write(
            backend.db.jobs,
            "insert_one",
            {"_id": "old-running-job", "status": "running", "message_id": 101},
        )
        backend.write(
            backend.db.receipts,
            "insert_one",
            {"_id": "old-receipt", "message_ids": [101, 102]},
        )
        backend.write(
            backend.db.backups,
            "insert_one",
            {"_id": "old-checkpoint", "channel_id": OLD, "revision": old["revision"]},
        )
        credentials = collect_credentials(settings, SimpleNamespace(mongo=backend))
        credentials["bot_tokens"] = [NEW_BOT]
        recovered = remap_catalog(old, NEW, {"101": 1101, "102": 1102})
        candidate = {
            "catalog": recovered,
            "credentials": credentials,
            "complete": True,
            "token": BACKUP_BOT,
            "key": "f" * 64,
        }
        yield SimpleNamespace(
            settings=settings,
            backend=backend,
            original=old,
            candidate=candidate,
            item=item,
            tokens=tokens,
        )
    finally:
        backend.release()
        assert backend.db.name == name and name.startswith("tgdrive_test_activation_")
        backend.client.drop_database(name)
        backend.client.close()


def test_live_mongo_catalog_preferences_and_credentials_commit_together(deployment):
    row = deployment
    identity = prepare_activation(row.settings, row.candidate)
    pending = effective_settings(row.settings)
    assert pending.storage_channel == NEW and pending.bot_tokens == (NEW_BOT,)
    assert apply_activation(pending, row.backend)
    store = MongoStore(row.backend)
    payload = store.export()
    assert payload == row.candidate["catalog"]
    file = store.get_file(node_path(row.item))
    assert file["message_id"] == 1101
    assert [part["message_id"] for part in file["parts"]] == [1101, 1102]
    assert file["sha256"] == hashlib.sha256(b"abcdef").hexdigest()
    assert file["starred"] is True
    assert all(store.authorize_file(node_path(row.item), token) for token in row.tokens)
    assert {link["label"] for link in store.list_shares()} == {"Review", "External"}
    store.close()
    assert row.backend.db.jobs.count_documents({}) == 0
    assert row.backend.db.receipts.count_documents({}) == 0
    assert row.backend.db.operations.count_documents({}) == 0
    assert row.backend.db.backups.count_documents({}) == 0
    document = read_document(row.settings.resolved_data_dir)
    assert [
        client.credential.get_secret_value()
        for client in document.clients
        if client.enabled
    ] == [NEW_BOT]
    assert (
        next(
            client for client in document.clients if client.id == "original-bot"
        ).enabled
        is False
    )
    assert (
        row.backend.db.settings.find_one({"_id": "performance-preferences"})[
            "document"
        ]["timezone"]
        == "UTC"
    )
    assert row.backend.db.settings.find_one({"_id": "transfer-preferences"})[
        "document"
    ] == {"uploads_per_bot": 2}
    archive = (
        row.settings.resolved_data_dir
        / "recovery-archive"
        / identity
        / "mongodb-before.json"
    )
    saved = json.loads(read_private(archive))
    assert saved["jobs"][0]["_id"] == "old-running-job"
    assert saved["catalog"][0]["storage_channel_id"] == OLD
    assert archive.stat().st_mode & 0o777 == 0o600
    assert not list(row.settings.resolved_data_dir.glob("*.sqlite3"))
    assert not (row.settings.resolved_data_dir / "telegram-settings.json").exists()
    assert not apply_activation(pending, row.backend)


def test_live_mongo_aborted_catalog_write_rolls_back_files_settings_and_jobs(
    deployment, monkeypatch
):
    row = deployment
    prepare_activation(row.settings, row.candidate)
    before = MongoStore(row.backend).export()
    settings_before = list(row.backend.db.settings.find({}))
    from pymongo.synchronous.collection import Collection

    original = Collection.replace_one
    once = True

    def interrupt(collection, query, replacement, **kwargs):
        nonlocal once
        result = original(collection, query, replacement, **kwargs)
        if (
            once
            and collection.database.name == row.backend.db.name
            and collection.name == "catalog"
            and replacement.get("replica_activation_id")
        ):
            once = False
            raise RuntimeError("synthetic transaction interruption")
        return result

    monkeypatch.setattr(Collection, "replace_one", interrupt)
    with pytest.raises(RuntimeError, match="transaction interruption"):
        apply_activation(row.settings, row.backend)
    assert MongoStore(row.backend).export() == before
    assert list(row.backend.db.settings.find({})) == settings_before
    assert row.backend.db.jobs.find_one({"_id": "old-running-job"})
    assert row.backend.db.receipts.find_one({"_id": "old-receipt"})
    assert (
        json.loads(read_private(row.settings.resolved_data_dir / MANIFEST))["phase"]
        == "installing"
    )
    assert apply_activation(row.settings, row.backend)
    assert MongoStore(row.backend).storage_channel_id == NEW


def test_live_mongo_lost_commit_reply_reuses_marker_and_preserves_later_revision(
    deployment, monkeypatch
):
    row = deployment
    prepare_activation(row.settings, row.candidate)
    transaction = row.backend.transaction
    once = True

    def lose_reply(operation):
        nonlocal once
        result = transaction(operation)
        if once and operation.__name__ == "replace":
            once = False
            raise RuntimeError("synthetic lost commit reply")
        return result

    monkeypatch.setattr(row.backend, "transaction", lose_reply)
    with pytest.raises(RuntimeError, match="lost commit reply"):
        apply_activation(row.settings, row.backend)
    metadata = row.backend.db.catalog.find_one({"_id": "catalog"})
    assert metadata["storage_channel_id"] == NEW
    assert metadata["replica_activation_id"]
    assert row.backend.db.settings.find_one({"_id": "telegram"})["revision"] == 2
    assert apply_activation(row.settings, row.backend)
    assert (
        row.backend.db.catalog.find_one({"_id": "catalog"})["generation"]
        == metadata["generation"]
    )
    active = MongoStore(row.backend)
    active.create_folder("/", "Created after recovery")
    revision = active.revision
    active.close()
    assert not apply_activation(row.settings, row.backend)
    assert MongoStore(row.backend).revision == revision


def test_live_mongo_foreign_or_changed_catalog_cannot_be_overwritten(deployment):
    row = deployment
    foreign = copy.deepcopy(row.candidate)
    foreign["catalog"]["drive_id"] = "different-drive"
    with pytest.raises(ActivationError, match="different drive"):
        prepare_activation(row.settings, foreign)
    prepare_activation(row.settings, row.candidate)
    current = MongoStore(row.backend)
    current.create_folder("/", "Newer owner work")
    revision = current.revision
    current.close()
    with pytest.raises(ActivationError, match="metadata changed"):
        apply_activation(row.settings, row.backend)
    assert MongoStore(row.backend).revision == revision
    assert row.backend.db.jobs.find_one({"_id": "old-running-job"})


def test_live_mongo_same_channel_database_recovery_preserves_share_secret(deployment):
    row = deployment
    original = copy.deepcopy(row.candidate)
    original["catalog"] = copy.deepcopy(row.original)
    prepare_activation(row.settings, original)
    active = effective_settings(row.settings)
    assert active.storage_channel == OLD
    assert active.session_key == row.settings.session_key
    assert apply_activation(active, row.backend)
    reopened = MongoStore(row.backend)
    assert reopened.get_file(node_path(row.item))["message_id"] == 101
    assert all(
        reopened.authorize_file(node_path(row.item), token) for token in row.tokens
    )
    reopened.close()


@pytest.mark.parametrize("interrupted", [False, True])
def test_live_mongo_unified_file_restores_durable_state_without_replaying_work(
    deployment, monkeypatch, interrupted
):
    from argon2 import PasswordHasher
    from bson import Binary
    from pymongo.synchronous.collection import Collection

    from tgdrive.recovery.mongo_backup import COLLECTIONS
    from tgdrive.recovery.recovery_file import capture_database, snapshot_catalog
    from tgdrive.replication.replica_settings import credentials_from_snapshot
    from tgdrive.replication.replica_state import decode_bundle, encode_bundle

    row = deployment
    backend = row.backend
    # The legacy fixture's minimal job is replaced with a real journal record.
    backend.write(backend.db.jobs, "delete_one", {"_id": "old-running-job"})
    backend.write(
        backend.db.jobs,
        "insert_one",
        {
            "_id": "interrupted-v2-upload",
            "data": {
                "id": "interrupted-v2-upload",
                "phase": "uploading",
                "status": "running",
                "current": 3,
                "total": 6,
                "created_at": 1000.0,
                "updated_at": 1001.0,
                "context": {
                    "kind": "upload",
                    "name": "split.bin",
                    "local_path": "/old/upload.part",
                },
            },
        },
    )
    extra = {
        "_id": "extra-saved-policy",
        "document": {
            "at": datetime(2026, 10, 28, tzinfo=UTC),
            "value": Binary(b"durable", subtype=128),
        },
    }
    backend.write(backend.db.settings, "insert_one", extra)
    backend.write(backend.db.sessions, "insert_one", {"_id": "old-browser-session"})
    backend.write(
        backend.db.bot_folders,
        "insert_one",
        {"_id": "saved-bot-folder", "node_id": "root"},
    )
    before = {name: list(backend.db[name].find({})) for name in COLLECTIONS}
    database = capture_database(backend)
    original = snapshot_catalog(database)
    payload = {
        "database": database,
        "catalog": original,
        "replica_catalog": remap_catalog(
            original, -100555666, {"101": 2101, "102": 2102}
        ),
        "credentials": credentials_from_snapshot(row.settings, database),
        "created_at": "2026-10-28T10:00:00Z",
        "recovery_token": BACKUP_BOT,
    }
    restored_file = decode_bundle(
        encode_bundle(payload, row.candidate["key"]), row.candidate["key"]
    )
    candidate = copy.deepcopy(row.candidate)
    candidate.update(
        database=restored_file["database"],
        backup_config={"channel": -100555666, "token": BACKUP_BOT},
    )
    candidate["credentials"] = dict(restored_file["credentials"], bot_tokens=[NEW_BOT])
    prepare_activation(row.settings, candidate)
    active = effective_settings(row.settings)
    original_replace = Collection.replace_one
    fail_once = interrupted

    def interrupt(collection, query, replacement, **kwargs):
        nonlocal fail_once
        result = original_replace(collection, query, replacement, **kwargs)
        if (
            fail_once
            and collection.database.name == backend.db.name
            and collection.name == "catalog"
            and replacement.get("replica_activation_id")
        ):
            fail_once = False
            raise RuntimeError("synthetic unified transaction interruption")
        return result

    monkeypatch.setattr(Collection, "replace_one", interrupt)
    if interrupted:
        with pytest.raises(RuntimeError, match="unified transaction interruption"):
            apply_activation(active, backend)
        assert {name: list(backend.db[name].find({})) for name in COLLECTIONS} == before
    assert apply_activation(active, backend)
    reopened = MongoStore(backend)
    assert reopened.export() == candidate["catalog"]
    assert all(
        reopened.authorize_file(node_path(row.item), token) for token in row.tokens
    )
    reopened.close()
    assert backend.db.settings.find_one({"_id": extra["_id"]}) == extra
    assert backend.db.bot_folders.find_one({"_id": "saved-bot-folder"})
    assert not any(
        backend.db[name].count_documents({})
        for name in (
            "sessions",
            "identities",
            "operations",
            "deletions",
            "preparations",
            "receipts",
            "backups",
        )
    )
    history = list(backend.db.jobs.find({}))
    assert len(history) == 1 and history[0]["archived"] is True
    assert (
        history[0]["status"] == "cancelled"
        and history[0]["summary"]["name"] == "split.bin"
    )
    assert "data" not in history[0] and "context" not in history[0]["summary"]
    bootstrap = backend.db.settings.find_one({"_id": "bootstrap"})["document"]
    assert bootstrap["MONGODB_BACKUP_KEY"] == candidate["key"]
    assert bootstrap["STORAGE_CHANNEL"] == NEW and bootstrap["BOT_TOKENS"] == NEW_BOT
    assert PasswordHasher().verify(
        bootstrap["ADMIN_PASSWORD_HASH"], "synthetic recovery owner password"
    )
    assert not apply_activation(active, backend)


def test_live_mongo_unified_key_survives_losing_local_journal(deployment):
    from tgdrive.replication.replica import ChannelReplica

    row = deployment
    store = MongoStore(row.backend)
    original = store.export()
    settings_before = {
        entry["_id"]: entry for entry in row.backend.db.settings.find({})
    }
    legacy_key = row.settings.mongodb_backup_key.get_secret_value()
    unified_key = "a1" * 32
    assert unified_key != legacy_key
    runtime = SimpleNamespace(mongo=row.backend, store=store)
    first = ChannelReplica(row.settings, runtime)
    restored = None
    try:
        first.journal.set("key", unified_key)
        assert first.key() == unified_key
        assert row.backend.db.settings.find_one({"_id": "recovery-key"}) == {
            "_id": "recovery-key",
            "document": {"key": unified_key, "drive_id": store.drive_id},
        }
        reinstalled = row.settings.model_copy(
            update={
                "data_dir": row.settings.resolved_data_dir.parent / "reinstalled-data",
            }
        )
        restored = ChannelReplica(reinstalled, runtime)
        assert restored.journal.get("key") is None
        assert restored.key() == unified_key
        assert restored.journal.get("key") == unified_key
        assert reinstalled.mongodb_backup_key.get_secret_value() == legacy_key
        assert row.settings.mongodb_backup_key.get_secret_value() == legacy_key
        assert store.export() == original
        assert {
            entry["_id"]: entry
            for entry in row.backend.db.settings.find({"_id": {"$ne": "recovery-key"}})
        } == settings_before
    finally:
        first.journal.close()
        if restored is not None:
            restored.journal.close()
        store.close()
