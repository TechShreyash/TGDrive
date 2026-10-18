"""Supervised browser, URL, and Telegram transfer pipelines."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import UploadFile
from telethon.errors import FloodWaitError

from tgdrive.backup import BackupError
from tgdrive.client_pool import (
    ClientPool,
    ClientPoolNotRunning,
    NoClientAvailable,
    NoPremiumClientAvailable,
    current_upload_limit,
)
from tgdrive.config import Settings
from tgdrive.errors import (
    DriveError,
    NotDirectoryError,
    NotFoundError,
    TrashedItemError,
    ValidationError,
)
from tgdrive.remote import RemoteCancelled, RemoteClient, RemoteFileInfo, RemoteTooLarge
from tgdrive.security import safe_name
from tgdrive.store import DriveStore
from tgdrive.tasks import TransferRecord, TransferRegistry, TransferStatus

logger = logging.getLogger(__name__)
_CHUNK_SIZE = 1024 * 1024
_UPLOAD_CLEANUP_TIMEOUT = 1.0
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


class TransferError(RuntimeError):
    pass


class TransferTooLarge(TransferError):
    pass


class TransferEmptyFile(TransferError):
    pass


class TransferCancelled(TransferError):
    pass


class BackupPending(ValueError):
    """A catalogued upload must finish protection instead of deleting its media."""

    code = "BACKUP_PENDING"


def _media_from_message(message: Any) -> Any | None:
    for attribute in _MEDIA_ATTRIBUTES:
        media = getattr(message, attribute, None)
        if media is not None:
            return media
    return None


async def restore_telegram_cooldowns(pool: ClientPool, records) -> None:
    """Load every future provider wait before startup checkpoint publication."""
    now = time.time()
    account_deadlines: dict[str, float] = {}
    channel_deadline = 0.0

    def future(value: Any) -> bool:
        return type(value) in (int, float) and math.isfinite(value) and value > now

    for record in records:
        context = record.context
        deadline = context.get("channel_retry_at")
        if future(deadline):
            channel_deadline = max(channel_deadline, deadline)
        plan = context.get("multipart_plan")
        candidates = plan.get("parts", []) if isinstance(plan, dict) else [context]
        if not isinstance(candidates, list):
            continue
        for part in candidates:
            if not isinstance(part, dict):
                continue
            sender, deadline = part.get("sender"), part.get("retry_at")
            if isinstance(sender, str) and sender and future(deadline):
                account_deadlines[sender] = max(
                    account_deadlines.get(sender, 0),
                    deadline,
                )
    if channel_deadline:
        await pool.defer_channel(channel_deadline)
    for sender, deadline in account_deadlines.items():
        await pool.defer_client(sender, deadline)


class TransferManager:
    def __init__(
        self,
        *,
        settings: Settings,
        pool: ClientPool,
        store: DriveStore,
        remote: RemoteClient,
        registry: TransferRegistry,
        ensure_backup: Callable[[int], Awaitable[int]] | None = None,
        ensure_lineage: Callable[[], Awaitable[None]] | None = None,
        check_upload_allowed: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.store = store
        self.remote = remote
        self.registry = registry
        self._ensure_backup = ensure_backup
        self._ensure_lineage = ensure_lineage
        self._check_upload_allowed = check_upload_allowed
        self.cache_dir = settings.resolved_cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_transfers)

    def _resolve_upload_destination(self, record: TransferRecord, path: str) -> str:
        binding = self.store.upload_destination(
            path,
            directory_id=record.context.get("destination_id"),
            recover_legacy_identity="destination_id" not in record.context,
        )
        if (
            record.context.get("destination_id") != binding["id"]
            or record.context.get("destination") != binding["path"]
        ):
            record.context.update(
                destination_id=binding["id"], destination=binding["path"]
            )
            self.registry.persist(record)
        return binding["path"]

    def _check_destination_or_cancel(self, record: TransferRecord, path: str) -> str:
        try:
            return self._resolve_upload_destination(record, path)
        except (NotFoundError, NotDirectoryError, TrashedItemError, ValidationError):
            record.context["cancel_requested"] = True
            record.cancel_event.set()
            self.registry.persist(record)
            return path

    def _destination_path(self, record: TransferRecord) -> str:
        path = record.context.get("destination", "")
        identity = record.context.get("destination_id")
        if identity is None and isinstance(path, str) and path.startswith("/"):
            # Trusted old jobs already persisted an opaque final UUID. Resolve
            # ancestry even while trashed, so deletion waits for their cleanup.
            identity = path.rstrip("/").rsplit("/", 1)[-1] or "root"
        if identity is not None:
            try:
                return self.store.resolve_id(identity)
            except DriveError:
                pass
        return path

    async def receive_browser_upload(
        self,
        upload: UploadFile,
        destination: str,
        *,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        on_created: Callable[[TransferRecord], None] | None = None,
    ) -> TransferRecord:
        """Save an incoming multipart stream, then supervise Telegram upload."""

        binding = self.store.upload_destination(destination)
        destination = binding["path"]
        declared_size = getattr(upload, "size", None)
        admission_limit = await current_upload_limit(self.settings, self.pool)
        try:
            if self._check_upload_allowed is not None:
                self._check_upload_allowed()
            binding = self.store.upload_destination(
                destination, directory_id=binding["id"]
            )
            destination = binding["path"]
        except BaseException:
            await upload.close()
            raise
        if isinstance(declared_size, int) and declared_size > admission_limit:
            await upload.close()
            raise TransferTooLarge(
                f"file exceeds the {admission_limit}-byte upload limit"
            )
        if declared_size == 0:
            await upload.close()
            raise TransferEmptyFile("Empty files cannot be uploaded to Telegram")
        record = self.registry.create(
            "saving", total=declared_size if isinstance(declared_size, int) else 0
        )
        self.registry.update(record.id, status=TransferStatus.RUNNING)
        upload_dir = self.cache_dir / "uploads"
        local_path = upload_dir / f"{record.id}.upload"
        record.context.update(
            kind="receiving",
            local_path=str(local_path),
            destination=destination,
            destination_id=binding["id"],
        )
        self.registry.persist(record)
        written = 0
        try:
            if on_created is not None:
                on_created(record)
            if record.cancel_event.is_set():
                raise TransferCancelled("Transfer cancelled")
            file_name = safe_name(upload.filename)
            upload_dir.mkdir(parents=True, exist_ok=True)
            async with self._semaphore, aiofiles.open(local_path, "xb") as output:
                while True:
                    if record.cancel_event.is_set():
                        raise TransferCancelled("Transfer cancelled")
                    chunk = await upload.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > admission_limit:
                        raise TransferTooLarge(
                            f"file exceeds the {admission_limit}-byte upload limit"
                        )
                    await output.write(chunk)
                    self.registry.update(
                        record.id,
                        current=written,
                        total=max(record.total, written),
                    )
            if isinstance(declared_size, int) and written != declared_size:
                raise TransferError(
                    f"multipart stream ended at {written} of {declared_size} bytes"
                )
            if written == 0:
                raise TransferEmptyFile("Empty files cannot be uploaded to Telegram")
            # The browser can abort in the narrow window after its multipart
            # body was accepted but before it receives the transfer id.  Do
            # not start a Telegram job the UI can no longer identify.
            if is_disconnected is not None and await is_disconnected():
                raise TransferCancelled("Transfer cancelled")
            record.context.update(
                kind="upload",
                local_path=str(local_path),
                destination=destination,
                name=file_name,
                size=written,
            )
            self.registry.persist(record)
            self.registry.spawn(
                record.id,
                lambda transfer: self._upload_path(
                    transfer,
                    local_path,
                    destination,
                    file_name,
                    written,
                ),
            )
            return record
        except asyncio.CancelledError:
            # Client disconnects and server shutdown cancel the request task
            # with a BaseException, so the generic handler below cannot clean
            # this up.  Keep the registry and cache consistent before
            # preserving cancellation semantics for Starlette.
            self.registry.cancel(record.id)
            with suppress(FileNotFoundError, NotADirectoryError):
                local_path.unlink()
            raise
        except TransferCancelled:
            self.registry.cancel(record.id)
            with suppress(FileNotFoundError, NotADirectoryError):
                local_path.unlink()
            raise
        except Exception as exc:
            self.registry.update(
                record.id,
                status=TransferStatus.ERROR,
                error=str(exc) or type(exc).__name__,
            )
            with suppress(FileNotFoundError, NotADirectoryError):
                local_path.unlink()
            raise
        finally:
            await upload.close()

    async def inspect_remote(self, url: str) -> RemoteFileInfo:
        info = await self.remote.inspect(url)
        limit = await current_upload_limit(self.settings, self.pool)
        if info.size is not None and info.size > limit:
            raise RemoteTooLarge(limit, info.size)
        return info

    def start_remote(
        self,
        *,
        url: str,
        destination: str,
        filename: str | None,
    ) -> TransferRecord:
        if self._check_upload_allowed is not None:
            self._check_upload_allowed()
        binding = self.store.upload_destination(destination)
        destination = binding["path"]
        record = self.registry.create("downloading")
        record.context.update(
            kind="remote",
            source_kind="remote",
            url=url,
            destination=destination,
            destination_id=binding["id"],
            name=filename,
        )
        self.registry.persist(record)
        self.registry.spawn(
            record.id,
            lambda transfer: self._remote_pipeline(
                transfer,
                url=url,
                destination=destination,
                filename=safe_name(filename) if filename else None,
            ),
        )
        return record

    async def _remote_pipeline(
        self,
        record: TransferRecord,
        *,
        url: str,
        destination: str,
        filename: str | None,
    ) -> None:
        download_dir = self.cache_dir / "remote" / record.id
        try:
            destination = self._check_destination_or_cancel(record, destination)
            if record.cancel_event.is_set():
                raise TransferCancelled("Transfer destination is unavailable")
            async with self._semaphore:
                admission_limit = await current_upload_limit(self.settings, self.pool)
                self.registry.update(
                    record.id,
                    status=TransferStatus.RUNNING,
                    phase="downloading",
                )

                async def progress(current: int, total: int | None) -> None:
                    observed = max(current, total or 0)
                    if observed > admission_limit:
                        raise RemoteTooLarge(admission_limit, observed)
                    self.registry.update(
                        record.id,
                        current=current,
                        # Unknown source length stays indeterminate until the
                        # upload stage; growing the total per chunk resets ETA.
                        total=total or 0,
                    )

                result = await self.remote.download(
                    url,
                    download_dir,
                    filename=filename,
                    # This UUID directory belongs only to this unpublished URL job.
                    # A crash after atomic local publication may leave the old output.
                    # Retry from zero and atomically replace only that stage.
                    overwrite=True,
                    cancel_event=record.cancel_event,
                    progress=progress,
                )
            await self._upload_path(
                record,
                result.path,
                destination,
                result.filename,
                result.size,
            )
        except (RemoteCancelled, TransferCancelled):
            self.registry.update(
                record.id,
                status=TransferStatus.CANCELLED,
                error="Transfer cancelled",
            )
        finally:
            if record.status in {TransferStatus.COMPLETED, TransferStatus.CANCELLED}:
                shutil.rmtree(download_dir, ignore_errors=True)

    def enqueue_upload(
        self,
        local_path: Path,
        destination: str,
        file_name: str,
        size: int,
        *,
        transfer_id: str | None = None,
    ) -> TransferRecord:
        if self._check_upload_allowed is not None:
            self._check_upload_allowed()
        binding = self.store.upload_destination(destination)
        destination = binding["path"]
        record = self.registry.create("queued", total=size, transfer_id=transfer_id)
        if record.task is not None or record.terminal:
            return record
        record.context.update(
            kind="upload",
            local_path=str(local_path),
            destination=destination,
            destination_id=binding["id"],
            name=file_name,
            size=size,
        )
        self.registry.persist(record)
        self.registry.spawn(
            record.id,
            lambda item: self._upload_path(
                item, local_path, destination, file_name, size
            ),
        )
        return record

    async def _verify_lineage(self, record: TransferRecord | None = None) -> None:
        if self._ensure_lineage is None:
            return
        while True:
            if record is not None:
                context = record.context
                remaining = context.get("lineage_retry_at", 0) - time.time()
                if remaining > 0:
                    self.registry.update(record.id, phase="retry_wait")
                    if not context.get("send_started"):
                        try:
                            await asyncio.wait_for(
                                record.cancel_event.wait(), remaining
                            )
                        except TimeoutError:
                            pass
                        else:
                            raise TransferCancelled("Transfer cancelled")
                    else:
                        await asyncio.sleep(remaining)
            try:
                await self._ensure_lineage()
            except BackupError as exc:
                delay = exc.retry_after
                if (
                    record is None
                    or getattr(exc, "code", None) == "RECOVERY_REQUIRED"
                    or type(delay) not in (int, float)
                    or not math.isfinite(delay)
                    or delay <= 0
                    or not math.isfinite(time.time() + delay)
                ):
                    raise
                record.context["lineage_retry_at"] = time.time() + delay
                self.registry.update(record.id, phase="retry_wait")
                continue
            if record is not None and record.context.pop("lineage_retry_at", None):
                self.registry.persist(record)
            return

    async def recover(self) -> None:
        # Failed rows do not spawn automatically, but their provider cooldowns
        # still constrain every new upload after a process restart.
        await restore_telegram_cooldowns(self.pool, self.registry.list())
        intents = self.store.pending_deletions()
        if intents or any(not row.terminal for row in self.registry.list()):
            await self._verify_lineage()
        for intent in intents:
            try:
                self.registry.get(intent["id"])
            except KeyError:
                self.start_deletion(intent["path"])
        for record in self.registry.list():
            if not record.terminal and record.task is None:
                self._resume(record)

    def cancel_job(self, job_id: str) -> TransferRecord:
        record = self.registry.get(job_id)
        if record.status not in {
            TransferStatus.COMPLETED,
            TransferStatus.CANCELLED,
        } and self._metadata_committed(record):
            raise BackupPending(
                "This file is already catalogued. Allow finalization to finish, "
                "or retry this transfer instead of cancelling it."
            )
        record = self.registry.cancel(job_id)
        if (
            record.context.get("send_started")
            and (record.task is None or record.task.done())
            and not record.terminal
        ):
            self._resume(record)
        if record.status is TransferStatus.CANCELLED and not record.context.get(
            "send_started"
        ):
            staged = record.context.get("local_path")
            if staged:
                target = Path(staged).resolve()
                if target.is_relative_to(self.cache_dir):
                    target.unlink(missing_ok=True)
            if hasattr(self, "uploads"):
                self.uploads.release(record.id)
        return record

    def retry_job(self, job_id: str) -> TransferRecord:
        record = self.registry.get(job_id)
        if not record.terminal and record.task is not None and not record.task.done():
            return record
        record = self.registry.retry(job_id)
        if record.context.get("cancel_requested"):
            record.cancel_event.set()
        self._resume(record)
        return record

    def _resume(self, record: TransferRecord) -> None:
        context = record.context
        if self._metadata_committed(record):
            self.registry.spawn(
                record.id,
                lambda item: self._upload_path(
                    item,
                    Path(context.get("local_path", self.cache_dir / record.id)),
                    context.get("destination", "/"),
                    context.get("name", "upload"),
                    context.get("size", record.total),
                ),
            )
        elif context.get("kind") == "delete":
            self.registry.spawn(record.id, lambda item: self._delete_intent(item))
        elif context.get("kind") == "upload" or (
            context.get("source_kind") == "remote"
            and context.get("local_path")
            and context.get("size")
        ):
            self.registry.spawn(
                record.id,
                lambda item: self._upload_path(
                    item,
                    Path(context["local_path"]),
                    context["destination"],
                    context["name"],
                    context["size"],
                ),
            )
        elif context.get("kind") == "remote":
            self.registry.spawn(
                record.id,
                lambda item: self._remote_pipeline(
                    item,
                    url=context["url"],
                    destination=context["destination"],
                    filename=context.get("name"),
                ),
            )
        else:
            self.registry.update(
                record.id,
                status=TransferStatus.ERROR,
                error="Interrupted browser receipt. Reselect the file to resume.",
            )

    def _metadata_committed(self, record: TransferRecord) -> bool:
        return bool(
            record.context.get("metadata_committed")
            or self.store.has_file_operation(record.id)
        )

    async def _protect_committed_upload(
        self, record: TransferRecord, size: int
    ) -> None:
        """Finish only the durable catalog checkpoint, never another media send."""
        context = record.context
        context["metadata_committed"] = True
        context.setdefault("catalog_revision", self.store.revision)
        required = context["catalog_revision"]
        if type(required) is not int or required < 0:
            raise TransferError("The committed catalog revision is invalid")
        # Cancellation cannot delete a message that already belongs to the catalog.
        context.pop("cancel_requested", None)
        record.cancel_event.clear()
        if record.cancel_handle is not None:
            record.cancel_handle.cancel()
            record.cancel_handle = None
        self.registry.persist(record)
        self.registry.update(
            record.id,
            status=TransferStatus.RUNNING,
            phase=(
                "finalizing"
                if getattr(self.store, "backend_name", None) == "mongodb"
                else "backup_pending"
            ),
            current=size,
            total=size,
        )
        while self._ensure_backup is not None:
            retry_at = context.get("backup_retry_at", 0)
            if retry_at > time.time():
                await asyncio.sleep(retry_at - time.time())
            try:
                protected = await self._ensure_backup(required)
            except BackupError as exc:
                delay = getattr(exc, "retry_after", None)
                if (
                    type(delay) not in (int, float)
                    or not math.isfinite(delay)
                    or delay <= 0
                    or not math.isfinite(time.time() + delay)
                ):
                    raise
                context["backup_retry_at"] = time.time() + delay
                self.registry.persist(record)
                continue
            if type(protected) is not int or protected < required:
                raise TransferError(
                    "The recovery checkpoint did not confirm this catalog revision"
                )
            context["protected_revision"] = protected
            context.pop("backup_retry_at", None)
            self.registry.persist(record)
            break
        self.registry.update(
            record.id,
            status=TransferStatus.COMPLETED,
            phase="completed",
            current=size,
            total=size,
        )

    async def _upload_path(
        self,
        record: TransferRecord,
        local_path: Path,
        destination: str,
        file_name: str,
        size: int,
    ) -> None:
        # Old journals may have an ambiguous one-message effect but no part hashes.
        # Preserve their original sender/random_id rather than inventing a new plan.
        context = record.context
        if self._metadata_committed(record) or (
            "multipart_plan" not in context
            and (context.get("send_started") or context.get("remote_message_id"))
        ):
            await self._upload_single_path(
                record, local_path, destination, file_name, size
            )
            return
        from tgdrive.multipart_transfer import upload_multipart

        await upload_multipart(self, record, local_path, destination, file_name, size)

    async def _upload_single_path(
        self,
        record: TransferRecord,
        local_path: Path,
        destination: str,
        file_name: str,
        size: int,
    ) -> None:
        context = record.context
        context.update(
            kind="upload",
            local_path=str(local_path),
            destination=destination,
            name=file_name,
            size=size,
        )
        context.setdefault("random_id", secrets.randbits(63))
        self.registry.persist(record)
        cleanup = False
        try:
            if self._metadata_committed(record):
                await self._protect_committed_upload(record, size)
                cleanup = True
                return
            if self._check_upload_allowed is not None:
                self._check_upload_allowed()
            else:
                await self._verify_lineage(record)
            if size == 0:
                raise TransferEmptyFile("Empty files cannot be uploaded to Telegram")
            destination = self._check_destination_or_cancel(record, destination)
            premium = size > self.settings.bot_upload_limit
            if context.get("sender") and context.get("retry_at", 0) > time.time():
                await self.pool.defer_client(context["sender"], context["retry_at"])
            if context.get("channel_retry_at", 0) > time.time():
                await self.pool.defer_channel(context["channel_retry_at"])
            self.registry.update(
                record.id, phase="queued", status=TransferStatus.QUEUED
            )
            while True:
                retry_at = context.get("retry_at", 0)
                if retry_at > time.time():
                    self.registry.update(record.id, phase="retry_wait")
                    await asyncio.sleep(retry_at - time.time())
                try:
                    async with (
                        self.pool.lease(
                            premium=premium,
                            client_name=context.get("sender"),
                            media=True,
                            request_id=record.id,
                        ) as client,
                    ):
                        context.setdefault("sender", client.name)
                        self.registry.persist(record)
                        if record.cancel_event.is_set() and not context.get(
                            "send_started"
                        ):
                            raise TransferCancelled("Transfer cancelled")

                        async def progress(current: int, total: int) -> None:
                            if record.cancel_event.is_set() and not context.get(
                                "send_started"
                            ):
                                raise TransferCancelled("Transfer cancelled")
                            self.registry.update(
                                record.id,
                                phase="finalizing" if current >= total else "uploading",
                                current=current,
                                total=total,
                            )

                        self.registry.update(
                            record.id,
                            status=TransferStatus.RUNNING,
                            phase="uploading",
                            current=0,
                            total=size,
                        )
                        if context.get("remote_message_id"):
                            message = await client.get_messages(
                                self.settings.storage_channel,
                                message_ids=context["remote_message_id"],
                            )
                        else:
                            if not await asyncio.to_thread(local_path.is_file):
                                raise TransferError(
                                    "Staged file missing. Reselect the source."
                                )

                            def fingerprint():
                                checksum = hashlib.sha256()
                                length = 0
                                with local_path.open("rb") as source:
                                    while chunk := source.read(1024 * 1024):
                                        checksum.update(chunk)
                                        length += len(chunk)
                                return length, checksum.hexdigest()

                            length, checksum = await asyncio.to_thread(fingerprint)
                            if length != size or (
                                context.get("sha256") and checksum != context["sha256"]
                            ):
                                raise TransferError(
                                    "Staged file changed. Reselect the source."
                                )
                            context["sha256"] = checksum
                            # Persist identity before dispatch and ambiguous retries.
                            context["send_started"] = True
                            self.registry.persist(record)
                            if hasattr(client, "send_staged"):
                                message = await client.send_staged(
                                    self.settings.storage_channel,
                                    local_path,
                                    file_name=file_name,
                                    random_id=context["random_id"],
                                    progress=progress,
                                )
                            else:
                                with local_path.open("rb") as document:
                                    message = await client.send_document(
                                        self.settings.storage_channel,
                                        document,
                                        file_name=file_name,
                                        force_document=True,
                                        disable_notification=True,
                                        progress=progress,
                                        random_id=context["random_id"],
                                    )
                        if message is None:
                            raise TransferError(
                                "Telegram did not confirm the storage message"
                            )
                        context["remote_message_id"] = message.id
                        self.registry.persist(record)
                        destination = self._check_destination_or_cancel(
                            record, destination
                        )
                        if record.cancel_event.is_set() or context.get(
                            "cancel_requested"
                        ):
                            await self._verify_lineage(record)
                            await client.delete_messages(
                                self.settings.storage_channel, message.id
                            )
                            cleanup = True
                            raise TransferCancelled("Transfer cancelled")
                        media = _media_from_message(message)
                        if media is None:
                            raise TransferError("Telegram upload returned no media")
                        actual_size = getattr(media, "file_size", size) or size
                        self.registry.update(record.id, phase="finalizing")
                        await asyncio.to_thread(
                            self.store.add_file,
                            destination,
                            file_name,
                            message.id,
                            actual_size,
                            operation_id=record.id,
                            keep_both=True,
                        )
                        context["metadata_committed"] = True
                        context["catalog_revision"] = self.store.revision
                        self.registry.persist(record)
                        self.registry.update(
                            record.id,
                            phase="backup_pending",
                            current=actual_size,
                            total=actual_size,
                        )
                        break
                except (ClientPoolNotRunning, NoClientAvailable) as exc:
                    if isinstance(exc, NoPremiumClientAvailable):
                        raise
                    if record.cancel_event.is_set() and not context.get("send_started"):
                        raise TransferCancelled("Transfer cancelled") from exc
                    self.registry.update(record.id, phase="waiting_for_telegram")
                    await asyncio.sleep(5)
                except FloodWaitError as exc:
                    deadline = time.time() + exc.seconds + 0.25
                    context["retry_at"] = deadline
                    if getattr(exc, "tgdrive_channel_flood", False):
                        context["channel_retry_at"] = deadline
                    self.registry.persist(record)
                    if getattr(exc, "tgdrive_channel_flood", False):
                        await self.pool.defer_channel(deadline)
                    await self.pool.defer_client(context["sender"], deadline)
                    self.registry.update(record.id, phase="retry_wait")
            # Checkpoint work uses its own backup lock/client lease. The ordinary
            # Telegram upload semaphore and media lease have both been released.
            await self._protect_committed_upload(record, actual_size)
            cleanup = True
        except TransferCancelled:
            record.status = TransferStatus.CANCELLED
            record.phase = "cancelled"
            record.error = "Transfer cancelled"
            self.registry.persist(record)
            cleanup = True
        finally:
            if cleanup:
                await asyncio.to_thread(local_path.unlink, missing_ok=True)
                uploads = getattr(self, "uploads", None)
                if uploads is not None:
                    uploads.release(record.id)

    def start_deletion(self, path: str) -> TransferRecord:
        intent = self.store.begin_deletion(
            path, channel_id=self.settings.storage_channel
        )
        record = self.registry.create(
            "deleting", total=len(intent["message_ids"]), transfer_id=intent["id"]
        )
        record.context.update(
            kind="delete", deletion_id=intent["id"], name=path.rsplit("/", 1)[-1]
        )
        self.registry.persist(record)
        if not record.terminal and (record.task is None or record.task.done()):
            self.registry.spawn(record.id, lambda item: self._delete_intent(item))
        return record

    async def _delete_intent(self, record: TransferRecord) -> None:
        intent = next(
            (
                item
                for item in self.store.pending_deletions()
                if item["id"] == record.context["deletion_id"]
            ),
            None,
        )
        if intent is None:
            self.registry.update(
                record.id, status=TransferStatus.COMPLETED, phase="completed"
            )
            return
        try:
            await self._verify_lineage(record)
            self.registry.update(
                record.id, status=TransferStatus.RUNNING, phase="deleting"
            )
            prefix = intent["path"].rstrip("/")
            for other in self.registry.list():
                destination = self._destination_path(other)
                if other.id == record.id or not (
                    destination == prefix or destination.startswith(prefix + "/")
                ):
                    continue
                if (
                    other.status is TransferStatus.COMPLETED
                    or other.status is TransferStatus.CANCELLED
                ):
                    continue
                self.cancel_job(other.id)
                if other.task is not None:
                    await asyncio.wait_for(asyncio.shield(other.task), timeout=60)
                if other.status is not TransferStatus.CANCELLED:
                    raise TransferError(
                        "An upload into this folder still needs Telegram cleanup"
                    )
            channel = intent["channel_id"] or self.settings.storage_channel
            async with self.pool.lease() as client:
                pending = intent["pending_message_ids"]
                for start in range(0, len(pending), 100):
                    batch = pending[start : start + 100]
                    await self._verify_lineage(record)
                    await client.delete_messages(channel, batch)
                    if callable(getattr(client, "get_messages", None)):
                        observed = await client.get_messages(channel, message_ids=batch)
                        if not isinstance(observed, list):
                            observed = [observed]
                        if any(
                            item is not None and not getattr(item, "empty", False)
                            for item in observed
                        ):
                            raise TransferError(
                                "Telegram deletion confirmation is incomplete"
                            )
                    await asyncio.to_thread(
                        self.store.confirm_deletion_messages, intent["id"], batch
                    )
                    self.registry.update(
                        record.id, current=min(record.total, start + len(batch))
                    )
            await asyncio.to_thread(self.store.finish_deletion, intent["id"])
            self.registry.update(
                record.id,
                status=TransferStatus.COMPLETED,
                phase="completed",
                current=record.total,
            )
        except Exception as exc:
            await asyncio.to_thread(
                self.store.fail_deletion, intent["id"], type(exc).__name__
            )
            raise

    async def delete_telegram_messages(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        async with self.pool.lease() as client:
            for start in range(0, len(message_ids), 100):
                await self._verify_lineage()
                await client.delete_messages(
                    self.settings.storage_channel,
                    message_ids[start : start + 100],
                )

    async def close(self) -> None:
        await self.registry.shutdown()
        await self.remote.close()
