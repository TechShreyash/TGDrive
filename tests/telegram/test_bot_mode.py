from __future__ import annotations

import asyncio
import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from telethon.extensions import html as telegram_html

from tests.support.mongo_fixtures import new_store, register_test_backend
from tgdrive.core.config import Settings
from tgdrive.settings.bot_settings import BotUploadPreferences
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.telegram.bot_mode import BotMode
from tgdrive.telegram.clients import Client


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, settings(tmp_path))


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        MONGODB_URI="mongodb://synthetic.invalid",
        API_ID=12345,
        API_HASH="a" * 32,
        BOT_TOKENS="123456:" + "x" * 30,
        MAIN_BOT_TOKEN="123456:" + "x" * 30,
        STORAGE_CHANNEL=-1001234567890,
        ADMIN_PASSWORD="correct horse battery staple",
        SESSION_SECRET="s" * 48,
        ENCRYPTION_KEY="e" * 64,
        TELEGRAM_ADMIN_IDS="1,2",
        DATA_DIR=tmp_path / "data",
        CACHE_DIR=tmp_path / "cache",
    )


class FakeBotClient:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    async def delete_messages(self, _channel: int, message_id: int) -> None:
        self.deleted.append(message_id)


class FakeLifecycleClient(FakeBotClient):
    def __init__(self) -> None:
        super().__init__()
        self.handlers: list[tuple[object, int]] = []
        self.starts = 0
        self.stops = 0
        self.is_connected = False

    def register_bot(self, mode: object) -> None:
        self.handlers.append((mode, 0))

    async def start(self) -> None:
        self.starts += 1
        self.is_connected = True

    async def stop(self) -> None:
        self.stops += 1
        self.is_connected = False


class FakeMessage:
    document: SimpleNamespace
    native: SimpleNamespace

    def __init__(
        self,
        user_id: int,
        *,
        text: str = "",
        command: list[str] | None = None,
        copied: Any | None = None,
    ) -> None:
        self.id = 1
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.command = command if command is not None else text.split()
        self.copied = copied
        self.replies: list[tuple[str, Any | None]] = []
        self.reply_modes: list[str | None] = []
        self.status = SimpleNamespace(edit_text=_async_recorder())

    async def reply_text(
        self,
        text: str,
        reply_markup: Any | None = None,
        *,
        parse_mode: str | None = None,
    ) -> Any:
        self.replies.append((text, reply_markup))
        self.reply_modes.append(parse_mode)
        return self.status

    async def copy(self, _channel: int, **_kwargs: object) -> Any:
        return self.copied


def _async_recorder():
    calls: list[object] = []

    async def record(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    return AsyncMock(side_effect=record, calls=calls)


def root_mode(**kwargs):
    mode = BotMode(**kwargs)
    mode.folder_settings.value = BotUploadPreferences(folder_path="/", encrypt=False)
    mode.folder_settings._loaded = True
    return mode


@pytest.mark.asyncio
async def test_photo_upload_gets_fallback_name_and_metadata(tmp_path: Path) -> None:
    store = new_store(tmp_path / "drive.json")
    client = FakeBotClient()
    mode = root_mode(client=client, settings=settings(tmp_path), store=store)
    copied = SimpleNamespace(
        id=55,
        media=SimpleNamespace(value="photo"),
        photo=SimpleNamespace(file_size=3),
    )
    message = FakeMessage(1, copied=copied)

    await mode._file(cast(Client, client), message)

    item = next(iter(store.list_directory("/").values()))
    assert item["name"].startswith("photo-")
    assert item["name"].endswith("-55.jpg")
    assert message.replies[0][0].startswith("<b>Saving")
    assert message.status.edit_text.calls[-1][0][0].startswith("<b>Saved ✓</b>")
    assert client.deleted == []


@pytest.mark.asyncio
async def test_bot_upload_preserves_copy_when_metadata_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = new_store(tmp_path / "drive.json")
    client = FakeBotClient()
    mode = root_mode(client=client, settings=settings(tmp_path), store=store)
    copied = SimpleNamespace(
        id=56,
        media=SimpleNamespace(value="photo"),
        photo=SimpleNamespace(file_size=3),
    )
    message = FakeMessage(1, copied=copied)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "add_file", fail)
    await mode._file(cast(Client, client), message)

    assert client.deleted == []
    assert not store.list_directory("/")
    assert "could not be confirmed" in message.status.edit_text.calls[-1][0][0]


