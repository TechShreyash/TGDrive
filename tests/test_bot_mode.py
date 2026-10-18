from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tgdrive.bot_mode import BotMode
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
        self.command = command or []
        self.copied = copied
        self.replies: list[tuple[str, Any | None]] = []

    async def reply_text(self, text: str, reply_markup: Any | None = None) -> None:
        self.replies.append((text, reply_markup))

    async def copy(self, _channel: int, **_kwargs: object) -> Any:
        return self.copied


@pytest.mark.asyncio
async def test_folder_choices_filter_files_and_are_scoped_per_admin(
    tmp_path: Path,
) -> None:
    store = DriveStore.create(tmp_path / "drive.json")
    folder = store.create_folder("/", "Match folder")
    store.add_file("/", "Match file.txt", 7, 1)
    mode = BotMode(
        client=FakeBotClient(),
        settings=settings(tmp_path),
        store=store,
    )
    message = FakeMessage(1)

    await mode._show_folder_choices(message, "Match")

    assert len(mode._choices) == 1
    request_id, choice = next(iter(mode._choices.items()))
    assert choice.user_id == 1
    assert set(choice.folders) == {folder["id"]}
    markup = message.replies[0][1]
    assert len(markup) == 1

    callback_message = SimpleNamespace(edit_text=_async_recorder())
    callback = FakeCallback(
        user_id=1,
        data=f"folder:{request_id}:{folder['id']}",
        message=callback_message,
    )
    await mode._folder_callback(FakeBotClient(), callback)
    assert mode._selected[1][1] == "Match folder"
    assert 2 not in mode._selected


def _async_recorder():
    calls: list[object] = []

    async def record(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    record.calls = calls
    return record


class FakeCallback:
    def __init__(self, user_id: int, data: str, message: Any) -> None:
        self.id = 1
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = message
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


@pytest.mark.asyncio
async def test_pending_folder_query_ignores_the_command_update(tmp_path: Path) -> None:
    store = DriveStore.create(tmp_path / "drive.json")
    store.create_folder("/", "Target")
    mode = BotMode(client=FakeBotClient(), settings=settings(tmp_path), store=store)
    command = FakeMessage(1, text="/set_folder", command=["set_folder"])

    await mode._set_folder(FakeBotClient(), command)
    await mode._folder_query(FakeBotClient(), command)
    assert 1 in mode._pending_queries

    query = FakeMessage(1, text="Target")
    await mode._folder_query(FakeBotClient(), query)
    assert 1 not in mode._pending_queries
    assert mode._choices


@pytest.mark.asyncio
async def test_photo_upload_gets_fallback_name_and_metadata(tmp_path: Path) -> None:
    store = DriveStore.create(tmp_path / "drive.json")
    client = FakeBotClient()
    mode = BotMode(client=client, settings=settings(tmp_path), store=store)
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
    assert message.replies[0][0].startswith("Uploaded")
    assert client.deleted == []


@pytest.mark.asyncio
async def test_bot_upload_rolls_back_copy_when_metadata_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DriveStore.create(tmp_path / "drive.json")
    client = FakeBotClient()
    mode = BotMode(client=client, settings=settings(tmp_path), store=store)
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
    assert message.replies[-1][0].startswith("Upload failed")


@pytest.mark.asyncio
async def test_bot_mode_lifecycle_is_idempotent(tmp_path: Path) -> None:
    store = DriveStore.create(tmp_path / "drive.json")
    client = FakeLifecycleClient()
    mode = BotMode(client=client, settings=settings(tmp_path), store=store)

    await mode.start()
    await mode.start()
    assert client.starts == 1
    assert len(client.handlers) == 1

    await mode.stop()
    await mode.stop()
    assert client.stops == 1


@pytest.mark.asyncio
async def test_bot_import_guard_before_copy_and_after_copy_preserves_metadata(tmp_path):
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

    verified = []

    async def verify_delete():
        verified.append(True)

    mode = BotMode(
        client=client,
        settings=settings(tmp_path),
        store=store,
        before_write=guard,
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

    mode = BotMode(
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
    mode = BotMode(client=client, settings=settings(tmp_path), store=store)
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
