"""Crash and suspended-database boundaries, using synthetic storage only."""

import asyncio
import threading
from contextlib import AsyncExitStack
from io import BytesIO

import httpx2 as httpx
import pytest
from fastapi import UploadFile
from pymongo.errors import ConnectionFailure

from tests.support.app_fixtures import FakeRuntime, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tests.transfers.test_transfers import FakeRemote, make_manager
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.store import DriveStore
from tgdrive.transfers.manager import TransferManager
from tgdrive.transfers.remote_scheduler import URLDownloadScheduler
from tgdrive.transfers.staging import StorageUnavailable, staging_usage
from tgdrive.transfers.tasks import TransferNotFound, TransferRegistry, TransferStatus
from tgdrive.transfers.uploads import ResumableUploads
from tgdrive.web.app import create_app


def hold_write(monkeypatch, backend, predicate, *, fail=False):
    entered, release = threading.Event(), threading.Event()
    original = backend.write

    def write(collection, method, *args, **kwargs):
        if (
            collection is backend.db.jobs
            and method == "replace_one"
            and not entered.is_set()
            and predicate(args[1]["data"])
        ):
            entered.set()
            assert release.wait(5), "Synthetic database write was not released"
            if fail:
                raise ConnectionFailure("Synthetic journal rejection")
        return original(collection, method, *args, **kwargs)

    monkeypatch.setattr(backend, "write", write)
    return entered, release


