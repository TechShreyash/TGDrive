"""Durable multipart upload coordinator; one catalog file, bounded Telegram pieces."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from telethon.errors import FloodWaitError

from tgdrive.client_pool import ClientPoolNotRunning, NoClientAvailable
from tgdrive.file_parts import MAX_FILE_PARTS, validate_parts, validate_sha256
from tgdrive.file_window import FileWindow
from tgdrive.tasks import TransferStatus
from tgdrive.transfers import (
    TransferCancelled,
    TransferEmptyFile,
    TransferError,
    _media_from_message,
)


def fingerprint(
    path: Path,
    size: int,
    part_size: int,
    *,
    cancel_event=None,
    stop_event=None,
) -> tuple[str, list[dict[str, Any]]]:
    """One bounded sequential pass computes whole-file and per-part checksums."""
    if path.stat().st_size != size:
        raise TransferError("Staged file changed. Reselect the source.")
    whole = hashlib.sha256()
    parts = []
    with path.open("rb") as source:
        offset = 0
        while offset < size:
            length = min(part_size, size - offset)
            digest = hashlib.sha256()
            remaining = length
            while remaining:
                if (cancel_event is not None and cancel_event.is_set()) or (
                    stop_event is not None and stop_event.is_set()
                ):
                    raise TransferCancelled("Transfer cancelled")
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise TransferError("Staged file changed. Reselect the source.")
                whole.update(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            parts.append(
                dict(
                    index=len(parts),
                    offset=offset,
                    size=length,
                    sha256=digest.hexdigest(),
                    random_id=secrets.randbits(63),
                    sender=None,
                    send_started=False,
                    message_id=None,
                    confirmed=False,
                )
            )
            offset += length
        if source.read(1):
            raise TransferError("Staged file changed. Reselect the source.")
    return whole.hexdigest(), parts


async def _fingerprint_owned(path, size, part_size, cancel_event):
    """Drain cooperative hashing before releasing its concurrency/staging ownership."""
    stop = threading.Event()
    work = asyncio.create_task(
        asyncio.to_thread(
            fingerprint,
            path,
            size,
            part_size,
            cancel_event=cancel_event,
            stop_event=stop,
        )
    )
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(work)
        except asyncio.CancelledError:
            interrupted = True
            stop.set()
            if work.done():
                if not work.cancelled():
                    work.exception()
                raise
            continue
        except BaseException:
            if interrupted:
                raise asyncio.CancelledError from None
            raise
        if interrupted:
            raise asyncio.CancelledError
        return result


def _validate_plan(plan: Any, size: int, checksum: Any) -> None:
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise TransferError("The multipart journal is invalid")
    parts = plan.get("parts")
    if (
        plan.get("kind") not in ("bot", "user")
        or not isinstance(parts, list)
        or not parts
    ):
        raise TransferError("The multipart journal is invalid")
    try:
        # This validates immutable ordering/offsets/sizes/checksums before any effect.
        validate_parts(
            [
                dict(
                    index=p["index"],
                    offset=p["offset"],
                    size=p["size"],
                    message_id=p["index"] + 1,
                    sha256=p["sha256"],
                )
                for p in parts
            ],
            size=size,
            message_id=1,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TransferError("The multipart journal is invalid") from exc
    try:
        validate_sha256(checksum, optional=False)
    except ValueError as exc:
        raise TransferError("The multipart checksum is missing") from exc
    random_ids = set()
    for part in parts:
        if (
            type(part.get("random_id")) is not int
            or not -(2**63) <= part["random_id"] < 2**63
        ):
            raise TransferError("The multipart request identity is invalid")
        if part["random_id"] in random_ids:
            raise TransferError("Multipart parts cannot reuse a request identity")
        random_ids.add(part["random_id"])
        if "document_id" in part and (
            type(part["document_id"]) is not int
            or part["document_id"] == 0
            or not -(2**63) <= part["document_id"] < 2**63
        ):
            raise TransferError("The multipart document identity is invalid")
        if part.get("message_id") is not None and (
            type(part["message_id"]) is not int or part["message_id"] <= 0
        ):
            raise TransferError("The multipart message identity is invalid")
        if (
            type(part.get("send_started")) is not bool
            or type(part.get("confirmed")) is not bool
        ):
            raise TransferError("The multipart effect journal is invalid")
        if part.get("send_started") and not isinstance(part.get("sender"), str):
            raise TransferError("The multipart sender identity is missing")
        if part.get("confirmed") and not part.get("message_id"):
            raise TransferError("The multipart confirmation is missing")


async def upload_multipart(manager, record, local_path, destination, file_name, size):
    context = record.context
    context.update(
        kind="upload",
        local_path=str(local_path),
        destination=destination,
        name=file_name,
        size=size,
    )
    manager.registry.persist(record)

    def persist():
        if record.context is not context:
            raise TransferError("The upload journal changed; retry the transfer")
        manager.registry.persist(record)

    cleanup = False
    try:
        if size <= 0:
            raise TransferEmptyFile("Empty files cannot be uploaded to Telegram")
        if manager._check_upload_allowed is not None and not context.get(
            "multipart_plan"
        ):
            manager._check_upload_allowed()
        else:
            await manager._verify_lineage(record)
        destination = manager._check_destination_or_cancel(record, destination)
        if "multipart_plan" not in context:
            if record.cancel_event.is_set():
                raise TransferCancelled("Transfer cancelled")
            bot_limit = manager.settings.bot_upload_limit
            premium_limit = getattr(
                manager.settings, "premium_upload_limit", bot_limit * 2
            )
            healthy_users = any(
                item.kind == "user"
                and item.started
                and item.healthy
                and item.connected is True
                for item in await manager.pool.status()
            )
            configured = getattr(manager.settings, "upload_split_mib", 2000)
            requested = configured * 1024**2 if configured else premium_limit
            premium = (
                bot_limit < size <= premium_limit
                and healthy_users
                and requested > bot_limit
            )
            maximum = premium_limit if premium else bot_limit
            part_size = min(maximum, requested)
            # Preserve bounded manifest size even for a deliberately tiny preference.
            part_size = max(part_size, (size + MAX_FILE_PARTS - 1) // MAX_FILE_PARTS)
            part_size = min(maximum, part_size)
            if (size + part_size - 1) // part_size > MAX_FILE_PARTS:
                raise TransferError("File exceeds the maximum number of Telegram parts")
            manager.registry.update(
                record.id, phase="verifying", status=TransferStatus.RUNNING
            )
            async with manager._semaphore:
                checksum, parts = await _fingerprint_owned(
                    local_path,
                    size,
                    part_size,
                    record.cancel_event,
                )
            if context.get("sha256") and context["sha256"] != checksum:
                raise TransferError("Staged file changed. Reselect the source.")
            context["sha256"] = checksum
            context["multipart_plan"] = dict(
                version=1,
                kind="user" if premium else "bot",
                parts=parts,
            )
            persist()
        plan = context["multipart_plan"]
        _validate_plan(plan, size, context.get("sha256"))
        parts = plan["parts"]
        # Restore persisted provider cooldowns before scheduling any new media effect.
        for part in parts:
            if part.get("sender") and part.get("retry_at", 0) > time.time():
                await manager.pool.defer_client(part["sender"], part["retry_at"])
        if context.get("channel_retry_at", 0) > time.time():
            await manager.pool.defer_channel(context["channel_retry_at"])
        in_flight = {}
        active_parts = set()
        manager.registry.update(
            record.id,
            phase="queued",
            status=TransferStatus.QUEUED,
            current=sum(p["size"] for p in parts if p.get("confirmed")),
            total=size,
        )

        def report(waiting_phase=None):
            if record.context is not context:
                raise TransferError("The upload journal changed; retry the transfer")
            current = sum(p["size"] for p in parts if p.get("confirmed"))
            current += sum(in_flight.values())
            phase = "finalizing" if current >= size else "uploading"
            if not active_parts and waiting_phase:
                phase = waiting_phase
            if phase == "retry_wait":
                future = [
                    p.get("retry_at", 0)
                    for p in parts
                    if not p.get("confirmed") and p.get("retry_at", 0) > time.time()
                ]
                deadline = max(
                    min(future, default=0),
                    context.get("channel_retry_at", 0),
                )
                if deadline > time.time():
                    context["retry_at"] = deadline
            else:
                context.pop("retry_at", None)
            manager.registry.update(
                record.id,
                phase=phase,
                status=(
                    TransferStatus.RUNNING
                    if active_parts or current >= size
                    else TransferStatus.QUEUED
                ),
                current=min(size, current),
                total=size,
            )

        async def upload_part(part):
            cancelling = record.cancel_event.is_set() or context.get("cancel_requested")
            if part.get("confirmed") or (cancelling and part.get("deleted")):
                return
            while True:
                if record.cancel_event.is_set() and not part.get("send_started"):
                    return
                remaining = part.get("retry_at", 0) - time.time()
                if remaining > 0:
                    report(waiting_phase="retry_wait")
                    await asyncio.sleep(remaining)
                try:
                    async with (
                        manager.pool.lease(
                            premium=plan["kind"] == "user",
                            allow_users=plan["kind"] == "bot",
                            client_name=part.get("sender"),
                            media=True,
                            request_id=record.id,
                        ) as client,
                    ):
                        if part.get("sender") is None:
                            part["sender"] = client.name
                            if part["index"] == 0:
                                context["sender"] = client.name
                                context["random_id"] = part["random_id"]
                            persist()
                        if record.cancel_event.is_set() and not part.get(
                            "send_started"
                        ):
                            return
                        active_parts.add(part["index"])
                        report()
                        if part.get("message_id"):
                            message = await client.get_messages(
                                manager.settings.storage_channel,
                                message_ids=part["message_id"],
                            )
                            if (
                                message is None or getattr(message, "empty", False)
                            ) and (
                                record.cancel_event.is_set()
                                or context.get("cancel_requested")
                            ):
                                # A delete can succeed before its local acknowledgement.
                                # Absence completes cleanup, never a catalog upload.
                                part["deleted"] = True
                                active_parts.discard(part["index"])
                                in_flight.pop(part["index"], None)
                                persist()
                                return
                        else:

                            def before_dispatch():
                                manager._check_destination_or_cancel(
                                    record, destination
                                )
                                if record.cancel_event.is_set() and not part.get(
                                    "send_started"
                                ):
                                    raise TransferCancelled("Transfer cancelled")
                                part["send_started"] = True
                                context["send_started"] = True
                                persist()

                            async def progress(current, total):
                                if record.cancel_event.is_set() and not part.get(
                                    "send_started"
                                ):
                                    raise TransferCancelled("Transfer cancelled")
                                in_flight[part["index"]] = min(current, part["size"])
                                report()

                            name = (
                                file_name
                                if len(parts) == 1
                                else (
                                    file_name[:180]
                                    + f".tgdrive-part-{part['index'] + 1:04d}"
                                    + f"-of-{len(parts):04d}"
                                )
                            )
                            if getattr(client, "supports_window_upload", False):
                                message = await client.send_staged(
                                    manager.settings.storage_channel,
                                    local_path,
                                    file_name=name,
                                    random_id=part["random_id"],
                                    progress=progress,
                                    offset=part["offset"],
                                    size=part["size"],
                                    expected_sha256=part["sha256"],
                                    before_dispatch=before_dispatch,
                                )
                            elif len(parts) == 1 and hasattr(client, "send_staged"):
                                # Compatibility with pre-window application adapters.
                                before_dispatch()
                                message = await client.send_staged(
                                    manager.settings.storage_channel,
                                    local_path,
                                    file_name=name,
                                    random_id=part["random_id"],
                                    progress=progress,
                                )
                            else:
                                with FileWindow(
                                    local_path,
                                    part["offset"],
                                    part["size"],
                                    name=name,
                                ) as source:
                                    before_dispatch()
                                    message = await client.send_document(
                                        manager.settings.storage_channel,
                                        source,
                                        file_name=name,
                                        random_id=part["random_id"],
                                        progress=progress,
                                        force_document=True,
                                        disable_notification=True,
                                    )
                                    source.verify(part["sha256"])
                        if (
                            message is None
                            or type(getattr(message, "id", None)) is not int
                        ):
                            raise TransferError(
                                "Telegram did not confirm the storage message"
                            )
                        if part.get("message_id") and part["message_id"] != message.id:
                            raise TransferError(
                                "Telegram returned a different part message"
                            )
                        document = getattr(
                            getattr(message, "native", message), "document", None
                        )
                        document_id = getattr(document, "id", None)
                        if (
                            part.get("document_id") is not None
                            and document_id != part["document_id"]
                        ):
                            raise TransferError(
                                "Telegram part document identity changed"
                            )
                        if type(document_id) is int and document_id != 0:
                            part["document_id"] = document_id
                        part["message_id"] = message.id
                        if part["index"] == 0:
                            context["remote_message_id"] = message.id
                        persist()
                        if record.cancel_event.is_set() or context.get(
                            "cancel_requested"
                        ):
                            return
                        media = _media_from_message(message)
                        if (
                            media is None
                            or getattr(media, "file_size", part["size"]) != part["size"]
                        ):
                            raise TransferError(
                                "Telegram returned an unexpected part size"
                            )
                        part["confirmed"] = True
                        part.pop("retry_at", None)
                        in_flight.pop(part["index"], None)
                        active_parts.discard(part["index"])
                        persist()
                        report()
                        return
                except TransferCancelled:
                    active_parts.discard(part["index"])
                    in_flight.pop(part["index"], None)
                    if part.get("send_started"):
                        raise
                    return
                except FloodWaitError as exc:
                    active_parts.discard(part["index"])
                    in_flight.pop(part["index"], None)
                    deadline = time.time() + exc.seconds + 0.25
                    part["retry_at"] = deadline
                    if getattr(exc, "tgdrive_channel_flood", False):
                        context["channel_retry_at"] = deadline
                    persist()
                    if getattr(exc, "tgdrive_channel_flood", False):
                        await manager.pool.defer_channel(deadline)
                    await manager.pool.defer_client(part["sender"], deadline)
                    report(waiting_phase="retry_wait")
                except (ClientPoolNotRunning, NoClientAvailable):
                    active_parts.discard(part["index"])
                    in_flight.pop(part["index"], None)
                    if record.cancel_event.is_set() and not part.get("send_started"):
                        return
                    report(waiting_phase="waiting_for_telegram")
                    await asyncio.sleep(1)

        cursor = 0

        async def worker():
            nonlocal cursor
            while cursor < len(parts):
                part = parts[cursor]
                cursor += 1
                await upload_part(part)

        workers = set()
        all_workers = set()
        try:
            while workers or cursor < len(parts):
                capacity = max(
                    1,
                    await manager.pool.media_capacity(
                        premium=plan["kind"] == "user",
                        allow_users=plan["kind"] == "bot",
                    ),
                )
                target = min(capacity, len(workers) + len(parts) - cursor)
                for _ in range(max(0, target - len(workers))):
                    task = asyncio.create_task(worker())
                    workers.add(task)
                    all_workers.add(task)
                if not workers:
                    break
                done, workers = await asyncio.wait(
                    workers, timeout=1, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
        except BaseException:
            for task in all_workers:
                task.cancel()
            await asyncio.gather(*all_workers, return_exceptions=True)
            raise
        destination = manager._check_destination_or_cancel(record, destination)
        if record.cancel_event.is_set() or context.get("cancel_requested"):
            await _cleanup_parts(manager, record, parts, premium=plan["kind"] == "user")
            cleanup = True
            raise TransferCancelled("Transfer cancelled")
        if not all(p.get("confirmed") for p in parts):
            raise TransferError("Telegram has not confirmed every file part")
        manifest = [
            {
                key: p[key]
                for key in (
                    "index",
                    "offset",
                    "size",
                    "message_id",
                    "sha256",
                    "document_id",
                )
                if key in p
            }
            for p in parts
        ]
        # No await between catalog commit and its durable recovery marker.
        await asyncio.to_thread(
            manager.store.add_file,
            destination,
            file_name,
            manifest[0]["message_id"],
            size,
            operation_id=record.id,
            keep_both=True,
            parts=manifest,
            sha256=context["sha256"],
        )
        context["remote_message_id"] = manifest[0]["message_id"]
        context["metadata_committed"] = True
        context["catalog_revision"] = manager.store.revision
        persist()
        await manager._protect_committed_upload(record, size)
        cleanup = True
    except asyncio.CancelledError:
        # User cancellation before any message effect may safely release local bytes.
        # Server shutdown keeps them for the durable QUEUED restart path.
        if (
            record.cancel_event.is_set() or context.get("cancel_requested")
        ) and not context.get("send_started"):
            cleanup = True
        raise
    except TransferCancelled:
        # This path is reached only when there is no unpublished ambiguous effect.
        if context.get("send_started") and not cleanup:
            raise
        record.status = TransferStatus.CANCELLED
        record.phase = "cancelled"
        record.error = "Transfer cancelled"
        persist()
        cleanup = True
    finally:
        if cleanup:
            await asyncio.to_thread(local_path.unlink, missing_ok=True)
            uploads = getattr(manager, "uploads", None)
            if uploads is not None:
                uploads.release(record.id)


async def _cleanup_parts(manager, record, parts, *, premium=False):
    """Delete only journalled own messages and retain staging until confirmation."""
    await manager._verify_lineage(record)
    for part in parts:
        if not part.get("message_id") or part.get("deleted"):
            continue
        while not part.get("deleted"):
            remaining = (
                max(
                    part.get("retry_at", 0),
                    record.context.get("channel_retry_at", 0),
                )
                - time.time()
            )
            if remaining > 0:
                manager.registry.update(record.id, phase="retry_wait")
                await asyncio.sleep(remaining)
            try:
                async with manager.pool.lease(
                    client_name=part["sender"],
                    premium=premium,
                    allow_users=not premium,
                    media=True,
                ) as client:
                    await manager._verify_lineage(record)
                    if part.get("document_id") is not None:
                        before = await client.get_messages(
                            manager.settings.storage_channel,
                            message_ids=part["message_id"],
                        )
                        document = getattr(
                            getattr(before, "native", before), "document", None
                        )
                        observed = getattr(document, "id", None)
                        if before is not None and observed != part["document_id"]:
                            raise TransferError(
                                "Telegram part document identity changed"
                            )
                    await client.delete_messages(
                        manager.settings.storage_channel,
                        part["message_id"],
                    )
                    if callable(getattr(client, "get_messages", None)):
                        message = await client.get_messages(
                            manager.settings.storage_channel,
                            message_ids=part["message_id"],
                        )
                        if message is not None and not getattr(message, "empty", False):
                            raise TransferError(
                                "Telegram part deletion confirmation is incomplete"
                            )
                    part["deleted"] = True
                    part.pop("retry_at", None)
                    manager.registry.persist(record)
            except FloodWaitError as exc:
                # Deletion FloodWait conservatively pauses channel writes.
                deadline = time.time() + exc.seconds + 0.25
                part["retry_at"] = deadline
                record.context["channel_retry_at"] = deadline
                manager.registry.persist(record)
                await manager.pool.defer_channel(deadline)
                await manager.pool.defer_client(part["sender"], deadline)
            except (ClientPoolNotRunning, NoClientAvailable):
                manager.registry.update(record.id, phase="waiting_for_telegram")
                await asyncio.sleep(1)
