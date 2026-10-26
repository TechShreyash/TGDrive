"""Narrow owner routes; credentials are write-only and never serialized."""

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from tgdrive.settings.bot_settings import BotUploadPreferences, get_bot_settings
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.settings.management import SettingsChange, get_connection_manager
from tgdrive.settings.premium_login_api import register_premium_routes
from tgdrive.settings.transfer_preferences import (
    UploadPreferences,
    get_transfer_preferences,
)


def register_management_routes(app, settings, *, require_admin, get_runtime):
    register_premium_routes(
        app,
        require_admin=require_admin,
        get_service=lambda request: get_connection_manager(
            settings, get_runtime(request)
        ),
    )
    admin = [Depends(require_admin)]

    @app.get("/api/v1/settings/bot-mode", dependencies=admin)
    async def bot_preferences(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await get_bot_settings(settings, get_runtime(request)).snapshot()

    @app.patch("/api/v1/settings/bot-mode", dependencies=admin)
    async def save_bot_preferences(
        change: BotUploadPreferences, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        return await get_bot_settings(settings, get_runtime(request)).save(change)

    @app.exception_handler(SettingsProblem)
    async def settings_problem(_request: Request, error: SettingsProblem):
        return JSONResponse(
            {"detail": str(error), "code": error.code}, status_code=error.status
        )

    @app.get("/api/v1/settings", dependencies=admin)
    async def connection_settings(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        manager = get_connection_manager(settings, get_runtime(request))
        return await manager.snapshot()

    @app.patch("/api/v1/settings", dependencies=admin)
    async def update_connection_settings(
        change: SettingsChange, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        manager = get_connection_manager(settings, get_runtime(request))
        try:
            return await manager.save(change)
        except ValidationError:
            raise SettingsProblem("Invalid connection settings") from None

    @app.post("/api/v1/settings/refresh", dependencies=admin, status_code=202)
    async def refresh_connections(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        manager = get_connection_manager(settings, get_runtime(request))
        return await manager.refresh_connections()

    @app.get("/api/v1/settings/uploads", dependencies=admin)
    async def upload_preferences(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await get_transfer_preferences(settings, get_runtime(request)).snapshot()

    @app.patch("/api/v1/settings/uploads", dependencies=admin)
    async def save_upload_preferences(
        change: UploadPreferences, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        return await get_transfer_preferences(settings, get_runtime(request)).save(
            change
        )
