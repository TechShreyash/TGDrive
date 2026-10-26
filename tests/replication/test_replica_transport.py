"""Offline contracts for the exact Telegram replica transport requests."""

from __future__ import annotations

import asyncio
import copy
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr
from telethon import TelegramClient, functions, types, utils
from telethon.crypto import AuthKey
from telethon.errors import (
    AccessTokenInvalidError,
    FloodWaitError,
    NetworkMigrateError,
    PhoneMigrateError,
    RandomIdDuplicateError,
    UserMigrateError,
)
from telethon.sessions import SQLiteSession, StringSession

from tgdrive.replication.replica_transport import (
    CAPSULE_NAME,
    MAX_CAPSULE_BYTES,
    MAX_VERIFY_BATCH,
    ReplicaRequestPacer,
    ReplicaTransport,
    ReplicaTransportError,
    _provider_error,
)

MAIN = -1001234567890
BACKUP = -1002234567890
TOKEN = "234567:" + "b" * 24
PRIMARY = "123456:" + "a" * 24


def channel(identity: int, **kwargs):
    return types.Channel(
        id=utils.resolve_id(identity)[0],
        title="Private storage",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=True,
        admin_rights=types.ChatAdminRights(
            post_messages=True, edit_messages=True, delete_messages=True
        ),
        **kwargs,
    )


def document(size=123, name="payload.bin"):
    return types.Document(
        id=123456789,
        access_hash=42,
        file_reference=b"fresh-reference",
        date=None,
        mime_type="application/octet-stream",
        size=size,
        dc_id=2,
        attributes=[types.DocumentAttributeFilename(name)],
    )


def message(identity, channel_id, doc):
    return SimpleNamespace(
        id=identity,
        peer_id=types.PeerChannel(utils.resolve_id(channel_id)[0]),
        document=doc,
        media=SimpleNamespace(ttl_seconds=None),
        noforwards=False,
        fwd_from=None,
    )


class Native:
    def __init__(self):
        self.connect = AsyncMock()
        self.sign_in = AsyncMock(return_value=SimpleNamespace(id=234567, bot=True))
        self.get_me = AsyncMock(return_value=None)
        self.disconnect = AsyncMock()
        self.chats = {MAIN: channel(MAIN), BACKUP: channel(BACKUP)}
        self.messages = {(MAIN, 77): message(77, MAIN, document())}
        self.requests = []
        self.downloads = []
        self.uploads = []
        self.pins = {}
        self.receipts = {}
        self.next_message = 901
        self.response_loss = False
        self.bad_copy = False
        self.wrong_destination_peer = False
        self.response_mode = "updates"
        self.body = b"encrypted capsule"
        self.delay_download = False

    async def get_input_entity(self, identity):
        return types.InputPeerChannel(utils.resolve_id(identity)[0], 1)

    async def get_messages(self, channel_id, *, ids):
        if isinstance(ids, list):
            return [self.messages.get((channel_id, identity)) for identity in ids]
        return self.messages.get((channel_id, ids))

    async def upload_file(self, source, **kwargs):
        self.body = source.read()
        self.uploads.append(kwargs)
        return types.InputFileBig(id=123, parts=1, name=kwargs["file_name"])

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.channels.GetFullChannelRequest):
            identity = utils.get_peer_id(request.channel)
            return SimpleNamespace(
                chats=[self.chats[identity]],
                full_chat=SimpleNamespace(pinned_msg_id=self.pins.get(identity)),
            )
        if isinstance(request, functions.messages.UpdatePinnedMessageRequest):
            self.pins[utils.get_peer_id(request.peer)] = request.id
            return True
        assert isinstance(request, functions.messages.SendMediaRequest)
        identity = utils.get_peer_id(request.peer)
        key = (identity, request.random_id)
        if key not in self.receipts:
            result = self.next_message
            self.next_message += 1
            self.receipts[key] = result
            if isinstance(request.media, types.InputMediaDocument):
                doc = copy.deepcopy(self.messages[(MAIN, 77)].document)
                if self.bad_copy:
                    doc.id += 1
            else:
                doc = document(len(self.body), request.media.attributes[0].file_name)
            self.messages[identity, result] = message(result, identity, doc)
            if self.wrong_destination_peer:
                self.messages[identity, result].peer_id = types.PeerChannel(999)
        result = self.receipts[key]
        if self.response_loss:
            self.response_loss = False
            raise TimeoutError("ambiguous response includes " + TOKEN)
        if self.response_mode == "short":
            return types.UpdateShort(
                update=types.UpdateMessageID(id=result, random_id=request.random_id),
                date=None,
            )
        if self.response_mode == "short-sent":
            return types.UpdateShortSentMessage(
                id=result, pts=1, pts_count=1, date=None, out=True
            )
        return SimpleNamespace(
            updates=[types.UpdateMessageID(id=result, random_id=request.random_id)]
        )

    def iter_download(self, doc, **kwargs):
        self.downloads.append((doc, kwargs))

        async def read():
            if self.delay_download:
                await asyncio.Event().wait()
            yield self.body[:5]
            yield self.body[5:]

        return read()


