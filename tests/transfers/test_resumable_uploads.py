from __future__ import annotations

import asyncio
import copy
import gc
import hashlib
import logging
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from pymongo.errors import ConnectionFailure

import tgdrive.transfers.uploads as module
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.storage.mongo import for_directory
from tgdrive.storage.store import DriveStore
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus
from tgdrive.transfers.uploads import CHUNK_SIZE, ResumableUploads, UploadError

pytestmark = pytest.mark.usefixtures("mongo_backend")


@pytest.fixture
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path / "data")


class Manager:
    def __init__(self, tmp_path: Path, *, backend=None):
        self.store = DriveStore.create()
        if backend is None:
            backend = for_directory(tmp_path / "data")
        self.registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
        self.enqueued = 0

    async def enqueue_upload(
        self, source, destination, name, size, *, transfer_id, folder_upload=None
    ):
        self.enqueued += 1
        job = await self.registry.create("queued", transfer_id=transfer_id, total=size)
        job.context.update(
            kind="upload",
            local_path=str(source),
            destination=destination,
            name=name,
            size=size,
        )
        if folder_upload is not None:
            job.context["folder_upload"] = copy.deepcopy(folder_upload)
        (await self.registry.persist(job))
        return job


def make(tmp_path: Path, *, manager=None, **overrides):
    config = dict(
        mongodb_uri=SecretStr("mongodb://synthetic.invalid"),
        mongodb_database="tgdrive",
        resolved_cache_dir=tmp_path / "cache",
        resolved_data_dir=tmp_path / "data",
        max_concurrent_transfers=2,
        upload_limit=CHUNK_SIZE * 3,
        staging_budget_bytes=CHUNK_SIZE * 6,
        min_free_bytes=0,
        upload_session_ttl_seconds=60,
    )
    config.update(overrides)
    manager = manager or Manager(tmp_path)
    return ResumableUploads(SimpleNamespace(**config), manager), manager


async def test_restart_resumes_exact_committed_offset_and_checksums(tmp_path):
    uploads, manager = make(tmp_path)
    data, tail = b"a" * CHUNK_SIZE, b"last"
    item = uploads.create("example.bin", "/", len(data) + len(tail))
    await uploads.append(item["id"], 0, data)
    # Simulate bytes written before a process died before committing its offset.
    with uploads.path(item["id"]).open("ab") as handle:
        handle.write(b"uncommitted tail")
    uploads.close()
    resumed, _ = make(tmp_path, manager=manager)
    assert resumed.get(item["id"])["offset"] == CHUNK_SIZE
    await resumed.append(item["id"], CHUNK_SIZE, tail)
    assert resumed.path(item["id"]).read_bytes() == data + tail
    state = resumed.get(item["id"])
    assert state["checksums"][0]["sha256"] == hashlib.sha256(data).hexdigest()
    job = await resumed.complete(item["id"])
    assert job.id == item["id"]
    assert manager.enqueued == 1


async def test_complete_is_idempotent_after_worker_removed_staging(tmp_path):
    uploads, manager = make(tmp_path)
    item = uploads.create("one.bin", "/", 3)
    await uploads.append(item["id"], 0, b"one")
    first = await uploads.complete(item["id"])
    (await manager.registry.update(first.id, status=TransferStatus.COMPLETED))
    uploads.path(item["id"]).unlink()
    uploads.release(item["id"])
    second = await uploads.complete(item["id"])
    assert first.id == second.id
    assert manager.enqueued == 1


