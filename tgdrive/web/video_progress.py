"""Owner-only, ordered video resume metadata; share access never grants it."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import Depends, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.video_progress import (
    MAX_SEQUENCE,
    SAVE_INTERVAL_SECONDS,
    SESSION_PATTERN,
    VideoProgressError,
    position_record,
)
from tgdrive.transfers.tasks import _journalled
from tgdrive.web.demo_access import is_demo_restricted

_worker = _journalled(asyncio.to_thread)
FileId = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class BeginVideoProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SaveVideoProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    session_id: str = Field(pattern="^" + SESSION_PATTERN + "$")
    sequence: int = Field(gt=0, le=MAX_SEQUENCE)
    position_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    final: bool = False

    @model_validator(mode="after")
    def within_duration(self):
        position_record(self.position_seconds, self.duration_seconds, 0)
        return self


def register_video_progress_routes(app, *, require_admin, get_store):
    admin_dependency = Depends(require_admin)

    async def owner(request: Request, _access=admin_dependency):
        if is_demo_restricted(request) or "auth" in request.query_params:
            raise SettingsProblem(
                "Video progress requires owner access; "
                "share and public-demo views use local progress.",
                code="VIDEO_PROGRESS_OWNER_ONLY",
                status=403,
            )

    admin = [Depends(owner)]

    @app.exception_handler(VideoProgressError)
    async def progress_error(_request: Request, error: VideoProgressError):
        headers = {"Cache-Control": "no-store"}
        body = {"detail": str(error), "code": error.code}
        if error.retry_after is not None:
            headers["Retry-After"] = str(error.retry_after)
            body["retry_after"] = error.retry_after
        return JSONResponse(body, status_code=error.status, headers=headers)

    @app.get("/api/v1/files/{file_id}/video-progress", dependencies=admin)
    async def read(file_id: FileId, request: Request):
        value = await asyncio.to_thread(get_store(request).video_progress, file_id)
        return JSONResponse({"progress": value}, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/files/{file_id}/video-progress/session", dependencies=admin)
    async def begin(file_id: FileId, payload: BeginVideoProgress, request: Request):
        value = await _worker(get_store(request).begin_video_progress, file_id)
        return JSONResponse(
            value | {"save_interval_seconds": SAVE_INTERVAL_SECONDS},
            headers={"Cache-Control": "no-store"},
        )

    @app.put("/api/v1/files/{file_id}/video-progress", dependencies=admin)
    async def save(file_id: FileId, payload: SaveVideoProgress, request: Request):
        value = await _worker(
            get_store(request).save_video_progress,
            file_id,
            payload.session_id,
            payload.sequence,
            payload.position_seconds,
            payload.duration_seconds,
            final=payload.final,
        )
        return JSONResponse({"progress": value}, headers={"Cache-Control": "no-store"})