class Clock:
    def __init__(self):
        self.value = 0.0
        self.delays = []

    def now(self):
        return self.value

    async def sleep(self, delay):
        self.delays.append(delay)
        self.value += delay
        await asyncio.sleep(0)


async def started(native=None, *, primary=PRIMARY, pacer=None):
    native = native or Native()
    transport = ReplicaTransport(
        SimpleNamespace(bot_tokens=(primary,), api_id=1),
        TOKEN,
        client=native,
        pacer=pacer or ReplicaRequestPacer(interval=0, maximum_interval=0),
    )
    await transport.start()
    return transport, native


def session_settings(directory, *, api_id=12345, api_hash="a" * 32):
    return SimpleNamespace(
        bot_tokens=(PRIMARY,),
        api_id=api_id,
        api_hash=SecretStr(api_hash),
        resolved_data_dir=directory,
    )


@pytest.fixture
def persistent_clients(monkeypatch):
    """Exercise real private SQLite auth storage without connecting to Telegram."""
    state = SimpleNamespace(
        instances=[], expired=False, failure=None, account_id=234567
    )

    def create(session_path, *_args, **_kwargs):
        native = Native()
        native.session = SQLiteSession(session_path)

        async def account():
            key = native.session.auth_key
            if not state.expired and key is not None and key.key is not None:
                return SimpleNamespace(id=state.account_id, bot=True)
            return None

        async def authorize(**_kwargs):
            if state.failure is not None:
                raise state.failure
            native.session.auth_key = AuthKey(bytes(range(256)))
            native.session.save()
            return SimpleNamespace(id=state.account_id, bot=True)

        native.sign_in = AsyncMock(side_effect=authorize)
        native.get_me = AsyncMock(side_effect=account)
        native.disconnect = AsyncMock(side_effect=native.session.close)
        state.instances.append(native)
        return native

    monkeypatch.setattr("tgdrive.replication.replica_transport.TelegramClient", create)
    return state


@pytest.mark.asyncio
async def test_saved_bot_session_avoids_authorization_on_reconnect_and_replacement(
    tmp_path,
    persistent_clients,
):
    settings = session_settings(tmp_path)
    first = ReplicaTransport(
        settings, TOKEN, pacer=ReplicaRequestPacer(interval=0, maximum_interval=0)
    )
    await first.start()
    await first.close()
    await first.start()
    await first.close()
    replacement = ReplicaTransport(
        settings, TOKEN, pacer=ReplicaRequestPacer(interval=0, maximum_interval=0)
    )
    try:
        await replacement.start()
        original, restored = persistent_clients.instances
        original.sign_in.assert_awaited_once_with(bot_token=TOKEN)
        assert original.get_me.await_count == 2
        restored.sign_in.assert_not_awaited()
        restored.get_me.assert_awaited_once()
        assert original.session.filename == restored.session.filename
        path = Path(restored.session.filename)
        assert path.name.startswith("backup-bot-234567-")
        assert TOKEN not in str(path) and "a" * 32 not in str(path)
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        await replacement.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["token", "api_id", "api_hash"])
async def test_session_cache_is_isolated_when_credentials_change(
    tmp_path,
    persistent_clients,
    change,
):
    settings = session_settings(tmp_path)
    first = ReplicaTransport(
        settings, TOKEN, pacer=ReplicaRequestPacer(interval=0, maximum_interval=0)
    )
    await first.start()
    await first.close()
    token = TOKEN
    if change == "token":
        token = "234567:" + "c" * 24
    elif change == "api_id":
        settings = session_settings(tmp_path, api_id=54321)
    else:
        settings = session_settings(tmp_path, api_hash="b" * 32)
    replacement = ReplicaTransport(
        settings, token, pacer=ReplicaRequestPacer(interval=0, maximum_interval=0)
    )
    try:
        await replacement.start()
        old, new = persistent_clients.instances
        assert old.session.filename != new.session.filename
        new.sign_in.assert_awaited_once_with(bot_token=token)
        assert token not in new.session.filename
    finally:
        await replacement.close()


