"""Persisted checkpoint FloodWait must protect every writer after restart."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.errors import FloodWaitError
from test_backup import FakeBackupClient, service_for, settings

from tgdrive.backup import BackupError, TelegramBackupService
from tgdrive.client_pool import ClientPool
from tgdrive.runtime import Runtime
from tgdrive.store import DriveStore
from tgdrive.tasks import TransferRegistry, TransferStatus


@pytest.mark.parametrize("checkpoint_only", [False, True])
async def test_restart_rehydrates_backup_wait_into_shared_channel_gate(
    tmp_path, checkpoint_only
):
    config = settings(tmp_path)
    pool = ClientPool([FakeBackupClient()])
    await pool.start()
    service = TelegramBackupService(config, pool)
    deadline = time.time() + 30
    store = DriveStore.create()
    try:
        if checkpoint_only:
            pending = service._prepare_checkpoint(store)
            with service._history() as db:
                db.execute(
                    "UPDATE checkpoints SET retry_at=? WHERE id=?",
                    (deadline, pending["id"]),
                )
                db.commit()
        else:
            with service._history() as db:
                db.execute("INSERT INTO publication_state VALUES(1,?)", (deadline,))
                db.commit()
        restarted = TelegramBackupService(config, pool)
        await restarted.restore_publication_cooldown()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(pool.pace_send(config.storage_channel), timeout=0.01)
        assert pool._channel_cooldown_until == deadline
    finally:
        store.close()
        await pool.stop()


async def test_live_backup_flood_defers_other_channel_writers_immediately(
    tmp_path, monkeypatch
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()

    async def flood(*_args, **_kwargs):
        raise FloodWaitError(request=None, capture=30)

    monkeypatch.setattr(client, "pin_message", flood)
    try:
        with pytest.raises(BackupError) as error:
            await service.ensure_revision(store, store.revision)
        assert error.value.retry_after == 30
        assert pool._channel_cooldown_until > time.time() + 28
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                pool.pace_send(service._settings.storage_channel), timeout=0.01
            )
    finally:
        store.close()
        await pool.stop()


@pytest.mark.parametrize("journal_cooldown", [False, True])
async def test_runtime_restores_publication_wait_before_startup_backup_or_imports(
    tmp_path,
    journal_cooldown,
):
    config = settings(tmp_path)
    config.resolved_data_dir.mkdir(parents=True)
    DriveStore.create(config.resolved_data_dir / "drive.sqlite3").close()
    deadline = time.time() + 30
    expected_deadline = deadline + 10 if journal_cooldown else deadline
    if journal_cooldown:
        journal = TransferRegistry(
            database_path=config.resolved_data_dir / "jobs.sqlite3"
        )
        record = journal.create("retry_wait")
        record.context.update(
            kind="upload",
            channel_retry_at=expected_deadline,
            multipart_plan={
                "parts": [{"sender": "bot-one", "retry_at": expected_deadline}]
            },
        )
        journal.update(record.id, status=TransferStatus.ERROR)
        journal.close()

    class Pool:
        deferred = 0
        started = False
        account_deferred = None

        async def set_uploads_per_bot(self, value):
            self.uploads_per_bot = value

        async def defer_client(self, sender, until):
            assert not self.started
            assert sender == "bot-one"
            self.account_deferred = until

        async def defer_channel(self, until):
            assert not self.started
            self.deferred = max(self.deferred, until)

        async def start(self):
            assert self.deferred == expected_deadline
            if journal_cooldown:
                assert self.account_deferred == expected_deadline
            self.started = True

        async def stop(self, **_):
            return ()

    pool = Pool()
    runtime = Runtime(config, pool=pool)
    with runtime.backup._history() as db:
        db.execute("INSERT INTO publication_state VALUES(1,?)", (deadline,))
        db.commit()

    async def blocked_backup(_store):
        assert pool.started and pool.deferred == expected_deadline
        raise BackupError("Synthetic persisted publication cooldown", retry_after=30)

    runtime.backup.backup = blocked_backup
    runtime.backup.start = Mock()
    runtime.backup.stop = AsyncMock()
    try:
        await runtime.start()
        assert runtime.ready
        assert runtime.bot_mode is None
    finally:
        await runtime.stop()
