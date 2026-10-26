from __future__ import annotations

import asyncio
import gc
import hashlib
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import ConnectionFailure

from tests.support.mongo_fixtures import register_test_backend
from tgdrive.core.errors import NotFoundError
from tgdrive.storage.mongo import DatabaseUnavailable, for_directory
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.manager import TransferManager
from tgdrive.transfers.tasks import TransferNotFound, TransferRegistry, TransferStatus

pytestmark = pytest.mark.usefixtures("mongo_backend")


@pytest.fixture
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path)


def registry_for(path, **kwargs):
    backend = for_directory(path.parent)
    return TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend, **kwargs)


class Telegram:
    def __init__(self):
        self.name = "stable-test-account"
        self.is_connected = False
        self.messages = {}
        self.random_ids = {}
        self.calls = []
        self.deleted = []
        self.lose_response = False
        self.block = False
        self.started = asyncio.Event()
        self.fail_delete_call = 0
        self.delete_calls = 0

    async def start(self):
        self.is_connected = True

    async def stop(self):
        self.is_connected = False

    async def send_staged(
        self,
        _channel,
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
            (await before_dispatch())
        self.calls.append(random_id)
        if random_id not in self.random_ids:
            message_id = 101 + len(self.random_ids)
            message = SimpleNamespace(
                id=message_id, document=SimpleNamespace(file_size=len(content))
            )
            self.random_ids[random_id] = message
            self.messages[message_id] = message
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("accepted message response was lost")
        return self.random_ids[random_id]

    async def get_messages(self, _channel, *, message_ids):
        if isinstance(message_ids, list):
            return [self.messages.get(message_id) for message_id in message_ids]
        return self.messages.get(message_ids)

    async def delete_messages(self, _channel, message_ids):
        self.delete_calls += 1
        if self.fail_delete_call == self.delete_calls:
            raise TimeoutError("delete response lost")
        for message_id in (
            message_ids if isinstance(message_ids, list) else [message_ids]
        ):
            self.messages.pop(message_id, None)
            self.deleted.append(message_id)


def manager_for(tmp_path, pool, *, store=None, registry=None):
    settings = SimpleNamespace(
        resolved_cache_dir=tmp_path / "cache",
        max_concurrent_transfers=1,
        storage_channel=-100123,
        bot_upload_limit=2_000_000_000,
        upload_limit=4_000_000_000,
    )
    return TransferManager(
        settings=settings,
        pool=pool,
        store=store or DriveStore.create(tmp_path / "drive.sqlite3"),
        registry=registry or registry_for(tmp_path / "jobs.sqlite3"),
        remote=SimpleNamespace(close=AsyncMock()),
    )


async def test_lost_send_response_retries_same_sender_and_random_id(tmp_path):
    telegram = Telegram()
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool)
    source = tmp_path / "receipt.part"
    source.write_bytes(b"immutable payload")
    telegram.lose_response = True
    job = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    await job.task
    assert job.status == TransferStatus.ERROR
    assert source.exists()
    assert len(telegram.messages) == 1
    sender, random_id = job.context["sender"], job.context["random_id"]
    recovered = manager_for(
        tmp_path,
        pool,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path / "jobs.sqlite3"),
    )
    retry = await recovered.retry_job(job.id)
    await retry.task
    assert retry.status == TransferStatus.COMPLETED
    assert retry.context["sender"] == sender
    assert telegram.calls == [random_id, random_id]
    assert len(recovered.store.list_directory("/")) == len(telegram.messages) == 1
    assert not source.exists()
    await pool.stop()


async def test_retry_after_metadata_commit_does_not_duplicate_visible_file(
    tmp_path, monkeypatch
):
    telegram = Telegram()
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool)
    source = tmp_path / "receipt.part"
    source.write_bytes(b"bytes")
    original = manager.store.add_file

    def commit_then_lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("process stopped after metadata committed")

    monkeypatch.setattr(manager.store, "add_file", commit_then_lose_response)
    job = await manager.enqueue_upload(source, "/", "one.bin", 5)
    await job.task
    assert job.status == TransferStatus.ERROR
    assert len(manager.store.list_directory("/")) == 1
    recovered = manager_for(
        tmp_path,
        pool,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path / "jobs.sqlite3"),
    )
    retry = await recovered.retry_job(job.id)
    await retry.task
    assert retry.status == TransferStatus.COMPLETED
    assert len(recovered.store.list_directory("/")) == 1
    assert len(telegram.calls) == 1
    assert not source.exists()
    await pool.stop()


