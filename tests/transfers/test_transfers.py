from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from tgdrive.core.config import Settings
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.manager import (
    TransferCancelled,
    TransferEmptyFile,
    TransferManager,
    TransferTooLarge,
)
from tgdrive.transfers.remote import DownloadResult
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus


class StopTransmission(Exception):
    pass


class FakeTelegramClient:
    def __init__(self) -> None:
        self.name = "transfer-test"
        self.is_connected = False
        self.deleted: list[int] = []
        self.started_upload = asyncio.Event()
        self.release_upload = asyncio.Event()
        self.wait_during_upload = False
        self.wait_after_progress = False
        self.fail_upload = False
        self.progress_reported = asyncio.Event()
        self.release_after_progress = asyncio.Event()

    async def start(self) -> None:
        self.is_connected = True

    async def stop(self) -> None:
        self.is_connected = False

    def stop_transmission(self) -> None:
        raise StopTransmission

    async def send_staged(
        self,
        _channel: int,
        path: Path,
        *,
        file_name: str,
        progress: object,
        offset: int = 0,
        size: int | None = None,
        expected_sha256: str | None = None,
        before_dispatch=None,
        **_kwargs: object,
    ):
        if self.fail_upload:
            raise RuntimeError("Telegram unavailable")
        end = offset + size if size is not None else None
        content = path.read_bytes()[offset:end]
        if expected_sha256 is not None:
            assert hashlib.sha256(content).hexdigest() == expected_sha256
        self.started_upload.set()
        if self.wait_during_upload:
            await self.release_upload.wait()
        try:
            await progress(len(content), len(content))
        except StopTransmission:
            return None
        self.progress_reported.set()
        if self.wait_after_progress:
            await self.release_after_progress.wait()
        if before_dispatch is not None:
            (await before_dispatch())
        return SimpleNamespace(
            id=99,
            document=SimpleNamespace(file_size=len(content), file_name=file_name),
        )

    async def delete_messages(self, _channel: int, ids: int | list[int]) -> None:
        self.deleted.extend(ids if isinstance(ids, list) else [ids])


class FakeRemote:
    async def inspect(self, url: str):
        return SimpleNamespace(
            url=url,
            filename="remote.bin",
            size=6,
            content_type="application/octet-stream",
        )

    async def download(
        self,
        url: str,
        output_dir: Path,
        *,
        filename: str | None,
        cancel_event: asyncio.Event,
        progress: object,
        **_kwargs: object,
    ) -> DownloadResult:
        assert not cancel_event.is_set()
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / (filename or "remote.bin")
        path.write_bytes(b"remote")
        await progress(6, 6)
        return DownloadResult(
            path=path,
            url=url,
            filename=path.name,
            size=6,
            content_type="application/octet-stream",
        )

    async def close(self) -> None:
        return None


def make_settings(tmp_path: Path, *, max_size: int = 1024) -> Settings:
    return Settings(
        _env_file=None,
        MONGODB_URI="mongodb://synthetic.invalid",
        API_ID=12345,
        API_HASH="a" * 32,
        BOT_TOKENS="123456:" + "x" * 30,
        STORAGE_CHANNEL=-1001234567890,
        ADMIN_PASSWORD="correct horse battery staple",
        SESSION_SECRET="s" * 48,
        DATA_DIR=tmp_path / "data",
        CACHE_DIR=tmp_path / "cache",
        MAX_FILE_SIZE=max_size,
        COOKIE_SECURE=False,
    )


async def make_manager(tmp_path: Path, *, max_size: int = 1024):
    settings = make_settings(tmp_path, max_size=max_size)
    client = FakeTelegramClient()
    pool = ClientPool([client])
    await pool.start()
    store = DriveStore.create(tmp_path / "drive.json")
    manager = TransferManager(
        settings=settings,
        pool=pool,
        store=store,
        remote=FakeRemote(),
        registry=TransferRegistry(),
    )
    return manager, store, client, pool


