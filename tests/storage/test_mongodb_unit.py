"""Small MongoDB failure/scheduling checks without network access."""

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast
from unittest.mock import AsyncMock, Mock

import pytest
from pymongo.errors import ConnectionFailure
from pymongo.results import UpdateResult

from tests.support.mongo_fixtures import FakeMongoBackend
from tests.support.recovery_fixtures import settings as recovery_settings
from tgdrive.core.logging import _redact
from tgdrive.recovery.backup import BackupError
from tgdrive.recovery.mongo_backup import (
    MongoProtection,
    check_restore_destination,
)
from tgdrive.recovery.recovery_file import write_recovery_snapshot
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.mongo import DatabaseUnavailable, MongoBackend
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.transfers.tasks import TransferRegistry


class ProviderCooldown(RuntimeError):
    seconds = 30


class OwnedLock(Protocol):
    def _is_owned(self) -> bool: ...


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


def decode_file(path):
    from tgdrive.core import encryption
    from tgdrive.recovery.recovery_file import decode_recovery_file

    return decode_recovery_file(path.read_bytes(), encryption.backup_key())["database"]


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
    store = MongoStore.create(
        cast(MongoBackend, backend), storage_channel=-1001234567890
    )
    settings = recovery_settings(tmp_path).model_copy(
        update={"cache_dir": tmp_path},
    )
    pool = SimpleNamespace(lease=Mock(side_effect=RuntimeError("Publication paused")))
    yield MongoProtection(settings, pool, backend), store, backend
    store.close()