@pytest.mark.parametrize("state", ["finished", "cancelled"])
async def test_terminal_receipt_cache_uses_persisted_expiry_after_restart(
    tmp_path, mongo_backend, monkeypatch, state
):
    uploads, manager = make(tmp_path)
    item = uploads.create("one.bin", "/", 3)
    if state == "cancelled":
        await uploads.cancel(item["id"])
    else:
        await uploads.append(item["id"], 0, b"one")
        job = await uploads.complete(item["id"])
        (await manager.registry.update(job.id, status=TransferStatus.COMPLETED))
        uploads.path(item["id"]).unlink()
        uploads.release(item["id"])
    saved = mongo_backend.db.receipts.find_one({"_id": item["id"]})
    deadline = saved["purge_at"].timestamp()
    assert deadline > item["expires_at"]

    monkeypatch.setattr(module.time, "time", lambda: item["expires_at"] + 1)
    assert uploads.get_unchecked(item["id"])["state"] == state
    restarted, _ = make(tmp_path, manager=manager)
    assert restarted.get_unchecked(item["id"])["state"] == state

    monkeypatch.setattr(module.time, "time", lambda: deadline + 1)
    replacement = restarted.create("replacement.bin", "/", 3)
    assert [row["id"] for row in restarted._all()] == [replacement["id"]]
    assert item["id"] not in restarted._items
    assert item["id"] not in restarted._terminal_expiry
    assert mongo_backend.db.receipts.find_one({"_id": item["id"]}) == saved
    restored, _ = make(tmp_path, manager=manager)
    with pytest.raises(UploadError) as expired:
        restored.get_unchecked(item["id"])
    assert expired.value.status == 404


@pytest.mark.parametrize("status", [TransferStatus.QUEUED, TransferStatus.ERROR])
async def test_receipt_cache_expiry_retains_unfinished_job(
    tmp_path, mongo_backend, monkeypatch, status
):
    uploads, manager = make(tmp_path)
    item = uploads.create("retry.bin", "/", 3)
    job = await manager.registry.create("uploading", transfer_id=item["id"], total=3)
    job.context.update(kind="upload", send_started=True)
    (await manager.registry.update(job.id, status=status))
    item["state"] = "finished"
    uploads._save(item)
    deadline = mongo_backend.db.receipts.find_one({"_id": item["id"]})[
        "purge_at"
    ].timestamp()
    monkeypatch.setattr(module.time, "time", lambda: deadline + 1)

    assert uploads.get_unchecked(item["id"])["state"] == "finished"
    assert [row["id"] for row in uploads._all()] == [item["id"]]
    assert uploads.path(item["id"]).is_file()
    assert manager.registry.get(job.id).context["send_started"] is True


async def test_receipt_lock_waiters_keep_expired_cache_owned(
    tmp_path, mongo_backend, monkeypatch
):
    uploads, _ = make(tmp_path)
    item = uploads.create("cancelled.bin", "/", 3)
    await uploads.cancel(item["id"])
    deadline = mongo_backend.db.receipts.find_one({"_id": item["id"]})[
        "purge_at"
    ].timestamp()
    lock = uploads.locks.setdefault(item["id"], asyncio.Lock())
    reference = weakref.ref(lock)
    await lock.acquire()
    waiting = asyncio.create_task(uploads.cancel(item["id"]))
    try:
        await asyncio.sleep(0)
        assert not waiting.done()
        monkeypatch.setattr(module.time, "time", lambda: deadline + 1)
        assert [row["id"] for row in uploads._all()] == [item["id"]]

        lock.release()
        lock = None
        # The awakened waiter owns the old lock before it resumes execution.
        assert reference() is uploads.locks[item["id"]]
        assert [row["id"] for row in uploads._all()] == [item["id"]]
        assert (await waiting)["state"] == "cancelled"
        gc.collect()
        assert reference() is None
        assert item["id"] not in uploads.locks
        assert uploads._all() == []
    finally:
        if lock is not None:
            lock.release()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_idle_receipt_locks_are_released_after_failed_lookups(tmp_path):
    uploads, _ = make(tmp_path)
    for index in range(20):
        with pytest.raises(UploadError) as missing:
            await uploads.cancel(f"{index:032x}")
        assert missing.value.status == 404
    del missing
    gc.collect()
    assert not uploads.locks


async def test_receipt_logs_acceptance_once_without_chunk_or_private_data(
    tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="tgdrive.transfers.uploads")
    uploads, manager = make(tmp_path)
    item = uploads.create("private-name.bin", "/", 3)
    await uploads.append(item["id"], 0, b"one")
    assert not caplog.records
    await uploads.complete(item["id"])
    await uploads.complete(item["id"])

    assert len(caplog.records) == 1
    assert (
        f"Device upload accepted id={item['id']} job_id={item['id']} bytes=3"
        in caplog.text
    )
    assert "verify_seconds=" in caplog.text and "handoff_seconds=" in caplog.text
    assert "private-name" not in caplog.text
    assert "one" not in caplog.text
    uploads.close()
    manager.registry.close()


