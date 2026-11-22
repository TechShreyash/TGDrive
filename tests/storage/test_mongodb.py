"""Focused live MongoDB persistence checks using disposable, isolated databases.

Opt in with TGDRIVE_MONGO_TESTS=1. The configured TGDrive database is never used.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import closing, contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.support.mongo_fixtures import store_from_payload
from tgdrive.core.errors import DuplicateNameError, NotFoundError, ValidationError
from tgdrive.core.security import InvalidSession, SessionManager
from tgdrive.storage.mongo import DatabaseUnavailable, MongoBackend, configure
from tgdrive.storage.mongo_store import MongoStore, SharePasswordRequired
from tgdrive.telegram.client_identity import IdentityCache
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus

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


@contextmanager
def isolated_backend(tmp_path):
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
def backend(tmp_path):
    with isolated_backend(tmp_path) as instance:
        yield instance


@pytest.fixture
def store(backend):
    instance = MongoStore.create(backend)
    try:
        yield instance
    finally:
        instance.close()


def path(row):
    return row["path"].rstrip("/") + "/" + row["id"]


async def test_pristine_startup_builds_catalog_indexes_once(
    backend, tmp_path, monkeypatch
):
    from tests.support.app_fixtures import make_settings
    from tgdrive.recovery.mongo_backup import MongoProtection
    from tgdrive.storage.mongo import initialize_if_empty

    settings = make_settings(tmp_path)
    indexes = Mock(wraps=MongoStore._indexes)
    monkeypatch.setattr(MongoStore, "_indexes", staticmethod(indexes))

    assert initialize_if_empty(settings, backend)
    store = await MongoProtection(settings, None, backend).restore()
    try:
        indexes.assert_called_once_with(backend)
        folder = store.create_folder("/", "Reopened")
        assert store.get_directory(path(folder))["name"] == "Reopened"
        assert "active_sibling_names" in backend.db.nodes.index_information()
    finally:
        store.close()


def test_pending_deletion_survives_an_unrelated_precommit_receipt_read(
    store, backend, monkeypatch
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    target = store.create_folder("/", "Delete me")
    other = store.create_folder("/", "Keep me")
    store.add_file(path(target), "retained.txt", 77, 3)
    store.set_trashed(path(target), True)
    before = store.statistics()
    entered, release = threading.Event(), threading.Event()
    original = backend.fence

    def hold_before_commit(session):
        original(session)
        entered.set()
        assert release.wait(5), "The test did not release the committing deletion"

    monkeypatch.setattr(backend, "fence", hold_before_commit)
    with ThreadPoolExecutor(max_workers=1) as workers:
        deletion = workers.submit(store.begin_deletion, path(target))
        try:
            assert entered.wait(3)
            # Receipt destination inspection is read-only and sees the prior
            # committed state while the deletion transaction is still open.
            assert store.upload_destination(path(other))["id"] == other["id"]
        finally:
            release.set()
        intent = deletion.result(timeout=3)
    monkeypatch.setattr(backend, "fence", original)
    with pytest.raises(ValidationError, match="pending permanent deletion"):
        store.set_trashed(path(target), False)
    assert store.statistics() == before
    store.finish_deletion(intent["id"])
    after = store.statistics()
    assert after["files"] == after["trashed_files"] == after["trashed_bytes"] == 0
    assert store.get_directory(path(other))["name"] == "Keep me"


@pytest.mark.parametrize("mutation", ["move", "delete"])
def test_search_hydration_uses_the_candidate_snapshot(store, monkeypatch, mutation):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    first = store.create_folder("/", "First")
    second = store.create_folder("/", "Second")
    item = store.add_file(path(first), "snapshot result.txt", 77, 3)
    entered, release = threading.Event(), threading.Event()
    original = store._find

    def hold_hydration(collection, query, projection=None, **options):
        if (
            collection == "nodes"
            and isinstance(query.get("_id"), dict)
            and "$in" in query["_id"]
        ):
            entered.set()
            assert release.wait(5), "The test did not release search hydration"
        return original(collection, query, projection, **options)

    monkeypatch.setattr(store, "_find", hold_hydration)
    with ThreadPoolExecutor(max_workers=1) as workers:
        search = workers.submit(store.search_page, "snapshot result")
        try:
            assert entered.wait(3)
            if mutation == "move":
                store.move(path(item), path(second))
            else:
                store.set_trashed(path(item), True)
                intent = store.begin_deletion(path(item))
                store.finish_deletion(intent["id"])
        finally:
            release.set()
        result = search.result(timeout=3)
    assert [row["id"] for row in result["items"]] == [item["id"]]
    assert result["items"][0]["path"] == path(first)
    assert result["items"][0]["parent_names"] == ["First"]
    latest = store.search_page("snapshot result")["items"]
    assert [row["path"] for row in latest] == (
        [path(second)] if mutation == "move" else []
    )


def test_shared_page_cannot_read_private_rows_added_after_scope_changes(
    store, monkeypatch
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from tgdrive.storage.mongo_store import ShareDenied

    shared = store.create_folder("/", "Shared")
    child = store.create_folder(path(shared), "Child")
    private = store.create_folder("/", "Private")
    public_file = store.add_file(path(child), "public.txt", 77, 3)
    secret = store.add_file(path(private), "secret.txt", 78, 4)
    token = store.create_share(path(shared))
    entered, release = threading.Event(), threading.Event()
    original = store._authorized

    def hold_after_authorization(chain, supplied, unlock=None):
        result = original(chain, supplied, unlock)
        if result is not None:
            entered.set()
            assert release.wait(5), "The test did not release shared page loading"
        return result

    monkeypatch.setattr(store, "_authorized", hold_after_authorization)
    with ThreadPoolExecutor(max_workers=1) as workers:
        listing = workers.submit(store.list_page, path(child), share_token=token)
        try:
            assert entered.wait(3)
            moved = store.move(path(child), path(private))
            store.move(path(secret), path(moved))
        finally:
            release.set()
        result = listing.result(timeout=3)
    assert [row["id"] for row in result["items"]] == [public_file["id"]]
    assert all(row["id"] != secret["id"] for row in result["items"])
    with pytest.raises(ShareDenied):
        store.list_page("/" + child["id"], share_token=token)


def test_performance_history_survives_restart_without_local_sqlite(
    backend, tmp_path, monkeypatch
):
    import sqlite3

    from tgdrive.observability.telemetry_history import (
        RETENTION_SECONDS,
        TelemetryHistory,
    )

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

    from tgdrive.core.config import Settings
    from tgdrive.storage.mongo import bootstrap_values

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
        "ENCRYPTION_KEY": "e" * 64,
    }
    for key, value in fixture.items():
        monkeypatch.setenv(key, value)
    for key in ("SESSION_SECRET", "ADMIN_PASSWORD_HASH"):
        monkeypatch.delenv(key, raising=False)
    first = Settings(_env_file=None)
    catalog = MongoStore.create(backend)
    catalog.bind_storage_channel(first.storage_channel)
    drive_id = catalog.drive_id
    backend.write(
        backend.db.settings,
        "replace_one",
        {"_id": "bootstrap"},
        {"_id": "bootstrap", "document": bootstrap_values(first)},
        upsert=True,
    )
    browser_session = SessionManager(
        first.session_key, 3600, mongo_backend=backend
    ).create()
    for key in ("API_ID", "API_HASH", "BOT_TOKENS", "STORAGE_CHANNEL"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "second-server"))
    reopened = Settings(_env_file=None)
    assert reopened.session_secret == first.session_secret
    assert reopened.admin_password_hash is not None
    assert PasswordHasher().verify(
        reopened.admin_password_hash.get_secret_value(), fixture["ADMIN_PASSWORD"]
    )
    assert MongoStore(backend).drive_id == drive_id
    assert SessionManager(reopened.session_key, 3600, mongo_backend=backend).verify(
        browser_session
    )
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


def test_parts_idempotency_shares_and_catalog_deletion(store, backend):
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
    assert store.list_trash() == {}
    with pytest.raises(ValidationError):
        store.confirm_deletion_messages(intent["id"], [103])
    store.confirm_deletion_messages(intent["id"], [101])
    reopened = MongoStore(backend)
    assert reopened.pending_deletions()[0]["pending_message_ids"] == [102]
    assert reopened.list_trash() == {}
    store.fail_deletion(intent["id"], "Backup temporarily unavailable")
    assert set(store.list_trash()) == {item["id"]}
    store.confirm_deletion_messages(intent["id"], [])
    assert store.list_trash() == {}
    assert store.finish_deletion(intent["id"]) == [101, 102]
    assert store.finish_deletion(intent["id"]) == []
    assert store.pending_deletions() == []
    completed = backend.db.deletions.find_one({"_id": intent["id"]})
    assert completed["message_ids"] == completed["pending_message_ids"] == []
    assert backend.db.nodes.find_one({"_id": item["id"]}) is None
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


async def test_interrupted_bot_intake_review_survives_mongodb_reopen(backend):
    registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    plan = {"dispatched": True, "random_id": 123}
    job = await registry.create(
        "interrupted", context={"bot_intake": plan, "source_kind": "telegram"}
    )
    assert job.bot_intake_review_required
    await registry.review_bot_intake(job.id)

    reopened = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    restored = reopened.get(job.id)
    assert restored.status is TransferStatus.ERROR
    assert restored.phase == "bot_intake_reviewed"
    assert restored.context["bot_intake"] == plan | {"reviewed_at": restored.updated_at}
    assert not restored.bot_intake_review_required


async def test_jobs_sessions_names_and_connection_settings_use_mongodb(
    backend, tmp_path
):
    from pydantic import SecretStr

    from tgdrive.settings.managed_settings import (
        ManagedClient,
        ManagedDocument,
        read_document,
        save_document,
    )

    registry = TransferRegistry(mongo=backend.db.jobs)
    job = await registry.create("queued", total=9)
    job.context.update(kind="upload", name="file.txt", retry_at=123.0)
    await registry.persist(job)
    reopened = TransferRegistry(mongo=backend.db.jobs)
    assert reopened.get(job.id).context == job.context
    await reopened.update(job.id, status=TransferStatus.COMPLETED)
    assert TransferRegistry(mongo=backend.db.jobs).get(job.id).terminal
    sessions = SessionManager(b"x" * 32, 3600, mongo_backend=backend)
    token = sessions.create()
    assert sessions.verify(token)
    assert SessionManager(b"x" * 32, 3600, mongo_backend=backend).verify(token)
    sessions.revoke(token)
    with pytest.raises(InvalidSession):
        sessions.verify(token)
    cache = IdentityCache(tmp_path)
    cache.write("bot-123", {"display_name": "Example", "username": "example_bot"})
    cache.defer("bot-123", 12345.0)
    cached = cache.read("bot-123")
    assert cached is not None and cached["display_name"] == "Example"
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
    saved = read_document(tmp_path)
    assert saved is not None and saved.revision == 1
    assert not list(tmp_path.glob("*.sqlite3"))
    assert not (tmp_path / "telegram-settings.json").exists()


def test_receipts_survive_new_instance_without_sqlite(backend, store, tmp_path):
    from tgdrive.transfers.uploads import ResumableUploads

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

    with patch("tgdrive.storage.mongo.for_settings", return_value=backend):
        receipt = ResumableUploads(settings, manager)
        item = receipt.create("file.txt", "/", 12)
        reopened = ResumableUploads(settings, manager)
        assert reopened.get_unchecked(item["id"])["total_size"] == 12
        receipt.close()
        reopened.close()
    assert not list(tmp_path.glob("*.sqlite3"))


def test_readers_see_only_committed_writes(store, backend):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered = threading.Event()
    proceed = threading.Event()
    original = backend.fence

    def delayed(session):
        entered.set()
        assert proceed.wait(5)
        return original(session)

    backend.fence = delayed
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(store.create_folder, "/", "Pending")
            assert entered.wait(5)
            try:
                # The insert is inside an open transaction: readers cannot see it.
                assert store.get_directory("/")["contents"] == {}
            finally:
                proceed.set()
            node = task.result(timeout=10)
    finally:
        backend.fence = original
    assert node["id"] in store.get_directory("/")["contents"]


def test_lost_commit_acknowledgement_replays_safely(store, backend, monkeypatch):
    original = backend.session_transaction
    failed = False

    from contextlib import contextmanager

    @contextmanager
    def lost():
        nonlocal failed
        with original() as session:
            yield session
        if not failed:
            failed = True
            raise DatabaseUnavailable("Synthetic lost acknowledgement")

    monkeypatch.setattr(backend, "session_transaction", lost)
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

    from tgdrive.recovery.mongo_backup import MongoProtection

    settings = SimpleNamespace(
        storage_channel=-1001234567890,
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


async def test_transfer_journal_cannot_write_after_owner_takeover(backend):
    registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    record = await registry.create("queued", total=10)
    backend.db.runtime.update_one({"_id": "owner"}, {"$set": {"token": "new-owner"}})
    with pytest.raises(DatabaseUnavailable):
        await registry.update(record.id, status=TransferStatus.COMPLETED)
    assert record.status is TransferStatus.QUEUED
    assert backend.db.jobs.find_one({"_id": record.id})["data"]["status"] == "queued"


@pytest.mark.asyncio
async def test_transfer_history_archive_survives_pruning_and_reopen(backend):
    import json
    import time

    registry = TransferRegistry(
        mongo=backend.db.jobs, mongo_backend=backend, ttl_seconds=1
    )
    record = await registry.create("uploading", total=100)
    record.context.update(
        name="history.bin",
        kind="upload",
        destination="/",
        source_url="https://private/?token=secret",
        local_path="/private/staging",
    )
    await registry.update(record.id, status=TransferStatus.COMPLETED, current=100)
    await registry.prune(now=time.time() + 2)
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
    assert (await reopened.history(query="HISTORY.BIN"))["jobs"][0]["id"] == record.id
    assert (await reopened.history(query=".*"))["jobs"] == []
    assert page["jobs"][0]["retained"] is False
    registry.close()
    reopened.close()


@pytest.mark.asyncio
async def test_archived_search_casefold_and_cursor_are_literal(backend):
    import time

    registry = TransferRegistry(
        mongo=backend.db.jobs, mongo_backend=backend, ttl_seconds=1
    )
    names = [
        "Straße [.*] 0.bin",
        "STRASSE [.*] 1.bin",
        "strasse [.*] 2.bin",
        "ΟΣ.txt",
        "unrelated.bin",
    ]
    for index, name in enumerate(names):
        record = await registry.create("uploading", total=10)
        record.context.update(name=name, kind="upload", destination="/")
        record.created_at = 1000 + index
        await registry.update(record.id, status=TransferStatus.COMPLETED, current=10)
    await registry.prune(now=time.time() + 2)
    first = await registry.history(query="STRASSE [.*]", limit=2)
    assert [row["name"] for row in first["jobs"]] == names[1:3][::-1]
    assert first["has_more"]
    second = await registry.history(
        query="strasse [.*]",
        limit=2,
        before=(first["next_before"], first["next_before_id"]),
    )
    assert [row["name"] for row in second["jobs"]] == [names[0]]
    assert not second["has_more"]
    assert (await registry.history(query="\u03bf\u03c3"))["jobs"][0]["name"] == "ΟΣ.txt"
    assert (await registry.history(query=".*"))["jobs"][0]["name"] == names[2]
    assert backend.db.jobs.count_documents({"search_name": {"$exists": False}}) == 0
    registry.close()


def test_transfer_locations_follow_exact_moved_and_deleted_item(store):
    from tests.transfers.test_transfer_locations import check_locations

    check_locations(store)


def test_share_listing_copyability_expiry_and_move(store, backend, monkeypatch):
    from pydantic import SecretStr

    from tgdrive.web.share_links import token_factory

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
    monkeypatch.setattr(
        "tgdrive.storage.mongo_store._utc_now", lambda: "9999-01-01T00:00:00Z"
    )
    assert reopened.list_shares() == []
    assert len(reopened.list_shares(include_expired=True)) == 1
    assert not reopened.authorize_file(record["path"], token)
    reopened.close()


async def test_operator_performance_policy_survives_new_service_with_mongodb(
    backend, tmp_path
):
    from unittest.mock import AsyncMock

    from tgdrive.settings.performance_preferences import PerformanceSettings

    def state():
        return SimpleNamespace(
            mongo=backend,
            pool=SimpleNamespace(
                _slots=[],
                set_media_per_bot=AsyncMock(),
                set_media_queue_limit=AsyncMock(),
            ),
        )

    settings = SimpleNamespace(
        resolved_data_dir=tmp_path / "policy-data",
        max_file_size=0,
        staging_budget_bytes=16 * 1024**3,
    )
    first, second = state(), state()
    service = PerformanceSettings(settings, first)
    try:
        before = await service.snapshot()
        saved = await service.save(
            {"timezone": "UTC", "auto_preload": False},
            expected=before["revision"],
        )
        restarted = PerformanceSettings(settings, second)
        observed = await restarted.snapshot()
        assert observed["revision"] == saved["revision"]
        assert observed["timezone"] == "UTC"
        assert observed["auto_preload"] is False
        assert not (
            settings.resolved_data_dir / "performance-preferences.json"
        ).exists()
    finally:
        for runtime in (first, second):
            manager = getattr(runtime, "preloads", None)
            if manager is not None:
                await manager.close()


def test_multiple_independent_share_links_survive_mongo_reopen_and_snapshot(
    store, backend
):
    from datetime import UTC, datetime, timedelta

    folder = store.create_folder("/", "Reading")
    target = path(folder)
    first = store.create_share(target, label="Alex", expires_at=None)
    second = store.create_share(target, label="Team")
    first_id = next(
        row["id"] for row in store.list_shares(target) if row["label"] == "Alex"
    )
    store.update_share(
        first_id,
        label="Alex review",
        expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
    )
    reopened = MongoStore(backend)
    assert reopened.authorize_directory(target, first) == target
    assert reopened.authorize_directory(target, second) == target
    assert {row["label"] for row in reopened.list_shares(target)} == {
        "Alex review",
        "Team",
    }
    portable = store_from_payload(reopened.export())
    try:
        assert portable.authorize_directory(target, first) == target
        assert portable.authorize_directory(target, second) == target
    finally:
        portable.close()
    reopened.revoke_share(first_id)
    assert not reopened.authorize_directory(target, first)
    assert reopened.authorize_directory(target, second) == target


def test_protected_share_stays_locked_after_mongo_reopen_and_recovery(store, backend):
    from tgdrive.web.share_links import share_password_hasher

    file_path = path(store.add_file("/", "Report.pdf", 42, 100))
    password_hash = share_password_hasher.hash("fixture-password")
    token = store.create_share(file_path, password_hash=password_hash)
    reopened = MongoStore(backend)
    portable = store_from_payload(reopened.export())
    try:
        for catalog in (reopened, portable):
            assert catalog.list_shares(file_path)[0]["password_required"]
            with pytest.raises(SharePasswordRequired):
                catalog.authorize_file(file_path, token)
            assert catalog.authorize_file(
                file_path, token, share_unlock=lambda value: value == password_hash
            )
    finally:
        portable.close()


def test_shared_file_lookup_revalidates_scope_password_and_expiry(
    store, backend, monkeypatch
):
    from tgdrive.storage import mongo_store
    from tgdrive.storage.mongo_store import ShareDenied
    from tgdrive.web.share_links import share_password_hasher

    folder = path(store.create_folder("/", "Shared"))
    other = path(store.create_folder("/", "Private"))
    item = store.add_file(folder, "Report.pdf", 42, 100)
    short_path = "/" + item["id"]
    original = share_password_hasher.hash("first-fixture-password")
    replacement = share_password_hasher.hash("second-fixture-password")
    token = store.create_share(folder, password_hash=original)
    share_id = store.list_shares(folder)[0]["id"]

    def unlock(value):
        return value == original

    def separate_share_read(_token):
        pytest.fail("Shared file delivery should use the joined share document")

    monkeypatch.setattr(store, "_active_share", separate_share_read)
    assert (
        store.get_file(short_path, share_token=token, share_unlock=unlock)["id"]
        == item["id"]
    )
    assert "requested_share" not in store.get_file(
        short_path, share_token=token, share_unlock=unlock
    )
    store.update_share(share_id, password_hash=replacement)
    with pytest.raises(SharePasswordRequired):
        store.get_file(short_path, share_token=token, share_unlock=unlock)

    store.update_share(share_id, password_hash=None, expires_at="2099-01-01T00:00:00Z")
    with monkeypatch.context() as expired:
        expired.setattr(mongo_store, "_utc_now", lambda: "2099-01-01T00:00:00Z")
        with pytest.raises(ShareDenied):
            store.get_file(short_path, share_token=token)
    assert store.get_file(short_path, share_token=token)["message_id"] == 42

    store.move(short_path, other)
    with pytest.raises(ShareDenied):
        store.get_file(short_path, share_token=token)
    assert store.get_file(short_path)["message_id"] == 42
    assert backend.db.shares.count_documents({}) == 1


def test_share_opens_concurrently_deduplicate_and_restore_legacy_counts(store, backend):
    from concurrent.futures import ThreadPoolExecutor

    from tgdrive.storage.mongo_store import ShareDenied

    item = path(store.add_file("/", "Report.pdf", 42, 100))
    token = store.create_share(item)
    row = store.list_shares(item)[0]
    assert row["opens_since"] == row["created_at"]
    # Older portable snapshots have neither field.
    originals = store.export()
    originals["shares"][0].pop("open_count")
    originals["shares"][0].pop("opens_since")
    legacy = store_from_payload(originals)
    try:
        assert legacy.list_shares()[0]["open_count"] == 0
        assert legacy.list_shares()[0]["opens_since"] is None
        legacy.record_share_open(item, token, "1" * 32)
        assert legacy.list_shares()[0]["open_count"] == 1
        assert legacy.list_shares()[0]["opens_since"]
    finally:
        legacy.close()

    identities = [f"{index:032x}" for index in range(12)] * 3
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(
            workers.map(
                lambda event: store.record_share_open(item, token, event), identities
            )
        )
    assert backend.db.share_open_events.count_documents({}) == 12
    assert (
        backend.db.share_open_events.index_information()["expire_at_1"][
            "expireAfterSeconds"
        ]
        == 0
    )
    store.update_share(row["id"], label="Review copy")
    reopened = MongoStore(backend)
    assert reopened.list_shares()[0]["open_count"] == 12
    portable = reopened.export()
    assert "share_open_events" not in portable and identities[0] not in repr(portable)
    restored = store_from_payload(portable)
    try:
        assert restored.list_shares()[0]["open_count"] == 12
        assert restored.list_shares()[0]["label"] == "Review copy"
        assert reopened.revoke_share(row["id"])
        assert not reopened.list_shares()
        with pytest.raises(ShareDenied):
            reopened.record_share_open(item, token, "f" * 32)
    finally:
        restored.close()
        reopened.close()


def test_starred_mongo_paths_snapshot_and_restart(store, backend):
    from tests.web.test_starred import check_starred

    check_starred(store)
    assert len(MongoStore(backend).list_starred()) == 1


def test_marked_and_share_pages_keep_mongodb_reads_bounded(tmp_path, monkeypatch):
    from tests.support.mixed_workload_probe import MongoCommands
    from tgdrive.storage import mongo

    commands = MongoCommands()
    mongo_client = mongo.MongoClient

    def monitored_client(*args, **kwargs):
        return mongo_client(*args, event_listeners=[commands], **kwargs)

    monkeypatch.setattr(mongo, "MongoClient", monitored_client)

    def read_page(view, read):
        before = commands.snapshot()
        page = read()
        observed = commands.snapshot() - before
        queries = sum(
            count
            for (_, command), count in observed.items()
            if command in {"find", "aggregate", "getMore", "count", "distinct"}
        )
        assert 0 < queries <= 4, (view, observed)
        return page

    with (
        isolated_backend(tmp_path) as backend,
        closing(MongoStore.create(backend)) as store,
    ):
        commands.database = backend.db.name
        items = []
        for index in range(26):
            folder = path(store.create_folder("/", f"Folder {index:02d}"))
            item = path(store.add_file(folder, f"File {index:02d}.txt", index + 100, 1))
            items.append(item)
            store.set_starred(item, True)
            store.create_share(item)

        starred = read_page(
            "starred", lambda: store.list_marked_page("starred", limit=25)
        )
        first_ids = [item.rsplit("/", 1)[-1] for item in items[:25]]
        assert list(starred["contents"]) == first_ids
        assert starred["next_cursor"]

        shares = read_page("shares", lambda: store.list_shares_page(limit=25))
        shared_paths = {row["path"] for row in shares["shares"]}
        assert shared_paths.issubset(items)
        assert len(shared_paths) == 25 and shares["next_cursor"]

        for item in items:
            store.set_trashed(item, True)
        trash = read_page("trash", lambda: store.list_marked_page("trash", limit=25))
        assert list(trash["contents"]) == first_ids
        assert trash["next_cursor"]


def test_legacy_node_migration_survives_restart_and_skips_completed_scans(
    store, backend, monkeypatch
):
    node = store.add_file("/", "legacy.txt", 77, 3)
    identity = node["_id"] if "_id" in node else node["id"]
    backend.db.catalog.update_one(
        {"_id": "catalog"}, {"$unset": {"node_fields_version": ""}}
    )
    backend.db.nodes.update_one(
        {"_id": identity},
        {
            "$set": {"id": identity, "fold": "legacy.txt", "grams": ["leg"]},
            "$unset": {"group": "", "tokens": ""},
        },
    )

    # The backfill is safe to repeat if the process dies before marking it done.
    store._migrate_node_fields(backend)
    backend.db.catalog.update_one(
        {"_id": "catalog"}, {"$unset": {"node_fields_version": ""}}
    )
    MongoStore(backend).close()
    migrated = backend.db.nodes.find_one({"_id": identity})
    assert migrated is not None
    assert migrated["group"] and "legacy" in migrated["tokens"]
    assert not {"id", "fold", "grams"} & migrated.keys()

    def unexpected(_backend):
        pytest.fail("Completed node migration ran again")

    monkeypatch.setattr(MongoStore, "_migrate_node_fields", staticmethod(unexpected))
    MongoStore(backend).close()


@pytest.mark.asyncio
async def test_indexed_numbered_history_matches_legacy_across_rebuild_and_archive(
    backend, monkeypatch
):
    import time

    from tgdrive.transfers import task_history_index
    from tgdrive.transfers.task_history_index import HISTORY_INDEX

    monkeypatch.setattr(task_history_index, "_BATCH", 2)
    folder = {
        "id": "summer-photos",
        "name": "Śummer folder",
        "created_at": 15,
        "root_path": "/summer",
    }
    other = {
        "id": "older-folder",
        "name": "Other",
        "created_at": 1,
        "root_path": "/older",
    }
    members = [
        ("scan-a", 40, "completed", "report [*.txt]", None),
        ("scan-b", 36, "completed", "old report", None),
        ("scan-c", 35, "error", "other error", None),
        ("scan-d", 33, "completed", "Straße.jpg", folder),
        ("scan-e", 25, "cancelled", "nested picture", folder),
        ("scan-f", 8, "completed", "other nested picture", other),
    ]
    backend.db.jobs.insert_many(
        [
            {
                "_id": identity,
                "archived": True,
                "created_at": at,
                "status": status,
                "summary": {
                    "id": identity,
                    "kind": "upload",
                    "created_at": at,
                    "status": status,
                    "name": name,
                    **({"folder_upload": parent} if parent else {}),
                },
            }
            for identity, at, status, name, parent in members
        ]
    )
    registry = TransferRegistry(
        mongo=backend.db.jobs, mongo_backend=backend, ttl_seconds=1
    )
    index = registry._history_index
    assert index is not None
    index.ensure_indexes()
    assert not index.rebuild_step()
    header = backend.db.runtime.find_one({"_id": HISTORY_INDEX})
    assert not header["ready"] and header["total_tasks"] == 2
    # Simulate resuming a partial generation written before totals were stored.
    backend.db.runtime.update_one(
        {"_id": HISTORY_INDEX}, {"$unset": {"total_tasks": ""}}
    )
    created = await registry.create(
        "running",
        total=10,
        transfer_id="live-child",
        context={"kind": "upload", "name": "Live photo", "folder_upload": folder},
    )
    created.created_at = 18
    await registry.update(created.id, status=TransferStatus.RUNNING)
    live = [registry.history_snapshot(record) for record in registry.list()]
    pending = dict(
        status="all", page=1, limit=2, query="", offset=None, exclude_folders=()
    )
    legacy = registry._numbered_history(live, **pending)
    assert index.numbered(live, **pending) is None
    # Reopen on the durable backfill cursor and complete the index.
    registry.close()
    registry = TransferRegistry(
        mongo=backend.db.jobs, mongo_backend=backend, ttl_seconds=1
    )
    try:
        assert await registry.task_history(limit=2) == legacy
        builder = registry._history_builder
        if builder is not None:
            await builder
        header = backend.db.runtime.find_one({"_id": HISTORY_INDEX})
        assert header["ready"] and header["total_tasks"] == 5
        reopened_index = registry._history_index
        assert reopened_index is not None
        with monkeypatch.context() as no_count:

            def unexpected_count(*args, **kwargs):
                raise AssertionError("Unfiltered history recounted archived tasks")

            no_count.setattr(
                type(reopened_index.tasks), "count_documents", unexpected_count
            )
            assert reopened_index.numbered(live, **pending) == legacy

        for status in ("all", "finished", "running", "completed", "error", "cancelled"):
            for query in ("", "strasse", "[*.txt]", "śummer", "nested", "no matches"):
                for page, offset in (
                    (1, None),
                    (2, None),
                    (99, None),
                    (1, 2),
                    (1, 4),
                    (1, 99),
                ):
                    options = pending | {
                        "status": status,
                        "query": query,
                        "page": page,
                        "offset": offset,
                    }
                    reopened_index = registry._history_index
                    assert reopened_index is not None
                    assert reopened_index.numbered(
                        live, **options
                    ) == registry._numbered_history(live, **options)

        # The derived collection and members change together when clearing.
        assert await registry.clear_history(exclude_folders=(other["id"],)) == 3
        after = await registry.task_history(limit=2)
        assert after["total_tasks"] == 2
        assert backend.db.runtime.find_one({"_id": HISTORY_INDEX})["total_tasks"] == 2
        assert {row["id"] for row in after["jobs"]} == {
            "scan-d",
            "scan-e",
            "scan-f",
            "live-child",
        }

        await registry.update("live-child", status=TransferStatus.COMPLETED)
        await registry.prune(now=time.time() + 2)
        assert (await registry.task_history(status="cancelled"))["total_tasks"] == 1
        assert backend.db.runtime.find_one({"_id": HISTORY_INDEX})["total_tasks"] == 2

        separate = await registry.create(
            "running", total=1, context={"kind": "upload", "name": "Standalone"}
        )
        await registry.update(separate.id, status=TransferStatus.COMPLETED)
        await registry.prune(now=time.time() + 2)
        assert backend.db.runtime.find_one({"_id": HISTORY_INDEX})["total_tasks"] == 3

        # A source replay of an archived member must retire it from the index.
        reused = await registry.create(
            "queued",
            transfer_id="scan-e",
            context={"kind": "upload", "name": "Replay"},
        )
        assert (await registry.task_history(status="cancelled"))["total_tasks"] == 0
        assert (await registry.task_history(status="queued"))["jobs"][0][
            "id"
        ] == reused.id
        assert backend.db.runtime.find_one({"_id": HISTORY_INDEX})["total_tasks"] == 3
        expected = await registry.task_history()
        assert expected["total_tasks"] == 4

        # A ready generation without a saved total still serves current history.
        backend.db.runtime.update_one(
            {"_id": HISTORY_INDEX}, {"$unset": {"total_tasks": ""}}
        )
        assert await registry.task_history() == expected
        builder = registry._history_builder
        if builder is not None:
            await builder
        assert backend.db.runtime.find_one({"_id": HISTORY_INDEX})["total_tasks"] == 3
    finally:
        await registry.shutdown()
        registry.close()


def test_large_deletion_manifest_resumes_with_small_buckets(
    store, backend, monkeypatch
):
    from tgdrive.storage import mongo_store
    from tgdrive.storage.deletion_batches import BUCKET_SIZE

    monkeypatch.setattr(mongo_store, "_BATCH", 2)

    folder = store.create_folder("/", "Delete multipart tree")
    unrelated = store.add_file("/", "Retain.bin", 77_777, 1)
    root = path(folder)
    branch = store.create_folder(root, "Branch")
    nested = store.create_folder(path(branch), "Nested")
    empty = store.create_folder(root, "Empty")
    surviving_share = store.create_share(path(unrelated))
    store.create_share(root)
    store.create_share(path(branch))
    store.create_share(path(nested))
    store.create_share(path(empty))
    checksum = "a" * 64
    count = BUCKET_SIZE + 18
    parts = [
        {"index": i, "offset": i, "size": 1, "message_id": i + 1, "sha256": checksum}
        for i in range(count)
    ]
    poster_id = 2 * BUCKET_SIZE + 1
    file = store.add_file(
        path(nested),
        "parts.mkv",
        1,
        count,
        parts=parts,
        sha256=checksum,
        poster={"message_id": poster_id, "size": 8, "sha256": checksum},
    )
    store.set_trashed(root, True)
    assert store.authorize_file(path(unrelated), surviving_share)
    assert backend.db.shares.count_documents({}) == 1
    intent = store.begin_deletion(root, channel_id=-100123)
    identity = intent["id"]
    assert intent["message_count"] == count + 1
    assert intent["message_ids"] == []
    batches = backend.db.deletions.find({"intent_id": identity}).sort("bucket", 1)
    assert [len(part["message_ids"]) for part in batches] == [BUCKET_SIZE, 18, 1]
    assert store.list_trash() == {}

    # A valid confirmation earlier in the request must roll back when a later
    # ID is unrelated. Replaying an already confirmed ID must also be safe.
    with pytest.raises(ValidationError, match="unrelated"):
        store.confirm_deletion_messages(
            identity, [1, unrelated.get("message_id", 77_777)]
        )
    assert (
        1
        in backend.db.deletions.find_one({"_id": identity + ":0"})[
            "pending_message_ids"
        ]
    )
    store.confirm_deletion_messages(identity, [1, BUCKET_SIZE + 1])
    store.fail_deletion(identity, "simulated interruption")
    reopened = MongoStore(backend)
    try:
        restored = reopened.deletion_intent(identity)
        assert restored is not None and restored["state"] == "failed"
        assert reopened.begin_deletion(root)["id"] == identity
        reopened.confirm_deletion_messages(identity, [1, BUCKET_SIZE + 1])
        pending = reopened.export()["deletions"][0]["pending_message_ids"]
        assert len(pending) == count - 1
        assert poster_id in pending
        assert 1 not in pending and BUCKET_SIZE + 1 not in pending
        assert reopened.finish_deletion(identity, return_messages=False) == []
        assert reopened.finish_deletion(identity) == []
        assert reopened.pending_deletions() == []
        assert reopened.message_ids(path(unrelated)) == [77_777]
        removed = [row["id"] for row in (folder, branch, nested, empty, file)]
        assert backend.db.nodes.count_documents({"_id": {"$in": removed}}) == 0
        assert backend.db.deletions.count_documents({"intent_id": identity}) == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("batched_manifest", [False, True])
def test_staged_deletion_recovers_with_connected_tree_and_shared_references(
    store, backend, monkeypatch, batched_manifest
):
    from tgdrive.storage import mongo_store

    monkeypatch.setattr(mongo_store, "_BATCH", 2)
    target = path(store.create_folder("/", "Delete"))
    first = path(store.create_folder(target, "First"))
    second = path(store.create_folder(first, "Second"))
    messages = [41] * 5
    if batched_manifest:
        monkeypatch.setattr(mongo_store, "BUCKET_SIZE", 2)
        messages = [41, 41, 41, 45, 2500]
    files = [
        store.add_file(second, f"File {i}", message, 3)
        for i, message in enumerate(messages)
    ]
    unrelated = path(store.add_file("/", "Keep", 41, 3))
    share = store.create_share(unrelated)
    store.set_trashed(target, True)
    intent = store.begin_deletion(target)
    original = store._remove_nodes
    committed = 0

    def interrupt_after_first_batch(identities, deadline):
        nonlocal committed
        if committed:
            raise RuntimeError("stopped between committed deletion batches")
        count = original(identities, deadline)
        committed += count
        return count

    with monkeypatch.context() as interrupted:
        interrupted.setattr(store, "_remove_nodes", interrupt_after_first_batch)
        with pytest.raises(RuntimeError, match="between committed"):
            store.finish_deletion(intent["id"])
    assert committed == 2
    assert store.statistics()["trashed_files"] == 3
    remaining = sorted(
        {
            row["message_id"]
            for row in backend.db.nodes.find(
                {"_id": {"$in": [file["id"] for file in files]}}
            )
        }
    )
    assert 41 in remaining
    retained = store.deletion_intent(intent["id"])
    assert retained is not None
    if batched_manifest:
        assert retained["message_count"] == len(remaining)
    else:
        assert retained["message_ids"] == remaining
    snapshot = store_from_payload(store.export())
    try:
        assert snapshot.statistics()["trashed_bytes"] == 9
        exported = snapshot.deletion_intent(intent["id"])
        assert exported is not None
        assert exported["message_ids"] == remaining
        assert snapshot.get_file(unrelated, share_token=share)["message_id"] == 41
    finally:
        snapshot.close()

    reopened = MongoStore(backend)
    try:
        assert reopened.finish_deletion(intent["id"]) == remaining
        assert reopened.finish_deletion(intent["id"]) == []
        assert reopened.pending_deletions() == []
        assert reopened.statistics()["trashed_files"] == 0
        assert reopened.get_file(unrelated, share_token=share)["message_id"] == 41
    finally:
        reopened.close()


@pytest.mark.parametrize("outside_links", [1, 5])
def test_trash_revokes_links_across_large_subtree_batches(
    store, monkeypatch, outside_links
):
    from tgdrive.storage import mongo_store

    monkeypatch.setattr(mongo_store, "_BATCH", 3)
    target = path(store.create_folder("/", "Target"))
    branch = path(store.create_folder(target, "Branch"))
    inside = path(store.add_file(branch, "Inside.bin", 1, 1))
    store.add_file(target, "First.bin", 2, 1)
    store.add_file(branch, "Second.bin", 3, 1)
    store.create_share(target)
    store.create_share(inside)
    outside = path(store.add_file("/", "Outside.bin", 4, 1))
    kept = [store.create_share(outside) for _ in range(outside_links)]
    ancestry_batches = []
    chains = store._chains

    def track_chains(identities):
        ancestry_batches.append(len(identities))
        return chains(identities)

    monkeypatch.setattr(store, "_chains", track_chains)
    store.set_trashed(target, True)
    assert ancestry_batches == ([3] if outside_links == 1 else [])
    store.set_trashed(target, False)
    assert store.list_shares(target) == []
    assert store.list_shares(inside) == []
    assert len(store.list_shares(outside)) == outside_links
    for token in kept:
        assert store.get_file(outside, share_token=token)["message_id"] == 4
