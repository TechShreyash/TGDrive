"""Lifetime transport repair without replacing authorization or owned work."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.errors import FloodWaitError

from tests.telegram.test_telegram_clients import FixtureClient, settings
from tests.telegram.test_telegram_clients import mongo_backend as mongo_backend
from tests.telegram.test_telegram_clients import native_transport as native_transport
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.connection_errors import TelegramConnectionError


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110 - bounded fake transport state.
            await asyncio.sleep(0)


def client_for(tmp_path, monkeypatch, *, premium=False, updates=False):
    config = settings(tmp_path)
    client = FixtureClient(
        name="unused",
        settings=config,
        session_string="session-A" if premium else None,
        bot_token=None if premium else config.bot_tokens[0],
        updates=updates,
    )
    monkeypatch.setattr(client, "_start_identity_refresh", lambda **_kw: None)
    waits = []

    async def wait(delay):
        waits.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(client, "_wait_reconnect", wait)
    return client, waits


@pytest.mark.parametrize(
    "premium,updates", [(False, False), (True, False), (False, True)]
)
async def test_repeated_native_retry_exhaustion(
    tmp_path, monkeypatch, premium, updates
):
    client, waits = client_for(tmp_path, monkeypatch, premium=premium, updates=updates)
    pool = ClientPool([client] if not premium else [], [client] if premium else [])
    try:
        await pool.start()
        for count in (2, 3):
            await client.native.disconnect()
            assert not client.is_connected
            await eventually(
                lambda count=count: (
                    client.native.get_me.await_count >= count and client.is_connected
                )
            )
        assert waits == [5, 5]
        assert not client.native.sign_ins
        async with pool.lease(premium=premium) as leased:
            assert leased is client
    finally:
        await pool.stop()
    assert client._connection_task is None
    assert not client.native.connected


async def test_backoff_verified_gate_and_cooldown(tmp_path, monkeypatch):
    client, waits = client_for(tmp_path, monkeypatch)
    pool = ClientPool([client])
    release, entered = asyncio.Event(), asyncio.Event()
    original = client.native.connect
    attempts = 0

    async def connect():
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OSError("synthetic outage")
        await original()
        entered.set()
        await release.wait()

    try:
        await pool.start()
        deadline = time.time() + 1
        await pool.defer_client(client.name, deadline)
        monkeypatch.setattr(client.native, "connect", connect)
        await client.native.disconnect()
        await asyncio.wait_for(entered.wait(), 2)
        assert not client.is_connected
        status = (await pool.status())[0]
        assert status.error_code == "TELEGRAM_RECONNECTING"
        assert status.error is not None
        assert "automatically" in status.error
        with pytest.raises(TelegramConnectionError, match="automatically"):
            async with pool.lease():
                pass
        release.set()
        await eventually(lambda: client.is_connected)
        assert waits == [5, 10, 20]
        assert (await pool.status())[0].error_code is None
        assert pool._slots[0].cooldown_until == deadline
    finally:
        release.set()
        await pool.stop()


@pytest.mark.parametrize("failure", ["unauthorized", "wrong_account", "revoked"])
async def test_no_sign_in_or_changed_identity(tmp_path, monkeypatch, failure):
    client, _waits = client_for(tmp_path, monkeypatch)
    try:
        await client.start()
        if failure == "unauthorized":
            client.native.get_me.side_effect = lambda: None
        elif failure == "wrong_account":
            client.native.me = SimpleNamespace(id=654321, bot=True, premium=False)
        else:
            revoked = type("SessionRevokedError", (Exception,), {})
            client.native.get_me.side_effect = revoked("raw private provider error")
        await client.native.disconnect()
        task = client._connection_task
        assert task is not None
        await eventually(lambda: task.done())
        assert not client.is_connected
        assert client.connection_error is not None
        assert client.connection_error.code == "TELEGRAM_CREDENTIALS_REVOKED"
        assert "raw private" not in str(client.connection_error)
        assert not client.native.sign_ins
        assert not client.native.connected
    finally:
        await client.stop()


async def test_stop_cancels_inflight_repair(tmp_path, monkeypatch):
    client, _waits = client_for(tmp_path, monkeypatch)
    entered = asyncio.Event()

    async def blocked_connect():
        entered.set()
        await asyncio.Event().wait()

    await client.start()
    monkeypatch.setattr(client.native, "connect", blocked_connect)
    await client.native.disconnect()
    await asyncio.wait_for(entered.wait(), 2)
    task = client._connection_task
    assert task is not None
    await asyncio.wait_for(client.stop(), 2)
    assert task.cancelled()
    assert client._connection_task is None
    assert not client.native.connected


async def test_channel_quarantine_survives_transport_repair(tmp_path, monkeypatch):
    client, _waits = client_for(tmp_path, monkeypatch)
    pool = ClientPool([client])
    try:
        await pool.start()
        error = TelegramConnectionError(
            "Channel access missing", "TELEGRAM_CHANNEL_ACCESS"
        )
        await pool.mark_unhealthy(client, error)
        await client.native.disconnect()
        await eventually(
            lambda: client.native.get_me.await_count == 2 and client.is_connected
        )
        status = (await pool.status())[0]
        assert not status.healthy
        assert status.error_code == "TELEGRAM_CHANNEL_ACCESS"
        with pytest.raises(TelegramConnectionError, match="Channel access"):
            async with pool.lease():
                pass
    finally:
        await pool.stop()


async def test_waits_for_native_retries_and_owns_one_watcher(tmp_path, monkeypatch):
    client, waits = client_for(tmp_path, monkeypatch)
    try:
        await client.start()
        task = client._connection_task
        await client.start()
        assert client._connection_task is task
        assert client.native.get_me.await_count == 1
        for _ in range(5):
            await asyncio.sleep(0)
        assert waits == []
        client.native.connected = False
        disconnected = client.native._disconnected
        assert disconnected is not None
        disconnected.set_exception(OSError("native retries exhausted"))
        await eventually(lambda: client.is_connected)
        assert waits == [5]
        assert client.native.get_me.await_count == 2
    finally:
        await client.stop()


async def test_stop_cancels_backoff_without_provider_call(tmp_path, monkeypatch):
    client, _waits = client_for(tmp_path, monkeypatch)
    entered = asyncio.Event()

    async def wait(_delay):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_wait_reconnect", wait)
    await client.start()
    await client.native.disconnect()
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(client.stop(), 2)
    assert client.native.get_me.await_count == 1
    assert client._connection_task is None
    assert not client.native.connected


async def test_provider_flood_wait_is_not_clamped_to_transport_backoff(
    tmp_path, monkeypatch
):
    client, waits = client_for(tmp_path, monkeypatch)
    pool = ClientPool([client])
    defer = AsyncMock(wraps=pool.defer_client)
    monkeypatch.setattr(pool, "defer_client", defer)
    client.native.get_me.side_effect = [
        client.native.me,
        FloodWaitError(request=None, capture=90),
        client.native.me,
    ]
    try:
        await pool.start()
        await client.native.disconnect()
        await eventually(
            lambda: client.native.get_me.await_count == 3 and client.is_connected
        )
        assert waits == [5, 90.25]
        defer.assert_awaited_once()
    finally:
        await pool.stop()


async def test_bot_actions_wait_for_verification_without_registering_twice(
    tmp_path, monkeypatch
):
    client, _waits = client_for(tmp_path, monkeypatch, updates=True)
    register = Mock()
    monkeypatch.setattr(client.native, "add_event_handler", register, raising=False)
    mode = SimpleNamespace(
        settings=SimpleNamespace(telegram_admin_ids=(1,)), _help=AsyncMock()
    )
    client.register_bot(mode)
    dispatch = register.call_args.args[0]
    message = SimpleNamespace(
        id=1, sender_id=1, raw_text="/help", file=None, photo=None
    )
    event = SimpleNamespace(is_private=True, sender_id=1, message=message)
    release, entered = asyncio.Event(), asyncio.Event()
    original = client.native.connect

    async def connect():
        await original()
        entered.set()
        await release.wait()

    try:
        await client.start()
        monkeypatch.setattr(client.native, "connect", connect)
        await client.native.disconnect()
        await asyncio.wait_for(entered.wait(), 2)
        await dispatch(event)
        mode._help.assert_not_awaited()
        release.set()
        await eventually(lambda: client.is_connected)
        await dispatch(event)
        mode._help.assert_awaited_once()
        assert register.call_count == 1
    finally:
        release.set()
        await client.stop()


async def test_stop_joins_native_shielded_cleanup_before_returning(
    tmp_path, monkeypatch
):
    client, _waits = client_for(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = client.native.disconnect
    calls = 0

    async def cleanup():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        await original()

    async def shielded_disconnect():
        await asyncio.shield(asyncio.create_task(cleanup()))

    await client.start()
    await original()
    monkeypatch.setattr(client.native, "disconnect", shielded_disconnect)
    await asyncio.wait_for(entered.wait(), 2)
    stopped = asyncio.create_task(client.stop())
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert not stopped.done()
        release.set()
        await asyncio.wait_for(stopped, 2)
        assert client.native.get_me.await_count == 1
        assert client._connection_task is None
        assert not client.native.connected
    finally:
        release.set()
        await stopped
