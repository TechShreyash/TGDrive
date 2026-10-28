"""Owner-only upload and restore of authenticated full MongoDB archives."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from tgdrive.core.security import SESSION_COOKIE
from tgdrive.recovery.backup import BackupError
from tgdrive.recovery.mongo_backup import (
    MAX_EXPANDED,
    check_restore_destination,
    decode_snapshot,
    restore_decoded_snapshot,
)
from tgdrive.storage.mongo import DatabaseUnavailable

logger = logging.getLogger(__name__)
RESTORE_PATH = "/api/v1/backup/restore"


class RestoreError(HTTPException):
    def __init__(self, status_code, detail, code):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


async def _drain(task):
    """Repeated disconnect cancellation cannot release resources before work ends."""
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError
    return result


async def _finish(operation, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    return await _drain(task)


def register_restore_routes(app, settings, *, require_admin, get_runtime):
    lock = asyncio.Lock()

    @app.exception_handler(RestoreError)
    async def restore_error(_request, error):
        return JSONResponse(
            {
                "detail": error.detail,
                "code": error.code,
                "restart_required": error.code == "DATABASE_RESTORE_UNCONFIRMED",
            },
            status_code=error.status_code,
            headers={"Cache-Control": "no-store"},
        )

    @app.post(RESTORE_PATH, dependencies=[Depends(require_admin)])
    async def restore_archive(request: Request):
        if settings.demo_mode:
            raise HTTPException(403, "Database restore is disabled in demo mode")
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type != "application/octet-stream":
            logger.warning(
                "Database archive restore rejected: invalid upload content type"
            )
            raise HTTPException(415, "Upload the archive as application/octet-stream")
        key_values = request.headers.getlist("x-tgdrive-recovery-key")
        if len(key_values) != 1 or not re.fullmatch(r"[a-fA-F0-9]{64}", key_values[0]):
            logger.warning(
                "Database archive restore rejected: recovery key format is invalid"
            )
            raise HTTPException(
                422, "Enter the original 64-character MongoDB recovery key"
            )
        if request.headers.getlist("x-tgdrive-confirm") != ["RESTORE"]:
            logger.warning(
                "Database archive restore rejected: owner confirmation is missing"
            )
            raise HTTPException(422, "Confirm the database restore by entering RESTORE")
        if lock.locked():
            raise HTTPException(409, "A database restore is already running")
        async with lock:
            await require_admin(request)
            runtime = get_runtime(request)
            if getattr(runtime, "archive_restore_restart_required", False):
                raise HTTPException(409, "Restart TGDrive before another restore")
            backend = runtime.mongo
            replica = getattr(runtime, "replica", None)
            if replica is not None and (
                replica.blocked or replica.status().get("configured")
            ):
                logger.warning(
                    "Database archive restore rejected: finish channel recovery "
                    "or disable its backup first"
                )
                raise HTTPException(
                    409,
                    "Finish channel recovery or disable the independent backup "
                    "before restoring",
                )
            if (settings.resolved_data_dir / "channel-activation.json").exists():
                logger.warning(
                    "Database archive restore rejected: pending channel activation "
                    "requires review"
                )
                raise HTTPException(
                    409, "Finish the pending channel activation before restoring"
                )
            try:
                await _finish(check_restore_destination, backend, allow_pristine=True)
            except FileExistsError:
                logger.warning(
                    "Database archive restore rejected: "
                    "destination has existing drive work"
                )
                raise RestoreError(
                    409,
                    "Restore requires an empty MongoDB database or a new, unused "
                    "drive. Existing data was preserved.",
                    "RESTORE_DESTINATION_NOT_EMPTY",
                ) from None
            except DatabaseUnavailable, PyMongoError:
                logger.warning(
                    "Database archive restore rejected: MongoDB is unavailable; "
                    "restore database access"
                )
                raise RestoreError(
                    503,
                    "MongoDB is unavailable; restore was not started",
                    "DATABASE_RESTORE_UNAVAILABLE",
                ) from None
            directory = settings.resolved_cache_dir / "mongo-restore"
            path = directory / (secrets.token_hex(16) + ".tgdb")
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                with path.open("xb") as target:
                    path.chmod(0o600)
                    total = 0
                    async for part in request.stream():
                        total += len(part)
                        if total > MAX_EXPANDED:
                            logger.warning(
                                "Database archive restore rejected: archive "
                                "exceeds the 1 GiB limit"
                            )
                            raise HTTPException(
                                413, "MongoDB archive exceeds the 1 GiB limit"
                            )
                        await _finish(target.write, part)
                from tgdrive.recovery.recovery_file import MAGIC as RECOVERY_MAGIC

                with path.open("rb") as staged:
                    if staged.read(len(RECOVERY_MAGIC)) == RECOVERY_MAGIC:
                        raise RestoreError(
                            422,
                            "Use Storage & Backups → Restore backup for this recovery "
                            "file so channel access and file locations are verified.",
                            "RECOVERY_FILE_REQUIRES_UNIFIED_RESTORE",
                        )
                data = await _finish(
                    decode_snapshot, path, bytes.fromhex(key_values[0])
                )
                await require_admin(request)
                try:
                    await _finish(
                        check_restore_destination, backend, allow_pristine=True
                    )
                except FileExistsError:
                    logger.warning(
                        "Database archive restore rejected: "
                        "destination changed during upload"
                    )
                    raise RestoreError(
                        409,
                        "The destination changed while the archive was uploaded. "
                        "Existing data was preserved.",
                        "RESTORE_DESTINATION_NOT_EMPTY",
                    ) from None
                if replica is not None and (
                    replica.blocked or replica.status().get("configured")
                ):
                    raise HTTPException(
                        409, "Channel recovery changed; inspect it before restoring"
                    )

                async def commit():
                    # Stop request admissions before draining all background writers.
                    runtime.archive_restore_restart_required = True
                    runtime.recovery_only = True
                    try:
                        await _finish(backend.pause_writes)
                        for name in (
                            "premium_login_service",
                            "thumbnail_rebuild",
                            "thumbnails",
                            "posters",
                        ):
                            resource = getattr(request.app.state, name, None)
                            if resource is not None:
                                await resource.close()
                        connections = getattr(runtime, "connection_manager", None)
                        if connections is not None:
                            await connections.close()
                        failures = await runtime.stop()
                        runtime.store = None
                        if failures:
                            raise RuntimeError("The drive did not stop cleanly")
                        await _finish(backend.acquire)
                        try:
                            await _finish(
                                restore_decoded_snapshot,
                                backend,
                                data,
                                allow_pristine=True,
                            )
                        finally:
                            await _finish(backend.release)
                    except Exception:
                        logger.warning(
                            "MongoDB archive restore could not be confirmed; "
                            "restart required"
                        )
                        raise RestoreError(
                            503,
                            "Restore could not be confirmed. Restart TGDrive and "
                            "review Storage & Backups before retrying.",
                            "DATABASE_RESTORE_UNCONFIRMED",
                        ) from None
                    # SessionManager caches persisted tokens for range requests;
                    # imported sessions are also discarded by the restore engine.
                    request.app.state.sessions._mongo_sessions.clear()
                    logger.info(
                        "Full MongoDB archive restored; service restart required"
                    )

                task = asyncio.create_task(commit())
                await _drain(task)
            except BackupError:
                logger.warning(
                    "Database archive restore rejected: archive authentication or "
                    "validation failed; check file and key"
                )
                raise RestoreError(
                    422,
                    "Archive authentication or validation failed. Check the backup "
                    "file and its original recovery key; nothing was restored.",
                    "ARCHIVE_INVALID",
                ) from None
            except DatabaseUnavailable, PyMongoError:
                logger.warning(
                    "Database archive restore was not started: MongoDB access failed"
                )
                raise RestoreError(
                    503,
                    "MongoDB is unavailable; restore was not started",
                    "DATABASE_RESTORE_UNAVAILABLE",
                ) from None
            except OSError:
                logger.warning(
                    "Database archive restore staging failed; check cache storage "
                    "and permissions"
                )
                raise RestoreError(
                    507,
                    "The archive could not be staged. Check temporary storage and "
                    "permissions, then retry.",
                    "ARCHIVE_STAGING_FAILED",
                ) from None
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "Could not remove staged encrypted restore archive; "
                        "check cache permissions"
                    )
            response = JSONResponse(
                {
                    "restart_required": True,
                    "detail": (
                        "Database restored. Restart TGDrive through Docker or "
                        "Coolify, then sign in using the restored credentials. "
                        "Explicit environment settings still take precedence."
                    ),
                },
                headers={"Cache-Control": "no-store"},
            )
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response