@pytest.mark.asyncio
async def test_bot_mode_lifecycle_is_idempotent(tmp_path: Path) -> None:
    store = new_store(tmp_path / "drive.json")
    client = FakeLifecycleClient()
    mode = root_mode(client=client, settings=settings(tmp_path), store=store)

    await mode.start()
    await mode.start()
    assert client.starts == 1
    assert len(client.handlers) == 1

    client.is_connected = False
    await mode.stop()
    await mode.stop()
    assert client.stops == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_bot_import_guard_before_copy_and_after_copy_preserves_metadata(
    tmp_path, asynchronous
):
    from tgdrive.settings.managed_settings import SettingsProblem

    store = new_store(tmp_path / "drive")
    client = FakeBotClient()
    copied = SimpleNamespace(id=57, photo=SimpleNamespace(file_size=3))
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise SettingsProblem("Connection restart required")

    async def async_guard():
        guard()

    verified = []

    async def verify_delete():
        verified.append(True)

    mode = root_mode(
        client=client,
        settings=settings(tmp_path),
        store=store,
        before_write=async_guard if asynchronous else guard,
        before_delete=verify_delete,
    )
    try:
        await mode._file(cast(Client, client), FakeMessage(1, copied=copied))
        assert checks == 2 and verified == [True] and client.deleted == [57]
        assert not store.list_directory("/")
        assert mode.active_imports == 0
    finally:
        mode.close()
        store.close()


@pytest.mark.asyncio
async def test_bot_rollback_does_not_delete_after_lineage_conflict(tmp_path):
    from tgdrive.recovery.backup import RecoveryConflict

    store = new_store(tmp_path / "drive")
    client = FakeBotClient()
    copied = SimpleNamespace(id=58)  # Invalid media causes rollback.

    async def verify_delete():
        raise RecoveryConflict("Recovery required")

    mode = root_mode(
        client=client,
        settings=settings(tmp_path),
        store=store,
        before_delete=verify_delete,
    )
    try:
        await mode._file(cast(Client, client), FakeMessage(1, copied=copied))
        assert client.deleted == []
        assert not store.list_directory("/")
        assert mode.active_imports == 0
    finally:
        mode.close()
        store.close()


@pytest.mark.asyncio
async def test_bot_active_import_counter_covers_copy_and_cancellation(tmp_path):
    store = new_store(tmp_path / "drive")
    client = FakeBotClient()
    mode = root_mode(client=client, settings=settings(tmp_path), store=store)
    entered = asyncio.Event()
    message = FakeMessage(1)

    async def copy(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    message.copy = copy
    task = asyncio.create_task(mode._file(cast(Client, client), message))
    try:
        await entered.wait()
        assert mode.active_imports == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert mode.active_imports == 0
    finally:
        mode.close()
        store.close()


@pytest.fixture
def bot(tmp_path):
    store = new_store(tmp_path / "drive")
    mode = root_mode(client=FakeBotClient(), settings=settings(tmp_path), store=store)
    try:
        yield mode
    finally:
        mode.close()
        store.close()


@pytest.mark.asyncio
async def test_success_feedback_names_actual_file_when_a_duplicate_is_renamed(bot):
    bot.store.add_file("/", "report.txt", 12, 3)
    copied = SimpleNamespace(
        id=59, document=SimpleNamespace(file_name="report.txt", file_size=3)
    )
    message = FakeMessage(1, copied=copied)
    await bot._file(bot.client, message)
    names = {item["name"] for item in bot.store.list_directory("/").values()}
    actual = next(name for name in names if name != "report.txt")
    assert actual in message.status.edit_text.calls[-1][0][0]
    assert bot.active_imports == 0


@pytest.mark.asyncio
async def test_failed_success_message_edit_does_not_roll_back_committed_upload(bot):
    copied = SimpleNamespace(id=60, photo=SimpleNamespace(file_size=3))
    message = FakeMessage(1, copied=copied)

    async def fail(*args, **kwargs):
        raise RuntimeError("message cannot be edited")

    message.status.edit_text = fail
    await bot._file(bot.client, message)
    assert len(bot.store.list_directory("/")) == 1
    assert not bot.client.deleted
    assert message.replies[-1][0].startswith("<b>Saved ✓</b>")


@pytest.mark.asyncio
async def test_paused_upload_explains_how_to_resume_without_copying(bot):
    from tgdrive.settings.managed_settings import SettingsProblem

    def guard():
        raise SettingsProblem("Connection restart required")

    bot.before_write = guard
    message = FakeMessage(1)
    await bot._file(bot.client, message)
    assert "Connection restart required" in message.replies[-1][0]
    assert "Open TGDrive" in message.replies[-1][0]
    assert not bot.store.list_directory("/")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["flood", "protected", "backup"])
