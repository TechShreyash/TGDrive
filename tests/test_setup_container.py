"""Container-side onboarding uses real stores/checkpoints with an isolated transport."""

import fcntl
import json
import time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from argon2 import PasswordHasher
from telethon import types
from test_backup import FakeBackupClient, settings

import tgdrive.setup as helper
from tgdrive.backup import TelegramBackupService
from tgdrive.client_pool import ClientPool
from tgdrive.store import DriveStore
from tgdrive.tasks import TransferRegistry, TransferStatus


class Native:
    def __init__(self, client):
        self.client = client
        self.rights = SimpleNamespace(
            post_messages=True, edit_messages=True, delete_messages=True
        )
        self.broadcast = True

    async def get_input_entity(self, _channel):
        return types.InputPeerChannel(channel_id=1234567890, access_hash=123)

    async def __call__(self, request):
        return SimpleNamespace(
            chats=[
                SimpleNamespace(
                    id=1234567890, broadcast=self.broadcast, admin_rights=self.rights
                )
            ],
            full_chat=SimpleNamespace(pinned_msg_id=self.client.pinned),
        )


def isolated_pool(monkeypatch):
    client = FakeBackupClient()
    client.native = Native(client)
    pool = ClientPool([client])
    monkeypatch.setattr(helper, "build_client_pool", lambda _: pool)
    return pool, client


async def test_inspect_checks_rights_without_creating_drive_or_publishing(
    tmp_path, monkeypatch
):
    _, client = isolated_pool(monkeypatch)
    configured = settings(tmp_path)
    status = await helper.prepare_installation(configured, "inspect")
    assert status == {
        "local_state": "missing",
        "has_pin": False,
        "checkpoint_candidate": False,
    }
    assert not (configured.resolved_data_dir / "drive.sqlite3").exists()
    assert client.requests == []
    assert client.pinned is None


async def test_explicit_new_prepares_protected_drive_and_existing_reuses_it(
    tmp_path, monkeypatch
):
    _, client = isolated_pool(monkeypatch)
    configured = settings(tmp_path)
    assert await helper.prepare_installation(configured, "prepare", mode="new") == {
        "state": "protected",
        "mode": "new",
    }
    store = DriveStore.load(configured.resolved_data_dir / "drive.sqlite3")
    identity = store.drive_id
    assert store.storage_channel_id == configured.storage_channel
    store.close()
    count = len(client.sent)
    assert client.pinned is not None
    assert await helper.prepare_installation(
        configured, "prepare", mode="existing"
    ) == {"state": "protected", "mode": "existing"}
    assert len(client.sent) == count
    store = DriveStore.load(configured.resolved_data_dir / "drive.sqlite3")
    assert store.drive_id == identity
    store.close()


@pytest.mark.parametrize("mode", ["new", "recover"])
async def test_existing_local_database_never_reinitialized_or_overwritten(
    tmp_path, monkeypatch, mode
):
    _, client = isolated_pool(monkeypatch)
    configured = settings(tmp_path)
    store = DriveStore.create(configured.resolved_data_dir / "drive.sqlite3")
    store.create_folder("/", "Preserve")
    identity = store.drive_id
    store.close()
    with pytest.raises(helper.SetupError) as error:
        await helper.prepare_installation(configured, "prepare", mode=mode)
    assert error.value.code == "LOCAL_STATE_EXISTS"
    store = DriveStore.load(configured.resolved_data_dir / "drive.sqlite3")
    assert store.drive_id == identity
    assert store.list_directory("/")
    store.close()
    assert client.requests == []


async def test_any_remote_pin_refuses_new_drive_including_unrelated_pin(
    tmp_path, monkeypatch
):
    _, client = isolated_pool(monkeypatch)
    configured = settings(tmp_path)
    client.pinned = 999
    with pytest.raises(helper.SetupError) as error:
        await helper.prepare_installation(configured, "prepare", mode="new")
    assert error.value.code == "CHANNEL_HAS_PIN"
    assert not (configured.resolved_data_dir / "drive.sqlite3").exists()
    assert client.requests == []


