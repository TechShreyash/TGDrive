"""Opt-in plaintext relay for one durable encrypted bot import."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import time
from contextlib import aclosing

from telethon.errors import FloodWaitError

from tgdrive.media.media_reader import BLOCK, media_cache_for, stream_pooled_media
from tgdrive.telegram.clients import _block_identity
from tgdrive.transfers.tasks import BOT_INTAKE_CLEANUP_PENDING as CLEANUP_PENDING
from tgdrive.transfers.tasks import _journalled

_worker = _journalled(asyncio.to_thread)
COPY_UNCERTAIN = (
    "Telegram did not confirm the temporary copy. The bot queue is paused. "
    "Review the private intake channel before continuing."
)


class SharedIntakeError(RuntimeError):
    pass


def intake_plan(operation, channel, account, attempt=0):
    random_id = int.from_bytes(
        hashlib.blake2b(
            f"tgdrive-intake:{operation}:{channel}:{attempt}".encode(), digest_size=8
        ).digest(),
        "big",
        signed=True,
    )
    return dict(channel=channel, account=account, random_id=random_id, attempt=attempt)


def _plan(mode, record):
    plan = record.context.get("bot_intake")
    if not isinstance(plan, dict):
        raise SharedIntakeError("The temporary intake plan is invalid; preserve it.")
    channel = plan.get("channel")
    expected = intake_plan(
        record.context["bot_operation_id"],
        channel,
        mode.client.name,
        plan.get("attempt", 0),
    )
    if (
        type(plan.get("attempt")) is not int
        or plan["attempt"] < 0
        or type(channel) is not int
        or channel >= 0
        or channel == mode.settings.storage_channel
        or plan.get("account") != mode.client.name
        or type(plan.get("random_id")) is not int
        or plan["random_id"] != expected["random_id"]
        or record.context.get("bot_encrypt") is not True
        or any(
            key in plan and type(plan[key]) is not bool
            for key in ("dispatched", "deleted", "cleaned")
        )
        or (
            "message_id" in plan
            and (
                type(plan["message_id"]) is not int
                or not 0 < plan["message_id"] < 2**63
            )
        )
    ):
        raise SharedIntakeError("The temporary intake identity changed; preserve it.")
    prefix = plan.get("cache_prefix")
    if prefix is not None and not (
        isinstance(prefix, list)
        and len(prefix) in (5, 6)
        and prefix[:2] == [channel, plan.get("message_id")]
        and type(prefix[3]) is int
        and prefix[3] == record.context["bot_source"]["document_id"]
        and type(prefix[-1]) is int
        and prefix[-1] == record.total
        and (
            (len(prefix) == 5 and prefix[2] == "document")
            or (
                len(prefix) == 6
                and prefix[2] == "photo"
                and isinstance(prefix[4], str)
                and len(prefix[4]) <= 64
            )
        )
    ):
        raise SharedIntakeError("The temporary cache identity changed; preserve it.")
    return plan


async def _channel_guard(mode, plan, *, deleting=False):
    runtime = mode.folder_settings.runtime
    replica = getattr(runtime, "replica", None)
    if replica is not None:
        config = await _worker(replica.journal.get, "config")
        if config and config.get("channel") == plan["channel"]:
            raise SharedIntakeError(
                "The intake channel is now a backup channel. Preserve it for review."
            )
    guard = mode.before_delete if deleting else mode.before_write
    if guard is not None:
        result = guard()
        if inspect.isawaitable(result):
            await result
    if deleting:
        return
    async with asyncio.timeout(45):
        problem = await mode.client.storage_channel_problem(
            plan["channel"], require_pin=False
        )
    if problem:
        raise SharedIntakeError(
            "Use a separate private intake channel with Post/Delete Messages rights."
        )


def _media_identity(message):
    native = getattr(message, "native", message)
    media = getattr(native, "document", None) or getattr(native, "photo", None)
    return getattr(media, "id", None)


def _validate_copy(mode, record, message, *, allow_missing=False):
    if message is None or getattr(message, "empty", False):
        if allow_missing:
            return False
        raise SharedIntakeError("The temporary intake copy is missing.")
    plan = _plan(mode, record)
    native = getattr(message, "native", message)
    if (
        getattr(message, "id", None) != plan["message_id"]
        or getattr(native, "chat_id", None) != plan["channel"]
        or _media_identity(message) != record.context["bot_source"]["document_id"]
        or getattr(mode._media(message), "file_size", None) != record.total
    ):
        raise SharedIntakeError(
            "The temporary intake copy changed. Preserve it for review."
        )
    return True


@_journalled
async def _copy(mode, record, source):
    plan = _plan(mode, record)
    if plan.get("cleaned"):
        plan = intake_plan(
            record.context["bot_operation_id"],
            plan["channel"],
            plan["account"],
            plan["attempt"] + 1,
        )
        record.context["bot_intake"] = plan
        await mode.transfers.registry.persist(record)
    if plan.get("message_id"):
        async with asyncio.timeout(45):
            result = await mode.client.get_messages(
                plan["channel"], message_ids=plan["message_id"]
            )
        _validate_copy(mode, record, result)
        return result
    if plan.get("dispatched"):
        raise SharedIntakeError(COPY_UNCERTAIN)
    if mode.settings.bot_intake_channel != plan["channel"]:
        raise SharedIntakeError(
            "Restore the queued intake-channel configuration before retrying."
        )
    await _channel_guard(mode, plan)
    if record.cancel_event.is_set():
        from tgdrive.transfers.manager import TransferCancelled

        raise TransferCancelled("Transfer cancelled")
    plan["dispatched"] = True
    await mode.transfers.registry.persist(record)
    try:
        async with asyncio.timeout(45):
            result = await mode.client.copy_media(
                plan["channel"],
                getattr(source, "native", source),
                random_id=plan["random_id"],
            )
    except FloodWaitError:
        # A provider rejection confirms that this dispatch made no copy.
        plan.pop("dispatched", None)
        await mode.transfers.registry.persist(record)
        raise
    except Exception as error:
        raise SharedIntakeError(COPY_UNCERTAIN) from error
    identity = getattr(result, "id", None)
    if type(identity) is not int or identity <= 0:
        raise SharedIntakeError("Telegram did not confirm a temporary-copy identity.")
    plan["message_id"] = identity
    await mode.transfers.registry.persist(record)
    _validate_copy(mode, record, result)
    return result


def _clear_disk(cache, prefix, size):
    disk = cache.disk
    if disk is None or prefix is None:
        return
    if disk.directory.is_symlink():
        raise OSError("The temporary media cache directory is unsafe")
    count = (size + BLOCK - 1) // BLOCK
    disk.clear((*prefix, index) for index in range(count))
    # Cache eviction is best-effort; privacy cleanup must also detect failures
    # and remove crash-orphaned blocks whose inventory was never flushed.
    for index in range(count):
        key = disk._key((*prefix, index))
        (disk.directory / (key + ".block")).unlink(missing_ok=True)


@_journalled
async def cleanup_shared(mode, record):
    """Keep the obligation even when the public transfer is already terminal."""
    plan = record.context["bot_intake"]
    try:
        plan = _plan(mode, record)
        if plan.get("cleaned") or not plan.get("dispatched"):
            return True
        if not plan.get("message_id"):
            return False  # Unknown dispatches must never become guessed deletions.
        await _channel_guard(mode, plan, deleting=True)
        if not plan.get("deleted"):
            async with asyncio.timeout(45):
                message = await mode.client.get_messages(
                    plan["channel"], message_ids=plan["message_id"]
                )
                if _validate_copy(mode, record, message, allow_missing=True):
                    await mode.client.delete_messages(
                        plan["channel"], plan["message_id"]
                    )
            plan["deleted"] = True
            await mode.transfers.registry.persist(record)
        cache = media_cache_for(mode.transfers.pool)
        identity = cache._file_identity(
            plan["channel"], plan["message_id"], record.total, None
        )
        await cache.clear(identity)
        await _worker(_clear_disk, cache, plan.get("cache_prefix"), record.total)
        plan.update(cleaned=True, retry_at=0)
        await mode.transfers.registry.persist(record)
        if not record.terminal and record.error == CLEANUP_PENDING:
            await mode.transfers.registry.update(
                record.id,
                error=None,
                phase="queued",
            )
        return True
    except Exception as error:
        delay = max(30, getattr(error, "seconds", 0))
        record.context["bot_intake"]["retry_at"] = time.time() + delay
        if record.terminal:
            await mode.transfers.registry.persist(record)
        else:
            await mode.transfers.registry.update(
                record.id,
                error=CLEANUP_PENDING,
                phase="bot_intake_cleanup",
            )
        return False


async def download_shared(mode, record, source, target, progress):
    try:
        copied = await _copy(mode, record, source)
        plan = _plan(mode, record)
        if record.cancel_event.is_set():
            from tgdrive.transfers.manager import TransferCancelled

            raise TransferCancelled("Transfer cancelled")
        block = _block_identity(copied, 0)
        if block is None:
            raise SharedIntakeError("The temporary copy lacks a pooled media identity.")
        plan["cache_prefix"] = list(block[0][:-1])
        await mode.transfers.registry.persist(record)
        cache = media_cache_for(mode.transfers.pool)
        cache.register_file(
            channel_id=plan["channel"],
            message_id=plan["message_id"],
            size=record.total,
            name=record.context["name"],
        )
        stream = stream_pooled_media(
            mode.transfers.pool,
            mode.client,
            copied,
            channel=plan["channel"],
            limit=(record.total + BLOCK - 1) // BLOCK,
            offset=0,
            background=True,
            request_id=f"bot-intake-{record.id}",
        )
        fd = await _worker(os.open, target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        output = os.fdopen(fd, "wb")
        try:
            received = 0
            async with aclosing(stream):
                async for data in stream:
                    await mode._yield_to_playback(record)
                    if received + len(data) > record.total:
                        raise SharedIntakeError("The temporary copy exceeded its size.")
                    await _worker(output.write, data)
                    received += len(data)
                    await progress(received, record.total)
            if received != record.total:
                raise SharedIntakeError(
                    "The temporary copy ended before its full size."
                )
        finally:
            await _worker(output.close)
    finally:
        await cleanup_shared(mode, record)