async def test_copy_failures_give_specific_recovery_instructions(bot, failure):
    from telethon.errors import ChatForwardsRestrictedError, FloodWaitError

    from tgdrive.recovery.backup import BackupError

    errors = {
        "flood": (FloodWaitError(request=None, capture=15), "Wait 15 seconds"),
        "protected": (ChatForwardsRestrictedError(request=None), "new attachment"),
        "backup": (BackupError("synthetic backup failure"), "Storage and Backups"),
    }
    error, expected = errors[failure]
    message = FakeMessage(1)

    async def fail(*args, **kwargs):
        raise error

    message.copy = fail
    await bot._file(bot.client, message)
    assert expected in message.status.edit_text.calls[-1][0][0]
    assert not bot.store.list_directory("/")
    assert bot.active_imports == 0


@pytest.mark.asyncio
async def test_uncertain_rollback_asks_user_to_check_drive_before_resending(bot):
    from tgdrive.recovery.backup import RecoveryConflict

    async def block_delete():
        raise RecoveryConflict("synthetic lineage mismatch")

    bot.before_delete = block_delete
    message = FakeMessage(1, copied=SimpleNamespace(id=61))  # No usable media.
    await bot._file(bot.client, message)
    feedback = message.status.edit_text.calls[-1][0][0]
    assert "could not be confirmed" in feedback
    assert "before resending" in feedback
    assert not bot.client.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_fails", [False, True])
async def test_bot_preserves_saved_media_after_lost_commit_acknowledgement(
    tmp_path, mongo_backend, monkeypatch, reload_fails
):
    store = MongoStore.create(mongo_backend)
    client = FakeBotClient()
    mode = root_mode(client=client, settings=settings(tmp_path), store=store)
    transaction = mongo_backend.session_transaction
    acknowledgement_lost = False

    @contextmanager
    def lose_acknowledgement():
        nonlocal acknowledgement_lost
        if acknowledgement_lost and reload_fails:
            raise DatabaseUnavailable("Catalog reload unavailable")
        with transaction() as session:
            yield session
        if not acknowledgement_lost:
            acknowledgement_lost = True
            raise DatabaseUnavailable("Catalog commit acknowledgement lost")

    monkeypatch.setattr(mongo_backend, "session_transaction", lose_acknowledgement)
    message = FakeMessage(
        1,
        copied=SimpleNamespace(
            id=62, document=SimpleNamespace(file_name="saved.txt", file_size=3)
        ),
    )
    try:
        await mode._file(cast(Client, client), message)
        assert mongo_backend.db.nodes.find_one({"message_id": 62}) is not None
        assert client.deleted == []
        assert "could not be confirmed" in message.status.edit_text.calls[-1][0][0]
        assert mode.active_imports == 0
    finally:
        mode.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_message", [False, True])