@pytest.mark.parametrize("right", ["post_messages", "edit_messages", "delete_messages"])
async def test_missing_administrator_permission_prevents_initialization(
    tmp_path, monkeypatch, right
):
    _, client = isolated_pool(monkeypatch)
    setattr(client.native.rights, right, False)
    configured = settings(tmp_path)
    with pytest.raises(helper.SetupError) as error:
        await helper.prepare_installation(configured, "prepare", mode="new")
    assert error.value.code == "CHANNEL_RIGHTS"
    assert client.requests == []
    assert not (configured.resolved_data_dir / "drive.sqlite3").exists()


async def test_group_is_not_mistaken_for_storage_channel(tmp_path, monkeypatch):
    _, client = isolated_pool(monkeypatch)
    client.native.broadcast = False
    with pytest.raises(helper.SetupError, match="rather than a group"):
        await helper.prepare_installation(settings(tmp_path), "prepare", mode="new")


async def test_corrupt_local_metadata_is_preserved_before_network_start(
    tmp_path, monkeypatch
):
    pool, client = isolated_pool(monkeypatch)
    configured = settings(tmp_path)
    configured.resolved_data_dir.mkdir(parents=True)
    target = configured.resolved_data_dir / "drive.sqlite3"
    target.write_bytes(b"preserve damaged operator database")
    start = AsyncMock(side_effect=AssertionError("Must reject local damage first"))
    monkeypatch.setattr(pool, "start", start)
    with pytest.raises(helper.SetupError) as error:
        await helper.prepare_installation(configured, "prepare", mode="new")
    assert error.value.code == "LOCAL_STATE_INVALID"
    assert target.read_bytes() == b"preserve damaged operator database"
    assert client.requests == []
    start.assert_not_awaited()


async def test_running_installation_lock_prevents_setup_mutations(
    tmp_path, monkeypatch
):
    _, client = isolated_pool(monkeypatch)
    configured = settings(tmp_path)
    configured.resolved_data_dir.mkdir(parents=True)
    with (configured.resolved_data_dir / ".instance.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(helper.SetupError) as error:
            await helper.prepare_installation(configured, "prepare", mode="new")
    assert error.value.code == "ALREADY_RUNNING"
    assert client.requests == []


async def test_recovery_from_fresh_local_state_reprotects_retained_catalog(
    tmp_path, monkeypatch
):
    pool, client = isolated_pool(monkeypatch)
    original = settings(tmp_path / "original")
    await helper.prepare_installation(original, "prepare", mode="new")
    store = DriveStore.load(original.resolved_data_dir / "drive.sqlite3")
    item = store.add_file("/", "retained.bin", 876, 12, operation_id="completed-file")
    token = store.create_share("/" + item["id"])
    await pool.start()
    await TelegramBackupService(original, pool).ensure_revision(store, store.revision)
    identity = store.drive_id
    store.close()
    await pool.stop()
    fresh = settings(tmp_path / "fresh")
    result = await helper.prepare_installation(fresh, "prepare", mode="recover")
    assert result["state"] == "protected"
    restored = DriveStore.load(fresh.resolved_data_dir / "drive.sqlite3")
    assert restored.drive_id == identity
    assert restored.get_file("/" + item["id"])["message_id"] == 876
    assert restored.has_file_operation("completed-file")
    assert not restored.authorize_file("/" + item["id"], token)
    assert restored.storage_channel_id == fresh.storage_channel
    assert client.pinned is not None
    restored.close()


def test_password_hash_private_stdin_has_no_settings_or_telegram_requirement(
    monkeypatch,
):
    password = "a fixture password kept private"
    monkeypatch.setattr(
        helper.sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(json.dumps({"password": password}).encode())),
    )
    result = helper.password_hash()
    assert PasswordHasher().verify(result["hash"], password)
    assert password not in result["hash"]