@pytest.mark.asyncio
async def test_expired_saved_auth_preserves_typed_signin_wait_without_erasing_session(
    tmp_path,
    persistent_clients,
):
    settings = session_settings(tmp_path)
    first = ReplicaTransport(
        settings, TOKEN, pacer=ReplicaRequestPacer(interval=0, maximum_interval=0)
    )
    await first.start()
    await first.close()
    session_path = Path(persistent_clients.instances[0].session.filename)
    persistent_clients.expired = True
    persistent_clients.failure = FloodWaitError(request=TOKEN, capture=2876)
    replacement = ReplicaTransport(
        settings, TOKEN, pacer=ReplicaRequestPacer(interval=0, maximum_interval=0)
    )
    with pytest.raises(ReplicaTransportError) as raised:
        await replacement.start()
    assert raised.value.code == "REPLICA_FLOOD_WAIT"
    assert raised.value.operation == "sign_in"
    assert raised.value.retryable and raised.value.retry_after == 2876
    assert TOKEN not in str(raised.value)
    assert session_path.is_file()
    expired = persistent_clients.instances[-1]
    expired.sign_in.assert_awaited_once_with(bot_token=TOKEN)
    expired.get_me.assert_awaited_once()
    expired.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_authorized_saved_session_still_requires_matching_bot_identity(
    tmp_path,
    persistent_clients,
):
    settings = session_settings(tmp_path)
    first = ReplicaTransport(
        settings, TOKEN, pacer=ReplicaRequestPacer(interval=0, maximum_interval=0)
    )
    await first.start()
    await first.close()
    persistent_clients.account_id = 999999
    replacement = ReplicaTransport(
        settings, TOKEN, pacer=ReplicaRequestPacer(interval=0, maximum_interval=0)
    )
    with pytest.raises(ReplicaTransportError) as raised:
        await replacement.start()
    assert raised.value.code == "REPLICA_CREDENTIALS"
    assert TOKEN not in str(raised.value)
    restored = persistent_clients.instances[-1]
    restored.sign_in.assert_not_awaited()
    restored.get_me.assert_awaited_once()
    restored.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["directory", "session", "journal", "wal", "shm"])
async def test_session_storage_rejects_symbolic_links_without_touching_target(
    tmp_path,
    persistent_clients,
    link,
):
    settings = session_settings(tmp_path)
    transport = ReplicaTransport(
        settings, TOKEN, pacer=ReplicaRequestPacer(interval=0, maximum_interval=0)
    )
    outside = tmp_path / "preserve-this-file"
    outside.write_bytes(b"unchanged")
    if link == "directory":
        target = tmp_path / "other-sessions"
        target.mkdir()
        (tmp_path / "sessions").symlink_to(target, target_is_directory=True)
    else:
        path = transport._persistent_session(234567)
        if link == "session":
            path.unlink()
            path.symlink_to(outside)
        else:
            suffix = {"journal": "-journal", "wal": "-wal", "shm": "-shm"}[link]
            path.with_name(path.name + suffix).symlink_to(outside)
    with pytest.raises(ReplicaTransportError) as raised:
        await transport.start()
    assert raised.value.code == "REPLICA_SESSION"
    assert TOKEN not in str(raised.value)
    assert not persistent_clients.instances
    assert outside.read_bytes() == b"unchanged"


@pytest.mark.asyncio
async def test_pacer_serializes_callers_and_spaces_completion_from_next_start():
    clock = Clock()
    pacer = ReplicaRequestPacer(clock=clock.now, sleep=clock.sleep)
    active = maximum_active = 0
    starts = []

    async def operation():
        nonlocal active, maximum_active
        async with pacer.slot():
            active += 1
            maximum_active = max(maximum_active, active)
            starts.append(clock.value)
            await asyncio.sleep(0)
            clock.value += 0.5
            active -= 1

    await asyncio.gather(operation(), operation(), operation())
    assert maximum_active == 1
    assert starts == [0, 1.5, 3]
    assert clock.delays == [1, 1]


@pytest.mark.asyncio
async def test_floodwait_slows_replacement_transport_and_retains_typed_operation():
    clock = Clock()
    pacer = ReplicaRequestPacer(clock=clock.now, sleep=clock.sleep)
    transport, native = await started(pacer=pacer)
    native.get_input_entity = AsyncMock(side_effect=FloodWaitError(TOKEN, capture=42))
    with pytest.raises(ReplicaTransportError) as raised:
        await transport.inspect(BACKUP)
    assert raised.value.retry_after == 42
    assert raised.value.operation == "resolve_peer"
    assert TOKEN not in str(raised.value)
    assert pacer.interval == 2
    await transport.close()

    clock.value += 42
    replacement, _ = await started(pacer=pacer)
    try:
        assert clock.delays[-1] == 2
        pacer.slow_down()
        pacer.slow_down()
        assert pacer.interval == 4
        async with pacer.slot():
            pass
        pacer.reset()
        async with pacer.slot():
            pass
        assert clock.delays[-1] == 4, "Reset cannot discard the already reserved gap"
        assert pacer.interval == 1
    finally:
        await replacement.close()


