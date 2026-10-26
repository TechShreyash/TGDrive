"""Supervised browser, URL, and Telegram transfer pipelines."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import UploadFile

from tgdrive.core.config import Settings
from tgdrive.core.errors import (
    DriveError,
    NotDirectoryError,
    NotFoundError,
    TrashedItemError,
    ValidationError,
)
from tgdrive.core.security import safe_name
from tgdrive.recovery.backup import BackupError
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool, current_upload_limit
from tgdrive.transfers.remote import (
    REMOTE_MAX_ATTEMPTS,
    RemoteCancelled,
    RemoteClient,
    RemoteFileInfo,
    RemoteTooLarge,
)
from tgdrive.transfers.staging import StorageUnavailable, check_staging
from tgdrive.transfers.tasks import (
    TransferRecord,
    TransferRegistry,
    TransferStatus,
    _journalled,
)

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


async def restore_telegram_cooldowns(
    pool: ClientPool, records: Iterable[TransferRecord]
) -> None:
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
    """Coordinate staging, Telegram work, and protection of catalogued uploads.

    The registry owns durable job state and supervised tasks. This manager keeps
    those records aligned with staged bytes, destination identities, and backups.
    """

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
        ensure_channel: Callable[[], Awaitable[None]] | None = None,
        check_upload_allowed: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.store = store
        self.remote = remote
        self.registry = registry
        self.registry.demo_mode = bool(getattr(settings, "demo_mode", False))
        self._ensure_backup = ensure_backup
        self._ensure_lineage = ensure_lineage
        self._ensure_channel = ensure_channel
        self._check_upload_allowed = check_upload_allowed
        self.cache_dir = settings.resolved_cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_transfers)
        self._pending_staging: dict[str, int] = {}

    async def _resolve_upload_destination(
        self, record: TransferRecord, path: str
    ) -> str:
        binding = self.store.upload_destination(
            path,
            directory_id=record.context.get("destination_id"),
        )
        if (
            record.context.get("destination_id") != binding["id"]
            or record.context.get("destination") != binding["path"]
        ):
            record.context.update(
                destination_id=binding["id"], destination=binding["path"]
            )
            await self.registry.persist(record)
        return binding["path"]

    async def _check_destination_or_cancel(
        self, record: TransferRecord, path: str
    ) -> str:
        try:
            return await self._resolve_upload_destination(record, path)
        except (NotFoundError, NotDirectoryError, TrashedItemError, ValidationError):
            record.context["cancel_requested"] = True
            record.cancel_event.set()
            await self.registry.persist(record)
            return path

    def _destination_path(self, record: TransferRecord) -> str:
        path = record.context.get("destination", "")
        identity = record.context.get("destination_id")
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
        on_created: Callable[[TransferRecord], Awaitable[None] | None] | None = None,
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
        try:
            check_staging(self.settings, self, declared_size or admission_limit)
        except BaseException:
            await upload.close()
            raise
        transfer_id = secrets.token_urlsafe(18)
        upload_dir = self.cache_dir / "uploads"
        local_path = upload_dir / f"{transfer_id}.upload"
        context = dict(
            kind="receiving",
            staging_size=declared_size or admission_limit,
            staging_written=0,
            local_path=str(local_path),
            destination=destination,
            destination_id=binding["id"],
        )
        self._pending_staging[transfer_id] = declared_size or admission_limit
        try:
            record = await self.registry.create(
                "saving",
                total=declared_size if isinstance(declared_size, int) else 0,
                transfer_id=transfer_id,
                context=context,
            )
        except BaseException:
            try:
                record = self.registry.get(transfer_id)
            except KeyError:
                pass
            else:
                await self.registry.cancel(record.id)
            await upload.close()
            raise
        finally:
            self._pending_staging.pop(transfer_id, None)
        written = 0
        try:
            await self.registry.update(record.id, status=TransferStatus.RUNNING)
            if on_created is not None:
                created = on_created(record)
                if inspect.isawaitable(created):
                    await created
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
                    check_staging(
                        self.settings,
                        self,
                        record.context["staging_size"],
                        exclude=record.id,
                        written=written - len(chunk),
                    )
                    await output.write(chunk)
                    record.context["staging_written"] = written
                    await self.registry.update(
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
            await self.registry.persist(record)
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
            await self.registry.cancel(record.id)
            with suppress(FileNotFoundError, NotADirectoryError):
                local_path.unlink()
            raise
        except TransferCancelled:
            await self.registry.cancel(record.id)
            with suppress(FileNotFoundError, NotADirectoryError):
                local_path.unlink()
            raise
        except Exception as exc:
            await self.registry.update(
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

    @_journalled
    async def start_remote(
        self,
        *,
        url: str,
        destination: str,
        filename: str | None,
        single_threaded: bool = False,
    ) -> TransferRecord:
        if self._check_upload_allowed is not None:
            self._check_upload_allowed()
        binding = self.store.upload_destination(destination)
        destination = binding["path"]
        record = await self.registry.create(
            "checking_source",
            context=dict(
                kind="remote",
                source_kind="remote",
                url=url,
                destination=destination,
                destination_id=binding["id"],
                name=filename,
                single_threaded=single_threaded,
            ),
        )
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
            destination = await self._check_destination_or_cancel(record, destination)
            if record.cancel_event.is_set():
                raise TransferCancelled("Transfer destination is unavailable")
            from tgdrive.transfers.remote_scheduler import WARM_STAGING_WINDOW

            admission_limit = await current_upload_limit(self.settings, self.pool)
            if self._check_upload_allowed is not None:
                self._check_upload_allowed()
            retry_not_before = record.context.get("source_retry_at", 0)
            record.context["source_attempt"] = 1
            await self.registry.update(
                record.id,
                status=TransferStatus.RUNNING,
                phase="source_retry_wait"
                if retry_not_before > time.time()
                else "checking_source",
            )
            admitted = False
            source_size = None
            mode = "full"

            async def mode_changed(value):
                nonlocal mode
                mode = value
                record.context["download_mode"] = value
                if value == "queued" and record.phase == "source_retry_wait":
                    record.context.pop("source_retry_at", None)
                    await self.registry.update(
                        record.id, phase="checking_source", error=None
                    )

            async def retry_scheduled(attempt: int, delay: float, error: Exception):
                nonlocal admitted, source_size
                admitted, source_size = False, None
                # The remote client has drained writers and removed its partial
                # file. Release the old claim before other jobs seek admission.
                record.context.update(
                    staging_size=0,
                    staging_written=0,
                    source_attempt=attempt,
                    source_retry_at=time.time() + delay,
                )
                record.context.pop("download_mode", None)
                await self.registry.update(
                    record.id,
                    phase="source_retry_wait",
                    current=0,
                    total=0,
                    error=None,
                )
                scheduler = getattr(self.remote, "scheduler", None)
                if scheduler is not None:
                    await scheduler.refresh()
                logger.info(
                    "Source retry scheduled id=%s attempt=%s/%s "
                    "error_type=%s retry_after_seconds=%.3f",
                    record.id,
                    attempt,
                    REMOTE_MAX_ATTEMPTS,
                    type(error).__name__,
                    delay,
                )

            def can_promote():
                if source_size is None:
                    return False
                if record.context.get("staging_size", 0) >= source_size:
                    return True
                try:
                    check_staging(
                        self.settings,
                        self,
                        source_size,
                        exclude=record.id,
                        written=record.context.get("staging_written", 0),
                    )
                except StorageUnavailable:
                    return False
                # Claim atomically on this event loop before another candidate
                # is promoted. Its next progress/lifecycle update journals it.
                record.context["staging_size"] = source_size
                return True

            async def progress(current: int, total: int | None) -> None:
                nonlocal admitted, source_size
                observed = max(current, total or 0)
                if observed > admission_limit:
                    raise RemoteTooLarge(admission_limit, observed)
                source_size = total if total is not None else admission_limit
                record.context["staging_written"] = current
                if mode == "full":
                    size = source_size
                else:
                    # Waiting sources reserve bounded windows, rather than
                    # taking the full staging budget away from active imports.
                    headroom = current + getattr(self.remote, "chunk_size", _CHUNK_SIZE)
                    windows = (
                        headroom + WARM_STAGING_WINDOW - 1
                    ) // WARM_STAGING_WINDOW
                    size = min(source_size, max(1, windows) * WARM_STAGING_WINDOW)
                size = max(size, record.context.get("staging_size", 0))
                reservation_changed = record.context.get("staging_size") != size
                refresh_needed = not admitted or reservation_changed
                if not admitted or reservation_changed:
                    while True:
                        try:
                            check_staging(
                                self.settings,
                                self,
                                size,
                                exclude=record.id,
                                written=current,
                            )
                            break
                        except StorageUnavailable:
                            record.context["download_mode"] = "waiting_storage"
                            await self.remote._cancelable(
                                asyncio.sleep(1), record.cancel_event
                            )
                    admitted = True
                record.context.update(staging_size=size, download_mode=mode)
                scheduler = getattr(self.remote, "scheduler", None)
                if scheduler is not None and refresh_needed:
                    await scheduler.refresh()
                await self.registry.update(
                    record.id,
                    current=current,
                    phase="downloading",
                    total=total or 0,
                    checkpoint_progress=False,
                )

            result = await self.remote.download(
                url,
                download_dir,
                filename=filename,
                # This UUID directory belongs only to this unpublished URL job.
                overwrite=True,
                cancel_event=record.cancel_event,
                progress=progress,
                max_bytes=admission_limit,
                single_threaded=bool(record.context.get("single_threaded", False)),
                can_promote=can_promote,
                mode_changed=mode_changed,
                retry=retry_scheduled,
                retry_not_before=retry_not_before,
            )
            record.context.pop("source_retry_at", None)
            await self._upload_path(
                record,
                result.path,
                destination,
                result.filename,
                result.size,
            )
        except (RemoteCancelled, TransferCancelled):
            await self.registry.update(
                record.id,
                status=TransferStatus.CANCELLED,
                error="Transfer cancelled",
            )
        finally:
            if record.status in {TransferStatus.COMPLETED, TransferStatus.CANCELLED}:
                shutil.rmtree(download_dir, ignore_errors=True)

    @_journalled
    async def enqueue_upload(
        self,
        local_path: Path,
        destination: str,
        file_name: str,
        size: int,
        *,
        transfer_id: str | None = None,
        folder_upload: dict[str, Any] | None = None,
    ) -> TransferRecord:
        if self._check_upload_allowed is not None:
            self._check_upload_allowed()
        binding = self.store.upload_destination(destination)
        destination = binding["path"]
        context = dict(
            kind="upload",
            local_path=str(local_path),
            destination=destination,
            destination_id=binding["id"],
            name=file_name,
            size=size,
        )
        if folder_upload is not None:
            context["folder_upload"] = dict(folder_upload)
        record = await self.registry.create(
            "queued", total=size, transfer_id=transfer_id, context=context
        )
        if record.task is not None or record.terminal:
            return record
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
                    await self.registry.update(record.id, phase="retry_wait")
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
                await self.registry.update(record.id, phase="retry_wait")
                continue
            if record is not None and record.context.pop("lineage_retry_at", None):
                await self.registry.persist(record)
            return

    async def recover(self) -> None:
        # Failed rows do not spawn automatically, but their provider cooldowns
        # still constrain every new upload after a process restart.
        await restore_telegram_cooldowns(self.pool, self.registry.list())
        intents = self.store.pending_deletions()
        restart_jobs = [
            row
            for row in self.registry.list()
            if row.status is TransferStatus.ERROR
            and row.context.get("restart_retryable") is True
            and row.context.get("kind") in {"upload", "remote", "delete"}
            and not row.context.get("cancel_requested")
        ]
        if (
            intents
            or restart_jobs
            or any(not row.terminal for row in self.registry.list())
        ):
            await self._verify_lineage()
        for record in restart_jobs:
            await self.registry.retry(record.id)
        for intent in intents:
            try:
                self.registry.get(intent["id"])
            except KeyError:
                await self.start_deletion(intent["path"])
        for record in self.registry.list():
            if not record.terminal and (record.task is None or record.task.done()):
                await self._resume(record)

    @_journalled
    async def cancel_job(self, job_id: str) -> TransferRecord:
        record = self.registry.get(job_id)
        if record.status not in {
            TransferStatus.COMPLETED,
            TransferStatus.CANCELLED,
        } and self._metadata_committed(record):
            raise BackupPending(
                "This file is already catalogued. Allow finalization to finish, "
                "or retry this transfer instead of cancelling it."
            )
        record = await self.registry.cancel(job_id)
        if (
            record.context.get("send_started")
            and (record.task is None or record.task.done())
            and not record.terminal
        ):
            await self._resume(record)
        if record.status is TransferStatus.CANCELLED and not record.context.get(
            "send_started"
        ):
            staged = record.context.get("local_path")
            if staged:

                def discard():
                    target = Path(staged).resolve()
                    if target.is_relative_to(self.cache_dir):
                        target.unlink(missing_ok=True)

                await asyncio.to_thread(discard)
            if hasattr(self, "uploads"):
                await asyncio.to_thread(self.uploads.release, record.id)
        return record

    @_journalled
    async def retry_job(self, job_id: str) -> TransferRecord:
        record = self.registry.get(job_id)
        if not record.terminal and record.task is not None and not record.task.done():
            return record
        record = await self.registry.retry(job_id)
        if record.context.get("cancel_requested"):
            record.cancel_event.set()
        await self._resume(record)
        return record

    async def _resume(self, record: TransferRecord) -> None:
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
            await self.registry.update(
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
        await self.registry.persist(record)
        await self.registry.update(
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
                await self.registry.persist(record)
                continue
            if type(protected) is not int or protected < required:
                raise TransferError(
                    "The recovery checkpoint did not confirm this catalog revision"
                )
            context["protected_revision"] = protected
            context.pop("backup_retry_at", None)
            await self.registry.persist(record)
            break
        await self.registry.update(
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
        if self._metadata_committed(record):
            await self._protect_committed_upload(record, size)
            await asyncio.to_thread(local_path.unlink, missing_ok=True)
            uploads = getattr(self, "uploads", None)
            if uploads is not None:
                await asyncio.to_thread(uploads.release, record.id)
            return
        context = record.context
        if "multipart_plan" not in context and (
            context.get("send_started") or context.get("remote_message_id")
        ):
            raise TransferError(
                "A dispatched upload is missing its part journal. "
                "Preserve the staged file and inspect the transfer before retrying."
            )
        from tgdrive.transfers.multipart_transfer import upload_multipart

        await upload_multipart(self, record, local_path, destination, file_name, size)

    @_journalled
    async def start_deletion(self, path: str) -> TransferRecord:
        intent = await asyncio.to_thread(
            self.store.begin_deletion, path, channel_id=self.settings.storage_channel
        )
        record = await self.registry.create(
            "deleting",
            total=len(intent["message_ids"]),
            transfer_id=intent["id"],
            context=dict(
                kind="delete", deletion_id=intent["id"], name=path.rsplit("/", 1)[-1]
            ),
        )
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
            await self.registry.update(
                record.id, status=TransferStatus.COMPLETED, phase="completed"
            )
            return
        try:
            await self._verify_lineage(record)
            protect = getattr(self, "_ensure_replica", None)
            if protect is not None:
                await self.registry.update(record.id, phase="checking_backup")
                await protect()
            await self.registry.update(
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
                await self.cancel_job(other.id)
                if other.task is not None:
                    await self.registry.update(record.id, phase="waiting_for_uploads")
                    await asyncio.wait_for(asyncio.shield(other.task), timeout=60)
                if other.status is not TransferStatus.CANCELLED:
                    raise TransferError(
                        "An upload into this folder still needs Telegram cleanup"
                    )
            # Permanent deletion forgets the catalog subtree, not its Telegram
            # documents. This policy applies consistently to every file age.
            await self._verify_lineage(record)
            await self.registry.update(record.id, phase="deleting")
            await asyncio.to_thread(self.store.finish_deletion, intent["id"])
            await self.registry.update(
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
