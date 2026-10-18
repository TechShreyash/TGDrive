"""Optional Telegram bot-mode uploads with per-admin folder state."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import sqlite3
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from telethon import Button

from tgdrive.config import Settings
from tgdrive.security import safe_name
from tgdrive.store import DriveStore
from tgdrive.telegram_clients import Client

Message = Any
CallbackQuery = Any

logger = logging.getLogger(__name__)
_MEDIA_ATTRIBUTES = (
    "document",
    "video",
    "audio",
    "photo",
    "sticker",
    "animation",
    "voice",
    "video_note",
)


@dataclass(slots=True)
class _FolderChoice:
    user_id: int
    expires_at: float
    folders: dict[str, tuple[str, str]]


class BotMode:
    def __init__(
        self,
        *,
        client: Client,
        settings: Settings,
        store: DriveStore,
        before_write: Callable[[], None] | None = None,
        before_delete: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.store = store
        self.before_write = before_write
        self.before_delete = before_delete
        self.active_imports = 0
        self._pending_queries: dict[int, float] = {}
        self._choices: dict[str, _FolderChoice] = {}
        settings.resolved_data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        from tgdrive.mongo import for_settings

        backend = for_settings(settings)
        self._mongo_backend = backend
        self._mongo = backend.db.bot_folders if backend else None
        self._db = None
        self._finalizer = lambda: None
        if self._mongo is not None:
            self._selected = {
                row["_id"]: (row["path"], row["name"]) for row in self._mongo.find({})
            }
        else:
            database = settings.resolved_data_dir / "bot-settings.sqlite3"
            self._db = sqlite3.connect(database)
            self._finalizer = weakref.finalize(self, self._db.close)
            database.chmod(0o600)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS folders "
                "(user_id INTEGER PRIMARY KEY, path TEXT, name TEXT)"
            )
            self._db.commit()
            self._selected = {
                row[0]: (row[1], row[2])
                for row in self._db.execute("SELECT * FROM folders")
            }
        self._registered = False
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        if not self._registered:
            self.client.register_bot(self)
            self._registered = True
        await self.client.start()
        self._started = True
        logger.info("Telegram bot mode started")

    async def stop(self) -> None:
        if getattr(self.client, "is_connected", False):
            await self.client.stop()
        self._started = False
        logger.info("Telegram bot mode stopped")

    def close(self) -> None:
        """Release local settings after handlers have stopped."""
        self._finalizer()

    async def _help(self, _client: Client, message: Message) -> None:
        await message.reply_text(
            "TGDrive bot mode\n\n"
            "Send or forward a file to upload it.\n"
            "/set_folder [name] — choose an upload folder\n"
            "/current_folder — show the current upload folder"
        )

    async def _set_folder(self, _client: Client, message: Message) -> None:
        user_id = message.from_user.id
        query = " ".join(message.command[1:]).strip()
        if query:
            await self._show_folder_choices(message, query)
            return
        self._pending_queries[user_id] = time.monotonic() + 60
        await message.reply_text(
            "Send the folder name within 60 seconds, or /cancel to stop."
        )

    async def _folder_query(self, _client: Client, message: Message) -> None:
        user_id = message.from_user.id
        deadline = self._pending_queries.get(user_id)
        if deadline is None:
            return
        text = (message.text or "").strip()
        if text.startswith("/") and text.lower() != "/cancel":
            return
        self._pending_queries.pop(user_id, None)
        if deadline < time.monotonic():
            await message.reply_text("Folder selection expired. Use /set_folder again.")
            return
        if text.lower() == "/cancel":
            await message.reply_text("Folder selection cancelled.")
            return
        await self._show_folder_choices(message, text)

    async def _show_folder_choices(self, message: Message, query: str) -> None:
        self._prune_choices()
        folders = {
            item_id: item
            for item_id, item in self.store.search(query).items()
            if item["type"] == "folder"
        }
        if not folders:
            await message.reply_text("No matching folder was found.")
            return
        request_id = secrets.token_urlsafe(6)
        options: dict[str, tuple[str, str]] = {}
        buttons: list[list[Any]] = []
        for item_id, item in list(folders.items())[:20]:
            path = f"{item['path'].rstrip('/')}/{item_id}"
            options[item_id] = (path, item["name"])
            buttons.append(
                [
                    Button.inline(
                        item["name"][:48],
                        data=f"folder:{request_id}:{item_id}",
                    )
                ]
            )
        self._choices[request_id] = _FolderChoice(
            user_id=message.from_user.id,
            expires_at=time.monotonic() + 300,
            folders=options,
        )
        await message.reply_text(
            "Choose the upload folder:",
            reply_markup=buttons,
        )

    async def _folder_callback(self, _client: Client, callback: CallbackQuery) -> None:
        self._prune_choices()
        try:
            _, request_id, folder_id = callback.data.split(":", 2)
        except (AttributeError, ValueError):
            await callback.answer("Invalid folder selection", show_alert=True)
            return
        choice = self._choices.pop(request_id, None)
        if (
            choice is None
            or choice.user_id != callback.from_user.id
            or choice.expires_at < time.monotonic()
            or folder_id not in choice.folders
        ):
            await callback.answer("This folder selection expired", show_alert=True)
            return
        path, name = choice.folders[folder_id]
        try:
            self.store.get_directory(path)
        except Exception:
            await callback.answer("That folder no longer exists", show_alert=True)
            return
        if self._mongo is not None:
            await asyncio.to_thread(
                self._mongo_backend.write,
                self._mongo,
                "replace_one",
                {"_id": callback.from_user.id},
                {"_id": callback.from_user.id, "path": path, "name": name},
                upsert=True,
            )
        else:
            with self._db:
                self._db.execute(
                    "INSERT INTO folders VALUES (?,?,?) ON CONFLICT(user_id) "
                    "DO UPDATE SET path=excluded.path,name=excluded.name",
                    (callback.from_user.id, path, name),
                )
        self._selected[callback.from_user.id] = (path, name)
        await callback.answer("Upload folder updated")
        if callback.message:
            await callback.message.edit_text(f"Upload folder set to: {name}")

    async def _current_folder(self, _client: Client, message: Message) -> None:
        _, name = self._selected.get(message.from_user.id, ("/", "/ (root directory)"))
        await message.reply_text(f"Current upload folder: {name}")

    async def _file(self, _client: Client, message: Message) -> None:
        self.active_imports += 1
        try:
            await self._import_file(_client, message)
        finally:
            self.active_imports -= 1

    async def _import_file(self, _client: Client, message: Message) -> None:
        user_id = message.from_user.id
        path, folder_name = self._selected.get(user_id, ("/", "/ (root directory)"))
        try:
            self.store.get_directory(path)
        except Exception:
            path, folder_name = "/", "/ (root directory)"
            self._selected[user_id] = (path, folder_name)

        source_chat = getattr(
            getattr(message, "native", None), "chat_id", message.from_user.id
        )
        copied: Message | None = None
        try:
            if self.before_write is not None:
                self.before_write()
            copied = await message.copy(
                self.settings.storage_channel,
                disable_notification=True,
            )
            media = self._media(copied)
            if media is None:
                raise RuntimeError("copied message contains no media")
            filename = self._media_name(media, copied)
            if self.before_write is not None:
                self.before_write()
            await asyncio.to_thread(
                self.store.add_file,
                path,
                filename,
                copied.id,
                getattr(media, "file_size", 0) or 0,
                operation_id=hashlib.sha256(
                    f"bot:{source_chat}:{message.id}".encode()
                ).hexdigest(),
                keep_both=True,
            )
        except Exception:
            if copied is not None:
                try:
                    if self.before_delete is not None:
                        await self.before_delete()
                    await self.client.delete_messages(
                        self.settings.storage_channel, copied.id
                    )
                except Exception:
                    logger.exception("Failed to roll back copied bot-mode media")
            logger.exception("Bot-mode upload failed")
            await message.reply_text("Upload failed. Check the server logs.")
            return
        await message.reply_text(f"Uploaded {filename} to {folder_name}.")

    @staticmethod
    def _media(message: Message) -> Any | None:
        for attribute in _MEDIA_ATTRIBUTES:
            media = getattr(message, attribute, None)
            if media is not None:
                return media
        return None

    @staticmethod
    def _media_name(media: Any, message: Message) -> str:
        explicit = getattr(media, "file_name", None)
        if explicit:
            return safe_name(explicit)
        kind = getattr(getattr(message, "media", None), "value", "file")
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        extensions = {
            "photo": ".jpg",
            "voice": ".ogg",
            "video": ".mp4",
            "video_note": ".mp4",
            "animation": ".mp4",
            "sticker": ".webp",
        }
        return f"{kind}-{timestamp}-{message.id}{extensions.get(kind, '')}"

    def _prune_choices(self) -> None:
        now = time.monotonic()
        expired = [
            request_id
            for request_id, choice in self._choices.items()
            if choice.expires_at < now
        ]
        for request_id in expired:
            self._choices.pop(request_id, None)
        while len(self._choices) >= 100:
            oldest = min(
                self._choices,
                key=lambda request_id: self._choices[request_id].expires_at,
            )
            self._choices.pop(oldest, None)