@pytest.mark.asyncio
async def test_browser_upload_completes_and_cleans_temp_file(tmp_path: Path) -> None:
    manager, store, _, pool = await make_manager(tmp_path)
    upload = UploadFile(BytesIO(b"hello"), size=5, filename="../hello + 100%.txt")

    record = await manager.receive_browser_upload(upload, "/")
    assert record.task is not None
    await record.task

    assert record.status is TransferStatus.COMPLETED
    item = next(iter(store.list_directory("/").values()))
    assert item["name"] == "hello + 100%.txt"
    assert not (manager.cache_dir / "uploads" / f"{record.id}.upload").exists()
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_upload_cancellation_does_not_create_metadata(tmp_path: Path) -> None:
    manager, store, client, pool = await make_manager(tmp_path)
    client.wait_during_upload = True
    upload = UploadFile(BytesIO(b"hello"), size=5, filename="hello.txt")

    record = await manager.receive_browser_upload(upload, "/")
    await client.started_upload.wait()
    (await manager.registry.cancel(record.id))
    assert record.status is TransferStatus.CANCELLING
    client.release_upload.set()
    assert record.task is not None
    await record.task

    assert record.status is TransferStatus.CANCELLED
    assert store.list_directory("/") == {}
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_complete_byte_count_enters_finalizing_phase(tmp_path: Path) -> None:
    manager, store, client, pool = await make_manager(tmp_path)
    client.wait_after_progress = True
    upload = UploadFile(BytesIO(b"hello"), size=5, filename="hello.txt")

    record = await manager.receive_browser_upload(upload, "/")
    await client.progress_reported.wait()

    assert record.status is TransferStatus.RUNNING
    assert record.phase == "finalizing"
    assert record.current == record.total == 5

    client.release_after_progress.set()
    assert record.task is not None
    await record.task
    assert record.status is TransferStatus.COMPLETED
    assert len(store.list_directory("/")) == 1
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_interrupted_browser_request_cancels_and_cleans_partial_file(
    tmp_path: Path,
) -> None:
    manager, store, _, pool = await make_manager(tmp_path)
    read_started = asyncio.Event()
    never_finish = asyncio.Event()

    class InterruptedUpload:
        filename = "partial.bin"
        size = 5

        def __init__(self) -> None:
            self.closed = False

        async def read(self, _size: int) -> bytes:
            read_started.set()
            await never_finish.wait()
            return b""

        async def close(self) -> None:
            self.closed = True

    upload = InterruptedUpload()
    request_task = asyncio.create_task(manager.receive_browser_upload(upload, "/"))
    await read_started.wait()
    record = next(iter(manager.registry._records.values()))
    request_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert upload.closed
    assert record.status is TransferStatus.CANCELLED
    assert record.cancel_event.is_set()
    assert store.list_directory("/") == {}
    assert not (manager.cache_dir / "uploads" / f"{record.id}.upload").exists()
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_disconnected_browser_does_not_spawn_unknown_telegram_transfer(
    tmp_path: Path,
) -> None:
    manager, store, client, pool = await make_manager(tmp_path)
    upload = UploadFile(BytesIO(b"hello"), size=5, filename="hello.txt")

    async def disconnected() -> bool:
        return True

    with pytest.raises(TransferCancelled):
        await manager.receive_browser_upload(
            upload,
            "/",
            is_disconnected=disconnected,
        )

    record = next(iter(manager.registry._records.values()))
    assert record.status is TransferStatus.CANCELLED
    assert record.task is None
    assert not client.started_upload.is_set()
    assert store.list_directory("/") == {}
    assert not (manager.cache_dir / "uploads" / f"{record.id}.upload").exists()
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_duplicate_upload_keeps_both_without_overwriting(tmp_path: Path) -> None:
    manager, store, client, pool = await make_manager(tmp_path)
    store.add_file("/", "duplicate.txt", 1, 1)
    upload = UploadFile(BytesIO(b"new"), size=3, filename="duplicate.txt")

    record = await manager.receive_browser_upload(upload, "/")
    assert record.task is not None
    await record.task

    assert record.status is TransferStatus.COMPLETED
    assert client.deleted == []
    assert len(store.list_directory("/")) == 2
    assert len({item["name"] for item in store.list_directory("/").values()}) == 2
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_oversized_upload_is_rejected_before_disk_write(tmp_path: Path) -> None:
    manager, store, _, pool = await make_manager(tmp_path, max_size=3)
    upload = UploadFile(BytesIO(b"four"), size=4, filename="large.bin")

    with pytest.raises(TransferTooLarge):
        await manager.receive_browser_upload(upload, "/")

    assert store.list_directory("/") == {}
    assert not (manager.cache_dir / "uploads").exists()
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_remote_pipeline_downloads_then_uploads(tmp_path: Path) -> None:
    manager, store, _, pool = await make_manager(tmp_path)

    info = await manager.inspect_remote("https://example.com/remote.bin")
    assert info.filename == "remote.bin"
    record = await manager.start_remote(
        url="https://example.com/remote.bin",
        destination="/",
        filename="chosen.bin",
    )
    assert record.task is not None
    await record.task

    assert record.status is TransferStatus.COMPLETED
    item = next(iter(store.list_directory("/").values()))
    assert item["name"] == "chosen.bin"
    assert not (manager.cache_dir / "remote" / record.id).exists()
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_telegram_error_retains_staging_for_retry(
    tmp_path: Path,
) -> None:
    manager, store, client, pool = await make_manager(tmp_path)
    client.fail_upload = True
    upload = UploadFile(BytesIO(b"hello"), size=5, filename="hello.txt")

    record = await manager.receive_browser_upload(upload, "/")
    assert record.task is not None
    await record.task

    assert record.status is TransferStatus.ERROR
    assert "Telegram unavailable" in (record.error or "")
    assert store.list_directory("/") == {}
    assert (manager.cache_dir / "uploads" / f"{record.id}.upload").exists()
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_delete_messages_is_chunked(tmp_path: Path) -> None:
    manager, _, client, pool = await make_manager(tmp_path)
    message_ids = list(range(1, 206))

    await manager.delete_telegram_messages(message_ids)

    assert client.deleted == message_ids
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_size", [0, None])
async def test_empty_file_is_rejected_and_closed(
    tmp_path: Path, declared_size: int | None
) -> None:
    manager, store, client, pool = await make_manager(tmp_path)
    upload = UploadFile(BytesIO(), size=declared_size, filename="empty.txt")

    with pytest.raises(TransferEmptyFile, match="Empty files"):
        await manager.receive_browser_upload(upload, "/")

    assert upload.file.closed
    assert not client.started_upload.is_set()
    assert store.list_directory("/") == {}
    assert not list(manager.cache_dir.rglob("*.upload"))
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_creation_callback_can_cancel_before_saving(tmp_path: Path) -> None:
    manager, _, client, pool = await make_manager(tmp_path)
    upload = UploadFile(BytesIO(b"hello"), size=5, filename="hello.txt")
    created: list[str] = []

    async def cancel_immediately(record: object) -> None:
        created.append(record.id)
        (await manager.registry.cancel(record.id))

    with pytest.raises(TransferCancelled):
        await manager.receive_browser_upload(upload, "/", on_created=cancel_immediately)

    record = manager.registry.get(created[0])
    assert record.status is TransferStatus.CANCELLED
    assert not client.started_upload.is_set()
    assert upload.file.closed
    assert not list(manager.cache_dir.rglob("*.upload"))
    await manager.close()
    await pool.stop()


@pytest.mark.asyncio
async def test_staging_setup_failure_closes_upload_and_marks_bound_record(
    tmp_path: Path,
) -> None:
    manager, _, client, pool = await make_manager(tmp_path)
    # A file occupying the expected staging-directory path is a realistic
    # permission/filesystem setup failure before the first multipart read.
    (manager.cache_dir / "uploads").write_bytes(b"occupied")
    upload = UploadFile(BytesIO(b"hello"), size=5, filename="hello.txt")
    bound: list[object] = []

    with pytest.raises(FileExistsError):
        await manager.receive_browser_upload(upload, "/", on_created=bound.append)

    assert len(bound) == 1
    assert bound[0].status is TransferStatus.ERROR
    assert upload.file.closed
    assert not client.started_upload.is_set()
    await manager.close()
    await pool.stop()
