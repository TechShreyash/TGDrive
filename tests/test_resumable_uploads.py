from __future__ import annotations

import asyncio
import copy
import gc
import hashlib
import logging
import sqlite3
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

import tgdrive.uploads as module
from tgdrive.store import DriveStore
from tgdrive.tasks import TransferRegistry, TransferStatus
from tgdrive.uploads import CHUNK_SIZE, ResumableUploads, UploadError


class Manager:
    def __init__(self, tmp_path: Path):
        self.store = DriveStore.create()
        self.registry = TransferRegistry(database_path=tmp_path / "jobs.sqlite3")
        self.enqueued = 0

    def enqueue_upload(
        self, source, destination, name, size, *, transfer_id, folder_upload=None
    ):
        self.enqueued += 1
        job = self.registry.create("queued", transfer_id=transfer_id, total=size)
        job.context.update(
            kind="upload",
            local_path=str(source),
            destination=destination,
            name=name,
            size=size,
        )
        if folder_upload is not None:
            job.context["folder_upload"] = copy.deepcopy(folder_upload)
        self.registry.persist(job)
        return job


def make(tmp_path: Path, *, manager=None, **overrides):
    config = dict(
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
    manager.registry.update(first.id, status=TransferStatus.COMPLETED)
    uploads.path(item["id"]).unlink()
    uploads.release(item["id"])
    second = await uploads.complete(item["id"])
    assert first.id == second.id
    assert manager.enqueued == 1


async def test_receipt_logs_acceptance_once_without_chunk_or_private_data(
    tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="tgdrive.uploads")
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
    caplog.set_level(logging.INFO, logger="tgdrive.uploads")
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


async def test_failed_offset_commit_replays_written_bytes_safely(tmp_path, monkeypatch):
    uploads, _ = make(tmp_path)
    item = uploads.create("one.bin", "/", 3)
    save = uploads._save

    def fail(_item):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(uploads, "_save", fail)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
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
    manager.registry.update(record.id, status=TransferStatus.COMPLETED)
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


def test_untrusted_receipt_ids_and_corrupt_database_fail_closed(tmp_path):
    uploads, _ = make(tmp_path)
    for invalid in ["../other", "", "0" * 33]:
        with pytest.raises(UploadError) as error:
            uploads.path(invalid)
        assert error.value.status == 404
    uploads.close()
    database = tmp_path / "data" / "uploads.sqlite3"
    database.write_bytes(b"corrupt database")
    with pytest.raises(sqlite3.DatabaseError):
        make(tmp_path)


async def test_restart_reconciles_job_committed_before_receipt_queue_commit(
    tmp_path, monkeypatch
):
    uploads, manager = make(tmp_path, staging_budget_bytes=3)
    item = uploads.create("one.bin", "/", 3)
    await uploads.append(item["id"], 0, b"one")
    save = uploads._save

    def fail_queued(state):
        if state["state"] == "queued":
            raise sqlite3.OperationalError("receipt commit failed")
        save(state)

    monkeypatch.setattr(uploads, "_save", fail_queued)
    with pytest.raises(sqlite3.OperationalError):
        await uploads.complete(item["id"])
    assert uploads.get(item["id"])["state"] == "receiving"
    record = manager.registry.get(item["id"])
    manager.registry.update(record.id, status=TransferStatus.COMPLETED)
    restarted, _ = make(tmp_path, manager=manager, staging_budget_bytes=3)
    assert restarted.get(item["id"])["state"] == "finished"
    assert restarted.create("replacement.bin", "/", 3)["offset"] == 0


def test_upload_receipt_finalizer_closes_abandoned_connection(tmp_path):
    uploads, manager = make(tmp_path)
    item = uploads.create("committed.bin", "/", 3)
    connection = uploads.db
    reference = weakref.ref(uploads)
    del uploads
    gc.collect()
    assert reference() is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
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
    monkeypatch.setattr("tgdrive.mongo.for_settings", lambda _settings: backend)
    uploads, manager = make(tmp_path, **overrides)
    manager.uploads = uploads
    return uploads, manager, backend, rows


async def test_cloud_creation_reserves_shared_budget_and_drains_after_disconnect(
    tmp_path, monkeypatch
):
    from tgdrive.staging import StorageUnavailable, check_staging

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
    from tgdrive.mongo import DatabaseUnavailable

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
    job = manager.registry.create("queued", transfer_id=item["id"], total=3)
    manager.registry.update(job.id, status=TransferStatus.COMPLETED)
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
