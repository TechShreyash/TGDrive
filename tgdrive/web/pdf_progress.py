"""Owner-only ordered PDF page saves; shared reading stays on the device."""

import asyncio

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.pdf_progress import MAX_PDF_PAGES, PdfProgressError, page_record
from tgdrive.storage.video_progress import (
    MAX_SEQUENCE,
    SAVE_INTERVAL_SECONDS,
    SESSION_PATTERN,
)
from tgdrive.transfers.tasks import _journalled
from tgdrive.web.demo_access import is_demo_restricted
from tgdrive.web.video_progress import FileId

_worker = _journalled(asyncio.to_thread)


class BeginPdfProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SavePdfProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(pattern="^" + SESSION_PATTERN + "$")
    sequence: int = Field(gt=0, le=MAX_SEQUENCE)
    page_number: int = Field(ge=1, le=MAX_PDF_PAGES)
    page_count: int = Field(ge=1, le=MAX_PDF_PAGES)
    final: bool = False

    @model_validator(mode="after")
    def within_document(self):
        page_record(self.page_number, self.page_count, 0)
        return self


def register_pdf_progress_routes(app, *, require_admin, get_store):
    admin_dependency = Depends(require_admin)

    async def owner(request: Request, _access=admin_dependency):
        if is_demo_restricted(request) or "auth" in request.query_params:
            raise SettingsProblem(
                "PDF progress requires owner access; "
                "share and public-demo views use local progress.",
                code="PDF_PROGRESS_OWNER_ONLY",
                status=403,
            )

    admin = [Depends(owner)]

    @app.exception_handler(PdfProgressError)
    async def progress_error(_request: Request, error: PdfProgressError):
        headers = {"Cache-Control": "no-store"}
        body = {"detail": str(error), "code": error.code}
        if error.retry_after is not None:
            headers["Retry-After"] = str(error.retry_after)
            body["retry_after"] = error.retry_after
        return JSONResponse(body, status_code=error.status, headers=headers)

    @app.get("/api/v1/files/{file_id}/pdf-progress", dependencies=admin)
    async def read(file_id: FileId, request: Request):
        value = await asyncio.to_thread(get_store(request).pdf_progress, file_id)
        return JSONResponse({"progress": value}, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/files/{file_id}/pdf-progress/session", dependencies=admin)
    async def begin(file_id: FileId, payload: BeginPdfProgress, request: Request):
        value = await _worker(get_store(request).begin_pdf_progress, file_id)
        return JSONResponse(
            value | {"save_interval_seconds": SAVE_INTERVAL_SECONDS},
            headers={"Cache-Control": "no-store"},
        )

    @app.put("/api/v1/files/{file_id}/pdf-progress", dependencies=admin)
    async def save(file_id: FileId, payload: SavePdfProgress, request: Request):
        value = await _worker(
            get_store(request).save_pdf_progress,
            file_id,
            payload.session_id,
            payload.sequence,
            payload.page_number,
            payload.page_count,
            final=payload.final,
        )
        return JSONResponse({"progress": value}, headers={"Cache-Control": "no-store"})
