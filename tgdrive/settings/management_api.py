"""Narrow owner routes; credentials are write-only and never serialized."""

import logging

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from tgdrive.settings.bot_settings import BotUploadPreferences, get_bot_settings
from tgdrive.settings.managed_settings import (
    SettingsProblem,
    apply_document,
    initial_clients,
    read_document,
)
from tgdrive.settings.management import SettingsChange, get_connection_manager
from tgdrive.settings.premium_login_api import register_premium_routes

logger = logging.getLogger(__name__)


async def apply_saved_connections(app, settings, manager) -> bool:
    """Swap in a runtime built from the saved connections when nothing is running.

    Busy installations keep the documented restart path so pending work and
    browser uploads are not interrupted.
    """
    factory = getattr(app.state, "runtime_factory", None)
    if factory is None or not manager.restart_required:
        return False
    async with manager.lock:
        try:
            manager._ensure_idle()
        except SettingsProblem:
            return False
        document = read_document(settings.resolved_data_dir)
        if document is None or document.revision == settings.managed_revision:
            return False
        old = manager.runtime
        logger.info("Applying saved connections without a restart")
        await old.stop()
        apply_document(settings, document)
        manager.active = initial_clients(settings)
        fresh = factory(settings)
        fresh.connection_manager = manager
        manager.runtime = fresh
        app.state.runtime = fresh
        try:
            await fresh.start()
        except Exception:
            logger.exception("Could not start TGDrive with the saved connections")
            raise SettingsProblem(
                "Connections are saved, but TGDrive could not reconnect. "
                "Restart TGDrive.",
                code="SETTINGS_APPLY_FAILED",
                status=503,
            ) from None
        return True


def register_management_routes(app, settings, *, require_admin, get_runtime):
    async def after_save(request: Request) -> dict | None:
        manager = get_connection_manager(settings, get_runtime(request))
        if await apply_saved_connections(request.app, settings, manager):
            return await manager.snapshot()
        return None

    register_premium_routes(
        app,
        require_admin=require_admin,
        get_service=lambda request: get_connection_manager(
            settings, get_runtime(request)
        ),
        after_save=after_save,
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
            saved = await manager.save(change)
        except ValidationError:
            raise SettingsProblem("Invalid connection settings") from None
        return await after_save(request) or saved

    @app.post("/api/v1/settings/refresh", dependencies=admin, status_code=202)
    async def refresh_connections(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        manager = get_connection_manager(settings, get_runtime(request))
        return await manager.refresh_connections()
