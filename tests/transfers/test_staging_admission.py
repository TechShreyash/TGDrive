from __future__ import annotations

import asyncio
import errno
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.support.app_fixtures import make_settings
from tests.support.mongo_fixtures import register_test_backend
from tests.transfers.test_resumable_uploads import make
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.settings.performance_preferences import PerformanceSettings
from tgdrive.transfers.staging import staging_usage
from tgdrive.transfers.tasks import TransferStatus
from tgdrive.transfers.uploads import UploadError


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path / "data")


async def test_upload_reservations_prevent_overbooking_and_release_after_cancel(
    tmp_path, monkeypatch
):
    uploads, _manager = make(
        tmp_path, min_free_bytes=20, upload_limit=100, staging_budget_bytes=1000
    )
    monkeypatch.setattr(
        "tgdrive.transfers.staging.shutil.disk_usage",
        lambda path: SimpleNamespace(free=150),
    )

    async def admit(name):
        return uploads.create(name, "/", 80)

    results = await asyncio.gather(admit("a"), admit("b"), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    failure = next(item for item in results if isinstance(item, UploadError))
    assert failure.status == 507 and "free disk space" in str(failure)
    item = next(item for item in results if isinstance(item, dict))
    assert uploads.path(item["id"]).stat().st_size == 0
    await uploads.cancel(item["id"])
    assert uploads.create("c", "/", 80)["offset"] == 0


async def test_browser_admission_includes_url_reservations_and_retained_bytes(
    tmp_path, monkeypatch
):
    uploads, manager = make(
        tmp_path, upload_limit=1000, staging_budget_bytes=1000, min_free_bytes=10
    )
    manager.uploads = uploads
    job = await manager.registry.create("downloading", total=100)
    job.context.update(staging_size=100, staging_written=40)
    (await manager.registry.update(job.id, status=TransferStatus.RUNNING))
    monkeypatch.setattr(
        "tgdrive.transfers.staging.shutil.disk_usage",
        lambda path: SimpleNamespace(free=150),
    )
    with pytest.raises(UploadError) as error:
        uploads.create("b", "/", 90)
    assert error.value.status == 507
    assert staging_usage(manager) == (100, 60)
    # Terminal failure releases future claims; retained bytes still count.
    path = tmp_path / "retained"
    path.write_bytes(b"x" * 40)
    job.context["local_path"] = str(path)
    (await manager.registry.update(job.id, status=TransferStatus.ERROR))
    assert staging_usage(manager) == (40, 0)


def test_large_declared_upload_can_exceed_16_gib_without_allocating_file(
    tmp_path, monkeypatch
):
    size = 20 * 1024**3
    uploads, _ = make(
        tmp_path,
        upload_limit=32 * 1024**3,
        staging_budget_bytes=64 * 1024**3,
        min_free_bytes=1024**3,
    )
    seen = []

    def disk(path):
        seen.append(path)
        return SimpleNamespace(free=100 * 1024**3)

    monkeypatch.setattr("tgdrive.transfers.staging.shutil.disk_usage", disk)
    item = uploads.create("large.bin", "/", size)
    assert item["total_size"] == size
    assert uploads.path(item["id"]).stat().st_size == 0
    assert seen == [uploads.settings.resolved_cache_dir]


async def test_restart_keeps_future_bytes_reserved_and_does_not_double_count_job(
    tmp_path, monkeypatch
):
    uploads, manager = make(
        tmp_path, staging_budget_bytes=200, upload_limit=100, min_free_bytes=10
    )
    item = uploads.create("a", "/", 80)
    uploads.close()
    resumed, _ = make(
        tmp_path,
        manager=manager,
        staging_budget_bytes=200,
        upload_limit=100,
        min_free_bytes=10,
    )
    manager.uploads = resumed
    assert staging_usage(manager) == (80, 80)
    monkeypatch.setattr(
        "tgdrive.transfers.staging.shutil.disk_usage",
        lambda path: SimpleNamespace(free=150),
    )
    with pytest.raises(UploadError):
        resumed.create("b", "/", 80)
    await resumed.append(item["id"], 0, b"x" * 80)
    await resumed.complete(item["id"])
    assert staging_usage(manager) == (80, 0)


async def test_disk_quota_error_keeps_receipt_acknowledgement_for_resume(
    tmp_path, monkeypatch
):
    uploads, _ = make(tmp_path)
    item = uploads.create("a", "/", 3)
    original = uploads.path(item["id"]).open

    class FullFile:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def truncate(self, offset):
            pass

        def seek(self, offset):
            pass

        def write(self, body):
            raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(
        type(uploads.root),
        "open",
        lambda self, *args, **kwargs: (
            FullFile() if self.name.endswith(".part") else original(*args, **kwargs)
        ),
    )
    with pytest.raises(UploadError) as error:
        await uploads.append(item["id"], 0, b"abc")
    assert error.value.status == 507
    assert uploads.get(item["id"])["offset"] == 0


async def test_owner_limits_persist_and_reject_invalid_combinations(
    tmp_path, mongo_backend
):
    settings = make_settings(tmp_path)
    settings.max_file_size = None
    settings.staging_budget_bytes = 24 * 1024**3
    state = SimpleNamespace(
        mongo=mongo_backend,
        pool=SimpleNamespace(
            _slots=[], set_media_per_bot=AsyncMock(), set_media_queue_limit=AsyncMock()
        ),
    )
    service = PerformanceSettings(settings, state)
    assert (await service.snapshot())["max_file_size"] == 24 * 1024**3
    for changes in [
        {"max_file_size": 32 * 1024**3},
        {"min_free_bytes": 0},
        {"staging_budget_bytes": True},
    ]:
        with pytest.raises((SettingsProblem, ValueError)):
            await service.save(changes)
    after = await service.save(
        {
            "max_file_size": 32 * 1024**3,
            "staging_budget_bytes": 64 * 1024**3,
            "min_free_bytes": 1024**3,
        }
    )
    assert settings.upload_limit == 32 * 1024**3
    second = PerformanceSettings(settings, state)
    assert (await second.snapshot())["staging_budget_bytes"] == after[
        "staging_budget_bytes"
    ]
    await state.preloads.close()


def test_unavailable_disk_fails_before_creating_receipt(tmp_path, monkeypatch):
    uploads, _ = make(tmp_path)

    def missing(path):
        raise OSError("drive offline")

    monkeypatch.setattr("tgdrive.transfers.staging.shutil.disk_usage", missing)
    with pytest.raises(UploadError) as error:
        uploads.create("a", "/", 3)
    assert error.value.status == 503
    assert not list(uploads.root.glob("*.part"))


@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("reserved", [True, False])
async def test_url_import_checks_shared_space_before_reading_body(
    tmp_path, monkeypatch, known, reserved
):
    from tests.transfers.test_remote import (
        PUBLIC_DNS,
        FakeResolver,
        FakeResponse,
        FakeSession,
    )
    from tests.transfers.test_transfers import make_manager
    from tgdrive.transfers.remote import RemoteClient

    manager, store, _, pool = await make_manager(tmp_path, max_size=100)
    manager.settings.min_free_bytes = 10
    response = FakeResponse(
        200, headers={"Content-Length": "80"} if known else {}, chunks=[b"x" * 80]
    )
    manager.remote = RemoteClient(
        max_bytes=100,
        session=FakeSession([response]),
        resolver=FakeResolver(PUBLIC_DNS),
    )
    monkeypatch.setattr(
        "tgdrive.transfers.staging.shutil.disk_usage",
        lambda path: SimpleNamespace(free=50),
    )
    record = await manager.start_remote(
        url="https://files.example/a", destination="/", filename=None
    )
    if reserved:
        # A restarted import must recheck admission even if its old reservation
        # has the same size as this source response.
        record.context.update(staging_size=80 if known else 100, staging_written=40)
        (await manager.registry.persist(record))
    import asyncio

    waiting = asyncio.Event()
    original_cancelable = manager.remote._cancelable

    async def observe_wait(operation, event, **kwargs):
        if record.context.get("download_mode") == "waiting_storage":
            waiting.set()
        return await original_cancelable(operation, event, **kwargs)

    monkeypatch.setattr(manager.remote, "_cancelable", observe_wait)
    await asyncio.wait_for(waiting.wait(), 3)
    assert not record.task.done()
    assert list(response.content.chunks) == [b"x" * 80]
    record.cancel_event.set()
    await record.task
    assert record.status == TransferStatus.CANCELLED
    assert not list((tmp_path / "cache" / "remote" / record.id).glob("*.part"))
    await manager.remote.close()
    await pool.stop()
    store.close()


async def test_owner_cannot_shrink_budget_below_reserved_receipts(
    tmp_path, mongo_backend
):
    settings = make_settings(tmp_path)
    uploads, manager = make(tmp_path)
    manager.uploads = uploads
    uploads.settings.upload_limit = 32 * 1024**3
    uploads.settings.staging_budget_bytes = 64 * 1024**3
    # Metadata-only reservation; no large allocation is required.
    item = uploads.create("held.bin", "/", 3)
    item["total_size"] = 24 * 1024**3
    uploads._save(item)
    state = SimpleNamespace(
        transfers=manager, pool=SimpleNamespace(_slots=[]), mongo=mongo_backend
    )
    service = PerformanceSettings(settings, state)
    before = await service.snapshot()
    with pytest.raises(SettingsProblem) as error:
        await service.save(
            {"staging_budget_bytes": 16 * 1024**3, "max_file_size": 16 * 1024**3}
        )
    assert error.value.status == 409
    assert (await service.snapshot())["revision"] == before["revision"]
    await state.preloads.close()


@pytest.mark.parametrize("known", [True, False])
async def test_url_reserves_once_and_reports_live_written_bytes(
    tmp_path, monkeypatch, known, mongo_backend
):
    from tests.transfers.test_remote import (
        PUBLIC_DNS,
        FakeResolver,
        FakeResponse,
        FakeSession,
    )
    from tests.transfers.test_transfers import make_manager
    from tgdrive.transfers.remote import RemoteClient
    from tgdrive.transfers.staging import check_staging
    from tgdrive.transfers.tasks import TransferRegistry

    manager, store, _, pool = await make_manager(tmp_path, max_size=100)
    manager.registry = TransferRegistry(
        mongo=mongo_backend.db.jobs, mongo_backend=mongo_backend
    )
    manager.settings.min_free_bytes = 0
    response = FakeResponse(
        200,
        headers={"Content-Length": "64"} if known else {},
        chunks=[b"x"] * 64,
    )
    manager.remote = RemoteClient(
        max_bytes=100,
        session=FakeSession([response]),
        resolver=FakeResolver(PUBLIC_DNS),
    )
    admissions = []
    written = []

    def admit(*args, **kwargs):
        admissions.append(args[2])
        return check_staging(*args, **kwargs)

    monkeypatch.setattr("tgdrive.transfers.manager.check_staging", admit)
    original_progress = manager.remote._report_progress

    async def observe(callback, current, total):
        await original_progress(callback, current, total)
        written.append(current)
        expected = 64 if known else 100
        assert staging_usage(manager) == (expected, expected - current)
        assert manager.registry._durable[record.id]["current"] == 0
        assert manager.registry._durable[record.id]["context"]["staging_written"] == 0

    monkeypatch.setattr(manager.remote, "_report_progress", observe)
    record = await manager.start_remote(
        url="https://files.example/a", destination="/", filename=None
    )
    try:
        await record.task
        assert record.status == TransferStatus.COMPLETED, record.error
        assert admissions == [64 if known else 100]
        assert written == list(range(65))
    finally:
        await manager.close()
        await pool.stop()
        store.close()


async def test_waiting_url_reserves_a_window_instead_of_the_whole_file(
    tmp_path, monkeypatch
):
    import asyncio

    from tests.transfers.test_remote import (
        PUBLIC_DNS,
        FakeResolver,
        FakeResponse,
        FakeSession,
    )
    from tests.transfers.test_transfers import make_manager
    from tgdrive.transfers.remote import RemoteClient

    manager, store, _, pool = await make_manager(tmp_path, max_size=100)
    manager.settings.staging_budget_bytes = 64
    manager.settings.min_free_bytes = 0
    monkeypatch.setattr("tgdrive.transfers.remote_scheduler.WARM_STAGING_WINDOW", 8)
    first_read, warm_read = asyncio.Event(), asyncio.Event()
    first = FakeResponse(200, headers={"Content-Length": "48"}, chunks=[b"x" * 48])
    second = FakeResponse(200, headers={"Content-Length": "48"}, chunks=[b"y" * 48])

    async def held_read(_size):
        first_read.set()
        await asyncio.Event().wait()

    original_read = second.content.read

    async def observe_read(size):
        warm_read.set()
        return await original_read(size)

    first.content.read = held_read
    second.content.read = observe_read
    manager.remote = RemoteClient(
        max_bytes=100,
        chunk_size=4,
        download_concurrency=1,
        session=FakeSession([first, second]),
        resolver=FakeResolver(PUBLIC_DNS),
    )
    records = []
    try:
        records.append(
            await manager.start_remote(
                url="https://files.example/first", destination="/", filename=None
            )
        )
        await asyncio.wait_for(first_read.wait(), 3)
        records.append(
            await manager.start_remote(
                url="https://files.example/second", destination="/", filename=None
            )
        )
        await asyncio.wait_for(warm_read.wait(), 3)
        assert records[0].context["staging_size"] == 48
        assert records[1].context["staging_size"] == 8
        assert records[1].context["download_mode"] == "warm"
        assert staging_usage(manager)[0] == 56
    finally:
        for record in records:
            record.cancel_event.set()
        await asyncio.gather(*(record.task for record in records))
        await manager.close()
        await pool.stop()
        store.close()