async def test_shutdown_preserves_ambiguous_send_for_restart_reconciliation(tmp_path):
    telegram = Telegram()
    telegram.block = True
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool)
    source = tmp_path / "receipt.part"
    source.write_bytes(b"bytes")
    job = await manager.enqueue_upload(source, "/", "one.bin", 5)
    await telegram.started.wait()
    await manager.registry.shutdown()
    assert job.status == TransferStatus.QUEUED
    assert source.exists()
    assert not job.context.get("cancel_requested")
    telegram.block = False
    recovered = manager_for(
        tmp_path,
        pool,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path / "jobs.sqlite3"),
    )
    await recovered.recover()
    resumed = recovered.registry.get(job.id)
    await resumed.task
    assert resumed.status == TransferStatus.COMPLETED
    assert len(telegram.messages) == 1
    assert telegram.calls[0] == telegram.calls[1]
    await pool.stop()


async def test_cancelled_ambiguous_send_stays_recoverable_until_remote_cleanup(
    tmp_path,
):
    telegram = Telegram()
    telegram.block = True
    pool = ClientPool([telegram])
    await pool.start()
    registry = registry_for(tmp_path / "jobs.sqlite3", cancel_grace_seconds=0.001)
    manager = manager_for(tmp_path, pool, registry=registry)
    source = tmp_path / "receipt.part"
    source.write_bytes(b"bytes")
    job = await manager.enqueue_upload(source, "/", "one.bin", 5)
    await telegram.started.wait()
    (await registry.cancel(job.id))
    with pytest.raises(asyncio.CancelledError):
        await job.task
    assert job.status == TransferStatus.QUEUED
    assert job.phase == "cleanup_pending"
    assert source.exists() and telegram.messages
    telegram.block = False
    recovered = manager_for(
        tmp_path,
        pool,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path / "jobs.sqlite3"),
    )
    assert recovered.registry.get(job.id).cancel_event.is_set()
    await recovered.recover()
    resumed = recovered.registry.get(job.id)
    await resumed.task
    assert resumed.status == TransferStatus.CANCELLED
    assert not telegram.messages and not source.exists()
    assert recovered.store.list_directory("/") == {}
    await pool.stop()


@pytest.mark.parametrize("confirmed", [0, 100])
async def test_failed_delete_retries_as_catalog_removal_after_restart(
    tmp_path, confirmed, monkeypatch
):
    telegram = Telegram()
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool)
    folder = manager.store.create_folder("/", "Delete")
    path = "/" + folder["id"]
    for message_id in range(1, 106):
        manager.store.add_file(path, f"{message_id}.bin", message_id, 1)
        telegram.messages[message_id] = SimpleNamespace(id=message_id)
    manager.store.set_trashed(path, True)
    # Preserve a failed deletion intent and its confirmed partial progress.
    intent = manager.store.begin_deletion(path, channel_id=-100123)
    manager.store.confirm_deletion_messages(intent["id"], list(range(1, confirmed + 1)))
    manager.store.fail_deletion(intent["id"], "MessageDeleteForbiddenError")
    finish = manager.store.finish_deletion

    def database_unavailable(_intent_id):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(manager.store, "finish_deletion", database_unavailable)
    job = await manager.start_deletion(path)
    await job.task
    assert job.status is TransferStatus.ERROR
    assert manager.store.message_ids(path, include_trashed=True) == list(range(1, 106))
    assert telegram.deleted == [] and len(telegram.messages) == 105
    monkeypatch.setattr(manager.store, "finish_deletion", finish)
    await manager.close()
    manager.registry.close()
    manager.store.close()
    recovered = manager_for(
        tmp_path,
        pool,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path / "jobs.sqlite3"),
    )
    retried = await recovered.retry_job(job.id)
    await retried.task
    assert retried.status == TransferStatus.COMPLETED
    assert telegram.deleted == [] and len(telegram.messages) == 105
    assert recovered.store.pending_deletions() == []
    assert recovered.store.list_directory("/") == {}
    assert recovered.store.export()["deletions"][0]["message_ids"] == []
    await recovered.close()
    recovered.registry.close()
    recovered.store.close()
    await pool.stop()


async def test_permanent_delete_needs_no_telegram_client_and_revokes_shares(tmp_path):
    pool = ClientPool([Telegram()])  # Deliberately not started.
    manager = manager_for(tmp_path, pool)
    file = manager.store.add_file("/", "old.bin", 77, 1)
    path = "/" + file["id"]
    token = manager.store.create_share(path)
    manager.store.set_trashed(path, True)
    try:
        job = await manager.start_deletion(path)
        await job.task
        assert job.status is TransferStatus.COMPLETED
        assert manager.store.list_trash() == {}
        assert manager.store.list_shares() == []
        with pytest.raises(NotFoundError):
            manager.store.authorize_file(path, token)
        assert manager.store.pending_deletions() == []
    finally:
        await manager.close()
        manager.registry.close()
        manager.store.close()


