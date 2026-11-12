"""Forward-to-save Telegram interface with a website-managed upload folder."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html import escape
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
    if record.phase == "downloading":
        label = "Receiving"
    elif record.phase == "uploading":
        label = "Saving"
    elif record.phase in {"finalizing", "verifying"}:
        label = record.phase.capitalize()
    elif record.phase in {"retry_wait", "waiting_for_telegram"}:
        return "<b>Waiting for Telegram</b>\nRetrying automatically…", -1
    elif record.phase == "queued":
        return "<b>Queued</b>\nWaiting for an upload slot…", -2
    else:
        return None
    total = record.total
    if not total:
        return f"<b>{label}</b> · {_size(record.current)}", -3
    percent = min(100, record.current * 100 // total)
    return (
        f"<b>{label}</b> · {percent}%\n{_size(record.current)} / {_size(total)}",
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
        self._answered_strangers: deque[int] = deque(maxlen=1024)

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
        if self._started or getattr(self.client, "is_connected", False):
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
        encryption = (
            "On · encrypted through this server"
            if value["encrypt"]
            else "Off · readable by channel members"
        )
        text = (
            "<b>TGDrive</b>\nSend or forward a file to save it.\n\n"
            f"<b>Folder</b> · <code>{escape(value['folder_label'])}</code>\n"
            f"<b>Encryption</b> · {encryption}"
        )
        words = message.text.split()
        command = words[0].split("@")[0].lower() if words else ""
        if command == "/start":
            text += "\n\nUse Help for commands."
        else:
            text += (
                "\n\n<b>Commands</b>\n"
                "/current_folder · show the destination\n"
                "/set_folder · change it on the website\n"
                "/cancel · reply to the original file to cancel an encrypted import"
                "\n\nYou can also cancel in Transfers on the website.\n"
                "Send photos as files to keep full quality."
            )
        await message.reply_text(text, reply_markup=self._menu(), parse_mode="html")

    async def _set_folder(self, _client: Client, message: Message) -> None:
        value = await self.folder_settings.snapshot()
        text = (
            f"<b>Upload folder</b>\n<code>{escape(value['folder_label'])}</code>\n\n"
            "Change it in TGDrive: Configuration → Transfer limits → Bot uploads.\n"
            "New files use the updated folder."
        )
        await message.reply_text(
            text,
            reply_markup=(
                [[Button.url("Change folder", value["settings_url"])]]
                if value["settings_url"]
                else self._menu()
            ),
            parse_mode="html",
        )

    async def _cancel(self, _client: Client, message: Message) -> None:
        reply_id = getattr(getattr(message, "native", None), "reply_to_msg_id", None)
        text = (
            "<b>Cancel an encrypted import</b>\n"
            "Reply to the original file with /cancel, or use Transfers in TGDrive."
        )
        if self.transfers is not None and type(reply_id) is int and reply_id > 0:
            identity = self._operation_id(message, reply_id)[:32]
            try:
                record = self.transfers.registry.get(identity)
                if record.context.get("source_kind") != "telegram":
                    raise KeyError(identity)
                if record.status.value in {"completed", "cancelled"}:
                    text = "<b>Import finished</b>\nCheck Transfers in TGDrive."
                else:
                    await self.transfers.cancel_job(identity)
                    text = (
                        "<b>Cancellation requested</b>\n"
                        "Check Transfers in TGDrive for cleanup. Saved files are kept."
                    )
            except KeyError:
                text = (
                    "<b>No matching encrypted import</b>\nCheck Transfers in TGDrive. "
                    "Direct Telegram copies cannot be cancelled here."
                )
            except BackupError, SettingsProblem, TransferError, ValueError:
                text = (
                    "<b>Cancellation unavailable</b>\n"
                    "Check Transfers in TGDrive for its current state."
                )
        await message.reply_text(
            text,
            reply_markup=self._menu(),
            parse_mode="html",
        )

    async def _folder_query(self, _client: Client, message: Message) -> None:
        await message.reply_text(
            "<b>Send a file to save it</b>\n"
            "Files, photos, video and audio are supported.\n"
            "Text is not saved. Use Help for commands.",
            reply_markup=self._menu(),
            parse_mode="html",
        )

    async def _current_folder(self, _client: Client, message: Message) -> None:
        value = await self.folder_settings.snapshot()
        await message.reply_text(
            f"<b>Upload folder</b>\n<code>{escape(value['folder_label'])}</code>"
            "\n\nUse /set_folder to change it.",
            reply_markup=self._menu(),
            parse_mode="html",
        )

    async def _unauthorized(self, _client: Client, message: Message) -> None:
        # Remember only recent strangers so unsolicited messages cannot grow memory.
        seen = self._answered_strangers
        if message.from_user.id in seen:
            return
        seen.append(message.from_user.id)
        await message.reply_text(
            "<b>Private TGDrive bot</b>\n"
            f"Your Telegram ID: <code>{escape(str(message.from_user.id))}</code>\n"
            "Ask the owner to add it to <code>TELEGRAM_ADMIN_IDS</code>.",
            parse_mode="html",
        )

    async def _feedback(
        self, message: Message, status: Message | None, text: str, buttons=None
    ) -> None:
        if status is not None:
            try:
                await status.edit_text(text, reply_markup=buttons, parse_mode="html")
                return
            except Exception:
                logger.warning("Could not update bot upload feedback", exc_info=True)
        await message.reply_text(
            text, reply_markup=buttons or self._menu(), parse_mode="html"
        )

    def _reporter(self, status: Message):
        """Edit one status message, throttled by time, percent and FloodWait."""
        shown: tuple[str, int] | None = None
        next_at = 0.0

        async def report(record: Any) -> None:
            nonlocal shown, next_at
            current = _progress(record)
            if current is None or self.clock() < next_at:
                return
            if current == shown:
                return
            if shown is not None and (
                current[0].split(" ", 1)[0] == shown[0].split(" ", 1)[0]
                and current[1] >= 0
                and shown[1] >= 0
                and 0 <= current[1] - shown[1] < _EDIT_PERCENT
            ):
                return
            try:
                await status.edit_text(current[0], parse_mode="html")
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
            f"<b>Saved ✓</b>\n<code>{escape(item['name'])}</code>\n"
            + (f"{_size(size)} · " if size else "")
            + f"<code>{escape(folder_name)}</code>",
            buttons,
        )

    async def _file(self, _client: Client, message: Message) -> None:
        self.active_imports += 1
        try:
            await self._import_file(_client, message)
        finally:
            self.active_imports -= 1

    async def _import_file(self, _client: Client, message: Message) -> None:
        operation_id = self._operation_id(message)
        copied: Message | None = None
        status: Message | None = None
        metadata_started = False
        try:
            if self.before_write is not None:
                check = self.before_write()
                if inspect.isawaitable(check):
                    await check
            encrypted = await self.folder_settings.encrypt()
            if encrypted and self.transfers is None:
                raise _ImportFailed(
                    "Encrypted bot uploads are unavailable while the transfer "
                    "service is not ready. Open TGDrive before resending."
                )
            path, folder_name = await self.folder_settings.destination()
            if path is None:
                raise RuntimeError("The upload destination is unavailable")
            # No reply keyboard here: inline buttons are edited in when saved.
            status = await message.reply_text(
                f"<b>Saving…</b>\nFolder · <code>{escape(folder_name)}</code>",
                parse_mode="html",
            )
            if encrypted:
                item = await self._encrypted_import(message, path, operation_id, status)
                await self._saved(message, status, item, path, folder_name)
                return
            copied = await message.copy(
                self.settings.storage_channel,
                disable_notification=True,
            )
            if copied is None:
                raise RuntimeError("Telegram did not return the copied message")
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
                    "<b>Not confirmed</b>\nUpload could not be confirmed. "
                    "Check your drive in TGDrive before resending."
                )
            elif isinstance(error, SettingsProblem):
                text = (
                    f"<b>Upload paused</b>\n{escape(str(error))}\n"
                    "Open TGDrive to resolve this before resending."
                )
            elif isinstance(error, BackupError):
                text = (
                    "<b>Upload paused</b>\nDrive protection needs attention.\n"
                    "Open TGDrive → Storage and Backups before resending."
                )
            elif isinstance(error, FloodWaitError):
                text = (
                    "<b>Telegram needs a pause</b>\n"
                    f"Wait {max(1, error.seconds)} seconds before resending."
                )
            elif isinstance(error, (_ImportFailed, TransferError, StorageUnavailable)):
                text = (
                    f"<b>Import stopped</b>\n{escape(str(error))}\n"
                    "Check Transfers in TGDrive before resending."
                )
            elif isinstance(error, ChatForwardsRestrictedError):
                text = (
                    "<b>Copying is restricted</b>\nThis chat protects its files.\n"
                    "Send the original file as a new attachment if you have it."
                )
            else:
                text = (
                    "<b>Upload failed</b>\nTry sending the file again. "
                    "If this repeats, check Connections in TGDrive."
                )
            await self._feedback(message, status, text)
            return
        await self._saved(message, status, item, path, folder_name)

    def _operation_id(self, message: Message, message_id: int | None = None) -> str:
        source_chat = getattr(
            getattr(message, "native", None), "chat_id", message.from_user.id
        )
        token = self.settings.main_bot_token_value
        if token is None:
            raise RuntimeError("Bot mode is not configured")
        bot_id = token.split(":", 1)[0]
        incoming_id = message.id if message_id is None else message_id
        return hashlib.sha256(
            f"bot:{bot_id}:{source_chat}:{incoming_id}".encode()
        ).hexdigest()

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
