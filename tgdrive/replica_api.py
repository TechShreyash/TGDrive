"""Owner-only optional replica controls, including recovery-only startup."""

from __future__ import annotations

import json

from fastapi import Depends, HTTPException, Request, Response
from pydantic import Field, SecretStr, StrictBool

from tgdrive.managed_settings import PrivateModel, SettingsProblem


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
            from tgdrive.replica import ChannelReplica

            runtime.replica = ChannelReplica(settings, runtime)
        return runtime.replica

    async def invoke(operation):
        try:
            return await operation
        except SettingsProblem:
            raise
        except Exception as error:
            from tgdrive.replica_transport import ReplicaTransportError

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
