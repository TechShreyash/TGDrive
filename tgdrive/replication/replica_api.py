"""Owner-only optional replica controls, including recovery-only startup."""

from __future__ import annotations

import json
import re
import secrets
import shutil

from fastapi import Depends, HTTPException, Request, Response
from pydantic import Field, SecretStr, StrictBool

from tgdrive.settings.managed_settings import PrivateModel, SettingsProblem


class ConfigureReplica(PrivateModel):
    channel_id: int = Field(lt=0)
    bot_token: SecretStr = Field(min_length=20, max_length=256)
    independent_accounts: StrictBool


class ReplicaAction(PrivateModel):
    action: str = Field(max_length=32)


class RecoverReplica(PrivateModel):
    source_channel: int | None = Field(default=None, lt=0)
    bot_token: SecretStr | None = Field(default=None, min_length=20, max_length=256)
    key: SecretStr | None = Field(default=None, min_length=64, max_length=64)
    message_id: int | None = Field(default=None, gt=0)
    target_channel: int | None = Field(default=None, lt=0)
    target_bot_token: SecretStr | None = Field(
        default=None, min_length=20, max_length=256
    )
    confirm_channel: str = Field(default="", max_length=32)
    activate: StrictBool = False
    catalog_only: StrictBool = False


def register_replica_routes(app, settings, require_admin, get_runtime):
    def service(request):
        runtime = get_runtime(request)
        if getattr(runtime, "replica", None) is None:
            from tgdrive.replication.replica import ChannelReplica

            runtime.replica = ChannelReplica(settings, runtime)
        return runtime.replica

    async def invoke(operation):
        try:
            return await operation
        except SettingsProblem:
            raise
        except Exception as error:
            from tgdrive.replication.replica_transport import ReplicaTransportError

            detail = (
                str(error)
                if isinstance(error, (ReplicaTransportError, ValueError))
                else "Backup operation could not be verified. "
                "Review channel access and try again."
            )
            raise HTTPException(422, detail) from None

    admin = [Depends(require_admin)]

    @app.get("/api/v1/replica", dependencies=admin)
    async def status(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return service(request).status()

    @app.post("/api/v1/replica/configure", dependencies=admin)
    async def configure(payload: ConfigureReplica, request: Request):
        current = service(request)
        if getattr(get_runtime(request), "store", None) is None:
            raise HTTPException(
                409, "Restore the existing catalog before configuring a new backup."
            )
        async with current.control:
            if current._operation.locked():
                raise HTTPException(
                    409, "Pause and wait for the current part to finish first."
                )
            async with current._operation:
                return await invoke(
                    current.configure(
                        payload.channel_id,
                        payload.bot_token.get_secret_value(),
                        payload.independent_accounts,
                    )
                )

    @app.post("/api/v1/replica/action", dependencies=admin)
    async def action(payload: ReplicaAction, request: Request):
        current = service(request)
        async with current.control:
            return await invoke(current.action(payload.action))

    @app.get("/api/v1/replica/key", dependencies=admin)
    async def key(request: Request):
        current = service(request)
        return Response(
            json.dumps(current.recovery_kit()),
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    'attachment; filename="tgdrive-channel-recovery-key.json"'
                ),
            },
        )

    @app.get("/api/v1/replica/file", dependencies=admin)
    async def backup_file(request: Request):
        current = service(request)
        return Response(
            current.recovery_file(),
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": 'attachment; filename="tgdrive-backup.tgdb"',
            },
        )

    @app.post("/api/v1/replica/import", dependencies=admin)
    async def import_file(request: Request):
        from tgdrive.recovery.restore_api import _finish
        from tgdrive.replication.replica_state import MAX_BUNDLE

        if settings.demo_mode:
            raise HTTPException(403, "Backup restore is disabled in demo mode")
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/octet-stream"
        ):
            raise HTTPException(
                415, "Upload the recovery file as application/octet-stream"
            )
        keys = request.headers.getlist("x-tgdrive-recovery-key")
        if len(keys) != 1 or re.fullmatch(r"[a-fA-F0-9]{64}", keys[0]) is None:
            raise HTTPException(422, "Enter the recovery key saved with this file")
        source_values = request.headers.getlist("x-tgdrive-source-channel")
        token_values = request.headers.getlist("x-tgdrive-bot-token")
        if len(source_values) > 1 or len(token_values) > 1:
            raise HTTPException(422, "Supply only one channel and bot token")
        source = None
        if source_values:
            if re.fullmatch(r"-[1-9][0-9]{0,18}", source_values[0]) is None:
                raise HTTPException(
                    422, "Enter the surviving channel's negative numeric ID"
                )
            source = int(source_values[0])
            if source < -(1 << 63):
                raise HTTPException(422, "The surviving channel ID is invalid")
        token = token_values[0] if token_values else None
        if (
            token is not None
            and re.fullmatch(r"[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}", token) is None
        ):
            raise HTTPException(422, "Enter a valid replacement bot token")
        current = service(request)
        async with current.control:
            if current._operation.locked() or current.blocked:
                raise HTTPException(
                    409,
                    "Pause or finish the existing recovery before opening another file",
                )
            async with current._operation:
                path = current.directory / (".replica-import-" + secrets.token_hex(16))
                try:
                    with path.open("xb") as target:
                        path.chmod(0o600)
                        size = 0
                        async for part in request.stream():
                            size += len(part)
                            if size > MAX_BUNDLE:
                                raise HTTPException(
                                    413, "Recovery file exceeds the 64 MiB limit"
                                )
                            if (
                                shutil.disk_usage(current.directory).free
                                < len(part) + settings.min_free_bytes
                            ):
                                raise HTTPException(
                                    507,
                                    "Not enough temporary space "
                                    "to open the recovery file",
                                )
                            await _finish(target.write, part)
                    await require_admin(request)
                    return await invoke(
                        current.inspect_file(path, keys[0], source, token)
                    )
                except OSError:
                    raise HTTPException(
                        507,
                        "The recovery file could not be staged. "
                        "Check temporary space and permissions.",
                    ) from None
                finally:
                    path.unlink(missing_ok=True)

    @app.get("/api/v1/replica/report", dependencies=admin)
    async def report(request: Request):
        return Response(
            json.dumps(service(request).status(), ensure_ascii=False),
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    'attachment; filename="tgdrive-channel-backup-report.json"'
                ),
            },
        )

    @app.post("/api/v1/replica/recover", dependencies=admin)
    async def recover(payload: RecoverReplica, request: Request):
        current = service(request)
        async with current.control:
            if current._operation.locked():
                raise HTTPException(
                    409, "Pause and wait for the current part to finish first."
                )
            async with current._operation:
                if payload.activate:
                    candidate = current.journal.get("candidate")
                    if candidate and candidate.get("complete"):
                        catalog = candidate.get("catalog")
                        destination = (
                            catalog.get("storage_channel_id")
                            if isinstance(catalog, dict)
                            else None
                        )
                        if type(destination) is not int or destination >= 0:
                            raise SettingsProblem(
                                "The recovery destination is invalid. "
                                "Inspect recovery again.",
                                status=409,
                            )
                        if payload.confirm_channel != str(destination):
                            raise SettingsProblem(
                                "Type the recovered destination channel ID "
                                "to confirm activation.",
                                status=422,
                            )
                    return current.activate()
                if payload.target_channel is not None:
                    return await invoke(
                        current.recover_to(
                            payload.target_channel,
                            payload.target_bot_token.get_secret_value()
                            if payload.target_bot_token
                            else "",
                            payload.confirm_channel,
                            catalog_only=payload.catalog_only,
                        )
                    )
                if payload.source_channel is None or payload.bot_token is None:
                    raise HTTPException(
                        422,
                        "Supply the surviving channel, its backup bot "
                        "and the offline recovery key.",
                    )
                return await invoke(
                    current.inspect_recovery(
                        payload.source_channel,
                        payload.bot_token.get_secret_value(),
                        payload.key.get_secret_value() if payload.key else None,
                        payload.message_id,
                    )
                )
