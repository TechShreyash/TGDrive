"""Revoked-account failures preserve bytes and only reassign safe operations."""

import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    SessionRevokedError,
)

from tests.media.test_pooled_media import account
from tests.media.test_streaming import FakeStreamClient, make_request, response_body
from tests.media.test_telegram_cache import Native
from tests.support.mongo_fixtures import register_test_backend
from tests.telegram.test_client_pool import FakeClient
from tests.transfers.test_multipart_transfers import (
    Account,
    dispose,
    manager_for,
    staged,
    stored,
)
from tgdrive.media.streaming import stream_telegram_file
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.connection_errors import TelegramConnectionError
from tgdrive.transfers.multipart_transfer import fingerprint
from tgdrive.transfers.tasks import TransferStatus


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path)


@pytest.mark.parametrize("error_type", [AuthKeyUnregisteredError, SessionRevokedError])
async def test_revoked_account_is_quarantined_and_errors_do_not_leak(error_type):
    first, second = FakeClient("first"), FakeClient("second")
    pool = ClientPool([first, second], allow_partial_start=True)
    await pool.start()
    try:
        with pytest.raises(TelegramConnectionError) as caught:
            async with pool.lease(client_name=first.name):
                raise error_type(request="secret-token-request")
        assert caught.value.code == "TELEGRAM_CREDENTIALS_REVOKED"
        assert "Connections" in str(caught.value)
        async with pool.lease() as healthy:
            assert healthy is second
        status = await pool.status()
        assert not status[0].healthy and status[0].error
        assert "secret-token-request" not in json.dumps([asdict(s) for s in status])
        assert (await pool.health()).ready
        assert all(s.active_leases == 0 for s in status)
    finally:
        await pool.stop()


async def test_revocation_wakes_waiters_and_preserves_fixed_sender():
    first = FakeClient("first")
    pool = ClientPool([first])
    await pool.start()

    async def waiter():
        async with pool.lease(media=True, client_name=first.name):
            pytest.fail("a revoked sender must not be selected")

    try:
        with pytest.raises(TelegramConnectionError):
            async with pool.lease(media=True, client_name=first.name):
                waiting = asyncio.create_task(waiter())
                await asyncio.sleep(0)
                raise SessionRevokedError(request=None)
        with pytest.raises(TelegramConnectionError):
            await asyncio.wait_for(waiting, 1)
        with pytest.raises(TelegramConnectionError, match="no longer configured"):
            async with pool.lease(media=True, client_name="removed"):
                pytest.fail("a removed sender must fail with a repair action")
    finally:
        await pool.stop()


async def test_nested_lease_failure_does_not_disable_an_unrelated_account():
    first, second = FakeClient("revoked"), FakeClient("healthy")
    pool = ClientPool([first, second])
    await pool.start()
    try:
        with pytest.raises(TelegramConnectionError):
            async with (
                pool.lease(client_name=second.name),
                pool.lease(client_name=first.name),
            ):
                raise SessionRevokedError(request=None)
        statuses = await pool.status()
        assert not statuses[0].healthy
        assert statuses[1].healthy
    finally:
        await pool.stop()


async def test_verified_channel_repair_restores_access_but_not_revoked_sessions():
    client = FakeClient("bot")
    pool = ClientPool([client])
    await pool.start()
    try:
        await pool.report_failure(client, ChannelPrivateError(request=None))
        assert not (await pool.status())[0].healthy
        await pool.restore_channel_access(client.name)
        assert (await pool.status())[0].healthy
        await pool.report_failure(client, SessionRevokedError(request=None))
        await pool.restore_channel_access(client.name)
        assert not (await pool.status())[0].healthy
    finally:
        await pool.stop()


async def test_transient_network_failure_does_not_quarantine_account():
    client = FakeClient("bot")
    pool = ClientPool([client])
    await pool.start()
    try:
        with pytest.raises(ConnectionError):
            async with pool.lease():
                raise ConnectionError("offline")
        assert (await pool.status())[0].healthy
    finally:
        await pool.stop()


async def test_revoked_storage_bot_metadata_falls_back_before_http_headers():
    payload = b"original bytes"
    first, second = FakeStreamClient(payload), FakeStreamClient(payload)
    first.name, second.name = "revoked", "healthy"
    first.get_messages = AsyncMock(side_effect=AuthKeyUnregisteredError(request=None))
    pool = ClientPool([first, second])
    await pool.start()
    try:
        response = await stream_telegram_file(
            pool=pool,
            channel_id=-100123,
            message_id=7,
            file_name="data.bin",
            expected_size=len(payload),
            request=make_request(),
        )
        assert await asyncio.wait_for(response_body(response), 2) == payload
        assert not (await pool.status())[0].healthy
        assert all(item.active_leases == 0 for item in await pool.status())
    finally:
        await pool.stop()


