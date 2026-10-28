"""Small MongoDB failure/scheduling checks without network access."""

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr
from pymongo.errors import ConnectionFailure

from tests.support.mongo_fixtures import FakeMongoBackend
from tests.support.recovery_fixtures import settings as recovery_settings
from tgdrive.core.logging import _redact
from tgdrive.recovery.backup import BackupError
from tgdrive.recovery.mongo_backup import (
    MongoProtection,
    check_restore_destination,
    decode_snapshot,
    write_snapshot,
)
from tgdrive.recovery.recovery_file import write_recovery_snapshot
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.transfers.tasks import TransferRegistry


async def test_recent_snapshot_survives_restart_without_immediate_duplicate(
    monkeypatch,
):
    latest = {"confirmed_at": datetime.now(UTC).isoformat()}
    backend = SimpleNamespace(
        db=SimpleNamespace(
            backups=SimpleNamespace(
                find_one=lambda query, **_kwargs: (
                    latest if query["status"] == "confirmed" else None
                )
            )
        )
    )
    service = MongoProtection(
        SimpleNamespace(mongodb_backup_seconds=3600), object(), backend
    )
    service.snapshot_now = AsyncMock()
    intervals = []

    async def stop_wait(awaitable, timeout):
        intervals.append(timeout)
        awaitable.close()
        return True

    monkeypatch.setattr("tgdrive.recovery.mongo_backup.asyncio.wait_for", stop_wait)
    await service._loop(object())
    assert len(intervals) == 1 and 3590 < intervals[0] <= 3600
    service.snapshot_now.assert_not_called()


async def test_failed_cloud_journal_does_not_admit_an_unrecorded_job():
    def fail(*_args, **_kwargs):
        raise ConnectionFailure("Synthetic database outage")

    registry = TransferRegistry(
        mongo=SimpleNamespace(find=lambda _query: [], replace_one=fail)
    )
    with pytest.raises(DatabaseUnavailable):
        await registry.create("queued")
    assert registry.list() == []


async def test_startup_read_cannot_rewind_concurrent_backup_confirmation(monkeypatch):
    pending = {
        "id": "publishing",
        "status": "pending",
        "created_at": "2026-10-27T01:00:00+00:00",
    }
    confirmed = dict(
        pending, status="confirmed", confirmed_at="2026-10-27T01:01:00+00:00"
    )
    loop = asyncio.get_running_loop()

    def read(query, **_kwargs):
        if query["status"] == "pending":
            return pending
        loop.call_soon_threadsafe(service._observe_backup, confirmed)
        return None

    backend = SimpleNamespace(
        db=SimpleNamespace(backups=SimpleNamespace(find_one=read))
    )
    service = MongoProtection(
        SimpleNamespace(mongodb_backup_seconds=3600), object(), backend
    )

    async def stop_wait(awaitable, timeout):
        awaitable.close()
        return True

    monkeypatch.setattr("tgdrive.recovery.mongo_backup.asyncio.wait_for", stop_wait)
    await service._loop(object())
    assert service.latest_history() == [confirmed]
    assert service._last_success_at == confirmed["confirmed_at"]


def test_mongodb_connection_credentials_are_redacted():
    uri = "mongodb+srv://fixture_user:fixture_password@cluster.invalid/?appName=test"
    result = _redact("Database failed: " + uri)
    assert "fixture_user" not in result and "fixture_password" not in result
    assert "[REDACTED_MONGODB_URI]" in result


@pytest.mark.parametrize("forced", [False, True])
async def test_encrypted_snapshot_checks_live_channel_before_staging_or_send(
    tmp_path, monkeypatch, forced
):
    cache = tmp_path / "cache"
    backend = SimpleNamespace(
        check_owner=Mock(),
        db=SimpleNamespace(backups=SimpleNamespace(find_one=Mock())),
    )
    pool = SimpleNamespace(lease=Mock())
    service = MongoProtection(
        SimpleNamespace(storage_channel=-1001234567890, resolved_cache_dir=cache),
        pool,
        backend,
    )
    store = SimpleNamespace(storage_channel_id=-1001234567890, revision=1)
    service.verify_lineage = AsyncMock(
        side_effect=SettingsProblem("The storage channel is public.")
    )
    write = Mock()
    monkeypatch.setattr("tgdrive.recovery.mongo_backup.write_recovery_snapshot", write)
    with pytest.raises(SettingsProblem, match="channel is public"):
        if forced:
            await service.backup(store, force=True)
        else:
            await service.snapshot_now(store)
    service.verify_lineage.assert_awaited_once_with(store)
    backend.db.backups.find_one.assert_not_called()
    pool.lease.assert_not_called()
    write.assert_not_called()
    assert not cache.exists()


