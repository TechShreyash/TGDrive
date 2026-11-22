"""Owner-only ZIP creation and extraction endpoints."""

from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, StrictBool

from tgdrive.storage.demo_files import check_path
from tgdrive.transfers.archives import public_destination, start_pdf_password

PathValue = Annotated[str, Field(min_length=1)]
ArchivePassword = Annotated[SecretStr, Field(min_length=1, max_length=255)]


class CreateArchive(BaseModel):
    paths: list[PathValue] = Field(min_length=1)
    destination: PathValue = "/"
    name: str = Field(min_length=1, max_length=255)
    password: ArchivePassword | None = None
    allow_public_exposure: StrictBool = False


class ExtractArchive(BaseModel):
    path: PathValue
    destination: PathValue = "/"
    folder_name: str | None = Field(default=None, min_length=1, max_length=255)
    password: ArchivePassword | None = None
    allow_public_exposure: StrictBool = False


class PdfPasswordRequest(BaseModel):
    path: PathValue
    destination: PathValue = "/"
    operation: Literal["add", "change", "remove"]
    password: ArchivePassword | None = None
    new_password: ArchivePassword | None = None
    allow_public_exposure: StrictBool = False


def register_archive_routes(
    app, *, require_admin, get_store, get_manager, job_snapshot
):
    admin = [Depends(require_admin)]

    def destination(payload, request):
        store = get_store(request)
        check_path(store, payload.destination)
        binding = store.upload_destination(payload.destination)
        if (
            public_destination(store, binding["path"])
            and not payload.allow_public_exposure
        ):
            return JSONResponse(
                {
                    "detail": (
                        "This destination is publicly shared. "
                        "Confirm to expose the created files."
                    ),
                    "code": "PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED",
                },
                status_code=409,
            )
        return None

    @app.post("/api/v1/archives", dependencies=admin, status_code=202)
    async def create_archive(payload: CreateArchive, request: Request) -> Any:
        warning = destination(payload, request)
        if warning is not None:
            return warning
        record = await get_manager(request).start_archive(
            paths=payload.paths,
            destination=payload.destination,
            name=payload.name,
            password=payload.password.get_secret_value() if payload.password else None,
            allow_public_exposure=payload.allow_public_exposure,
        )
        return {"id": record.id, "job": job_snapshot(record, request)}

    @app.post("/api/v1/archives/extract", dependencies=admin, status_code=202)
    async def extract_archive(payload: ExtractArchive, request: Request) -> Any:
        warning = destination(payload, request)
        if warning is not None:
            return warning
        record = await get_manager(request).start_extraction(
            path=payload.path,
            destination=payload.destination,
            folder_name=payload.folder_name,
            password=payload.password.get_secret_value() if payload.password else None,
            allow_public_exposure=payload.allow_public_exposure,
        )
        return {"id": record.id, "job": job_snapshot(record, request)}

    @app.post("/api/v1/pdfs/password", dependencies=admin, status_code=202)
    async def pdf_password(payload: PdfPasswordRequest, request: Request) -> Any:
        warning = destination(payload, request)
        if warning is not None:
            return warning
        record = await start_pdf_password(
            get_manager(request),
            payload.path,
            payload.destination,
            payload.operation,
            password=payload.password.get_secret_value() if payload.password else None,
            new_password=(
                payload.new_password.get_secret_value()
                if payload.new_password
                else None
            ),
            allow_public_exposure=payload.allow_public_exposure,
        )
        return {"id": record.id, "job": job_snapshot(record, request)}
