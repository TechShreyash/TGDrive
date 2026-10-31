"""Erase this installation's drive so the next start creates a new one."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

CONFIRMATION = "START NEW"
# Kept: the ownership lease (held while erasing), generated deployment secrets
# and Telegram login sessions, which belong to the deployment, not the drive.
_KEEP_COLLECTIONS = {"runtime"}
_KEEP_DATA = {".instance.lock", ".deployment-secrets.lock", "deployment-secrets.json"}


def erase_database(backend) -> list[str]:
    """Delete every drive record; the ownership fence stays checked."""
    backend.check_owner()
    names = [
        n for n in backend.db.list_collection_names() if n not in _KEEP_COLLECTIONS
    ]
    for name in names:
        backend.db[name].delete_many({})
    with backend._settings_lock:
        backend._settings_cached = False
        backend._settings_document = None
    return names


def erase_local(settings) -> None:
    """Remove cached bytes, staged uploads and recovery files from the volumes."""
    for directory, keep in (
        (Path(settings.resolved_cache_dir), set()),
        (Path(settings.resolved_data_dir), _KEEP_DATA),
    ):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.name in keep:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)


async def unpin_backups(pool, channel: int) -> bool:
    """Unpin the storage channel so an old backup is not offered for restore.

    Old messages stay in the channel; only the pin that marks a backup goes.
    """
    from telethon.tl import functions

    try:
        async with pool.lease(media=False) as client:
            native = client.native
            await native(
                functions.messages.UnpinAllMessagesRequest(
                    peer=await native.get_input_entity(channel)
                )
            )
        return True
    except Exception as error:
        logger.warning(
            "Could not unpin the storage channel (%s); unpin it in Telegram "
            "before restarting, or the old backup is offered again",
            type(error).__name__,
        )
        return False


class StartNew(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: str = Field(max_length=32)
    password: str = Field(min_length=1, max_length=512)


def register_start_new_route(app, settings, require_admin, password_hash):
    import asyncio

    from tgdrive.core.security import verify_password

    lock = asyncio.Lock()

    @app.post("/api/v1/start-new", dependencies=[Depends(require_admin)])
    async def start_new(payload: StartNew, request: Request):
        """Erase the drive's database, pin and local data, then start fresh."""
        if settings.demo_mode:
            raise HTTPException(403, "Starting a new drive is disabled in demo mode")
        if payload.confirm != CONFIRMATION:
            raise HTTPException(422, f"Type {CONFIRMATION} to confirm")
        if not await asyncio.to_thread(
            verify_password, payload.password, password_hash
        ):
            raise HTTPException(403, "The password is incorrect")
        if lock.locked():
            raise HTTPException(409, "A new drive is already being started")
        async with lock:
            from tgdrive.core.runtime import Runtime

            old = request.app.state.runtime
            logger.warning("Start new drive: erasing this installation's drive")
            unpinned = await unpin_backups(old.pool, settings.storage_channel)
            await old.stop()
            backend = old.mongo
            await asyncio.to_thread(backend.acquire)
            try:
                erased = await asyncio.to_thread(erase_database, backend)
            finally:
                backend.release()
            await asyncio.to_thread(erase_local, settings)
            logger.warning(
                "Start new drive: erased %d collections and local data; starting",
                len(erased),
            )
            fresh = Runtime(settings)
            request.app.state.runtime = fresh
            await fresh.start()
            return {"status": "ok", "unpinned": unpinned, "ready": fresh.ready}