@pytest.mark.asyncio
async def test_cancelled_pacer_wait_does_not_issue_a_request_or_hold_the_lock():
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def sleep(_delay):
        entered.set()
        await blocked.wait()

    pacer = ReplicaRequestPacer(clock=lambda: 0, sleep=sleep)
    async with pacer.slot():
        pass
    called = False

    async def operation():
        nonlocal called
        async with pacer.slot():
            called = True

    task = asyncio.create_task(operation())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not called
    blocked.set()
    await operation()
    assert called


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "missing", "size", "peer", "protected"])
async def test_batched_verification_checks_every_document_without_payload_io(failure):
    transport, native = await started()
    native.messages[BACKUP, 901] = message(901, BACKUP, document(123))
    native.messages[BACKUP, 902] = message(902, BACKUP, document(456))
    if failure == "missing":
        native.messages.pop((BACKUP, 902))
    elif failure == "size":
        native.messages[BACKUP, 902].document.size = 455
    elif failure == "peer":
        native.messages[BACKUP, 902].peer_id = types.PeerChannel(999)
    elif failure == "protected":
        native.messages[BACKUP, 902].noforwards = True
    native.get_messages = AsyncMock(wraps=native.get_messages)
    try:
        if failure:
            with pytest.raises(ReplicaTransportError):
                await transport.verify_many(BACKUP, [(901, 123), (902, 456)])
        else:
            await transport.verify_many(BACKUP, [(901, 123), (902, 456)])
        native.get_messages.assert_awaited_once_with(BACKUP, ids=[901, 902])
        assert len(native.requests) == 1
        assert isinstance(native.requests[0], functions.channels.GetFullChannelRequest)
        assert native.uploads == native.downloads == []
        with pytest.raises(ReplicaTransportError) as raised:
            await transport.verify_many(BACKUP, [(901, 123)] * (MAX_VERIFY_BATCH + 1))
        assert raised.value.code == "REPLICA_INPUT"
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_paced_copies_keep_fresh_channel_checks_and_reference_only_media():
    clock = Clock()
    pacer = ReplicaRequestPacer(clock=clock.now, sleep=clock.sleep)
    transport, native = await started(pacer=pacer)
    native.get_input_entity = AsyncMock(wraps=native.get_input_entity)
    try:
        copies = await asyncio.gather(
            transport.copy(MAIN, 77, BACKUP, random_id=11, expected_size=123),
            transport.copy(MAIN, 77, BACKUP, random_id=12, expected_size=123),
        )
        assert copies == [901, 902]
        assert all(delay == 1 for delay in clock.delays)
        assert native.get_input_entity.await_count == 2
        assert native.uploads == native.downloads == []
        native.chats[BACKUP].username = "now_public"
        with pytest.raises(ReplicaTransportError) as raised:
            await transport.copy(MAIN, 77, BACKUP, random_id=13, expected_size=123)
        assert raised.value.code == "REPLICA_PUBLIC_CHANNEL"
        assert len(native.receipts) == 2
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_start_distinguishes_identity_and_does_not_reveal_credentials():
    native = Native()
    transport = ReplicaTransport(
        SimpleNamespace(bot_tokens=(TOKEN,)), TOKEN, client=native
    )
    with pytest.raises(ReplicaTransportError) as error:
        await transport.start()
    assert error.value.code == "REPLICA_SAME_BOT"
    native.connect.assert_not_called()
    assert TOKEN not in str(error.value)
    native.get_me.return_value = SimpleNamespace(id=999999, bot=True)
    candidate = ReplicaTransport(
        SimpleNamespace(bot_tokens=(PRIMARY,)), TOKEN, client=native
    )
    with pytest.raises(ReplicaTransportError, match="identity"):
        await candidate.start()
    native.disconnect.assert_awaited_once()


def test_inflight_duplicate_is_retryable_without_echoing_provider_request():
    error = RandomIdDuplicateError(request=TOKEN)
    mapped = _provider_error(error)
    assert mapped.code == "REPLICA_IN_FLIGHT"
    assert mapped.retryable and mapped.retry_after == 5
    assert TOKEN not in str(mapped)