async def test_capacity_eviction_and_ttl_pruning_are_persistent(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    registry = registry_for(path, max_entries=1, ttl_seconds=1)
    first = await registry.create("upload")
    (await registry.update(first.id, status=TransferStatus.COMPLETED))
    second = await registry.create("upload")
    recovered = registry_for(path)
    assert [record.id for record in recovered.list()] == [second.id]
    with pytest.raises(TransferNotFound):
        recovered.get(first.id)
    (await registry.update(second.id, status=TransferStatus.COMPLETED))
    (await registry.prune(now=second.updated_at + 2))
    assert registry_for(path).list() == []


async def test_unresolved_jobs_and_staging_survive_ttl_and_capacity_pressure(tmp_path):
    source = tmp_path / "receipt.part"
    source.write_bytes(b"needed for reconciliation")
    path = tmp_path / "jobs.sqlite3"
    registry = registry_for(path, max_entries=1)
    record = await registry.create("upload")
    record.context.update(kind="upload", local_path=str(source), send_started=True)
    (await registry.update(record.id, status=TransferStatus.ERROR))
    (await registry.prune(now=record.updated_at + 1_000_000))
    assert registry.get(record.id).id == record.id
    with pytest.raises(RuntimeError, match="too many"):
        (await registry.create("other"))
    assert registry_for(path).get(record.id).context["send_started"]


async def test_persistence_failure_rolls_back_reported_job_state(
    tmp_path, mongo_backend, monkeypatch
):
    path = tmp_path / "jobs"
    registry = registry_for(path)
    record = await registry.create("upload")
    original = mongo_backend.db.jobs.replace_one

    def unavailable(*_args, **_kwargs):
        raise ConnectionFailure("synthetic database outage")

    monkeypatch.setattr(mongo_backend.db.jobs, "replace_one", unavailable)
    with pytest.raises(DatabaseUnavailable, match="could not be saved to MongoDB"):
        (await registry.update(record.id, status=TransferStatus.COMPLETED, current=100))
    assert record.status == TransferStatus.QUEUED and record.current == 0
    monkeypatch.setattr(mongo_backend.db.jobs, "replace_one", original)
    assert registry_for(path).get(record.id).status == TransferStatus.QUEUED


async def test_permanent_delete_cannot_be_cancelled(tmp_path):
    registry = registry_for(tmp_path / "jobs.sqlite3")
    record = await registry.create("deleting")
    record.context.update(kind="delete", deletion_id="intent")
    (await registry.persist(record))
    with pytest.raises(ValueError, match="cannot be cancelled"):
        (await registry.cancel(record.id))


async def test_corrupt_persisted_job_identity_fails_loading_without_reset(
    tmp_path, mongo_backend
):
    path = tmp_path / "jobs"
    registry = registry_for(path)
    record = await registry.create("queued")
    stored = mongo_backend.db.jobs.find_one({"_id": record.id})
    stored["data"]["id"] = "different-id"
    mongo_backend.db.jobs.replace_one({"_id": record.id}, stored)
    with pytest.raises(ValueError, match="job identity"):
        registry_for(path)
    assert mongo_backend.db.jobs.count_documents({}) == 1


async def test_abandoned_registry_leaves_shared_mongo_connection_and_records_available(
    tmp_path,
):
    path = tmp_path / "jobs"
    registry = registry_for(path)
    record = await registry.create("queued")
    reference = weakref.ref(registry)
    del registry
    gc.collect()
    assert reference() is None
    restored = registry_for(path)
    assert restored.get(record.id).id == record.id
    restored.close()
    restored.close()


async def test_restart_retries_lost_network_response_without_duplicate_upload(tmp_path):
    telegram = Telegram()
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool)
    source = tmp_path / "receipt.part"
    source.write_bytes(b"immutable payload")
    telegram.lose_response = True
    job = await manager.enqueue_upload(source, "/", "one.bin", source.stat().st_size)
    await job.task
    assert job.status is TransferStatus.ERROR
    assert job.context["restart_retryable"] is True
    assert "restart TGDrive" in job.error
    random_id = job.context["random_id"]
    await manager.close()
    manager.registry.close()
    manager.store.close()
    recovered = manager_for(
        tmp_path, pool, store=DriveStore.load(tmp_path / "drive.sqlite3")
    )
    try:
        await recovered.recover()
        resumed = recovered.registry.get(job.id)
        await resumed.task
        assert resumed.status is TransferStatus.COMPLETED
        assert telegram.calls == [random_id, random_id]
        assert len(telegram.messages) == 1
        assert len(recovered.store.list_directory("/")) == 1
        assert not source.exists()
    finally:
        await recovered.close()
        recovered.registry.close()
        recovered.store.close()
        await pool.stop()


