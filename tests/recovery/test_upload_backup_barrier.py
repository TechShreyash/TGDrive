"""Uploads finish only after their catalog revision has a confirmed checkpoint."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.support.mongo_fixtures import register_test_backend
from tgdrive.recovery.backup import BackupError
from tgdrive.storage.mongo import for_directory
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.manager import BackupPending, TransferManager
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path)


def registry_for(directory):
    backend = for_directory(directory)
    return TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)


class MediaClient:
    name = "barrier-test-bot"

    def __init__(self):
        self.is_connected = False
        self.messages = {}
        self.sent = []
        self.deleted = []

    async def start(self):
        self.is_connected = True

    async def stop(self):
        self.is_connected = False

    async def send_staged(
        self,
        channel,
        path,
        *,
        random_id,
        file_name,
        progress,
        offset=0,
        size=None,
        expected_sha256=None,
        before_dispatch=None,
    ):
        end = offset + size if size is not None else None
        content = path.read_bytes()[offset:end]
        if expected_sha256 is not None:
            assert hashlib.sha256(content).hexdigest() == expected_sha256
        if before_dispatch is not None:
            await before_dispatch()
        self.sent.append(random_id)
        message = SimpleNamespace(
            id=101 + len(self.messages),
            document=SimpleNamespace(file_size=len(content)),
        )
        self.messages[message.id] = message
        return message

    async def get_messages(self, channel, *, message_ids):
        return self.messages.get(message_ids)

    async def delete_messages(self, channel, message_ids):
        self.deleted.append(message_ids)


def manager_for(tmp_path, pool, callback, *, store=None, registry=None):
    return TransferManager(
        settings=SimpleNamespace(
            resolved_cache_dir=tmp_path / "cache",
            max_concurrent_transfers=1,
            storage_channel=-100123,
            bot_upload_limit=2_000_000_000,
            upload_limit=4_000_000_000,
        ),
        pool=pool,
        store=store or DriveStore.create(tmp_path / "drive.sqlite3"),
        registry=registry or registry_for(tmp_path),
        remote=SimpleNamespace(close=AsyncMock()),
        ensure_backup=callback,
    )


def staged_file(tmp_path, name="receipt.part"):
    path = tmp_path / name
    path.write_bytes(b"immutable bytes")
    return path


async def test_checkpoint_wait_releases_upload_slot_and_protects_committed_media(
    tmp_path,
):
    client = MediaClient()
    pool = ClientPool([client])
    await pool.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    revisions = []
    both_entered = asyncio.Event()

    async def protect(required):
        revisions.append(required)
        if len(revisions) == 2:
            both_entered.set()
        entered.set()
        await release.wait()
        return required

    manager = manager_for(tmp_path, pool, protect)
    first = staged_file(tmp_path, "first.part")
    second = staged_file(tmp_path, "second.part")
    one = await manager.enqueue_upload(first, "/", "one.bin", first.stat().st_size)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert one.phase == "backup_pending"
        assert one.status == TransferStatus.RUNNING
        assert one.context["metadata_committed"]
        assert one.context["catalog_revision"] == manager.store.revision
        assert first.exists()
        assert (await pool.health()).active_leases == 0
        with pytest.raises(BackupPending) as exc:
            await manager.cancel_job(one.id)
        assert exc.value.code == "BACKUP_PENDING"
        assert not one.cancel_event.is_set()

        two = await manager.enqueue_upload(
            second, "/", "two.bin", second.stat().st_size
        )
        await asyncio.wait_for(both_entered.wait(), 2)
        assert len(client.sent) == 2
        assert two.phase == "backup_pending"
        assert first.exists() and second.exists()
        assert client.deleted == []
        release.set()
        await asyncio.gather(one.task, two.task)
        assert one.status == two.status == TransferStatus.COMPLETED
        assert not first.exists() and not second.exists()
        assert one.context["protected_revision"] >= one.context["catalog_revision"]
    finally:
        release.set()
        await manager.registry.shutdown()
        manager.registry.close()
        manager.store.close()
        await pool.stop()


async def test_backup_error_restarts_without_staged_bytes_or_second_media_send(
    tmp_path,
):
    client = MediaClient()
    pool = ClientPool([client])
    await pool.start()
    failed = AsyncMock(side_effect=BackupError("checkpoint unavailable"))
    manager = manager_for(tmp_path, pool, failed)
    source = staged_file(tmp_path)
    record = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    await record.task
    assert record.status == TransferStatus.ERROR
    assert record.phase == "backup_pending"
    assert source.exists()
    assert len(client.sent) == 1
    required = record.context["catalog_revision"]
    manager.registry.close()
    manager.store.close()
    source.unlink()

    callback = AsyncMock(return_value=required)
    recovered = manager_for(
        tmp_path,
        pool,
        callback,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path),
    )
    try:
        with pytest.raises(BackupPending):
            await recovered.cancel_job(record.id)
        retry = await recovered.retry_job(record.id)
        await retry.task
        assert retry.status == TransferStatus.COMPLETED
        callback.assert_awaited_once_with(required)
        assert len(client.sent) == 1
        assert client.deleted == []
        assert len(recovered.store.list_directory("/")) == 1
    finally:
        await recovered.registry.shutdown()
        recovered.registry.close()
        recovered.store.close()
        await pool.stop()


async def test_store_commit_journal_gap_is_detected_before_cancel_or_resend(
    tmp_path, monkeypatch
):
    client = MediaClient()
    pool = ClientPool([client])
    await pool.start()
    callback = AsyncMock(side_effect=BackupError("not reached before crash"))
    manager = manager_for(tmp_path, pool, callback)
    source = staged_file(tmp_path)
    original = manager.store.add_file

    def commit_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("process lost response after catalog commit")

    monkeypatch.setattr(manager.store, "add_file", commit_then_crash)
    record = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    await record.task
    assert record.status == TransferStatus.ERROR
    assert not record.context.get("metadata_committed")
    assert manager.store.has_file_operation(record.id)
    with pytest.raises(BackupPending):
        await manager.cancel_job(record.id)
    callback.assert_not_awaited()
    required = manager.store.revision
    manager.registry.close()
    manager.store.close()

    confirmed = AsyncMock(return_value=required)
    recovered = manager_for(
        tmp_path,
        pool,
        confirmed,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path),
    )
    try:
        retry = await recovered.retry_job(record.id)
        await retry.task
        assert retry.status == TransferStatus.COMPLETED
        confirmed.assert_awaited_once_with(required)
        assert len(client.sent) == 1
        assert client.deleted == []
        assert not source.exists()
    finally:
        await recovered.registry.shutdown()
        recovered.registry.close()
        recovered.store.close()
        await pool.stop()


async def test_checkpoint_flood_wait_retries_only_backup_and_persists_deadline(
    tmp_path,
):
    client = MediaClient()
    pool = ClientPool([client])
    await pool.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def protect(required):
        nonlocal count
        count += 1
        if count == 1:
            raise BackupError("checkpoint rate limited", retry_after=0.01)
        entered.set()
        await release.wait()
        return required

    manager = manager_for(tmp_path, pool, protect)
    source = staged_file(tmp_path)
    record = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert record.phase == "backup_pending"
        assert record.context["backup_retry_at"] > 0
        journal = registry_for(tmp_path)
        assert journal.get(record.id).context["backup_retry_at"] > 0
        journal.close()
        assert len(client.sent) == 1
        release.set()
        await record.task
        assert record.status == TransferStatus.COMPLETED
        assert "backup_retry_at" not in record.context
        assert len(client.sent) == 1
    finally:
        release.set()
        await manager.registry.shutdown()
        manager.registry.close()
        manager.store.close()
        await pool.stop()


@pytest.mark.parametrize("protected", [0, True, None])
async def test_insufficient_checkpoint_keeps_staging_and_retryable_phase(
    tmp_path, protected
):
    client = MediaClient()
    pool = ClientPool([client])
    await pool.start()
    manager = manager_for(tmp_path, pool, AsyncMock(return_value=protected))
    source = staged_file(tmp_path)
    record = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    try:
        await record.task
        assert record.status == TransferStatus.ERROR
        assert record.phase == "backup_pending"
        assert record.context["metadata_committed"]
        assert source.exists()
        assert len(client.sent) == 1
        assert client.deleted == []
        retried = await manager.retry_job(record.id)
        await retried.task
        assert retried.status == TransferStatus.ERROR
        assert retried.phase == "backup_pending"
        assert source.exists()
        assert len(client.sent) == 1
        assert client.deleted == []
    finally:
        await manager.registry.shutdown()
        manager.registry.close()
        manager.store.close()
        await pool.stop()


async def test_shutdown_during_checkpoint_recovers_protection_without_media_resend(
    tmp_path,
):
    client = MediaClient()
    pool = ClientPool([client])
    await pool.start()
    entered = asyncio.Event()

    async def interrupted_checkpoint(required):
        entered.set()
        await asyncio.Event().wait()

    manager = manager_for(tmp_path, pool, interrupted_checkpoint)
    source = staged_file(tmp_path)
    record = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    await asyncio.wait_for(entered.wait(), 2)
    required = record.context["catalog_revision"]
    await manager.registry.shutdown()
    assert record.status == TransferStatus.QUEUED
    assert record.context["metadata_committed"]
    assert source.exists()
    manager.registry.close()
    manager.store.close()

    confirmed = AsyncMock(return_value=required)
    recovered = manager_for(
        tmp_path,
        pool,
        confirmed,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path),
    )
    try:
        await recovered.recover()
        resumed = recovered.registry.get(record.id)
        await resumed.task
        assert resumed.status == TransferStatus.COMPLETED
        confirmed.assert_awaited_once_with(required)
        assert len(client.sent) == 1
        assert client.deleted == []
        assert not source.exists()
    finally:
        await recovered.registry.shutdown()
        recovered.registry.close()
        recovered.store.close()
        await pool.stop()