@pytest.fixture
def snapshot_source(tmp_path):
    backend = FakeMongoBackend()
    backend.client = SimpleNamespace(
        start_session=Mock(side_effect=lambda **_kwargs: nullcontext(object()))
    )
    store = MongoStore.create(backend, storage_channel=-1001234567890)
    settings = recovery_settings(tmp_path).model_copy(
        update={"cache_dir": tmp_path, "mongodb_backup_key": SecretStr("a1" * 32)},
    )
    pool = SimpleNamespace(lease=Mock(side_effect=RuntimeError("Publication paused")))
    yield MongoProtection(settings, pool, backend), store, backend
    store.close()


async def test_snapshot_record_uses_exported_revision_when_catalog_advances(
    snapshot_source, monkeypatch
):
    service, store, backend = snapshot_source
    exported_revision = store.revision

    def export_then_advance(*args):
        result = write_recovery_snapshot(*args)
        store.create_folder("/", "Arrived after the snapshot")
        return result

    monkeypatch.setattr(
        "tgdrive.recovery.mongo_backup.write_recovery_snapshot", export_then_advance
    )
    with pytest.raises(RuntimeError, match="Publication paused"):
        await service.snapshot_now(store)
    record = backend.db.backups.find_one({"status": "pending"})
    decoded = decode_snapshot(
        service.settings.resolved_cache_dir
        / "mongo-backups"
        / (record["id"] + ".tgdb"),
        bytes.fromhex("a1" * 32),
    )
    assert record["revision"] == decoded["catalog"][0]["revision"] == exported_revision
    assert store.revision == exported_revision + 1
    assert len(decoded["nodes"]) == 1
    assert len(backend.db.nodes.find({})) == 2
    observed = service.latest_history()[0]
    assert observed["id"] == record["id"]
    assert observed["status"] == "pending"
    assert "_id" not in observed and "local_path" not in observed
    assert service._last_success_at is None


async def test_legacy_journal_work_still_creates_original_archive_format(
    snapshot_source,
):
    service, store, backend = snapshot_source
    with pytest.raises(RuntimeError, match="Publication paused"):
        await service.snapshot_now(store, legacy=True)
    row = backend.db.backups.find_one({"status": "pending"})
    assert row["format"] == "mongodb-encrypted-v1"
    assert Path(row["local_path"]).read_bytes().startswith(b"TGDRIVE-MONGO-1\n")
    assert decode_snapshot(Path(row["local_path"]), bytes.fromhex("a1" * 32))["catalog"]


async def test_standalone_v2_uses_unified_replica_key_and_preserves_pending_key_binding(
    snapshot_source,
):
    service, store, backend = snapshot_source
    key = "b2" * 32
    service.replica = SimpleNamespace(key=lambda: key)
    with pytest.raises(RuntimeError, match="Publication paused"):
        await service.snapshot_now(store)
    row = backend.db.backups.find_one({"status": "pending"})
    assert row["format"] == "tgdrive-backup-v2"
    assert decode_snapshot(Path(row["local_path"]), bytes.fromhex(key))["catalog"]
    service.replica.key = lambda: "c3" * 32
    with pytest.raises(BackupError, match="different key"):
        await service.snapshot_now(store)
    assert backend.db.backups.find_one({"status": "pending"}) == row


