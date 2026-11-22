"""Offline shared relay checks: real pooled reader, durable journal, fake Telegram."""

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx2
import pytest
from telethon import types, utils
from telethon.errors import FloodWaitError

from tests.media.test_pooled_media import account
from tests.media.test_telegram_cache import MIB, message
from tests.storage.test_mongodb import backend as mongo_test_backend  # noqa: F401
from tests.support.app_fixtures import FakeRuntime
from tests.support.mongo_fixtures import register_test_backend
from tests.telegram.test_bot_intake import configured_intake, settled
from tests.transfers.test_multipart_transfers import dispose, registry_for
from tgdrive.media.media_reader import media_cache_for
from tgdrive.media.media_scheduler import effective_priority, media_priority
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import TelegramMessage, _block_identity
from tgdrive.telegram.shared_intake import (
    SharedIntakeError,
    _copy,
    cleanup_shared,
    download_shared,
)
from tgdrive.transfers.staging import staging_usage
from tgdrive.transfers.tasks import TransferStatus
from tgdrive.web.app import create_app

CHANNEL = -1000000000123


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    backend = register_test_backend(monkeypatch, tmp_path)
    return register_test_backend(monkeypatch, tmp_path / "data", backend)


async def shared(tmp_path, *, payload=b"relay payload", gate=None, backend=None):
    mode, manager, original = await configured_intake(tmp_path, backend=backend)
    client: Any = original
    mode.settings.bot_intake_channel = CHANNEL
    mode.settings.max_file_size = 16 * MIB
    mode.settings.staging_budget_bytes = 16 * MIB
    await manager.pool.stop()
    accounts = [account(f"reader-{i}", payload, gate) for i in range(3)]
    pool = ClientPool(accounts, media_cache_dir=tmp_path / "media-cache")
    await pool.start()
    manager.pool = pool
    source = client.source(1, size=len(payload))
    source.native.document.id = 1
    copied = TelegramMessage(mode.client, message(1, len(payload)))
    client.sources[(CHANNEL, copied.id)] = copied
    client.storage_channel_problem = AsyncMock(return_value=None)
    client.copy_media = AsyncMock(return_value=copied)

    async def delete(channel, identity):
        assert channel == CHANNEL and identity == copied.id
        client.sources.pop((channel, identity), None)

    client.delete_messages = AsyncMock(side_effect=delete)
    await mode._file(mode.client, source)
    record = manager.registry.list()[0]
    target = Path(record.context["local_path"])
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    return mode, manager, client, accounts, source, record, target


async def test_shared_receipt_distributes_chunks_and_cleans_only_its_copy(tmp_path):
    payload = b"".join(bytes([i]) * MIB for i in range(8)) + b"tail"
    mode, manager, client, accounts, source, row, target = await shared(
        tmp_path, payload=payload
    )

    async def dispatch(*args, **kwargs):
        saved = manager.registry._durable[row.id]["context"]["bot_intake"]
        assert saved["dispatched"] and saved["random_id"] == kwargs["random_id"]
        return client.sources[(CHANNEL, 77)]

    client.copy_media.side_effect = dispatch
    progress = AsyncMock()
    cache = media_cache_for(manager.pool)
    unrelated = TelegramMessage(mode.client, message(2, 4))
    unrelated.native.id = 88
    block = _block_identity(unrelated, 0)
    assert block is not None
    await cache.get(block[0], 4, AsyncMock(return_value=b"keep"))
    retained = set(cache._files)
    try:
        await download_shared(mode, row, source, target, progress)
        assert target.read_bytes() == payload
        assert target.stat().st_mode & 0o777 == 0o600
        assert sum(bool(item.native.requests) for item in accounts) >= 2
        client.copy_media.assert_awaited_once()
        client.delete_messages.assert_awaited_once_with(CHANNEL, 77)
        assert client.sources[(1, source.id)] is source
        assert row.context["bot_intake"]["cleaned"]
        assert set(cache._files) == retained
        assert (
            await cache.get(block[0], 4, AsyncMock(side_effect=AssertionError))
            == b"keep"
        )
        assert progress.await_args is not None
        assert progress.await_args.args == (len(payload), len(payload))
    finally:
        await dispose(manager)