@pytest.mark.asyncio
async def test_native_client_preserves_typed_error_for_durable_coordinator_retry(
    monkeypatch,
    tmp_path,
):
    native = Native()
    constructor = Mock(return_value=native)
    monkeypatch.setattr(
        "tgdrive.replication.replica_transport.TelegramClient", constructor
    )
    transport = ReplicaTransport(
        SimpleNamespace(
            bot_tokens=(PRIMARY,),
            api_id=12345,
            api_hash=SecretStr("a" * 32),
            resolved_data_dir=tmp_path,
        ),
        TOKEN,
    )
    try:
        await transport.start()
        assert constructor.call_args.kwargs["raise_last_call_error"] is True
        assert constructor.call_args.kwargs["request_retries"] == 0
        assert constructor.call_args.kwargs["receive_updates"] is False
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_real_telethon_auth_probe_preserves_floodwait_without_signing_in():
    native = TelegramClient(
        StringSession(),
        12345,
        "a" * 32,
        receive_updates=False,
        request_retries=0,
        raise_last_call_error=True,
        flood_sleep_threshold=0,
    )
    account = types.User(id=234567, access_hash=1, is_self=True, bot=True)
    native.connect = AsyncMock()
    native.disconnect = AsyncMock()
    native.sign_in = AsyncMock(wraps=native.sign_in)
    requests = []

    async def respond(request):
        request = getattr(request, "query", request)
        requests.append(request)
        if len(requests) == 1:
            raise FloodWaitError(request=request, capture=2876)
        # If the initial wait is swallowed, get_me succeeds and allows the
        # erroneous startup to continue, reproducing the real SDK behavior.
        assert isinstance(request, functions.users.GetUsersRequest)
        return [account]

    native._sender = SimpleNamespace(
        send=Mock(side_effect=lambda request, **_kwargs: respond(request))
    )
    transport = ReplicaTransport(
        SimpleNamespace(bot_tokens=(PRIMARY,)),
        TOKEN,
        client=native,
        pacer=ReplicaRequestPacer(interval=0, maximum_interval=0),
    )
    with pytest.raises(ReplicaTransportError) as raised:
        await transport.start()
    assert raised.value.code == "REPLICA_FLOOD_WAIT"
    assert raised.value.retry_after == 2876
    assert raised.value.retryable
    assert raised.value.operation == "get_me"
    assert TOKEN not in str(raised.value)
    assert len(requests) == 1
    assert isinstance(requests[0], functions.users.GetUsersRequest)
    native.sign_in.assert_not_awaited()
    native.disconnect.assert_awaited_once()
    assert not transport._started


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "migration", [UserMigrateError, NetworkMigrateError, PhoneMigrateError]
)
async def test_real_telethon_login_replays_auth_once_after_dc_switch(migration):
    native = TelegramClient(
        StringSession(),
        12345,
        "a" * 32,
        receive_updates=False,
        request_retries=0,
        raise_last_call_error=True,
        flood_sleep_threshold=0,
    )
    account = types.User(id=234567, access_hash=1, is_self=True, bot=True)
    native.connect = AsyncMock()
    native.disconnect = AsyncMock()
    native.get_me = AsyncMock(side_effect=[None, None, None, None])
    native._on_login = AsyncMock(return_value=account)
    native._switch_dc = AsyncMock()
    native._authorized = False
    requests = []

    async def respond(request):
        request = getattr(request, "query", request)
        assert isinstance(request, functions.auth.ImportBotAuthorizationRequest)
        requests.append(request)
        if len(requests) == 1:
            raise migration(request=request, capture=4)
        return types.auth.Authorization(user=account)

    def send(request, *, ordered=False):
        return respond(request)

    native._sender = SimpleNamespace(send=Mock(side_effect=send))
    transport = ReplicaTransport(
        SimpleNamespace(bot_tokens=(PRIMARY,)), TOKEN, client=native
    )
    try:
        await transport.start()
        assert transport._started
        assert native._request_retries == 0
        assert len(requests) == 2
        native._switch_dc.assert_awaited_once_with(4)
        native._on_login.assert_awaited_once_with(account)
    finally:
        await transport.close()
    native.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "migration", [UserMigrateError, NetworkMigrateError, PhoneMigrateError]
)
async def test_repeated_login_migration_is_bounded_and_sanitized(migration):
    native = Native()
    native.sign_in.side_effect = [
        migration(request=TOKEN, capture=4),
        migration(request=TOKEN, capture=5),
    ]
    transport = ReplicaTransport(
        SimpleNamespace(bot_tokens=(PRIMARY,)), TOKEN, client=native
    )
    with pytest.raises(ReplicaTransportError) as error:
        await transport.start()
    assert native.sign_in.await_count == 2
    assert error.value.code == "REPLICA_DC_MIGRATION"
    assert error.value.retryable
    assert TOKEN not in str(error.value)
    assert error.value.__cause__ is None
    assert not transport._started
    assert native.get_me.await_count == 2
    native.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,code",
    [
        (AccessTokenInvalidError(request=TOKEN), "REPLICA_CREDENTIALS"),
        (FloodWaitError(request=TOKEN, capture=42), "REPLICA_FLOOD_WAIT"),
        (TimeoutError(TOKEN), "REPLICA_UNAVAILABLE"),
    ],
)
async def test_nonmigration_login_failures_are_not_retried(failure, code):
    native = Native()
    native.sign_in.side_effect = failure
    transport = ReplicaTransport(
        SimpleNamespace(bot_tokens=(PRIMARY,)), TOKEN, client=native
    )
    with pytest.raises(ReplicaTransportError) as error:
        await transport.start()
    native.sign_in.assert_awaited_once_with(bot_token=TOKEN)
    native.disconnect.assert_awaited_once()
    assert error.value.code == code
    assert TOKEN not in str(error.value)
    assert not transport._started


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["short", "short-sent"])
@pytest.mark.parametrize("failure", [None, "channel", "document"])
async def test_typed_short_receipts_require_fresh_matching_destination(mode, failure):
    transport, native = await started()
    native.response_mode = mode
    native.wrong_destination_peer = failure == "channel"
    native.bad_copy = failure == "document"
    try:
        if failure is None:
            assert (
                await transport.copy(MAIN, 77, BACKUP, random_id=321, expected_size=123)
                == 901
            )
        else:
            with pytest.raises(ReplicaTransportError) as error:
                await transport.copy(MAIN, 77, BACKUP, random_id=321, expected_size=123)
            assert error.value.code == "REPLICA_VERIFY_FAILED"
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,code",
    [
        ("username", "REPLICA_PUBLIC_CHANNEL"),
        ("usernames", "REPLICA_PUBLIC_CHANNEL"),
        ("noforwards", "REPLICA_PROTECTED"),
        ("restricted", "REPLICA_PROTECTED"),
        ("group", "REPLICA_CHANNEL_TYPE"),
        ("permissions", "REPLICA_PERMISSIONS"),
        ("no-admin", "REPLICA_PERMISSIONS"),
    ],
)
async def test_private_channel_preflight_rejects_unsafe_states(mode, code):
    transport, native = await started()
    chat = native.chats[BACKUP]
    if mode == "username":
        chat.username = "publicchannel"
    elif mode == "usernames":
        chat.usernames = [types.Username(username="publicchannel", active=True)]
    elif mode in {"restricted", "noforwards"}:
        setattr(chat, mode, True)
    elif mode == "group":
        chat.broadcast = False
    elif mode == "permissions":
        chat.admin_rights.edit_messages = False
    else:
        chat.admin_rights = None
    with pytest.raises(ReplicaTransportError) as error:
        await transport.inspect(BACKUP)
    assert error.value.code == code
    await transport.close()