async def test_backup_observation_reloads_on_restore_and_history_stays_fresh(
    snapshot_source,
):
    service, store, backend = snapshot_source
    first = {
        "_id": "first",
        "id": "first",
        "status": "confirmed",
        "created_at": "2026-10-27T01:00:00+00:00",
        "confirmed_at": "2026-10-27T01:01:00+00:00",
        "local_path": "/private/first.tgdb",
        "counts": {"nodes": 1},
    }
    backend.db.backups.insert_one(first)
    reopened = await service.restore()
    try:
        assert reopened.revision == store.revision
        observed = service.latest_history()[0]
        assert observed["id"] == "first"
        assert "_id" not in observed and "local_path" not in observed
        assert service._last_success_at == first["confirmed_at"]
        observed["counts"]["nodes"] = 999
        assert service.latest_history()[0]["counts"] == {"nodes": 1}

        second = dict(
            first, _id="second", id="second", created_at="2026-10-27T02:00:00+00:00"
        )
        backend.db.backups.insert_one(second)
        assert service.history(limit=1)[0]["id"] == "second"
        assert service.latest_history()[0]["id"] == "first"
    finally:
        reopened.close()
    reopened = await service.restore()
    try:
        assert service.latest_history()[0]["id"] == "second"
        backend.db.backups.delete_many({})
    finally:
        reopened.close()
    reopened = await service.restore()
    try:
        assert service.latest_history() == []
        assert service._last_success_at is None
    finally:
        reopened.close()


async def test_optional_backup_history_failure_does_not_block_catalog_or_owner_checks(
    snapshot_source, monkeypatch
):
    service, store, backend = snapshot_source
    monkeypatch.setattr(
        service, "history", Mock(side_effect=ConnectionFailure("History unavailable"))
    )
    reopened = await service.restore()
    try:
        assert reopened.revision == store.revision
        assert service.latest_history() == []
        assert service.protection_status(reopened)["state"] == "protected"
        assert service._last_error

        async def stop_wait(awaitable, timeout):
            awaitable.close()
            return True

        monkeypatch.setattr("tgdrive.recovery.mongo_backup.asyncio.wait_for", stop_wait)
        await service._loop(reopened)
        assert service._last_error is None
        service._last_error = "Existing snapshot retry error"
        await service._loop(reopened)
        assert service._last_error == "Existing snapshot retry error"
        monkeypatch.setattr(
            backend, "check_owner", Mock(side_effect=DatabaseUnavailable("Lost owner"))
        )
        assert service.protection_status(reopened)["state"] == "error"
    finally:
        reopened.close()


@pytest.mark.parametrize("confirmation_fails", [False, True])
async def test_backup_observation_confirms_only_after_verified_publication_and_commit(
    snapshot_source, monkeypatch, confirmation_fails
):
    service, store, backend = snapshot_source
    payload = b""

    async def send_document(_channel, path, **_kwargs):
        nonlocal payload
        assert service.latest_history()[0]["status"] == "pending"
        payload = Path(path).read_bytes()
        return SimpleNamespace(id=77)

    async def get_messages(*_args, **_kwargs):
        return SimpleNamespace(document=SimpleNamespace(file_size=len(payload)))

    async def stream_media(*_args, **_kwargs):
        yield payload

    @asynccontextmanager
    async def lease(**_kwargs):
        yield SimpleNamespace(
            name="backup-bot",
            send_document=send_document,
            get_messages=get_messages,
            stream_media=stream_media,
        )

    service.pool.lease = lease
    if confirmation_fails:
        update = backend.db.backups.update_one

        def fail_confirmation(query, change, **kwargs):
            if change.get("$set", {}).get("status") == "confirmed":
                raise ConnectionFailure("Confirmation acknowledgement unavailable")
            return update(query, change, **kwargs)

        monkeypatch.setattr(backend.db.backups, "update_one", fail_confirmation)
        with pytest.raises(ConnectionFailure, match="Confirmation"):
            await service.snapshot_now(store)
    else:
        assert await service.snapshot_now(store)
    observed = service.latest_history()[0]
    record = backend.db.backups.find_one({"id": observed["id"]})
    assert "_id" not in observed and "local_path" not in observed
    if confirmation_fails:
        assert observed["status"] == record["status"] == "pending"
        assert service._last_success_at is None
        assert Path(record["local_path"]).is_file()
    else:
        assert observed["status"] == record["status"] == "confirmed"
        assert observed["message_id"] == 77
        assert service._last_success_at == observed["confirmed_at"]
        assert Path(record["local_path"]).is_file()
        service._observe_backup(
            dict(
                record,
                id="older",
                created_at="2000-01-01T00:00:00+00:00",
                confirmed_at="2000-01-01T00:01:00+00:00",
            )
        )
        assert service.latest_history()[0]["id"] == record["id"]
        assert service._last_success_at == observed["confirmed_at"]