async def test_corrupted_receipt_logs_failed_verification_without_acceptance(
    tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="tgdrive.transfers.uploads")
    uploads, manager = make(tmp_path)
    item = uploads.create("private-name.bin", "/", 3)
    await uploads.append(item["id"], 0, b"one")
    uploads.path(item["id"]).write_bytes(b"two")
    with pytest.raises(UploadError, match="checksum"):
        await uploads.complete(item["id"])
    assert "Device upload verification failed" in caplog.text
    assert "cancel this receipt and upload the original file again" in caplog.text
    assert "Device upload accepted" not in caplog.text
    assert "private-name" not in caplog.text
    uploads.close()
    manager.registry.close()


async def test_complete_rejects_corrupted_and_missing_staging(tmp_path):
    uploads, manager = make(tmp_path)
    item = uploads.create("one.bin", "/", 3)
    await uploads.append(item["id"], 0, b"one")
    uploads.path(item["id"]).write_bytes(b"two")
    with pytest.raises(UploadError, match="checksum"):
        await uploads.complete(item["id"])
    uploads.path(item["id"]).unlink()
    with pytest.raises(UploadError, match="missing"):
        await uploads.complete(item["id"])
    assert manager.enqueued == 0


async def test_wrong_offset_and_short_nonfinal_chunks_do_not_advance(tmp_path):
    uploads, _ = make(tmp_path)
    item = uploads.create("big.bin", "/", CHUNK_SIZE + 1)
    with pytest.raises(UploadError, match="offset") as conflict:
        await uploads.append(item["id"], 1, b"wrong")
    assert conflict.value.status == 409
    assert conflict.value.code == "UPLOAD_OFFSET_CHANGED"
    with pytest.raises(UploadError, match="last chunk") as invalid:
        await uploads.append(item["id"], 0, b"short")
    assert invalid.value.code == "UPLOAD_ERROR"
    assert uploads.get(item["id"])["offset"] == 0
    assert uploads.path(item["id"]).stat().st_size == 0


async def test_inspection_expires_and_cleans_receipt_under_its_lock(
    tmp_path, monkeypatch
):
    uploads, _ = make(tmp_path)
    item = uploads.create("expired.bin", "/", 3)
    await uploads.append(item["id"], 0, b"one")
    deadline = uploads.get_unchecked(item["id"])["expires_at"]
    monkeypatch.setattr(module.time, "time", lambda: deadline + 1)

    with pytest.raises(UploadError) as expired:
        await uploads.inspect(item["id"])

    assert expired.value.status == 410
    assert uploads.get_unchecked(item["id"])["state"] == "cancelled"
    assert not uploads.path(item["id"]).exists()


async def test_failed_offset_commit_replays_written_bytes_safely(tmp_path, monkeypatch):
    uploads, _ = make(tmp_path)
    item = uploads.create("one.bin", "/", 3)
    save = uploads._save

    def fail(_item):
        raise ConnectionFailure("disk full")

    monkeypatch.setattr(uploads, "_save", fail)
    with pytest.raises(ConnectionFailure, match="disk full"):
        await uploads.append(item["id"], 0, b"one")
    assert uploads.get(item["id"])["offset"] == 0
    monkeypatch.setattr(uploads, "_save", save)
    await uploads.append(item["id"], 0, b"two")
    assert uploads.path(item["id"]).read_bytes() == b"two"
    assert uploads.get(item["id"])["offset"] == 3


