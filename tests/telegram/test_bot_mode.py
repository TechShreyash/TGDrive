from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from tests.support.mongo_fixtures import register_test_backend
from tgdrive.core.config import Settings
from tgdrive.settings.bot_settings import BotUploadPreferences
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.bot_mode import BotMode


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
async def test_bot_upload_preserves_copy_when_metadata_fails(
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

    assert client.deleted == []
    assert not store.list_directory("/")
    assert "could not be confirmed" in message.status.edit_text.calls[-1][0][0]


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
    from tgdrive.settings.managed_settings import SettingsProblem

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
    from tgdrive.recovery.backup import RecoveryConflict

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
    from tgdrive.recovery.backup import RecoveryConflict

    async def block_delete():
        raise RecoveryConflict("synthetic lineage mismatch")

    bot.before_delete = block_delete
    message = FakeMessage(1, copied=SimpleNamespace(id=61))  # No usable media.
    await bot._file(bot.client, message)
    feedback = message.status.edit_text.calls[-1][0][0]
    assert "could not be confirmed" in feedback
    assert "before sending this file again" in feedback
    assert not bot.client.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_fails", [False, True])
async def test_bot_preserves_saved_media_after_lost_commit_acknowledgement(
    tmp_path, mongo_backend, monkeypatch, reload_fails
):
    store = MongoStore.create(mongo_backend)
    client = FakeBotClient()
    mode = root_mode(client=client, settings=settings(tmp_path), store=store)
    transaction = mongo_backend.transaction
    acknowledgement_lost = False

    def lose_acknowledgement(callback):
        nonlocal acknowledgement_lost
        if acknowledgement_lost and reload_fails:
            raise DatabaseUnavailable("Catalog reload unavailable")
        result = transaction(callback)
        if not acknowledgement_lost:
            acknowledgement_lost = True
            raise DatabaseUnavailable("Catalog commit acknowledgement lost")
        return result

    monkeypatch.setattr(mongo_backend, "transaction", lose_acknowledgement)
    message = FakeMessage(
        1,
        copied=SimpleNamespace(
            id=62, document=SimpleNamespace(file_name="saved.txt", file_size=3)
        ),
    )
    try:
        await mode._file(client, message)
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
        assert message.status.edit_text.calls[-1][0][0].startswith("Uploaded")

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
        assert replay.status.edit_text.calls[-1][0][0].startswith("Uploaded")
        assert replacement.client.deleted == []
    finally:
        replacement.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_media", [False, True])
@pytest.mark.parametrize("trashed", [False, True])
async def test_bot_legacy_replay_reuses_only_matching_stored_media(
    bot, same_media, trashed
):
    legacy_operation_id = hashlib.sha256(b"bot:1:1").hexdigest()
    item = bot.store.add_file(
        "/", "report.txt", 90, 3, operation_id=legacy_operation_id
    )
    if trashed:
        bot.store.set_trashed("/" + item["id"], True)
    message = FakeMessage(
        1,
        copied=SimpleNamespace(
            id=90 if same_media else 91,
            document=SimpleNamespace(file_name="report.txt", file_size=3),
        ),
    )

    await bot._file(bot.client, message)

    assert len(bot.store.list_directory("/", include_trashed=True)) == (
        1 if same_media else 2
    )
    assert message.status.edit_text.calls[-1][0][0].startswith("Uploaded")
    assert bot.client.deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_bot_replay_preserves_saved_media_when_write_guard_pauses(bot, legacy):
    from tgdrive.settings.managed_settings import SettingsProblem

    message = FakeMessage(
        1,
        copied=SimpleNamespace(
            id=92, document=SimpleNamespace(file_name="saved.txt", file_size=3)
        ),
    )
    if legacy:
        bot.store.add_file(
            "/",
            "saved.txt",
            92,
            3,
            operation_id=hashlib.sha256(b"bot:1:1").hexdigest(),
        )
    else:
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