async def test_missing_pending_snapshot_updates_observed_status(snapshot_source):
    service, store, backend = snapshot_source
    with pytest.raises(RuntimeError, match="Publication paused"):
        await service.snapshot_now(store)
    record = backend.db.backups.find_one({"status": "pending"})
    Path(record["local_path"]).unlink()
    with pytest.raises(BackupError, match="previous backup file is unavailable"):
        await service.snapshot_now(store)
    assert service.latest_history()[0]["status"] == "interrupted"
    assert service._last_success_at is None


async def test_snapshot_cooldown_updates_observation_without_confirmation(
    snapshot_source,
):
    service, store, _backend = snapshot_source
    error = RuntimeError("Synthetic provider cooldown")
    error.seconds = 30
    service.pool.lease = Mock(side_effect=error)
    service.pool.defer_channel = AsyncMock()
    with pytest.raises(RuntimeError, match="provider cooldown"):
        await service.snapshot_now(store)
    observed = service.latest_history()[0]
    assert observed["status"] == "pending" and observed["retry_at"] > 0
    assert service._last_success_at is None
    service.pool.defer_channel.assert_awaited_once_with(observed["retry_at"])


@pytest.mark.parametrize(
    "failure,expected,detail",
    [
        ("cursor", ConnectionFailure, "Interrupted snapshot"),
        ("limit", BackupError, "1 GiB expanded limit"),
        ("fsync", OSError, "Snapshot fsync failed"),
    ],
)
def test_failed_snapshot_export_removes_only_its_file_and_allows_retry(
    snapshot_source, tmp_path, monkeypatch, failure, expected, detail
):
    _, store, backend = snapshot_source
    path = tmp_path / "snapshot.tgdb"
    preserved = tmp_path / "existing.tgdb"
    preserved.write_bytes(b"Preserve another pending archive")
    key = bytes.fromhex("a1" * 32)
    with monkeypatch.context() as patch:
        if failure == "cursor":
            find = backend.db.nodes.find

            def interrupted(*args, **kwargs):
                yield from find(*args, **kwargs)
                raise ConnectionFailure("Interrupted snapshot")

            patch.setattr(backend.db.nodes, "find", interrupted)
        elif failure == "limit":
            patch.setattr("tgdrive.recovery.mongo_backup.MAX_EXPANDED", 1)
        else:

            def failed_fsync(_descriptor):
                raise OSError("Snapshot fsync failed")

            patch.setattr("tgdrive.recovery.mongo_backup.os.fsync", failed_fsync)
        with pytest.raises(expected, match=detail):
            write_snapshot(backend, path, key)
    assert not path.exists()
    assert preserved.read_bytes() == b"Preserve another pending archive"
    revision, counts, digest = write_snapshot(backend, path, key)
    assert revision == store.revision
    assert counts["catalog"] == 1
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert decode_snapshot(path, key)["catalog"][0]["revision"] == revision


def test_snapshot_export_never_removes_an_existing_destination(
    snapshot_source, tmp_path
):
    _, _, backend = snapshot_source
    path = tmp_path / "existing.tgdb"
    path.write_bytes(b"Preserve this archive")
    with pytest.raises(FileExistsError):
        write_snapshot(backend, path, bytes.fromhex("a1" * 32))
    assert path.read_bytes() == b"Preserve this archive"


@pytest.mark.parametrize("revision", [True, 0])
def test_snapshot_export_rejects_invalid_catalog_revision(
    snapshot_source, tmp_path, revision
):
    _, _, backend = snapshot_source
    backend.db.catalog.update_one({"_id": "catalog"}, {"$set": {"revision": revision}})
    path = tmp_path / "invalid.tgdb"
    with pytest.raises(BackupError, match="catalog revision is invalid"):
        write_snapshot(backend, path, bytes.fromhex("a1" * 32))
    assert not path.exists()


