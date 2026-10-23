from __future__ import annotations

import asyncio
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

    def enqueue_upload(self, source, destination, name, size, *, transfer_id):
        self.enqueued += 1
        job = self.registry.create("queued", transfer_id=transfer_id, total=size)
        job.context.update(
            kind="upload",
            local_path=str(source),
            destination=destination,
            name=name,
            size=size,
        )
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
    with pytest.raises(UploadError, match="offset"):
        await uploads.append(item["id"], 1, b"wrong")
    with pytest.raises(UploadError, match="last chunk"):
        await uploads.append(item["id"], 0, b"short")
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
