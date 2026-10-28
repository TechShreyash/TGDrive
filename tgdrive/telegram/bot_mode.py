"""Forward-to-save Telegram interface with a website-managed upload folder."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from telethon import Button
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError

from tgdrive.core.config import Settings
from tgdrive.core.security import safe_name
from tgdrive.recovery.backup import BackupError
from tgdrive.settings.bot_settings import BotSettings
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.clients import Client

Message = Any

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


class BotMode:
    def __init__(
        self,
        *,
        client: Client,
        settings: Settings,
        store: DriveStore,
        before_write: Callable[[], Awaitable[None] | None] | None = None,
        before_delete: Callable[[], Awaitable[None]] | None = None,
        folder_settings: BotSettings | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.store = store
        self.before_write = before_write
        self.before_delete = before_delete
        self.active_imports = 0
        if folder_settings is None:
            from tgdrive.storage.mongo import for_settings

            folder_settings = BotSettings(
                settings, SimpleNamespace(store=store, mongo=for_settings(settings))
            )
        self.folder_settings = folder_settings
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
        """Folder preferences have no open local handles."""

    @staticmethod
    def _menu() -> list[list[Any]]:
        return [
            [
                Button.text("Current folder", resize=True, persistent=True),
                Button.text("Help", resize=True, persistent=True),
            ]
        ]

    async def _help(self, _client: Client, message: Message) -> None:
        value = await self.folder_settings.snapshot()
        await message.reply_text(
            "Save files to TGDrive\n\n"
            "Send or forward files, photos, videos or audio here. "
            "They will appear in your drive once saved.\n\n"
            f"Saving to: {value['folder_label']}\n\n"
            "Use /set_folder to change the upload folder on the TGDrive website.\n"
            "Tip: send photos as files to keep their original quality.",
            reply_markup=self._menu(),
        )

    async def _set_folder(self, _client: Client, message: Message) -> None:
        value = await self.folder_settings.snapshot()
        text = (
            f"Bot upload folder: {value['folder_label']}\n\n"
            "Choose the upload folder on the TGDrive website under "
            "Configuration → Transfer limits → Bot uploads. "
            "Your next forwarded file will be saved there."
        )
        if value["settings_url"]:
            text += "\n\n" + value["settings_url"]
        await message.reply_text(
            text,
            reply_markup=(
                [[Button.url("Set upload folder", value["settings_url"])]]
                if value["settings_url"]
                else self._menu()
            ),
        )

    async def _cancel(self, _client: Client, message: Message) -> None:
        await message.reply_text(
            "There is no folder selection to cancel. "
            "Use /set_folder to manage the upload folder on the website.",
            reply_markup=self._menu(),
        )

    async def _folder_query(self, _client: Client, message: Message) -> None:
        await message.reply_text(
            "Send or forward a file to save it. "
            "Use /set_folder to change its destination on the website.",
            reply_markup=self._menu(),
        )

    async def _current_folder(self, _client: Client, message: Message) -> None:
        value = await self.folder_settings.snapshot()
        await message.reply_text(
            f"Saving new files to: {value['folder_label']}\n\n"
            "Use /set_folder to change it on the website.",
            reply_markup=self._menu(),
        )

    async def _feedback(
        self, message: Message, status: Message | None, text: str
    ) -> None:
        if status is not None:
            try:
                await status.edit_text(text)
                return
            except Exception:
                logger.warning("Could not update bot upload feedback", exc_info=True)
        await message.reply_text(text, reply_markup=self._menu())

    async def _file(self, _client: Client, message: Message) -> None:
        self.active_imports += 1
        try:
            await self._import_file(_client, message)
        finally:
            self.active_imports -= 1

    async def _import_file(self, _client: Client, message: Message) -> None:
        source_chat = getattr(
            getattr(message, "native", None), "chat_id", message.from_user.id
        )
        bot_id = self.settings.main_bot_token_value.split(":", 1)[0]
        operation_id = hashlib.sha256(
            f"bot:{bot_id}:{source_chat}:{message.id}".encode()
        ).hexdigest()
        legacy_operation_id = hashlib.sha256(
            f"bot:{source_chat}:{message.id}".encode()
        ).hexdigest()
        copied: Message | None = None
        status: Message | None = None
        metadata_started = False
        try:
            if self.before_write is not None:
                check = self.before_write()
                if inspect.isawaitable(check):
                    await check
            path, folder_name = await self.folder_settings.destination()
            status = await message.reply_text(
                f"Saving your file to: {folder_name}…",
                reply_markup=self._menu(),
            )
            copied = await message.copy(
                self.settings.storage_channel,
                disable_notification=True,
            )
            media = self._media(copied)
            if media is None:
                raise RuntimeError("copied message contains no media")
            filename = self._media_name(media, copied)
            if await asyncio.to_thread(
                self.store.has_file_operation,
                legacy_operation_id,
                message_id=copied.id,
            ):
                operation_id = legacy_operation_id
            if self.before_write is not None:
                check = self.before_write()
                if inspect.isawaitable(check):
                    await check
            metadata_started = True
            item = await asyncio.to_thread(
                self.store.add_file,
                path,
                filename,
                copied.id,
                getattr(media, "file_size", 0) or 0,
                operation_id=operation_id,
                keep_both=True,
            )
        except Exception as error:
            rolled_back = copied is None
            # A failed acknowledgement can follow a successful catalog commit.
            if copied is not None and not metadata_started:
                try:
                    if self.before_delete is not None:
                        await self.before_delete()
                    if not (
                        await asyncio.to_thread(
                            self.store.has_file_operation, operation_id
                        )
                        or await asyncio.to_thread(
                            self.store.has_file_operation, legacy_operation_id
                        )
                    ):
                        await self.client.delete_messages(
                            self.settings.storage_channel, copied.id
                        )
                        rolled_back = True
                except Exception:
                    logger.exception("Failed to roll back copied bot-mode media")
            logger.exception("Bot-mode upload failed")
            if not rolled_back:
                text = (
                    "Upload could not be confirmed. Check your drive in TGDrive "
                    "before sending this file again."
                )
            elif isinstance(error, SettingsProblem):
                text = (
                    f"Upload paused. {error}\n"
                    "Open TGDrive to resolve this, then resend."
                )
            elif isinstance(error, BackupError):
                text = (
                    "Upload paused while drive protection needs attention. "
                    "Open TGDrive → Storage & Backups, then send this file again."
                )
            elif isinstance(error, FloodWaitError):
                text = (
                    f"Telegram needs a pause. Wait {max(1, error.seconds)} seconds, "
                    "then send this file again."
                )
            elif isinstance(error, ChatForwardsRestrictedError):
                text = (
                    "This chat protects its files from copying. "
                    "If you have the original file, send it here as a new attachment."
                )
            else:
                text = (
                    "Upload failed. Try sending this file again. "
                    "If it keeps failing, check Telegram connections in TGDrive."
                )
            await self._feedback(message, status, text)
            return
        await self._feedback(
            message,
            status,
            f"Uploaded {item['name']}\nSaved to: {folder_name}\n\n"
            "Send another file to save it here, or use /set_folder.",
        )

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
