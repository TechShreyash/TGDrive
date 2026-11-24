import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import functions
from telethon.sessions import MemorySession

from tests.media.test_cached_http_concurrency import ASGI_SCOPE, receive
from tests.media.test_pooled_media import account
from tests.media.test_streaming import make_request
from tests.media.test_telegram_cache import MIB, Native
from tgdrive.media.streaming import stream_telegram_file
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import Client
from tgdrive.telegram.telethon_client import MediaTelegramClient


@pytest.mark.parametrize("download", [True, False], ids=["attachment", "video-range"])
@pytest.mark.parametrize("stall", ["import_authorization", "get_file"])
async def test_cross_dc_handoff_completes_started_response(
    monkeypatch, download, stall
):
    payload = b"a" * MIB + b"b" * MIB
    bots = [account(name, payload) for name in ("one", "two")]
    pool = ClientPool(bots, media_per_bot=1)
    session = MemorySession()
    session.set_dc(1, "127.0.0.1", 443)  # The fixture document lives in DC 2.
    native = MediaTelegramClient(session, 1, "a" * 32)
    sender = Mock(connect=AsyncMock(), disconnect=AsyncMock())
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def import_auth(request):
        assert isinstance(
            request.query.query, functions.auth.ImportAuthorizationRequest
        )
        if stall == "import_authorization":
            entered.set()
            await blocked.wait()

    async def rpc(_sender, request, **kwargs):
        if isinstance(request, functions.auth.ExportAuthorizationRequest):
            return SimpleNamespace(id=1, bytes=b"test auth")
        assert isinstance(request, functions.upload.GetFileRequest)
        assert _sender is sender
        if request.offset == MIB:
            return SimpleNamespace(bytes=payload[MIB : MIB + request.limit])
        assert request.offset == MIB + MIB // 2
        entered.set()
        await blocked.wait()
        raise AssertionError("the overdue read should have been cancelled")

    sender.send = AsyncMock(side_effect=import_auth)
    monkeypatch.setattr(
        "tgdrive.telegram.telethon_client.MTProtoSender", Mock(return_value=sender)
    )
    dc = SimpleNamespace(id=2, ip_address="127.0.0.1", port=443)
    monkeypatch.setattr(native, "_get_dc", AsyncMock(return_value=dc))
    monkeypatch.setattr(native, "_call", rpc)
    monkeypatch.setattr(Client, "_download_reassignment_interval", 0.02)
    # Use Telethon's real iterator and sender pool for the stalled block.
    # Other reads use the normal in-memory transport with offset accounting.
    original = Native.iter_download
    stalled = None

    def iter_download(owner, target, **kwargs):
        nonlocal stalled
        if kwargs["offset"] == MIB and stalled is None:
            stalled = owner
        if kwargs["offset"] == MIB and owner is stalled:
            return native.iter_download(target, **kwargs)
        return original(owner, target, **kwargs)

    monkeypatch.setattr(Native, "iter_download", iter_download)
    await pool.start()
    try:
        for bot in bots:
            bot.download_part_sample = (time.monotonic(), 0.001)
        response = await stream_telegram_file(
            pool=pool,
            channel_id=-100123,
            message_id=77,
            file_name="video.mkv",
            expected_size=len(payload),
            request=make_request(None if download else f"bytes=0-{len(payload) - 2}"),
            preview=not download,
            download=download,
        )
        starts = []
        chunks = []

        async def send(event):
            if event["type"] == "http.response.start":
                starts.append(event["status"])
            else:
                chunks.append(event["body"])

        async with asyncio.timeout(2):  # Below the ordinary five-second timeout.
            await response(ASGI_SCOPE, receive, send)
        assert entered.is_set()
        assert starts == [200 if download else 206]
        expected_tail = payload[MIB:] if download else payload[MIB:-1]
        assert chunks == [payload[:MIB], expected_tail, b""]
        assert int(response.headers["content-length"]) == sum(map(len, chunks))
        spare = next(bot for bot in bots if bot.native is not stalled)
        assert spare.native.requests[1, MIB] == 1
        assert all(row.active_leases == 0 for row in await pool.status())
        if stall == "import_authorization":
            sender.disconnect.assert_awaited_once()
            assert not native._borrowed_senders
        else:
            state, pooled_sender = native._borrowed_senders[2]
            assert pooled_sender is sender and state._n == 0
            sender.disconnect.assert_not_awaited()
    finally:
        blocked.set()
        await pool.stop()


async def test_cancelled_exported_sender_return_releases_one_borrow():
    native = MediaTelegramClient(MemorySession(), 1, "a" * 32)
    sender = SimpleNamespace(dc_id=2)
    state = Mock()
    native._borrowed_senders[2] = (state, sender)
    async with native._borrow_sender_lock:
        task = asyncio.create_task(native._return_exported_sender(sender))
        await asyncio.sleep(0)  # Let return reach the contended lock.
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    state.add_return.assert_called_once_with()
