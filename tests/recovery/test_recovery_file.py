"""Unified recovery files keep one consistent database and both channel mappings."""

import secrets
import zlib
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bson import json_util
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tests.support.mongo_fixtures import FakeMongoBackend
from tests.support.recovery_fixtures import settings as recovery_settings
from tgdrive.recovery import recovery_file
from tgdrive.recovery.mongo_backup import COLLECTIONS, decode_snapshot
from tgdrive.replication.replica_settings import credentials_from_snapshot
from tgdrive.replication.replica_state import (
    ReplicaJournal,
    decode_bundle,
    encode_bundle,
    files_in,
    parts_in,
    remap_catalog,
)
from tgdrive.storage.mongo_store import MongoStore

KEY = "a1" * 32
MAIN = -1001234567890
BACKUP = -1002234567890


@pytest.fixture
def source(tmp_path):
    settings = recovery_settings(tmp_path)
    backend = FakeMongoBackend()
    backend.client = SimpleNamespace(
        start_session=Mock(return_value=nullcontext(object()))
    )
    store = MongoStore.create(backend, storage_channel=MAIN)
    store.add_file("/", "Report.txt", 41, 3)
    backend.db.settings.insert_one(
        {
            "_id": "telegram",
            "revision": 1,
            "document": {
                "schema_version": 1,
                "revision": 1,
                "api_id": 12345,
                "api_hash": "a" * 32,
                "clients": [
                    {
                        "id": "bot",
                        "label": "Storage",
                        "kind": "bot",
                        "enabled": True,
                        "credential": settings.bot_tokens[0],
                        "runtime_name": None,
                    },
                    {
                        "id": "account",
                        "label": "Premium",
                        "kind": "user",
                        "enabled": True,
                        "credential": "sensitive-telegram-session",
                        "runtime_name": None,
                    },
                ],
            },
        }
    )
    backend.db.settings.insert_one(
        {
            "_id": "bootstrap",
            "document": {
                "STRING_SESSIONS": "sensitive-telegram-session",
                "MONGODB_BACKUP_KEY": KEY,
            },
        }
    )
    backend.db.sessions.insert_one(
        {"_id": "browser-login", "token": "sensitive-cookie"}
    )
    backend.db.jobs.insert_one(
        {
            "_id": "finished-job",
            "status": "completed",
            "archived": True,
            "summary": {"name": "Stored upload"},
            "finished_at": datetime(2026, 10, 28, tzinfo=UTC),
        }
    )
    try:
        yield settings, backend, store
    finally:
        store.close()


def bundle(source):
    settings, backend, _store = source
    database = recovery_file.capture_database(backend)
    catalog = recovery_file.snapshot_catalog(database)
    mapping = {
        str(part["message_id"]): part["message_id"] + 100
        for node in files_in(catalog)
        for part in parts_in(node)
    }
    return {
        "catalog": catalog,
        "replica_catalog": remap_catalog(catalog, BACKUP, mapping),
        "database": database,
        "credentials": credentials_from_snapshot(settings, database),
        "created_at": "2026-10-28T10:00:00Z",
        "recovery_token": "234567:" + "b" * 30,
    }


def open_raw(content):
    offset = len(recovery_file.MAGIC)
    return zlib.decompress(
        AESGCM(bytes.fromhex(KEY)).decrypt(
            content[offset : offset + 12],
            content[offset + 12 :],
            recovery_file.MAGIC,
        )
    )


def seal_raw(raw):
    nonce = secrets.token_bytes(12)
    return (
        recovery_file.MAGIC
        + nonce
        + AESGCM(bytes.fromhex(KEY)).encrypt(
            nonce,
            zlib.compress(raw),
            recovery_file.MAGIC,
        )
    )


def test_unified_file_has_one_catalog_copy_and_preserves_database_history(source):
    value = bundle(source)
    encrypted = encode_bundle(value, KEY)
    raw = json_util.loads(open_raw(encrypted))
    assert "catalog" not in raw and "replica_catalog" not in raw
    assert set(raw["replica"]) == {"channel", "messages"}
    assert b"Report.txt" not in encrypted
    decoded = decode_bundle(encrypted, KEY)
    assert decoded["catalog"] == value["catalog"]
    assert decoded["replica_catalog"] == value["replica_catalog"]
    assert decoded["database"] == value["database"]
    assert decoded["recovery_token"] == value["recovery_token"]
    assert decoded["database"]["jobs"][0]["finished_at"].tzinfo is not None


