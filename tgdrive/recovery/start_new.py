"""Guard intentional fresh starts until in-place reset is recoverable."""

from __future__ import annotations

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from tgdrive.settings.managed_settings import SettingsProblem

CONFIRMATION = "START NEW"


class StartNew(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: str = Field(max_length=32)
    password: str = Field(min_length=1, max_length=512)


def register_start_new_route(app, settings, require_admin, password_hash):
    import asyncio

    from tgdrive.core.security import verify_password

    @app.post("/api/v1/start-new", dependencies=[Depends(require_admin)])
    async def start_new(payload: StartNew):
        if settings.demo_mode:
            raise HTTPException(403, "Starting a new drive is disabled in demo mode")
        if payload.confirm != CONFIRMATION:
            raise HTTPException(422, f"Type {CONFIRMATION} to confirm")
        if not await asyncio.to_thread(
            verify_password, payload.password, password_hash
        ):
            raise HTTPException(403, "The password is incorrect")
        raise SettingsProblem(
            "In-place reset is temporarily unavailable. To start a new drive, "
            "use a fresh dedicated MongoDB database and a new storage channel. "
            "Keep the existing database, channel and encryption key for recovery.",
            code="START_NEW_UNAVAILABLE",
            status=409,
        )