async def test_bot_repeated_file_forwards_and_same_message_replay(bot, same_message):
    for incoming_id in (1, 1, 1) if same_message else (1, 2, 3):
        message = FakeMessage(
            1,
            copied=SimpleNamespace(
                id=70 + incoming_id,
                document=SimpleNamespace(file_name="report.pdf", file_size=3),
            ),
        )
        message.id = incoming_id
        await bot._file(bot.client, message)
        assert message.status.edit_text.calls[-1][0][0].startswith("<b>Saved ✓</b>")

    names = {item["name"] for item in bot.store.list_directory("/").values()}
    assert names == (
        {"report.pdf"}
        if same_message
        else {"report.pdf", "report (1).pdf", "report (2).pdf"}
    )
    assert bot.client.deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("same_account", [False, True])
async def test_bot_operation_identity_survives_token_rotation_and_bot_replacement(
    bot, same_account
):
    message = FakeMessage(
        1,
        copied=SimpleNamespace(
            id=80, document=SimpleNamespace(file_name="report.txt", file_size=3)
        ),
    )
    await bot._file(bot.client, message)
    bot_id = "123456" if same_account else "654321"
    configured = bot.settings.model_copy(
        update={"main_bot_token": SecretStr(bot_id + ":" + "z" * 30)}
    )
    replacement = root_mode(
        client=FakeBotClient(), settings=configured, store=bot.store
    )
    copied = SimpleNamespace(
        id=80 if same_account else 81,
        document=SimpleNamespace(file_name="report.txt", file_size=3),
    )
    try:
        replay = FakeMessage(1, copied=copied)
        await replacement._file(replacement.client, replay)
        assert len(bot.store.list_directory("/")) == (1 if same_account else 2)
        assert replay.status.edit_text.calls[-1][0][0].startswith("<b>Saved ✓</b>")
        assert cast(FakeBotClient, replacement.client).deleted == []
    finally:
        replacement.close()


@pytest.mark.asyncio
async def test_bot_replay_preserves_saved_media_when_write_guard_pauses(bot):
    from tgdrive.settings.managed_settings import SettingsProblem

    message = FakeMessage(
        1,
        copied=SimpleNamespace(
            id=92, document=SimpleNamespace(file_name="saved.txt", file_size=3)
        ),
    )
    await bot._file(bot.client, message)
    checks = 0

    def pause():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise SettingsProblem("Connection restart required")

    bot.before_write = pause
    await bot._file(bot.client, message)

    assert checks == 2
    assert len(bot.store.list_directory("/")) == 1
    assert bot.client.deleted == []
    assert "could not be confirmed" in message.status.edit_text.calls[-1][0][0]


class ProgressStatus:
    def __init__(self, *, flood_once=False) -> None:
        self.edits: list[tuple[str, Any]] = []
        self.flood_once = flood_once
        self.modes: list[str | None] = []

    async def edit_text(
        self,
        text: str,
        reply_markup: Any | None = None,
        *,
        parse_mode: str | None = None,
    ) -> None:
        if self.flood_once:
            from telethon.errors import FloodWaitError

            self.flood_once = False
            raise FloodWaitError(request=None, capture=30)
        self.edits.append((text, reply_markup))
        self.modes.append(parse_mode)


@pytest.mark.asyncio
async def test_progress_edits_are_throttled_by_time_percent_and_flood_wait(bot):
    now = 0.0
    bot.clock = lambda: now
    status = ProgressStatus(flood_once=True)
    report = bot._reporter(status)
    record = SimpleNamespace(
        phase="downloading", current=0, total=100 * 1024**2, context={"name": "a.mkv"}
    )

    async def at(seconds, current, phase="downloading"):
        nonlocal now
        now = seconds
        record.current, record.phase = current, phase
        await report(record)

    mib = 1024**2
    await at(0, 0)  # FloodWait: wait 30 s before the next edit.
    await at(10, 20 * mib)
    assert status.edits == []
    await at(31, 42 * mib)
    assert status.edits[-1][0] == "<b>Receiving</b> · 42%\n42.0 MB / 100.0 MB"
    await at(33, 60 * mib)  # Under the edit interval.
    await at(40, 44 * mib)  # Under 5 percentage points.
    assert len(status.edits) == 1
    await at(41, 60 * mib)
    await at(43, 10 * mib, "uploading")  # Under the edit interval.
    await at(46, 10 * mib, "uploading")  # A new stage shows despite a lower %.
    assert [
        telegram_html.parse(text)[0].split(" ", 1)[0] for text, _ in status.edits
    ] == [
        "Receiving",
        "Receiving",
        "Saving",
    ]
    assert status.modes == ["html"] * 3


