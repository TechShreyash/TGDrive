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
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError, TimedOutError

from tgdrive.core.config import Settings
from tgdrive.core.security import safe_name
from tgdrive.recovery.backup import BackupError
from tgdrive.settings.bot_settings import SETTINGS_LOCATION, BotSettings
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.telegram.bot_intake import BotIntake
from tgdrive.telegram.clients import Client
from tgdrive.telegram.shared_intake import (
    CLEANUP_PENDING,
    COPY_UNCERTAIN,
    SharedIntakeError,
    cleanup_shared,
    download_shared,
    intake_plan,
)
from tgdrive.transfers.manager import TransferCancelled, TransferError
from tgdrive.transfers.staging import StorageUnavailable, check_staging
from tgdrive.transfers.tasks import TransferStatus

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
_TELEGRAM_TIMEOUTS = (TimeoutError, TimedOutError)
_TELEGRAM_TIMEOUT_MESSAGE = "Telegram timed out. Try again."
_TELEGRAM_TIMEOUT_FEEDBACK = f"<b>Upload failed</b>\n{_TELEGRAM_TIMEOUT_MESSAGE}"
_RETRY_IMPORT_HINT = "\nOpen Transfers in TGDrive and choose Retry on this import."


class _ImportFailed(Exception):
    pass