@pytest.mark.asyncio
async def test_copy_existing_premium_document_uses_reference_not_original_bytes():
    transport, native = await started()
    native.messages[MAIN, 77].document.size = 4 * 1024**3
    result = await transport.copy(
        MAIN, 77, BACKUP, random_id=123, expected_size=4 * 1024**3
    )
    assert result == 901
    assert native.uploads == native.downloads == []
    sent = [
        r for r in native.requests if isinstance(r, functions.messages.SendMediaRequest)
    ]
    assert len(sent) == 1 and isinstance(sent[0].media, types.InputMediaDocument)
    assert sent[0].media.id.id == native.messages[MAIN, 77].document.id
    assert sent[0].random_id == 123 and sent[0].silent
    assert not any(
        isinstance(r, functions.messages.ForwardMessagesRequest)
        for r in native.requests
    )


@pytest.mark.asyncio
async def test_ambiguous_send_reuses_persisted_random_id_without_double_copy():
    transport, native = await started()
    native.response_loss = True
    with pytest.raises(ReplicaTransportError) as error:
        await transport.copy(MAIN, 77, BACKUP, random_id=-555, expected_size=123)
    assert error.value.retryable and TOKEN not in str(error.value)
    assert (
        await transport.copy(MAIN, 77, BACKUP, random_id=-555, expected_size=123) == 901
    )
    assert len(native.receipts) == 1
    sent = [
        r for r in native.requests if isinstance(r, functions.messages.SendMediaRequest)
    ]
    assert [r.random_id for r in sent] == [-555, -555]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "size", "identity", "channel"])
async def test_copy_never_confirms_wrong_missing_or_changed_documents(failure):
    transport, native = await started()
    if failure == "missing":
        native.messages.clear()
    elif failure == "size":
        native.messages[MAIN, 77].document.size = 124
    elif failure == "identity":
        native.bad_copy = True
    else:
        native.messages[MAIN, 77].peer_id = types.PeerChannel(999)
    with pytest.raises(ReplicaTransportError):
        await transport.copy(MAIN, 77, BACKUP, random_id=123, expected_size=123)


@pytest.mark.asyncio
async def test_inspection_is_fresh_and_detects_public_change_before_copy():
    transport, native = await started()
    assert (await transport.inspect(BACKUP))["private"]
    native.chats[BACKUP].username = "became_public"
    with pytest.raises(ReplicaTransportError) as error:
        await transport.copy(MAIN, 77, BACKUP, random_id=123, expected_size=123)
    assert error.value.code == "REPLICA_PUBLIC_CHANNEL"
    assert not native.receipts


