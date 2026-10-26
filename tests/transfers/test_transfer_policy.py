import asyncio
from unittest.mock import AsyncMock

from tests.transfers.test_multipart_transfers import (
    dispose,
    manager_for,
    staged,
    stored,
)
from tests.transfers.test_multipart_transfers import mongo_backend as mongo_backend
from tests.transfers.test_transfers import make_manager
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus


async def test_smaller_operator_split_preserves_all_bytes(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    manager.settings.bot_upload_limit = 4 * 1024**2
    manager.settings.upload_split_mib = 1
    manager.settings.upload_limit = 16 * 1024**2
    payload = b"a" * (2 * 1024**2) + b"end"
    source = staged(manager, payload)
    record = await manager.enqueue_upload(source, "/", "split.bin", len(payload))
    try:
        await asyncio.wait_for(record.task, 5)
        assert record.status is TransferStatus.COMPLETED
        item = stored(manager)
        assert [part["size"] for part in item["parts"]] == [1024**2, 1024**2, 3]
        result = b"".join(
            ledger.messages[part["message_id"]].content for part in item["parts"]
        )
        assert result == payload
    finally:
        await dispose(manager)


async def test_url_source_remains_remote_after_staging_and_resume_uses_staged_file(
    tmp_path, mongo_backend
):
    manager, store, client, pool = await make_manager(tmp_path)
    manager.registry = TransferRegistry(
        mongo=mongo_backend.db.jobs, mongo_backend=mongo_backend
    )
    client.wait_during_upload = True
    job = await manager.start_remote(
        url="https://example.com/file.bin", destination="/", filename=None
    )
    try:
        await asyncio.wait_for(client.started_upload.wait(), 2)
        assert manager.registry.history_snapshot(job)["kind"] == "remote"
        # Simulate interruption after durable local staging, before final publication.
        await manager.registry.shutdown()
        assert job.status is TransferStatus.QUEUED
        manager.registry.close()
        manager.registry = TransferRegistry(
            mongo=mongo_backend.db.jobs, mongo_backend=mongo_backend
        )
        job = manager.registry.get(job.id)
        assert manager.registry.history_snapshot(job)["kind"] == "remote"
        manager._upload_path = AsyncMock()
        manager._remote_pipeline = AsyncMock()
        (await manager._resume(job))
        await job.task
        manager._upload_path.assert_awaited_once()
        manager._remote_pipeline.assert_not_awaited()
    finally:
        client.release_upload.set()
        await manager.close()
        await pool.stop()
        store.close()
        manager.registry.close()
