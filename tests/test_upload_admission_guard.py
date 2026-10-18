"""Append admission does not borrow remote publication cooldowns or leases."""

import time

import pytest
from test_backup import service_for, settings

from tgdrive.backup import BackupError, RecoveryConflict, TelegramBackupService


async def test_local_upload_admission_survives_publication_cooldown(
    tmp_path, monkeypatch
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    await service.ensure_revision(store, store.revision)
    store.create_folder("/", "Pending metadata")
    with service._history() as connection:
        connection.execute(
            "INSERT INTO publication_state VALUES(1,?)",
            (time.time() + 60,),
        )
        connection.commit()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Append admission must not read Telegram")

    monkeypatch.setattr(client, "get_pinned_message_id", forbidden)
    service.check_upload_allowed(store)
    with pytest.raises(BackupError, match="waiting before retry"):
        await service.ensure_revision(store, store.revision)
    assert not service.recovery_required
    store.close()
    await pool.stop()


async def test_confirmed_conflict_blocks_local_upload_admission(tmp_path):
    service, pool, _ = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    service._conflict("A newer remote catalog requires recovery")
    with pytest.raises(RecoveryConflict, match="requires recovery"):
        service.check_upload_allowed(store)
    store.close()
    await pool.stop()


async def test_changed_channel_blocks_local_upload_without_rpc(tmp_path):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    store.bind_storage_channel(service._settings.storage_channel)
    changed = settings(tmp_path).model_copy(update={"storage_channel": -100999})
    restarted = TelegramBackupService(changed, pool)
    with pytest.raises(RecoveryConflict, match="another storage channel"):
        restarted.check_upload_allowed(store)
    assert client.requests == []
    assert restarted.recovery_required
    store.close()
    await pool.stop()
