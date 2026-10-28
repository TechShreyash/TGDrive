"""Telethon client adapters with bounded uploads, reads, and account requests."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import io
import logging
import math
import mimetypes
import secrets
import time
import weakref
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import (
    FileReferenceExpiredError,
    FileReferenceInvalidError,
    FilerefUpgradeNeededError,
    FloodPremiumWaitError,
    FloodWaitError,
    RPCError,
)
from telethon.sessions import StringSession

from tgdrive.core.config import Settings
from tgdrive.media.media_scheduler import MediaScheduler
from tgdrive.telegram.client_identity import IdentityCache, account_identity
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.connection_errors import TelegramConnectionError
from tgdrive.transfers.file_window import FileWindow

log = logging.getLogger(__name__)

_DOWNLOAD_BLOCK = 1024**2
_DOWNLOAD_REQUEST = 512 * 1024
_DOWNLOAD_PREFETCH = 4

_BLOCK_CACHE_BYTES = 24 * 1024**2
_BLOCK_CACHE_ENTRIES = 128
_BLOCK_CACHE_TTL = 30.0
_BLOCK_CACHE_READS = 8


@dataclass(slots=True)
class _BlockFlight:
    task: asyncio.Task[bytes] | None = None
    waiters: int = 0
    abandoned: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)


class _BlockCache:
    """Bounded payload LRU and shared reads; never an authorization cache."""

    def __init__(
        self,
        *,
        max_bytes: int = _BLOCK_CACHE_BYTES,
        max_entries: int = _BLOCK_CACHE_ENTRIES,
        ttl: float = _BLOCK_CACHE_TTL,
        read_slots: int = _BLOCK_CACHE_READS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(max_bytes, max_entries, ttl, read_slots) <= 0:
            raise ValueError("Block-cache bounds must be positive")
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.ttl = ttl
        self.clock = clock
        self.bytes_used = 0
        self.entries: OrderedDict[tuple[Any, ...], tuple[float, bytes]] = OrderedDict()
        self.flights: dict[tuple[Any, ...], _BlockFlight] = {}
        self._slots = asyncio.Semaphore(read_slots)

    def _hit(self, key: tuple[Any, ...]) -> bytes | None:
        now = self.clock()
        for identity, (expires, data) in list(self.entries.items()):
            if expires <= now:
                del self.entries[identity]
                self.bytes_used -= len(data)
        item = self.entries.get(key)
        if item is None:
            return None
        self.entries.move_to_end(key)
        return item[1]

    def _put(self, key: tuple[Any, ...], data: bytes) -> None:
        if not data or len(data) > self.max_bytes:
            return
        self._hit(key)
        old = self.entries.pop(key, None)
        if old is not None:
            self.bytes_used -= len(old[1])
        while self.entries and (
            self.bytes_used + len(data) > self.max_bytes
            or len(self.entries) >= self.max_entries
        ):
            _, (_, evicted) = self.entries.popitem(last=False)
            self.bytes_used -= len(evicted)
        self.entries[key] = (self.clock() + self.ttl, data)
        self.bytes_used += len(data)

    async def get(
        self,
        key: tuple[Any, ...],
        expected: int,
        load: Callable[[], Awaitable[bytes]],
    ) -> bytes:
        while True:
            hit = self._hit(key)
            if hit is not None:
                return hit
            flight = self.flights.get(key)
            if flight is not None and flight.abandoned:
                # Do not join another request's cancelled read or hedge it.
                await flight.done.wait()
                continue
            if flight is None:
                await self._slots.acquire()
                hit = self._hit(key)
                if hit is not None:
                    self._slots.release()
                    return hit
                flight = self.flights.get(key)
                if flight is not None:
                    self._slots.release()
                    if flight.abandoned:
                        await flight.done.wait()
                        continue
                else:
                    flight = _BlockFlight()
                    self.flights[key] = flight
                    try:
                        task = asyncio.create_task(load())
                    except BaseException:
                        del self.flights[key]
                        self._slots.release()
                        flight.done.set()
                        raise
                    flight.task = task

                    def complete(
                        done: asyncio.Task[bytes],
                        owner: _BlockFlight = flight,
                    ) -> None:
                        try:
                            data = done.result()
                        except BaseException:
                            # The awaiting consumers receive the original error.
                            return
                        else:
                            if not owner.abandoned and len(data) == expected:
                                self._put(key, data)
                        finally:
                            if self.flights.get(key) is owner:
                                del self.flights[key]
                            self._slots.release()
                            owner.done.set()

                    task.add_done_callback(complete)
            assert flight.task is not None
            task = flight.task
            flight.waiters += 1
            try:
                return await asyncio.shield(task)
            finally:
                flight.waiters -= 1
                if flight.waiters == 0 and not task.done():
                    flight.abandoned = True
                    task.cancel()
                    # Keep iterator cleanup owned by the last consumer even if
                    # its surrounding cancellation scope issues repeated cancels.
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break


# Both keys and values are weak: a semaphore can retain its owning loop after
# contention, so a strong cache value here would keep a closed loop alive.
_LOOP_BLOCK_CACHES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[_BlockCache]
] = weakref.WeakKeyDictionary()


def _client_block_cache(client: Any) -> _BlockCache:
    loop = asyncio.get_running_loop()
    reference = _LOOP_BLOCK_CACHES.get(loop)
    cache = reference() if reference is not None else None
    if cache is None:
        cache = _BlockCache()
        _LOOP_BLOCK_CACHES[loop] = weakref.ref(cache)
    # Live transport clients retain their loop's cache. Finished payloads hold
    # no clients, sessions, tokens or authorization decisions.
    client._download_block_cache = cache
    return cache


def _block_identity(message: Any, index: int) -> tuple[tuple[Any, ...], int] | None:
    native = getattr(message, "native", message)
    target = (
        message.download_media
        if isinstance(message, TelegramMediaSelection)
        else native
    )
    document = (
        target
        if isinstance(target, types.Document)
        else getattr(target, "document", None)
    )
    if isinstance(document, types.Document):
        identity = ("document", document.id, document.size)
        size = document.size
    else:
        photo = (
            target
            if isinstance(target, types.Photo)
            else getattr(target, "photo", None)
        )
        if not isinstance(photo, types.Photo) or not photo.sizes:
            return None
        rendition = photo.sizes[-1]
        size = _photo_size(rendition)
        if size is None:
            return None
        identity = ("photo", photo.id, rendition.type, size)
    expected = min(_DOWNLOAD_BLOCK, max(0, size - index * _DOWNLOAD_BLOCK))
    if not expected:
        return None
    return ((getattr(native, "chat_id", None), native.id, *identity, index), expected)


def _variant_id(message: Any, identity: str) -> str:
    native = getattr(message, "native", message)
    value = f"{getattr(native, 'chat_id', None)}:{native.id}:{identity}".encode()
    return "v-" + hashlib.sha256(value).hexdigest()[:24]


def _document_info(document: Any, *, original: bool, identity: str) -> dict[str, Any]:
    video = next(
        (
            item
            for item in document.attributes
            if isinstance(item, types.DocumentAttributeVideo)
        ),
        None,
    )
    width = video.w if video else None
    height = video.h if video else None
    return dict(
        id=identity,
        label="Original" if original else f"{height}p" if height else "Video variant",
        width=width,
        height=height,
        duration=video.duration if video else None,
        size=document.size,
        mime_type=document.mime_type,
        is_original=original,
    )


def _photo_size(size: Any) -> int | None:
    if isinstance(size, types.PhotoSize):
        return size.size
    if isinstance(size, types.PhotoSizeProgressive):
        return max(size.sizes, default=0)
    if isinstance(size, types.PhotoCachedSize):
        return len(size.bytes)
    return None


def _media_options(message: Any) -> list[tuple[dict[str, Any], Any]]:
    native = getattr(message, "native", message)
    document = getattr(native, "document", None)
    if isinstance(document, types.Document):
        options = [
            (_document_info(document, original=True, identity="original"), native)
        ]
        for alternate in (
            getattr(getattr(native, "media", None), "alt_documents", None) or []
        ):
            if not isinstance(alternate, types.Document) or alternate.id == document.id:
                continue
            identity = _variant_id(message, f"document:{alternate.id}")
            options.append(
                (
                    _document_info(alternate, original=False, identity=identity),
                    alternate,
                )
            )
        return options
    photo = getattr(native, "photo", None)
    if not isinstance(photo, types.Photo):
        return []
    sizes = [size for size in photo.sizes if _photo_size(size)]
    if not sizes:
        return []
    options = []
    for index, size in enumerate(reversed(sizes)):
        original = index == 0
        identity = (
            "original"
            if original
            else _variant_id(message, f"photo:{photo.id}:{size.type}")
        )
        selected = copy.copy(photo)
        selected.sizes = [size]
        info = dict(
            id=identity,
            label="Original" if original else f"{size.w}x{size.h}",
            width=size.w,
            height=size.h,
            duration=None,
            size=_photo_size(size),
            mime_type="image/jpeg",
            is_original=original,
        )
        options.append((info, selected))
    return options


class TelegramMediaSelection:
    """A selected rendition tied to its source message, without changing storage."""

    def __init__(self, message: Any, descriptor: dict[str, Any], media: Any) -> None:
        self.source_message = getattr(message, "source_message", message)
        self.native = getattr(message, "native", message)
        self.variant_id = descriptor["id"]
        self.download_media = media
        self.id = self.native.id
        metadata = SimpleNamespace(
            file_size=descriptor["size"],
            file_name=None,
            mime_type=descriptor["mime_type"],
        )
        self.photo = metadata if isinstance(media, types.Photo) else None
        self.document = metadata if self.photo is None else None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)


class TelegramMessage:
    def __init__(self, client: Client, message: Any) -> None:
        self._client = client
        self.native = message
        self.id = message.id
        self.empty = False
        self.from_user = SimpleNamespace(id=message.sender_id)
        self.text = message.raw_text or ""
        self.command = self.text.split()
        self.photo = None
        self.document = None
        self.media = SimpleNamespace(value="photo" if message.photo else "document")
        if message.file:
            media = SimpleNamespace(
                file_size=message.file.size, file_name=message.file.name
            )
            if message.photo:
                self.photo = media
            else:
                self.document = media

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)

    async def reply_text(self, text: str, reply_markup: Any = None) -> Any:
        sent = await self.native.reply(text, buttons=reply_markup, parse_mode=None)
        return TelegramMessage(self._client, sent)

    async def edit_text(self, text: str, reply_markup: Any = None) -> Any:
        return await self.native.edit(text, buttons=reply_markup, parse_mode=None)

    async def copy(self, channel: int, **_kwargs: Any) -> Any:
        identity = f"{channel}:{self.native.chat_id}:{self.id}".encode()
        random_id = int.from_bytes(
            hashlib.blake2b(identity, digest_size=8).digest(), "big", signed=True
        )
        return await self._client.copy_media(channel, self.native, random_id=random_id)


class Client:
    """Adapt one Telegram account to the pool's transfer and media operations."""

    def __init__(
        self,
        *,
        name: str,
        settings: Settings,
        bot_token: str | None = None,
        session_string: str | None = None,
        updates: bool = False,
    ) -> None:
        self._thumbnail_directory = settings.resolved_cache_dir / "thumbnails"
        self._token = bot_token
        self._upload_pipeline = getattr(settings, "telegram_upload_pipeline", 4)
        self._media_scheduler = MediaScheduler(
            limit=getattr(settings, "telegram_request_limit", 8),
            rate=getattr(settings, "telegram_parts_per_second", 48),
        )
        self._premium = bool(session_string)
        self.display_name: str | None = None
        self.username: str | None = None
        self._identity_cache = IdentityCache(settings.resolved_data_dir)
        self._identity_task: asyncio.Task | None = None
        self._identity_pool = None
        self._identity_next_refresh = 0.0
        self.identity_retry_at: float | None = None
        if bot_token:
            identity = bot_token.split(":", 1)[0]
            prefix = "bot-mode" if updates else "storage-bot"
            self.name = f"{prefix}-{identity}"
        elif session_string:
            identity = hashlib.sha256(session_string.encode()).hexdigest()[:24]
            self.name = f"premium-session-{identity}"
        else:
            self.name = name
        directory = settings.resolved_data_dir / "sessions"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        session = (
            StringSession(session_string)
            if session_string
            else str(directory / self.name)
        )
        self.native = TelegramClient(
            session,
            settings.api_id,
            settings.api_hash.get_secret_value(),
            receive_updates=updates,
            flood_sleep_threshold=0,
            request_retries=2,
            connection_retries=3,
            timeout=20,
            auto_reconnect=True,
        )
        self.native.parse_mode = None

    @property
    def is_connected(self) -> bool:
        return self.native.is_connected()

    async def start(self) -> None:
        await self.native.connect()
        try:
            if not await self.native.is_user_authorized():
                if not self._token:
                    raise TelegramConnectionError(
                        "This Premium session is no longer authorized. Open "
                        "Connections and sign in to the same account again, then "
                        "restart TGDrive and retry.",
                        "TELEGRAM_CREDENTIALS_REVOKED",
                        client_name=self.name,
                    )
                await self.native.sign_in(bot_token=self._token)
            me = await self.native.get_me()
            if me is None:
                raise RuntimeError("Telegram did not confirm the authenticated account")
            if self._token:
                expected_id = int(self._token.split(":", 1)[0])
                if not me.bot or me.id != expected_id:
                    raise RuntimeError("Telegram bot session identity mismatch")
            elif self._premium:
                if me.bot or not me.premium:
                    raise RuntimeError(
                        "Configured Premium session is not a Premium user account"
                    )
                self.name = f"premium-user-{me.id}"
            session_name = getattr(self.native.session, "filename", None)
            if session_name:
                await asyncio.to_thread(Path(session_name).chmod, 0o600)
            if isinstance(self.native, TelegramClient):
                await self._refresh_main_endpoint()
            self._start_identity_refresh(seed=me)
        except BaseException:
            await self.native.disconnect()
            raise

    async def _refresh_main_endpoint(self) -> None:
        """Refresh a stale same-DC address only during startup, with rollback.

        Never choose media-only endpoints for the main RPC connection. Telethon
        continues to own exported senders and migration for file downloads.
        """
        session = self.native.session
        if not all(
            hasattr(session, name) for name in ("dc_id", "server_address", "port")
        ):
            return
        previous = (session.dc_id, session.server_address, session.port)
        try:
            async with asyncio.timeout(8):
                config = await self.native(functions.help.GetConfigRequest())
            options = [
                option
                for option in config.dc_options
                if option.id == previous[0]
                and not option.media_only
                and not option.cdn
                and bool(option.ipv6) == (":" in previous[1])
                and not getattr(option, "tcpo_only", False)
            ]
        except OSError, RPCError, TimeoutError:
            return  # A failed optional refresh cannot prevent startup.
        if not options:
            return
        selected = options[0]
        replacement = (previous[0], selected.ip_address, selected.port)
        if replacement == previous:
            return
        await self.native.disconnect()
        try:
            session.set_dc(*replacement)
            session.save()
            async with asyncio.timeout(15):
                await self.native.connect()
                if not await self.native.is_user_authorized():
                    raise RuntimeError("Refreshed Telegram endpoint lost authorization")
        except BaseException as error:
            await self.native.disconnect()
            session.set_dc(*previous)
            session.save()
            await self.native.connect()
            if isinstance(error, asyncio.CancelledError):
                raise
            log.warning("Telegram endpoint refresh failed; restored previous address")

    def configure_transport(
        self, *, upload_pipeline: int, request_limit: int, parts_per_second: float
    ) -> None:
        if type(upload_pipeline) is not int or not 1 <= upload_pipeline <= 16:
            raise ValueError("Upload pipeline must be between 1 and 16")
        self._media_scheduler.configure(limit=request_limit, rate=parts_per_second)
        self._upload_pipeline = upload_pipeline

    @property
    def identity_refreshing(self) -> bool:
        return self._identity_task is not None and not self._identity_task.done()

    def bind_identity_pool(self, pool: Any) -> None:
        self._identity_pool = pool

    def refresh_identity(self) -> None:
        if self.identity_refreshing or time.monotonic() < self._identity_next_refresh:
            return
        self._identity_next_refresh = time.monotonic() + 5
        self._start_identity_refresh()

    def _start_identity_refresh(self, *, seed: Any = None) -> None:
        if not self.identity_refreshing:
            self._identity_task = asyncio.create_task(self._refresh_identity(seed))

    async def _refresh_identity(self, seed: Any) -> None:
        delay = 5.0
        try:
            deadline = await asyncio.to_thread(self._identity_cache.retry_at, self.name)
        except Exception:
            deadline = 0.0
        if seed is not None:
            try:
                saved = await asyncio.to_thread(self._identity_cache.read, self.name)
                if saved and any(saved.values()):
                    self.display_name, self.username = (
                        saved["display_name"],
                        saved["username"],
                    )
                    if deadline <= time.time():
                        return
            except Exception:
                saved = None  # The optional cache cannot prevent client startup.
        if deadline > time.time():
            self.identity_retry_at = deadline
            if self._identity_pool is not None:
                await self._identity_pool.defer_client(self.name, deadline)
            await self._wait_identity_retry(deadline - time.time())
            seed = None
        while True:
            self.identity_retry_at = None
            try:
                if seed is None:
                    if self._identity_pool is None:
                        async with asyncio.timeout(8):
                            account = await self.native.get_me()
                    else:
                        async with (
                            self._identity_pool.lease(
                                client_name=self.name, premium=self._premium
                            ),
                            asyncio.timeout(8),
                        ):
                            account = await self.native.get_me()
                else:
                    account = seed
                expected_id = int(self.name.rsplit("-", 1)[1])
                if (
                    account is None
                    or account.id != expected_id
                    or bool(account.bot) != bool(self._token)
                ):
                    raise ValueError("Telegram identity does not match this client")
                identity = account_identity(account)
                if not any(identity.values()):
                    raise ValueError("Telegram name is unavailable")
                self.display_name, self.username = (
                    identity["display_name"],
                    identity["username"],
                )
                # A failed optional cache write retries without repeating Telegram RPCs.
                seed = account
                await asyncio.to_thread(self._identity_cache.write, self.name, identity)
                return
            except asyncio.CancelledError:
                raise
            except FloodWaitError as error:
                wait = max(delay, float(error.seconds) + 0.25)
                deadline = time.time() + wait
                with suppress(Exception):
                    await asyncio.to_thread(
                        self._identity_cache.defer, self.name, deadline
                    )
                if self._identity_pool is not None:
                    await self._identity_pool.defer_client(self.name, deadline)
                seed = None
            except Exception:
                wait = delay
                if seed is not None and not (self.display_name or self.username):
                    seed = None
            self.identity_retry_at = time.time() + wait
            await self._wait_identity_retry(wait)
            delay = min(delay * 2, 300)

    async def _wait_identity_retry(self, delay: float) -> None:
        await asyncio.sleep(delay)

    async def stop(self) -> None:
        if self._identity_task is not None:
            self._identity_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._identity_task
            self._identity_task = None
        self.identity_retry_at = None
        await self.native.disconnect()

    async def _defer_read(self, error: FloodWaitError | FloodPremiumWaitError) -> None:
        deadline = time.time() + max(1, error.seconds) + 0.25
        pool = getattr(self, "_identity_pool", None)
        if pool is not None:
            await pool.defer_client(self.name, deadline)
        cache = getattr(self, "_identity_cache", None)
        if cache is not None:
            with suppress(Exception):
                await asyncio.to_thread(cache.defer, self.name, deadline)

    async def get_messages(self, channel: int, *, message_ids: Any) -> Any:
        scheduler = getattr(self, "_media_scheduler", None)
        try:
            if scheduler is None:
                result = await self.native.get_messages(channel, ids=message_ids)
            else:
                async with scheduler.request():
                    result = await self.native.get_messages(channel, ids=message_ids)
        except FloodWaitError as error:
            if scheduler is not None:
                await scheduler.flood(max(1, error.seconds) + 0.25)
            await self._defer_read(error)
            raise
        if isinstance(result, list):
            return [TelegramMessage(self, item) if item else None for item in result]
        return TelegramMessage(self, result) if result else None

    async def _sent(self, channel: int, result: Any) -> TelegramMessage:
        if any(
            isinstance(item, types.UpdateNewScheduledMessage)
            or getattr(
                getattr(item, "message", None), "video_processing_pending", False
            )
            for item in result.updates
        ):
            raise RuntimeError(
                "Telegram is processing scheduled media; no delivered storage "
                "message has been confirmed. Preserve the source for recovery."
            )
        message_id = next(
            (
                item.id
                for item in result.updates
                if isinstance(item, types.UpdateMessageID)
            ),
            None,
        )
        if message_id is None:
            message_id = next(
                (
                    item.message.id
                    for item in result.updates
                    if hasattr(item, "message")
                ),
                None,
            )
        if message_id is None:
            raise RuntimeError(
                "Telegram has not confirmed the storage message; "
                "retry the same operation"
            )
        message = await self.get_messages(channel, message_ids=message_id)
        if message is None:
            raise RuntimeError(
                "Telegram confirmed a message that cannot yet be fetched"
            )
        return message

    supports_generated_thumbnails = True

    def bind_send_gate(
        self,
        gate: Callable[[int], Awaitable[None]],
        defer_channel: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._send_gate = gate
        self._defer_channel = defer_channel

    async def _publish(self, channel: int, request: Any) -> Any:
        gate = getattr(self, "_send_gate", None)
        if gate is not None:
            await gate(channel)
        try:
            return await self.native(request)
        except FloodWaitError as exc:
            # Save*Part FloodWait happens before this boundary and is account-scoped.
            # SendMedia may be channel-scoped: every writer observes this cooldown.
            exc.tgdrive_channel_flood = True
            defer = getattr(self, "_defer_channel", None)
            if defer is not None:
                await defer(time.time() + exc.seconds + 0.25)
            raise

    async def send_staged(
        self,
        channel: int,
        path: Path,
        *,
        file_name: str,
        random_id: int,
        progress: Any = None,
        caption: str = "",
        offset: int = 0,
        size: int | None = None,
        expected_sha256: str | None = None,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
        preview_name: str | None = None,
        thumbnail: bytes | None = None,
        generate_thumbnail: bool = True,
    ) -> TelegramMessage:
        length = (
            (await asyncio.to_thread(path.stat)).st_size - offset
            if size is None
            else size
        )
        from tgdrive.media.generated_thumbnails import generate, save

        cover = thumbnail
        if cover is None and generate_thumbnail and offset == 0:
            cover = await generate(path, preview_name or file_name)
        with FileWindow(path, offset, length, name=file_name) as source:
            uploaded = await self._upload_parts(
                source, length=length, file_name=file_name, progress=progress
            )
            if expected_sha256 is not None:
                source.verify(expected_sha256)
        thumbnail = None
        if cover:
            try:
                async with asyncio.timeout(4):

                    class CoverSource(io.BytesIO):
                        def verify(self, _expected):
                            pass

                    thumbnail = await self._upload_parts(
                        CoverSource(cover),
                        length=len(cover),
                        file_name="thumbnail.jpg",
                        progress=None,
                    )
            except OSError, TimeoutError, RPCError:
                pass  # A failed optional thumbnail must not fail the original.
        media = types.InputMediaUploadedDocument(
            file=uploaded,
            thumb=thumbnail,
            mime_type=mimetypes.guess_type(file_name)[0] or "application/octet-stream",
            attributes=[types.DocumentAttributeFilename(file_name)],
            force_file=True,
        )
        peer = await self.native.get_input_entity(channel)
        gate = getattr(self, "_send_gate", None)
        if gate is not None:
            await gate(channel)
        if before_dispatch is not None:
            await before_dispatch()
        # Gate was consumed above so the durable boundary is immediately before RPC.
        try:
            result = await self.native(
                functions.messages.SendMediaRequest(
                    peer=peer,
                    media=media,
                    message=caption,
                    random_id=random_id,
                    silent=True,
                )
            )
        except FloodWaitError as exc:
            exc.tgdrive_channel_flood = True
            defer = getattr(self, "_defer_channel", None)
            if defer is not None:
                await defer(time.time() + exc.seconds + 0.25)
            raise
        except RPCError as exc:
            if thumbnail is None or "THUMB" not in str(exc).upper():
                raise
            media.thumb = None
            result = await self._publish(
                channel,
                functions.messages.SendMediaRequest(
                    peer=peer,
                    media=media,
                    message=caption,
                    random_id=random_id,
                    silent=True,
                ),
            )
        message = await self._sent(channel, result)
        directory = getattr(self, "_thumbnail_directory", None)
        if cover and directory is not None:
            with suppress(OSError):
                await asyncio.to_thread(save, directory, channel, message.id, cover)
        return message

    async def _upload_parts(
        self, source: FileWindow, *, length: int, file_name: str, progress: Any
    ) -> Any:
        """Pipeline bounded idempotent protocol parts; publish only after all ACKs."""
        part_size = 512 * 1024
        part_count = math.ceil(length / part_size)
        file_id = secrets.randbits(63)
        big = length > 10 * 1024 * 1024
        digest = hashlib.md5(usedforsecurity=False) if not big else None
        pending: set[asyncio.Task[int]] = set()
        acknowledged = 0

        async def send(request: Any, count: int) -> int:
            attempt = 0
            while True:
                try:
                    async with self._media_scheduler.request():
                        accepted = await self.native(request)
                        if not accepted:
                            raise OSError(
                                "Telegram did not acknowledge the upload part"
                            )
                    self._media_scheduler.record_bytes(count, upload=True)
                    return count
                except (FloodWaitError, FloodPremiumWaitError) as error:
                    await self._media_scheduler.flood(max(1, error.seconds) + 0.25)
                    await self._defer_read(error)
                    # The shared scheduler, not each worker, observes the wait.
                    # Flood waits do not consume the transient network retry
                    # budget or discard successfully acknowledged sibling parts.
                except (OSError, TimeoutError, RPCError) as error:
                    if isinstance(error, RPCError) and getattr(error, "code", 0) < 500:
                        raise
                    attempt += 1
                    if attempt >= 3:
                        raise
                    await asyncio.sleep(0.25 * (2 ** (attempt - 1)))

        async def collect() -> None:
            nonlocal pending, acknowledged
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            # Retrieve every outcome even if another completed part failed.
            results = await asyncio.gather(*done, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
                acknowledged += result
            if progress is not None:
                value = progress(acknowledged, length)
                if inspect.isawaitable(value):
                    await value

        try:
            for index in range(part_count):
                if len(pending) >= self._upload_pipeline:
                    await collect()
                read = asyncio.create_task(asyncio.to_thread(source.read, part_size))
                try:
                    payload = await asyncio.shield(read)
                except asyncio.CancelledError:
                    # FileWindow must outlive an already running filesystem read.
                    await read
                    raise
                expected = min(part_size, length - index * part_size)
                if len(payload) != expected:
                    raise OSError("Staged file ended before the declared upload length")
                if digest is not None:
                    digest.update(payload)
                request = (
                    functions.upload.SaveBigFilePartRequest(
                        file_id=file_id,
                        file_part=index,
                        file_total_parts=part_count,
                        bytes=payload,
                    )
                    if big
                    else functions.upload.SaveFilePartRequest(
                        file_id=file_id,
                        file_part=index,
                        bytes=payload,
                    )
                )
                pending.add(asyncio.create_task(send(request, len(payload))))
            while pending:
                await collect()
            source.verify(None)
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if big:
            return types.InputFileBig(id=file_id, parts=part_count, name=file_name)
        return types.InputFile(
            id=file_id,
            parts=part_count,
            name=file_name,
            md5_checksum=digest.hexdigest(),
        )

    async def send_document(
        self, channel: int, document: Any, **options: Any
    ) -> TelegramMessage:
        path = Path(document if isinstance(document, (str, Path)) else document.name)
        return await self.send_staged(
            channel,
            path,
            file_name=options.get("file_name", path.name),
            random_id=options.get("random_id", secrets.randbits(63)),
            progress=options.get("progress"),
            **(
                {"caption": options.get("caption") or ""}
                if "caption" in options
                else {}
            ),
        )

    async def copy_media(
        self, channel: int, message: Any, *, random_id: int
    ) -> TelegramMessage:
        result = await self._publish(
            channel,
            functions.messages.SendMediaRequest(
                peer=await self.native.get_input_entity(channel),
                media=utils.get_input_media(message.media),
                message=message.raw_text or "",
                random_id=random_id,
                silent=True,
            ),
        )
        return await self._sent(channel, result)

    async def get_pinned_message_id(self, channel: int) -> int | None:
        """Discover the channel's latest pin without bot-prohibited history/search."""
        peer = await self.native.get_input_entity(channel)
        result = await self.native(
            functions.channels.GetFullChannelRequest(utils.get_input_channel(peer))
        )
        identity = getattr(result.full_chat, "pinned_msg_id", None)
        if identity is not None and (type(identity) is not int or identity <= 0):
            raise RuntimeError("Telegram returned an invalid pinned message identity")
        return identity

    async def storage_channel_problem(
        self, channel: int, *, require_pin: bool
    ) -> tuple[str, str] | None:
        """Read current retention, privacy and bot rights without changing messages."""
        from tgdrive.telegram.channel_state import (
            storage_bot_rights_problem,
            storage_channel_problem,
        )

        peer = await self.native.get_input_entity(channel)
        result = await self.native(
            functions.channels.GetFullChannelRequest(utils.get_input_channel(peer))
        )
        chat = next(
            (item for item in result.chats if utils.get_peer_id(item) == channel),
            None,
        )
        return storage_channel_problem(
            chat, result.full_chat
        ) or storage_bot_rights_problem(chat, require_pin=require_pin)

    async def pin_message(
        self, channel: int, message_id: int, *, silent: bool = True
    ) -> Any:
        """Pin a known message; channel administrators require edit_messages."""
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("Pinned message identity must be a positive integer")
        return await self.native(
            functions.messages.UpdatePinnedMessageRequest(
                peer=await self.native.get_input_entity(channel),
                id=message_id,
                silent=silent,
            )
        )

    async def delete_messages(self, channel: int, message_ids: Any) -> Any:
        return await self.native.delete_messages(channel, message_ids, revoke=True)

    async def download_media(self, message: Any, *, file_name: str) -> Any:
        return await self.native.download_media(
            getattr(message, "native", message), file=file_name
        )

    async def download_thumbnail(
        self,
        message: Any,
        *,
        max_bytes: int = 128 * 1024,
        target_size: int = 320,
        max_dimension: int = 640,
    ) -> bytes | None:
        """Read one advertised image thumbnail; never download the document.

        Telegram's regular download_media defaults to the entire original when
        no thumbnail is supplied. Build an explicit thumbnail location instead,
        reject oversized advertised sizes, and bound even a malformed response.
        Animated/video thumbnails and SVG sticker outlines are not requested.
        """
        native = getattr(message, "native", message)
        document = getattr(native, "document", None)
        photo = getattr(native, "photo", None)
        if isinstance(document, types.Document):
            media, sizes = document, document.thumbs or []
            location_type = types.InputDocumentFileLocation
        elif isinstance(photo, types.Photo):
            media, sizes = photo, photo.sizes
            location_type = types.InputPhotoFileLocation
        else:
            return None
        max_bytes = min(max_bytes, 128 * 1024)
        max_dimension = min(max_dimension, 640)
        choices = []
        stripped = []
        for size in sizes:
            if isinstance(size, types.PhotoStrippedSize):
                if 0 < len(size.bytes) <= max_bytes:
                    stripped.append(size)
                continue
            if not isinstance(
                size,
                (types.PhotoSize, types.PhotoSizeProgressive, types.PhotoCachedSize),
            ):
                continue
            amount = _photo_size(size)
            dimension = max(size.w, size.h)
            if (
                amount is not None
                # An empty document thumb selector requests the original.
                and isinstance(size.type, str)
                and bool(size.type)
                and 0 < amount <= max_bytes
                and min(size.w, size.h) > 0
                and dimension <= max_dimension
            ):
                choices.append((size, dimension, amount))
        if not choices:
            if stripped:
                result = utils.stripped_photo_to_jpg(stripped[0].bytes)
                return result if 0 < len(result) <= max_bytes else None
            return None
        # Prefer the smallest rendition which fills a 320px tile. If none
        # reaches that size, use the largest smaller rendition, never original.
        selected, _dimension, _amount = min(
            choices,
            key=lambda choice: (
                choice[1] < target_size,
                abs(choice[1] - target_size),
                choice[2],
            ),
        )
        if isinstance(selected, types.PhotoCachedSize):
            return selected.bytes
        location = location_type(
            id=media.id,
            access_hash=media.access_hash,
            file_reference=media.file_reference,
            thumb_size=selected.type,
        )
        request_size = 32 * 1024
        iterator = self.native.iter_download(
            location,
            request_size=request_size,
            chunk_size=request_size,
            limit=(max_bytes + request_size - 1) // request_size + 1,
            dc_id=media.dc_id,
        )
        data = bytearray()
        scheduler = getattr(self, "_media_scheduler", None)
        try:
            while True:
                if scheduler is None:
                    part = await anext(iterator, None)
                else:
                    async with scheduler.request():
                        part = await anext(iterator, None)
                    if part is not None:
                        scheduler.record_bytes(len(part))
                if part is None:
                    break
                if len(data) + len(part) > max_bytes:
                    return None
                data.extend(part)
            return bytes(data) or None
        except (FloodWaitError, FloodPremiumWaitError) as error:
            if scheduler is not None:
                await scheduler.flood(max(1, error.seconds) + 0.25)
            await self._defer_read(error)
            raise
        finally:
            await iterator.close()

    @staticmethod
    def describe_media(message: Any) -> dict[str, Any]:
        options = _media_options(message)
        return {
            "original": options[0][0] if options else None,
            "variants": [descriptor for descriptor, _ in options[1:]],
        }

    @staticmethod
    def select_media_variant(message: Any, quality_id: str | None) -> Any:
        if quality_id in {None, "original"}:
            return getattr(message, "source_message", message)
        for descriptor, media in _media_options(message):
            if descriptor["id"] == quality_id:
                return TelegramMediaSelection(message, descriptor, media)
        raise ValueError("The requested media quality is unavailable for this file")

    supports_uncached_media = True

    async def stream_media(
        self, message: Any, *, limit: int, offset: int, use_cache: bool = True
    ) -> AsyncIterator[bytes]:
        """Yield ordered MiB blocks with at most four windows of read-ahead."""
        if limit <= 0:
            return
        if offset < 0:
            raise ValueError("Download offset cannot be negative")
        refreshed = 0
        generation = 0
        refresh_lock = asyncio.Lock()
        selection_id = getattr(message, "variant_id", None)

        def media_identity(value: Any) -> tuple[str, int] | None:
            native = getattr(value, "native", value)
            for kind in ("document", "photo"):
                media = getattr(native, kind, None)
                if media is not None:
                    return kind, media.id
            return None

        original_identity = media_identity(message)

        async def read_remote_block(index: int) -> bytes:
            nonlocal message, refreshed, generation
            data = bytearray()
            while len(data) < _DOWNLOAD_BLOCK:
                version = generation
                # Real Telethon messages also have a download_media method.
                # Only our explicit selection wrapper owns a rendition target.
                target = (
                    message.download_media
                    if isinstance(message, TelegramMediaSelection)
                    else getattr(message, "native", message)
                )
                iterator = self.native.iter_download(
                    target,
                    offset=index * _DOWNLOAD_BLOCK + len(data),
                    request_size=_DOWNLOAD_REQUEST,
                    limit=(_DOWNLOAD_BLOCK - len(data) + _DOWNLOAD_REQUEST - 1)
                    // _DOWNLOAD_REQUEST,
                )
                try:
                    while len(data) < _DOWNLOAD_BLOCK:
                        scheduler = getattr(self, "_media_scheduler", None)
                        if scheduler is None:
                            try:
                                part = await anext(iterator)
                            except StopAsyncIteration:
                                break
                        else:
                            async with scheduler.request():
                                try:
                                    part = await anext(iterator)
                                except StopAsyncIteration:
                                    part = None
                            if part is None:
                                break
                            scheduler.record_bytes(len(part))
                        data.extend(part[: _DOWNLOAD_BLOCK - len(data)])
                        if len(data) == _DOWNLOAD_BLOCK:
                            break
                    return bytes(data)
                except (FloodWaitError, FloodPremiumWaitError) as error:
                    scheduler = getattr(self, "_media_scheduler", None)
                    if scheduler is not None:
                        await scheduler.flood(max(1, error.seconds) + 0.25)
                    await self._defer_read(error)
                    raise
                except (
                    FileReferenceExpiredError,
                    FileReferenceInvalidError,
                    FilerefUpgradeNeededError,
                ):
                    async with refresh_lock:
                        if version == generation:
                            if refreshed >= 2:
                                raise
                            refreshed += 1
                            native = getattr(message, "native", message)
                            fresh = await self.get_messages(
                                native.chat_id, message_ids=native.id
                            )
                            if fresh is None:
                                raise FileNotFoundError(
                                    "Stored Telegram message was removed"
                                ) from None
                            if (
                                original_identity is not None
                                and media_identity(fresh) != original_identity
                            ):
                                raise FileNotFoundError(
                                    "Stored Telegram media was replaced"
                                ) from None
                            message = self.select_media_variant(fresh, selection_id)
                            generation += 1
                finally:
                    await iterator.close()
            return bytes(data)

        async def read_block(index: int) -> bytes:
            if not use_cache:
                return await read_remote_block(index)
            identity = _block_identity(message, index)
            if identity is None:
                return await read_remote_block(index)
            media_key, expected = identity
            cache = _client_block_cache(self)
            key = (id(self), getattr(self, "name", None), *media_key)
            return await cache.get(key, expected, lambda: read_remote_block(index))

        pending: deque[asyncio.Task[bytes]] = deque()
        tasks: set[asyncio.Task[bytes]] = set()
        next_index = offset
        end = offset + limit

        def schedule() -> None:
            nonlocal next_index
            task = asyncio.create_task(read_block(next_index))
            pending.append(task)
            tasks.add(task)
            next_index += 1

        for _ in range(min(_DOWNLOAD_PREFETCH, limit)):
            schedule()
        try:
            while pending:
                task = pending.popleft()
                data = await task
                tasks.discard(task)
                if not data:
                    return
                yield data
                if len(data) < _DOWNLOAD_BLOCK:
                    return
                if next_index < end:
                    schedule()
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def register_bot(self, mode: Any) -> None:
        async def dispatch(event: Any) -> None:
            if (
                not event.is_private
                or event.sender_id not in mode.settings.telegram_admin_ids
            ):
                return
            message = TelegramMessage(self, event.message)
            words = message.text.split()
            command = words[0].split("@")[0].lower() if words else ""
            label = message.text.strip().casefold()
            if message.document or message.photo:
                await mode._file(self, message)
            elif command in {"/start", "/help"} or label == "help":
                await mode._help(self, message)
            elif command in {"/set_folder", "/folders"} or label == "choose folder":
                await mode._set_folder(self, message)
            elif command == "/current_folder" or label == "current folder":
                await mode._current_folder(self, message)
            elif command == "/cancel":
                await mode._cancel(self, message)
            else:
                await mode._folder_query(self, message)

        self.native.add_event_handler(dispatch, events.NewMessage(incoming=True))


def build_client_pool(settings: Settings) -> ClientPool:
    """Create lazy client factories with the configured account admission limits."""
    return ClientPool(
        bot_clients=[
            partial(
                Client, name=f"storage-bot-{index}", settings=settings, bot_token=token
            )
            for index, token in enumerate(settings.bot_tokens, 1)
        ],
        user_clients=[
            partial(
                Client,
                name=f"premium-user-{index}",
                settings=settings,
                session_string=session,
            )
            for index, session in enumerate(settings.string_sessions, 1)
        ],
        allow_partial_start=True,
        media_send_interval=1.0,
        uploads_per_bot=settings.telegram_uploads_per_bot,
        media_cache_dir=settings.resolved_data_dir / "media-cache",
        media_per_bot=getattr(settings, "telegram_media_per_bot", 3),
        max_media_queue=getattr(settings, "telegram_max_queue", 256),
    )


def build_bot_mode_client(settings: Settings) -> Client | None:
    """Create the separate update-receiving bot only when bot mode is configured."""
    token = settings.main_bot_token_value
    if not token:
        return None
    return Client(
        name="tgdrive-main-bot", settings=settings, bot_token=token, updates=True
    )
