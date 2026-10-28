"""Video posters share the original upload's durable publication and cleanup."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import tests.transfers.test_multipart_transfers as multipart_tests
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.media import generated_thumbnails as covers
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.connection_errors import TelegramConnectionError
from tgdrive.transfers import multipart_transfer
from tgdrive.transfers.manager import TransferManager
from tgdrive.transfers.tasks import TransferStatus, upload_effects

THUMBNAIL = b"\xff\xd8\xffsmall\xff\xd9"
POSTER = b"\xff\xd8\xfflarge\xff\xd9"
PAYLOAD = b"original video payload"


class PosterLedger(multipart_tests.Ledger):
    def __init__(self):
        super().__init__()
        self.options = []
        self.lose_poster_reply = False
        self.poster_failure = None
        self.hold_poster = False


class PosterAccount(multipart_tests.Account):
    supports_generated_thumbnails = True

    async def send_staged(
        self,
        *args,
        preview_name=None,
        thumbnail=None,
        generate_thumbnail=True,
        **kwargs,
    ):
        state = self.ledger
        state.options.append(
            dict(
                sender=self.name,
                file_name=kwargs["file_name"],
                random_id=kwargs["random_id"],
                thumbnail=thumbnail,
                generate_thumbnail=generate_thumbnail,
            )
        )
        if kwargs["file_name"] == "tgdrive-poster.jpg":
            if state.poster_failure is not None:
                raise state.poster_failure
            if state.lose_poster_reply:
                state.lose_poster_reply = False
                state.lose_once = True
            if state.hold_poster:
                state.hold = True
        return await super().send_staged(*args, **kwargs)


@pytest.fixture(autouse=True)
def poster_backend(tmp_path, monkeypatch):
    register_test_backend(monkeypatch, tmp_path)
    monkeypatch.setattr(multipart_tests, "Account", PosterAccount)


@pytest.fixture
def images(monkeypatch):
    generate = AsyncMock(return_value=(THUMBNAIL, POSTER))
    monkeypatch.setattr(covers, "generate_video_images", generate)
    return generate


async def manager_for(tmp_path):
    manager, ledger = await multipart_tests.manager_for(tmp_path, ledger=PosterLedger())
    manager.settings.bot_upload_limit = 1024**2
    return manager, ledger


async def test_single_video_commits_two_documents_without_changing_original(
    tmp_path, images
):
    manager, ledger = await manager_for(tmp_path)
    source = multipart_tests.staged(manager, PAYLOAD)
    image_paths = multipart_transfer.upload_image_paths(source)
    try:
        record = await manager.enqueue_upload(source, "/", "clip.mp4", len(PAYLOAD))
        await record.task
        assert record.status is TransferStatus.COMPLETED
        item = multipart_tests.stored(manager)
        assert len(ledger.requests) == len(ledger.messages) == 2
        assert item["size"] == record.current == len(PAYLOAD)
        assert item["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
        assert len(item["parts"]) == 1
        assert ledger.messages[item["message_id"]].content == PAYLOAD
        poster = item["poster"]
        assert poster["message_id"] != item["message_id"]
        assert ledger.messages[poster["message_id"]].content == POSTER
        assert poster["sha256"] == hashlib.sha256(POSTER).hexdigest()
        assert ledger.options[0]["thumbnail"] == THUMBNAIL
        assert all(not row["generate_thumbnail"] for row in ledger.options)
        images.assert_awaited_once_with(source, "clip.mp4")
        assert (
            covers.read(
                manager.cache_dir / "posters",
                manager.settings.storage_channel,
                poster["message_id"],
                maximum=covers.MAX_POSTER_BYTES,
            )
            == POSTER
        )
        assert not source.exists()
        assert not any(path.exists() for path in image_paths)
    finally:
        await multipart_tests.dispose(manager)


async def test_lost_poster_reply_reuses_identity_and_does_not_regenerate(
    tmp_path, images
):
    manager, ledger = await manager_for(tmp_path)
    ledger.lose_poster_reply = True
    source = multipart_tests.staged(manager, PAYLOAD)
    try:
        record = await manager.enqueue_upload(source, "/", "clip.mp4", len(PAYLOAD))
        await record.task
        assert record.status is TransferStatus.ERROR
        poster = record.context["multipart_plan"]["poster"]
        identity = poster["sender"], poster["random_id"]
        assert poster["send_started"] and not poster["confirmed"]
        assert not manager.store.list_directory("/")
        assert source.exists()
        record = await manager.retry_job(record.id)
        await record.task
        assert record.status is TransferStatus.COMPLETED
        attempts = [
            row for row in ledger.options if row["file_name"] == "tgdrive-poster.jpg"
        ]
        assert [(row["sender"], row["random_id"]) for row in attempts] == [identity] * 2
        assert len(ledger.messages) == len(ledger.requests) == 2
        assert images.await_count == 1
        poster_id = multipart_tests.stored(manager)["poster"]["message_id"]
        assert poster_id in ledger.messages
    finally:
        await multipart_tests.dispose(manager)


@pytest.mark.parametrize("available", [False, True], ids=["no-images", "small-only"])
async def test_missing_poster_does_not_block_original(tmp_path, images, available):
    manager, ledger = await manager_for(tmp_path)
    images.return_value = (THUMBNAIL if available else None, None)
    try:
        record = await manager.enqueue_upload(
            multipart_tests.staged(manager, PAYLOAD), "/", "clip.mp4", len(PAYLOAD)
        )
        await record.task
        assert record.status is TransferStatus.COMPLETED
        assert not multipart_tests.stored(manager).get("poster")
        assert len(ledger.messages) == 1
        assert ledger.options[0]["thumbnail"] == (THUMBNAIL if available else None)
        assert ledger.options[0]["generate_thumbnail"] is False
    finally:
        await multipart_tests.dispose(manager)


@pytest.mark.parametrize("failure", ["staging", "upload", "connection"])
async def test_predispatch_poster_failures_fall_back_to_original(
    tmp_path, monkeypatch, images, failure
):
    manager, ledger = await manager_for(tmp_path)
    if failure == "staging":
        stage_image = multipart_transfer._stage_image

        def stage(path, content):
            if path.name.endswith(".poster.jpg"):
                raise OSError("synthetic optional image staging failure")
            stage_image(path, content)

        monkeypatch.setattr(multipart_transfer, "_stage_image", stage)
    else:
        ledger.poster_failure = (
            TelegramConnectionError("synthetic unavailable account", "TEST_ACCOUNT")
            if failure == "connection"
            else OSError("synthetic optional image upload failure")
        )
    source = multipart_tests.staged(manager, PAYLOAD)
    try:
        record = await manager.enqueue_upload(source, "/", "clip.mp4", len(PAYLOAD))
        await record.task
        assert record.status is TransferStatus.COMPLETED
        assert not multipart_tests.stored(manager).get("poster")
        assert len(ledger.messages) == 1
        assert ledger.messages[next(iter(ledger.messages))].content == PAYLOAD
        assert not source.exists()
        assert not any(
            path.exists() for path in multipart_transfer.upload_image_paths(source)
        )
    finally:
        await multipart_tests.dispose(manager)


async def test_cancellation_deletes_original_poster_and_owned_image_sources(
    tmp_path, images
):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold_poster = True
    source = multipart_tests.staged(manager, PAYLOAD)
    try:
        record = await manager.enqueue_upload(source, "/", "clip.mp4", len(PAYLOAD))
        await asyncio.wait_for(ledger.both_dispatched.wait(), 2)
        await manager.cancel_job(record.id)
        assert source.exists()
        ledger.release.set()
        await record.task
        assert record.status is TransferStatus.CANCELLED
        assert not ledger.messages
        assert not manager.store.list_directory("/")
        assert not source.exists()
        assert not any(
            path.exists() for path in multipart_transfer.upload_image_paths(source)
        )
        assert all(
            part["deleted"]
            for part in upload_effects(record.context["multipart_plan"])
            if part.get("message_id")
        )
    finally:
        ledger.release.set()
        await multipart_tests.dispose(manager)


async def test_restart_replays_ambiguous_poster_from_owned_staging(tmp_path, images):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold_poster = True
    source = multipart_tests.staged(manager, PAYLOAD)
    record = await manager.enqueue_upload(source, "/", "clip.mp4", len(PAYLOAD))
    await asyncio.wait_for(ledger.both_dispatched.wait(), 2)
    poster = record.context["multipart_plan"]["poster"]
    identity = poster["sender"], poster["random_id"], poster["sha256"]
    record_id = record.id
    await manager.close()
    manager.registry.close()
    manager.store.close()
    await manager.pool.stop()
    assert source.exists()
    assert all(path.exists() for path in multipart_transfer.upload_image_paths(source))
    ledger.hold_poster = ledger.hold = False
    ledger.release.set()
    pool = ClientPool(
        [PosterAccount("bot-one", ledger), PosterAccount("bot-two", ledger)]
    )
    await pool.start()
    recovered = TransferManager(
        settings=manager.settings,
        pool=pool,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=multipart_tests.registry_for(tmp_path),
        remote=SimpleNamespace(close=AsyncMock()),
    )
    try:
        await recovered.recover()
        record = recovered.registry.get(record_id)
        await record.task
        assert record.status is TransferStatus.COMPLETED
        poster = record.context["multipart_plan"]["poster"]
        assert (poster["sender"], poster["random_id"], poster["sha256"]) == identity
        assert len(ledger.messages) == len(ledger.requests) == 2
        assert images.await_count == 1
        assert not source.exists()
        assert not any(
            path.exists() for path in multipart_transfer.upload_image_paths(source)
        )
    finally:
        await multipart_tests.dispose(recovered)
