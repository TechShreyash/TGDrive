"""Typed owner API for durable uploads, jobs and drive actions."""

from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool

from tgdrive.backup import RecoveryConflict
from tgdrive.client_pool import current_upload_limit
from tgdrive.errors import ValidationError as DriveValidationError
from tgdrive.management_api import register_management_routes
from tgdrive.schemas import FolderTreeRequest
from tgdrive.share_links import token_factory
from tgdrive.store import MAX_NAME_LENGTH, MAX_SEARCH_OFFSET, FolderTreeConflictError
from tgdrive.transfer_locations import enrich_locations, locate_transfer
from tgdrive.uploads import CHUNK_SIZE, ResumableUploads, UploadError


class TransferLocationRequest(BaseModel):
    transfer_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    destination: str = Field(default="/", max_length=4096)
    completed: bool = False


class UploadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=1024)
    parent_id: str = Field(default="/", max_length=4096)
    total_size: int = Field(gt=0)


class StarItem(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    starred: StrictBool


class ShareCreate(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    expires_in_days: int | None = Field(default=7, ge=1, le=3650)
    expires_at: datetime | None = None
    label: str = Field(default="", max_length=80)


class MoveItems(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=1000)
    destination: str = Field(min_length=1, max_length=4096)
    conflict: Literal["keep_both", "skip"] = "keep_both"
    allow_public_exposure: bool = False


class BulkItems(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=1000)
    action: Literal["trash", "restore", "delete"]
    allow_public_exposure: bool = False


class RevokeShare(BaseModel):
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    share_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


class ShareUpdate(BaseModel):
    share_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(default="", max_length=80)
    expires_at: datetime | None = None


def register_routes(
    app: Any,
    settings: Any,
    *,
    require_admin: Any,
    is_admin: Any,
    get_store: Any,
    get_manager: Any,
    get_runtime: Any,
) -> None:
    register_management_routes(
        app, settings, require_admin=require_admin, get_runtime=get_runtime
    )
    from tgdrive.performance_api import register_performance_routes

    register_performance_routes(
        app,
        settings,
        require_admin=require_admin,
        get_runtime=get_runtime,
        get_store=get_store,
    )
    admin = [Depends(require_admin)]
    chunk_receivers = asyncio.Semaphore(settings.max_concurrent_transfers)

    def uploads(request: Request) -> ResumableUploads:
        manager = get_manager(request)
        if not hasattr(manager, "uploads"):
            manager.uploads = ResumableUploads(settings, manager)
        return manager.uploads

    @app.exception_handler(UploadError)
    async def upload_error(_request: Request, exc: UploadError) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "code": "UPLOAD_ERROR"}, status_code=exc.status
        )

    @app.get("/api/v1/session")
    async def session(request: Request) -> dict[str, bool]:
        return {"authenticated": is_admin(request)}

    @app.get("/api/v1/status", dependencies=admin)
    async def status(request: Request) -> dict[str, Any]:
        runtime = get_runtime(request)
        pool = getattr(runtime, "pool", None)
        health = await pool.health() if pool else None
        backup = getattr(runtime, "backup", None)
        history = []
        if backup and hasattr(backup, "history"):
            try:
                history = await asyncio.to_thread(backup.history, limit=1)
            except TypeError:
                history = (await asyncio.to_thread(backup.history))[:1]
        connections = getattr(runtime, "connection_manager", None)
        restart_required = bool(connections and connections.restart_required)
        return {
            "restart_required": restart_required,
            "admission_paused": restart_required,
            "telegram_ready": bool(health and health.ready),
            "demo": bool(getattr(runtime, "synthetic", False)),
            "max_file_size": await current_upload_limit(settings, pool),
            "staging_budget_bytes": settings.staging_budget_bytes,
            "metadata_revision": get_store(request).revision,
            "backup": history,
            "recovery": backup.protection_status(get_store(request))
            if backup and hasattr(backup, "protection_status")
            else None,
            "clients": asdict(health) if health else None,
        }

    @app.post("/api/v1/backup", dependencies=admin)
    async def back_up_now(request: Request) -> dict[str, Any]:
        runtime = get_runtime(request)
        service = getattr(runtime, "backup", None)
        if service is None or not hasattr(service, "ensure_revision"):
            raise HTTPException(503, "Drive backup is not available yet")
        store = get_store(request)
        try:
            if getattr(runtime, "mongo", None) is not None:
                await service.snapshot_now(store)
            else:
                await service.ensure_revision(store, store.revision)
        except RecoveryConflict:
            raise
        except Exception:
            # A lost confirmation does not prove that Telegram received nothing.
            # The durable checkpoint journal owns retries and reconciliation.
            raise HTTPException(
                503,
                "Backup is not confirmed yet. Check the backup status and try again.",
            ) from None
        return {"recovery": service.protection_status(store)}

    @app.get("/api/v1/backup/recovery-key", dependencies=admin)
    async def recovery_key(request: Request):
        runtime = get_runtime(request)
        if getattr(runtime, "mongo", None) is None or not settings.mongodb_backup_key:
            raise HTTPException(404, "MongoDB backup recovery key is unavailable")
        return Response(
            "MONGODB_BACKUP_KEY="
            + settings.mongodb_backup_key.get_secret_value()
            + "\n",
            media_type="text/plain",
            headers={
                "Content-Disposition": (
                    'attachment; filename="tgdrive-recovery-key.env"'
                ),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/search", dependencies=admin)
    async def search(
        request: Request,
        q: str = Query(min_length=1, max_length=MAX_NAME_LENGTH),
        offset: int = Query(default=0, ge=0, le=MAX_SEARCH_OFFSET),
        limit: int = Query(default=100, ge=1, le=200),
        sort: Literal["name", "date", "size"] = "name",
        order: Literal["asc", "desc"] | None = None,
    ) -> dict[str, Any]:
        try:
            return get_store(request).search_page(
                q, offset=offset, limit=limit, sort=sort, order=order
            )
        except DriveValidationError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/v1/starred", dependencies=admin)
    async def starred_items(
        request: Request,
        response: Response,
        q: str = Query(default="", max_length=MAX_NAME_LENGTH),
    ):
        response.headers["Cache-Control"] = "no-store"
        items = await asyncio.to_thread(get_store(request).list_starred, q)
        return {"data": {"contents": items}}

    @app.patch("/api/v1/starred", dependencies=admin)
    async def star_item(body: StarItem, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        result = await asyncio.to_thread(
            get_store(request).set_starred, body.path, body.starred
        )
        return {"item": result}

    @app.get("/api/v1/trash", dependencies=admin)
    async def search_trash(
        request: Request,
        response: Response,
        q: str = Query(default="", max_length=MAX_NAME_LENGTH),
    ):
        response.headers["Cache-Control"] = "no-store"
        contents = await asyncio.to_thread(
            get_store(request).list_trash, unicodedata.normalize("NFC", q.strip())
        )
        return {"data": {"contents": contents}}

    @app.get("/api/v1/nodes", dependencies=admin)
    async def nodes(
        request: Request,
        parent_id: str = "/",
        offset: int = 0,
        limit: int = 100,
        sort: str = "name",
    ) -> dict[str, Any]:
        if (
            offset < 0
            or not 1 <= limit <= 500
            or sort not in {"name", "size", "type", "date"}
        ):
            raise HTTPException(422, "Invalid pagination or sort")
        data = get_store(request).get_directory(parent_id)
        items = list(data["contents"].values())
        items.sort(
            key=lambda item: (
                item["type"] != "folder",
                str(item.get("upload_date" if sort == "date" else sort, "")).casefold()
                if sort != "size"
                else item.get("size", 0),
                item["id"],
            )
        )
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "offset": offset,
        }

    @app.post("/api/v1/folders/tree", dependencies=admin, status_code=201)
    async def prepare_folder_tree(payload: FolderTreeRequest, request: Request) -> Any:
        try:
            return await asyncio.to_thread(
                get_store(request).prepare_folder_tree,
                payload.destination,
                payload.root_name,
                payload.folders,
                payload.client_request_id,
            )
        except FolderTreeConflictError as exc:
            return JSONResponse(
                {"detail": str(exc), "code": "FOLDER_TREE_CONFLICT"}, status_code=409
            )

    @app.post("/api/v1/uploads", dependencies=admin, status_code=201)
    async def create_upload(payload: UploadCreate, request: Request) -> dict[str, Any]:
        limit = await current_upload_limit(
            settings, getattr(get_runtime(request), "pool", None)
        )
        await require_admin(request)  # Recheck after awaiting live account health.
        if payload.total_size > limit:
            raise UploadError(
                f"File exceeds the currently available {limit}-byte upload limit", 413
            )
        return uploads(request).create(
            payload.name, payload.parent_id, payload.total_size
        )

    @app.get("/api/v1/uploads/{upload_id}", dependencies=admin)
    async def inspect_upload(upload_id: str, request: Request) -> dict[str, Any]:
        return uploads(request).get(upload_id)

    @app.head("/api/v1/uploads/{upload_id}", dependencies=admin)
    async def upload_offset(upload_id: str, request: Request) -> Response:
        item = uploads(request).get(upload_id)
        return Response(
            headers={
                "Upload-Offset": str(item["offset"]),
                "Upload-Length": str(item["total_size"]),
                "Cache-Control": "no-store",
            }
        )

    @app.patch("/api/v1/uploads/{upload_id}", dependencies=admin)
    async def append_upload(upload_id: str, request: Request) -> Response:
        try:
            offset = int(request.headers["upload-offset"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, "Upload-Offset is required") from exc
        if (
            request.headers.get("content-type", "").split(";")[0]
            != "application/octet-stream"
        ):
            raise HTTPException(415, "Use application/octet-stream")
        async with chunk_receivers:
            body = bytearray()
            async for part in request.stream():
                if len(body) + len(part) > CHUNK_SIZE:
                    raise UploadError("Upload chunk is too large", 413)
                body.extend(part)
            await require_admin(request)
            item = await uploads(request).append(upload_id, offset, body)
        return Response(status_code=204, headers={"Upload-Offset": str(item["offset"])})

    @app.post(
        "/api/v1/uploads/{upload_id}/complete", dependencies=admin, status_code=202
    )
    async def complete_upload(upload_id: str, request: Request) -> dict[str, Any]:
        await require_admin(request)
        record = await uploads(request).complete(upload_id)
        return {"id": record.id, "job": record.as_dict()}

    @app.post("/api/v1/uploads/{upload_id}/cancel", dependencies=admin)
    async def cancel_upload(upload_id: str, request: Request) -> dict[str, Any]:
        return await uploads(request).cancel(upload_id)

    def job_snapshot(record: Any, request: Request) -> dict[str, Any]:
        return record.as_dict() | {
            "name": record.context.get("name", "Transfer"),
            "kind": record.context.get(
                "source_kind", record.context.get("kind", "upload")
            ),
            "queue": (
                get_runtime(request).pool.queue_estimate(record.id)
                if callable(getattr(get_runtime(request).pool, "queue_estimate", None))
                else None
            ),
            "destination": record.context.get("destination", "/"),
        }

    @app.get("/api/v1/jobs", dependencies=admin)
    async def jobs(request: Request) -> dict[str, Any]:
        return {
            "jobs": [
                job_snapshot(record, request)
                for record in get_manager(request).registry.list()
            ]
        }

    @app.get("/api/v1/transfers/history", dependencies=admin)
    async def transfer_history(
        request: Request,
        response: Response,
        status: Literal[
            "all", "finished", "queued", "running", "completed", "error", "cancelled"
        ] = "all",
        limit: int = Query(default=50, ge=1, le=200),
        before: float | None = Query(default=None, ge=0, allow_inf_nan=False),
        before_id: str | None = Query(
            default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
        ),
        q: str = Query(default="", max_length=MAX_NAME_LENGTH),
    ) -> dict[str, Any]:
        if (before is None) != (before_id is None):
            raise HTTPException(400, "Both history cursor fields are required")
        response.headers["Cache-Control"] = "no-store"
        result = await get_manager(request).registry.history(
            status=status,
            limit=limit,
            before=(before, before_id) if before is not None else None,
            query=q,
        )
        estimate = getattr(get_runtime(request).pool, "queue_estimate", None)
        if callable(estimate):
            for row in result["jobs"]:
                row["queue"] = estimate(row["id"])
        await asyncio.to_thread(enrich_locations, get_store(request), result["jobs"])
        return result

    @app.post("/api/v1/transfers/location", dependencies=admin)
    async def transfer_location(
        payload: TransferLocationRequest, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        return await asyncio.to_thread(
            locate_transfer,
            get_store(request),
            {
                "id": payload.transfer_id,
                "destination": payload.destination,
                "status": "completed" if payload.completed else "queued",
            },
        )

    @app.get("/api/v1/jobs/{job_id}", dependencies=admin)
    async def job(job_id: str, request: Request) -> dict[str, Any]:
        return job_snapshot(get_manager(request).registry.get(job_id), request)

    @app.post("/api/v1/jobs/{job_id}/cancel", dependencies=admin)
    async def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
        try:
            return job_snapshot(get_manager(request).cancel_job(job_id), request)
        except ValueError as exc:
            if getattr(exc, "code", None) == "BACKUP_PENDING":
                raise
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/jobs/{job_id}/retry", dependencies=admin)
    async def retry_job(job_id: str, request: Request) -> dict[str, Any]:
        try:
            return job_snapshot(get_manager(request).retry_job(job_id), request)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/shares", dependencies=admin)
    async def shares(request: Request) -> Response:
        records = await asyncio.to_thread(
            get_store(request).list_shares,
            include_expired=True,
            token_factory=token_factory(settings),
        )
        return JSONResponse({"shares": records}, headers={"Cache-Control": "no-store"})

    def selected_share(store, payload):
        rows = store.list_shares(token_factory=token_factory(settings))
        if payload.share_id:
            rows = [row for row in rows if row["id"] == payload.share_id]
        elif payload.path:
            path = store.resolve_id(payload.path.rstrip("/").rsplit("/", 1)[-1])
            rows = [row for row in rows if row["path"] == path]
            if len(rows) > 1:
                raise HTTPException(409, "Choose the individual link in Shared Links.")
        else:
            raise HTTPException(422, "A link ID or item path is required.")
        if not rows:
            raise HTTPException(410, "This link has expired or been revoked.")
        return rows[0]

    @app.post("/api/v1/shares/link", dependencies=admin)
    async def copy_share(payload: RevokeShare, request: Request) -> Response:
        def lookup():
            row = selected_share(get_store(request), payload)
            if not row["copyable"]:
                raise HTTPException(
                    409,
                    "This older link cannot be recovered. "
                    "Create a separate new link instead.",
                )
            return {
                "auth": token_factory(settings)(row["id"]),
                "id": row["id"],
                "path": row["path"],
                "type": row["type"],
                "node_id": row["node_id"],
            }

        return JSONResponse(
            await asyncio.to_thread(lookup), headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/v1/shares", dependencies=admin, status_code=201)
    async def create_share(payload: ShareCreate, request: Request) -> Response:
        expires = (
            payload.expires_at.isoformat()
            if payload.expires_at
            else (
                datetime.now(UTC) + timedelta(days=payload.expires_in_days)
            ).isoformat()
            if payload.expires_in_days
            else None
        )

        def create():
            store = get_store(request)
            with store.metadata_batch():
                factory = token_factory(settings)
                token = store.create_share(
                    payload.path,
                    expires_at=expires,
                    label=payload.label,
                    token_factory=factory,
                )
                row = next(
                    row
                    for row in store.list_shares(payload.path, token_factory=factory)
                    if factory(row["id"]) == token
                )
                return {
                    "auth": token,
                    "id": row["id"],
                    "path": row["path"],
                    "expires_at": row["expires_at"],
                    "label": row["label"],
                }

        return JSONResponse(
            await asyncio.to_thread(create),
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @app.patch("/api/v1/shares", dependencies=admin)
    async def update_share(payload: ShareUpdate, request: Request) -> dict[str, str]:
        await asyncio.to_thread(
            get_store(request).update_share,
            payload.share_id,
            label=payload.label,
            expires_at=payload.expires_at.isoformat() if payload.expires_at else None,
        )
        return {"status": "ok"}

    @app.post("/api/v1/download-links", dependencies=admin)
    async def download_link(payload: RevokeShare, request: Request) -> Response:
        def create_or_reuse():
            store = get_store(request)
            with store.metadata_batch():
                if not payload.path:
                    raise HTTPException(422, "An item path is required.")
                path = store.resolve_id(payload.path.rstrip("/").rsplit("/", 1)[-1])
                store.get_file(path)
                rows = [
                    row
                    for row in store.list_shares(
                        path, token_factory=token_factory(settings)
                    )
                    if row.get("label") == "Download link" and row["copyable"]
                ]
                if rows:
                    row = rows[0]
                    if not row["copyable"]:
                        raise HTTPException(
                            409,
                            "Create a replacement in Shared Links "
                            "before copying this older link.",
                        )
                    token, expires = (
                        token_factory(settings)(row["id"]),
                        row["expires_at"],
                    )
                else:
                    expires = (datetime.now(UTC) + timedelta(days=1)).isoformat()
                    token = store.create_share(
                        path,
                        expires_at=expires,
                        token_factory=token_factory(settings),
                        label="Download link",
                    )
            from urllib.parse import urlencode

            url = (
                str(request.base_url).rstrip("/")
                + "/file?"
                + urlencode({"path": path, "auth": token, "download": "1"})
            )
            return {"url": url, "expires_at": expires, "path": path}

        return JSONResponse(
            await asyncio.to_thread(create_or_reuse),
            headers={"Cache-Control": "no-store"},
        )

    @app.delete("/api/v1/shares", dependencies=admin)
    async def revoke_share(payload: RevokeShare, request: Request) -> dict[str, Any]:
        def revoke():
            store = get_store(request)
            if payload.share_id:
                store.revoke_share(payload.share_id)
            elif payload.path:
                path = store.resolve_id(payload.path.rstrip("/").rsplit("/", 1)[-1])
                store.revoke_share(path)
            else:
                raise HTTPException(422, "A link ID or item path is required.")

        await asyncio.to_thread(revoke)
        return {"status": "ok"}

    def public_destination(store: Any, path: str) -> bool:
        destination = path.rstrip("/") or "/"
        return any(
            destination == share["path"].rstrip("/")
            or destination.startswith(share["path"].rstrip("/") + "/")
            for share in store.list_shares()
        )

    def selection(paths: list[str]) -> list[str]:
        selected = sorted(
            dict.fromkeys("/" + path.strip("/") for path in paths), key=len
        )
        return [
            path
            for path in selected
            if not any(
                path.startswith(parent.rstrip("/") + "/")
                for parent in selected
                if parent != path
            )
        ]

    @app.post("/api/v1/nodes/move", dependencies=admin)
    def move_items(payload: MoveItems, request: Request) -> Any:
        store = get_store(request)
        with store.metadata_batch():
            if (
                public_destination(store, payload.destination)
                and not payload.allow_public_exposure
            ):
                return JSONResponse(
                    {
                        "detail": "This destination is publicly shared. "
                        "Confirm to expose these items.",
                        "code": "PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED",
                    },
                    status_code=409,
                )
            results = []
            for path in selection(payload.paths):
                result = store.move(
                    path, payload.destination, conflict=payload.conflict
                )
                results.append(
                    {
                        "path": path,
                        "status": "moved" if result else "skipped",
                        "item": result,
                    }
                )
        return {"results": results}

    @app.post("/api/v1/nodes/bulk", dependencies=admin)
    async def bulk_items(payload: BulkItems, request: Request) -> Any:
        store = get_store(request)
        selected = selection(payload.paths)
        results = []
        if payload.action == "delete":
            for path in selected:
                store.validate_deletion(path)
            for path in selected:
                record = get_manager(request).start_deletion(path)
                results.append({"path": path, "id": record.id, "status": "deleting"})
        else:

            def apply_metadata():
                with store.metadata_batch():
                    if (
                        payload.action == "restore"
                        and not payload.allow_public_exposure
                    ):
                        for path in selected:
                            if public_destination(store, path.rsplit("/", 1)[0] or "/"):
                                return JSONResponse(
                                    {
                                        "detail": (
                                            "Restoring here makes content "
                                            "publicly accessible."
                                        ),
                                        "code": "PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED",
                                    },
                                    status_code=409,
                                )
                    for path in selected:
                        store.set_trashed(path, payload.action == "trash")
                        results.append(
                            {
                                "path": path,
                                "status": "trashed"
                                if payload.action == "trash"
                                else "restored",
                            }
                        )
                return None

            response = await asyncio.to_thread(apply_metadata)
            if response is not None:
                return response
        return JSONResponse(
            {"results": results}, status_code=202 if payload.action == "delete" else 200
        )
