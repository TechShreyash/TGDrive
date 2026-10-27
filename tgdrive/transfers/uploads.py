"""Durable browser receipts with bounded staging and verified resumable chunks."""

from __future__ import annotations

import asyncio
import copy
import errno
import hashlib
import logging
import os
import re
import secrets
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from tgdrive.core.errors import DriveError
from tgdrive.core.security import safe_name
from tgdrive.storage.store import ROOT_ID, _normalize_path
from tgdrive.transfers.staging import StorageUnavailable, check_staging
from tgdrive.transfers.tasks import _journalled
from tgdrive.web.schemas import FolderUploadMetadata

logger = logging.getLogger(__name__)

CHUNK_SIZE = 8 * 1024 * 1024
_ACTIVE = {"receiving", "queued", "cancelling"}
_ID = re.compile(r"[0-9a-f]{32}")


class UploadError(ValueError):
    def __init__(
        self, message: str, status: int = 409, *, code: str = "UPLOAD_ERROR"
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class ResumableUploads:
    def __init__(self, settings: Any, manager: Any) -> None:
        self.settings = settings
        self.manager = manager
        self.root = settings.resolved_cache_dir / "receipts"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings.resolved_data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        from tgdrive.storage.mongo import for_settings

        backend = for_settings(settings)
        self._mongo_backend = backend
        self._mongo = backend.db.receipts
        self._items: dict[str, dict[str, Any]] = {}
        self._terminal_expiry: dict[str, float] = {}
        for row in self._mongo.find({}):
            self._items[row["_id"]] = row["data"]
            if "purge_at" in row:
                self._terminal_expiry[row["_id"]] = row["purge_at"].timestamp()
        # Held contexts and waiting tasks own locks; idle entries retain no lock.
        self.locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self.slots = asyncio.Semaphore(settings.max_concurrent_transfers)
        self._creation_lock = asyncio.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._reconcile_finished()

    def close(self) -> None:
        """The shared MongoDB backend owns its connection lifecycle."""

    def _all(self):
        self._prune_terminal()
        # Pending receipts reserve space before their first worker await,
        # including for URL/multipart admissions sharing the same budget.
        items = dict(self._pending)
        items.update(self._items)
        return copy.deepcopy(list(items.values()))

    def _prune_terminal(self, upload_id: str | None = None) -> None:
        """Expire only settled cached receipts, using their durable TTL deadline."""
        now = time.time()
        if upload_id is None:
            candidates = tuple(self._terminal_expiry.items())
        else:
            deadline = self._terminal_expiry.get(upload_id)
            candidates = () if deadline is None else ((upload_id, deadline),)
        for upload_id, deadline in candidates:
            if (
                deadline > now
                or upload_id in self._pending
                or self.locks.get(upload_id) is not None
                or self._items[upload_id]["state"] not in {"finished", "cancelled"}
            ):
                continue
            record = self._existing_job(upload_id)
            if record is not None and (
                str(record.status) not in {"completed", "cancelled"}
                or (record.task is not None and not record.task.done())
            ):
                continue
            del self._items[upload_id]
            del self._terminal_expiry[upload_id]

    def _save(self, item: dict[str, Any]) -> None:
        from datetime import UTC, datetime

        document = {"_id": item["id"], "data": item}
        if item["state"] in {"finished", "cancelled"}:
            document["purge_at"] = datetime.fromtimestamp(
                max(item["expires_at"], time.time() + 86400), UTC
            )
        self._mongo_backend.write(
            self._mongo, "replace_one", {"_id": item["id"]}, document, upsert=True
        )
        if "purge_at" in document:
            self._terminal_expiry[item["id"]] = document["purge_at"].timestamp()
        else:
            self._terminal_expiry.pop(item["id"], None)
        self._items[item["id"]] = copy.deepcopy(item)

    def _existing_job(self, upload_id: str) -> Any | None:
        try:
            return self.manager.registry.get(upload_id).committed
        except KeyError:
            return None

    def _reconcile_finished(self) -> None:
        for item in self._all():
            if item["state"] not in _ACTIVE:
                continue
            record = self._existing_job(item["id"])
            if record is not None and item["state"] == "receiving":
                # A crash can occur after enqueue committed but before the
                # receipt's queued state committed. The job owns its bytes now.
                item["state"] = "queued"
                self._save(item)
            if record is not None and str(record.status) in {"completed", "cancelled"}:
                path = self.root / (item["id"] + ".part")
                path.unlink(missing_ok=True)
                item["state"] = (
                    "cancelled" if str(record.status) == "cancelled" else "finished"
                )
                self._save(item)

    def get_unchecked(self, upload_id: str) -> dict[str, Any]:
        if not isinstance(upload_id, str) or not _ID.fullmatch(upload_id):
            raise UploadError("Upload session not found", 404)
        self._prune_terminal(upload_id)
        item = copy.deepcopy(self._items.get(upload_id))
        if item is None:
            raise UploadError("Upload session not found", 404)
        if item.get("id") != upload_id or item.get("state") not in _ACTIVE | {
            "finished",
            "cancelled",
        }:
            raise UploadError("Stored upload session is invalid", 500)
        return item

    def _destination(self, item: dict[str, Any]) -> str:
        try:
            binding = self.manager.store.upload_destination(
                item["parent_id"],
                directory_id=item.get("parent_node_id"),
            )
        except DriveError as exc:
            raise UploadError(
                "The destination folder is unavailable. "
                "Restore it or cancel this upload."
            ) from exc
        item.update(parent_node_id=binding["id"], parent_id=binding["path"])
        return binding["path"]

    def get(self, upload_id: str) -> dict[str, Any]:
        # Server callers hold the receipt lock through any expiry cleanup.
        item = self.get_unchecked(upload_id)
        # Inspection remains available when a folder is trashed/deleted, so owners
        # can cancel the receipt; active chunks/complete enforce the binding below.
        with suppress(UploadError):
            self._destination(item)
        if item["state"] == "receiving" and item["expires_at"] <= time.time():
            record = self._existing_job(upload_id)
            if record is not None:
                item["state"] = {
                    "completed": "finished",
                    "cancelled": "cancelled",
                }.get(str(record.status), "queued")
                return item
            self._cancel_receiving(item)
            raise UploadError("Upload session expired. Select the file again.", 410)
        return item

    async def inspect(self, upload_id: str) -> dict[str, Any]:
        async with self.locks.setdefault(upload_id, asyncio.Lock()):
            return await self._owned_worker(self.get, upload_id)

    def path(self, upload_id: str) -> Path:
        self.get_unchecked(upload_id)
        return self.root / (upload_id + ".part")

    def create(
        self,
        name: str,
        parent_id: str,
        total_size: int,
        *,
        folder_upload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._reconcile_finished()
        for item in self._all():
            lock = self.locks.get(item["id"])
            if (
                item["id"] not in self._pending
                and item["state"] == "receiving"
                and item["expires_at"] <= time.time()
                and (lock is None or not lock.locked())
            ):
                self._cancel_receiving(item)
        item = self._admit(name, parent_id, total_size, folder_upload)
        self._open_receipt(item)
        return item

    def _admit(
        self,
        name: str,
        parent_id: str,
        total_size: int,
        folder_upload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if type(total_size) is not int or total_size <= 0:
            raise UploadError("File size must be a positive integer", 422)
        if total_size > self.settings.upload_limit:
            raise UploadError("File exceeds the configured upload limit", 413)
        if folder_upload is not None:
            try:
                folder_upload = FolderUploadMetadata.model_validate(
                    folder_upload
                ).model_dump()
            except ValueError:
                raise UploadError("Invalid folder upload metadata", 422) from None
            root_parts, original_root = _normalize_path(folder_upload["root_path"])
            if not root_parts or root_parts[-1] == ROOT_ID:
                raise UploadError("A folder upload requires a non-root folder", 422)
            # Browser queue entries retain their original paths. Bind both
            # opaque identities again so moving the folder or an ancestor does
            # not strand files that have not created a receipt yet.
            root = self.manager.store.upload_destination(
                original_root, directory_id=root_parts[-1]
            )
            parent_parts, original_parent = _normalize_path(parent_id)
            binding = self.manager.store.upload_destination(
                original_parent,
                directory_id=parent_parts[-1] if parent_parts else ROOT_ID,
            )
            parent_id = binding["path"]
            root_path = root["path"]
            if parent_id != root_path and not parent_id.startswith(root_path + "/"):
                raise UploadError("The file is outside its folder upload", 422)
            folder_upload["root_path"] = root_path
        else:
            binding = self.manager.store.upload_destination(parent_id)
            parent_id = binding["path"]
        active = self._all()
        active = [item for item in active if item["state"] in _ACTIVE]
        if len(active) >= 1000:
            raise UploadError("Too many unfinished upload sessions", 429)
        try:
            check_staging(self.settings, self.manager, total_size, receipts=self)
        except StorageUnavailable as exc:
            raise UploadError(str(exc), exc.status) from None
        upload_id = secrets.token_hex(16)
        item = {
            "id": upload_id,
            "name": safe_name(name),
            "parent_id": parent_id,
            "parent_node_id": binding["id"],
            "total_size": total_size,
            "offset": 0,
            "checksums": [],
            "chunk_size": CHUNK_SIZE,
            "state": "receiving",
            "created_at": time.time(),
            "expires_at": time.time() + self.settings.upload_session_ttl_seconds,
        }
        if folder_upload is not None:
            item["folder_upload"] = folder_upload
        return item

    def _open_receipt(self, item: dict[str, Any]) -> None:
        upload_id = item["id"]
        path = self.root / (upload_id + ".part")
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        try:
            self._save(item)
        except BaseException:
            # A lost acknowledgement is not evidence of rollback. Keep the
            # file and reservation until expiry/reconciliation; a committed
            # cloud receipt must never point at a file we just deleted.
            self._items[upload_id] = copy.deepcopy(item)
            raise
        logger.debug(
            "Device receipt opened id=%s bytes=%s", upload_id, item["total_size"]
        )

    @staticmethod
    async def _owned_worker(callback: Any, *args: Any) -> Any:
        work = asyncio.get_running_loop().run_in_executor(None, callback, *args)
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(work)
                break
            except asyncio.CancelledError:
                # A disconnected request still owns its worker through fsync
                # and the MongoDB commit, including repeated cancellation.
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _reconcile_for_creation(self) -> None:
        for snapshot in self._all():
            upload_id = snapshot["id"]
            if upload_id in self._pending or snapshot["state"] not in _ACTIVE:
                continue
            lock = self.locks.setdefault(upload_id, asyncio.Lock())
            if lock.locked():
                # An active append/complete/cancel owns this receipt's state.
                continue
            async with lock:
                item = self.get_unchecked(upload_id)
                record = self._existing_job(upload_id)
                state = item["state"]
                if record is not None and str(record.status) in {
                    "completed",
                    "cancelled",
                }:
                    item["state"] = (
                        "cancelled" if str(record.status) == "cancelled" else "finished"
                    )
                elif (
                    record is None
                    and item["state"] == "receiving"
                    and item["expires_at"] <= time.time()
                ):
                    item["state"] = "cancelled"
                if item["state"] == state:
                    # Active jobs release their own receipt. Saving a queued
                    # snapshot here could race that synchronous terminal save.
                    continue

                def reconcile(
                    item: dict[str, Any] = item, upload_id: str = upload_id
                ) -> None:
                    if item["state"] in {"finished", "cancelled"}:
                        (self.root / (upload_id + ".part")).unlink(missing_ok=True)
                    self._save(item)

                # Job inspection and receipt locking stay on the event loop;
                # only immutable receipt work reaches the cloud worker.
                await self._owned_worker(reconcile)

    async def create_async(
        self,
        name: str,
        parent_id: str,
        total_size: int,
        *,
        folder_upload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._creation_lock:
            await self._reconcile_for_creation()
            item = self._admit(name, parent_id, total_size, folder_upload)
            self._pending[item["id"]] = item
            try:
                await self._owned_worker(self._open_receipt, item)
            finally:
                self._pending.pop(item["id"], None)
            return item

    async def append(
        self, upload_id: str, offset: int, body: bytes | bytearray
    ) -> dict[str, Any]:
        async with self.slots, self.locks.setdefault(upload_id, asyncio.Lock()):
            item = await self._owned_worker(self.get, upload_id)
            if item["state"] != "receiving":
                raise UploadError("This upload no longer accepts chunks")
            await self._owned_worker(self._destination, item)
            if type(offset) is not int or offset != item["offset"]:
                raise UploadError(
                    "Upload offset changed. Read the current offset and resume.",
                    code="UPLOAD_OFFSET_CHANGED",
                )
            if (
                not body
                or len(body) > CHUNK_SIZE
                or offset + len(body) > item["total_size"]
            ):
                raise UploadError("Invalid upload chunk length", 413)
            if len(body) != CHUNK_SIZE and offset + len(body) != item["total_size"]:
                raise UploadError(
                    "Only the last chunk may be smaller than chunk_size", 422
                )
            path = self.path(upload_id)
            if not path.exists() or path.stat().st_size < offset:
                raise UploadError(
                    "Staged bytes are missing. Cancel and select the file again."
                )
            if (
                shutil.disk_usage(self.root).free - len(body)
                < self.settings.min_free_bytes
            ):
                raise UploadError("Temporary storage is full", 507)

            def commit(checksum: str) -> None:
                item["checksums"].append(
                    {"offset": offset, "size": len(body), "sha256": checksum}
                )
                item["offset"] += len(body)
                item["expires_at"] = (
                    time.time() + self.settings.upload_session_ttl_seconds
                )
                self._save(item)

            def write() -> str:
                with path.open("r+b") as handle:
                    handle.truncate(offset)
                    handle.seek(offset)
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                checksum = hashlib.sha256(body).hexdigest()
                # A majority transaction can take several network round
                # trips. Keep it off the HTTP event loop, but in the same
                # owned worker as the fsync so disconnects still drain it.
                commit(checksum)
                return checksum

            work = asyncio.get_running_loop().run_in_executor(None, write)
            cancelled = False
            while True:
                try:
                    await asyncio.shield(work)
                    break
                except OSError as exc:
                    if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                        raise UploadError(
                            "Upload storage filled while receiving data. "
                            "Free disk space, then resume this upload.",
                            507,
                        ) from None
                    raise
                except asyncio.CancelledError:
                    # Repeated disconnect/shutdown cancellation still cannot
                    # release this lock while an OS write or its durable cloud
                    # receipt commit is in progress.
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            return item

    @staticmethod
    def _verify(item: dict[str, Any], path: Path) -> None:
        if not path.is_file() or path.stat().st_size != item["total_size"]:
            raise UploadError(
                "Staged bytes are missing or incomplete. Select the file again."
            )
        expected_offset = 0
        with path.open("rb") as handle:
            for chunk in item["checksums"]:
                if (
                    chunk["offset"] != expected_offset
                    or not 0 < chunk["size"] <= CHUNK_SIZE
                ):
                    raise UploadError("Upload checksum journal is inconsistent")
                content = handle.read(chunk["size"])
                if (
                    len(content) != chunk["size"]
                    or hashlib.sha256(content).hexdigest() != chunk["sha256"]
                ):
                    raise UploadError(
                        "Staged file checksum failed. Select the file again."
                    )
                expected_offset += len(content)
        if expected_offset != item["total_size"]:
            raise UploadError("Upload checksum journal is incomplete")

    @_journalled
    async def complete(self, upload_id: str) -> Any:
        started = time.monotonic()
        async with self.locks.setdefault(upload_id, asyncio.Lock()):
            item = await self._owned_worker(self.get, upload_id)
            if item["state"] in {"cancelled", "cancelling"}:
                raise UploadError("Upload was cancelled")
            existing = self._existing_job(upload_id)
            if existing is not None:
                if item["state"] == "receiving":
                    item["state"] = "queued"
                    await self._owned_worker(self._save, item)
                return existing
            if item["state"] == "finished":
                raise UploadError("Completed upload history expired", 410)
            if item["offset"] != item["total_size"]:
                raise UploadError("Upload is incomplete")
            await self._owned_worker(self._destination, item)
            verification_started = time.monotonic()
            verification = asyncio.get_running_loop().run_in_executor(
                None, self._verify, item, self.path(upload_id)
            )
            cancelled = False
            while True:
                try:
                    await asyncio.shield(verification)
                    break
                except asyncio.CancelledError:
                    # All bytes already belong to the server. A browser leaving
                    # during verification must not strand a complete receipt, or
                    # release its lock while its disk worker is still reading.
                    cancelled = True
                except UploadError:
                    logger.warning(
                        "Device upload verification failed id=%s bytes=%s; "
                        "cancel this receipt and upload the original file again",
                        upload_id,
                        item["total_size"],
                    )
                    raise
            verification_seconds = time.monotonic() - verification_started
            # A folder may move while disk verification yields. Resolve its UUID
            # immediately before enqueue, rather than reusing the old ancestry.
            destination = await self._owned_worker(self._destination, item)
            record = await self.manager.enqueue_upload(
                self.path(upload_id),
                destination,
                item["name"],
                item["total_size"],
                transfer_id=upload_id,
                **(
                    {"folder_upload": copy.deepcopy(item["folder_upload"])}
                    if item.get("folder_upload") is not None
                    else {}
                ),
            )
            item["state"] = "queued"
            await self._owned_worker(self._save, item)
            logger.info(
                "Device upload accepted id=%s job_id=%s bytes=%s "
                "receipt_age_seconds=%s "
                "verify_seconds=%.3f handoff_seconds=%.3f",
                upload_id,
                record.id,
                item["total_size"],
                (
                    f"{max(0.0, time.time() - item['created_at']):.3f}"
                    if type(item.get("created_at")) in (int, float)
                    else "unknown"
                ),
                verification_seconds,
                time.monotonic() - started,
            )
            if cancelled:
                raise asyncio.CancelledError
            return record

    def _cancel_receiving(self, item: dict[str, Any]) -> None:
        (self.root / (item["id"] + ".part")).unlink(missing_ok=True)
        item["state"] = "cancelled"
        self._save(item)

    async def cancel(self, upload_id: str) -> dict[str, Any]:
        async with self.locks.setdefault(upload_id, asyncio.Lock()):
            item = self.get_unchecked(upload_id)
            if item["state"] in {"finished", "cancelled"}:
                return item
            if item["state"] == "receiving" and self._existing_job(upload_id) is None:
                await self._owned_worker(self._cancel_receiving, item)
            else:
                cancel = getattr(
                    self.manager, "cancel_job", self.manager.registry.cancel
                )
                # The manager checks committed metadata before mutating the
                # receipt or job; a protected file must never enter cleanup.
                record = await cancel(upload_id)
                item["state"] = (
                    "cancelled" if str(record.status) == "cancelled" else "cancelling"
                )
                await self._owned_worker(self._save, item)
            return item

    def release(self, upload_id: str) -> None:
        try:
            item = self.get_unchecked(upload_id)
        except UploadError as exc:
            if exc.status == 404:
                return
            raise
        item["state"] = (
            "cancelled" if item["state"] in {"cancelled", "cancelling"} else "finished"
        )
        self._save(item)
