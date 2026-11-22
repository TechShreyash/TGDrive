"""Sequential bot intake reuses durable jobs and their cancellation owner."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.support.mongo_fixtures import register_test_backend
from tests.telegram.test_bot_mode import (
    FakeLifecycleClient,
    FakeMessage,
    root_mode,
    settings,
)
from tests.transfers.test_multipart_transfers import dispose, manager_for, registry_for
from tgdrive.settings.bot_settings import BotUploadPreferences
from tgdrive.telegram.bot_intake import BotIntake
from tgdrive.transfers.staging import staging_usage
from tgdrive.transfers.tasks import TransferStatus


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    backend = register_test_backend(monkeypatch, tmp_path)
    return register_test_backend(monkeypatch, tmp_path / "data", backend)


async def test_batch_has_one_full_pipeline_and_pending_cancel_is_immediate(tmp_path):
    manager, _ = await manager_for(tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()
    started = []
    active = peak = 0

    async def process(record):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.append(record.id)
        await manager.registry.update(record.id, status=TransferStatus.RUNNING)
        # Receiving is finished; publishing must still hold the intake worker.
        record.context["kind"] = "upload"
        await manager.registry.persist(record)
        entered.set()
        await finish.wait()
        await manager.registry.update(record.id, status=TransferStatus.COMPLETED)
        active -= 1

    queue = BotIntake(manager, process, AsyncMock(return_value=True))
    try:
        records = [
            await queue.submit(
                f"{index:032x}",
                80,
                {
                    "kind": "receiving",
                    "bot_source": {"message_id": index + 1},
                    "staging_size": 0,
                },
            )
            for index in range(50)
        ]
        replay = await queue.submit(records[0].id, 80, records[0].context)
        assert replay is records[0]
        queue.start()
        await asyncio.wait_for(entered.wait(), 2)
        assert len(started) == 1
        assert sum(row.task is not None for row in records) == 1
        cancelled = await manager.cancel_job(records[1].id)
        assert cancelled.status is TransferStatus.CANCELLED
        assert records[1].id not in started
        finish.set()
        async with asyncio.timeout(5):
            while queue.pending():  # noqa: ASYNC110 - bounded journal observation.
                await asyncio.sleep(0.01)
        assert len(started) == 49
        assert peak == 1
    finally:
        finish.set()
        await manager.close()
        await queue.stop()
        await dispose(manager)


class IntakeClient(FakeLifecycleClient):
    name = "bot-mode-123456"

    def __init__(self):
        super().__init__()
        self.sources = {}
        self.downloads = []
        self.copies = []
        self.get_messages = AsyncMock(side_effect=self.lookup)
        self.reply_to = AsyncMock()

    async def lookup(self, channel, *, message_ids):
        return self.sources.get((channel, message_ids))

    async def download_media(self, message, *, file_name, progress):
        self.downloads.append(message.id)
        data = b"x" * message.document.file_size
        await asyncio.to_thread(Path(file_name).write_bytes, data)
        await progress(len(data), len(data))

    def source(self, identity, size=80):
        message = FakeMessage(1)
        message.id = identity
        message.document = SimpleNamespace(file_name=f"{identity}.bin", file_size=size)
        message.native = SimpleNamespace(
            chat_id=1, document=SimpleNamespace(id=100 + identity)
        )
        copied = SimpleNamespace(
            id=1000 + identity,
            document=SimpleNamespace(file_name=f"{identity}.bin", file_size=size),
        )

        async def copy(*args, **kwargs):
            self.copies.append(identity)
            self.sources[(-1001234567890, copied.id)] = copied
            return copied

        message.copy = copy
        self.sources[(1, identity)] = message
        return message


async def configured_intake(tmp_path, *, encrypted=True, backend=None):
    manager, _ = await manager_for(tmp_path, backend=backend)
    configured = settings(tmp_path).model_copy(
        update={
            "max_file_size": 100,
            "staging_budget_bytes": 100,
            "min_free_bytes": 0,
        }
    )
    manager.settings = configured
    client = IntakeClient()
    mode = root_mode(
        client=client, settings=configured, store=manager.store, transfers=manager
    )
    mode.folder_settings.value = BotUploadPreferences(
        folder_path="/", encrypt=encrypted
    )
    return mode, manager, client


async def settled(queue):
    async with asyncio.timeout(5):
        while queue.pending():  # noqa: ASYNC110 - bounded journal observation.
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("encrypted", [False, True])
async def test_live_entry_serializes_full_pipeline_and_keeps_only_active_claim(
    tmp_path, encrypted
):
    mode, manager, client = await configured_intake(tmp_path, encrypted=encrypted)
    entered, release = asyncio.Event(), asyncio.Event()
    first, second = client.source(1), client.source(2)
    if encrypted:

        async def protect(revision):
            entered.set()
            await release.wait()
            return revision

        manager._ensure_backup = protect
    else:
        copy = first.copy

        async def protected_copy(*args, **kwargs):
            entered.set()
            await release.wait()
            return await copy(*args, **kwargs)

        first.copy = protected_copy
    try:
        await mode.start()
        await mode._file(mode.client, first)
        await mode._file(mode.client, second)
        await asyncio.wait_for(entered.wait(), 3)
        assert mode.intake is not None
        assert len(mode.intake.pending()) == 2
        assert all(
            row.context["bot_encrypt"] is encrypted for row in mode.intake.pending()
        )
        assert staging_usage(manager)[0] <= 80
        assert client.downloads == ([1] if encrypted else [])
        assert client.copies == []
        if encrypted:
            assert any(
                "Receiving" in call.args[0]
                for call in first.status.edit_text.await_args_list
            )
        release.set()
        await settled(mode.intake)
        assert client.downloads == ([1, 2] if encrypted else [])
        assert client.copies == ([] if encrypted else [1, 2])
        assert len(manager.store.list_directory("/")) == 2
        assert all(
            row.status is TransferStatus.COMPLETED for row in manager.registry.list()
        )
        # Terminal status can precede staged-file removal by a disk worker.
        await asyncio.gather(*(row.task for row in manager.registry.list() if row.task))
        assert staging_usage(manager) == (0, 0)
    finally:
        release.set()
        await manager.close()
        await mode.stop()
        await dispose(manager)


async def test_accepted_intake_survives_reloaded_journal_and_discards_partial_bytes(
    tmp_path,
):
    mode, manager, client = await configured_intake(tmp_path)
    source = client.source(1)
    try:
        # Accept into the journal before starting its background worker.
        await mode._file(mode.client, source)
        record = manager.registry.list()[0]
        path = Path(record.context["local_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"partial")
        await manager.close()
        await mode.stop()
        manager.registry.close()
        manager.registry = registry_for(tmp_path)
        replacement = root_mode(
            client=client,
            settings=manager.settings,
            store=manager.store,
            transfers=manager,
        )
        await manager.recover()
        await replacement.start()
        try:
            await settled(replacement.intake)
            assert client.downloads == [1]
            assert manager.registry.get(record.id).status is TransferStatus.COMPLETED
            assert not path.exists()
            assert len(manager.store.list_directory("/")) == 1
        finally:
            await manager.close()
            await replacement.stop()
    finally:
        await dispose(manager)


async def test_unknown_plaintext_copy_outcome_is_not_replayed(tmp_path):
    mode, manager, client = await configured_intake(tmp_path, encrypted=False)
    source = client.source(1)
    try:
        await mode._file(mode.client, source)
        record = manager.registry.list()[0]
        record.context["send_started"] = True
        await manager.registry.persist(record)
        await mode.start()
        await settled(mode.intake)
        assert record.status is TransferStatus.ERROR
        assert client.copies == []
        assert manager.store.list_directory("/") == {}
        assert record.error is not None
        assert "review" in record.error.lower()
    finally:
        await manager.close()
        await mode.stop()
        await dispose(manager)


async def test_private_bot_download_yields_to_existing_foreground_demand(
    tmp_path, monkeypatch
):
    mode, manager, client = await configured_intake(tmp_path)
    source = client.source(1)
    busy = [True]
    monkeypatch.setattr(
        manager.pool,
        "performance_snapshot",
        lambda: {
            "active_requests": [{"operation": "download", "priority": "demand"}]
            if busy[0]
            else [],
        },
    )
    try:
        await mode._file(mode.client, source)
        await mode.start()
        async with asyncio.timeout(2):
            while manager.registry.list()[0].context["staging_size"] == 0:  # noqa: ASYNC110 - bounded journal observation.
                await asyncio.sleep(0.01)
        assert client.downloads == []
        busy[0] = False
        await settled(mode.intake)
        assert client.downloads == [1]
    finally:
        busy[0] = False
        await manager.close()
        await mode.stop()
        await dispose(manager)


@pytest.mark.parametrize("during", ["source lookup", "download"])
async def test_bot_timeout_fails_with_manual_retry_message(tmp_path, during):
    mode, manager, client = await configured_intake(tmp_path)
    source = client.source(1)
    lookup, download = client.get_messages, client.download_media

    async def timed_out(*args, **kwargs):
        raise TimeoutError("provider deadline")

    try:
        await mode._file(mode.client, source)
        if during == "source lookup":
            client.get_messages = AsyncMock(side_effect=timed_out)
        else:
            client.download_media = timed_out
        await mode.start()
        await settled(mode.intake)
        record = manager.registry.list()[0]
        assert record.status is TransferStatus.ERROR
        assert record.as_dict()["error"] == "Telegram timed out. Try again."
        assert not record.context.get("restart_retryable")
        assert not Path(record.context["local_path"]).exists()
        assert record.phase == "bot_intake_timeout"
        feedback = (
            "<b>Upload failed</b>\nTelegram timed out. Try again.\n"
            "Open Transfers in TGDrive and choose Retry on this import."
        )
        if during == "source lookup":
            client.reply_to.assert_awaited_once_with(
                1, source.id, feedback, parse_mode="html"
            )
        else:
            assert source.status.edit_text.await_args_list[-1].args[0] == feedback
            client.reply_to.assert_not_awaited()

        await manager.close()
        await mode.stop()
        manager.registry.close()
        manager.registry = registry_for(tmp_path)
        record = manager.registry.get(record.id)
        client.get_messages, client.download_media = lookup, download
        await manager.recover()
        await mode.start()
        assert record.status is TransferStatus.ERROR
        assert not client.downloads
        assert not mode.intake.pending()
        assert not manager.store.list_directory("/")

        await asyncio.gather(manager.retry_job(record.id), manager.retry_job(record.id))
        await settled(mode.intake)
        assert record.status is TransferStatus.COMPLETED
        assert client.downloads == [1]
        assert len(manager.store.list_directory("/")) == 1
    finally:
        await manager.close()
        await mode.stop()
        await dispose(manager)


async def test_bot_lookup_timeout_is_saved_even_if_telegram_reply_fails(tmp_path):
    mode, manager, client = await configured_intake(tmp_path)
    source = client.source(1)
    client.get_messages = AsyncMock(side_effect=TimeoutError("lookup timed out"))
    client.reply_to.side_effect = TimeoutError("reply timed out")
    try:
        await mode._file(mode.client, source)
        await mode.start()
        await settled(mode.intake)
        row = manager.registry.list()[0]
        assert row.phase == "bot_intake_timeout"
        assert row.error == "Telegram timed out. Try again."
        assert row.task is not None
        await row.task
        client.reply_to.assert_awaited_once()
    finally:
        await manager.close()
        await mode.stop()
        await dispose(manager)


@pytest.mark.parametrize("encrypted", [False, True])
async def test_active_cancel_advances_queue_without_replaying_unknown_copy(
    tmp_path, encrypted
):
    mode, manager, client = await configured_intake(tmp_path, encrypted=encrypted)
    entered, release = asyncio.Event(), asyncio.Event()
    manager.registry._cancel_grace = 0.01
    first, second = client.source(1), client.source(2)
    if encrypted:
        original = client.download_media

        async def blocked(message, *, file_name, progress):
            if message.id == 1:
                client.downloads.append(1)
                await asyncio.to_thread(Path(file_name).write_bytes, b"part")
                await progress(4, 80)
                entered.set()
                await release.wait()
            else:
                await original(message, file_name=file_name, progress=progress)

        client.download_media = blocked
    else:
        copy = first.copy

        async def blocked_copy(*args, **kwargs):
            entered.set()
            await release.wait()
            return await copy(*args, **kwargs)

        first.copy = blocked_copy
    try:
        await mode._file(mode.client, first)
        await mode._file(mode.client, second)
        first_record = next(
            row
            for row in manager.registry.list()
            if row.context["bot_source"]["message_id"] == 1
        )
        await mode.start()
        await asyncio.wait_for(entered.wait(), 3)
        command = FakeMessage(1, text="/cancel")
        command.native = SimpleNamespace(chat_id=1, reply_to_msg_id=1)
        await mode._cancel(mode.client, command)
        await settled(mode.intake)
        assert "Cancellation requested" in command.replies[-1][0]
        assert first_record.status is (
            TransferStatus.CANCELLED if encrypted else TransferStatus.ERROR
        )
        if not encrypted:
            assert first_record.error is not None
            assert "review" in first_record.error.lower()
            assert client.copies == [2]
        assert len(manager.store.list_directory("/")) == 1
        assert not Path(first_record.context["local_path"]).exists()
    finally:
        release.set()
        await manager.close()
        await mode.stop()
        await dispose(manager)


@pytest.mark.parametrize("mutation", ["path", "account", "sender", "content"])
async def test_recovered_source_or_path_changes_never_start_provider_effects(
    tmp_path, mutation
):
    mode, manager, client = await configured_intake(tmp_path)
    source = client.source(1)
    preserved = tmp_path / "preserved"
    preserved.write_bytes(b"keep")
    try:
        await mode._file(mode.client, source)
        record = manager.registry.list()[0]
        if mutation == "path":
            record.context["local_path"] = str(preserved)
        elif mutation == "account":
            record.context["bot_source"]["account"] = "another-bot"
        elif mutation == "sender":
            record.context["bot_source"]["sender_id"] = 2
        else:
            source.native.document.id += 1
        await manager.registry.persist(record)
        await mode.start()
        await settled(mode.intake)
        assert record.status is TransferStatus.ERROR
        assert client.downloads == client.copies == []
        assert preserved.read_bytes() == b"keep"
        assert manager.store.list_directory("/") == {}
    finally:
        await manager.close()
        await mode.stop()
        await dispose(manager)


async def test_known_copy_retries_metadata_without_copying_again(tmp_path, monkeypatch):
    mode, manager, client = await configured_intake(tmp_path, encrypted=False)
    source = client.source(1)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                manager.store,
                "add_file",
                lambda *a, **kw: (_ for _ in ()).throw(
                    OSError("isolated metadata failure")
                ),
            )
            await mode._file(mode.client, source)
            await mode.start()
            await settled(mode.intake)
        record = manager.registry.list()[0]
        assert record.status is TransferStatus.ERROR
        assert record.context["bot_copy_message_id"] == 1001
        assert client.copies == [1]
        await manager.retry_job(record.id)
        await settled(mode.intake)
        assert record.status is TransferStatus.COMPLETED
        assert client.copies == [1]
        assert len(manager.store.list_directory("/")) == 1
    finally:
        await manager.close()
        await mode.stop()
        await dispose(manager)


@pytest.mark.parametrize("cancel", [False, True])
async def test_storage_wait_resumes_or_cancels_without_leaking_claim(tmp_path, cancel):
    mode, manager, client = await configured_intake(tmp_path)
    source = client.source(1)
    try:
        await mode._file(mode.client, source)
        record = manager.registry.list()[0]
        assert "Folder · <code>My Files</code>" in source.replies[0][0]
        assert "Encryption · On" in source.replies[0][0]
        manager.settings.staging_budget_bytes = 40
        await mode.start()
        async with asyncio.timeout(2):
            while record.phase != "waiting_for_staging":  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert record.status is TransferStatus.QUEUED
        assert client.downloads == []
        assert staging_usage(manager) == (0, 0)
        assert any(
            "Waiting for temporary storage" in call.args[0]
            for call in source.status.edit_text.await_args_list
        )
        if cancel:
            await manager.cancel_job(record.id)
        else:
            manager.settings.staging_budget_bytes = 100
        assert record.task is not None
        await record.task
        assert record.status is (
            TransferStatus.CANCELLED if cancel else TransferStatus.COMPLETED
        )
        assert client.downloads == ([] if cancel else [1])
        assert not Path(record.context["local_path"]).exists()
        assert staging_usage(manager) == (0, 0)
    finally:
        await manager.close()
        await mode.stop()
        await dispose(manager)


async def test_pending_import_resumes_when_main_bot_reconnects(tmp_path, monkeypatch):
    mode, manager, client = await configured_intake(tmp_path)
    source = client.source(1)
    disconnected, reconnected = asyncio.Event(), asyncio.Event()

    async def pause(_record):
        disconnected.set()
        await reconnected.wait()

    monkeypatch.setattr(mode, "_background_pause", pause)
    try:
        await mode.start()
        client.is_connected = False
        await mode._file(mode.client, source)
        await asyncio.wait_for(disconnected.wait(), timeout=3)
        record = manager.registry.list()[0]
        assert record.status is TransferStatus.QUEUED
        assert record.as_dict()["phase"] == "waiting_for_telegram"
        client.get_messages.assert_not_awaited()
        assert staging_usage(manager) == (0, 0)
        client.is_connected = True
        reconnected.set()
        assert record.task is not None
        await asyncio.wait_for(record.task, timeout=3)
        assert record.status is TransferStatus.COMPLETED
        assert client.downloads == [1]
        assert len(manager.store.list_directory("/")) == 1
    finally:
        reconnected.set()
        await manager.close()
        await mode.stop()
        await dispose(manager)


async def test_staged_file_restarts_publication_without_original_source(tmp_path):
    mode, manager, client = await configured_intake(tmp_path)
    source = client.source(1)
    try:
        await mode._file(mode.client, source)
        record = manager.registry.list()[0]
        path = Path(record.context["local_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 80)
        record.context.update(
            kind="upload", size=80, staging_size=80, staging_written=80
        )
        await manager.registry.persist(record)
        client.sources.clear()
        await mode.start()
        await settled(mode.intake)
        assert record.status is TransferStatus.COMPLETED
        client.get_messages.assert_not_awaited()
        assert not path.exists()
        assert len(manager.store.list_directory("/")) == 1
    finally:
        await manager.close()
        await mode.stop()
        await dispose(manager)


async def test_explicit_provider_wait_is_durable_and_blocks_new_requests(tmp_path):
    from telethon.errors import FloodWaitError

    mode, manager, client = await configured_intake(tmp_path)
    source = client.source(1)
    lookup = client.lookup
    calls = 0

    async def limited(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FloodWaitError(request=None, capture=100)
        return await lookup(*args, **kwargs)

    client.get_messages.side_effect = limited
    try:
        await mode._file(mode.client, source)
        record = manager.registry.list()[0]
        await mode.start()
        async with asyncio.timeout(2):
            while record.context.get("bot_retry_at", 0) <= time.time():  # noqa: ASYNC110 - bounded journal observation.
                await asyncio.sleep(0.01)
        assert calls == 1
        assert record.status is TransferStatus.QUEUED
        assert record.phase == "waiting_for_telegram"
        assert manager.registry._mongo is not None
        saved = manager.registry._mongo.find_one({"_id": record.id})
        assert (
            saved["data"]["context"]["bot_retry_at"] == record.context["bot_retry_at"]
        )
        record.context["bot_retry_at"] = time.time() - 1
        await manager.registry.persist(record)
        assert mode.intake is not None
        mode.intake.wake()
        await settled(mode.intake)
        assert record.status is TransferStatus.COMPLETED
        assert calls == 2
    finally:
        await manager.close()
        await mode.stop()
        await dispose(manager)
