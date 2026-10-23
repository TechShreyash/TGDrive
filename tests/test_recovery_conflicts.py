"""Recovery fences preserve newer catalogs and channel-scoped message IDs."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_backup import service_for, settings
from test_transfers import FakeTelegramClient

import tgdrive.runtime as runtime_module
from tgdrive.backup import BackupError, RecoveryConflict, TelegramBackupService
from tgdrive.client_pool import ClientPool
from tgdrive.errors import CorruptStoreError, ValidationError
from tgdrive.runtime import Runtime
from tgdrive.store import DriveStore
from tgdrive.tasks import TransferRegistry, TransferStatus
from tgdrive.transfers import TransferManager


def manager_for(tmp_path, pool, store, guard):
    return TransferManager(
        settings=SimpleNamespace(
            resolved_cache_dir=tmp_path / "cache",
            max_concurrent_transfers=1,
            storage_channel=-1001234567890,
            bot_upload_limit=2_000_000_000,
        ),
        pool=pool,
        store=store,
        remote=SimpleNamespace(close=AsyncMock()),
        registry=TransferRegistry(),
        ensure_lineage=guard,
    )


def test_channel_binding_is_durable_idempotent_and_never_rebound(tmp_path):
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    before = store.revision
    assert store.storage_channel_id is None
    store.bind_storage_channel(-100123)
    assert store.revision == before + 1
    store.bind_storage_channel(-100123)
    assert store.revision == before + 1
    with pytest.raises(ValidationError, match="another storage channel"):
        store.bind_storage_channel(-100999)
    payload = store.export()
    assert payload["storage_channel_id"] == -100123
    restored = DriveStore._from_payload(payload)
    assert restored.storage_channel_id == -100123
    restored.close()
    store.close()
    reopened = DriveStore.load(tmp_path / "drive.sqlite3")
    assert reopened.storage_channel_id == -100123
    reopened.close()


@pytest.mark.parametrize("channel", [0, True, 1.2, "-1001", 2**63, -(2**63) - 1])
def test_portable_binding_rejects_ambiguous_or_out_of_range_channels(channel):
    store = DriveStore.create()
    payload = store.export()
    payload["storage_channel_id"] = channel
    with pytest.raises(CorruptStoreError, match="storage_channel_id"):
        DriveStore._from_payload(payload)
    assert store.storage_channel_id is None
    store.close()


async def test_completed_checkpoint_cannot_relabel_files_with_new_channel(tmp_path):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    store.add_file("/", "original.bin", 876, 5)
    await service.ensure_revision(store, store.revision)
    count = len(client.requests)
    old_binding = store.storage_channel_id
    changed = service._settings.model_copy(update={"storage_channel": -100999})
    restarted = TelegramBackupService(changed, pool)
    client.pinned = None
    with pytest.raises(RecoveryConflict) as blocked:
        await restarted.ensure_revision(store, store.revision)
    assert blocked.value.code == "RECOVERY_REQUIRED"
    assert restarted.recovery_required
    assert restarted.protection_status(store)["code"] == "RECOVERY_REQUIRED"
    assert len(client.requests) == count
    assert store.storage_channel_id == old_binding
    store.close()
    await pool.stop()


async def test_legacy_history_channel_checked_before_first_binding(tmp_path):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    checkpoint = service._prepare_checkpoint(store)
    with service._history() as connection:
        connection.execute(
            "UPDATE checkpoints SET drive_id=NULL,channel_id=-100999 WHERE id=?",
            (checkpoint["id"],),
        )
        connection.commit()
    with pytest.raises(RecoveryConflict, match="history"):
        await service.ensure_revision(store, store.revision)
    assert store.storage_channel_id is None
    assert client.requests == []
    store.close()
    await pool.stop()


async def test_transient_verification_failure_does_not_latch_recovery_mode(
    tmp_path, monkeypatch
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()

    async def unavailable(*args, **kwargs):
        raise TimeoutError("private transport detail")

    monkeypatch.setattr(client, "get_pinned_message_id", unavailable)
    with pytest.raises(BackupError, match="TimeoutError"):
        await service.verify_lineage(store)
    assert not service.recovery_required
    status = service.protection_status(store)
    assert status["state"] == "error"
    assert "code" not in status
    assert "private transport detail" not in status["error"]
    store.close()
    await pool.stop()


async def test_conflict_latch_clears_only_after_verified_publication(tmp_path):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    await service.ensure_revision(store, store.revision)
    original = client.captions[client.pinned]
    client.captions[client.pinned] = original.replace(store.drive_id, "other-drive")
    with pytest.raises(RecoveryConflict):
        await service.verify_lineage(store)
    assert service.recovery_required
    client.captions[client.pinned] = original
    with pytest.raises(RecoveryConflict):
        await service.verify_lineage(store)
    await service.ensure_revision(store, store.revision)
    assert not service.recovery_required
    assert service.protection_status(store)["state"] == "protected"
    store.close()
    await pool.stop()


async def test_clean_older_database_does_not_resume_deletion_or_bot_on_startup(
    tmp_path, monkeypatch
):
    service, pool, client = await service_for(tmp_path / "original", initialize=True)
    store = await service.restore()
    file = store.add_file("/", "retained.bin", 567, 9)
    path = "/" + file["id"]
    store.set_trashed(path, True)
    store.begin_deletion(path, channel_id=service._settings.storage_channel)
    await service.ensure_revision(store, store.revision)
    old_payload = json.loads(store.backup_snapshot()[1])
    store.create_folder("/", "Newer remote catalog")
    await service.ensure_revision(store, store.revision)
    configured = settings(tmp_path / "older")
    old = DriveStore._from_payload(
        old_payload,
        storage_path=configured.resolved_data_dir / "drive.sqlite3",
        persist=True,
    )
    assert not old.dirty
    old.close()
    deleted = []

    async def delete(*args):
        deleted.append(args)

    monkeypatch.setattr(client, "delete_messages", delete, raising=False)
    bot_factory = AsyncMock(side_effect=AssertionError("Bot must not resume"))
    monkeypatch.setattr(runtime_module, "build_bot_mode_client", bot_factory)
    runtime = Runtime(configured, pool=pool)
    try:
        await runtime.start()
        assert runtime.ready
        assert runtime.backup.recovery_required
        assert runtime.store.get_file(path, include_trashed=True)["message_id"] == 567
        assert runtime.store.pending_deletions()
        assert runtime.registry.list() == []
        assert deleted == []
        bot_factory.assert_not_called()
    finally:
        await runtime.stop()
        store.close()


async def test_late_newer_catalog_blocks_remote_deletion(tmp_path):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    file = store.add_file("/", "retained.bin", 567, 9)
    path = "/" + file["id"]
    store.set_trashed(path, True)
    store.begin_deletion(path, channel_id=service._settings.storage_channel)
    await service.ensure_revision(store, store.revision)
    newer = DriveStore._from_payload(store.export())
    newer.create_folder("/", "Later state")
    publisher = TelegramBackupService(settings(tmp_path / "publisher"), pool)
    await publisher.ensure_revision(newer, newer.revision)
    deleted = []

    async def delete(*args):
        deleted.append(args)

    client.delete_messages = delete
    manager = manager_for(tmp_path, pool, store, lambda: service.verify_lineage(store))
    record = manager.start_deletion(path)
    await record.task
    assert record.status is TransferStatus.ERROR
    assert service.recovery_required
    assert deleted == []
    assert store.pending_deletions()[0]["pending_message_ids"] == [567]
    await manager.close()
    manager.registry.close()
    newer.close()
    store.close()
    await pool.stop()


async def test_catalog_deletion_rechecks_lineage_before_removing_refs(
    tmp_path,
):
    client = FakeTelegramClient()
    pool = ClientPool([client])
    await pool.start()
    store = DriveStore.create()
    folder = store.create_folder("/", "Bulk")
    path = "/" + folder["id"]
    for identity in range(1, 102):
        store.add_file(path, str(identity), identity, 1)
    store.set_trashed(path, True)
    calls = 0

    async def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RecoveryConflict("A newer remote catalog appeared")

    manager = manager_for(tmp_path, pool, store, guard)
    record = manager.start_deletion(path)
    await record.task
    assert record.status is TransferStatus.ERROR
    assert client.deleted == []
    assert store.pending_deletions()[0]["pending_message_ids"] == list(range(1, 102))
    assert len(store.list_directory(path, include_trashed=True)) == 101
    await manager.close()
    manager.registry.close()
    store.close()
    await pool.stop()


async def test_cancel_cleanup_does_not_delete_media_after_late_conflict(
    tmp_path, monkeypatch
):
    client = FakeTelegramClient()
    pool = ClientPool([client])
    await pool.start()
    store = DriveStore.create()
    calls = 0

    async def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RecoveryConflict("A newer remote catalog appeared")

    manager = manager_for(tmp_path, pool, store, guard)
    record = manager.registry.create("uploading", total=6)
    source = tmp_path / "retained.part"
    source.write_bytes(b"source")
    original = client.send_document

    async def cancel_after_acceptance(*args, **kwargs):
        message = await original(*args, **kwargs)
        record.cancel_event.set()
        return message

    monkeypatch.setattr(client, "send_document", cancel_after_acceptance)
    with pytest.raises(RecoveryConflict):
        await manager._upload_path(record, source, "/", "source.bin", 6)
    assert client.deleted == []
    assert source.read_bytes() == b"source"
    assert record.context["remote_message_id"] == 99
    await manager.close()
    manager.registry.close()
    store.close()
    await pool.stop()


async def test_conflict_cannot_be_bypassed_by_later_local_revision_increments(tmp_path):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    await service.ensure_revision(store, store.revision)
    newer = DriveStore._from_payload(store.export())
    newer.create_folder("/", "Newer remote data")
    publisher = TelegramBackupService(settings(tmp_path / "publisher"), pool)
    await publisher.ensure_revision(newer, newer.revision)
    with pytest.raises(RecoveryConflict):
        await service.verify_lineage(store)
    protected_pin = client.pinned
    for index in range(3):
        store.create_folder("/", f"Late worker state {index}")
    assert store.revision > newer.revision
    with pytest.raises(RecoveryConflict):
        await service.ensure_revision(store, store.revision)
    assert client.pinned == protected_pin
    assert service.recovery_required
    newer.close()
    store.close()
    await pool.stop()


async def test_reconnect_verifies_conflict_before_recovery_or_bot_start(
    tmp_path, monkeypatch
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    await service.ensure_revision(store, store.revision)
    client.captions[client.pinned] = client.captions[client.pinned].replace(
        store.drive_id, "another-drive"
    )
    runtime = Runtime(service._settings, pool=pool)
    runtime.store = store
    runtime.backup = service
    runtime.ready = True
    resumed = AsyncMock()
    runtime.transfers = SimpleNamespace(recover=resumed)
    bot = AsyncMock(side_effect=AssertionError("Bot must not resume"))
    monkeypatch.setattr(runtime_module, "build_bot_mode_client", bot)

    async def stop_after_first_attempt(delay):
        assert delay == 30
        runtime.ready = False

    monkeypatch.setattr(runtime_module.asyncio, "sleep", stop_after_first_attempt)
    # Replace only the publication's coalescing wait so reconnect's retry delay
    # remains the explicit loop exit above.
    original = service.backup

    async def verify_without_coalescing(current):
        service._recovery_error = "Conflicting remote catalog"
        raise RecoveryConflict(service._recovery_error)

    monkeypatch.setattr(service, "backup", verify_without_coalescing)
    await runtime._reconnect()
    resumed.assert_not_awaited()
    bot.assert_not_called()
    assert service.recovery_required
    monkeypatch.setattr(service, "backup", original)
    store.close()
    await pool.stop()


async def test_offline_start_latches_changed_channel_before_any_file_access(tmp_path):
    from test_runtime_backup_barrier import RuntimePool

    configured = settings(tmp_path)
    store = DriveStore.create(configured.resolved_data_dir / "drive.sqlite3")
    store.bind_storage_channel(-100999)
    store.create_folder("/", "Readable metadata")
    store.close()
    runtime = Runtime(configured, pool=RuntimePool(unavailable=True))
    try:
        await runtime.start()
        assert runtime.ready
        assert runtime.backup.recovery_required
        assert runtime.store.list_directory("/")
        assert runtime.registry.list() == []
    finally:
        await runtime.stop()
