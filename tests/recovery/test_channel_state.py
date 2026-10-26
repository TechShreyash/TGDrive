"""Telegram configuration changes are checked again after initial setup."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from tests.core.test_runtime import FakeBackup, FakePool, FakeRemote, settings
from tests.core.test_runtime import mongo_backend as mongo_backend_fixture
from tgdrive.core.runtime import Runtime
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.channel_state import storage_bot_rights_problem
from tgdrive.telegram.clients import Client

mongo_backend = mongo_backend_fixture

CHANNEL = -1001234567890


@pytest.mark.parametrize("require_pin", [False, True])
async def test_live_adapter_checks_channel_privacy_retention_and_required_rights(
    require_pin,
):
    chat = types.Channel(
        id=1234567890,
        title="Storage",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=True,
        admin_rights=types.ChatAdminRights(
            post_messages=True, delete_messages=True, edit_messages=require_pin
        ),
    )
    full_chat = SimpleNamespace(ttl_period=None)

    class Native:
        def __init__(self):
            self.requests = []

        async def get_input_entity(self, channel):
            assert channel == CHANNEL
            return types.InputPeerChannel(channel_id=chat.id, access_hash=123)

        async def __call__(self, request):
            self.requests.append(request)
            assert isinstance(request, functions.channels.GetFullChannelRequest)
            return SimpleNamespace(chats=[chat], full_chat=full_chat)

    client = Client.__new__(Client)
    client.native = Native()
    assert (
        await client.storage_channel_problem(CHANNEL, require_pin=require_pin)
    ) is None
    full_chat.ttl_period = 86400
    problem = await client.storage_channel_problem(CHANNEL, require_pin=require_pin)
    assert problem[0] == "CHANNEL_AUTO_DELETE"
    full_chat.ttl_period = None
    chat.username = "public_storage"
    problem = await client.storage_channel_problem(CHANNEL, require_pin=require_pin)
    assert problem[0] == "CHANNEL_PUBLIC"
    chat.username = None
    chat.admin_rights.delete_messages = False
    problem = await client.storage_channel_problem(CHANNEL, require_pin=require_pin)
    assert problem[0] == "CHANNEL_RIGHTS"
    assert len(client.native.requests) == 4


@pytest.mark.parametrize("right", ["post_messages", "delete_messages", "edit_messages"])
def test_bot_rights_match_checkpoint_mode(right):
    rights = SimpleNamespace(
        post_messages=True, delete_messages=True, edit_messages=True
    )
    setattr(rights, right, False)
    chat = SimpleNamespace(admin_rights=rights)
    assert storage_bot_rights_problem(chat, require_pin=True)[0] == "CHANNEL_RIGHTS"
    assert bool(storage_bot_rights_problem(chat, require_pin=False)) == (
        right != "edit_messages"
    )


class ChannelPool(FakePool):
    def __init__(self, *problems):
        super().__init__()
        self.clients = {
            f"storage-{index}": SimpleNamespace(
                storage_channel_problem=AsyncMock(return_value=problem)
            )
            for index, problem in enumerate(problems)
        }

    async def status(self):
        return [
            SimpleNamespace(name=name, kind="bot", healthy=True)
            for name in self.clients
        ]

    @asynccontextmanager
    async def lease(self, *, client_name):
        yield self.clients[client_name]


async def test_runtime_rechecks_every_bot_and_failed_checks_allow_immediate_retry(
    tmp_path,
):
    pool = ChannelPool(None, ("CHANNEL_RIGHTS", "Restore Delete Messages permission."))
    runtime = Runtime(settings(tmp_path), pool=pool)
    with pytest.raises(SettingsProblem, match="Delete Messages"):
        await runtime._verify_storage_channel()
    assert runtime.storage_channel_error == "Restore Delete Messages permission."
    pool.clients["storage-1"].storage_channel_problem.return_value = None
    await runtime._verify_storage_channel()
    assert runtime.storage_channel_error is None
    for client in pool.clients.values():
        assert client.storage_channel_problem.await_count == 2
        client.storage_channel_problem.assert_awaited_with(CHANNEL, require_pin=False)
    await runtime._verify_storage_channel()
    assert pool.clients["storage-0"].storage_channel_problem.await_count == 2


async def test_channel_probe_network_error_is_safe_and_retryable(tmp_path):
    pool = ChannelPool(None)
    probe = pool.clients["storage-0"].storage_channel_problem
    probe.side_effect = TimeoutError("sensitive request details")
    runtime = Runtime(settings(tmp_path), pool=pool)
    with pytest.raises(ConnectionError, match="could not be verified") as error:
        await runtime._verify_storage_channel()
    assert "sensitive" not in str(error.value)
    probe.side_effect = None
    await runtime._verify_storage_channel()
    assert runtime.storage_channel_error is None


async def test_unsafe_channel_preserves_metadata_and_pauses_startup_recovery(
    tmp_path, monkeypatch
):
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    store.create_folder("/", "Preserve")
    pool = ChannelPool(("CHANNEL_AUTO_DELETE", "Turn auto-delete off."))
    runtime = Runtime(settings(tmp_path), pool=pool)
    runtime.backup = FakeBackup(store)
    runtime.remote = FakeRemote()
    monkeypatch.setattr("tgdrive.core.runtime.build_bot_mode_client", lambda _: None)
    try:
        await runtime.start()
        assert runtime.ready
        assert runtime.store.list_directory("/")
        assert runtime.backup.backups == 0
        assert runtime.storage_channel_error == "Turn auto-delete off."
    finally:
        await runtime.stop()