@pytest.mark.asyncio
async def test_floodwait_carries_wait_without_provider_message_or_credentials():
    transport, native = await started()
    native.get_input_entity = AsyncMock(side_effect=FloodWaitError(None, capture=42))
    with pytest.raises(ReplicaTransportError) as error:
        await transport.inspect(BACKUP)
    assert error.value.retryable and error.value.retry_after == 42
    assert error.value.code == "REPLICA_FLOOD_WAIT"
    assert TOKEN not in str(error.value)


@pytest.mark.asyncio
async def test_unresolved_numeric_channel_reports_access_without_provider_detail():
    transport, native = await started()
    native.get_input_entity = AsyncMock(side_effect=ValueError(TOKEN))
    try:
        with pytest.raises(ReplicaTransportError) as error:
            await transport.inspect(BACKUP)
        assert error.value.code == "REPLICA_ACCESS"
        assert str(BACKUP) in str(error.value)
        assert TOKEN not in str(error.value)
        assert error.value.__cause__ is None
        assert not native.requests
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_publish_verifies_remote_capsule_then_pins_backup(tmp_path):
    transport, native = await started()
    source = tmp_path / "snapshot.tgdb"
    source.write_bytes(b"authenticated encrypted bytes")
    result = await transport.publish(
        BACKUP, source, random_id=12, caption="TGDrive replica", pin=True
    )
    assert result == 901 and native.pins[BACKUP] == 901
    assert native.uploads[0]["file_name"] == CAPSULE_NAME
    assert native.downloads[0][1]["file_size"] == source.stat().st_size
    assert native.messages[BACKUP, 901].document.attributes[0].file_name == CAPSULE_NAME
    result = await transport.publish(
        MAIN, source, random_id=13, caption="TGDrive replica", pin=False
    )
    assert result == 902 and MAIN not in native.pins


@pytest.mark.asyncio
async def test_publish_does_not_pin_when_remote_size_or_bytes_do_not_match(tmp_path):
    transport, native = await started()
    source = tmp_path / "snapshot.tgdb"
    source.write_bytes(b"snapshot")
    original_download = native.iter_download

    def corrupt(doc, **options):
        native.body = b"corrupt!"
        return original_download(doc, **options)

    native.iter_download = corrupt
    with pytest.raises(ReplicaTransportError) as error:
        await transport.publish(BACKUP, source, random_id=12, caption="safe", pin=True)
    assert error.value.code == "REPLICA_VERIFY_FAILED"
    assert not native.pins


@pytest.mark.asyncio
async def test_publish_rejects_large_capsule_without_upload(tmp_path):
    transport, native = await started()
    source = tmp_path / "snapshot.tgdb"
    with source.open("wb") as handle:
        handle.truncate(MAX_CAPSULE_BYTES + 1)
    with pytest.raises(ReplicaTransportError) as error:
        await transport.publish(BACKUP, source, random_id=12, caption="safe", pin=True)
    assert error.value.code == "REPLICA_ARCHIVE_TOO_LARGE"
    assert native.uploads == []


@pytest.mark.asyncio
async def test_download_only_pinned_capsule_into_private_exclusive_path(tmp_path):
    transport, native = await started()
    native.messages[BACKUP, 901] = message(
        901, BACKUP, document(len(native.body), CAPSULE_NAME)
    )
    native.pins[BACKUP] = 901
    target = tmp_path / "recovery.tgdb"
    assert await transport.download(BACKUP, None, target) == 901
    assert target.read_bytes() == native.body
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ReplicaTransportError) as error:
        await transport.download(BACKUP, 901, target)
    assert error.value.code == "REPLICA_LOCAL_EXISTS"
    assert target.read_bytes() == native.body


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["name", "oversize", "truncated", "excess"])
async def test_download_rejects_wrong_or_unbounded_capsules(tmp_path, bad):
    transport, native = await started()
    doc = document(len(native.body), CAPSULE_NAME)
    if bad == "name":
        doc.attributes[0].file_name = "../../etc/passwd"
    elif bad == "oversize":
        doc.size = MAX_CAPSULE_BYTES + 1
    elif bad == "truncated":
        doc.size += 1
    else:
        doc.size -= 1
    native.messages[BACKUP, 901] = message(901, BACKUP, doc)
    target = tmp_path / "recovery.tgdb"
    with pytest.raises(ReplicaTransportError):
        await transport.download(BACKUP, 901, target)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_cancelled_download_removes_partial_file_and_can_resume(tmp_path):
    transport, native = await started()
    native.messages[BACKUP, 901] = message(
        901, BACKUP, document(len(native.body), CAPSULE_NAME)
    )
    native.delay_download = True
    target = tmp_path / "recovery.tgdb"
    task = asyncio.create_task(transport.download(BACKUP, 901, target))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []
    native.delay_download = False
    assert await transport.download(BACKUP, 901, target) == 901