async def test_restart_does_not_retry_nontransient_transfer_failure(tmp_path):
    telegram = Telegram()
    telegram.send_staged = AsyncMock(side_effect=PermissionError("channel access lost"))
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool)
    source = tmp_path / "receipt.part"
    source.write_bytes(b"bytes")
    job = await manager.enqueue_upload(source, "/", "one.bin", 5)
    await job.task
    assert job.status is TransferStatus.ERROR
    assert not job.context.get("restart_retryable")
    await manager.close()
    manager.registry.close()
    manager.store.close()
    recovered = manager_for(
        tmp_path, pool, store=DriveStore.load(tmp_path / "drive.sqlite3")
    )
    try:
        await recovered.recover()
        resumed = recovered.registry.get(job.id)
        assert resumed.status is TransferStatus.ERROR
        assert resumed.task is None
        assert telegram.send_staged.await_count == 1
        assert source.exists()
    finally:
        await recovered.close()
        recovered.registry.close()
        recovered.store.close()
        await pool.stop()


async def test_recovery_retries_a_finished_task_without_process_restart(tmp_path):
    telegram = Telegram()
    telegram.lose_response = True
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool)
    source = tmp_path / "receipt.part"
    source.write_bytes(b"retained payload")
    try:
        job = await manager.enqueue_upload(
            source, "/", "one.bin", source.stat().st_size
        )
        failed_task = job.task
        await failed_task
        assert job.status is TransferStatus.ERROR
        assert job.context["restart_retryable"] is True
        await manager.recover()
        assert job.task is not failed_task
        await job.task
        assert job.status is TransferStatus.COMPLETED
        assert len(telegram.messages) == 1
        assert len(manager.store.list_directory("/")) == 1
    finally:
        await manager.close()
        manager.registry.close()
        manager.store.close()
        await pool.stop()


@pytest.mark.parametrize("resumed", [False, True])
async def test_live_channel_check_blocks_upload_before_any_telegram_send(
    tmp_path, resumed
):
    from tgdrive.settings.managed_settings import SettingsProblem

    telegram = Telegram()
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool)
    manager._check_upload_allowed = lambda: None
    manager._ensure_channel = AsyncMock(
        side_effect=SettingsProblem("Turn storage channel auto-delete off.")
    )
    source = tmp_path / "receipt.part"
    source.write_bytes(b"retained payload")
    try:
        if resumed:
            from tgdrive.transfers.multipart_transfer import fingerprint

            checksum, parts = fingerprint(
                source, source.stat().st_size, source.stat().st_size
            )
            parts[0].update(random_id=123, sender=telegram.name, send_started=True)
            job = await manager.registry.create(
                "interrupted", total=source.stat().st_size
            )
            job.context.update(
                kind="upload",
                local_path=str(source),
                destination="/",
                destination_id="root",
                random_id=123,
                name="one.bin",
                size=source.stat().st_size,
                send_started=True,
                sender=telegram.name,
                sha256=checksum,
                multipart_plan=dict(version=1, kind="bot", parts=parts),
            )
            (await manager._resume(job))
        else:
            job = await manager.enqueue_upload(
                source, "/", "one.bin", source.stat().st_size
            )
        await job.task
        assert job.status is TransferStatus.ERROR
        assert "auto-delete" in job.error
        assert telegram.calls == []
        assert telegram.messages == {}
        assert manager.store.list_directory("/") == {}
        assert source.exists()
        manager._ensure_channel.assert_awaited_once()
    finally:
        await manager.close()
        manager.registry.close()
        manager.store.close()
        await pool.stop()


@pytest.mark.parametrize("effect", ["send_started", "remote_message_id"])
async def test_dispatched_upload_without_part_journal_preserves_staging(
    tmp_path, effect
):
    telegram = Telegram()
    pool = ClientPool([telegram])
    await pool.start()
    manager = manager_for(tmp_path, pool)
    source = tmp_path / "receipt.part"
    source.write_bytes(b"retained payload")
    job = await manager.registry.create("interrupted", total=source.stat().st_size)
    job.context.update(
        kind="upload",
        local_path=str(source),
        destination="/",
        destination_id="root",
        name="one.bin",
        size=source.stat().st_size,
        **{effect: True if effect == "send_started" else 101},
    )
    (await manager.registry.persist(job))
    try:
        (await manager._resume(job))
        await job.task
        assert job.status is TransferStatus.ERROR
        assert "missing its part journal" in job.error
        assert source.read_bytes() == b"retained payload"
        assert telegram.calls == []
        assert telegram.deleted == []
        assert manager.store.list_directory("/") == {}
    finally:
        await manager.close()
        manager.registry.close()
        manager.store.close()
        await pool.stop()