def test_capture_omits_logged_in_sessions_without_changing_source(source):
    _settings, backend, _store = source
    value = bundle(source)
    raw = json_util.dumps(value)
    assert "sensitive-telegram-session" not in raw
    assert "sensitive-cookie" not in raw
    assert value["database"]["sessions"] == []
    assert backend.db.sessions.find_one({})["token"] == "sensitive-cookie"
    assert (
        len(backend.db.settings.find_one({"_id": "telegram"})["document"]["clients"])
        == 2
    )


def test_standalone_file_restores_database_through_existing_archive_reader(
    source, tmp_path
):
    value = bundle(source)
    value["replica_catalog"] = None
    path = tmp_path / "tgdrive-backup.tgdb"
    path.write_bytes(encode_bundle(value, KEY))
    decoded = decode_bundle(path.read_bytes(), KEY)
    assert decoded["replica_catalog"] is None
    assert decode_snapshot(path, bytes.fromhex(KEY)) == value["database"]


def test_newer_live_catalog_cannot_be_mixed_with_captured_database(source):
    value = bundle(source)
    source[2].create_folder("/", "Later")
    value["catalog"] = source[2].export()
    with pytest.raises(ValueError, match="different restore points"):
        encode_bundle(value, KEY)


def test_rebased_recovery_file_uses_replacement_channel_and_retains_safe_history(
    source,
):
    value = bundle(source)
    replacement = value["replica_catalog"]
    original_database = value["database"]
    rebased = recovery_file.rebase_database(original_database, replacement)
    assert recovery_file.snapshot_catalog(rebased) == replacement
    assert rebased["jobs"] == original_database["jobs"]
    assert rebased["sessions"] == rebased["operations"] == rebased["deletions"] == []
    assert rebased["preparations"] == rebased["receipts"] == []
    assert recovery_file.snapshot_catalog(original_database) == value["catalog"]
    value.update(database=rebased, catalog=replacement, replica_catalog=None)
    restored = decode_bundle(encode_bundle(value, KEY), KEY)
    assert restored["catalog"]["storage_channel_id"] == BACKUP
    assert restored["database"]["jobs"] == original_database["jobs"]


@pytest.mark.parametrize(
    "damage", ["key", "ciphertext", "mapping", "duplicate", "nonfinite", "expanded"]
)
def test_invalid_unified_files_never_expose_restore_state(source, monkeypatch, damage):
    content = encode_bundle(bundle(source), KEY)
    key = KEY
    if damage == "key":
        key = "b2" * 32
    elif damage == "ciphertext":
        content = content[:-1] + bytes([content[-1] ^ 1])
    elif damage == "mapping":
        value = json_util.loads(open_raw(content))
        value["replica"]["messages"]["999"] = 999
        content = seal_raw(json_util.dumps(value).encode())
    elif damage == "duplicate":
        content = seal_raw(
            open_raw(content).replace(b'"version":2', b'"version":2,"version":2', 1)
        )
    elif damage == "nonfinite":
        value = json_util.loads(open_raw(content))
        value["database"]["jobs"][0]["extra"] = float("nan")
        content = seal_raw(json_util.dumps(value).encode())
    else:
        monkeypatch.setattr(recovery_file, "MAX_EXPANDED", 32)
    with pytest.raises(ValueError, match="nothing changed"):
        decode_bundle(content, key)


def test_capture_uses_one_shared_mongodb_snapshot_session(source, monkeypatch):
    _settings, backend, _store = source
    observed = []
    for name in COLLECTIONS:
        collection = backend.db[name]
        find = collection.find

        def record(*args, _find=find, **kwargs):
            observed.append(kwargs["session"])
            return _find(*args, **kwargs)

        monkeypatch.setattr(collection, "find", record)
    recovery_file.capture_database(backend)
    backend.client.start_session.assert_called_once_with(snapshot=True)
    assert len(observed) == len(COLLECTIONS)
    assert all(session is observed[0] for session in observed)


def test_replica_journal_retains_bson_datetimes(tmp_path):
    journal = ReplicaJournal(tmp_path)
    value = {"finished_at": datetime(2026, 10, 28, tzinfo=UTC)}
    try:
        journal.set("database", value)
        assert journal.get("database") == value
    finally:
        journal.close()


def test_standalone_recovery_file_keeps_free_space_reserve(
    source, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from tgdrive.recovery.backup import BackupError

    configured, backend, _store = source
    path = tmp_path / "blocked-backup.tgdb"
    monkeypatch.setattr(
        recovery_file.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0)
    )
    with pytest.raises(BackupError, match="temporary space"):
        recovery_file.write_recovery_snapshot(
            backend, path, bytes.fromhex(KEY), configured
        )
    assert not path.exists()
