"""Folder identity remains stable across receipt, queue, send and restart boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.support.mongo_fixtures import register_test_backend
from tests.transfers.test_multipart_transfers import (
    Account,
    dispose,
    manager_for,
    registry_for,
    staged,
)
from tests.transfers.test_resumable_uploads import make
from tgdrive.core.errors import (
    DriveError,
    NotFoundError,
    PersistenceError,
    TrashedItemError,
)
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.manager import TransferManager
from tgdrive.transfers.tasks import TransferStatus
from tgdrive.transfers.uploads import UploadError


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    backend = register_test_backend(monkeypatch, tmp_path)
    return register_test_backend(monkeypatch, tmp_path / "data", backend)


def folder(store, name, parent="/"):
    node = store.create_folder(parent, name)
    return store.resolve_id(node["id"])


@pytest.fixture
def receipt(tmp_path):
    uploads, manager = make(tmp_path)
    try:
        yield uploads, manager
    finally:
        uploads.close()
        manager.registry.close()
        manager.store.close()


async def test_receipt_follows_moved_ancestor_and_rejects_new_stale_requests(receipt):
    uploads, manager = receipt
    parent = folder(manager.store, "Original")
    target = folder(manager.store, "資料", parent)
    receiving = uploads.create("file.bin", target, 3)
    identity = receiving["parent_node_id"]
    outer = folder(manager.store, "Other")
    manager.store.move(parent, outer)
    current = manager.store.resolve_id(identity)
    assert current != target
    with pytest.raises(NotFoundError):
        uploads.create("forged.bin", target, 3)
    with pytest.raises(NotFoundError):
        uploads.create("forged.bin", "/wrong/" + identity, 3)
    await uploads.append(receiving["id"], 0, b"one")
    job = await uploads.complete(receiving["id"])
    assert job.context["destination"] == current
    assert uploads.get(receiving["id"])["parent_id"] == current


async def test_inspection_of_moved_receipt_cannot_rewind_concurrent_append(
    receipt, monkeypatch
):
    uploads, manager = receipt
    monkeypatch.setattr("tgdrive.transfers.uploads.CHUNK_SIZE", 4)
    parent = folder(manager.store, "Original")
    target = folder(manager.store, "Target", parent)
    receiving = uploads.create("file.bin", target, 8)
    manager.store.move(parent, folder(manager.store, "Other"))
    current = manager.store.resolve_id(receiving["parent_node_id"])
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    destination = uploads._destination
    first = True

    def hold_inspection(item):
        nonlocal first
        result = destination(item)
        if first:
            first = False
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2), "Inspection worker was not released"
        return result

    monkeypatch.setattr(uploads, "_destination", hold_inspection)
    inspection = asyncio.create_task(uploads.inspect(receiving["id"]))
    append = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert uploads.get_unchecked(receiving["id"])["parent_id"] == target
        append = asyncio.create_task(uploads.append(receiving["id"], 0, b"abcd"))
        await asyncio.sleep(0)
        assert not append.done()
        release.set()
        snapshot = await inspection
        assert snapshot["parent_id"] == current
        assert snapshot["offset"] == 0
        await append
        committed = uploads.get_unchecked(receiving["id"])
        assert committed["parent_id"] == current
        assert committed["offset"] == 4
        assert committed["checksums"] == [
            {"offset": 0, "size": 4, "sha256": hashlib.sha256(b"abcd").hexdigest()}
        ]
        assert uploads.path(receiving["id"]).read_bytes() == b"abcd"
    finally:
        release.set()
        await asyncio.gather(
            inspection,
            *([append] if append is not None else []),
            return_exceptions=True,
        )


async def test_move_during_receipt_verification_uses_latest_path(receipt, monkeypatch):
    uploads, manager = receipt
    target = folder(manager.store, "Target")
    receiving = uploads.create("file.bin", target, 3)
    await uploads.append(receiving["id"], 0, b"one")
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = uploads._verify

    def verify(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        original(*args)

    monkeypatch.setattr(uploads, "_verify", verify)
    work = asyncio.create_task(uploads.complete(receiving["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        manager.store.move(target, folder(manager.store, "Other"))
        expected = manager.store.resolve_id(receiving["parent_node_id"])
        release.set()
        assert (await work).context["destination"] == expected
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


async def test_receipt_trash_blocks_writes_but_restore_or_cancel_remains_available(
    receipt,
):
    uploads, manager = receipt
    target = folder(manager.store, "Target")
    receiving = uploads.create("file.bin", target, 3)
    manager.store.set_trashed(target, True)
    assert uploads.get(receiving["id"])["offset"] == 0
    with pytest.raises(UploadError, match="destination folder"):
        await uploads.append(receiving["id"], 0, b"one")
    assert uploads.path(receiving["id"]).read_bytes() == b""
    manager.store.set_trashed(target, False)
    await uploads.append(receiving["id"], 0, b"one")
    manager.store.set_trashed(target, True)
    with pytest.raises(UploadError, match="destination folder"):
        await uploads.complete(receiving["id"])
    manager.store.delete(target)
    assert (await uploads.cancel(receiving["id"]))["state"] == "cancelled"
    assert not (uploads.root / (receiving["id"] + ".part")).exists()
    assert manager.enqueued == 0


async def test_receipt_rejects_destination_with_pending_permanent_deletion(receipt):
    uploads, manager = receipt
    target = folder(manager.store, "Target")
    receiving = uploads.create("file.bin", target, 3)
    manager.store.set_trashed(target, True)
    manager.store.begin_deletion(target)
    with pytest.raises(UploadError, match="destination folder"):
        await uploads.append(receiving["id"], 0, b"one")
    with pytest.raises(DriveError):
        uploads.create("new.bin", target, 3)


async def test_queued_restart_follows_uuid_after_destination_move(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    target = folder(manager.store, "Target")
    source = staged(manager, b"abcdefghij")
    manager._semaphore = asyncio.Semaphore(0)
    record = await manager.enqueue_upload(source, target, "ten.bin", 10)
    await asyncio.sleep(0)
    await manager.close()
    manager.store.move(target, folder(manager.store, "Other"))
    expected = manager.store.resolve_id(target[1:])
    settings = manager.settings
    manager.registry.close()
    manager.store.close()
    await manager.pool.stop()
    pool = ClientPool([Account("bot-one", ledger), Account("bot-two", ledger)])
    await pool.start()
    recovered = TransferManager(
        settings=settings,
        pool=pool,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path),
        remote=SimpleNamespace(close=AsyncMock()),
    )
    try:
        await recovered.recover()
        current = recovered.registry.get(record.id)
        await current.task
        assert current.status == TransferStatus.COMPLETED
        assert current.context["destination"] == expected
        assert current.context["destination_id"] == target[1:]
        items = recovered.store.list_directory(expected)
        assert len(items) == 1
        assert (
            recovered.store.get_file(expected + "/" + next(iter(items)))["size"] == 10
        )
    finally:
        await dispose(recovered)


async def test_move_during_remote_send_commits_in_same_current_shared_folder(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold = True
    target = folder(manager.store, "Target")
    shared = folder(manager.store, "Shared")
    token = manager.store.create_share(shared)
    record = await manager.enqueue_upload(
        staged(manager, b"abcdefghij"), target, "ten.bin", 10
    )
    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        manager.store.move(target, shared)
        expected = manager.store.resolve_id(record.context["destination_id"])
        ledger.release.set()
        await record.task
        assert record.status == TransferStatus.COMPLETED
        items = manager.store.list_directory(expected)
        path = expected + "/" + next(iter(items))
        assert manager.store.authorize_file(path, token)
        assert record.context["destination"] == expected
    finally:
        ledger.release.set()
        await dispose(manager)


async def test_trashed_destination_during_send_reconciles_and_cleans_own_parts(
    tmp_path,
):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold = True
    target = folder(manager.store, "Target")
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, target, "ten.bin", 10)
    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        manager.store.set_trashed(target, True)
        ledger.release.set()
        await record.task
        assert record.status == TransferStatus.CANCELLED
        assert not ledger.messages and not source.exists()
        assert not manager.store.has_file_operation(record.id)
    finally:
        ledger.release.set()
        await dispose(manager)


async def test_delete_worker_matches_moved_destination_uuid_before_cleanup(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold = True
    target = folder(manager.store, "Target")
    record = await manager.enqueue_upload(
        staged(manager, b"abcdefghij"), target, "ten.bin", 10
    )
    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        manager.store.move(target, folder(manager.store, "Other"))
        current = manager.store.resolve_id(record.context["destination_id"])
        assert record.context["destination"] == target
        manager.store.set_trashed(current, True)
        deletion = await manager.start_deletion(current)
        await asyncio.wait_for(record.cancel_event.wait(), 2)
        ledger.release.set()
        await asyncio.wait_for(deletion.task, 2)
        assert record.status == TransferStatus.CANCELLED
        assert deletion.status == TransferStatus.COMPLETED
        assert not ledger.messages
        with pytest.raises(NotFoundError):
            manager.store.resolve_id(target[1:])
    finally:
        ledger.release.set()
        await dispose(manager)


def test_store_destination_binding_requires_a_folder_and_honors_live_ancestry():
    store = DriveStore.create()
    try:
        parent = folder(store, "Parent")
        target = folder(store, "Target", parent)
        file = store.add_file("/", "file.bin", 5, 1)
        with pytest.raises(DriveError):
            store.upload_destination("/", directory_id=file["id"])
        store.set_trashed(parent, True)
        with pytest.raises(TrashedItemError):
            store.upload_destination(target, directory_id=target.rsplit("/", 1)[1])
        assert store.upload_destination("/") == {"id": "root", "path": "/"}
    finally:
        store.close()


async def test_catalog_outage_does_not_turn_destination_validation_into_cancellation(
    tmp_path, monkeypatch
):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold = True
    target = folder(manager.store, "Target")
    source = staged(manager, b"abcdefgh")
    record = await manager.enqueue_upload(source, target, "eight.bin", 8)
    original = manager.store.upload_destination

    def unavailable(*_args, **_kwargs):
        raise PersistenceError("synthetic catalog read outage")

    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        monkeypatch.setattr(manager.store, "upload_destination", unavailable)
        ledger.release.set()
        await record.task
        assert record.status == TransferStatus.ERROR
        assert not record.context.get("cancel_requested")
        assert source.exists() and ledger.messages
        sent = len(ledger.requests)
        monkeypatch.setattr(manager.store, "upload_destination", original)
        retried = await manager.retry_job(record.id)
        await retried.task
        assert retried.status == TransferStatus.COMPLETED
        assert len(ledger.requests) == sent
    finally:
        ledger.release.set()
        await dispose(manager)
