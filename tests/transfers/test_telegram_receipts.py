"""Bot-mode imports appear as website transfers while Telegram bytes arrive."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.support.mongo_fixtures import register_test_backend
from tests.transfers.test_multipart_transfers import dispose, manager_for, stored
from tgdrive.transfers.staging import StorageUnavailable, staging_usage
from tgdrive.transfers.tasks import TransferRecord, TransferRegistry, TransferStatus


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path)


def fake_download(content=b"abcdefghij", *, gate=None, fail=None):
    calls = []

    async def download(target, progress):
        calls.append(target)
        with open(target, "wb") as output:  # noqa: ASYNC230
            output.write(content[:4])
            await progress(4, len(content))
            if gate is not None:
                await gate.wait()
            if fail is not None:
                raise fail
            output.write(content[4:])
        await progress(len(content), len(content))

    return AsyncMock(side_effect=download, calls=calls)


async def test_receipt_is_a_visible_job_then_uploads_and_removes_staged_bytes(
    tmp_path,
):
    manager, _ = await manager_for(tmp_path)
    gate = asyncio.Event()
    download = fake_download(gate=gate)
    staged = manager.cache_dir / "bot.part"
    manager.cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        record = await manager.receive_telegram_file(
            download, staged, "/", "ten.bin", 10, transfer_id="t" * 32
        )
        for _ in range(50):
            if record.current == 4:
                break
            await asyncio.sleep(0.01)
        assert record.phase == "downloading" and record.current == 4
        public = TransferRegistry.history_snapshot(record)
        assert public["kind"] == "telegram" and public["name"] == "ten.bin"
        gate.set()
        assert record.task is not None
        await asyncio.wait_for(record.task, 2)
        assert record.status is TransferStatus.COMPLETED
        assert stored(manager)["size"] == 10
        assert not staged.exists()
    finally:
        gate.set()
        await dispose(manager)


async def test_replayed_message_reuses_the_running_receipt(tmp_path):
    manager, _ = await manager_for(tmp_path)
    manager.settings.staging_budget_bytes = 10
    gate = asyncio.Event()
    download = fake_download(gate=gate)
    staged = manager.cache_dir / "bot.part"
    manager.cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        first, second = await asyncio.gather(
            *(
                manager.receive_telegram_file(
                    download, staged, "/", "ten.bin", 10, transfer_id="r" * 32
                )
                for _ in range(2)
            )
        )
        assert first is second
        gate.set()
        assert first.task is not None
        await asyncio.wait_for(first.task, 2)
        again = await manager.receive_telegram_file(
            download, staged, "/", "ten.bin", 10, transfer_id="r" * 32
        )
        assert again is first and len(download.calls) == 1
        assert len(manager.store.list_directory("/")) == 1
    finally:
        gate.set()
        await dispose(manager)


@pytest.mark.parametrize("outcome", ["error", "cancel"])
async def test_failed_or_cancelled_receipt_removes_partial_bytes(tmp_path, outcome):
    manager, _ = await manager_for(tmp_path)
    gate = asyncio.Event()
    download = fake_download(
        gate=gate, fail=ConnectionError("lost") if outcome == "error" else None
    )
    staged = manager.cache_dir / "bot.part"
    manager.cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        record = await manager.receive_telegram_file(
            download, staged, "/", "ten.bin", 10, transfer_id="f" * 32
        )
        for _ in range(50):
            if staged.exists():
                break
            await asyncio.sleep(0.01)
        if outcome == "cancel":
            await manager.cancel_job(record.id)
        gate.set()
        assert record.task is not None
        await asyncio.wait_for(record.task, 2)
        assert record.status is (
            TransferStatus.ERROR if outcome == "error" else TransferStatus.CANCELLED
        )
        assert not staged.exists()
        assert manager.store.list_directory("/") == {}
    finally:
        gate.set()
        await dispose(manager)


async def test_distinct_bot_receipts_cannot_overbook_while_journal_is_busy(tmp_path):
    manager, _ = await manager_for(tmp_path)
    manager.settings.staging_budget_bytes = 100
    gate = asyncio.Event()
    download = fake_download(b"x" * 80, gate=gate)
    admissions = []
    try:
        async with manager.registry._journal_lock:
            admissions = [
                asyncio.create_task(
                    manager.receive_telegram_file(
                        download,
                        manager.cache_dir / f"{index}.part",
                        "/",
                        f"{index}.bin",
                        80,
                        transfer_id=str(index) * 32,
                    )
                )
                for index in (1, 2)
            ]
            await asyncio.sleep(0)
        outcomes = await asyncio.gather(*admissions, return_exceptions=True)
        assert sum(isinstance(item, TransferRecord) for item in outcomes) == 1
        failures = [item for item in outcomes if isinstance(item, StorageUnavailable)]
        assert len(failures) == 1
        assert failures[0].status == 507
        assert staging_usage(manager)[0] == 80
    finally:
        await asyncio.gather(*admissions, return_exceptions=True)
        await dispose(manager)


async def test_failed_bot_receipt_persistence_releases_pending_claim(
    tmp_path, monkeypatch
):
    manager, _ = await manager_for(tmp_path)
    manager.settings.staging_budget_bytes = 100
    gate = asyncio.Event()
    download = fake_download(b"x" * 80, gate=gate)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                manager.registry,
                "_persist",
                AsyncMock(side_effect=OSError("isolated journal failure")),
            )
            with pytest.raises(OSError, match="isolated journal failure"):
                await manager.receive_telegram_file(
                    download,
                    manager.cache_dir / "failed.part",
                    "/",
                    "failed.bin",
                    80,
                    transfer_id="f" * 32,
                )
        assert staging_usage(manager) == (0, 0)
        record = await manager.receive_telegram_file(
            download,
            manager.cache_dir / "next.part",
            "/",
            "next.bin",
            80,
            transfer_id="n" * 32,
        )
        assert record.id == "n" * 32
        assert staging_usage(manager)[0] == 80
    finally:
        await dispose(manager)


async def test_cancelled_bot_admission_retains_durable_claim_and_replay(
    tmp_path, monkeypatch
):
    manager, _ = await manager_for(tmp_path)
    manager.settings.staging_budget_bytes = 100
    gate = asyncio.Event()
    download = fake_download(b"x" * 80, gate=gate)
    admission = None
    journal_entered = asyncio.Event()
    create = manager.registry.create

    async def journal_create(*args, **kwargs):
        journal_entered.set()
        return await create(*args, **kwargs)

    monkeypatch.setattr(manager.registry, "create", journal_create)
    try:
        async with manager.registry._journal_lock:
            admission = asyncio.create_task(
                manager.receive_telegram_file(
                    download,
                    manager.cache_dir / "cancelled.part",
                    "/",
                    "cancelled.bin",
                    80,
                    transfer_id="c" * 32,
                )
            )
            await asyncio.wait_for(journal_entered.wait(), 1)
            admission.cancel()
            await asyncio.sleep(0)
            assert not admission.done()
            with pytest.raises(StorageUnavailable):
                await asyncio.wait_for(
                    manager.receive_telegram_file(
                        download,
                        manager.cache_dir / "other.part",
                        "/",
                        "other.bin",
                        80,
                        transfer_id="o" * 32,
                    ),
                    1,
                )
        with pytest.raises(asyncio.CancelledError):
            await admission
        assert staging_usage(manager)[0] == 80
        replay = await manager.receive_telegram_file(
            download,
            manager.cache_dir / "cancelled.part",
            "/",
            "cancelled.bin",
            80,
            transfer_id="c" * 32,
        )
        assert replay.id == "c" * 32
        assert staging_usage(manager)[0] == 80
    finally:
        if admission is not None:
            await asyncio.gather(admission, return_exceptions=True)
        await dispose(manager)