async def test_cancelled_snapshot_drains_export_before_removing_unadmitted_file(
    snapshot_source, monkeypatch
):
    service, store, backend = snapshot_source
    entered, release = threading.Event(), threading.Event()

    def held_export(*args):
        result = write_recovery_snapshot(*args)
        entered.set()
        assert release.wait(3), "The event loop could not release the export"
        return result

    monkeypatch.setattr(
        "tgdrive.recovery.mongo_backup.write_recovery_snapshot", held_export
    )
    task = asyncio.create_task(service.snapshot_now(store))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        path = next((service.settings.resolved_cache_dir / "mongo-backups").iterdir())
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert service._lock.locked()
        assert path.is_file()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not path.exists()
        assert backend.db.backups.find({}) == []
        service.pool.lease.assert_not_called()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unknown_pending_snapshot_commit_preserves_ciphertext_for_retry(
    snapshot_source, monkeypatch
):
    service, store, backend = snapshot_source
    insert = backend.db.backups.insert_one

    def committed_without_acknowledgement(*args, **kwargs):
        insert(*args, **kwargs)
        raise ConnectionFailure("Pending snapshot acknowledgement lost")

    with monkeypatch.context() as patch:
        patch.setattr(
            backend.db.backups, "insert_one", committed_without_acknowledgement
        )
        with pytest.raises(ConnectionFailure, match="acknowledgement lost"):
            await service.snapshot_now(store)
    record = backend.db.backups.find_one({"status": "pending"})
    path = (
        service.settings.resolved_cache_dir / "mongo-backups" / (record["id"] + ".tgdb")
    )
    ciphertext = path.read_bytes()
    assert hashlib.sha256(ciphertext).hexdigest() == record["sha256"]
    export = Mock(side_effect=AssertionError("A pending snapshot must be reused"))
    monkeypatch.setattr("tgdrive.recovery.mongo_backup.write_recovery_snapshot", export)
    with pytest.raises(RuntimeError, match="Publication paused"):
        await service.snapshot_now(store)
    export.assert_not_called()
    assert path.read_bytes() == ciphertext
    assert backend.db.backups.find_one({"status": "pending"}) == record


@pytest.mark.parametrize("failure", [None, "send", "confirmation", "cleanup"])
async def test_local_snapshot_retention_waits_for_durable_confirmation(
    snapshot_source, tmp_path, monkeypatch, caplog, failure
):
    import os

    service, store, backend = snapshot_source
    directory = service.settings.resolved_cache_dir / "mongo-backups"
    directory.mkdir()
    paths = {}
    for identity, status, format_name in (
        ("previous-one", "confirmed", "tgdrive-backup-v2"),
        ("previous-two", "confirmed", "tgdrive-backup-v2"),
        ("retry-ciphertext", "interrupted", "tgdrive-backup-v2"),
        ("legacy-copy", "confirmed", "mongodb-encrypted-v1"),
    ):
        path = directory / (identity + ".tgdb")
        path.write_bytes(identity.encode())
        paths[identity] = path
        backend.db.backups.insert_one(
            {
                "_id": identity,
                "id": identity,
                "status": status,
                "format": format_name,
                "local_path": str(path),
            }
        )
    outside = tmp_path / "keep-outside.tgdb"
    outside.write_bytes(b"outside private copy")
    linked = directory / "linked.tgdb"
    linked.symlink_to(outside)
    backend.db.backups.insert_one(
        {
            "_id": "linked",
            "id": "linked",
            "status": "confirmed",
            "format": "tgdrive-backup-v2",
            "local_path": str(linked),
        }
    )
    payload = b""

    async def send_document(_channel, path, **_kwargs):
        nonlocal payload
        assert all(paths[name].exists() for name in paths)
        assert service.latest_history()[0]["status"] == "pending"
        if failure == "send":
            raise OSError("Synthetic publication failure")
        payload = Path(path).read_bytes()
        return SimpleNamespace(id=77)

    async def get_messages(*_args, **_kwargs):
        return SimpleNamespace(document=SimpleNamespace(file_size=len(payload)))

    async def stream_media(*_args, **_kwargs):
        yield payload

    @asynccontextmanager
    async def lease(**_kwargs):
        yield SimpleNamespace(
            name="backup-bot",
            send_document=send_document,
            get_messages=get_messages,
            stream_media=stream_media,
        )

    service.pool.lease = lease
    if failure == "confirmation":
        update = backend.db.backups.update_one

        def reject_confirmation(query, change, **kwargs):
            if change.get("$set", {}).get("status") == "confirmed":
                raise ConnectionFailure("Synthetic confirmation failure")
            return update(query, change, **kwargs)

        monkeypatch.setattr(backend.db.backups, "update_one", reject_confirmation)
    elif failure == "cleanup":
        unlink = os.unlink

        def reject_cleanup(path, **kwargs):
            if str(path) == "previous-one.tgdb":
                raise PermissionError("Synthetic cleanup failure")
            return unlink(path, **kwargs)

        monkeypatch.setattr(os, "unlink", reject_cleanup)
    if failure in {"send", "confirmation"}:
        with pytest.raises((OSError, ConnectionFailure), match="Synthetic"):
            await service.snapshot_now(store)
    else:
        assert await service.snapshot_now(store)
    latest = service.latest_history()[0]
    current = backend.db.backups.find_one({"id": latest["id"]})
    assert Path(current["local_path"]).is_file()
    assert paths["retry-ciphertext"].read_bytes() == b"retry-ciphertext"
    assert paths["legacy-copy"].read_bytes() == b"legacy-copy"
    assert outside.read_bytes() == b"outside private copy" and linked.is_symlink()
    if failure is None:
        assert not paths["previous-one"].exists() and not paths["previous-two"].exists()
    else:
        assert paths["previous-one"].is_file()
    assert current["status"] == (
        "pending" if failure in {"send", "confirmation"} else "confirmed"
    )
    assert len(backend.db.backups.find({})) == 6
    if failure == "cleanup":
        assert service._last_error is None
        assert "new backup remains confirmed" in caplog.text