class _TelegramTimeout(_ImportFailed):
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
    elif record.phase == "waiting_for_staging":
        return (
            "<b>Waiting for temporary storage</b>\n"
            "The import starts when the server has room.",
            -4,
        )
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
        self._active_handlers = 0
        self.intake = (
            BotIntake(transfers, self._process_queued, self._cleanup_intake)
            if transfers
            else None
        )
        if transfers is not None:
            transfers._bot_intake = self.intake
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
        if self.intake is not None:
            self.intake.start()
        logger.info("Telegram bot mode started")

    async def stop(self) -> None:
        if self.intake is not None:
            await self.intake.stop()
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
                "/cancel · reply to the original file to cancel an import"
                "\n\nCancel imports in Transfers on the website.\n"
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
            "<b>Cancel a bot import</b>\n"
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
                        "Check Transfers in TGDrive for cleanup or copy review. "
                        "Saved files are kept."
                    )
            except KeyError:
                text = (
                    "<b>No matching bot import</b>\nCheck Transfers in TGDrive. "
                    "A dispatched copy may need review before retrying."
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

    @property
    def active_imports(self):
        pending = len(self.intake.pending()) if self.intake is not None else 0
        return max(self._active_handlers, pending)

    async def _file(self, _client: Client, message: Message) -> None:
        try:
            await self._queue_file(_client, message)
        except Exception as error:
            logger.exception("Bot intake could not be queued")
            detail = (
                str(error)
                if isinstance(
                    error,
                    (_ImportFailed, SettingsProblem, TransferError, StorageUnavailable),
                )
                else "Open TGDrive and check the destination and connections."
            )
            await self._feedback(
                message, None, f"<b>Import not queued</b>\n{escape(detail)}"
            )

    async def _queue_file(self, _client: Client, message: Message) -> None:
        if self.intake is None:
            await self._feedback(
                message, None, "<b>Upload paused</b>\nOpen TGDrive before resending."
            )
            return
        if message.from_user.id not in self.settings.telegram_admin_ids:
            await self._unauthorized(_client, message)
            return
        if self.before_write is not None:
            result = self.before_write()
            if inspect.isawaitable(result):
                await result
        media = self._media(message)
        if media is None:
            raise _ImportFailed("The message contains no file.")
        native = getattr(message, "native", message)
        payload = getattr(native, "document", None) or getattr(native, "photo", None)
        document_id = getattr(payload, "id", None)
        chat_id = getattr(native, "chat_id", None)
        if (
            type(chat_id) is not int
            or chat_id != message.from_user.id
            or type(message.id) is not int
            or message.id <= 0
            or type(document_id) is not int
            or document_id <= 0
        ):
            raise _ImportFailed("Telegram did not confirm a private file identity.")
        encrypted = await self.folder_settings.encrypt()
        size = getattr(media, "file_size", 0) or 0
        if (
            type(size) is not int
            or size < 0
            or (encrypted and (size == 0 or size > self.settings.upload_limit))
        ):
            raise _ImportFailed(
                "The file size is unavailable or exceeds the upload limit."
            )
        path, label = await self.folder_settings.destination()
        binding = await asyncio.to_thread(self.store.upload_destination, path)
        operation = self._operation_id(message)
        target = (
            self.settings.resolved_cache_dir / "bot-imports" / (operation + ".part")
        )
        context: dict[str, Any] = dict(
            kind="receiving",
            source_kind="telegram",
            destination=binding["path"],
            destination_id=binding["id"],
            name=self._media_name(media, message),
            local_path=str(target),
            staging_size=0,
            staging_written=0,
            bot_encrypt=encrypted,
            bot_operation_id=operation,
            bot_folder_label=label,
            bot_source=dict(
                chat_id=chat_id,
                message_id=message.id,
                document_id=document_id,
                sender_id=message.from_user.id,
                account=self.client.name,
            ),
        )
        if encrypted and self.settings.bot_intake_channel is not None:
            context["bot_intake"] = intake_plan(
                operation, self.settings.bot_intake_channel, self.client.name
            )
        record = await self.intake.submit(operation[:32], size, context)
        folder = escape(record.context["bot_folder_label"])
        encryption = "On" if record.context["bot_encrypt"] else "Off"
        await self._feedback(
            message,
            None,
            (
                f"<b>Queued</b>\nFolder · <code>{folder}</code>\n"
                f"Encryption · {encryption}\n"
                "One file at a time. Follow progress in Transfers."
            )
            if not record.terminal
            else "<b>Import finished</b>\nCheck Transfers in TGDrive.",
        )

    async def _background_pause(self, record):
        if record.cancel_event.is_set():
            raise TransferCancelled("Transfer cancelled")
        try:
            await asyncio.wait_for(record.cancel_event.wait(), 0.25)
        except TimeoutError:
            return
        raise TransferCancelled("Transfer cancelled")

    async def _yield_to_playback(self, record):
        pool = getattr(self.transfers, "pool", None)
        snapshot = getattr(pool, "performance_snapshot", None)
        if not callable(snapshot):
            return
        while True:
            sample = snapshot()
            active = (
                sample.get("active_requests", []) if isinstance(sample, dict) else []
            )
            if not isinstance(active, list) or not any(
                isinstance(row, dict)
                and row.get("operation") == "download"
                and row.get("priority") == "demand"
                for row in active
            ):
                return
            await self._background_pause(record)

    async def _defer_bot(self, record, seconds):
        record.context["bot_retry_at"] = time.time() + max(1, seconds) + 0.25
        await self.transfers.registry.update(
            record.id,
            status=TransferStatus.QUEUED,
            phase="waiting_for_telegram",
            error="Telegram requested a pause.",
        )

    async def _cleanup_intake(self, record):
        return await cleanup_shared(self, record)

    async def _fail_timed_out_import(self, record):
        await self.transfers.registry.update(
            record.id,
            status=TransferStatus.ERROR,
            phase="bot_intake_timeout",
            error=_TELEGRAM_TIMEOUT_MESSAGE,
        )

    async def _process_queued(self, record):
        try:
            await self._run_queued(record)
            relay = record.context.get("bot_intake")
            if relay and relay.get("message_id") and not relay.get("cleaned"):
                await self.transfers.registry.update(
                    record.id, error=CLEANUP_PENDING, phase="bot_intake_cleanup"
                )
        except (_TelegramTimeout, *_TELEGRAM_TIMEOUTS):
            await self._fail_timed_out_import(record)
        except TransferCancelled:
            if record.context.get("send_started"):
                await self.transfers.registry.update(
                    record.id,
                    status=TransferStatus.ERROR,
                    error="The Telegram copy outcome requires review before retrying.",
                )
            else:
                from pathlib import Path

                await asyncio.to_thread(
                    Path(record.context["local_path"]).unlink, missing_ok=True
                )
                record.context.update(staging_size=0, staging_written=0)
                await self.transfers.registry.update(
                    record.id,
                    status=TransferStatus.CANCELLED,
                    phase="cancelled",
                    error="Transfer cancelled",
                )

    async def _run_queued(self, record):
        source = record.context["bot_source"]
        if not isinstance(source, dict) or any(
            type(source.get(key)) is not int or not 0 < source[key] < 2**63
            for key in ("chat_id", "message_id", "document_id", "sender_id")
        ):
            raise _ImportFailed("The queued Telegram source identity is invalid.")
        if source["chat_id"] != source["sender_id"]:
            raise _ImportFailed("The queued source is not a private sender chat.")
        token = self.settings.main_bot_token_value
        if token is None:
            raise _ImportFailed(
                "Restore the original bot configuration before retrying."
            )
        bot_id = token.split(":", 1)[0]
        operation = hashlib.sha256(
            f"bot:{bot_id}:{source['chat_id']}:{source['message_id']}".encode()
        ).hexdigest()
        if (
            record.id != operation[:32]
            or record.context.get("bot_operation_id") != operation
            or type(record.context.get("bot_encrypt")) is not bool
        ):
            raise _ImportFailed("The queued Telegram request identity changed.")
        expected = (
            self.settings.resolved_cache_dir / "bot-imports" / (operation + ".part")
        )
        from pathlib import Path

        actual = Path(record.context["local_path"])
        valid_path = actual == expected and await asyncio.to_thread(
            lambda: (
                not actual.is_symlink()
                and expected.parent.resolve().is_relative_to(
                    self.settings.resolved_cache_dir.resolve()
                )
            )
        )
        if not valid_path:
            raise _ImportFailed(
                "The queued temporary-file path is invalid. Preserve it for review."
            )
        if not record.context["bot_encrypt"] and await asyncio.to_thread(
            self.store.has_file_operation, record.context["bot_operation_id"]
        ):
            record.context["metadata_committed"] = True
            await self.transfers.registry.update(
                record.id,
                status=TransferStatus.COMPLETED,
                phase="completed",
                current=record.total,
            )
            return
        if record.context.get("kind") == "upload":
            await self.transfers._upload_path(
                record,
                actual,
                record.context["destination"],
                record.context["name"],
                record.context["size"],
            )
            return
        while getattr(self.client, "is_connected", None) is False:
            issue = getattr(self.client, "connection_error", None)
            if getattr(issue, "code", None) in {
                "TELEGRAM_CREDENTIALS_REVOKED",
                "TELEGRAM_CONNECTION_UNAVAILABLE",
            }:
                raise _ImportFailed("Restore the original bot account before retrying.")
            if record.phase != "waiting_for_telegram":
                await self.transfers.registry.update(
                    record.id, phase="waiting_for_telegram"
                )
            await self._background_pause(record)
        if source["account"] != self.client.name:
            raise _ImportFailed(
                "The original bot account is required to read this file."
            )
        if source["sender_id"] not in self.settings.telegram_admin_ids:
            raise _ImportFailed(
                "The original sender is no longer allowed to import files."
            )
        try:
            async with asyncio.timeout(45):
                message = await self.client.get_messages(
                    source["chat_id"], message_ids=source["message_id"]
                )
        except FloodWaitError as error:
            await self._defer_bot(record, error.seconds)
            return
        except _TELEGRAM_TIMEOUTS:
            await self._fail_timed_out_import(record)
            try:
                async with asyncio.timeout(5):
                    await self.client.reply_to(
                        source["chat_id"],
                        source["message_id"],
                        _TELEGRAM_TIMEOUT_FEEDBACK + _RETRY_IMPORT_HINT,
                        parse_mode="html",
                    )
            except Exception:
                logger.warning("Could not send bot import timeout feedback")
            return
        native = getattr(message, "native", message)
        media = getattr(native, "document", None) or getattr(native, "photo", None)
        if (
            message is None
            or getattr(message, "empty", False)
            or getattr(media, "id", None) != source["document_id"]
            or getattr(message, "id", None) != source["message_id"]
        ):
            raise _ImportFailed(
                "The original file is missing or changed. Send it again."
            )
        try:
            await self._execute_file(self.client, message, record)
        except BaseException as error:
            relay = record.context.get("bot_intake")
            if (
                relay
                and relay.get("dispatched")
                and not relay.get("cleaned")
                and not self.transfers.registry._shutting_down
            ):
                await self.transfers.registry.update(
                    record.id,
                    status=TransferStatus.CANCELLED
                    if relay.get("message_id") and record.cancel_event.is_set()
                    else TransferStatus.ERROR,
                    phase="bot_intake_timeout"
                    if relay.get("message_id") and isinstance(error, _TelegramTimeout)
                    else "bot_intake_cleanup",
                    error=(
                        _TELEGRAM_TIMEOUT_MESSAGE
                        if isinstance(error, _TelegramTimeout)
                        else CLEANUP_PENDING
                    )
                    if relay.get("message_id")
                    else COPY_UNCERTAIN,
                )
                return
            if record.status in {TransferStatus.COMPLETED, TransferStatus.CANCELLED}:
                logger.warning("Finished bot import feedback could not be delivered")
                return
            if (
                not record.context["bot_encrypt"]
                and record.context.get("send_started")
                and not self.transfers.registry._shutting_down
            ):
                committed = await asyncio.to_thread(
                    self.store.has_file_operation, record.context["bot_operation_id"]
                )
                if committed:
                    record.context["metadata_committed"] = True
                    await self.transfers.registry.update(
                        record.id,
                        status=TransferStatus.COMPLETED,
                        phase="completed",
                        current=record.total,
                    )
                else:
                    removed = record.context.get("bot_copy_deleted")
                    await self.transfers.registry.update(
                        record.id,
                        status=TransferStatus.CANCELLED
                        if removed and record.cancel_event.is_set()
                        else TransferStatus.ERROR,
                        error="The Telegram copy outcome requires review. "
                        "Check the drive before resending; the source is preserved.",
                    )
                return
            raise

    async def _execute_file(self, _client, message, record=None):
        self._active_handlers += 1
        try:
            await self._import_file(_client, message, record)
        finally:
            self._active_handlers -= 1

    async def _import_file(
        self, _client: Client, message: Message, record=None
    ) -> None:
        if record is not None:
            assert self.transfers is not None
        operation_id = self._operation_id(message)
        copied: Message | None = None
        status: Message | None = None
        metadata_started = False
        try:
            if self.before_write is not None:
                check = self.before_write()
                if inspect.isawaitable(check):
                    await check
            encrypted = (
                record.context["bot_encrypt"]
                if record
                else await self.folder_settings.encrypt()
            )
            if encrypted and self.transfers is None:
                raise _ImportFailed(
                    "Encrypted bot uploads are unavailable while the transfer "
                    "service is not ready. Open TGDrive before resending."
                )
            if record is None:
                path, folder_name = await self.folder_settings.destination()
            else:
                path = await self.transfers._resolve_upload_destination(
                    record, record.context["destination"]
                )
                folder_name = record.context["bot_folder_label"]
            if path is None:
                raise RuntimeError("The upload destination is unavailable")
            # No reply keyboard here: inline buttons are edited in when saved.
            status = await message.reply_text(
                f"<b>Saving…</b>\nFolder · <code>{escape(folder_name)}</code>",
                parse_mode="html",
            )
            if encrypted:
                item = await self._encrypted_import(
                    message, path, operation_id, status, record
                )
                await self._saved(message, status, item, path, folder_name)
                return
            if record and record.context.get("bot_copy_deleted"):
                raise _ImportFailed(
                    "The earlier copy was removed. Send the source again."
                )
            copy_id = record.context.get("bot_copy_message_id") if record else None
            if copy_id:
                copied = await self.client.get_messages(
                    self.settings.storage_channel, message_ids=copy_id
                )
            else:
                if record and record.context.get("send_started"):
                    raise _ImportFailed(
                        "The earlier copy was not confirmed. "
                        "Check TGDrive before resending."
                    )
                if record:
                    if record.cancel_event.is_set():
                        raise TransferCancelled("Transfer cancelled")
                    record.context["send_started"] = True
                    await self.transfers.registry.persist(record)
                copied = await message.copy(
                    self.settings.storage_channel,
                    disable_notification=True,
                )
                if record and copied is not None:
                    record.context["bot_copy_message_id"] = copied.id
                    await self.transfers.registry.persist(record)
            if copied is None:
                raise RuntimeError("Telegram did not return the copied message")
            media = self._media(copied)
            if media is None:
                raise RuntimeError("copied message contains no media")
            filename = self._media_name(media, copied)
            if record and record.cancel_event.is_set():
                raise TransferCancelled("Transfer cancelled")
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
            if record is not None and isinstance(error, FloodWaitError):
                if copied is None:
                    # A FloodWait is a provider rejection, not an ambiguous send.
                    record.context.pop("send_started", None)
                assert self.transfers is not None
                await self._defer_bot(record, error.seconds)
                await self._feedback(
                    message,
                    status,
                    "<b>Waiting for Telegram</b>\nRetrying after its pause.",
                )
                return
            rolled_back = copied is None and not (
                record and record.context.get("send_started")
            )
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
                        if record:
                            record.context["bot_copy_deleted"] = True
                            assert self.transfers is not None
                            await self.transfers.registry.persist(record)
                except Exception:
                    logger.exception("Failed to roll back copied bot-mode media")
            if record is not None and isinstance(error, TransferCancelled):
                raise
            logger.exception("Bot-mode upload failed")
            if rolled_back and isinstance(error, _TELEGRAM_TIMEOUTS):
                text = _TELEGRAM_TIMEOUT_FEEDBACK
                if record is not None:
                    relay = record.context.get("bot_intake")
                    if relay and relay.get("message_id") and not relay.get("cleaned"):
                        text += (
                            "\nOpen Transfers in TGDrive. "
                            "Retry after temporary cleanup finishes."
                        )
                    else:
                        await self._fail_timed_out_import(record)
                        text += _RETRY_IMPORT_HINT
                try:
                    async with asyncio.timeout(5):
                        await self._feedback(message, status, text)
                except Exception:
                    logger.warning("Could not send bot import timeout feedback")
                if record is not None:
                    raise _TelegramTimeout(_TELEGRAM_TIMEOUT_MESSAGE) from error
                return
            if record is not None and record.bot_intake_copy_uncertain:
                text = (
                    f"<b>Bot import needs review</b>\n{COPY_UNCERTAIN}\n"
                    "Open Transfers in TGDrive and choose "
                    "Confirm intake channel reviewed."
                )
            elif not rolled_back:
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
            elif isinstance(
                error,
                (_ImportFailed, SharedIntakeError, TransferError, StorageUnavailable),
            ):
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
            if record is not None:
                raise _ImportFailed(
                    str(error)
                    if isinstance(error, SharedIntakeError)
                    else "Bot import stopped. Review its Telegram feedback."
                ) from None
            return
        if record:
            record.context["metadata_committed"] = True
            await self.transfers.registry.update(
                record.id,
                status=TransferStatus.COMPLETED,
                phase="completed",
                current=item.get("size", record.total),
                total=item.get("size", record.total),
            )
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

    async def _encrypted_import(
        self, message, path, operation_id, status, record: Any = None
    ):
        """Download the forwarded file, then upload it encrypted like a web upload."""
        media = self._media(message)
        if media is None:
            raise RuntimeError("message contains no media")
        name = self._media_name(media, message)
        directory = self.settings.resolved_cache_dir / "bot-imports"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The suffix stops Telethon from appending the media's own extension.
        staged = directory / f"{operation_id}.part"
        report = self._reporter(status)

        async def download(target, progress) -> None:
            if record is None:
                await self.client.download_media(
                    message, file_name=str(target), progress=progress
                )
                return
            active_record = record
            await self._yield_to_playback(active_record)

            async def received(current, total):
                if current > active_record.total:
                    raise _ImportFailed("Telegram returned more bytes than advertised.")
                await progress(current, total)
                await report(active_record)
                await self._yield_to_playback(active_record)

            if active_record.context.get("bot_intake"):
                await download_shared(self, active_record, message, target, received)
            else:
                await self.client.download_media(
                    message, file_name=str(target), progress=received
                )

        if record is not None:
            await asyncio.to_thread(staged.unlink, missing_ok=True)
            while True:
                if record.cancel_event.is_set():
                    raise TransferCancelled("Transfer cancelled")
                try:
                    check_staging(
                        self.settings, self.transfers, record.total, exclude=record.id
                    )
                except StorageUnavailable:
                    if record.phase != "waiting_for_staging":
                        await self.transfers.registry.update(
                            record.id, phase="waiting_for_staging"
                        )
                        await report(record)
                    try:
                        await asyncio.wait_for(record.cancel_event.wait(), 1)
                    except TimeoutError:
                        continue
                    raise TransferCancelled("Transfer cancelled") from None
                break
            record.context.update(staging_size=record.total, staging_written=0)
            await self.transfers.registry.persist(record)

            async def report_progress():
                while True:
                    await asyncio.sleep(1)
                    await report(record)

            reporter = asyncio.create_task(report_progress(), name="bot-progress")
            try:
                await self.transfers._receive_telegram_file(record, download)
            finally:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
            if record.status is not TransferStatus.COMPLETED:
                raise _ImportFailed(
                    record.error or "The encrypted upload did not finish."
                )
            return {
                "name": record.context.get("name", name),
                "size": record.context.get("size", record.total),
            }

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
