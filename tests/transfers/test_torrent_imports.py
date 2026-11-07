"""Torrent parents reuse durable upload jobs without duplicating stored files."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.support.mongo_fixtures import reopen_store
from tests.transfers.test_multipart_transfers import (
    Account,
    dispose,
    manager_for,
    registry_for,
)
from tests.transfers.test_multipart_transfers import mongo_backend as mongo_backend
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.manager import TransferManager
from tgdrive.transfers.staging import staging_usage
from tgdrive.transfers.tasks import TransferStatus
from tgdrive.transfers.torrent import (
    TorrentCancelled,
    TorrentError,
    TorrentFile,
    TorrentManifest,
    TorrentTransportError,
)

SOURCE = "magnet:?xt=urn:btih:" + "a" * 40


async def test_transient_download_retries_reuse_reservation_and_release_slot(
    tmp_path, monkeypatch
):
    manager, ledger = await manager_for(tmp_path)
    manifest = TorrentManifest("file.bin", (TorrentFile("file.bin", 4),), 4, False)
    attempts, waits = [], []
    monkeypatch.setattr("tgdrive.transfers.torrent.available", lambda: True)

    async def download(source, directory, *, admit, **_kwargs):
        attempts.append((source, directory))
        await admit(manifest)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "file.bin"
        if len(attempts) == 1:
            path.write_bytes(b"pa")
            raise TorrentTransportError("Synthetic transport timeout")
        assert path.read_bytes() == b"pa"
        path.write_bytes(b"data")
        return manifest

    async def wait(delay, cancel_event):
        parent = next(
            job
            for job in manager.registry.list()
            if job.context.get("kind") == "torrent"
        )
        waits.append(delay)
        assert (
            manager._torrent_slots._value == manager.settings.max_concurrent_transfers
        )
        assert parent.status is TransferStatus.QUEUED
        assert parent.phase == "source_retry_wait"
        assert parent.as_dict()["source_attempt"] == 2
        assert parent.as_dict()["retry_at"] > 0
        durable = manager.registry._durable[parent.id]["context"]
        assert durable["source_attempt"] == 2
        assert durable["source_retry_at"] == parent.context["source_retry_at"]
        assert staging_usage(manager)[0] == 4
        assert not cancel_event.is_set()

    manager.remote._wait_for_retry = wait
    monkeypatch.setattr("tgdrive.transfers.torrent.download_torrent", download)
    try:
        record = await manager.start_torrent(source=SOURCE, destination="/")
        await record.task
        assert record.status is TransferStatus.COMPLETED, record.error
        assert attempts == [(SOURCE, manager.cache_dir / "torrents" / record.id)] * 2
        assert len(waits) == 1 and 14 <= waits[0] <= 15
        assert len(ledger.messages) == 1
    finally:
        await dispose(manager)


@pytest.mark.parametrize("transient", [False, True])
async def test_permanent_failure_stops_and_transient_failures_use_three_retries(
    tmp_path, monkeypatch, transient
):
    manager, ledger = await manager_for(tmp_path)
    attempts = []
    monkeypatch.setattr("tgdrive.transfers.torrent.available", lambda: True)

    async def failed(*_args, **_kwargs):
        attempts.append(True)
        error = TorrentTransportError if transient else TorrentError
        raise error("Synthetic source failure")

    manager.remote._wait_for_retry = AsyncMock()
    monkeypatch.setattr("tgdrive.transfers.torrent.download_torrent", failed)
    try:
        record = await manager.start_torrent(source=SOURCE, destination="/")
        await record.task
        assert record.status is TransferStatus.ERROR
        assert len(attempts) == (4 if transient else 1)
        assert manager.remote._wait_for_retry.await_count == (3 if transient else 0)
        if transient:
            delays = [
                call.args[0] for call in manager.remote._wait_for_retry.await_args_list
            ]
            assert all(
                expected - 1 <= actual <= expected
                for actual, expected in zip(delays, [15, 30, 60], strict=True)
            )
        assert ledger.calls == []
        assert not record.context.get("restart_retryable")
    finally:
        await dispose(manager)


async def test_cancel_during_torrent_backoff_never_starts_another_attempt(
    tmp_path, monkeypatch
):
    from tgdrive.transfers.remote import RemoteClient

    manager, ledger = await manager_for(tmp_path)
    manager.remote = RemoteClient()
    waiting = asyncio.Event()
    attempts = []
    monkeypatch.setattr("tgdrive.transfers.torrent.available", lambda: True)

    async def failed(*_args, **_kwargs):
        attempts.append(True)
        raise TorrentTransportError("Synthetic timeout")

    original_wait = manager.remote._wait_for_retry

    async def wait(delay, cancel_event):
        waiting.set()
        await original_wait(delay, cancel_event)

    monkeypatch.setattr(manager.remote, "_wait_for_retry", wait)
    monkeypatch.setattr("tgdrive.transfers.torrent.download_torrent", failed)
    try:
        record = await manager.start_torrent(source=SOURCE, destination="/")
        await asyncio.wait_for(waiting.wait(), 2)
        await manager.cancel_job(record.id)
        await record.task
        assert record.status is TransferStatus.CANCELLED
        assert attempts == [True] and ledger.calls == []
        assert (
            manager._torrent_slots._value == manager.settings.max_concurrent_transfers
        )
    finally:
        await dispose(manager)


async def test_reopened_torrent_preserves_retry_deadline_and_remaining_budget(
    tmp_path, monkeypatch
):
    manager, ledger = await manager_for(tmp_path)
    record = await manager.registry.create(
        "source_retry_wait",
        context={
            "kind": "torrent",
            "source": SOURCE,
            "destination": "/",
            "source_attempt": 3,
            "source_retry_at": time.time() + 30,
        },
    )
    manager.registry = registry_for(tmp_path)
    record = manager.registry.get(record.id)
    attempts = []

    async def failed(*_args, **_kwargs):
        attempts.append(True)
        raise TorrentTransportError("Synthetic timeout")

    manager.remote._wait_for_retry = AsyncMock()
    monkeypatch.setattr("tgdrive.transfers.torrent.download_torrent", failed)
    try:
        await manager._resume(record)
        await record.task
        assert record.status is TransferStatus.ERROR
        assert len(attempts) == 2
        delays = [
            call.args[0] for call in manager.remote._wait_for_retry.await_args_list
        ]
        assert len(delays) == 2
        assert 29 <= delays[0] <= 30 and 59 <= delays[1] <= 60
        assert ledger.calls == []
        manager.remote._wait_for_retry.reset_mock()
        await manager.retry_job(record.id)
        await record.task
        assert record.status is TransferStatus.ERROR
        assert len(attempts) == 6
        assert manager.remote._wait_for_retry.await_count == 3
    finally:
        await dispose(manager)


def torrent_source(monkeypatch, *, manifest=None):
    manifest = manifest or TorrentManifest(
        "Album",
        (TorrentFile("Album/readme.txt", 3), TorrentFile("Album/Disc/song.bin", 3)),
        6,
        True,
        "a" * 40,
    )
    calls = []

    async def download(source, directory, *, remote, cancel_event, progress, admit):
        calls.append(source)
        await admit(manifest)
        current = 0
        for file in manifest.files:
            target = directory / file.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * file.size)
            current += file.size
            await progress(current, manifest.total_size)
        return manifest

    monkeypatch.setattr("tgdrive.transfers.torrent.available", lambda: True)
    monkeypatch.setattr("tgdrive.transfers.torrent.download_torrent", download)
    return calls


async def test_nested_torrent_keeps_existing_folder_and_shows_one_task(
    tmp_path, monkeypatch
):
    calls = torrent_source(monkeypatch)
    manager, ledger = await manager_for(tmp_path)
    existing = manager.store.create_folder("/", "Album")
    try:
        record = await manager.start_torrent(source=SOURCE, destination="/")
        await record.task
        assert record.status is TransferStatus.COMPLETED
        tree = record.context["torrent_tree"]
        assert tree["root"]["name"] == "Album (1)"
        assert tree["root"]["id"] != existing["id"]
        assert manager.store.list_directory("/" + existing["id"]) == {}
        assert record.context["destination"] == tree["folders"][""]
        assert [
            item["name"]
            for item in manager.store.list_directory(tree["folders"]["Disc"]).values()
        ] == ["song.bin"]
        assert len(ledger.messages) == 2
        assert calls == [SOURCE]
        assert record.as_dict()["torrent_files"] == {"total": 2, "completed": 2}
        history = await manager.registry.task_history()
        assert history["total_tasks"] == 1
        assert [job["id"] for job in history["jobs"]] == [record.id]
        assert history["jobs"][0]["kind"] == "torrent"
        assert staging_usage(manager) == (0, 0)
    finally:
        await dispose(manager)


@pytest.mark.parametrize("restart", [False, True])
async def test_torrent_retry_skips_completed_files_even_after_child_pruning(
    tmp_path, monkeypatch, restart
):
    calls = torrent_source(monkeypatch)
    manager, ledger = await manager_for(tmp_path)
    original = manager._upload_path
    failed = False

    async def fail_second(record, local_path, destination, file_name, size):
        nonlocal failed
        if file_name == "song.bin" and not failed:
            failed = True
            raise ConnectionError("synthetic interrupted upload")
        await original(record, local_path, destination, file_name, size)

    monkeypatch.setattr(manager, "_upload_path", fail_second)
    record = await manager.start_torrent(source=SOURCE, destination="/")
    await record.task
    assert record.status is TransferStatus.ERROR
    assert record.context["torrent_completed"] == [0]
    assert len(ledger.messages) == 1
    await manager.registry._remove(f"{record.id}_0")
    if restart:
        settings = manager.settings
        await dispose(manager)
        pool = ClientPool([Account("bot-one", ledger), Account("bot-two", ledger)])
        await pool.start()
        manager = TransferManager(
            settings=settings,
            pool=pool,
            store=reopen_store(tmp_path / "drive"),
            registry=registry_for(tmp_path),
            remote=manager.remote,
        )
    try:
        retried = await manager.retry_job(record.id)
        await retried.task
        assert retried.status is TransferStatus.COMPLETED
        assert retried.context["torrent_completed"] == [0, 1]
        assert len(ledger.messages) == len(ledger.calls) == 2
        assert calls == [SOURCE]
        assert len(manager.store.list_directory("/")) == 1
    finally:
        await dispose(manager)


@pytest.mark.parametrize("per_file", [False, True])
async def test_torrent_limits_reject_before_writing_payload(
    tmp_path, monkeypatch, per_file
):
    torrent_source(monkeypatch)
    manager, ledger = await manager_for(tmp_path)
    if per_file:
        manager.settings.upload_limit = 2
    else:
        manager.settings.staging_budget_bytes = 5
    try:
        record = await manager.start_torrent(source=SOURCE, destination="/")
        await record.task
        assert record.status is TransferStatus.ERROR
        assert not record.context.get("torrent_downloaded")
        assert not (manager.cache_dir / "torrents" / record.id / "Album").exists()
        assert ledger.calls == []
        assert manager.store.list_directory("/") == {}
    finally:
        await dispose(manager)


async def test_cancelling_torrent_download_stops_and_removes_its_stage(
    tmp_path, monkeypatch
):
    ready = asyncio.Event()
    monkeypatch.setattr("tgdrive.transfers.torrent.available", lambda: True)

    async def downloading(source, directory, *, cancel_event, progress, admit, **_):
        manifest = TorrentManifest("file.bin", (TorrentFile("file.bin", 4),), 4, False)
        await admit(manifest)
        directory.mkdir(parents=True)
        (directory / "file.bin").write_bytes(b"part")
        await progress(4, 4)
        ready.set()
        await cancel_event.wait()
        raise TorrentCancelled("cancelled")

    monkeypatch.setattr("tgdrive.transfers.torrent.download_torrent", downloading)
    manager, ledger = await manager_for(tmp_path)
    try:
        record = await manager.start_torrent(source=SOURCE, destination="/")
        await asyncio.wait_for(ready.wait(), 2)
        await manager.cancel_job(record.id)
        await record.task
        assert record.status is TransferStatus.CANCELLED
        assert not (manager.cache_dir / "torrents" / record.id).exists()
        assert ledger.calls == []
        assert staging_usage(manager) == (0, 0)
    finally:
        await dispose(manager)


async def test_cancelled_parent_preserves_error_child_and_its_storage_claim(tmp_path):
    manager, _ = await manager_for(tmp_path)
    try:
        parent = await manager.registry.create(
            "torrent_metadata", context={"kind": "torrent"}
        )
        directory = manager.cache_dir / "torrents" / parent.id
        directory.mkdir(parents=True)
        staged = directory / "retained.bin"
        staged.write_bytes(b"keep")
        parent.context.update(staging_directory=str(directory), staging_size=4)
        child = await manager.registry.create(
            "cleanup_pending",
            context=dict(
                kind="upload",
                torrent_parent_id=parent.id,
                local_path=str(staged),
                send_started=True,
            ),
        )
        await manager.registry.update(child.id, status=TransferStatus.ERROR)
        await manager.registry.update(parent.id, status=TransferStatus.CANCELLED)
        manager._discard_torrent_stage(parent)
        assert staged.read_bytes() == b"keep"
        assert staging_usage(manager) == (4, 0)
        assert manager.registry._retained(parent)
    finally:
        await dispose(manager)


async def test_torrent_resume_counts_retained_allocation_during_zero_hash_progress(
    tmp_path, monkeypatch
):
    manager, ledger = await manager_for(tmp_path)
    manager.settings.upload_limit = 8192
    manager.settings.min_free_bytes = 100
    monkeypatch.setattr("tgdrive.transfers.torrent.available", lambda: True)
    observed = []

    async def resume(source, directory, *, progress, admit, **_):
        directory.mkdir(parents=True)
        payload = directory / "retained.bin"
        with payload.open("wb") as output:
            output.write(b"x" * 4096)
            output.truncate(8192)
        info = payload.stat()
        blocks = getattr(info, "st_blocks", None)
        allocated = min(8192, info.st_size if blocks is None else blocks * 512)
        monkeypatch.setattr(
            "tgdrive.transfers.staging.shutil.disk_usage",
            lambda _: SimpleNamespace(free=100 + 8192 - allocated),
        )
        manifest = TorrentManifest(
            "retained.bin", (TorrentFile("retained.bin", 8192),), 8192, False
        )
        parent = next(
            job
            for job in manager.registry.list()
            if job.context.get("kind") == "torrent"
        )
        assert parent.context.get("staging_written", 0) == 0
        await admit(manifest)
        assert parent.context["staging_written"] == allocated
        assert staging_usage(manager) == (8192, 8192 - allocated)
        for current in (0, 1024, min(8192, allocated + 1024)):
            await progress(current, 8192)
            assert parent.current == current
            assert parent.context["staging_written"] == max(allocated, current)
            assert staging_usage(manager) == (8192, 8192 - max(allocated, current))
            observed.append(current)
        raise TorrentError("synthetic stop after verifying resume admission")

    monkeypatch.setattr("tgdrive.transfers.torrent.download_torrent", resume)
    try:
        record = await manager.start_torrent(source=SOURCE, destination="/")
        await record.task
        assert record.status is TransferStatus.ERROR
        assert observed[:2] == [0, 1024]
        assert len(observed) == 3
        assert ledger.calls == []
    finally:
        await dispose(manager)


async def test_busy_torrent_slots_keep_next_queued_and_allow_local_uploads(
    tmp_path, monkeypatch
):
    manager, ledger = await manager_for(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    downloads = []
    monkeypatch.setattr("tgdrive.transfers.torrent.available", lambda: True)

    async def download(source, directory, *, progress, admit, **_):
        manifest = TorrentManifest(
            "torrent.bin", (TorrentFile("torrent.bin", 3),), 3, False
        )
        await admit(manifest)
        downloads.append(directory)
        if len(downloads) == 2:
            entered.set()
        await release.wait()
        directory.mkdir(parents=True)
        (directory / "torrent.bin").write_bytes(b"abc")
        await progress(3, 3)
        return manifest

    monkeypatch.setattr("tgdrive.transfers.torrent.download_torrent", download)
    try:
        torrents = [
            await manager.start_torrent(source=SOURCE, destination="/")
            for _ in range(3)
        ]
        await asyncio.wait_for(entered.wait(), 2)
        local = manager.cache_dir / "device.upload"
        local.write_bytes(b"abc")
        upload = await manager.enqueue_upload(local, "/", "device.bin", 3)
        await asyncio.wait_for(upload.task, 2)
        assert upload.status is TransferStatus.COMPLETED
        assert len(downloads) == 2
        assert torrents[2].status is TransferStatus.QUEUED
        assert torrents[2].phase == "torrent_metadata"
        release.set()
        await asyncio.gather(*(record.task for record in torrents))
        assert all(record.status is TransferStatus.COMPLETED for record in torrents)
        assert len(ledger.messages) == 4
    finally:
        release.set()
        await dispose(manager)


async def test_chosen_torrent_files_are_the_only_ones_reserved_and_imported(
    tmp_path, monkeypatch
):
    manifest = TorrentManifest(
        "Album",
        (
            TorrentFile("Album/a.txt", 3),
            TorrentFile("Album/Disc/b.bin", 4),
            TorrentFile("Album/Extra/c.bin", 5),
        ),
        12,
        True,
        "a" * 40,
    )
    selections = []

    async def download(source, directory, *, remote, cancel_event, progress, admit):
        selected = await admit(manifest)
        selections.append(selected)
        for index, file in enumerate(manifest.files):
            if selected is None or index in selected:
                target = directory / file.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x" * file.size)
        return manifest

    monkeypatch.setattr("tgdrive.transfers.torrent.available", lambda: True)
    monkeypatch.setattr("tgdrive.transfers.torrent.download_torrent", download)
    manager, ledger = await manager_for(tmp_path)
    try:
        record = await manager.start_torrent(
            source=SOURCE, destination="/", select_files=True
        )
        await record.task
        # Paused after metadata: nothing reserved, the choice is offered.
        assert record.phase == "torrent_select"
        assert record.context["staging_size"] == 0
        offered = record.as_dict()["torrent_choice"]
        assert [row["path"] for row in offered] == [f.path for f in manifest.files]
        assert not ledger.messages

        for invalid in ([], [0, 0], [3], [-1]):
            with pytest.raises(ValueError):
                await manager.select_torrent_files(record.id, invalid)

        await manager.select_torrent_files(record.id, [2, 0])
        await record.task
        assert record.status is TransferStatus.COMPLETED
        assert selections == [[0, 2]]
        assert record.total == 8
        assert record.as_dict()["torrent_files"] == {"total": 2, "completed": 2}
        tree = record.context["torrent_tree"]
        assert set(tree["folders"]) == {"", "Extra"}  # No empty "Disc" folder.
        # Only the two chosen files reach Telegram (plus any generated covers).
        stored = [
            row for row in manager.store.backend.db.nodes.rows if row["kind"] == "file"
        ]
        assert sorted(row["name"] for row in stored) == ["a.txt", "c.bin"]
        with pytest.raises(ValueError, match="not waiting"):
            await manager.select_torrent_files(record.id, [1])
    finally:
        await dispose(manager)


async def test_single_file_torrent_starts_without_waiting_for_a_choice(
    tmp_path, monkeypatch
):
    manifest = TorrentManifest(
        "song.bin", (TorrentFile("song.bin", 3),), 3, False, "a" * 40
    )
    torrent_source(monkeypatch, manifest=manifest)
    manager, ledger = await manager_for(tmp_path)
    try:
        record = await manager.start_torrent(
            source=SOURCE, destination="/", select_files=True
        )
        await record.task
        assert record.status is TransferStatus.COMPLETED
        assert len(ledger.messages) == 1
    finally:
        await dispose(manager)