@pytest.mark.parametrize("with_catalog", [False, True])
@pytest.mark.parametrize("binding", [None, "current", "different"])
def test_pristine_restore_accepts_only_its_unmodified_recovery_key(
    with_catalog, binding
):
    backend = FakeMongoBackend()
    store = (
        MongoStore.create(backend, storage_channel=-1001234567890)
        if with_catalog
        else None
    )
    drive_id = store.drive_id if store else None
    key_drive = drive_id if binding == "current" else binding
    backend.db.settings.insert_one(
        {
            "_id": "recovery-key",
            "document": {"key": "a1" * 32, "drive_id": key_drive},
        }
    )
    try:
        if binding == "different":
            with pytest.raises(FileExistsError, match="settings changes"):
                check_restore_destination(backend, allow_pristine=True)
        else:
            check_restore_destination(backend, allow_pristine=True)
    finally:
        if store:
            store.close()


@pytest.mark.parametrize(
    "damage",
    ["key", "key-type", "missing-binding", "extra-field", "extra-document-field"],
)
def test_pristine_restore_rejects_malformed_recovery_key_records(damage):
    backend = FakeMongoBackend()
    row = {"_id": "recovery-key", "document": {"key": "a1" * 32, "drive_id": None}}
    if damage == "key":
        row["document"]["key"] = "invalid"
    elif damage == "key-type":
        row["document"]["key"] = 42
    elif damage == "missing-binding":
        row["document"].pop("drive_id")
    elif damage == "extra-field":
        row["changed"] = True
    else:
        row["document"]["changed"] = True
    backend.db.settings.insert_one(row)
    with pytest.raises(FileExistsError, match="settings changes"):
        check_restore_destination(backend, allow_pristine=True)


@pytest.mark.parametrize("enabled", [False, True])
def test_database_status_reports_active_independent_checkpoint_without_recursion(
    snapshot_source, enabled
):
    service, store, _backend = snapshot_source
    standalone = "2026-10-28T10:00:00+00:00"
    independent = "2026-10-28T11:00:00+00:00"
    service._last_success_at = standalone
    service._last_error = "An older standalone snapshot failed"
    saved = {
        "config": {"enabled": enabled},
        "status": {
            "last_success_at": independent,
            "protected_revision": store.revision - 1,
            "error": {"detail": "The independent copy is waiting for Telegram"},
        },
        "key": "a1" * 32,
    }
    service.replica = SimpleNamespace(
        journal=SimpleNamespace(
            get=lambda name, default=None: saved.get(name, default)
        ),
        status=Mock(
            side_effect=AssertionError(
                "Status must not recursively load backup history"
            )
        ),
    )
    result = service.protection_status(store)
    assert result["last_success_at"] == (independent if enabled else standalone)
    assert result["backup_error"] == (
        "The independent copy is waiting for Telegram"
        if enabled
        else service._last_error
    )
    assert result["state"] == "protected"
    assert result["metadata_revision"] == result["protected_revision"] == store.revision
    service.replica.status.assert_not_called()
