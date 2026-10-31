"""Bot-mode imports appear as website transfers while Telegram bytes arrive."""

import asyncio

import pytest

from tests.support.mongo_fixtures import register_test_backend
from tests.transfers.test_multipart_transfers import dispose, manager_for, stored
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus


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

    download.calls = calls
    return download


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
        await asyncio.wait_for(record.task, 2)
        assert record.status is TransferStatus.COMPLETED
        assert stored(manager)["size"] == 10
        assert not staged.exists()
    finally:
        gate.set()
        await dispose(manager)


async def test_replayed_message_reuses_the_running_receipt(tmp_path):
    manager, _ = await manager_for(tmp_path)
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
        await asyncio.wait_for(record.task, 2)
        assert record.status is (
            TransferStatus.ERROR if outcome == "error" else TransferStatus.CANCELLED
        )
        assert not staged.exists()
        assert manager.store.list_directory("/") == {}
    finally:
        gate.set()
        await dispose(manager)