async def test_unknown_dispatch_is_retained_and_never_copied_or_deleted_again(tmp_path):
    mode, manager, client, _, source, row, target = await shared(tmp_path)
    client.copy_media.side_effect = TimeoutError("lost provider acknowledgement")
    try:
        with pytest.raises(SharedIntakeError, match="did not confirm"):
            await download_shared(mode, row, source, target, AsyncMock())
        assert row.context["bot_intake"]["dispatched"]
        assert "message_id" not in row.context["bot_intake"]
        await manager.registry.update(row.id, status=TransferStatus.ERROR)
        assert await manager.registry.clear_history() == 0
        assert not row.history_hidden
        await manager.registry.prune(now=time.time() + 10**9)
        assert manager.registry.get(row.id) is row
        with pytest.raises(SharedIntakeError, match="did not confirm"):
            await _copy(mode, row, source)
        client.copy_media.assert_awaited_once()
        client.delete_messages.assert_not_awaited()
    finally:
        await dispose(manager)


async def test_known_copy_timeout_exposes_retry_after_cleanup(tmp_path, monkeypatch):
    mode, manager, client, _, source, row, _ = await shared(tmp_path)
    cleanup_started, release_cleanup, cleaned = (
        asyncio.Event(), asyncio.Event(), asyncio.Event()
    )
    intake = mode.intake
    assert intake is not None

    async def timed_out(mode, record, message, target, progress):
        await _copy(mode, record, message)
        raise TimeoutError("download timed out after copy")

    async def cleanup(record):
        cleanup_started.set()
        await release_cleanup.wait()
        result = await cleanup_shared(mode, record)
        cleaned.set()
        return result

    monkeypatch.setattr("tgdrive.telegram.bot_mode.download_shared", timed_out)
    monkeypatch.setattr(intake, "cleanup", cleanup)
    try:
        await mode.start()
        await asyncio.wait_for(cleanup_started.wait(), timeout=3)
        assert row.status is TransferStatus.ERROR
        assert row.as_dict()["phase"] == "bot_intake_cleanup"
        assert row.as_dict()["error"] == "Telegram timed out. Try again."
        assert not row.bot_intake_review_required
        text = source.status.edit_text.await_args_list[-1].args[0]
        assert "Retry after temporary cleanup finishes." in text
        release_cleanup.set()
        await asyncio.wait_for(cleaned.wait(), timeout=3)
        assert row.as_dict()["phase"] == "bot_intake_timeout"
        client.copy_media.assert_awaited_once()
        client.delete_messages.assert_awaited_once_with(CHANNEL, 77)
        assert client.sources[(1, source.id)] is source
    finally:
        release_cleanup.set()
        await manager.close()
        await mode.stop()
        await dispose(manager)


async def test_terminal_cleanup_failure_survives_restart_and_saved_deadline(tmp_path):
    mode, manager, client, _, source, row, _ = await shared(tmp_path)
    try:
        await _copy(mode, row, source)
        await manager.registry.update(
            row.id, status=TransferStatus.CANCELLED, phase="cancelled"
        )
        client.delete_messages.side_effect = ConnectionError("offline")
        assert not await cleanup_shared(mode, row)
        deadline = row.context["bot_intake"]["retry_at"]
        assert deadline > time.time()
        assert row.as_dict()["phase"] == "bot_intake_cleanup"
        assert await manager.registry.clear_history() == 0
        assert not row.history_hidden
        await manager.registry.prune(now=time.time() + 10**9)
        await manager.close()
        manager.registry.close()
        manager.registry = registry_for(tmp_path)
        restored = manager.registry.get(row.id)
        assert restored.context["bot_intake"]["retry_at"] == deadline
        assert restored.as_dict()["phase"] == "bot_intake_cleanup"
        client.delete_messages.reset_mock()
        assert mode.intake is not None
        mode.intake.start()
        await asyncio.sleep(0.05)
        client.delete_messages.assert_not_awaited()

        async def remove(channel, identity):
            client.sources.pop((channel, identity), None)

        client.delete_messages.side_effect = remove
        restored.context["bot_intake"]["retry_at"] = 0
        await manager.registry.persist(restored)
        mode.intake.wake()
        async with asyncio.timeout(3):
            while not restored.context["bot_intake"].get("cleaned"):  # noqa: ASYNC110 - bounded journal observation.
                await asyncio.sleep(0.01)
        assert restored.status is TransferStatus.CANCELLED
        assert restored.as_dict()["phase"] == "cancelled"
        client.copy_media.assert_awaited_once()
        client.delete_messages.assert_awaited_once_with(CHANNEL, 77)
    finally:
        await mode.stop()
        await dispose(manager)


