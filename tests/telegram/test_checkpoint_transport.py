"""Real Telethon request contracts for discoverable checkpoint transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from tgdrive.telegram.clients import Client

CHANNEL = -1001234567890


class CheckpointNative:
    def __init__(self, pinned=42):
        self.requests = []
        self.pinned = pinned
        self.get_input_entity = AsyncMock(
            return_value=types.InputPeerChannel(channel_id=123, access_hash=456)
        )
        self.get_messages = AsyncMock(
            return_value=SimpleNamespace(
                id=77,
                chat_id=CHANNEL,
                sender_id=1,
                raw_text="marker",
                photo=None,
                file=SimpleNamespace(size=3, name="checkpoint.json"),
            )
        )

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(full_chat=SimpleNamespace(pinned_msg_id=self.pinned))
        if isinstance(request, functions.messages.SendMediaRequest):
            return SimpleNamespace(
                updates=[types.UpdateMessageID(id=77, random_id=request.random_id)]
            )
        assert isinstance(request, functions.messages.UpdatePinnedMessageRequest)
        return SimpleNamespace()


def client_for(native):
    client = Client.__new__(Client)
    client.name = "checkpoint-test-bot"
    client.native = native
    client._upload_parts = AsyncMock(
        return_value=types.InputFileBig(id=1, parts=1, name="checkpoint.json")
    )
    return client


async def test_checkpoint_caption_reaches_raw_send_and_known_message_read(tmp_path):
    native = CheckpointNative()
    client = client_for(native)
    path = tmp_path / "checkpoint.json"
    path.write_bytes(b"{}\n")
    caption = "TGDrive checkpoint v1\nDrive: portable-id\nRevision: 7"
    sent = await client.send_document(
        CHANNEL, path, caption=caption, file_name="checkpoint.json", random_id=987
    )

    assert sent.id == 77
    (request,) = native.requests
    assert isinstance(request, functions.messages.SendMediaRequest)
    assert request.message == caption
    assert request.random_id == 987
    assert request.silent is True
    native.get_messages.assert_awaited_once_with(CHANNEL, ids=77)


@pytest.mark.parametrize("pinned", [None, 42])
async def test_pin_discovery_uses_full_channel_without_history_or_search(pinned):
    native = CheckpointNative(pinned)
    client = client_for(native)

    assert await client.get_pinned_message_id(CHANNEL) == pinned

    (request,) = native.requests
    assert isinstance(request, functions.channels.GetFullChannelRequest)
    assert request.channel == types.InputChannel(channel_id=123, access_hash=456)
    native.get_input_entity.assert_awaited_once_with(CHANNEL)
    native.get_messages.assert_not_awaited()


@pytest.mark.parametrize("pinned", [True, 0, -1, "42"])
async def test_pin_discovery_rejects_malformed_message_identity(pinned):
    with pytest.raises(RuntimeError, match="invalid pinned"):
        await client_for(CheckpointNative(pinned)).get_pinned_message_id(CHANNEL)


async def test_pin_uses_resolved_peer_and_silent_update_request():
    native = CheckpointNative()
    await client_for(native).pin_message(CHANNEL, 77)

    (request,) = native.requests
    assert isinstance(request, functions.messages.UpdatePinnedMessageRequest)
    assert request.peer == types.InputPeerChannel(channel_id=123, access_hash=456)
    assert request.id == 77
    assert request.silent is True
    native.get_input_entity.assert_awaited_once_with(CHANNEL)


@pytest.mark.parametrize("message_id", [True, 0, -1, "77"])
async def test_pin_rejects_invalid_id_before_telegram(message_id):
    native = CheckpointNative()
    with pytest.raises(ValueError, match="positive integer"):
        await client_for(native).pin_message(CHANNEL, message_id)
    assert native.requests == []
    native.get_input_entity.assert_not_awaited()