async def test_receipt_cancellation_waits_for_disk_write_then_cannot_resurrect(
    tmp_path, monkeypatch
):
    uploads, _ = make(tmp_path)
    item = uploads.create("one.bin", "/", 3)
    started, release = threading.Event(), threading.Event()
    fsync = module.os.fsync

    def blocked(descriptor):
        started.set()
        assert release.wait(3)
        fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", blocked)
    append = asyncio.create_task(uploads.append(item["id"], 0, b"one"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        cancel = asyncio.create_task(uploads.cancel(item["id"]))
        await asyncio.sleep(0)
        assert not cancel.done()
        release.set()
        await append
        await cancel
    finally:
        release.set()
    assert uploads.get_unchecked(item["id"])["state"] == "cancelled"
    assert not uploads.path(item["id"]).exists()


async def test_request_disconnect_waits_for_thread_and_records_committed_offset(
    tmp_path, monkeypatch
):
    uploads, _ = make(tmp_path)
    item = uploads.create("one.bin", "/", 3)
    started, release = threading.Event(), threading.Event()
    fsync = module.os.fsync

    def blocked(descriptor):
        started.set()
        assert release.wait(3)
        fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", blocked)
    append = asyncio.create_task(uploads.append(item["id"], 0, b"one"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        append.cancel()
        await asyncio.sleep(0)
        append.cancel()
        await asyncio.sleep(0)
        assert not append.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await append
    finally:
        release.set()
    assert uploads.get(item["id"])["offset"] == 3
    assert uploads.path(item["id"]).read_bytes() == b"one"


async def test_request_disconnect_during_verification_still_enqueues_receipt(
    tmp_path, monkeypatch
):
    uploads, manager = make(tmp_path)
    item = uploads.create("one.bin", "/", 3)
    await uploads.append(item["id"], 0, b"one")
    started, release = threading.Event(), threading.Event()
    verify = uploads._verify

    def blocked(state, path):
        started.set()
        assert release.wait(3)
        verify(state, path)

    monkeypatch.setattr(uploads, "_verify", blocked)
    completion = asyncio.create_task(uploads.complete(item["id"]))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        completion.cancel()
        await asyncio.sleep(0)
        completion.cancel()
        await asyncio.sleep(0)
        assert not completion.done()
        assert uploads.locks[item["id"]].locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await completion
    finally:
        release.set()
    assert uploads.get(item["id"])["state"] == "queued"
    job = manager.registry.get(item["id"])
    assert job.context["local_path"] == str(uploads.path(item["id"]))
    assert (await uploads.complete(item["id"])).id == job.id
    assert manager.enqueued == 1


async def test_budget_free_space_expiration_and_postrestart_release(
    tmp_path, monkeypatch
):
    uploads, manager = make(tmp_path, staging_budget_bytes=3)
    item = uploads.create("one.bin", "/", 3)
    with pytest.raises(UploadError) as full:
        uploads.create("two.bin", "/", 1)
    assert full.value.status == 507
    await uploads.append(item["id"], 0, b"one")
    record = await uploads.complete(item["id"])
    # Worker finished before receipts were constructed after a restart.
    (await manager.registry.update(record.id, status=TransferStatus.COMPLETED))
    restarted, _ = make(tmp_path, manager=manager, staging_budget_bytes=3)
    assert restarted.get(item["id"])["state"] == "finished"
    assert not restarted.path(item["id"]).exists()
    other = restarted.create("two.bin", "/", 3)
    monkeypatch.setattr(module.time, "time", lambda: other["expires_at"] + 1)
    with pytest.raises(UploadError) as expired:
        restarted.get(other["id"])
    assert expired.value.status == 410
    assert not restarted.path(other["id"]).exists()
    monkeypatch.undo()
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(UploadError) as disk:
        restarted.create("three.bin", "/", 1)
    assert disk.value.status == 507


def test_untrusted_receipt_ids_and_database_outage_fail_closed(
    tmp_path, mongo_backend, monkeypatch
):
    uploads, manager = make(tmp_path)
    for invalid in ["../other", "", "0" * 33]:
        with pytest.raises(UploadError) as error:
            uploads.path(invalid)
        assert error.value.status == 404
    uploads.close()

    def unavailable(_query):
        raise ConnectionFailure("synthetic receipt database outage")

    monkeypatch.setattr(mongo_backend.db.receipts, "find", unavailable)
    with pytest.raises(ConnectionFailure):
        make(tmp_path, manager=manager)
    assert not (tmp_path / "data" / "uploads.sqlite3").exists()


async def test_restart_reconciles_job_committed_before_receipt_queue_commit(
    tmp_path, monkeypatch
):
    uploads, manager = make(tmp_path, staging_budget_bytes=3)
    item = uploads.create("one.bin", "/", 3)
    await uploads.append(item["id"], 0, b"one")
    save = uploads._save

    def fail_queued(state):
        if state["state"] == "queued":
            raise ConnectionFailure("receipt commit failed")
        save(state)

    monkeypatch.setattr(uploads, "_save", fail_queued)
    with pytest.raises(ConnectionFailure):
        await uploads.complete(item["id"])
    assert uploads.get(item["id"])["state"] == "receiving"
    record = manager.registry.get(item["id"])
    (await manager.registry.update(record.id, status=TransferStatus.COMPLETED))
    restarted, _ = make(tmp_path, manager=manager, staging_budget_bytes=3)
    assert restarted.get(item["id"])["state"] == "finished"
    assert restarted.create("replacement.bin", "/", 3)["offset"] == 0


async def test_expired_receipt_inspection_keeps_bytes_owned_by_admitted_job(
    tmp_path, monkeypatch
):
    uploads, manager = make(tmp_path)
    item = uploads.create("owned.bin", "/", 3)
    await uploads.append(item["id"], 0, b"one")
    save = uploads._save

    def fail_queued(state):
        if state["state"] == "queued":
            raise ConnectionFailure("receipt commit failed after job admission")
        save(state)

    monkeypatch.setattr(uploads, "_save", fail_queued)
    with pytest.raises(ConnectionFailure):
        await uploads.complete(item["id"])
    assert uploads.get_unchecked(item["id"])["state"] == "receiving"
    deadline = uploads.get_unchecked(item["id"])["expires_at"]
    monkeypatch.setattr(module.time, "time", lambda: deadline + 1)

    snapshot = await uploads.inspect(item["id"])

    assert snapshot["state"] == "queued"
    assert snapshot["offset"] == 3
    assert uploads.path(item["id"]).read_bytes() == b"one"
    assert manager.registry.get(item["id"]).status is TransferStatus.QUEUED


def test_abandoned_upload_service_retains_shared_mongodb_receipts(
    tmp_path, mongo_backend
):
    uploads, manager = make(tmp_path)
    item = uploads.create("committed.bin", "/", 3)
    reference = weakref.ref(uploads)
    del uploads
    gc.collect()
    assert reference() is None
    assert mongo_backend.db.receipts.find_one({"_id": item["id"]}) is not None
    restored, _ = make(tmp_path, manager=manager)
    assert restored.get(item["id"])["offset"] == 0
    restored.close()
    restored.close()


def make_cloud(tmp_path, monkeypatch, **overrides):
    rows = {}
    collection = SimpleNamespace(find=lambda _query: list(rows.values()))

    def write(target, method, query, document, *, upsert):
        assert target is collection and method == "replace_one" and upsert
        rows[query["_id"]] = copy.deepcopy(document)

    backend = SimpleNamespace(db=SimpleNamespace(receipts=collection), write=write)
    monkeypatch.setattr("tgdrive.storage.mongo.for_settings", lambda _settings: backend)
    uploads, manager = make(tmp_path, **overrides)
    manager.uploads = uploads
    return uploads, manager, backend, rows


async def test_cloud_creation_reserves_shared_budget_and_drains_after_disconnect(
    tmp_path, monkeypatch
):
    from tgdrive.transfers.staging import StorageUnavailable, check_staging

    uploads, manager, backend, rows = make_cloud(
        tmp_path, monkeypatch, staging_budget_bytes=3
    )
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    fsync, write = module.os.fsync, backend.write

    def worker_fsync(descriptor):
        assert threading.get_ident() != loop_thread
        return fsync(descriptor)

    def slow_commit(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3), "The event loop could not release receipt creation"
        return write(*args, **kwargs)

    monkeypatch.setattr(module.os, "fsync", worker_fsync)
    monkeypatch.setattr(backend, "write", slow_commit)
    creation = asyncio.create_task(uploads.create_async("one.bin", "/", 3))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not creation.done()
        assert rows == {}
        [pending] = uploads._all()
        assert pending["total_size"] == 3
        # URL/multipart admissions use the same accounting while the first
        # receipt has not received a durable cloud acknowledgement.
        with pytest.raises(StorageUnavailable):
            check_staging(uploads.settings, manager, 1)
        with pytest.raises(UploadError) as unavailable:
            uploads.get_unchecked(pending["id"])
        assert unavailable.value.status == 404
        second = asyncio.create_task(uploads.create_async("two.bin", "/", 1))
        await asyncio.sleep(0)
        assert not second.done()
        creation.cancel()
        await asyncio.sleep(0)
        creation.cancel()
        await asyncio.sleep(0)
        assert not creation.done()
        assert uploads._creation_lock.locked()
        assert len(uploads._all()) == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await creation
        with pytest.raises(UploadError) as full:
            await second
        assert full.value.status == 507
        committed = uploads.get_unchecked(pending["id"])
        assert committed["state"] == "receiving"
        assert committed["offset"] == 0
        assert rows[pending["id"]]["data"] == committed
        assert uploads.path(pending["id"]).is_file()
        assert uploads._pending == {}
        assert len(uploads._all()) == 1
    finally:
        release.set()
        await asyncio.gather(
            creation, *([second] if second is not None else []), return_exceptions=True
        )
        uploads.close()
        manager.registry.close()
        manager.store.close()


async def test_cloud_creation_cleans_up_local_failure_before_persistence(
    tmp_path, monkeypatch
):
    uploads, manager, _backend, rows = make_cloud(tmp_path, monkeypatch)

    def broken_fsync(_descriptor):
        raise OSError("Synthetic local receipt failure")

    monkeypatch.setattr(module.os, "fsync", broken_fsync)
    try:
        with pytest.raises(OSError, match="Synthetic local"):
            await uploads.create_async("one.bin", "/", 3)
        assert uploads._pending == {}
        assert uploads._all() == []
        assert rows == {}
        assert not list(uploads.root.glob("*.part"))
    finally:
        uploads.close()
        manager.registry.close()
        manager.store.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_cloud_creation_commit_keeps_file_and_reservation(
    tmp_path, monkeypatch, committed
):
    from tgdrive.storage.mongo import DatabaseUnavailable

    uploads, manager, backend, rows = make_cloud(
        tmp_path, monkeypatch, staging_budget_bytes=3
    )
    write = backend.write

    def unknown(*args, **kwargs):
        if committed:
            write(*args, **kwargs)
        raise DatabaseUnavailable("Synthetic unknown receipt commit")

    monkeypatch.setattr(backend, "write", unknown)
    try:
        with pytest.raises(DatabaseUnavailable):
            await uploads.create_async("one.bin", "/", 3)
        assert uploads._pending == {}
        [retained] = uploads._all()
        assert uploads.path(retained["id"]).is_file()
        assert (retained["id"] in rows) is committed
        with pytest.raises(UploadError) as full:
            await uploads.create_async("two.bin", "/", 1)
        assert full.value.status == 507
        monkeypatch.setattr(backend, "write", write)
        monkeypatch.setattr(module.time, "time", lambda: retained["expires_at"] + 1)
        assert uploads._all() == [retained]
        replacement = await uploads.create_async("two.bin", "/", 3)
        assert replacement["state"] == "receiving"
        assert uploads.get_unchecked(retained["id"])["state"] == "cancelled"
        assert not uploads.path(retained["id"]).exists()
    finally:
        uploads.close()
        manager.registry.close()
        manager.store.close()


async def test_cloud_creation_reconciles_jobs_on_loop_and_saves_on_worker(
    tmp_path, monkeypatch
):
    uploads, manager, backend, _rows = make_cloud(
        tmp_path, monkeypatch, staging_budget_bytes=3
    )
    item = uploads.create("finished.bin", "/", 3)
    job = await manager.registry.create("queued", transfer_id=item["id"], total=3)
    (await manager.registry.update(job.id, status=TransferStatus.COMPLETED))
    loop_thread = threading.get_ident()
    write, get = backend.write, manager.registry.get

    def worker_write(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        return write(*args, **kwargs)

    def loop_get(*args, **kwargs):
        assert threading.get_ident() == loop_thread
        return get(*args, **kwargs)

    monkeypatch.setattr(backend, "write", worker_write)
    monkeypatch.setattr(manager.registry, "get", loop_get)
    try:
        replacement = await uploads.create_async("replacement.bin", "/", 3)
        assert replacement["state"] == "receiving"
        assert uploads.get_unchecked(item["id"])["state"] == "finished"
        assert not uploads.path(item["id"]).exists()
    finally:
        uploads.close()
        manager.registry.close()
        manager.store.close()


async def test_explicit_folder_metadata_survives_receipt_restart_and_job_handoff(
    tmp_path,
):
    uploads, manager = make(tmp_path)
    folder = manager.store.create_folder("/", "Photos")
    root_path = "/" + folder["id"]
    metadata = {
        "id": "folder-upload-session-1",
        "name": "Photos",
        "root_path": root_path,
        "created_at": 1234.5,
    }
    original = copy.deepcopy(metadata)
    try:
        item = await uploads.create_async(
            "one.bin", root_path, 3, folder_upload=metadata
        )
        metadata["name"] = "Caller mutation"
        assert item["folder_upload"] == original
        uploads.close()
        uploads, _ = make(tmp_path, manager=manager)
        assert uploads.get_unchecked(item["id"])["folder_upload"] == original
        await uploads.append(item["id"], 0, b"one")
        job = await uploads.complete(item["id"])
        assert job.context["folder_upload"] == original
        assert job.as_dict()["folder_upload"] == original
        history = await manager.registry.history()
        assert history["jobs"][0]["folder_upload"] == original
        standalone = uploads.create("standalone.bin", "/", 3)
        assert "folder_upload" not in standalone
        with pytest.raises(UploadError, match="outside"):
            uploads.create("outside.bin", "/", 3, folder_upload=original)
    finally:
        uploads.close()
        manager.registry.close()
        manager.store.close()


@pytest.mark.parametrize("move_ancestor", [False, True])
async def test_new_folder_receipts_follow_moved_root_and_ancestor(
    tmp_path, move_ancestor
):
    uploads, manager = make(tmp_path)
    store = manager.store
    ancestor = store.create_folder("/", "Original parent")
    ancestor_path = "/" + ancestor["id"]
    folder = store.create_folder(ancestor_path, "Photos")
    original_root = ancestor_path + "/" + folder["id"]
    child = store.create_folder(original_root, "Nested")
    original_parent = original_root + "/" + child["id"]
    target = store.create_folder("/", "Moved here")
    target_path = "/" + target["id"]
    metadata = {
        "id": "folder-upload-session-moved",
        "name": "Photos",
        "root_path": original_root,
        "created_at": 1234.5,
    }
    try:
        store.move(ancestor_path if move_ancestor else original_root, target_path)
        current_root = store.resolve_id(folder["id"])
        current_parent = store.resolve_id(child["id"])
        item = await uploads.create_async(
            "later.bin", original_parent, 3, folder_upload=metadata
        )
        assert item["parent_id"] == current_parent
        assert item["parent_node_id"] == child["id"]
        assert item["folder_upload"] == metadata | {"root_path": current_root}
        assert metadata["root_path"] == original_root
        await uploads.append(item["id"], 0, b"one")
        job = await uploads.complete(item["id"])
        assert job.context["destination"] == current_parent
        assert job.context["folder_upload"]["id"] == metadata["id"]
        assert job.context["folder_upload"]["name"] == metadata["name"]
        assert job.context["folder_upload"]["root_path"] == current_root
        with pytest.raises(UploadError, match="outside"):
            await uploads.create_async(
                "unrelated.bin", target_path, 3, folder_upload=metadata
            )
    finally:
        uploads.close()
        manager.registry.close()
        manager.store.close()
