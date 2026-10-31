"""Forward-to-save Telegram interface with a website-managed upload folder."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from telethon import Button
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError

from tgdrive.core.config import Settings
from tgdrive.core.security import safe_name
from tgdrive.recovery.backup import BackupError
from tgdrive.settings.bot_settings import SETTINGS_LOCATION, BotSettings
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.telegram.clients import Client
from tgdrive.transfers.manager import TransferError
from tgdrive.transfers.staging import StorageUnavailable

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
# Telegram rate-limits edits; one status edit per few seconds stays well clear.
_EDIT_SECONDS = 4
_EDIT_PERCENT = 5


class _ImportFailed(Exception):
    pass


def _size(value: float) -> str:
    """Match the website's 1024-based byte labels."""
    exponent = 0
    while value >= 1024 and exponent < 4:
        value /= 1024
        exponent += 1
    unit = ("B", "KB", "MB", "GB", "TB")[exponent]
    return f"{value:.0f} {unit}" if exponent == 0 else f"{value:.1f} {unit}"


def _progress(record: Any) -> tuple[str, int] | None:
    """Status text and percent for a bot import transfer, or None when idle."""
    name = record.context.get("name", "file")
    if record.phase == "downloading":
        label = "Receiving"
    elif record.phase in {"uploading", "finalizing", "verifying"}:
        label = "Saving to TGDrive"
    elif record.phase in {"retry_wait", "waiting_for_telegram"}:
        return f"Waiting for Telegram, retrying automatically…\n{name}", -1
    elif record.phase == "queued":
        return f"Waiting for a free upload slot…\n{name}", -2
    else:
        return None
    total = record.total
    percent = min(100, record.current * 100 // total) if total else 0
    return (
        f"{label} {percent}% · {_size(record.current)} of {_size(total)}\n{name}",
        percent,
    )


class BotMode:
    def __init__(
        self,
        *,
        client: Client,
        settings: Settings,
        store: MongoStore,
        before_write: Callable[[], Awaitable[None] | None] | None = None,
        before_delete: Callable[[], Awaitable[None]] | None = None,
        folder_settings: BotSettings | None = None,
        transfers: Any = None,
    ) -> None:
        self.client = client
        self.transfers = transfers
        self.settings = settings
        self.store = store
        self.before_write = before_write
        self.before_delete = before_delete
        self.active_imports = 0
        self.clock = time.monotonic
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
            "Send or forward files here to save them to TGDrive.\n\n"
            f"Folder: {value['folder_label']}\n\n"
            "/current_folder · show the folder\n"
            "/set_folder · change it on the website\n\n"
            "Tip: send photos as files to keep full quality.",
            reply_markup=self._menu(),
        )

    async def _set_folder(self, _client: Client, message: Message) -> None:
        value = await self.folder_settings.snapshot()
        text = (
            f"Folder: {value['folder_label']}\n\n"
            "Change it on the TGDrive website: Configuration → Transfer limits "
            "→ Bot uploads. New files use the new folder right away."
        )
        if value["settings_url"]:
            text += "\n\n" + value["settings_url"]
        await message.reply_text(
            text,
            reply_markup=(
                [[Button.url("Change folder", value["settings_url"])]]
                if value["settings_url"]
                else self._menu()
            ),
        )

    async def _cancel(self, _client: Client, message: Message) -> None:
        await message.reply_text(
            "Nothing to cancel here. To stop a running import, cancel it in "
            "Transfers on the TGDrive website.",
            reply_markup=self._menu(),
        )

    async def _folder_query(self, _client: Client, message: Message) -> None:
        await message.reply_text(
            "Send a file, photo, video or audio to save it. "
            "Text messages are not saved. /help shows the commands.",
            reply_markup=self._menu(),
        )

    async def _current_folder(self, _client: Client, message: Message) -> None:
        value = await self.folder_settings.snapshot()
        await message.reply_text(
            f"Folder: {value['folder_label']}\n\nUse /set_folder to change it.",
            reply_markup=self._menu(),
        )

    async def _unauthorized(self, _client: Client, message: Message) -> None:
        # Answer each stranger once so the bot cannot be used to send spam.
        # shortcut: per-process memory, cleared on restart; persist if abused.
        seen = self.__dict__.setdefault("_answered_strangers", set())
        if message.from_user.id in seen:
            return
        seen.add(message.from_user.id)
        await message.reply_text(
            "This is a private TGDrive bot.\n\n"
            f"Your Telegram user ID is {message.from_user.id}. The owner can "
            "allow you by adding it to TELEGRAM_ADMIN_IDS."
        )

    async def _feedback(
        self, message: Message, status: Message | None, text: str, buttons=None
    ) -> None:
        if status is not None:
            try:
                await status.edit_text(text, reply_markup=buttons)
                return
            except Exception:
                logger.warning("Could not update bot upload feedback", exc_info=True)
        await message.reply_text(text, reply_markup=buttons or self._menu())

    def _reporter(self, status: Message):
        """Edit one status message, throttled by time, percent and FloodWait."""
        shown: tuple[str, int] | None = None
        next_at = 0.0

        async def report(record: Any) -> None:
            nonlocal shown, next_at
            current = _progress(record)
            if current is None or self.clock() < next_at:
                return
            if shown is not None and (
                current[0].split(" ", 1)[0] == shown[0].split(" ", 1)[0]
                and 0 <= current[1] - shown[1] < _EDIT_PERCENT
            ):
                return
            try:
                await status.edit_text(current[0])
            except FloodWaitError as error:
                next_at = self.clock() + max(1, error.seconds)
                return
            except Exception:
                logger.debug("Could not update bot progress", exc_info=True)
            shown = current
            next_at = self.clock() + _EDIT_SECONDS

        return report

    async def _saved(self, message, status, item, path, folder_name) -> None:
        settings_url = (await self.folder_settings.snapshot())["settings_url"]
        buttons = None
        if settings_url:
            site = settings_url.removesuffix(SETTINGS_LOCATION)
            folder = path.rstrip("/").rsplit("/", 1)[-1] if path else ""
            link = (
                site + "/" + ("?" + urlencode({"path": "/" + folder}) if folder else "")
            )
            buttons = [[Button.url("Open in TGDrive", link)]]
        size = item.get("size")
        await self._feedback(
            message,
            status,
            f"Saved ✓ {item['name']}\n"
            + (f"{_size(size)} · " if size else "")
            + f"in {folder_name}",
            buttons,
        )

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
        copied: Message | None = None
        status: Message | None = None
        metadata_started = False
        try:
            if self.before_write is not None:
                check = self.before_write()
                if inspect.isawaitable(check):
                    await check
            path, folder_name = await self.folder_settings.destination()
            # No reply keyboard here: inline buttons are edited in when saved.
            status = await message.reply_text(f"Saving to {folder_name}…")
            if self.transfers is not None and await self.folder_settings.encrypt():
                item = await self._encrypted_import(message, path, operation_id, status)
                await self._saved(message, status, item, path, folder_name)
                return
            copied = await message.copy(
                self.settings.storage_channel,
                disable_notification=True,
            )
            media = self._media(copied)
            if media is None:
                raise RuntimeError("copied message contains no media")
            filename = self._media_name(media, copied)
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
                    if not await asyncio.to_thread(
                        self.store.has_file_operation, operation_id
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
            elif isinstance(error, (_ImportFailed, TransferError, StorageUnavailable)):
                text = (
                    f"Couldn't save this file: {error}\n"
                    "Check Transfers in TGDrive, or send the file again."
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
        await self._saved(message, status, item, path, folder_name)

    async def _encrypted_import(self, message, path, operation_id, status):
        """Download the forwarded file, then upload it encrypted like a web upload."""
        media = self._media(message)
        if media is None:
            raise RuntimeError("message contains no media")
        name = self._media_name(media, message)
        directory = self.settings.resolved_cache_dir / "bot-imports"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The suffix stops Telethon from appending the media's own extension.
        staged = directory / f"{operation_id}.part"

        async def download(target, progress):
            await self.client.download_media(
                message, file_name=str(target), progress=progress
            )

        # The job owns the staged bytes: it removes them after a failed download
        # and keeps them after a failed upload so the website can retry it.
        record = await self.transfers.receive_telegram_file(
            download,
            staged,
            path,
            name,
            getattr(media, "file_size", 0) or 0,
            transfer_id=operation_id[:32],
        )
        task = record.task
        report = self._reporter(status)
        # asyncio.wait never cancels the transfer when this handler is cancelled.
        while task is not None and not task.done():
            await report(record)
            await asyncio.wait({task}, timeout=1)
        if record.status.value == "cancelled":
            raise _ImportFailed("it was cancelled in TGDrive.")
        if record.status.value != "completed":
            raise _ImportFailed(record.error or "the upload did not finish.")
        return {
            "name": record.context.get("name", name),
            "size": record.context.get("size", record.total),
        }

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