@pytest.mark.parametrize("error_type", [SessionRevokedError, ChannelPrivateError])
async def test_payload_recovers_using_another_account(error_type, monkeypatch):
    class Removed(Native):
        def iter_download(self, *args, **kwargs):
            raise error_type(request="secret-request")

    payload = b"original bytes"
    bot = account("bot", payload)
    premium = account("premium", payload, native_type=Removed)
    pool = ClientPool([bot], [premium])
    await pool.start()
    lease = pool.lease
    first_payload = True

    def fault_first_payload(**kwargs):
        nonlocal first_payload
        if kwargs.get("download") and first_payload:
            first_payload = False
            kwargs["client_name"] = premium.name
        return lease(**kwargs)

    monkeypatch.setattr(pool, "lease", fault_first_payload)
    try:
        response = await stream_telegram_file(
            pool=pool,
            channel_id=-100123,
            message_id=77,
            file_name="data.bin",
            expected_size=len(payload),
            request=make_request(),
        )
        assert await asyncio.wait_for(response_body(response), 2) == payload
        assert bot.native.requests
        assert not (await pool.status())[1].healthy
    finally:
        await pool.stop()


async def test_upload_reassigns_only_before_message_dispatch(tmp_path):
    manager, ledger = await manager_for(tmp_path, users=True)
    user = next(s.client for s in manager.pool._slots if s.kind == "user")
    user.send_staged = AsyncMock(side_effect=SessionRevokedError(request=None))
    payload = b"abcdef"
    source = staged(manager, payload)
    checksum, parts = fingerprint(
        source, len(payload), manager.settings.bot_upload_limit
    )
    parts[0]["sender"] = user.name
    record = await manager.registry.create("queued", total=len(payload))
    record.context.update(
        sha256=checksum,
        multipart_plan=dict(version=1, kind="bot", parts=parts),
    )
    (await manager.registry.persist(record))
    try:
        await asyncio.wait_for(
            manager._upload_path(record, source, "/", "data.bin", len(payload)), 2
        )
        assert record.status == TransferStatus.COMPLETED
        user.send_staged.assert_awaited()
        parts = stored(manager)["parts"]
        assert (
            b"".join(ledger.messages[p["message_id"]].content for p in parts) == payload
        )
        assert len(ledger.messages) == 2
        assert all(
            p["sender"] != "premium" for p in record.context["multipart_plan"]["parts"]
        )
        assert not (await manager.pool.status())[-1].healthy
    finally:
        await dispose(manager)


async def test_post_dispatch_revocation_preserves_bytes_and_operation_identity(
    tmp_path,
):
    manager, ledger = await manager_for(tmp_path)

    class RevokedAfterSend(Account):
        async def send_staged(self, *args, **kwargs):
            await super().send_staged(*args, **kwargs)
            raise SessionRevokedError(request=None)

    await manager.pool.stop()
    manager.pool = ClientPool([RevokedAfterSend("bot-one", ledger)])
    await manager.pool.start()
    source = staged(manager, b"abc")
    record = await manager.enqueue_upload(source, "/", "data.bin", 3)
    try:
        await asyncio.wait_for(record.task, 2)
        assert record.status == TransferStatus.ERROR
        assert "Connections" in record.error
        assert source.exists() and manager.store.list_directory("/") == {}
        part = record.context["multipart_plan"]["parts"][0]
        original = (part["sender"], part["random_id"])
        assert part["send_started"] and len(ledger.messages) == 1
        await manager.pool.stop()
        manager.pool = ClientPool([Account("replacement-bot", ledger)])
        await manager.pool.start()
        (await manager.retry_job(record.id))
        await asyncio.wait_for(record.task, 2)
        assert record.status == TransferStatus.ERROR
        assert "no longer configured" in record.error
        assert source.exists() and len(ledger.messages) == 1
        await manager.pool.stop()
        manager.pool = ClientPool([Account("bot-one", ledger)])
        await manager.pool.start()
        (await manager.retry_job(record.id))
        await asyncio.wait_for(record.task, 2)
        assert record.status == TransferStatus.COMPLETED
        assert (part["sender"], part["random_id"]) == original
        assert len(ledger.messages) == 1
    finally:
        await dispose(manager)