async def test_snapshot_record_uses_exported_revision_when_catalog_advances(
    snapshot_source, monkeypatch
):
    service, store, backend = snapshot_source
    exported_revision = store.revision

    def export_then_advance(*args, **kwargs):
        result = write_recovery_snapshot(*args, **kwargs)
        store.create_folder("/", "Arrived after the snapshot")
        return result

    monkeypatch.setattr(
        "tgdrive.recovery.mongo_backup.write_recovery_snapshot", export_then_advance
    )
    with pytest.raises(RuntimeError, match="Publication paused"):
        await service.snapshot_now(store)
    record = backend.db.backups.find_one({"status": "pending"})
    decoded = decode_file(
        service.settings.resolved_cache_dir / "mongo-backups" / (record["id"] + ".tgdb")
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


async def test_backup_file_uses_the_key_derived_from_encryption_key(
    snapshot_source,
):
    service, store, backend = snapshot_source
    with pytest.raises(RuntimeError, match="Publication paused"):
        await service.snapshot_now(store)
    row = backend.db.backups.find_one({"status": "pending"})
    assert row["format"] == "tgdrive-backup-v2"
    assert decode_file(Path(row["local_path"]))["catalog"]
    # A pending file encrypted under another key is never resent.
    backend.db.backups.update_one(
        {"_id": row["_id"]}, {"$set": {"key_sha256": "0" * 64}}
    )
    with pytest.raises(RuntimeError, match="Publication paused"):
        await service.snapshot_now(store)
    assert backend.db.backups.find_one({"_id": row["_id"]})["status"] == "superseded"


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
            pin_message=AsyncMock(),
            get_pinned_message_id=AsyncMock(return_value=77),
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
    error = ProviderCooldown("Synthetic provider cooldown")
    service.pool.lease = Mock(side_effect=error)
    service.pool.defer_channel = AsyncMock()
    with pytest.raises(RuntimeError, match="provider cooldown"):
        await service.snapshot_now(store)
    observed = service.latest_history()[0]
    assert observed["status"] == "pending" and observed["retry_at"] > 0
    assert service._last_success_at is None
    service.pool.defer_channel.assert_awaited_once_with(observed["retry_at"])


async def test_cancelled_snapshot_drains_export_before_removing_unadmitted_file(
    snapshot_source, monkeypatch
):
    service, store, backend = snapshot_source
    entered, release = threading.Event(), threading.Event()

    def held_export(*args, **kwargs):
        result = write_recovery_snapshot(*args, **kwargs)
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
        assert list(backend.db.backups.find({})) == []
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
            pin_message=AsyncMock(),
            get_pinned_message_id=AsyncMock(return_value=77),
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
        MongoStore.create(cast(MongoBackend, backend), storage_channel=-1001234567890)
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
            get_many=lambda defaults: {
                name: saved.get(name, default) for name, default in defaults.items()
            }
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


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("phase", ["protected", "syncing", "paused", "error"])
def test_database_status_distinguishes_independent_backup_evidence(
    snapshot_source, enabled, phase
):
    service, store, _backend = snapshot_source
    service._last_success_at = "2026-11-03T06:17:59+00:00"
    saved_status = {
        "phase": phase,
        "protected_revision": max(0, store.revision - 1),
        "last_success_at": None,
        "error": {"code": "PIN_WAIT", "detail": "Waiting for Telegram"},
    }
    journal = SimpleNamespace(
        get_many=Mock(
            return_value={"config": {"enabled": enabled}, "status": saved_status}
        )
    )
    service.replica = SimpleNamespace(journal=journal)

    result = service.protection_status(store)

    assert result["state"] == "protected"
    assert result["independent_backup"] == {
        "enabled": enabled,
        "phase": phase if enabled else None,
        "protected_revision": saved_status["protected_revision"] if enabled else None,
        "last_success_at": None,
        "error": saved_status["error"] if enabled else None,
    }
    journal.get_many.assert_called_once_with({"config": {}, "status": {}})


def test_database_status_retains_independent_confirmation_without_history_lookup(
    snapshot_source,
):
    service, store, _backend = snapshot_source
    confirmed_at = "2026-11-11T15:02:47+00:00"
    service._last_success_at = "2026-11-03T06:17:59+00:00"
    service.replica = SimpleNamespace(
        journal=SimpleNamespace(
            get_many=lambda _defaults: {
                "config": {"enabled": True},
                "status": {
                    "phase": "protected",
                    "protected_revision": store.revision,
                    "last_success_at": confirmed_at,
                    "error": None,
                },
            }
        )
    )

    result = service.protection_status(store)

    assert result["independent_backup"] == {
        "enabled": True,
        "phase": "protected",
        "protected_revision": store.revision,
        "last_success_at": confirmed_at,
        "error": None,
    }
    assert result["last_success_at"] == confirmed_at


def test_owner_renewal_waits_for_open_catalog_transactions():
    from tgdrive.storage.mongo import MongoBackend

    backend = MongoBackend("mongodb://synthetic.invalid", "tgdrive")
    backend._stop = Mock(wait=Mock(side_effect=[False, True]))
    held = []

    def renew():
        # A renewal inside a transaction would WriteConflict that transaction.
        held.append(cast(OwnedLock, backend._write_lock)._is_owned())
        return UpdateResult({"n": 1, "nModified": 0, "ok": 1.0}, True)

    backend._renew = renew
    backend._renew_loop()
    assert held == [True] and not backend._lost
    backend.client.close()


async def test_scheduled_backup_does_not_skip_in_place_job_changes(
    snapshot_source, monkeypatch
):
    service, store, backend = snapshot_source
    backend.db.backups.insert_one(
        {
            "_id": "b1",
            "status": "confirmed",
            "created_at": "2026-11-01T00:00:00+00:00",
        }
    )
    backend.db.jobs.insert_one({"_id": "job"})
    backend.db.jobs.update_one(
        {"_id": "job"}, {"$set": {"data": {"phase": "finished"}}}
    )

    async def interval_elapsed(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    async def snapshot(_store):
        assert backend.db.jobs.find_one({})["data"]["phase"] == "finished"
        service._stop.set()

    service.snapshot_now = AsyncMock(side_effect=snapshot)
    monkeypatch.setattr(
        "tgdrive.recovery.mongo_backup.asyncio.wait_for", interval_elapsed
    )
    await service._loop(store)
    service.snapshot_now.assert_awaited_once_with(store)


async def test_verified_backup_retries_pin_after_restart_without_local_file(
    snapshot_source,
):
    service, store, backend = snapshot_source
    with pytest.raises(RuntimeError, match="Publication paused"):
        await service.snapshot_now(store)
    row = backend.db.backups.find_one({"status": "pending"})
    # This is the durable boundary after successful ciphertext verification.
    backend.db.backups.update_one({"_id": row["id"]}, {"$set": {"message_id": 77}})
    Path(row["local_path"]).unlink()
    restarted = MongoProtection(service.settings, service.pool, backend)
    restarted._pin = AsyncMock(
        side_effect=[PermissionError("Missing Edit Messages"), None]
    )
    with pytest.raises(PermissionError):
        await restarted.snapshot_now(store)
    assert backend.db.backups.find_one({"_id": row["id"]})["status"] == "pending"
    assert restarted._last_success_at is None
    assert await restarted.snapshot_now(store)
    assert backend.db.backups.find_one({"_id": row["id"]})["status"] == "confirmed"
    assert restarted._last_success_at
    assert (
        restarted._pin.await_args_list[0].args[1]
        == restarted._pin.await_args_list[1].args[1]
    )


@pytest.mark.parametrize("cooldown", [False, True])
async def test_actual_verified_send_and_pin_retry_share_durable_failure_handling(
    snapshot_source, monkeypatch, cooldown
):
    service, store, backend = snapshot_source
    clock = [1000]
    monkeypatch.setattr(
        "tgdrive.recovery.mongo_backup.time", SimpleNamespace(time=lambda: clock[0])
    )
    directory = service.settings.resolved_cache_dir / "mongo-backups"
    directory.mkdir(parents=True)
    previous = directory / "last-good.tgdb"
    previous.write_bytes(b"last confirmed recovery file")
    good = {
        "_id": "last-good",
        "id": "last-good",
        "status": "confirmed",
        "pin_verified_at": "2000-01-01T00:00:01+00:00",
        "created_at": "2000-01-01T00:00:00+00:00",
        "confirmed_at": "2000-01-01T00:00:01+00:00",
        "format": "tgdrive-backup-v2",
        "local_path": str(previous),
    }
    backend.db.backups.insert_one(good)
    service._observe_backup(good)
    payload = b""
    failures = [
        PermissionError("Missing Edit Messages"),
        BackupError("Wait", retry_after=60),
    ]
    if cooldown:
        failures[0] = ProviderCooldown("Telegram FloodWait")

    async def send(_channel, path, **_kwargs):
        nonlocal payload
        payload = Path(path).read_bytes()
        return SimpleNamespace(id=77)

    async def stream(*_args, **_kwargs):
        yield payload

    async def pin(_channel, message):
        pending = backend.db.backups.find_one({"status": "pending"})
        # The receipt must already be durable, even for the first failed pin.
        assert pending["message_id"] == message == 77
        assert previous.is_file()
        if failures:
            raise failures.pop(0)

    client = SimpleNamespace(
        name="backup-bot",
        send_document=AsyncMock(side_effect=send),
        get_messages=AsyncMock(
            side_effect=lambda *_args, **_kwargs: SimpleNamespace(
                document=SimpleNamespace(file_size=len(payload))
            )
        ),
        stream_media=stream,
        pin_message=AsyncMock(side_effect=pin),
        get_pinned_message_id=AsyncMock(return_value=77),
    )
    leased = []

    @asynccontextmanager
    async def lease(**kwargs):
        leased.append(kwargs)
        yield client

    service.pool.lease = lease
    service.pool.defer_channel = AsyncMock()
    with pytest.raises(BackupError, match="awaiting its recovery pin"):
        await service.snapshot_now(store)
    row = backend.db.backups.find_one({"status": "pending"})
    assert row["message_id"] == 77
    assert service._last_success_at == good["confirmed_at"]
    assert previous.is_file()
    Path(row["local_path"]).unlink()
    restarted = MongoProtection(service.settings, service.pool, backend)
    if cooldown:
        assert row["retry_at"] == 1030.25
        leases_before_wait = len(leased)
        with pytest.raises(BackupError, match="waiting before retry"):
            await restarted.snapshot_now(store)
        client.pin_message.assert_awaited_once()
        client.send_document.assert_awaited_once()
        assert len(leased) == leases_before_wait
    clock[0] = 1040
    with pytest.raises(BackupError, match="awaiting its recovery pin"):
        await restarted.snapshot_now(store)
    pending = backend.db.backups.find_one({"_id": row["_id"]})
    assert pending["retry_at"] == 1100.25
    assert restarted.latest_history()[0]["retry_at"] == pending["retry_at"]
    assert previous.is_file()
    clock[0] = 1110
    assert await restarted.snapshot_now(store)
    confirmed = backend.db.backups.find_one({"_id": row["_id"]})
    assert confirmed["status"] == "confirmed"
    assert confirmed["pin_verified_at"]
    assert confirmed["random_id"] == row["random_id"]
    assert confirmed["sender"] == "backup-bot" and confirmed["message_id"] == 77
    assert client.send_document.await_count == 1
    assert client.pin_message.await_count == 3
    assert leased[0] == {"client_name": None}
    assert all(lease == {"client_name": "backup-bot"} for lease in leased[1:])
    assert not previous.exists()
    assert backend.db.backups.find_one({"_id": "last-good"}) == good
    assert service.pool.defer_channel.await_count == (2 if cooldown else 1)
    service.pool.defer_channel.assert_awaited_with(1100.25)


@pytest.fixture
def verified_publication(snapshot_source):
    from tgdrive.core.encryption import backup_key

    service, store, backend = snapshot_source
    directory = service.settings.resolved_cache_dir / "mongo-backups"
    directory.mkdir(parents=True)
    previous = directory / "last-good.tgdb"
    previous.write_bytes(b"last confirmed ciphertext")
    good = {
        "_id": "last-good",
        "id": "last-good",
        "status": "confirmed",
        "format": "tgdrive-backup-v2",
        "local_path": str(previous),
        "created_at": "2000-01-01T00:00:00+00:00",
        "confirmed_at": "2000-01-01T00:00:01+00:00",
        "pin_verified_at": "2000-01-01T00:00:01+00:00",
    }
    current = directory / "pending.tgdb"
    current.write_bytes(b"verified pending ciphertext")
    pending = {
        "_id": "pending",
        "id": "pending",
        "status": "pending",
        "created_at": "2000-01-02T00:00:00+00:00",
        "revision": store.revision,
        "local_path": str(current),
        "sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
        "random_id": 12345,
        "sender": "backup-bot",
        "message_id": 77,
        "channel_id": service.settings.storage_channel,
        "retry_at": 0,
        "counts": {},
        "format": "tgdrive-backup-v2",
        "key_sha256": hashlib.sha256(bytes.fromhex(backup_key())).hexdigest(),
    }
    backend.db.backups.insert_one(good)
    backend.db.backups.insert_one(pending)
    service._observe_backup(good)
    return service, store, backend, good, pending, previous


@pytest.mark.parametrize("boundary", ["before", "lease", "after"])
async def test_publication_preserves_last_good_backup_after_ownership_loss(
    verified_publication, monkeypatch, boundary
):
    service, store, backend, good, pending, previous = verified_publication
    lost = False
    fences, leased = [], []

    def check_owner():
        if lost:
            raise DatabaseUnavailable("Synthetic ownership lost")

    def fence(_session):
        check_owner()
        fences.append(True)

    def pin(_channel, _message):
        nonlocal lost
        if boundary == "after":
            lost = True

    client = SimpleNamespace(
        pin_message=AsyncMock(side_effect=pin),
        get_pinned_message_id=AsyncMock(return_value=77),
    )

    @asynccontextmanager
    async def lease(**kwargs):
        nonlocal lost
        leased.append(kwargs)
        if boundary == "lease":
            lost = True
        yield client

    observe = service._observe_backup

    def observed(row):
        nonlocal lost
        observe(row)
        if boundary == "before" and row["id"] == pending["id"]:
            lost = True

    # Exercise production's fenced writer rather than the fixture's direct one.
    backend._write_lock = threading.RLock()
    monkeypatch.setattr(backend, "write", MongoBackend.write.__get__(backend))
    monkeypatch.setattr(backend, "check_owner", check_owner)
    monkeypatch.setattr(backend, "fence", fence)
    monkeypatch.setattr(service, "_observe_backup", observed)
    monkeypatch.setattr(service, "_prune_confirmed_files", Mock())
    service.pool.lease = lease
    with pytest.raises(DatabaseUnavailable, match="ownership lost"):
        await service.snapshot_now(store)
    assert client.pin_message.await_count == (1 if boundary == "after" else 0)
    assert len(leased) == (0 if boundary == "before" else 1)
    assert fences == []  # The failed owner check ran before any durable mutation.
    assert backend.db.backups.find_one({"_id": pending["id"]}) == pending
    assert service.latest_history()[0]["status"] == "pending"
    assert service._last_success_at == good["confirmed_at"]
    assert previous.read_bytes() == b"last confirmed ciphertext"
    assert Path(pending["local_path"]).is_file()
    service._prune_confirmed_files.assert_not_called()


async def test_cancelled_backup_confirmation_drains_its_fenced_write(
    verified_publication, monkeypatch
):
    service, store, backend, good, pending, previous = verified_publication
    entered, release = threading.Event(), threading.Event()
    update = backend.db.backups.update_one

    def held_confirmation(query, change, **kwargs):
        if change.get("$set", {}).get("status") == "confirmed":
            entered.set()
            assert release.wait(3), "The test did not release backup confirmation"
        return update(query, change, **kwargs)

    @asynccontextmanager
    async def lease(**_kwargs):
        yield SimpleNamespace(
            pin_message=AsyncMock(), get_pinned_message_id=AsyncMock(return_value=77)
        )

    backend._write_lock = threading.RLock()
    monkeypatch.setattr(backend, "write", MongoBackend.write.__get__(backend))
    monkeypatch.setattr(backend.db.backups, "update_one", held_confirmation)
    monkeypatch.setattr(service, "_prune_confirmed_files", Mock())
    service.pool.lease = lease
    task = asyncio.create_task(service.snapshot_now(store))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and service._lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            backend.db.backups.find_one({"_id": pending["id"]})["status"] == "confirmed"
        )
        assert service._last_success_at == good["confirmed_at"]
        assert previous.is_file()
        service._prune_confirmed_files.assert_not_called()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("pin", [77, 88, ConnectionError("Telegram unavailable")])
async def test_restore_rechecks_historic_pin_without_repinning_or_losing_evidence(
    snapshot_source, pin
):
    service, store, backend = snapshot_source
    path = service.settings.resolved_cache_dir / "historic.tgdb"
    path.write_bytes(b"historical ciphertext")
    row = {
        "_id": "historic",
        "id": "historic",
        "format": "tgdrive-backup-v2",
        "status": "confirmed",
        "created_at": "2000-01-01T00:00:00+00:00",
        "confirmed_at": "2000-01-01T00:00:01+00:00",
        "local_path": str(path),
        "message_id": 77,
        "sender": "backup-bot",
        "channel_id": service.settings.storage_channel,
    }
    backend.db.backups.insert_one(row)
    client = SimpleNamespace(
        get_pinned_message_id=AsyncMock(
            side_effect=pin if isinstance(pin, Exception) else None,
            return_value=pin,
        ),
        pin_message=AsyncMock(),
        send_document=AsyncMock(),
    )

    @asynccontextmanager
    async def lease(**kwargs):
        assert kwargs == {"client_name": "backup-bot"}
        yield client

    service.pool.lease = lease
    reopened = await service.restore()
    try:
        observed = service.latest_history()[0]
        saved = backend.db.backups.find_one({"_id": row["_id"]})
        assert saved["confirmed_at"] == row["confirmed_at"]
        assert path.read_bytes() == b"historical ciphertext"
        assert saved["message_id"] == 77
        assert reopened.revision == store.revision
        if pin == 77:
            assert saved["status"] == observed["status"] == "confirmed"
            assert saved["pin_verified_at"] == observed["pin_verified_at"]
            assert service._last_success_at == row["confirmed_at"]
            assert service._last_error is None
        else:
            assert saved["status"] == observed["status"] == "unverified"
            assert "pin_verified_at" not in saved
            assert service._last_success_at is None
            assert "new backup will be scheduled" in service._last_error
        client.get_pinned_message_id.assert_awaited_once_with(row["channel_id"])
        client.pin_message.assert_not_awaited()
        client.send_document.assert_not_awaited()
    finally:
        reopened.close()
    # A verified marker needs no extra provider read; an unverified row stays
    # visibly unverified across another restart until a fresh backup succeeds.
    reopened = await service.restore()
    try:
        client.get_pinned_message_id.assert_awaited_once()
        if pin != 77:
            assert service.latest_history()[0]["status"] == "unverified"
            assert service._last_success_at is None
            assert service._last_error
    finally:
        reopened.close()


async def test_historic_pin_startup_check_is_bounded_and_does_not_block_drive_access(
    snapshot_source, monkeypatch
):
    service, store, backend = snapshot_source
    row = {
        "_id": "historic",
        "id": "historic",
        "format": "tgdrive-backup-v2",
        "status": "confirmed",
        "created_at": "2000-01-01T00:00:00+00:00",
        "confirmed_at": "2000-01-01T00:00:01+00:00",
        "message_id": 77,
        "sender": "backup-bot",
        "channel_id": service.settings.storage_channel,
    }
    backend.db.backups.insert_one(row)
    started = asyncio.Event()

    async def blocked_pin(_channel):
        started.set()
        await asyncio.Event().wait()

    client = SimpleNamespace(get_pinned_message_id=blocked_pin, pin_message=AsyncMock())

    @asynccontextmanager
    async def lease(**_kwargs):
        yield client

    service.pool.lease = lease
    real_timeout = asyncio.timeout

    def short_timeout(seconds):
        assert seconds == 10
        return real_timeout(0.01)

    monkeypatch.setattr("tgdrive.recovery.mongo_backup.asyncio.timeout", short_timeout)
    reopened = await asyncio.wait_for(service.restore(), 1)
    try:
        assert started.is_set()
        assert reopened.revision == store.revision
        assert service.latest_history()[0]["status"] == "unverified"
        assert service.protection_status(reopened)["state"] == "protected"
        assert service._last_success_at is None
        assert service._last_error
        client.pin_message.assert_not_awaited()
    finally:
        reopened.close()


async def test_newer_verified_backup_leaves_older_historic_archives_unchanged(
    snapshot_source,
):
    service, _store, backend = snapshot_source
    old = {
        "_id": "old",
        "id": "old",
        "format": "tgdrive-backup-v2",
        "status": "confirmed",
        "created_at": "2000-01-01T00:00:00+00:00",
        "confirmed_at": "2000-01-01T00:00:01+00:00",
        "message_id": 77,
    }
    current = dict(
        old,
        _id="current",
        id="current",
        created_at="2000-01-02T00:00:00+00:00",
        confirmed_at="2000-01-02T00:00:01+00:00",
        message_id=88,
        pin_verified_at="2000-01-02T00:00:01+00:00",
    )
    backend.db.backups.insert_many([old, current])
    reopened = await service.restore()
    try:
        assert backend.db.backups.find_one({"_id": old["id"]}) == old
        assert service.latest_history()[0]["id"] == "current"
        assert service._last_success_at == current["confirmed_at"]
        assert service._last_error is None
        service.pool.lease.assert_not_called()
    finally:
        reopened.close()


def test_mutations_read_current_pending_deletions():
    from tests.support.mongo_fixtures import new_store

    store = new_store()
    reads = []
    original = store.db.deletions.find

    def find(*args, **kwargs):
        if args[1:] == ({"root_id": 1},):  # The pending-roots list query.
            reads.append(args)
        return original(*args, **kwargs)

    store.db.deletions.find = find
    folder = store.create_folder("/", "A")
    store.create_folder("/", "B")
    store.create_folder("/", "C")
    assert len(reads) >= 3
    store.set_trashed("/" + folder["id"], True)
    store.begin_deletion("/" + folder["id"])
    # An unrelated read must not seed shared mutation state.
    assert store._active_deletion_roots() == [folder["id"]]
    from tgdrive.core.errors import ValidationError

    with pytest.raises(ValidationError, match="pending permanent deletion"):
        store.set_trashed("/" + folder["id"], False)
    assert store._active_deletion_roots() == [folder["id"]]
    store.close()


def test_deletion_finalization_refuses_a_restored_target():
    from tests.support.mongo_fixtures import new_store
    from tgdrive.core.errors import TrashedItemError

    store = new_store()
    folder = store.create_folder("/", "A")
    path = "/" + folder["id"]
    store.set_trashed(path, True)
    intent = store.begin_deletion(path)
    store.db.nodes.update_one({"_id": folder["id"]}, {"$set": {"trashed": False}})
    with pytest.raises(TrashedItemError):
        store.finish_deletion(intent["id"])
    assert store.db.nodes.find_one({"_id": folder["id"]}) is not None
    assert store.pending_deletions()[0]["state"] == "pending"
    store.close()


@pytest.mark.parametrize("name", ["漢" * 255, "ΐ" * 255, "😀" * 255])
def test_long_unicode_cursor_round_trips(name):
    from tests.support.mongo_fixtures import new_store

    store = new_store()
    store.create_folder("/", name)
    store.create_folder("/", name[:-1] + "z")
    page = store.list_page("/", limit=1)
    after = store.list_page("/", limit=1, after=page["next_cursor"])
    assert len(after["items"]) == 1
    store.close()


@pytest.mark.parametrize(
    "kind,key,identity",
    [
        ("file", 2**63, "valid"),
        ("file", -1, "valid"),
        ([], 1, "valid"),
        ("file", 1, "bad/id"),
    ],
)
def test_cursor_rejects_invalid_query_keys(kind, key, identity):
    from tgdrive.core.errors import ValidationError
    from tgdrive.storage.mongo_store import _decode_cursor, _encode_cursor

    with pytest.raises(ValidationError):
        _decode_cursor(_encode_cursor("size", (kind, key, identity)), "size")


@pytest.mark.parametrize("operation", ["verify_lineage", "ensure_revision"])
async def test_backup_lineage_checks_leave_the_event_loop_responsive(
    snapshot_source, monkeypatch, operation
):
    service, store, _backend = snapshot_source
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()

    def held_check(_store):
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    monkeypatch.setattr(service, "check_upload_allowed", held_check)
    arguments = (store, store.revision) if operation == "ensure_revision" else (store,)
    task = asyncio.create_task(getattr(service, operation)(*arguments))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    await task
