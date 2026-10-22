"""Bounded Telegram transport for independent, journaled channel replicas.

Existing documents are sent with messages.sendMedia/InputMediaDocument rather
than forwarded or downloaded/re-uploaded. The caller persists random IDs before
calling this layer, and must retain them after any ambiguous provider response.
https://core.telegram.org/method/messages.sendMedia
Account-wide deduplication and temporary in-flight duplicates:
https://core.telegram.org/api/updates#updatemessageid-updates
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from telethon import TelegramClient, functions, types, utils
from telethon.sessions import StringSession

CAPSULE_NAME = "tgdrive-replica.tgdb"
MAX_CAPSULE_BYTES = 64 * 1024 * 1024
_BLOCK = 512 * 1024
_BOT = re.compile(r"[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}")


class ReplicaTransportError(RuntimeError):
    """A credential-free error suitable for the owner-facing replica report."""

    def __init__(
        self,
        detail: str,
        code: str = "REPLICA_TRANSPORT",
        *,
        retry_after: float | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.retry_after = retry_after
        self.retryable = retryable


def _provider_error(error: Exception) -> ReplicaTransportError:
    name = type(error).__name__
    if name == "RandomIdDuplicateError":
        return ReplicaTransportError(
            "Telegram is still completing this message. Backup will retry the "
            "same saved operation after a short wait.",
            "REPLICA_IN_FLIGHT",
            retry_after=5,
            retryable=True,
        )
    seconds = getattr(error, "seconds", None)
    if (
        type(seconds) in (int, float)
        and math.isfinite(seconds)
        and (seconds > 0 or name in {"FloodWaitError", "SlowModeWaitError"})
    ):
        return ReplicaTransportError(
            "Telegram requested a pause. Backup will resume after the wait.",
            "REPLICA_FLOOD_WAIT",
            retry_after=max(1, seconds),
            retryable=True,
        )
    if name in {
        "AccessTokenExpiredError",
        "AccessTokenInvalidError",
        "AuthKeyUnregisteredError",
        "AuthKeyInvalidError",
        "AuthKeyDuplicatedError",
        "SessionRevokedError",
        "UserDeactivatedError",
        "UserDeactivatedBanError",
        "TokenInvalidError",
        "UnauthorizedError",
    }:
        return ReplicaTransportError(
            "The backup bot cannot sign in. Check its token or replace the bot.",
            "REPLICA_CREDENTIALS",
        )
    if name in {
        "ChannelPrivateError",
        "ChannelInvalidError",
        "ChatAdminRequiredError",
        "UserNotParticipantError",
        "ChatWriteForbiddenError",
        "PeerIdInvalidError",
        "ChatForbiddenError",
        "UserBannedInChannelError",
    }:
        return ReplicaTransportError(
            "The backup bot needs administrator access to both private channels. "
            "Check channel IDs and its post, edit and delete permissions.",
            "REPLICA_ACCESS",
        )
    if name in {"ChatForwardsRestrictedError", "MediaTtlInvalidError"}:
        return ReplicaTransportError(
            "Telegram content protection prevents copying. Disable Restrict Saving "
            "Content on the dedicated TGDrive channels, then retry.",
            "REPLICA_PROTECTED",
        )
    if name in {
        "FileReferenceExpiredError",
        "FileReferenceInvalidError",
        "FilerefUpgradeNeededError",
    }:
        return ReplicaTransportError(
            "Telegram's file reference expired. Backup will fetch it again and retry.",
            "REPLICA_FILE_REFERENCE",
            retryable=True,
        )
    if isinstance(error, (TimeoutError, OSError)) or name in {
        "ServerError",
        "RpcCallFailError",
        "TimedOutError",
        "FloodError",
    }:
        return ReplicaTransportError(
            "Telegram could not confirm the operation. It is safe to resume the "
            "same backup operation after connectivity returns.",
            "REPLICA_UNAVAILABLE",
            retryable=True,
        )
    return ReplicaTransportError(
        "Telegram rejected the backup operation. Check channel access and retry; "
        "the existing confirmed backup has been preserved.",
        "REPLICA_PROVIDER_ERROR",
    )


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReplicaTransportError(f"Invalid {name}.", "REPLICA_INPUT")
    return value


def _channel(value: Any) -> int:
    return _integer(value, "channel ID", minimum=-(1 << 63), maximum=-1)


def _message(value: Any) -> int:
    return _integer(value, "message ID", minimum=1, maximum=(1 << 31) - 1)


def _random(value: Any) -> int:
    value = _integer(
        value, "operation identity", minimum=-(1 << 63), maximum=(1 << 63) - 1
    )
    if value == 0:
        raise ReplicaTransportError("Invalid operation identity.", "REPLICA_INPUT")
    return value


class ReplicaTransport:
    """One independent backup bot, with no unbounded history/search operations.

    The backup bot must be an administrator in source and destination channels:
    a fresh source message supplies its current file reference, while an actual
    destination message is independently confirmed before a replica is counted.
    A native client may be injected for offline transport-contract tests.
    """

    def __init__(self, settings, bot_token: str, *, client=None) -> None:
        self._settings = settings
        self._token = bot_token.strip() if isinstance(bot_token, str) else ""
        self._client = client
        self._started = False
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _guard(self, seconds: int = 90):
        try:
            async with asyncio.timeout(seconds):
                yield
        except ReplicaTransportError:
            raise
        except Exception as error:
            raise _provider_error(error) from None

    async def start(self) -> None:
        if self._started:
            return
        if not _BOT.fullmatch(self._token):
            raise ReplicaTransportError(
                "Enter a valid backup bot token.", "REPLICA_CREDENTIALS"
            )
        identity = int(self._token.split(":", 1)[0])
        primary = getattr(self._settings, "bot_tokens", ())
        if any(int(token.split(":", 1)[0]) == identity for token in primary):
            raise ReplicaTransportError(
                "Use a separate backup bot; this bot is already a primary storage bot.",
                "REPLICA_SAME_BOT",
            )
        if self._client is None:
            self._client = TelegramClient(
                StringSession(),
                self._settings.api_id,
                self._settings.api_hash.get_secret_value(),
                receive_updates=False,
                flood_sleep_threshold=0,
                request_retries=0,
                raise_last_call_error=True,
                connection_retries=0,
                timeout=15,
                auto_reconnect=True,
            )
            self._client.parse_mode = None
        try:
            async with self._guard(30):
                await self._client.connect()
                await self._client.sign_in(bot_token=self._token)
                account = await self._client.get_me()
                if (
                    account is None
                    or not getattr(account, "bot", False)
                    or getattr(account, "id", None) != identity
                ):
                    raise ReplicaTransportError(
                        "The backup bot identity did not match its token.",
                        "REPLICA_CREDENTIALS",
                    )
            self._started = True
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self._started = False
        if self._client is not None:
            with suppress(Exception):
                async with asyncio.timeout(10):
                    await self._client.disconnect()

    def _ready(self) -> None:
        if not self._started:
            raise ReplicaTransportError(
                "Connect the backup bot before continuing.", "REPLICA_NOT_CONNECTED"
            )

    async def inspect(self, channel: int, *, writable: bool = True) -> dict:
        self._ready()
        channel = _channel(channel)
        async with self._guard(25):
            peer = await self._client.get_input_entity(channel)
            result = await self._client(
                functions.channels.GetFullChannelRequest(utils.get_input_channel(peer))
            )
            chat = next(
                (row for row in result.chats if utils.get_peer_id(row) == channel), None
            )
            if chat is None or not getattr(chat, "broadcast", False):
                raise ReplicaTransportError(
                    "Use a dedicated private broadcast channel, not a group.",
                    "REPLICA_CHANNEL_TYPE",
                )
            if getattr(chat, "username", None) or any(
                getattr(row, "active", False)
                for row in (getattr(chat, "usernames", None) or ())
            ):
                raise ReplicaTransportError(
                    "Both TGDrive channels must be private. Remove the public "
                    "channel username in Telegram, then retry.",
                    "REPLICA_PUBLIC_CHANNEL",
                )
            if (
                getattr(chat, "restricted", False)
                or getattr(chat, "noforwards", False)
                or getattr(result.full_chat, "noforwards", False)
            ):
                raise ReplicaTransportError(
                    "This channel restricts saving or copying content. Remove the "
                    "restriction in Telegram before using channel backups.",
                    "REPLICA_PROTECTED",
                )
            if getattr(chat, "left", False):
                raise ReplicaTransportError(
                    "The backup bot no longer belongs to this channel. Add it "
                    "again as an administrator before resuming.",
                    "REPLICA_ACCESS",
                )
            rights = getattr(chat, "admin_rights", None)
            creator = getattr(chat, "creator", False)
            required = ("post_messages", "edit_messages", "delete_messages")
            if not creator and (
                rights is None
                or (
                    writable
                    and not all(getattr(rights, key, False) for key in required)
                )
            ):
                raise ReplicaTransportError(
                    "Add the backup bot as an administrator in both private "
                    "channels with post, edit and delete permissions.",
                    "REPLICA_PERMISSIONS",
                )
            return {"id": channel, "title": str(chat.title)[:256], "private": True}

    async def pinned_message(self, channel: int) -> int | None:
        """Read only the current channel pin, never history or a search result."""
        self._ready()
        channel = _channel(channel)
        async with self._lock, self._guard(25):
            await self.inspect(channel, writable=False)
            peer = await self._client.get_input_entity(channel)
            result = await self._client(
                functions.channels.GetFullChannelRequest(utils.get_input_channel(peer))
            )
            identity = getattr(result.full_chat, "pinned_msg_id", None)
            return None if identity is None else _message(identity)

    async def _get(self, channel: int, message_id: int):
        message = await self._client.get_messages(channel, ids=message_id)
        if message is None or getattr(message, "id", None) != message_id:
            raise ReplicaTransportError(
                "A required channel message is missing. Restore channel access or "
                "choose another confirmed recovery point.",
                "REPLICA_MESSAGE_MISSING",
            )
        peer = getattr(message, "peer_id", None)
        if peer is not None and utils.get_peer_id(peer) != channel:
            raise ReplicaTransportError(
                "Telegram returned a message from an unexpected channel.",
                "REPLICA_VERIFY_FAILED",
            )
        document = getattr(message, "document", None)
        if document is None:
            raise ReplicaTransportError(
                "The required message no longer contains a document.",
                "REPLICA_DOCUMENT_MISSING",
            )
        if getattr(message, "noforwards", False) or getattr(
            getattr(message, "media", None), "ttl_seconds", None
        ):
            raise ReplicaTransportError(
                "The required document is protected or expires automatically.",
                "REPLICA_PROTECTED",
            )
        return message, document

    async def _sent(self, channel: int, result: Any, random_id: int) -> int:
        if isinstance(result, types.UpdateShortSentMessage):
            return _message(result.id)
        updates = (
            [result.update]
            if isinstance(result, types.UpdateShort)
            else getattr(result, "updates", ())
        )
        if any(isinstance(row, types.UpdateNewScheduledMessage) for row in updates):
            raise ReplicaTransportError(
                "Telegram scheduled the backup instead of confirming delivery.",
                "REPLICA_NOT_CONFIRMED",
                retryable=True,
            )
        identity = next(
            (
                row.id
                for row in updates
                if isinstance(row, types.UpdateMessageID) and row.random_id == random_id
            ),
            None,
        )
        if identity is None:
            identity = next(
                (
                    row.message.id
                    for row in updates
                    if isinstance(
                        row, (types.UpdateNewMessage, types.UpdateNewChannelMessage)
                    )
                    and getattr(row.message, "peer_id", None) is not None
                    and utils.get_peer_id(row.message.peer_id) == channel
                ),
                None,
            )
        if identity is None:
            raise ReplicaTransportError(
                "Telegram did not confirm a destination message. Resume the same "
                "operation; do not start a second copy.",
                "REPLICA_NOT_CONFIRMED",
                retryable=True,
            )
        return _message(identity)

    async def copy(
        self,
        source_channel: int,
        source_message: int,
        destination_channel: int,
        *,
        random_id: int,
        expected_size: int,
    ) -> int:
        self._ready()
        source_channel, destination_channel = (
            _channel(source_channel),
            _channel(destination_channel),
        )
        source_message, random_id = _message(source_message), _random(random_id)
        _integer(expected_size, "document size", minimum=0, maximum=(1 << 63) - 1)
        if source_channel == destination_channel:
            raise ReplicaTransportError(
                "Choose a backup channel different from the source channel.",
                "REPLICA_SAME_CHANNEL",
            )
        async with self._lock, self._guard():
            await self.inspect(source_channel, writable=False)
            await self.inspect(destination_channel)
            _, document = await self._get(source_channel, source_message)
            if getattr(document, "size", None) != expected_size:
                raise ReplicaTransportError(
                    "A source document size differs from the catalog. Preserve "
                    "the current backup and review this file.",
                    "REPLICA_SIZE_MISMATCH",
                )
            peer = await self._client.get_input_entity(destination_channel)
            result = await self._client(
                functions.messages.SendMediaRequest(
                    peer=peer,
                    media=types.InputMediaDocument(
                        id=utils.get_input_document(document)
                    ),
                    message="",
                    random_id=random_id,
                    silent=True,
                )
            )
            identity = await self._sent(destination_channel, result, random_id)
            sent, copied = await self._get(destination_channel, identity)
            if (
                copied.size != expected_size
                or copied.id != document.id
                or copied.access_hash != document.access_hash
                or getattr(sent, "fwd_from", None) is not None
            ):
                raise ReplicaTransportError(
                    "Telegram's copied document did not match the source. The "
                    "replica has not been marked protected.",
                    "REPLICA_VERIFY_FAILED",
                )
            return identity

    async def verify(self, channel: int, message_id: int, expected_size: int) -> None:
        """Verify an existing committed document without reading original bytes."""
        self._ready()
        channel, message_id = _channel(channel), _message(message_id)
        _integer(expected_size, "document size", minimum=0, maximum=(1 << 63) - 1)
        async with self._lock, self._guard():
            await self.inspect(channel, writable=False)
            _, document = await self._get(channel, message_id)
            if getattr(document, "size", None) != expected_size:
                raise ReplicaTransportError(
                    "The channel document does not match its catalog size. "
                    "Preserve the current catalog and choose another recovery point.",
                    "REPLICA_SIZE_MISMATCH",
                )

    @staticmethod
    def _capsule(document, filename: str = CAPSULE_NAME) -> int:
        size = getattr(document, "size", None)
        names = [
            row.file_name
            for row in getattr(document, "attributes", ())
            if isinstance(row, types.DocumentAttributeFilename)
        ]
        if names != [filename]:
            raise ReplicaTransportError(
                "The selected message has a different document type. Choose a "
                "confirmed TGDrive recovery checkpoint.",
                "REPLICA_DOCUMENT_TYPE",
            )
        if type(size) is not int or not 0 < size <= MAX_CAPSULE_BYTES:
            raise ReplicaTransportError(
                "The selected recovery archive exceeds the bounded transport "
                "size. Choose a supported confirmed checkpoint.",
                "REPLICA_ARCHIVE_INVALID",
            )
        return size

    async def _read_to(self, document, target, size: int) -> str:
        digest = hashlib.sha256()
        count = 0
        iterator = self._client.iter_download(
            document, request_size=_BLOCK, chunk_size=_BLOCK, file_size=size
        )
        try:
            async for block in iterator:
                if not isinstance(block, (bytes, bytearray, memoryview)):
                    raise ReplicaTransportError(
                        "Telegram returned an invalid recovery archive block.",
                        "REPLICA_VERIFY_FAILED",
                    )
                count += len(block)
                if count > size or count > MAX_CAPSULE_BYTES:
                    raise ReplicaTransportError(
                        "The recovery archive exceeded its declared size.",
                        "REPLICA_VERIFY_FAILED",
                    )
                digest.update(block)
                if target is not None:
                    target.write(block)
        finally:
            if callable(getattr(iterator, "aclose", None)):
                await iterator.aclose()
        if count != size:
            raise ReplicaTransportError(
                "Telegram returned an incomplete recovery archive. Retry the "
                "same operation.",
                "REPLICA_VERIFY_FAILED",
                retryable=True,
            )
        return digest.hexdigest()

    async def _publish(
        self,
        channel: int,
        path: Path,
        *,
        random_id: int,
        caption: str,
        pin: bool,
        filename: str,
    ) -> int:
        self._ready()
        channel, random_id, path = _channel(channel), _random(random_id), Path(path)
        if not isinstance(caption, str) or len(caption) > 1024:
            raise ReplicaTransportError("Invalid checkpoint caption.", "REPLICA_INPUT")
        async with self._lock, self._guard(180):
            await self.inspect(channel)
            size = path.stat().st_size
            if not 0 < size <= MAX_CAPSULE_BYTES:
                raise ReplicaTransportError(
                    "The recovery archive exceeds the 64 MiB transport limit.",
                    "REPLICA_ARCHIVE_TOO_LARGE",
                )
            with await asyncio.to_thread(path.open, "rb") as source:
                digest = await asyncio.to_thread(
                    lambda: hashlib.file_digest(source, "sha256").hexdigest()
                )
                source.seek(0)
                uploaded = await self._client.upload_file(
                    source, file_name=filename, file_size=size, part_size_kb=512
                )
            result = await self._client(
                functions.messages.SendMediaRequest(
                    peer=await self._client.get_input_entity(channel),
                    media=types.InputMediaUploadedDocument(
                        file=uploaded,
                        mime_type="application/octet-stream",
                        attributes=[types.DocumentAttributeFilename(filename)],
                        force_file=True,
                    ),
                    message=caption,
                    random_id=random_id,
                    silent=True,
                )
            )
            identity = await self._sent(channel, result, random_id)
            _, document = await self._get(channel, identity)
            if (
                self._capsule(document, filename) != size
                or await self._read_to(document, None, size) != digest
            ):
                raise ReplicaTransportError(
                    "The remote recovery archive checksum did not match. "
                    "The previous confirmed recovery point has been preserved.",
                    "REPLICA_VERIFY_FAILED",
                )
            if pin:
                await self._client(
                    functions.messages.UpdatePinnedMessageRequest(
                        peer=await self._client.get_input_entity(channel),
                        id=identity,
                        silent=True,
                    )
                )
            return identity

    async def _download(
        self,
        channel: int,
        message_id: int | None,
        destination: Path,
        *,
        filename: str,
    ) -> int:
        self._ready()
        channel, destination = _channel(channel), Path(destination)
        if message_id is not None:
            message_id = _message(message_id)
        async with self._lock, self._guard(180):
            await self.inspect(channel, writable=False)
            if message_id is None:
                peer = await self._client.get_input_entity(channel)
                full = await self._client(
                    functions.channels.GetFullChannelRequest(
                        utils.get_input_channel(peer)
                    )
                )
                message_id = getattr(full.full_chat, "pinned_msg_id", None)
                if message_id is None:
                    raise ReplicaTransportError(
                        "No recovery checkpoint is pinned. Enter a confirmed "
                        "TGDrive replica checkpoint message ID.",
                        "REPLICA_CHECKPOINT_MISSING",
                    )
                message_id = _message(message_id)
            _, document = await self._get(channel, message_id)
            size = self._capsule(document, filename)
            if destination.exists():
                raise ReplicaTransportError(
                    "The recovery destination file already exists. Preserve it "
                    "and choose a new temporary destination.",
                    "REPLICA_LOCAL_EXISTS",
                )
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = destination.with_name(
                destination.name + "." + secrets.token_hex(8)
            )
            try:
                with temporary.open("xb") as target:
                    temporary.chmod(0o600)
                    await self._read_to(document, target, size)
                    target.flush()
                    os.fsync(target.fileno())
                # Link creates the final name exclusively; a concurrent file is
                # preserved instead of overwritten by a recovery download.
                os.link(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return message_id

    async def download(
        self, channel: int, message_id: int | None, destination: Path
    ) -> int:
        """Download a confirmed replica capsule by ID or latest pinned message."""
        return await self._download(
            channel, message_id, destination, filename=CAPSULE_NAME
        )

    async def download_catalog(
        self, channel: int, destination: Path, message_id: int | None = None
    ) -> int:
        """Read a legacy SQLite drive.json pin without guessing or scanning.

        The caller must validate catalog format, drive identity and channel
        binding. Falling back from a capsule is only safe on DOCUMENT_TYPE,
        never on failed decryption, authorization or malformed archive errors.
        """
        return await self._download(
            channel, message_id, destination, filename="drive.json"
        )

    async def publish(
        self, channel: int, path: Path, *, random_id: int, caption: str, pin: bool
    ) -> int:
        return await self._publish(
            channel,
            path,
            random_id=random_id,
            caption=caption,
            pin=pin,
            filename=CAPSULE_NAME,
        )

    async def publish_catalog(
        self,
        channel: int,
        path: Path,
        *,
        random_id: int,
        caption: str,
        pin: bool = True,
    ) -> int:
        """Confirm and pin a normal SQLite drive.json checkpoint after recovery.

        The caller owns the validated portable catalog and its normal checkpoint
        marker. This does not replace the encrypted capsule retained elsewhere.
        """
        return await self._publish(
            channel,
            path,
            random_id=random_id,
            caption=caption,
            pin=pin,
            filename="drive.json",
        )