@pytest.mark.asyncio
async def test_missing_pin_does_not_search_history_or_download(tmp_path):
    transport, native = await started()
    with pytest.raises(ReplicaTransportError) as error:
        await transport.download(BACKUP, None, tmp_path / "recovery.tgdb")
    assert error.value.code == "REPLICA_CHECKPOINT_MISSING"
    assert not native.downloads


@pytest.mark.asyncio
async def test_catalog_download_reads_only_explicit_legacy_name(tmp_path):
    transport, native = await started()
    native.messages[MAIN, 78] = message(
        78, MAIN, document(len(native.body), "drive.json")
    )
    native.pins[MAIN] = 78
    with pytest.raises(ReplicaTransportError) as error:
        await transport.download(MAIN, None, tmp_path / "capsule.tgdb")
    assert error.value.code == "REPLICA_DOCUMENT_TYPE"
    assert not native.downloads
    assert await transport.download_catalog(MAIN, tmp_path / "catalog.json") == 78
    assert (tmp_path / "catalog.json").read_bytes() == native.body
    assert MAIN in native.pins and native.pins[MAIN] == 78


@pytest.mark.asyncio
async def test_provider_exceptions_are_sanitized_and_cancellation_propagates():
    transport, native = await started()
    native.get_input_entity = AsyncMock(side_effect=RuntimeError(TOKEN))
    with pytest.raises(ReplicaTransportError) as error:
        await transport.inspect(MAIN)
    assert TOKEN not in str(error.value)
    assert error.value.code == "REPLICA_PROVIDER_ERROR"
    assert error.value.__cause__ is None
    native.get_input_entity = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await transport.inspect(MAIN)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, 0, 1, "-1001234567890"])
async def test_invalid_channel_identifiers_are_rejected_before_network(value):
    transport, native = await started()
    with pytest.raises(ReplicaTransportError) as error:
        await transport.inspect(value)
    assert error.value.code == "REPLICA_INPUT"
    assert not native.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["exists", "missing", "wrong-size"])
async def test_verify_existing_document_without_payload_download(state):
    transport, native = await started()
    if state == "missing":
        native.messages.clear()
    elif state == "wrong-size":
        native.messages[MAIN, 77].document.size = 99
    if state == "exists":
        assert await transport.verify(MAIN, 77, 123) is None
    else:
        with pytest.raises(ReplicaTransportError):
            await transport.verify(MAIN, 77, 123)
    assert native.uploads == native.downloads == []
    assert not native.receipts


@pytest.mark.asyncio
async def test_publish_catalog_preserves_exact_legacy_filename_and_pin(tmp_path):
    transport, native = await started()
    source = tmp_path / "catalog.json"
    source.write_bytes(b'{"schema_version":4}')
    result = await transport.publish_catalog(
        MAIN, source, random_id=99, caption="TGDrive checkpoint v1", pin=True
    )
    assert native.pins[MAIN] == result
    assert native.uploads[0]["file_name"] == "drive.json"
    assert (
        native.messages[MAIN, result].document.attributes[0].file_name == "drive.json"
    )
    target = tmp_path / "restored.json"
    assert await transport.download_catalog(MAIN, target) == result
    assert target.read_bytes() == source.read_bytes()


@pytest.mark.asyncio
async def test_pinned_message_reads_only_current_pin_without_messages_or_history():
    transport, native = await started()
    assert await transport.pinned_message(BACKUP) is None
    native.pins[BACKUP] = 901
    assert await transport.pinned_message(BACKUP) == 901
    assert all(
        isinstance(request, functions.channels.GetFullChannelRequest)
        for request in native.requests
    )
    assert not native.downloads and not native.uploads and not native.receipts
    native.pins[BACKUP] = True
    with pytest.raises(ReplicaTransportError):
        await transport.pinned_message(BACKUP)


@pytest.mark.asyncio
async def test_channel_auto_delete_prevents_backup_copy_before_publishing():
    transport, native = await started()
    original = native.__class__.__call__

    async def with_auto_delete(self, request):
        result = await original(self, request)
        if isinstance(request, functions.channels.GetFullChannelRequest):
            result.full_chat.ttl_period = 86400
        return result

    from unittest.mock import patch

    with (
        patch.object(native.__class__, "__call__", with_auto_delete),
        pytest.raises(ReplicaTransportError) as error,
    ):
        await transport.copy(MAIN, 77, BACKUP, random_id=123, expected_size=123)
    assert error.value.code == "REPLICA_AUTO_DELETE"
    assert not native.receipts
