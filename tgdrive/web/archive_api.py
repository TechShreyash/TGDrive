"""Owner-only ZIP creation and extraction endpoints."""

from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool

from tgdrive.storage.demo_files import check_path
from tgdrive.transfers.archives import public_destination

PathValue = Annotated[str, Field(min_length=1, max_length=4096)]


class CreateArchive(BaseModel):
    paths: list[PathValue] = Field(min_length=1, max_length=1000)
    destination: PathValue = "/"
    name: str = Field(min_length=1, max_length=255)
    allow_public_exposure: StrictBool = False


class ExtractArchive(BaseModel):
    path: PathValue
    destination: PathValue = "/"
    folder_name: str | None = Field(default=None, min_length=1, max_length=255)
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
            allow_public_exposure=payload.allow_public_exposure,
        )
        return {"id": record.id, "job": job_snapshot(record, request)}
