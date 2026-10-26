"""Catalog removal preserves originals and the independent backup barrier."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.replication.test_replica import BACKUP, remote_bundle, setup
from tests.replication.test_replica import replica_backend as replica_backend
from tests.transfers.test_durable_jobs import Telegram, manager_for
from tests.transfers.test_durable_jobs import mongo_backend as mongo_backend
from tgdrive.replication.replica_state import files_in, parts_in
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.tasks import TransferStatus

pytestmark = pytest.mark.usefixtures("mongo_backend", "replica_backend")


async def cleanup(manager, pool, replica=None):
    await manager.close()
    manager.registry.close()
    if replica is not None:
        await replica.close()
    manager.store.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_catalog_delete_waits_for_backup_and_retains_original_messages(
    tmp_path,
):
    configured, store, remote, replica, _ = setup(tmp_path)
    telegram = Telegram()
    telegram.messages[77] = SimpleNamespace(id=77)
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool, store=store)
    manager.settings.storage_channel = configured.storage_channel
    manager._ensure_replica = replica.before_mutation
    node = next(files_in(store.export()))
    path = store.resolve_id(node["id"])
    store.set_trashed(path, True)
    original_finish = store.finish_deletion

    def checked_finish(intent_id):
        # The catalog subtree cannot disappear before its restore point exists.
        assert replica.journal.get("restore_point")
        copied = next(files_in(remote_bundle(remote, replica)["replica_catalog"]))
        assert remote.messages[BACKUP, copied["message_id"]] == b"abc"
        return original_finish(intent_id)

    store.finish_deletion = checked_finish
    try:
        job = await manager.start_deletion(path)
        await job.task
        assert job.status is TransferStatus.COMPLETED
        assert telegram.deleted == [] and 77 in telegram.messages
        assert remote.messages[configured.storage_channel, 77] == b"abc"
        assert store.list_trash() == {}
        copied = next(files_in(remote_bundle(remote, replica)["replica_catalog"]))
        assert copied["trashed"]
        assert remote.messages[BACKUP, parts_in(copied)[0]["message_id"]] == b"abc"
        assert store.pending_deletions() == []
    finally:
        await cleanup(manager, pool, replica)


@pytest.mark.asyncio
async def test_paused_backup_prevents_permanent_delete_and_retry_preserves_intent(
    tmp_path,
):
    configured, store, remote, replica, _ = setup(tmp_path)
    telegram = Telegram()
    telegram.messages[77] = SimpleNamespace(id=77)
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool, store=store)
    manager.settings.storage_channel = configured.storage_channel
    manager._ensure_replica = replica.before_mutation
    path = store.resolve_id(next(files_in(store.export()))["id"])
    store.set_trashed(path, True)
    replica.update(phase="paused")
    try:
        job = await manager.start_deletion(path)
        await job.task
        assert job.status is TransferStatus.ERROR
        assert telegram.deleted == [] and 77 in telegram.messages
        assert store.pending_deletions()[0]["pending_message_ids"] == [77]
        assert not remote.pins
        replica.update(phase="idle")
        retry = await manager.retry_job(job.id)
        await retry.task
        assert retry.status is TransferStatus.COMPLETED
        assert telegram.deleted == [] and 77 in telegram.messages
        assert replica.journal.get("restore_point")
    finally:
        await cleanup(manager, pool, replica)


@pytest.mark.asyncio
async def test_unavailable_backup_blocks_delete_without_erasing_main_or_its_refs(
    tmp_path,
):
    _, store, _, replica, _ = setup(tmp_path)
    telegram = Telegram()
    telegram.messages[77] = SimpleNamespace(id=77)
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool, store=store)
    path = store.resolve_id(next(files_in(store.export()))["id"])
    store.set_trashed(path, True)
    manager._ensure_replica = AsyncMock(
        side_effect=SettingsProblem("Independent copy is not confirmed.", status=503)
    )
    try:
        job = await manager.start_deletion(path)
        await job.task
        assert job.status is TransferStatus.ERROR
        manager._ensure_replica.assert_awaited_once()
        assert job.phase == "checking_backup"
        assert not telegram.deleted
        assert store.pending_deletions()[0]["pending_message_ids"] == [77]
        assert manager.store.message_ids(path, include_trashed=True) == [77]
    finally:
        await cleanup(manager, pool, replica)


@pytest.mark.asyncio
async def test_background_backup_does_not_block_normal_upload_completion(tmp_path):
    telegram = Telegram()
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool)
    manager._ensure_replica = AsyncMock(
        side_effect=SettingsProblem("Backup is paused.", status=409)
    )
    source = tmp_path / "payload.part"
    source.write_bytes(b"normal foreground upload")
    try:
        job = await manager.enqueue_upload(
            source, "/", "new.bin", source.stat().st_size
        )
        await job.task
        assert job.status is TransferStatus.COMPLETED
        manager._ensure_replica.assert_not_called()
        assert len(manager.store.list_directory("/")) == 1
        assert len(telegram.messages) == 1
    finally:
        await cleanup(manager, pool)
