"""Provider cooldowns preserve queued uploads instead of failing ordinary files."""

import asyncio
import time
from unittest.mock import AsyncMock

from tests.recovery.test_upload_backup_barrier import (
    MediaClient,
    manager_for,
    staged_file,
)
from tests.recovery.test_upload_backup_barrier import mongo_backend as mongo_backend
from tgdrive.recovery.backup import BackupError, RecoveryConflict
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.tasks import TransferStatus


async def ready_manager(tmp_path):
    client = MediaClient()
    pool = ClientPool([client])
    await pool.start()
    callback = AsyncMock(side_effect=lambda revision: revision)
    return manager_for(tmp_path, pool, callback), pool, client


async def close(manager, pool):
    await manager.registry.shutdown()
    manager.registry.close()
    manager.store.close()
    await pool.stop()


async def wait_phase(record, phase):
    async with asyncio.timeout(2):
        while record.phase != phase:  # noqa: ASYNC110 - observe durable job state
            await asyncio.sleep(0.001)


async def test_provider_wait_retries_same_upload_and_exposes_deadline(tmp_path):
    manager, pool, client = await ready_manager(tmp_path)
    manager._ensure_lineage = AsyncMock(
        side_effect=[
            BackupError("Provider cooldown", retry_after=0.05),
            None,
        ]
    )
    source = staged_file(tmp_path)
    record = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    try:
        await wait_phase(record, "retry_wait")
        assert record.as_dict()["retry_at"] > time.time()
        assert not client.sent
        await asyncio.wait_for(record.task, 2)
        assert record.status == TransferStatus.COMPLETED
        assert manager._ensure_lineage.await_count == 2
        assert len(client.sent) == 1
        assert "lineage_retry_at" not in record.context
        assert not source.exists()
    finally:
        await close(manager, pool)


async def test_cancel_wakes_an_unsent_provider_wait_without_sending(tmp_path):
    manager, pool, client = await ready_manager(tmp_path)
    manager._ensure_lineage = AsyncMock(
        side_effect=BackupError("Provider cooldown", retry_after=3600)
    )
    source = staged_file(tmp_path)
    record = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    try:
        await wait_phase(record, "retry_wait")
        (await manager.cancel_job(record.id))
        await asyncio.wait_for(record.task, 1)
        assert record.status == TransferStatus.CANCELLED
        assert not client.sent
        assert not source.exists()
    finally:
        await close(manager, pool)


async def test_confirmed_conflict_never_retries_or_sends(tmp_path):
    manager, pool, client = await ready_manager(tmp_path)
    manager._ensure_lineage = AsyncMock(
        side_effect=RecoveryConflict("Explicit recovery required", retry_after=0.01)
    )
    source = staged_file(tmp_path)
    record = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    try:
        await record.task
        assert record.status == TransferStatus.ERROR
        assert manager._ensure_lineage.await_count == 1
        assert not client.sent
        assert source.exists()
    finally:
        await close(manager, pool)


async def test_append_admission_uses_local_guard_but_still_waits_for_checkpoint(
    tmp_path,
):
    manager, pool, client = await ready_manager(tmp_path)
    manager._ensure_lineage = AsyncMock(
        side_effect=BackupError("Publication cooldown", retry_after=3600)
    )
    checked = []
    manager._check_upload_allowed = lambda: checked.append(len(client.sent))
    source = staged_file(tmp_path)
    record = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    try:
        assert checked == [0]  # Admission is guarded before background work begins.
        await asyncio.wait_for(record.task, 2)
        assert len(checked) >= 2  # A queued job rechecks admission before sending.
        assert set(checked) == {0}
        assert manager._ensure_lineage.await_count == 0
        assert manager._ensure_backup.await_count == 1
        assert record.status == TransferStatus.COMPLETED
        assert len(client.sent) == 1
    finally:
        await close(manager, pool)


async def test_restart_preserves_guard_deadline_and_sends_only_once(
    tmp_path, monkeypatch
):
    manager, pool, client = await ready_manager(tmp_path)
    manager._ensure_lineage = AsyncMock(
        side_effect=BackupError("Provider cooldown", retry_after=0.3)
    )
    source = staged_file(tmp_path)
    record = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    await wait_phase(record, "retry_wait")
    deadline = record.context["lineage_retry_at"]
    import tgdrive.transfers.tasks as tasks_module

    monkeypatch.setattr(tasks_module, "_SHUTDOWN_DRAIN_SECONDS", 0.001)
    await manager.registry.shutdown()
    manager.registry.close()
    manager.store.close()
    resumed = manager_for(
        tmp_path,
        pool,
        AsyncMock(side_effect=lambda revision: revision),
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
    )
    resumed._ensure_lineage = AsyncMock()
    try:
        await resumed.recover()
        recovered = resumed.registry.get(record.id)
        assert recovered.context["lineage_retry_at"] == deadline
        await asyncio.sleep(0.01)
        assert not client.sent
        await asyncio.wait_for(recovered.task, 2)
        assert recovered.status == TransferStatus.COMPLETED
        assert len(client.sent) == 1
    finally:
        await close(resumed, pool)
