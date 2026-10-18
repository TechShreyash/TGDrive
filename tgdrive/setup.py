"""Private container-side checks for the interactive Docker setup helper."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sys
from contextlib import contextmanager

from argon2 import PasswordHasher
from pydantic import ValidationError
from telethon import functions, utils

from tgdrive.backup import BackupError, TelegramBackupService
from tgdrive.config import Settings
from tgdrive.store import DriveStore
from tgdrive.tasks import TransferRegistry
from tgdrive.telegram_clients import build_client_pool
from tgdrive.transfers import restore_telegram_cooldowns


class SetupError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@contextmanager
def installation_lock(settings: Settings):
    directory = settings.resolved_data_dir
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with (directory / ".instance.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SetupError(
                    "ALREADY_RUNNING",
                    "This Docker drive is already running. Open its existing web "
                    "address, or stop the app before preparing it again.",
                ) from exc
            yield
    except PermissionError as exc:
        raise SetupError(
            "DATA_PERMISSION",
            "The container cannot write its persistent data volume. Keep the "
            "volume and correct ownership for UID/GID 10001:10001.",
        ) from exc


async def check_channel(settings: Settings, pool) -> tuple[int | None, object]:
    """Read membership/rights without uploading, deleting, or pinning anything."""
    statuses = await pool.status()
    bots = [item for item in statuses if item.kind == "bot"]
    if not bots or any(not item.healthy for item in bots):
        raise SetupError(
            "BOT_LOGIN",
            "A configured storage bot could not sign in. Check its token locally "
            "and keep at least one working storage bot.",
        )
    pinned = None
    message = None
    for index, status in enumerate(bots):
        async with pool.lease(client_name=status.name) as client:
            peer = await client.native.get_input_entity(settings.storage_channel)
            info = await client.native(
                functions.channels.GetFullChannelRequest(utils.get_input_channel(peer))
            )
            chat = next(
                (chat for chat in info.chats if chat.id == peer.channel_id), None
            )
            rights = getattr(chat, "admin_rights", None)
            if not getattr(chat, "broadcast", False):
                raise SetupError(
                    "CHANNEL_TYPE",
                    "Use a dedicated Telegram channel, rather than a group.",
                )
            if not rights or not all(
                getattr(rights, name, False)
                for name in (
                    ("post_messages", "delete_messages")
                    if settings.mongodb_uri
                    else ("post_messages", "edit_messages", "delete_messages")
                )
            ):
                raise SetupError(
                    "CHANNEL_RIGHTS",
                    "Every storage bot needs the required channel administrator "
                    "permissions to post/delete messages.",
                )
            if index == 0 and not settings.mongodb_uri:
                pinned = getattr(info.full_chat, "pinned_msg_id", None)
                if pinned is not None:
                    message = await client.get_messages(
                        settings.storage_channel, message_ids=pinned
                    )
    return pinned, message


async def prepare_mongodb(settings, action, *, mode=None):
    from tgdrive.managed_settings import (
        ManagedDocument,
        initial_clients,
        read_document,
        save_document,
    )
    from tgdrive.mongo import for_settings, save_bootstrap
    from tgdrive.mongo_store import MongoStore

    backend = for_settings(settings)
    pool = build_client_pool(settings)
    local = None
    with installation_lock(settings):
        await asyncio.to_thread(backend.acquire)
        try:
            exists = await asyncio.to_thread(
                backend.db.catalog.find_one, {"_id": "catalog"}
            )
            if exists:
                local = await asyncio.to_thread(MongoStore, backend)
                if local.storage_channel_id not in (None, settings.storage_channel):
                    raise SetupError(
                        "CHANNEL_MISMATCH",
                        "This database belongs to a different channel.",
                    )
            await pool.start()
            await check_channel(settings, pool)
            if action == "inspect":
                return {
                    "backend": "mongodb",
                    "local_state": "healthy" if exists else "missing",
                    "has_pin": False,
                    "checkpoint_candidate": False,
                }
            if mode == "new":
                if exists:
                    raise SetupError(
                        "DATABASE_EXISTS", "The MongoDB drive already exists."
                    )
                local = await asyncio.to_thread(MongoStore.create, backend)
                await asyncio.to_thread(
                    local.bind_storage_channel, settings.storage_channel
                )
                await asyncio.to_thread(save_bootstrap, settings, backend)
                if (
                    await asyncio.to_thread(read_document, settings.resolved_data_dir)
                    is None
                ):
                    await asyncio.to_thread(
                        save_document,
                        settings.resolved_data_dir,
                        ManagedDocument(
                            revision=1,
                            api_id=settings.api_id,
                            api_hash=settings.api_hash,
                            clients=initial_clients(settings),
                        ),
                        0,
                    )
            elif mode != "existing" or not exists:
                raise SetupError(
                    "DATABASE_MISSING",
                    "Use an existing MongoDB drive or explicitly create a new one.",
                )
            return {"state": "protected", "mode": mode, "backend": "mongodb"}
        finally:
            if local is not None:
                local.close()
            await pool.stop(drain_timeout=10)
            await asyncio.to_thread(backend.release)


async def prepare_installation(settings: Settings, action: str, *, mode=None) -> dict:
    if settings.mongodb_uri:
        return await prepare_mongodb(settings, action, mode=mode)
    pool = build_client_pool(settings)
    service = TelegramBackupService(settings, pool)
    try:
        with installation_lock(settings):
            target = service.store_path
            local = None
            if target.exists():
                try:
                    local = DriveStore.load(target)
                    service.check_local_binding(local)
                except Exception as exc:
                    if local is not None:
                        local.close()
                    raise SetupError(
                        "LOCAL_STATE_INVALID",
                        "Existing local metadata cannot safely use this configuration. "
                        "It has been preserved; review recovery/configuration in "
                        "docs/operations.md instead of initializing again.",
                    ) from exc
            try:
                journal_path = settings.resolved_data_dir / "jobs.sqlite3"
                if journal_path.exists():
                    try:
                        journal = TransferRegistry(
                            database_path=journal_path,
                            ttl_seconds=settings.transfer_ttl_seconds,
                        )
                    except Exception as exc:
                        raise SetupError(
                            "LOCAL_JOURNAL_INVALID",
                            "Existing transfer state cannot be loaded safely. It was "
                            "preserved; review local jobs/configuration before setup.",
                        ) from exc
                    try:
                        await restore_telegram_cooldowns(pool, journal.list())
                    finally:
                        journal.close()
                await service.restore_publication_cooldown()
                await pool.start()
                pinned, message = await check_channel(settings, pool)
                if action == "inspect":
                    candidate = service._marker(message, pinned) if pinned else None
                    return {
                        "local_state": "healthy" if local is not None else "missing",
                        "has_pin": pinned is not None,
                        "checkpoint_candidate": candidate is not None,
                    }
                if mode in {"new", "recover"} and local is not None:
                    raise SetupError(
                        "LOCAL_STATE_EXISTS",
                        "A local Docker drive already exists. Choose reuse; new and "
                        "recover never overwrite it.",
                    )
                if mode == "new":
                    if pinned is not None:
                        raise SetupError(
                            "CHANNEL_HAS_PIN",
                            "This channel already has a pinned message. Choose "
                            "recovery "
                            "for an existing TGDrive, or use a new dedicated channel.",
                        )
                    local = DriveStore.create(target)
                elif mode == "recover":
                    local = await service.recover_latest()
                elif mode == "existing":
                    if local is None:
                        raise SetupError(
                            "LOCAL_STATE_MISSING",
                            "No local Docker drive exists. Choose verified Telegram "
                            "recovery, or explicitly choose a new dedicated channel.",
                        )
                else:
                    raise SetupError(
                        "MODE_INVALID", "Choose new, recover, or existing."
                    )
                await service.ensure_revision(local, local.revision)
                return {"state": "protected", "mode": mode}
            finally:
                if local is not None:
                    local.close()
    finally:
        await pool.stop(drain_timeout=10)


def password_hash() -> dict:
    content = sys.stdin.buffer.read(4097)
    if len(content) > 4096:
        raise SetupError("PASSWORD_INVALID", "The private password input is too large.")
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise SetupError("PASSWORD_INVALID", "Invalid private password input.") from exc
    password = payload.get("password") if isinstance(payload, dict) else None
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise SetupError(
            "PASSWORD_INVALID", "Choose a password with 12-256 characters."
        )
    return {"hash": PasswordHasher().hash(password)}


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("inspect")
    sub.add_parser("password-hash")
    prepare = sub.add_parser("prepare")
    prepare.add_argument(
        "--mode", choices=("new", "recover", "existing"), required=True
    )
    args = parser.parse_args()
    try:
        if args.action == "password-hash":
            result = password_hash()
        else:
            result = asyncio.run(
                prepare_installation(
                    Settings(), args.action, mode=getattr(args, "mode", None)
                )
            )
        print(json.dumps(result))
        return 0
    except ValidationError as exc:
        fields = sorted(
            {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        )
        error = SetupError(
            "CONFIG_INVALID",
            "Correct these local configuration fields: " + ", ".join(fields),
        )
    except SetupError as exc:
        error = exc
    except BackupError:
        error = SetupError(
            "RECOVERY_UNCONFIRMED",
            "Telegram recovery/checkpoint protection could not be confirmed. Local "
            "state is preserved. Check channel, pin, and account access; see "
            "docs/operations.md for known-message-ID recovery of legacy checkpoints.",
        )
    except Exception as exc:
        kind = type(exc).__name__
        if isinstance(exc, PermissionError):
            error = SetupError(
                "DATA_PERMISSION",
                "Cannot access persistent storage; check volume permissions.",
            )
        elif any(part in kind for part in ("Auth", "Token", "Session", "ApiId")):
            error = SetupError(
                "BOT_LOGIN",
                "Telegram login failed. Recheck credentials locally; they were not "
                "printed.",
            )
        else:
            error = SetupError(
                "TELEGRAM_UNAVAILABLE",
                "Telegram/channel checks failed (" + kind + "). Check network, "
                "private-channel ID, bot membership, and administrator rights.",
            )
    print(json.dumps({"error": str(error), "code": error.code}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
