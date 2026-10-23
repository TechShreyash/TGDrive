from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tgdrive.bot_mode import BotMode
from tgdrive.bot_settings import BotUploadPreferences
from tgdrive.config import Settings
from tgdrive.store import DriveStore


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        API_ID=12345,
        API_HASH="a" * 32,
        BOT_TOKENS="123456:" + "x" * 30,
        STORAGE_CHANNEL=-1001234567890,
        DATABASE_BACKUP_MSG_ID=42,
        ADMIN_PASSWORD="correct horse battery staple",
        SESSION_SECRET="s" * 48,
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
        self.status = SimpleNamespace(edit_text=_async_recorder())

    async def reply_text(self, text: str, reply_markup: Any | None = None) -> Any:
        self.replies.append((text, reply_markup))
        return self.status

    async def copy(self, _channel: int, **_kwargs: object) -> Any:
        return self.copied


def _async_recorder():
    calls: list[object] = []

    async def record(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    record.calls = calls
    return record


def root_mode(**kwargs):
    mode = BotMode(**kwargs)
    mode.folder_settings.value = BotUploadPreferences(folder_path="/")
    mode.folder_settings._loaded = True
    return mode


@pytest.mark.asyncio
async def test_photo_upload_gets_fallback_name_and_metadata(tmp_path: Path) -> None:
    store = DriveStore.create(tmp_path / "drive.json")
    client = FakeBotClient()
    mode = root_mode(client=client, settings=settings(tmp_path), store=store)
    copied = SimpleNamespace(
        id=55,
        media=SimpleNamespace(value="photo"),
        photo=SimpleNamespace(file_size=3),
    )
    message = FakeMessage(1, copied=copied)

    await mode._file(client, message)

    item = next(iter(store.list_directory("/").values()))
    assert item["name"].startswith("photo-")
    assert item["name"].endswith("-55.jpg")
    assert message.replies[0][0].startswith("Saving")
    assert message.status.edit_text.calls[-1][0][0].startswith("Uploaded")
    assert client.deleted == []


@pytest.mark.asyncio
async def test_bot_upload_rolls_back_copy_when_metadata_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DriveStore.create(tmp_path / "drive.json")
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
    await mode._file(client, message)

    assert client.deleted == [56]
    assert message.status.edit_text.calls[-1][0][0].startswith("Upload failed")


@pytest.mark.asyncio
async def test_bot_mode_lifecycle_is_idempotent(tmp_path: Path) -> None:
    store = DriveStore.create(tmp_path / "drive.json")
    client = FakeLifecycleClient()
    mode = root_mode(client=client, settings=settings(tmp_path), store=store)

    await mode.start()
    await mode.start()
    assert client.starts == 1
    assert len(client.handlers) == 1

    await mode.stop()
    await mode.stop()
    assert client.stops == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_bot_import_guard_before_copy_and_after_copy_preserves_metadata(
    tmp_path, asynchronous
):
    from tgdrive.managed_settings import SettingsProblem

    store = DriveStore.create(tmp_path / "drive.sqlite3")
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
        await mode._file(client, FakeMessage(1, copied=copied))
        assert checks == 2 and verified == [True] and client.deleted == [57]
        assert not store.list_directory("/")
        assert mode.active_imports == 0
    finally:
        mode.close()
        store.close()


@pytest.mark.asyncio
async def test_bot_rollback_does_not_delete_after_lineage_conflict(tmp_path):
    from tgdrive.backup import RecoveryConflict

    store = DriveStore.create(tmp_path / "drive.sqlite3")
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
        await mode._file(client, FakeMessage(1, copied=copied))
        assert client.deleted == []
        assert not store.list_directory("/")
        assert mode.active_imports == 0
    finally:
        mode.close()
        store.close()


@pytest.mark.asyncio
async def test_bot_active_import_counter_covers_copy_and_cancellation(tmp_path):
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    client = FakeBotClient()
    mode = root_mode(client=client, settings=settings(tmp_path), store=store)
    entered = asyncio.Event()
    message = FakeMessage(1)

    async def copy(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    message.copy = copy
    task = asyncio.create_task(mode._file(client, message))
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
    store = DriveStore.create(tmp_path / "drive.sqlite3")
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
    assert message.replies[-1][0].startswith("Uploaded")


@pytest.mark.asyncio
async def test_paused_upload_explains_how_to_resume_without_copying(bot):
    from tgdrive.managed_settings import SettingsProblem

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

    from tgdrive.backup import BackupError

    errors = {
        "flood": (FloodWaitError(request=None, capture=15), "Wait 15 seconds"),
        "protected": (ChatForwardsRestrictedError(request=None), "new attachment"),
        "backup": (BackupError("synthetic backup failure"), "Storage & Backups"),
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
    from tgdrive.backup import RecoveryConflict

    async def block_delete():
        raise RecoveryConflict("synthetic lineage mismatch")

    bot.before_delete = block_delete
    message = FakeMessage(1, copied=SimpleNamespace(id=61))  # No usable media.
    await bot._file(bot.client, message)
    feedback = message.status.edit_text.calls[-1][0][0]
    assert "could not be confirmed" in feedback
    assert "before sending this file again" in feedback
    assert not bot.client.deleted