def test_invalid_config_diagnostics_never_include_input_credentials(
    monkeypatch, capsys
):
    invalid = "fixture-secret-that-must-not-be-displayed"
    monkeypatch.setattr(helper.sys, "argv", ["setup", "inspect"])

    def invalid_configuration():
        return helper.Settings(_env_file=None, API_ID=0, API_HASH=invalid)

    monkeypatch.setattr(helper, "Settings", invalid_configuration)
    # Call the real Settings class through a saved reference rather than recursion.
    from tgdrive.config import Settings

    monkeypatch.setattr(
        helper, "Settings", lambda: Settings(_env_file=None, API_ID=0, API_HASH=invalid)
    )
    assert helper.main() == 1
    output = capsys.readouterr().out
    assert invalid not in output
    assert json.loads(output)["code"] == "CONFIG_INVALID"


@pytest.mark.parametrize("publication_cooldown", [False, True])
async def test_setup_restores_all_provider_waits_before_start_and_publication(
    tmp_path,
    monkeypatch,
    publication_cooldown,
):
    pool, client = isolated_pool(monkeypatch)
    configured = settings(tmp_path)
    DriveStore.create(configured.resolved_data_dir / "drive.sqlite3").close()
    deadline = time.time() + 30
    publication_deadline = deadline + 10 if publication_cooldown else deadline
    journal = TransferRegistry(
        database_path=configured.resolved_data_dir / "jobs.sqlite3",
    )
    record = journal.create("retry_wait")
    record.context.update(
        kind="upload",
        channel_retry_at=deadline,
        multipart_plan={"parts": [{"sender": client.name, "retry_at": deadline}]},
    )
    journal.update(record.id, status=TransferStatus.ERROR)
    journal.close()
    if publication_cooldown:
        service = TelegramBackupService(configured, pool)
        with service._history() as db:
            db.execute(
                "INSERT INTO publication_state VALUES(1,?)", (publication_deadline,)
            )
            db.commit()
    original_start = pool.start
    observed = []

    async def checked_start():
        assert pool._channel_cooldown_until == publication_deadline
        assert pool._account_cooldowns[client.name] == deadline
        observed.append("start")
        return await original_start()

    async def checked_publish(self, store, required):
        assert observed == ["start"]
        assert pool._channel_cooldown_until == publication_deadline
        assert pool._slots[0].cooldown_until == deadline
        observed.append("publication")
        return required

    monkeypatch.setattr(pool, "start", checked_start)
    monkeypatch.setattr(TelegramBackupService, "ensure_revision", checked_publish)
    result = await helper.prepare_installation(configured, "prepare", mode="existing")
    assert result["state"] == "protected"
    assert observed == ["start", "publication"]
    assert client.requests == []
    restored = TransferRegistry(
        database_path=configured.resolved_data_dir / "jobs.sqlite3"
    )
    assert restored.get(record.id).status == TransferStatus.ERROR
    restored.close()


async def test_setup_preserves_invalid_job_journal_before_network_start(
    tmp_path, monkeypatch
):
    pool, client = isolated_pool(monkeypatch)
    configured = settings(tmp_path)
    configured.resolved_data_dir.mkdir(parents=True)
    target = configured.resolved_data_dir / "jobs.sqlite3"
    target.write_bytes(b"preserve invalid transfer journal")
    start = AsyncMock(
        side_effect=AssertionError("Do not connect with invalid local state")
    )
    monkeypatch.setattr(pool, "start", start)
    with pytest.raises(helper.SetupError) as error:
        await helper.prepare_installation(configured, "inspect")
    assert error.value.code == "LOCAL_JOURNAL_INVALID"
    assert target.read_bytes() == b"preserve invalid transfer journal"
    start.assert_not_awaited()
    assert client.requests == []


async def test_mongodb_channel_preflight_does_not_require_pin_rights(
    tmp_path, monkeypatch
):
    from pydantic import SecretStr

    pool, client = isolated_pool(monkeypatch)
    configured = settings(tmp_path).model_copy(
        update={"mongodb_uri": SecretStr("mongodb://localhost:27017/")}
    )
    client.native.rights.edit_messages = False
    await pool.start()
    try:
        assert await helper.check_channel(configured, pool) == (None, None)
        assert client.requests == []
    finally:
        await pool.stop()
