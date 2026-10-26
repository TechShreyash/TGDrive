"""Slow receipt commits must not freeze streaming or acknowledge early."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.storage.test_mongodb import backend as backend_fixture
from tests.transfers.test_resumable_uploads import Manager
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.transfers.uploads import ResumableUploads

backend = backend_fixture
pytestmark = pytest.mark.skipif(
    os.environ.get("TGDRIVE_MONGO_TESTS") != "1",
    reason="Set TGDRIVE_MONGO_TESTS=1 for isolated MongoDB receipt checks",
)


def receipt_for(backend, tmp_path):
    settings = SimpleNamespace(
        resolved_cache_dir=tmp_path / "cache",
        resolved_data_dir=tmp_path / "data",
        max_concurrent_transfers=2,
        upload_limit=16 * 1024**2,
        staging_budget_bytes=32 * 1024**2,
        min_free_bytes=0,
        upload_session_ttl_seconds=3600,
    )
    manager = Manager(tmp_path, backend=backend)
    with patch("tgdrive.storage.mongo.for_settings", return_value=backend):
        uploads = ResumableUploads(settings, manager)
    return uploads, manager


@pytest.mark.asyncio
async def test_receipt_commit_allows_event_loop_to_run_and_drains_on_disconnect(
    backend, tmp_path, monkeypatch
):
    uploads, manager = receipt_for(backend, tmp_path)
    payload = b"receipt data" * 10000
    item = uploads.create("receipt.bin", "/", len(payload))
    entered, release = threading.Event(), threading.Event()
    original = backend.write
    loop_thread = threading.get_ident()

    def slow_commit(*args, **kwargs):
        entered.set()
        assert threading.get_ident() != loop_thread, (
            "A synchronous MongoDB receipt commit must not block the HTTP event loop"
        )
        assert release.wait(3), "The event loop could not release the slow commit"
        return original(*args, **kwargs)

    monkeypatch.setattr(backend, "write", slow_commit)
    task = asyncio.create_task(uploads.append(item["id"], 0, payload))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if task.done():
            task.result()
        # This coroutine runs while the actual MongoDB commit is deliberately
        # held. Existing committed state remains readable, and ACK is pending.
        assert not task.done()
        assert uploads.get_unchecked(item["id"])["offset"] == 0
        assert backend.db.receipts.find_one({"_id": item["id"]})["data"]["offset"] == 0
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()  # Disconnect cannot abandon a commit in progress.
        assert uploads.locks[item["id"]].locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        committed = uploads.get_unchecked(item["id"])
        assert committed["offset"] == len(payload)
        assert committed["checksums"] == [
            {
                "offset": 0,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ]
        assert backend.db.receipts.find_one({"_id": item["id"]})["data"] == committed
        assert uploads.path(item["id"]).read_bytes() == payload
        idle = uploads.locks.get(item["id"])
        assert idle is None or not idle.locked()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        uploads.close()
        manager.registry.close()


@pytest.mark.asyncio
async def test_failed_cloud_receipt_commit_keeps_old_offset_and_retries_exactly_once(
    backend, tmp_path, monkeypatch
):
    uploads, manager = receipt_for(backend, tmp_path)
    payload = b"durable retry" * 20000
    item = uploads.create("retry.bin", "/", len(payload))
    original = backend.write

    def unavailable(*_args, **_kwargs):
        raise DatabaseUnavailable("Synthetic receipt transaction failure")

    try:
        monkeypatch.setattr(backend, "write", unavailable)
        with pytest.raises(DatabaseUnavailable):
            await uploads.append(item["id"], 0, payload)
        assert uploads.get_unchecked(item["id"])["offset"] == 0
        assert backend.db.receipts.find_one({"_id": item["id"]})["data"]["offset"] == 0
        idle = uploads.locks.get(item["id"])
        assert idle is None or not idle.locked()
        monkeypatch.setattr(backend, "write", original)
        committed = await uploads.append(item["id"], 0, payload)
        assert committed["offset"] == len(payload)
        assert len(committed["checksums"]) == 1
        assert (
            committed["checksums"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
        )
        assert uploads.path(item["id"]).read_bytes() == payload
        assert backend.db.receipts.find_one({"_id": item["id"]})["data"] == committed
    finally:
        uploads.close()
        manager.registry.close()