@pytest.fixture
async def journal_app(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    store = DriveStore.create()
    runtime = FakeRuntime(store)
    backend = register_test_backend(monkeypatch, settings, runtime.mongo)
    registry = runtime.transfers.registry = TransferRegistry(
        mongo=backend.db.jobs, mongo_backend=backend
    )
    uploads = runtime.transfers.uploads = ResumableUploads(settings, runtime.transfers)
    app = create_app(settings, runtime=runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client,
    ):
        response = await client.post(
            "/api/checkPassword", json={"pass": "correct horse battery staple"}
        )
        assert response.status_code == 200
        yield client, registry, backend, uploads
    await registry.shutdown()
    registry.close()
    store.close()


@pytest.mark.parametrize("fail", [False, True])
async def test_pending_completion_is_not_public_and_disconnect_drains_commit(
    journal_app, monkeypatch, fail
):
    client, registry, backend, uploads = journal_app
    receipt = uploads.create("synthetic.bin", "/", 3)
    await uploads.append(receipt["id"], 0, b"one")
    job = await registry.create(
        "uploading", transfer_id=receipt["id"], total=3, context={"kind": "upload"}
    )
    await registry.update(job.id, status=TransferStatus.RUNNING)
    uploads._reconcile_finished()
    entered, release = hold_write(
        monkeypatch, backend, lambda row: row["status"] == "completed", fail=fail
    )
    finishing = asyncio.create_task(
        registry.update(job.id, status=TransferStatus.COMPLETED, phase="completed")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        finishing.cancel()
        await asyncio.sleep(0)
        finishing.cancel()
        await asyncio.sleep(0)
        assert not finishing.done()
        response = await asyncio.wait_for(client.get("/api/v1/jobs"), 0.5)
        assert response.status_code == 200
        assert response.json()["jobs"][0]["status"] == "running"
        assert response.json()["jobs"][0]["phase"] == "uploading"
        assert not job.terminal
        await asyncio.to_thread(uploads._reconcile_finished)
        assert uploads.get_unchecked(job.id)["state"] == "queued"
        assert uploads.path(job.id).exists()
        completed = await client.get("/api/v1/transfers/history?status=completed")
        assert completed.status_code == 200
        assert completed.json()["jobs"] == []
        running = await client.get("/api/v1/transfers/history?status=running")
        assert running.json()["jobs"][0]["status"] == "running"
    finally:
        release.set()
    with pytest.raises(DatabaseUnavailable if fail else asyncio.CancelledError):
        await finishing
    expected = "running" if fail else "completed"
    assert (await client.get(f"/api/v1/jobs/{job.id}")).json()["status"] == expected
    assert backend.db.jobs.find_one({"_id": job.id})["data"]["status"] == expected
    uploads._reconcile_finished()
    assert uploads.get_unchecked(job.id)["state"] == ("queued" if fail else "finished")
    assert uploads.path(job.id).exists() is fail


async def test_first_job_write_contains_context_and_is_hidden_until_ack(
    journal_app, monkeypatch
):
    client, registry, backend, _uploads = journal_app
    context = {"kind": "upload", "name": "synthetic.txt", "size": 3}
    entered, release = hold_write(monkeypatch, backend, lambda _row: True)
    creating = asyncio.create_task(
        registry.create("queued", transfer_id="initial-write", total=3, context=context)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        context["name"] = "caller-mutated.txt"
        assert (await client.get("/api/v1/jobs")).json()["jobs"] == []
        assert (await client.get("/api/v1/jobs/initial-write")).status_code == 404
        with pytest.raises(TransferNotFound):
            registry.get("initial-write")
    finally:
        release.set()
    job = await creating
    assert job.context["name"] == "synthetic.txt"
    assert backend.db.jobs.find_one({"_id": job.id})["data"]["context"] == job.context


@pytest.mark.parametrize("fail", [False, True])
async def test_cancel_flags_wait_for_ack_and_keep_parallel_part_references(
    journal_app, monkeypatch, fail
):
    _client, registry, backend, _uploads = journal_app
    job = await registry.create(
        "uploading",
        context={
            "kind": "upload",
            "send_started": True,
            "multipart_plan": {"parts": [{"confirmed": False}]},
        },
    )
    await registry.update(job.id, status=TransferStatus.RUNNING)
    part = job.context["multipart_plan"]["parts"][0]
    entered, release = hold_write(
        monkeypatch,
        backend,
        lambda row: row["context"].get("cancel_requested"),
        fail=fail,
    )
    cancelling = asyncio.create_task(registry.cancel(job.id))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not job.cancel_event.is_set()
        assert not job.context.get("cancel_requested")
        part["confirmed"] = True
        following = asyncio.create_task(registry.persist(job))
        await asyncio.sleep(0)
        assert not following.done()
    finally:
        release.set()
    if fail:
        with pytest.raises(DatabaseUnavailable):
            await cancelling
    else:
        await cancelling
    await following
    assert job.context["multipart_plan"]["parts"][0] is part
    assert part["confirmed"]
    assert job.cancel_event.is_set() is (not fail)
    assert bool(job.context.get("cancel_requested")) is (not fail)
    saved = backend.db.jobs.find_one({"_id": job.id})["data"]["context"]
    assert saved["multipart_plan"]["parts"][0]["confirmed"]
    assert bool(saved.get("cancel_requested")) is (not fail)


async def test_complete_receipt_recovers_if_process_stops_before_spawn(
    tmp_path, monkeypatch
):
    manager, store, _telegram, pool = await make_manager(tmp_path)
    backend = register_test_backend(monkeypatch, manager.settings)
    manager.registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    uploads = manager.uploads = ResumableUploads(manager.settings, manager)
    receipt = uploads.create("crash.bin", "/", 3)
    await uploads.append(receipt["id"], 0, b"one")

    def interrupted(*_args):
        raise RuntimeError("Synthetic process stopped before spawn")

    monkeypatch.setattr(manager.registry, "spawn", interrupted)
    recovered = None
    try:
        with pytest.raises(RuntimeError, match="before spawn"):
            await uploads.complete(receipt["id"])
        saved = backend.db.jobs.find_one({"_id": receipt["id"]})["data"]["context"]
        assert saved["kind"] == "upload"
        assert saved["name"] == "crash.bin"
        assert saved["size"] == 3
        assert saved["destination_id"]
        assert saved["local_path"] == str(uploads.path(receipt["id"]))
        recovered = TransferManager(
            settings=manager.settings,
            pool=pool,
            store=store,
            remote=FakeRemote(),
            registry=TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend),
        )
        recovered.uploads = ResumableUploads(manager.settings, recovered)
        await recovered.recover()
        job = recovered.registry.get(receipt["id"])
        await job.task
        assert job.status == TransferStatus.COMPLETED
        assert store.has_file_operation(job.id)
        assert len(store.list_directory("/")) == 1
        assert recovered.uploads.get_unchecked(job.id)["state"] == "finished"
        assert not uploads.path(job.id).exists()
    finally:
        if recovered is not None:
            await recovered.close()
        await manager.close()
        await pool.stop()
        store.close()


async def test_pending_first_browser_job_still_reserves_staging(tmp_path, monkeypatch):
    manager, store, _telegram, pool = await make_manager(tmp_path)
    manager.settings.staging_budget_bytes = 5
    manager.settings.min_free_bytes = 0
    backend = register_test_backend(monkeypatch, manager.settings)
    manager.registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    entered, release = hold_write(
        monkeypatch, backend, lambda row: row["phase"] == "saving"
    )
    first = asyncio.create_task(
        manager.receive_browser_upload(
            UploadFile(BytesIO(b"one"), size=3, filename="one.bin"), "/"
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert manager.registry.list() == []
        assert staging_usage(manager) == (3, 3)
        second = UploadFile(BytesIO(b"two"), size=3, filename="two.bin")
        with pytest.raises(StorageUnavailable, match="reserved"):
            await manager.receive_browser_upload(second, "/")
        assert second.file.closed
    finally:
        release.set()
    try:
        job = await first
        await job.task
        assert job.status == TransferStatus.COMPLETED
        assert manager._pending_staging == {}
    finally:
        await manager.close()
        await pool.stop()
        store.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_scheduler_releases_ticket_if_awaited_admission_notify_stops(cancel):
    scheduler = URLDownloadScheduler(1)
    entered = asyncio.Event()

    async def notify(mode):
        if mode == "warm":
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("Synthetic notification failure")

    async def wait(operation):
        return await operation

    async def use():
        async with scheduler.admit(wait, changed=notify):
            pytest.fail("Failed admission must not yield a ticket")

    admission = asyncio.create_task(use())
    await entered.wait()
    if cancel:
        admission.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await admission
    assert scheduler._tickets == []
    assert scheduler._monitor is None


async def test_concurrent_scheduler_refresh_cannot_exceed_slots_during_notify():
    scheduler = URLDownloadScheduler(1)
    ready = False
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(operation):
        return await operation

    async def notify(mode):
        if mode == "full":
            entered.set()
            await release.wait()

    async with AsyncExitStack() as stack:
        tickets = [
            await stack.enter_async_context(
                scheduler.admit(wait, lambda: ready, notify)
            )
            for _ in range(3)
        ]
        ready = True
        first = asyncio.create_task(scheduler.refresh())
        await entered.wait()
        second = asyncio.create_task(scheduler.refresh())
        try:
            await asyncio.sleep(0)
            assert sum(ticket.full_speed for ticket in tickets) == 1
            assert not second.done()
        finally:
            release.set()
        await asyncio.gather(first, second)
        assert sum(ticket.full_speed for ticket in tickets) == 1