class FakeTransfers:
    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.calls = 0

    async def receive_telegram_file(
        self, download, local_path, destination, name, size, *, transfer_id
    ):
        self.calls += 1
        record = SimpleNamespace(
            id=transfer_id,
            phase="downloading",
            status=SimpleNamespace(value="running"),
            current=0,
            total=size,
            error=None,
            context={"name": name, "size": size},
        )

        async def run():
            await download(local_path, None)
            if self.fail:
                record.status.value, record.error = "error", self.fail
            else:
                record.status.value = "completed"

        record.task = asyncio.create_task(run())
        return record


class FakeDownloadClient(FakeBotClient):
    async def download_media(self, _message, *, file_name, progress):
        Path(file_name).write_bytes(b"abc")


def encrypted_bot(tmp_path, transfers, site_url=None):
    store = new_store(tmp_path / "drive")
    folder = store.create_folder("/", "Inbox")
    mode = root_mode(
        client=FakeDownloadClient(),
        settings=settings(tmp_path),
        store=store,
        transfers=transfers,
    )
    mode.folder_settings.value = BotUploadPreferences(
        folder_path=f"/{folder['id']}", site_url=site_url
    )
    return mode, folder


@pytest.mark.asyncio
async def test_encrypted_import_reports_saved_file_with_open_button(tmp_path):
    transfers = FakeTransfers()
    mode, folder = encrypted_bot(tmp_path, transfers, "https://drive.example")
    message = FakeMessage(1)
    message.document = SimpleNamespace(file_name="movie.mkv", file_size=3 * 1024**2)
    try:
        await mode._file(mode.client, message)
        assert message.replies[0] == (
            "<b>Saving…</b>\nFolder · <code>My Files / Inbox</code>",
            None,
        )
        assert message.reply_modes == ["html"]
        text = message.status.edit_text.calls[-1][0][0]
        buttons = message.status.edit_text.calls[-1][1]["reply_markup"]
        assert text == (
            "<b>Saved ✓</b>\n<code>movie.mkv</code>\n"
            "3.0 MB · <code>My Files / Inbox</code>"
        )
        assert message.status.edit_text.calls[-1][1]["parse_mode"] == "html"
        assert buttons[0][0].text == "Open in TGDrive"
        assert (
            buttons[0][0].type.url == f"https://drive.example/?path=%2F{folder['id']}"
        )
        assert transfers.calls == 1 and mode.active_imports == 0
    finally:
        mode.store.close()


@pytest.mark.asyncio
async def test_encrypted_import_failure_explains_where_to_retry(tmp_path):
    mode, _ = encrypted_bot(tmp_path, FakeTransfers(fail="Telegram rejected it"))
    message = FakeMessage(1)
    message.document = SimpleNamespace(file_name="movie.mkv", file_size=3)
    try:
        await mode._file(mode.client, message)
        text = message.status.edit_text.calls[-1][0][0]
        assert "Telegram rejected it" in text and "Transfers in TGDrive" in text
    finally:
        mode.store.close()


@pytest.mark.asyncio
async def test_strangers_get_one_private_bot_notice(tmp_path):
    client = FakeBotClient()
    mode = root_mode(client=client, settings=settings(tmp_path), store=new_store())
    try:
        first, second = FakeMessage(4242), FakeMessage(4242)
        await mode._unauthorized(cast(Client, client), first)
        await mode._unauthorized(cast(Client, client), second)
        assert "4242" in first.replies[-1][0]
        assert second.replies == []
    finally:
        mode.close()