@pytest.mark.parametrize("unsafe", ["storage", "backup", "public", "changed"])
async def test_shared_copy_rejects_unsafe_channel_or_media(tmp_path, unsafe):
    mode, manager, client, _, source, row, _ = await shared(tmp_path)
    if unsafe == "storage":
        row.context["bot_intake"]["channel"] = mode.settings.storage_channel
    elif unsafe == "backup":
        mode.folder_settings.runtime.replica = SimpleNamespace(
            journal=SimpleNamespace(get=lambda _: {"channel": CHANNEL})
        )
    elif unsafe == "public":
        client.storage_channel_problem.return_value = ("CHANNEL_PUBLIC", "public")
    else:
        client.sources[(CHANNEL, 77)].native.document.id = 999
    try:
        with pytest.raises(SharedIntakeError):
            await _copy(mode, row, source)
        if unsafe != "changed":
            client.copy_media.assert_not_awaited()
        else:
            assert not await cleanup_shared(mode, row)
            client.delete_messages.assert_not_awaited()
    finally:
        await dispose(manager)


async def test_cancelled_shared_receipt_drains_readers_and_removes_owned_copy(tmp_path):
    gate = asyncio.Event()
    mode, manager, client, accounts, source, row, target = await shared(
        tmp_path, payload=b"x" * (4 * MIB), gate=gate
    )
    task = asyncio.create_task(download_shared(mode, row, source, target, AsyncMock()))
    try:
        async with asyncio.timeout(3):
            while not any(item.native.active for item in accounts):  # noqa: ASYNC110 - bounded provider observation.
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(item.native.active == 0 for item in accounts)
        assert row.context["bot_intake"]["cleaned"]
        client.delete_messages.assert_awaited_once_with(CHANNEL, 77)
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)
        await dispose(manager)


async def test_confirmed_cleanup_allows_new_attempt_without_reusing_deleted_id(
    tmp_path,
):
    mode, manager, client, _, source, row, _ = await shared(tmp_path)
    try:
        await _copy(mode, row, source)
        first = row.context["bot_intake"]["random_id"]
        assert await cleanup_shared(mode, row)
        client.sources[(CHANNEL, 77)] = client.copy_media.return_value
        await _copy(mode, row, source)
        assert row.context["bot_intake"]["random_id"] != first
        assert row.context["bot_intake"]["attempt"] == 1
        assert await cleanup_shared(mode, row)
    finally:
        await dispose(manager)


