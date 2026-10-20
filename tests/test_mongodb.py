"""Focused live MongoDB persistence checks using disposable, isolated databases.

Opt in with TGDRIVE_MONGO_TESTS=1. The configured TGDrive database is never used.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from types import SimpleNamespace

import pytest

from tgdrive.client_identity import IdentityCache
from tgdrive.errors import DuplicateNameError, NotFoundError, ValidationError
from tgdrive.mongo import DatabaseUnavailable, MongoBackend, configure
from tgdrive.mongo_backup import decode_snapshot, restore_snapshot, write_snapshot
from tgdrive.mongo_store import MongoStore
from tgdrive.security import InvalidSession, SessionManager
from tgdrive.tasks import TransferRegistry, TransferStatus

pytestmark = pytest.mark.skipif(
    os.environ.get("TGDRIVE_MONGO_TESTS") != "1",
    reason="Set TGDRIVE_MONGO_TESTS=1 for isolated live MongoDB checks",
)


def mongo_uri():
    uri = os.environ.get("TGDRIVE_TEST_MONGODB_URI")
    if not uri:
        raise pytest.UsageError(
            "Set TGDRIVE_TEST_MONGODB_URI to an isolated MongoDB replica set"
        )
    return uri


@pytest.fixture
def backend(tmp_path):
    uri = mongo_uri()
    name = "tgdrive_test_" + secrets.token_hex(8)
    instance = configure(tmp_path, uri, name)
    instance.acquire()
    try:
        yield instance
    finally:
        instance.release()
        assert instance.db.name == name and name.startswith("tgdrive_test_")
        instance.client.drop_database(name)
        instance.client.close()


@pytest.fixture
def store(backend):
    instance = MongoStore.create(backend)
    try:
        yield instance
    finally:
        instance.close()


def path(row):
    return row["path"].rstrip("/") + "/" + row["id"]


def test_performance_history_survives_restart_without_local_sqlite(
    backend, tmp_path, monkeypatch
):
    import sqlite3

    from tgdrive.telemetry_history import RETENTION_SECONDS, TelemetryHistory

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_a, **_k: pytest.fail("SQLite used for Mongo telemetry"),
    )

    def sample(at):
        return dict(
            at=at,
            cpu_percent=25,
            cpu_scope="cgroup",
            memory_bytes=1024,
            memory_limit_bytes=8192,
            received_bytes_per_second=100,
            sent_bytes_per_second=50,
            network_interface="eth0",
        )

    def counters(value):
        return dict(
            network=dict(available=True, received_bytes=value, sent_bytes=value * 2),
            upload_bytes=value * 3,
            download_bytes=value * 4,
            requests=value,
            errors=0,
        )

    history = TelemetryHistory(tmp_path)
    history.observe(sample(960), counters(100))
    history.observe(sample(963), counters(200))
    history.flush(force=True, now=964)
    history.flush(force=True, now=964)
    restarted = TelemetryHistory(tmp_path)
    restarted.observe(sample(966), counters(5))
    restarted.observe(sample(969), counters(10))
    restarted.flush(force=True, now=970)
    result = TelemetryHistory(tmp_path).query(960, 1020, now=1100)
    assert result["samples"][0]["memory_bytes"] == 1024
    assert result["samples"][0]["coverage_seconds"]["cpu_percent"] == 6
    assert result["totals"]["network_received_bytes"] == 210
    assert result["tracked_since"] == 960
    assert backend.db.telemetry_minutes.count_documents({}) == 1
    assert not (tmp_path / "telemetry.sqlite3").exists()
    now = 970 + RETENTION_SECONDS + 120
    restarted.observe(sample(now), counters(20))
    restarted.flush(force=True, now=now)
    assert backend.db.telemetry_minutes.count_documents({}) == 0
    assert (
        backend.db.telemetry_state.find_one({"_id": "lifetime"})["totals"][
            "network_received_bytes"
        ]
        == 220
    )


def test_container_keys_and_owner_survive_move_without_local_volume(
    backend, tmp_path, monkeypatch
):
    from argon2 import PasswordHasher

    from tgdrive.config import Settings
    from tgdrive.mongo import save_bootstrap

    fixture = {
        "TGDRIVE_AUTO_SECRETS": "true",
        "MONGODB_URI": mongo_uri(),
        "MONGODB_DATABASE": backend.db.name,
        "DATA_DIR": str(tmp_path / "first-server"),
        "API_ID": "12345",
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30,
        "STORAGE_CHANNEL": "-1001234567890",
        "ADMIN_PASSWORD": "synthetic deployment owner password",
    }
    for key, value in fixture.items():
        monkeypatch.setenv(key, value)
    for key in ("SESSION_SECRET", "MONGODB_BACKUP_KEY", "ADMIN_PASSWORD_HASH"):
        monkeypatch.delenv(key, raising=False)
    first = Settings(_env_file=None)
    catalog = MongoStore.create(backend)
    catalog.bind_storage_channel(first.storage_channel)
    drive_id = catalog.drive_id
    save_bootstrap(first, backend)
    browser_session = SessionManager(
        first.session_key, 3600, first.resolved_data_dir / "auth.sqlite3"
    ).create()
    for key in ("API_ID", "API_HASH", "BOT_TOKENS", "STORAGE_CHANNEL"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "second-server"))
    reopened = Settings(_env_file=None)
    assert reopened.session_secret == first.session_secret
    assert reopened.mongodb_backup_key == first.mongodb_backup_key
    assert PasswordHasher().verify(
        reopened.admin_password_hash.get_secret_value(), fixture["ADMIN_PASSWORD"]
    )
    assert MongoStore(backend).drive_id == drive_id
    assert SessionManager(
        reopened.session_key, 3600, reopened.resolved_data_dir / "auth.sqlite3"
    ).verify(browser_session)
    assert not (tmp_path / "second-server" / "deployment-secrets.json").exists()


def test_catalog_roundtrip_without_local_sqlite(store, backend, monkeypatch):
    import sqlite3

    monkeypatch.setattr(
        sqlite3, "connect", lambda *_a, **_k: pytest.fail("SQLite used")
    )
    store.bind_storage_channel(-1001234567890)
    folder = store.create_folder("/", "Résumé")
    item = store.add_file(
        path(folder), "notes.txt", 42, 128, operation_id="operation123"
    )
    reopened = MongoStore(backend)
    assert reopened.export() == store.export()
    assert reopened.get_file(path(item))["message_id"] == 42
    assert reopened.upload_destination("/", directory_id=folder["id"])["path"] == path(
        folder
    )


def test_atomic_batch_rollback_and_savepoint(store, backend):
    first = store.create_folder("/", "First")
    before = store.export()
    with pytest.raises(NotFoundError), store.metadata_batch():
        store.rename(path(first), "Changed")
        store.rename("/missing", "Fail")
    assert store.export() == before
    assert MongoStore(backend).export() == before
    with store.metadata_batch():
        store.rename(path(first), "Final")
        with pytest.raises(DuplicateNameError):
            store.create_folder("/", "FINAL")
        second = store.create_folder("/", "Second")
    assert (
        MongoStore(backend).get_directory("/")["contents"][second["id"]]["name"]
        == "Second"
    )
    with store.metadata_batch():
        store.rename(path(first), "Temporary")
        store.rename(path(second), "Final")
        store.rename(path(first), "Second")
    loaded = MongoStore(backend).get_directory("/")["contents"]
    assert loaded[first["id"]]["name"] == "Second"
    assert loaded[second["id"]]["name"] == "Final"


def test_folder_manifest_retry_move_search_and_uniqueness(store):
    result = store.prepare_folder_tree(
        "/", "Root", [["Inner"]], "folder-request-id-12345"
    )
    assert (
        store.prepare_folder_tree("/", "Root", [["Inner"]], "folder-request-id-12345")
        == result
    )
    inner = result["folders"]["Inner"]
    item = store.add_file(inner, "100%_notes.txt", 43, 7)
    destination = store.create_folder("/", "Destination")
    store.move(path(result["root"]), path(destination))
    current = store.upload_destination(inner, directory_id=inner.rsplit("/", 1)[-1])
    assert current["path"].startswith(path(destination))
    matches = store.search_page("%_")
    assert len(matches["items"]) == 1 and matches["items"][0]["id"] == item["id"]
    assert matches["items"][0]["parent_names"] == ["Destination", "Root", "Inner"]
    with pytest.raises(DuplicateNameError):
        store.create_folder("/", "destination")


def test_parts_idempotency_shares_and_delete_confirmations(store, backend):
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
    kwargs = dict(
        operation_id="multipart-op",
        parts=parts,
        sha256=hashlib.sha256(b"abcdef").hexdigest(),
    )
    item = store.add_file("/", "large.bin", 101, 6, **kwargs)
    assert store.add_file("/", "large.bin", 101, 6, **kwargs) == item
    token = store.create_share(path(item))
    assert store.authorize_file(path(item), token)
    store.set_trashed(path(item), True)
    assert not store.authorize_file(path(item), token)
    intent = store.begin_deletion(path(item), channel_id=-100123)
    assert intent["message_ids"] == [101, 102]
    with pytest.raises(ValidationError):
        store.confirm_deletion_messages(intent["id"], [103])
    store.confirm_deletion_messages(intent["id"], [101])
    with pytest.raises(ValidationError):
        store.finish_deletion(intent["id"])
    reopened = MongoStore(backend)
    assert reopened.pending_deletions()[0]["pending_message_ids"] == [102]
    store.confirm_deletion_messages(intent["id"], [102])
    assert store.finish_deletion(intent["id"]) == [101, 102]
    assert store.has_file_operation("multipart-op")
    with pytest.raises(ValidationError):
        store.add_file("/", "large.bin", 101, 6, **kwargs)


def test_ownership_excludes_second_process_and_fences_expired_writer(store, backend):
    other = MongoBackend(mongo_uri(), backend.db.name)
    try:
        with pytest.raises(DatabaseUnavailable):
            other.acquire()
        backend.db.runtime.update_one(
            {"_id": "owner"}, {"$set": {"token": "different-owner"}}
        )
        before = store.export()
        with pytest.raises(DatabaseUnavailable):
            store.create_folder("/", "Must not commit")
        assert store.export() == before
        assert backend.db.nodes.count_documents({"name": "Must not commit"}) == 0
    finally:
        other.client.close()


def test_jobs_sessions_names_and_connection_settings_use_mongodb(backend, tmp_path):
    from pydantic import SecretStr

    from tgdrive.managed_settings import (
        ManagedClient,
        ManagedDocument,
        read_document,
        save_document,
    )

    registry = TransferRegistry(mongo=backend.db.jobs)
    job = registry.create("queued", total=9)
    job.context.update(kind="upload", name="file.txt", retry_at=123.0)
    registry.persist(job)
    reopened = TransferRegistry(mongo=backend.db.jobs)
    assert reopened.get(job.id).context == job.context
    reopened.update(job.id, status=TransferStatus.COMPLETED)
    assert TransferRegistry(mongo=backend.db.jobs).get(job.id).terminal
    sessions = SessionManager(b"x" * 32, 3600, tmp_path / "auth.sqlite3")
    token = sessions.create()
    assert sessions.verify(token)
    assert SessionManager(b"x" * 32, 3600, tmp_path / "auth.sqlite3").verify(token)
    sessions.revoke(token)
    with pytest.raises(InvalidSession):
        sessions.verify(token)
    cache = IdentityCache(tmp_path)
    cache.write("bot-123", {"display_name": "Example", "username": "example_bot"})
    cache.defer("bot-123", 12345.0)
    assert cache.read("bot-123")["display_name"] == "Example"
    assert cache.retry_at("bot-123") == 12345.0
    document = ManagedDocument(
        revision=1,
        api_id=12345,
        api_hash=SecretStr("a" * 32),
        clients=[
            ManagedClient(
                id="bot-test",
                label="Bot",
                kind="bot",
                credential=SecretStr("123456:AA" + "x" * 24),
            )
        ],
    )
    save_document(tmp_path, document, 0)
    assert read_document(tmp_path).revision == 1
    assert not list(tmp_path.glob("*.sqlite3"))
    assert not (tmp_path / "telegram-settings.json").exists()


def test_encrypted_complete_snapshot_roundtrip_and_tamper_rejection(
    store, backend, tmp_path
):
    from tgdrive.backup import BackupError

    item = store.add_file("/", "safe.txt", 51, 10)
    backend.db.settings.insert_one({"_id": "private", "token": "fixture-private-token"})
    backup = tmp_path / "snapshot.tgdb"
    key = secrets.token_bytes(32)
    counts = write_snapshot(backend, backup, key)
    assert counts["settings"] == 1
    assert b"fixture-private-token" not in backup.read_bytes()
    decoded = decode_snapshot(backup, key)
    assert decoded["settings"][0]["token"] == "fixture-private-token"
    with pytest.raises(BackupError):
        decode_snapshot(backup, secrets.token_bytes(32))
    name = "tgdrive_test_" + secrets.token_hex(8)
    target = MongoBackend(mongo_uri(), name)
    try:
        restore_snapshot(target, backup, key)
        restored = MongoStore(target)
        assert restored.get_file(path(item))["message_id"] == 51
        assert (
            target.db.settings.find_one({"_id": "private"})["token"]
            == "fixture-private-token"
        )
        with pytest.raises(FileExistsError):
            restore_snapshot(target, backup, key)
    finally:
        assert name.startswith("tgdrive_test_")
        target.client.drop_database(name)
        target.client.close()
    content = bytearray(backup.read_bytes())
    content[-20] ^= 1
    backup.write_bytes(content)
    with pytest.raises(BackupError):
        decode_snapshot(backup, key)


def test_receipts_survive_new_instance_without_sqlite(backend, store, tmp_path):
    from tgdrive.uploads import ResumableUploads

    settings = SimpleNamespace(
        resolved_data_dir=tmp_path,
        resolved_cache_dir=tmp_path / "cache",
        max_concurrent_transfers=2,
        upload_limit=1024**3,
        staging_budget_bytes=1024**3,
        min_free_bytes=0,
        upload_session_ttl_seconds=3600,
    )
    manager = SimpleNamespace(
        store=store, registry=TransferRegistry(mongo=backend.db.jobs)
    )
    # Explicit backend already bound to this private directory.
    from unittest.mock import patch

    with patch("tgdrive.mongo.for_settings", return_value=backend):
        receipt = ResumableUploads(settings, manager)
        item = receipt.create("file.txt", "/", 12)
        reopened = ResumableUploads(settings, manager)
        assert reopened.get_unchecked(item["id"])["total_size"] == 12
        receipt.close()
        reopened.close()
    assert not list(tmp_path.glob("*.sqlite3"))


def test_readers_see_last_committed_snapshot_without_waiting_for_cloud(
    store, backend, monkeypatch
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered = threading.Event()
    proceed = threading.Event()
    original = backend.transaction

    def delayed(callback):
        entered.set()
        assert proceed.wait(5)
        return original(callback)

    monkeypatch.setattr(backend, "transaction", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(store.create_folder, "/", "Pending")
        assert entered.wait(5)
        try:
            assert store.get_directory("/")["contents"] == {}
        finally:
            proceed.set()
        node = task.result(timeout=10)
    assert node["id"] in store.get_directory("/")["contents"]


def test_lost_commit_acknowledgement_reloads_authoritative_catalog(
    store, backend, monkeypatch
):
    original = backend.transaction
    failed = False

    def lost(callback):
        nonlocal failed
        result = original(callback)
        if not failed:
            failed = True
            raise DatabaseUnavailable("Synthetic lost acknowledgement")
        return result

    monkeypatch.setattr(backend, "transaction", lost)
    with pytest.raises(DatabaseUnavailable):
        store.add_file("/", "safe.txt", 77, 2**32, operation_id="lost-ack-operation")
    row = store.add_file("/", "safe.txt", 77, 2**32, operation_id="lost-ack-operation")
    assert row["size"] == 2**32
    assert backend.db.nodes.count_documents({"name": "safe.txt"}) == 1
    assert MongoStore(backend).get_file(path(row))["size"] == 2**32


def test_primary_database_commits_do_not_wait_for_telegram_backups(
    store, backend, tmp_path
):
    import asyncio

    from tgdrive.mongo_backup import MongoProtection

    settings = SimpleNamespace(
        storage_channel=-1001234567890,
        mongodb_backup_key=None,
        resolved_cache_dir=tmp_path,
        mongodb_backup_seconds=3600,
    )
    service = MongoProtection(settings, object(), backend)
    store.bind_storage_channel(settings.storage_channel)
    item = store.add_file("/", "saved.txt", 81, 1)
    assert asyncio.run(service.ensure_revision(store, store.revision)) == store.revision
    assert backend.db.nodes.find_one({"_id": item["id"]}) is not None
    assert service.protection_status(store)["state"] == "protected"
    assert backend.db.backups.count_documents({}) == 0


def test_transfer_journal_cannot_write_after_owner_takeover(backend):
    registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    record = registry.create("queued", total=10)
    backend.db.runtime.update_one({"_id": "owner"}, {"$set": {"token": "new-owner"}})
    with pytest.raises(DatabaseUnavailable):
        registry.update(record.id, status=TransferStatus.COMPLETED)
    assert record.status is TransferStatus.QUEUED
    assert backend.db.jobs.find_one({"_id": record.id})["data"]["status"] == "queued"


@pytest.mark.asyncio
async def test_transfer_history_archive_survives_pruning_and_reopen(backend):
    import json
    import time

    registry = TransferRegistry(
        mongo=backend.db.jobs, mongo_backend=backend, ttl_seconds=1
    )
    record = registry.create("uploading", total=100)
    record.context.update(
        name="history.bin",
        kind="upload",
        destination="/",
        source_url="https://private/?token=secret",
        local_path="/private/staging",
    )
    registry.update(record.id, status=TransferStatus.COMPLETED, current=100)
    registry.prune(now=time.time() + 2)
    assert registry.list() == []
    row = backend.db.jobs.find_one({"_id": record.id})
    assert row["archived"] is True
    assert "data" not in row
    assert "context" not in row["summary"]
    assert "secret" not in json.dumps(row)
    reopened = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    assert reopened.list() == []
    page = await reopened.history(status="completed", limit=1)
    assert page["jobs"][0]["name"] == "history.bin"
    assert page["jobs"][0]["retained"] is False
    registry.close()
    reopened.close()


def test_transfer_locations_follow_exact_moved_and_deleted_item(store):
    from test_transfer_locations import check_locations

    check_locations(store)


def test_share_listing_copyability_expiry_and_move(store, backend, monkeypatch):
    from pydantic import SecretStr

    from tgdrive.share_links import token_factory

    factory = token_factory(SimpleNamespace(session_secret=SecretStr("s" * 48)))
    parent = store.create_folder("/", "Destination")
    item = store.add_file("/", "report.txt", 42, 4)
    original = path(item)
    token = store.create_share(original, token_factory=factory)
    record = store.list_shares(include_expired=True, token_factory=factory)[0]
    assert record["copyable"] and factory(record["id"]) == token
    assert token not in str(store.export())
    store.move(original, path(parent))
    store.rename(store.resolve_id(item["id"]), "renamed.txt")
    reopened = MongoStore(backend)
    record = reopened.list_shares(token_factory=factory)[0]
    assert record["folder_label"] == "My Files / Destination"
    assert record["name"] == "renamed.txt" and record["copyable"]
    assert reopened.authorize_file(record["path"], token)
    monkeypatch.setattr("tgdrive.mongo_store._utc_now", lambda: "9999-01-01T00:00:00Z")
    assert reopened.list_shares() == []
    assert len(reopened.list_shares(include_expired=True)) == 1
    assert not reopened.authorize_file(record["path"], token)
    reopened.close()


async def test_operator_performance_policy_survives_new_service_with_mongodb(
    backend, tmp_path
):
    from unittest.mock import AsyncMock

    from tgdrive.performance_preferences import PerformanceSettings

    def state():
        return SimpleNamespace(
            mongo=backend,
            pool=SimpleNamespace(
                _slots=[],
                set_media_per_bot=AsyncMock(),
                set_media_queue_limit=AsyncMock(),
            ),
        )

    settings = SimpleNamespace(resolved_data_dir=tmp_path / "policy-data")
    first, second = state(), state()
    service = PerformanceSettings(settings, first)
    try:
        before = await service.snapshot()
        saved = await service.save(
            {"timezone": "UTC", "telegram_media_per_bot": 4},
            expected=before["revision"],
        )
        restarted = PerformanceSettings(settings, second)
        observed = await restarted.snapshot()
        assert observed["revision"] == saved["revision"]
        assert observed["timezone"] == "UTC"
        assert observed["telegram_media_per_bot"] == 4
        assert not service.path.exists()
    finally:
        for runtime in (first, second):
            manager = getattr(runtime, "preloads", None)
            if manager is not None:
                await manager.close()