@pytest.mark.asyncio
async def test_bot_encryption_unavailable_never_falls_back_to_plaintext(bot):
    bot.folder_settings.value = BotUploadPreferences(folder_path="/", encrypt=True)
    message = FakeMessage(1)
    message.document = SimpleNamespace(file_name="private.txt", file_size=3)
    message.copy = AsyncMock(
        return_value=SimpleNamespace(id=333, document=message.document)
    )

    await bot._file(bot.client, message)

    message.copy.assert_not_awaited()
    assert "encrypted" in message.replies[-1][0].lower()
    assert not bot.store.list_directory("/")
    assert bot.active_imports == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "completed", "other", "missing"])
async def test_bot_cancel_reply_targets_only_its_matching_import(bot, state):
    record = SimpleNamespace(
        context={"source_kind": "web" if state == "other" else "telegram"},
        status=SimpleNamespace(value=state),
    )

    def get(_identity):
        if state == "missing":
            raise KeyError(_identity)
        return record

    cancel = AsyncMock(
        return_value=SimpleNamespace(status=SimpleNamespace(value="cancelling"))
    )
    bot.transfers = SimpleNamespace(
        registry=SimpleNamespace(get=get), cancel_job=cancel
    )
    message = FakeMessage(1, text="/cancel")
    message.native = SimpleNamespace(chat_id=1, reply_to_msg_id=77)

    await bot._cancel(bot.client, message)

    if state == "running":
        identity = hashlib.sha256(b"bot:123456:1:77").hexdigest()[:32]
        cancel.assert_awaited_once_with(identity)
        assert "Cancellation requested" in message.replies[-1][0]
    else:
        cancel.assert_not_awaited()
        assert "Transfers" in message.replies[-1][0]
    assert not bot.client.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True])
async def test_bot_help_explains_mode_and_reply_cancellation(bot, encrypted):
    bot.folder_settings.value = BotUploadPreferences(folder_path="/", encrypt=encrypted)
    message = FakeMessage(1)
    await bot._help(bot.client, message)
    text = message.replies[-1][0]
    assert f"<b>Encryption</b> · {'On' if encrypted else 'Off'}" in text
    assert "/cancel" in text and "reply" in text.lower()


@pytest.mark.asyncio
async def test_bot_unknown_size_and_wait_changes_keep_progress_truthful(bot):
    now = 0.0
    bot.clock = lambda: now
    status = ProgressStatus()
    report = bot._reporter(status)
    record = SimpleNamespace(
        phase="downloading", current=512, total=0, context={"name": "unknown.bin"}
    )
    await report(record)
    assert "512 B" in status.edits[-1][0] and "of 0" not in status.edits[-1][0]
    now = 5
    record.current = 1024
    await report(record)
    assert "1.0 KB" in status.edits[-1][0]
    now = 10
    record.phase = "queued"
    await report(record)
    now = 15
    record.phase = "waiting_for_telegram"
    await report(record)
    assert "retrying automatically" in status.edits[-1][0].lower()
    assert len(status.edits) == 4


@pytest.mark.asyncio
async def test_bot_cancel_cannot_select_another_private_chats_import(bot):
    identity = hashlib.sha256(b"bot:123456:1:77").hexdigest()[:32]
    record = SimpleNamespace(context={"source_kind": "telegram"})
    cancel = AsyncMock()
    bot.transfers = SimpleNamespace(
        registry=SimpleNamespace(get={identity: record}.__getitem__), cancel_job=cancel
    )
    message = FakeMessage(2, text="/cancel")
    message.native = SimpleNamespace(chat_id=2, reply_to_msg_id=77)

    await bot._cancel(bot.client, message)

    cancel.assert_not_awaited()
    assert "<b>No matching encrypted import</b>" in message.replies[-1][0]


