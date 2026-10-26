"""Channel binding and transfer lineage guards preserve catalog references."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.transfers.test_transfers import FakeTelegramClient
from tgdrive.core.errors import CorruptStoreError, ValidationError
from tgdrive.recovery.backup import RecoveryConflict
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.manager import TransferManager
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus


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
    record = await manager.start_deletion(path)
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
    record = await manager.registry.create("uploading", total=6)
    source = tmp_path / "retained.part"
    source.write_bytes(b"source")
    original = client.send_staged

    async def cancel_after_acceptance(*args, **kwargs):
        message = await original(*args, **kwargs)
        record.cancel_event.set()
        return message

    monkeypatch.setattr(client, "send_staged", cancel_after_acceptance)
    with pytest.raises(RecoveryConflict):
        await manager._upload_path(record, source, "/", "source.bin", 6)
    assert client.deleted == []
    assert source.read_bytes() == b"source"
    assert record.context["remote_message_id"] == 99
    await manager.close()
    manager.registry.close()
    store.close()
    await pool.stop()