@pytest.mark.parametrize(
    "live_mongo", [False, True], ids=["fixture-journal", "mongo-journal"]
)
async def test_shared_queue_runs_one_pooled_import_at_a_time_with_foreground_reads(
    tmp_path, monkeypatch, request, live_mongo
):
    backend = None
    if live_mongo:
        if os.environ.get("TGDRIVE_MONGO_TESTS") != "1":
            pytest.skip("Set TGDRIVE_MONGO_TESTS=1 for isolated MongoDB checks")
        backend = request.getfixturevalue("mongo_test_backend")
        register_test_backend(monkeypatch, tmp_path, backend)
        register_test_backend(monkeypatch, tmp_path / "data", backend)
    receiving, gate = asyncio.Event(), asyncio.Event()

    async def wait_for_intake():
        if effective_priority(media_priority.get()) == "preload":
            receiving.set()
            await gate.wait()

    payload = b"x" * (8 * MIB)
    mode, manager, client, accounts, first, first_row, _ = await shared(
        tmp_path,
        payload=payload,
        gate=SimpleNamespace(wait=wait_for_intake),
        backend=backend,
    )
    second = client.source(2, size=first.document.file_size)
    second.native.document.id = 1
    copied = client.copy_media.return_value
    second_native = message(1, second.document.file_size)
    second_native.id = 78
    copied_second = TelegramMessage(mode.client, second_native)
    video_bytes = b"v" * (2 * MIB)
    video_native = types.Message(
        id=79,
        peer_id=types.PeerChannel(
            utils.resolve_id(manager.settings.storage_channel)[0]
        ),
        date=copied.native.date,
        message="",
        media=message(2, len(video_bytes)).media,
    )
    video = TelegramMessage(mode.client, video_native)
    video_channel = video.chat_id

    def lookup(channel, *, message_ids):
        if (channel, message_ids) == (video_channel, video.id):
            return video
        return client.sources[(channel, message_ids)]

    for reader in accounts:
        reader.native.payloads[2] = video_bytes
        monkeypatch.setattr(reader, "get_messages", AsyncMock(side_effect=lookup))

    async def copy(channel, original, **_options):
        assert original is first.native or original is second.native
        temporary = copied if original is first.native else copied_second
        client.sources[(channel, temporary.id)] = temporary
        return temporary

    async def delete(channel, identity):
        assert channel == CHANNEL and identity in {copied.id, copied_second.id}
        del client.sources[(channel, identity)]

    client.copy_media.side_effect = copy
    client.delete_messages.side_effect = delete
    completed = []

    async def upload(record, local_path, destination, file_name, size):
        completed.append((record.id, local_path.read_bytes()))
        local_path.unlink()
        await manager.registry.update(
            record.id, status=TransferStatus.COMPLETED, phase="completed"
        )

    manager._upload_path = upload
    folder = manager.store.create_folder("/", "Playback")
    folder_path = "/" + folder["id"]
    stored_video = manager.store.add_file(
        folder_path, "video.mp4", video.id, len(video_bytes)
    )
    runtime = FakeRuntime(manager.store)
    runtime.pool = manager.pool
    monkeypatch.setattr(runtime, "transfers", manager)
    app = create_app(manager.settings, runtime=runtime)
    browser = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="https://testserver"
    )
    try:
        login = await browser.post(
            "/api/checkPassword", json={"pass": "correct horse battery staple"}
        )
        assert login.status_code == 200
        await mode.start()
        await mode._file(mode.client, second)
        await asyncio.wait_for(receiving.wait(), 3)
        assert client.copy_media.await_count == 1
        assert mode.intake is not None
        assert len(mode.intake.pending()) == 2
        second_row = mode.intake.pending()[1]
        assert first_row.context["staging_size"] == len(payload)
        assert second_row.context["staging_size"] == 0
        assert staging_usage(manager) == (len(payload), len(payload))
        async with asyncio.timeout(3):
            listing = await browser.get(
                "/api/v1/nodes", params={"parent_id": folder_path}
            )
            assert listing.status_code == 200
            assert [item["id"] for item in listing.json()["items"]] == [
                stored_video["id"]
            ]
            response = await browser.get(
                "/file",
                params={"path": f"{folder_path}/{stored_video['id']}"},
                headers={"Range": "bytes=0-1048575"},
            )
            assert response.status_code == 206
            assert response.content == video_bytes[:MIB]
        assert client.copy_media.await_count == 1
        assert not completed
        gate.set()
        await settled(mode.intake)
        await asyncio.gather(*(row.task for row in (first_row, second_row) if row.task))
        assert completed == [(first_row.id, payload), (second_row.id, payload)]
        assert staging_usage(manager) == (0, 0)
        reopened = registry_for(tmp_path)
        assert all(
            reopened.get(row.id).status is TransferStatus.COMPLETED
            and reopened.get(row.id).context["bot_intake"]["cleaned"]
            for row in (first_row, second_row)
        )
        assert (
            sum(
                any(identity == 1 for identity, _offset in reader.native.requests)
                for reader in accounts
            )
            >= 2
        )
        assert client.copy_media.await_count == 2
        assert [call.args for call in client.delete_messages.await_args_list] == [
            (CHANNEL, copied.id),
            (CHANNEL, copied_second.id),
        ]
        assert (CHANNEL, copied.id) not in client.sources
        assert (CHANNEL, copied_second.id) not in client.sources
        assert client.sources[(1, first.id)] is first
        assert client.sources[(1, second.id)] is second
        assert client.downloads == []
    finally:
        gate.set()
        await browser.aclose()
        app.state.sessions.close()
        await manager.close()
        await mode.stop()
        await dispose(manager)


