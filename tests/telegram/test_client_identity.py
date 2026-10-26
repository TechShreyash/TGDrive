"""Optional names are cached, nonblocking, retryable and separate from auth."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError

from tests.support.mongo_fixtures import register_test_backend
from tests.telegram.test_telegram_clients import native_transport as native_transport
from tests.telegram.test_telegram_clients import settings
from tgdrive.telegram.client_identity import IdentityCache
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import Client

pytestmark = pytest.mark.usefixtures("mongo_backend")


@pytest.fixture
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, settings(tmp_path))


def client_for(tmp_path, name="Drive Bot", username="drive_bot"):
    config = settings(tmp_path)
    client = Client(name="unused", settings=config, bot_token=config.bot_tokens[0])
    client.native.me.first_name = name
    client.native.me.username = username
    client.native.get_me = AsyncMock(return_value=client.native.me)
    return client


async def settled(client):
    await asyncio.wait_for(asyncio.shield(client._identity_task), 2)


async def test_cache_loading_cannot_delay_authenticated_client_start(
    tmp_path, monkeypatch
):
    client = client_for(tmp_path)
    release = threading.Event()
    entered = threading.Event()

    def slow_cache(_account):
        entered.set()
        release.wait(3)
        return None

    monkeypatch.setattr(client._identity_cache, "read", slow_cache)
    try:
        await asyncio.wait_for(client.start(), 1)
        assert client.is_connected
        await asyncio.to_thread(entered.wait, 1)
        assert client.identity_refreshing
        release.set()
        await settled(client)
        assert client.display_name == "Drive Bot"
        assert client.native.get_me.await_count == 1
    finally:
        release.set()
        await client.stop()


async def test_names_survive_recreation_refresh_and_status_polling_without_extra_calls(
    tmp_path,
):
    first = client_for(tmp_path, "Original नाम")
    await first.start()
    await settled(first)
    await first.stop()
    second = client_for(tmp_path, "Changed नाम", "changed_bot")
    pool = ClientPool([second])
    try:
        await pool.start()
        await settled(second)
        assert second.display_name == "Original नाम"
        for _ in range(10):
            status = (await pool.status())[0]
            assert status.display_name == "Original नाम"
            assert status.username == "drive_bot"
            assert status.name == "storage-bot-123456"
        assert second.native.get_me.await_count == 1
        pool.refresh_identities()
        pool.refresh_identities()
        await settled(second)
        assert second.display_name == "Changed नाम"
        assert second.username == "changed_bot"
        assert second.native.get_me.await_count == 2
        cached = IdentityCache(settings(tmp_path).resolved_data_dir)
        assert cached.read(second.name)["display_name"] == "Changed नाम"
        assert not (
            settings(tmp_path).resolved_data_dir / "client-identities.sqlite3"
        ).exists()
    finally:
        await pool.stop()


async def test_failed_name_lookup_retries_without_stopping_authenticated_client(
    tmp_path,
    monkeypatch,
):
    client = client_for(tmp_path, None, None)
    good = SimpleNamespace(
        id=123456, bot=True, first_name="Recovered", username="recovered_bot"
    )
    client.native.get_me.side_effect = [client.native.me, OSError("offline"), good]
    waits = []

    async def wait(delay):
        waits.append(delay)

    monkeypatch.setattr(client, "_wait_identity_retry", wait)
    try:
        await client.start()
        await settled(client)
        assert waits == [5, 10]
        assert client.is_connected
        assert client.display_name == "Recovered"
        assert client.native.get_me.await_count == 3
    finally:
        await client.stop()


async def test_refresh_floodwait_retains_name_and_full_account_cooldown(
    tmp_path,
    monkeypatch,
):
    client = client_for(tmp_path)
    pool = ClientPool([client])
    waiting = asyncio.Event()
    observed = []

    async def wait(delay):
        observed.append(delay)
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_wait_identity_retry", wait)
    try:
        await pool.start()
        await settled(client)
        client.native.get_me.side_effect = FloodWaitError(request=None, capture=3600)
        pool.refresh_identities()
        await asyncio.wait_for(waiting.wait(), 1)
        assert observed[0] >= 3600.25
        assert pool._account_cooldowns[client.name] > 0
        assert client.display_name == "Drive Bot" and client.is_connected
        pool.refresh_identities()
        assert client.native.get_me.await_count == 2
    finally:
        await pool.stop()
    assert not client.identity_refreshing


async def test_broken_optional_cache_keeps_name_and_client_alive(tmp_path, monkeypatch):
    client = client_for(tmp_path)
    waiting = asyncio.Event()

    def broken(*_args):
        raise OSError("cache unavailable")

    async def wait(_delay):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client._identity_cache, "read", broken)
    monkeypatch.setattr(client._identity_cache, "write", broken)
    monkeypatch.setattr(client, "_wait_identity_retry", wait)
    try:
        await client.start()
        await asyncio.wait_for(waiting.wait(), 1)
        assert client.is_connected and client.display_name == "Drive Bot"
        assert client.native.get_me.await_count == 1
    finally:
        await client.stop()


async def test_persisted_name_floodwait_is_honored_after_recreation(
    tmp_path, monkeypatch
):
    client = client_for(tmp_path)
    cache = client._identity_cache
    cache.write(client.name, {"display_name": "Cached Bot", "username": "cached_bot"})
    deadline = time.time() + 3600
    cache.defer(client.name, deadline)
    waiting = asyncio.Event()
    observed = []

    async def wait(delay):
        observed.append(delay)
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_wait_identity_retry", wait)
    pool = ClientPool([client])
    try:
        await pool.start()
        await asyncio.wait_for(waiting.wait(), 1)
        assert observed[0] > 3590
        assert client.display_name == "Cached Bot"
        assert client.identity_retry_at == deadline
        assert pool._account_cooldowns[client.name] == deadline
        assert client.native.get_me.await_count == 1  # Existing auth only.
        pool.refresh_identities()
        assert client.native.get_me.await_count == 1
    finally:
        await pool.stop()