@pytest.mark.asyncio
async def test_bot_stranger_notices_keep_bounded_recent_memory(bot):
    for identity in range(1100):
        await bot._unauthorized(bot.client, FakeMessage(identity))
    assert len(bot._answered_strangers) <= 1024
    repeated = FakeMessage(1099)
    await bot._unauthorized(bot.client, repeated)
    assert repeated.replies == []


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [False, True])
async def test_start_is_short_and_help_keeps_commands_and_safety_guidance(
    bot, encrypted
):
    bot.folder_settings.value = BotUploadPreferences(folder_path="/", encrypt=encrypted)
    welcome = FakeMessage(1, text="/start@drive_bot")
    await bot._help(bot.client, welcome)
    text = telegram_html.parse(welcome.replies[-1][0])[0]
    assert "/cancel" not in text and "Commands" not in text
    assert len(text.splitlines()) <= 8
    assert "Encryption" in text and "Folder" in text
    assert (
        "encrypted through this server" if encrypted else "readable by channel members"
    ) in text
    help_message = FakeMessage(1, text="Help")
    await bot._help(bot.client, help_message)
    help_text = telegram_html.parse(help_message.replies[-1][0])[0]
    assert all(
        command in help_text
        for command in ("/current_folder", "/set_folder", "/cancel")
    )
    assert "reply to the original file" in help_text
    assert "encrypted import" in help_text and "full quality" in help_text
    assert welcome.reply_modes == help_message.reply_modes == ["html"]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["_help", "_current_folder", "_set_folder"])
async def test_folder_copy_escapes_markup_in_all_folder_messages(
    bot, monkeypatch, handler
):
    folder = 'My Files / <a href="https://unexpected.example">A & B</a>'
    monkeypatch.setattr(
        bot.folder_settings,
        "snapshot",
        AsyncMock(
            return_value={
                "folder_label": folder,
                "encrypt": True,
                "settings_url": None,
            }
        ),
    )
    message = FakeMessage(1)
    await getattr(bot, handler)(bot.client, message)
    text = message.replies[-1][0]
    plain, entities = telegram_html.parse(text)
    assert folder in plain
    assert '<a href="https://unexpected.example">' not in text
    assert not any(
        type(entity).__name__ == "MessageEntityTextUrl" for entity in entities
    )
    assert message.reply_modes == ["html"]


@pytest.mark.asyncio
async def test_saved_copy_escapes_actual_filename_and_folder(bot):
    name = '<b>A & "notes"</b>.txt'
    folder = "My Files / <i>Inbox & reports</i>"
    message = FakeMessage(1)
    await bot._saved(message, message.status, {"name": name, "size": 123}, "/", folder)
    text = message.status.edit_text.calls[-1][0][0]
    plain, entities = telegram_html.parse(text)
    assert name in plain and folder in plain and "123 B" in plain
    assert "<b>A" not in text and "<i>Inbox" not in text
    assert not any(
        type(entity).__name__ == "MessageEntityItalic" for entity in entities
    )
    assert message.status.edit_text.calls[-1][1]["parse_mode"] == "html"


@pytest.mark.asyncio
async def test_paused_copy_escapes_error_details_without_copying(bot):
    detail = '<a href="https://unexpected.example">repair & retry</a>'

    def pause():
        raise SettingsProblem(detail)

    bot.before_write = pause
    message = FakeMessage(1)
    message.copy = AsyncMock()
    await bot._file(bot.client, message)
    text = message.replies[-1][0]
    plain, entities = telegram_html.parse(text)
    assert detail in plain and "Upload paused" in plain
    assert not any(
        type(entity).__name__ == "MessageEntityTextUrl" for entity in entities
    )
    assert message.reply_modes == ["html"]
    message.copy.assert_not_awaited()


@pytest.mark.parametrize(
    "phase,label",
    [
        ("downloading", "Receiving"),
        ("uploading", "Saving"),
        ("finalizing", "Finalizing"),
        ("verifying", "Verifying"),
    ],
)
def test_progress_has_distinct_stage_bytes_and_no_repeated_filename(phase, label):
    from tgdrive.telegram.bot_mode import _progress

    record = SimpleNamespace(
        phase=phase, current=42, total=100, context={"name": "private-file-name.txt"}
    )
    result = _progress(record)
    assert result is not None
    text, percent = result
    assert telegram_html.parse(text)[0] == f"{label} · 42%\n42 B / 100 B"
    assert percent == 42 and "private-file-name" not in text
