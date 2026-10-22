from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import sqlite3
import uuid
from contextlib import closing

import pytest
from telethon.errors import FloodWaitError
from test_backup import FakeBackupClient, service_for, settings

from tgdrive.__main__ import recover_telegram
from tgdrive.backup import BackupError, TelegramBackupService
from tgdrive.client_pool import ClientPool
from tgdrive.errors import ValidationError
from tgdrive.store import DriveStore


async def test_publication_orders_sender_upload_id_pin_and_protection(
    tmp_path, monkeypatch
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    original_send = client.send_document
    original_pin = client.pin_message

    async def send(*args, **kwargs):
        row = service.history(limit=1)[0]
        assert row["sender"] == client.name
        assert row["message_id"] is None
        assert row["status"] == "pending"
        assert kwargs["caption"].startswith("TGDrive checkpoint v1\n")
        assert service.protection_status(store)["state"] == "pending"
        return await original_send(*args, **kwargs)

    async def pin(channel, message_id, **kwargs):
        row = service.history(limit=1)[0]
        assert row["message_id"] == message_id
        assert row["published_at"] is None
        assert store.dirty
        return await original_pin(channel, message_id, **kwargs)

    monkeypatch.setattr(client, "send_document", send)
    monkeypatch.setattr(client, "pin_message", pin)
    assert await service.ensure_revision(store, store.revision) == store.revision
    status = service.protection_status(store)
    assert status["state"] == "protected"
    assert status["protected_revision"] == store.revision
    assert status["checkpoint_message_id"] == client.pinned
    assert service.history(limit=1)[0]["published_at"]
    store.create_folder("/", "New metadata")
    assert service.protection_status(store)["state"] == "pending"
    store.close()
    await pool.stop()


async def test_complete_local_loss_recovers_catalog_ids_and_revokes_shares(tmp_path):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    tree = store.prepare_folder_tree("/", "Documents", [["Reports"]], str(uuid.uuid4()))
    file = store.add_file(
        tree["folders"]["Reports"],
        "report.pdf",
        1234,
        456,
        operation_id="completed-upload-id",
    )
    file_path = tree["folders"]["Reports"] + "/" + file["id"]
    token = store.create_share(tree["folders"][""])
    identity = store.drive_id
    await service.ensure_revision(store, store.revision)
    pinned = client.pinned
    store.close()
    # Only test-owned temporary state is removed. Telegram's simulated state survives.
    shutil.rmtree(service._settings.resolved_data_dir)
    fresh = FakeBackupClient()
    fresh.sent = copy.deepcopy(client.sent)
    fresh.captions = dict(client.captions)
    fresh.pinned = pinned
    fresh_pool = ClientPool([fresh])
    await fresh_pool.start()
    recovery = TelegramBackupService(settings(tmp_path, initialize=True), fresh_pool)
    assert not recovery.store_path.exists()
    restored = await recovery.recover_latest()
    assert restored.drive_id == identity
    assert restored.get_file(file_path)["message_id"] == 1234
    assert restored.has_file_operation("completed-upload-id")
    assert not restored.authorize_file(file_path, token)
    assert restored.list_shares() == []
    assert restored.export()["folder_preparations"]
    assert not fresh.sent.keys() - client.sent.keys(), (
        "Recovery is read-only on Telegram"
    )
    assert not (service._settings.resolved_data_dir / "jobs.sqlite3").exists()
    restored.close()
    await fresh_pool.stop()
    await pool.stop()


async def test_pin_failure_retries_only_pin_without_reuploading_document(
    tmp_path, monkeypatch
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    original = client.pin_message
    calls = 0

    async def pin(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("private provider data must not reach status")
        return await original(*args, **kwargs)

    monkeypatch.setattr(client, "pin_message", pin)
    with pytest.raises(BackupError, match="PermissionError"):
        await service.ensure_revision(store, store.revision)
    assert service.protection_status(store)["state"] == "error"
    assert "private provider" not in service.protection_status(store)["error"]
    assert store.dirty
    pending = service.history(limit=1)[0]
    assert pending["message_id"] == 100 and pending["sender"] == client.name
    restarted = TelegramBackupService(settings(tmp_path), pool)
    await restarted.ensure_revision(store, store.revision)
    assert len(client.requests) == 1
    assert len(client.sent) == 1
    assert not store.dirty
    store.close()
    await pool.stop()


async def test_lost_pin_response_is_reconciled_from_current_pin(tmp_path, monkeypatch):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    original = client.pin_message

    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise TimeoutError("accepted pin response lost")

    monkeypatch.setattr(client, "pin_message", lost)
    with pytest.raises(BackupError, match="TimeoutError"):
        await service.ensure_revision(store, store.revision)
    restarted = TelegramBackupService(settings(tmp_path), pool)
    assert await restarted.ensure_revision(store, store.revision) == store.revision
    assert len(client.requests) == 1
    assert restarted.history()[0]["status"] == "completed"
    store.close()
    await pool.stop()


async def test_pin_readback_is_required_not_just_send_acceptance(tmp_path, monkeypatch):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()

    async def ignored(*_args, **_kwargs):
        pass

    monkeypatch.setattr(client, "pin_message", ignored)
    with pytest.raises(BackupError, match="confirm the recovery pin"):
        await service.ensure_revision(store, store.revision)
    assert store.dirty
    assert service.history()[0]["status"] == "pending"
    assert service.protection_status(store)["protected_revision"] == 0
    store.close()
    await pool.stop()


async def test_floodwait_deadline_survives_restart_and_no_extra_rpc(
    tmp_path, monkeypatch
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    original = client.pin_message

    async def flood(*_args, **_kwargs):
        raise FloodWaitError(request=None, capture=30)

    monkeypatch.setattr(client, "pin_message", flood)
    with pytest.raises(BackupError) as raised:
        await service.ensure_revision(store, store.revision)
    assert raised.value.retry_after == 30
    assert service.history()[0]["retry_at"] > 0
    restarted = TelegramBackupService(settings(tmp_path), pool)

    async def forbidden(*_args):
        raise AssertionError("cooldown must be checked before Telegram")

    monkeypatch.setattr(client, "get_pinned_message_id", forbidden)
    with pytest.raises(BackupError, match="waiting") as waiting:
        await restarted.ensure_revision(store, store.revision)
    assert 0 < waiting.value.retry_after <= 30
    monkeypatch.undo()
    monkeypatch.setattr(client, "pin_message", original)
    with restarted._history() as db:
        db.execute("UPDATE checkpoints SET retry_at=0")
        db.execute("UPDATE publication_state SET retry_at=0")
        db.commit()
    await restarted.ensure_revision(store, store.revision)
    assert len(client.requests) == 1
    assert restarted.history()[0]["retry_at"] is None
    store.close()
    await pool.stop()


async def test_concurrent_barriers_coalesce_and_old_completed_flags_do_not_bypass(
    tmp_path,
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    store.mark_backed_up(store.revision)
    assert not store.dirty
    assert service.protection_status(store)["state"] == "pending"
    first = store.revision
    store.create_folder("/", "Two commits share publication")
    values = await asyncio.gather(
        service.ensure_revision(store, first),
        service.ensure_revision(store, store.revision),
    )
    assert values == [store.revision, store.revision]
    assert len(client.sent) == 1
    assert client.pinned == 100
    store.close()
    await pool.stop()


@pytest.mark.parametrize(
    "case", ["unmarked", "checksum", "channel", "drive", "revision", "schema"]
)
async def test_automatic_recovery_rejects_invalid_pins_without_creating_db(
    tmp_path, case
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    await service.ensure_revision(store, store.revision)
    checkpoint = service.history()[0]
    key = checkpoint["random_id"]
    message_id, content = client.sent[key]
    if case == "unmarked":
        client.captions[message_id] = "An unrelated pinned document"
    elif case == "checksum":
        client.sent[key] = (message_id, content.replace(b'"root"', b'"roox"', 1))
    elif case == "channel":
        client.captions[message_id] = client.captions[message_id].replace(
            str(service._settings.storage_channel), "-1009999999999"
        )
    elif case == "drive":
        client.captions[message_id] = client.captions[message_id].replace(
            store.drive_id, "different-drive-id"
        )
    elif case == "revision":
        client.captions[message_id] = client.captions[message_id].replace(
            f"Revision: {checkpoint['revision']}",
            f"Revision: {checkpoint['revision'] + 1}",
        )
    else:
        payload = json.loads(content)
        del payload["root"]
        content = json.dumps(payload).encode()
        client.sent[key] = (message_id, content)
        client.captions[message_id] = client.captions[message_id].replace(
            checkpoint["sha256"], hashlib.sha256(content).hexdigest()
        )
    target = tmp_path / "recovered.sqlite3"
    with pytest.raises(BackupError):
        await service.recover_latest(destination=target)
    assert not target.exists()
    store.close()
    await pool.stop()


async def test_existing_database_or_corrupt_target_is_never_overwritten(tmp_path):
    service, pool, _ = await service_for(tmp_path, initialize=True)
    before = service.store_path.read_bytes()
    with pytest.raises(FileExistsError):
        await service.recover_latest()
    assert service.store_path.read_bytes() == before
    corrupt = tmp_path / "preserve.sqlite3"
    corrupt.write_bytes(b"corrupt but operator-owned data")
    with pytest.raises(FileExistsError):
        await service.recover_latest(destination=corrupt)
    assert corrupt.read_bytes() == b"corrupt but operator-owned data"
    await pool.stop()


async def test_publish_refuses_another_drive_and_newer_or_forked_catalog(tmp_path):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    old = store.export()
    store.create_folder("/", "Newer remote state")
    await service.ensure_revision(store, store.revision)
    sent = len(client.sent)
    other = DriveStore.create()
    with pytest.raises(BackupError, match="another drive"):
        await service.ensure_revision(other, other.revision)
    older = DriveStore._from_payload(old)
    with pytest.raises(BackupError, match="newer catalog"):
        await service.ensure_revision(older, older.revision)
    fork = store.export()
    next(iter(fork["root"]["children"].values()))["name"] = "Forked same revision"
    forked = DriveStore._from_payload(fork)
    with pytest.raises(BackupError, match="conflict"):
        await service.ensure_revision(forked, forked.revision)
    assert len(client.sent) == sent
    for current in (store, other, older, forked):
        current.close()
    await pool.stop()


def test_drive_identity_and_file_operation_tombstones_roundtrip(tmp_path):
    database = tmp_path / "drive.sqlite3"
    store = DriveStore.create(database)
    identity = store.drive_id
    file = store.add_file("/", "finished.bin", 7, 8, operation_id="finished-operation")
    assert store.has_file_operation("finished-operation")
    assert not store.has_file_operation("unknown-operation")
    path = "/" + file["id"]
    store.set_trashed(path, True)
    intent = store.begin_deletion(path, channel_id=-1001)
    store.confirm_deletion_messages(intent["id"], [7])
    store.finish_deletion(intent["id"])
    assert store.has_file_operation("finished-operation")
    with pytest.raises(ValidationError):
        store.has_file_operation("../unsafe")
    restored = DriveStore._from_payload(store.export())
    assert restored.drive_id == identity
    assert restored.has_file_operation("finished-operation")
    restored.close()
    store.close()
    loaded = DriveStore.load(database)
    assert loaded.drive_id == identity
    loaded.close()


async def test_cli_recovery_checks_existing_target_before_client_start(
    tmp_path, monkeypatch
):
    configured = settings(tmp_path)
    configured.resolved_data_dir.mkdir(parents=True)
    target = configured.resolved_data_dir / "drive.sqlite3"
    target.write_bytes(b"preserve existing state")

    def forbidden(_settings):
        raise AssertionError("Existing state must be rejected before connecting")

    monkeypatch.setattr("tgdrive.__main__.build_client_pool", forbidden)
    with pytest.raises(FileExistsError):
        await recover_telegram(configured)
    assert target.read_bytes() == b"preserve existing state"


def test_v2_database_migration_preserves_nodes_and_adds_persistent_identity(tmp_path):
    database = tmp_path / "old.sqlite3"
    original = DriveStore.create(database)
    node = original.add_file("/", "old.bin", 98, 76)
    revision = original.revision
    original.close()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE metadata RENAME TO old_metadata")
        connection.execute(
            "CREATE TABLE metadata (singleton INTEGER PRIMARY KEY, "
            "revision INTEGER NOT NULL, backed_up_revision INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO metadata SELECT singleton,revision,backed_up_revision "
            "FROM old_metadata"
        )
        connection.execute("DROP TABLE old_metadata")
        connection.execute("ALTER TABLE nodes DROP COLUMN starred")
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    migrated = DriveStore.load(database)
    identity = migrated.drive_id
    assert identity
    assert migrated.revision == revision + 1  # v4 portable format has a new revision.
    assert migrated.get_file("/" + node["id"])["message_id"] == 98
    migrated.close()
    reloaded = DriveStore.load(database)
    assert reloaded.drive_id == identity
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == DriveStore.DATABASE_VERSION
        )
    reloaded.close()


def test_legacy_portable_checkpoint_gets_new_persistent_identity(tmp_path):
    old = DriveStore.create()
    payload = old.export()
    del payload["drive_id"]
    old.close()
    target = tmp_path / "restored.sqlite3"
    restored = DriveStore._from_payload(payload, storage_path=target, persist=True)
    identity = restored.drive_id
    restored.close()
    loaded = DriveStore.load(target)
    assert loaded.drive_id == identity
    loaded.close()


async def test_pending_checkpoint_channel_change_is_rejected_before_send(tmp_path):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    service._prepare_checkpoint(store)
    changed = settings(tmp_path).model_copy(update={"storage_channel": -100999})
    restarted = TelegramBackupService(changed, pool)
    with pytest.raises(BackupError, match="another storage channel"):
        await restarted.ensure_revision(store, store.revision)
    assert client.requests == []
    assert client.pinned is None
    store.close()
    await pool.stop()


async def test_discovery_floodwait_persists_before_any_checkpoint_exists(
    tmp_path, monkeypatch
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()

    async def flood(*args, **kwargs):
        raise FloodWaitError(request=None, capture=60)

    monkeypatch.setattr(client, "get_pinned_message_id", flood)
    with pytest.raises(BackupError) as first:
        await service.ensure_revision(store, store.revision)
    assert first.value.retry_after == 60
    assert service.history() == []
    restarted = TelegramBackupService(settings(tmp_path), pool)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Persisted flood wait must prevent discovery RPCs")

    monkeypatch.setattr(client, "get_pinned_message_id", forbidden)
    with pytest.raises(BackupError, match="waiting before retry") as second:
        await restarted.ensure_revision(store, store.revision)
    assert second.value.retry_after > 0
    assert client.requests == []
    store.close()
    await pool.stop()