async def test_copy_cancellation_drains_dispatch_and_keeps_returned_identity(tmp_path):
    mode, manager, client, _, source, row, target = await shared(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    copied = client.copy_media.return_value

    async def copy(*args, **kwargs):
        entered.set()
        await release.wait()
        return copied

    client.copy_media.side_effect = copy
    task = asyncio.create_task(download_shared(mode, row, source, target, AsyncMock()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert row.context["bot_intake"]["message_id"] == 77
        assert row.context["bot_intake"]["cleaned"]
        client.delete_messages.assert_awaited_once_with(CHANNEL, 77)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await dispose(manager)


async def test_copy_floodwait_is_definite_rejection_and_retries_after_deadline(
    tmp_path,
):
    mode, manager, client, _, _, row, _ = await shared(tmp_path)
    copied = client.copy_media.return_value
    client.copy_media.side_effect = [
        FloodWaitError(request=None, capture=60),
        copied,
    ]

    async def upload(record, local_path, destination, file_name, size):
        local_path.unlink()
        await manager.registry.update(
            record.id, status=TransferStatus.COMPLETED, phase="completed"
        )

    manager._upload_path = upload
    try:
        await mode.start()
        assert mode.intake is not None
        async with asyncio.timeout(3):
            while row.phase != "waiting_for_telegram":  # noqa: ASYNC110 - bounded journal observation.
                await asyncio.sleep(0.01)
        assert not row.context["bot_intake"].get("dispatched")
        assert row.context["bot_retry_at"] > time.time()
        await asyncio.sleep(0.02)
        client.copy_media.assert_awaited_once()
        row.context["bot_retry_at"] = 0
        await manager.registry.persist(row)
        mode.intake.wake()
        async with asyncio.timeout(3):
            while mode.intake.pending():  # noqa: ASYNC110 - bounded journal observation.
                await asyncio.sleep(0.01)
        assert row.status is TransferStatus.COMPLETED
        assert client.copy_media.await_count == 2
        client.delete_messages.assert_awaited_once_with(CHANNEL, 77)
    finally:
        await manager.close()
        await mode.stop()
        await dispose(manager)


@pytest.mark.parametrize("fail", [False, True])
async def test_crash_orphan_cache_blocks_are_strictly_removed_or_retained(
    tmp_path, monkeypatch, fail
):
    mode, manager, _, _, source, row, _ = await shared(tmp_path)
    unlink = Path.unlink
    try:
        copied = await _copy(mode, row, source)
        block = _block_identity(copied, 0)
        assert block is not None
        row.context["bot_intake"]["cache_prefix"] = list(block[0][:-1])
        await manager.registry.persist(row)
        cache = media_cache_for(manager.pool)
        assert cache.disk is not None
        cache.disk.write(block[0], b"x" * block[1])
        # Simulate the block reaching disk before media inventory was committed.
        assert not cache._blocks and not cache._files
        path = cache.disk.directory / (cache.disk._key(block[0]) + ".block")
        assert path.exists()
        if fail:

            def denied(candidate, *args, **kwargs):
                if candidate == path:
                    raise PermissionError("synthetic unavailable cache deletion")
                return unlink(candidate, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", denied)
        assert await cleanup_shared(mode, row) is not fail
        assert path.exists() is fail
        assert bool(row.context["bot_intake"].get("cleaned")) is not fail
        if fail:
            assert manager.registry._retained(row)
            assert row.context["bot_intake"]["retry_at"] > time.time()
    finally:
        if fail:
            monkeypatch.setattr(Path, "unlink", unlink)
        await dispose(manager)


async def test_unknown_copy_pauses_new_bot_dispatch_until_owner_review(
    tmp_path, monkeypatch
):
    mode, manager, client, accounts, source, row, _ = await shared(tmp_path)
    intake = mode.intake
    assert intake is not None
    dispatching, release, blocked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    second = client.source(2, size=source.document.file_size)
    second.native.document.id = source.native.document.id
    second_native = message(1, second.document.file_size)
    second_native.id = 78
    second_copy = TelegramMessage(mode.client, second_native)
    for account_client in accounts:
        monkeypatch.setattr(
            account_client, "get_messages", AsyncMock(return_value=second_copy)
        )

    async def copy(_channel, original, **_options):
        if original is source.native:
            dispatching.set()
            await release.wait()
            raise TimeoutError("lost copy response")
        assert original is second.native
        client.sources[(CHANNEL, 78)] = second_copy
        return second_copy

    async def delete(channel, identity):
        assert (channel, identity) == (CHANNEL, 78)
        del client.sources[(channel, identity)]

    async def upload(record, local_path, destination, file_name, size):
        local_path.unlink()
        await manager.registry.update(
            record.id, status=TransferStatus.COMPLETED, phase="completed"
        )

    client.copy_media.side_effect = copy
    client.delete_messages.side_effect = delete
    manager._upload_path = upload
    try:
        await mode.start()
        await asyncio.wait_for(dispatching.wait(), timeout=3)
        with pytest.raises(ValueError, match="Wait for the bot import"):
            await manager.review_bot_intake(row.id)
        release.set()
        await settled(intake)
        assert row.status is TransferStatus.ERROR
        assert row.error is not None
        original_error = row.error
        text = source.status.edit_text.await_args_list[-1].args[0]
        assert "The bot queue is paused" in text
        assert "Confirm intake channel reviewed" in text
        assert "Try again" not in text
        await mode.stop()
        await mode._file(mode.client, second)
        await manager.close()
        manager.registry.close()
        manager.registry = registry_for(tmp_path)
        restored = manager.registry.get(row.id)

        wait_for_wake = intake._changed.wait

        async def observe_wait():
            blocked.set()
            return await wait_for_wake()

        monkeypatch.setattr(intake._changed, "wait", observe_wait)
        await mode.start()
        await asyncio.wait_for(blocked.wait(), timeout=3)
        client.copy_media.assert_awaited_once()
        assert await manager.registry.clear_history() == 0
        assert not restored.history_hidden
        assert len(intake.pending()) == 1
        assert restored.bot_intake_review_required
        with monkeypatch.context() as patch:
            patch.setattr(
                manager.registry,
                "_write",
                AsyncMock(side_effect=DatabaseUnavailable("offline")),
            )
            with pytest.raises(DatabaseUnavailable):
                await manager.review_bot_intake(row.id)
        assert restored.bot_intake_review_required
        client.copy_media.assert_awaited_once()
        client.delete_messages.assert_not_awaited()

        await manager.review_bot_intake(row.id)
        reviewed_at = restored.context["bot_intake"]["reviewed_at"]
        await manager.review_bot_intake(row.id)
        await settled(intake)
        assert restored.status is TransferStatus.ERROR
        assert restored.error == original_error
        assert restored.phase == "bot_intake_reviewed"
        assert restored.context["bot_intake"]["reviewed_at"] == reviewed_at
        assert manager.registry.list()[0].status is TransferStatus.COMPLETED
        with pytest.raises(ValueError, match="cannot be retried"):
            await manager.retry_job(row.id)
        assert client.copy_media.await_count == 2
        client.delete_messages.assert_awaited_once_with(CHANNEL, 78)
        assert (CHANNEL, 77) in client.sources
        assert (1, source.id) in client.sources
        await manager.close()
        await mode.stop()
        manager.registry.close()
        manager.registry = registry_for(tmp_path)
        reopened = manager.registry.get(row.id)
        assert not reopened.bot_intake_review_required
        assert reopened.context["bot_intake"]["reviewed_at"] == reviewed_at
    finally:
        release.set()
        await manager.close()
        await mode.stop()
        await dispose(manager)
