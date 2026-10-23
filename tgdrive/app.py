"""FastAPI application factory and compatibility HTTP API."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, cast

from argon2 import PasswordHasher
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tgdrive.api_v1 import register_routes
from tgdrive.backup import RecoveryConflict
from tgdrive.client_pool import (
    ClientPoolNotRunning,
    NoClientAvailable,
    current_upload_limit,
)
from tgdrive.config import Settings, get_settings
from tgdrive.connection_errors import TelegramConnectionError
from tgdrive.demo_access import is_demo_restricted, session_access
from tgdrive.demo_files import annotate, check_path, check_transfer, request_policy
from tgdrive.errors import (
    DriveError,
    DuplicateNameError,
    InvalidPathError,
    NotDirectoryError,
    NotFileError,
    NotFoundError,
    RootMutationError,
    TrashedItemError,
)
from tgdrive.errors import (
    ValidationError as DriveValidationError,
)
from tgdrive.http_guard import (
    ApiBodyGuardMiddleware,
    SameOriginMiddleware,
    SecurityHeadersMiddleware,
    UploadGuardMiddleware,
)
from tgdrive.logging import configure_logging
from tgdrive.previews import describe_media, describe_preview
from tgdrive.remote import RemoteError, RemoteTooLarge, UnsafeRemoteURL
from tgdrive.request_ids import (
    RequestCapacityError,
    RequestIdCancelled,
    RequestIdConflict,
    RequestIdNotFound,
    TransferStart,
    TransferStarts,
)
from tgdrive.runtime import Runtime
from tgdrive.schemas import (
    DirectoryRequest,
    FolderCreateRequest,
    ItemPathRequest,
    LoginRequest,
    RemoteInspectRequest,
    RemoteStartRequest,
    RenameRequest,
    TransferCancelRequest,
    TransferLookupRequest,
    TransferRequest,
    TrashRequest,
)
from tgdrive.security import (
    SESSION_COOKIE,
    InvalidSession,
    LoginLimiter,
    SessionManager,
    credential_session_key,
    demo_admin_session_key,
    verify_password,
)
from tgdrive.store import DriveStore
from tgdrive.streaming import (
    TelegramFileNotFound,
    TelegramVariantNotFound,
    stream_telegram_file,
)
from tgdrive.tasks import TransferNotFound, TransferRecord
from tgdrive.thumbnails import ThumbnailBusy, ThumbnailCache, load_thumbnail
from tgdrive.transfers import (
    BackupPending,
    TransferCancelled,
    TransferEmptyFile,
    TransferManager,
    TransferTooLarge,
)

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parent.parent
WEBSITE_DIR = PROJECT_DIR / "website"
STATIC_DIR = (
    WEBSITE_DIR / "dist" / "static"
    if (WEBSITE_DIR / "dist" / "static").exists()
    else WEBSITE_DIR / "public" / "static"
)


def _json_error(detail: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": detail, "detail": detail},
    )


def _runtime(request: Request) -> Any:
    return request.app.state.runtime


def _store(request: Request) -> DriveStore:
    store = getattr(_runtime(request), "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="TGDrive is not ready")
    return cast(DriveStore, store)


def _transfers(request: Request) -> TransferManager:
    manager = getattr(_runtime(request), "transfers", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="TGDrive is not ready")
    return cast(TransferManager, manager)


def _is_admin(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        request.app.state.sessions.verify(token)
        return True
    except InvalidSession:
        return False


async def require_admin(request: Request) -> None:
    if not _is_admin(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    request_policy(request)
    # Use the matched route so proxy root paths cannot bypass demo restrictions.
    path = request.scope["route"].path
    if is_demo_restricted(request):
        management_path = any(
            path == root or path.startswith(root + "/")
            for root in ("/api/v1/settings", "/api/v1/replica", "/api/v1/backup")
        )
        secret_export = path in {
            "/api/v1/replica/key",
            "/api/v1/backup/recovery-key",
        }
        if secret_export or (
            management_path and request.method not in {"GET", "HEAD", "OPTIONS"}
        ):
            from tgdrive.managed_settings import SettingsProblem

            raise SettingsProblem(
                "Configuration and management are read-only in the public demo.",
                code="DEMO_MODE_READ_ONLY",
                status=403,
            )
    runtime = _runtime(request)
    replica = getattr(runtime, "replica", None)
    if (
        getattr(runtime, "recovery_only", False)
        or (replica is not None and replica.blocked)
    ) and (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and not request.url.path.startswith("/api/v1/replica")
        and request.url.path not in {"/api/checkPassword", "/api/logout"}
        and not request.url.path.endswith("/cancel")
    ):
        raise HTTPException(
            409, "Channel recovery has paused file changes. Open Storage & Backups."
        )
    connections = getattr(runtime, "connection_manager", None)
    if connections is not None and connections.restart_required:
        allowed = (
            request.method in {"GET", "HEAD", "OPTIONS"}
            or request.url.path
            in {"/api/checkPassword", "/api/logout", "/api/getDirectory"}
            or request.url.path == "/api/v1/settings"
            or request.url.path.startswith("/api/v1/settings/premium-login/")
            or request.url.path.endswith("/cancel")
        )
        if not allowed:
            from tgdrive.managed_settings import SettingsProblem

            raise SettingsProblem(
                "Restart TGDrive to apply the saved connection settings "
                "before making changes.",
                code="SETTINGS_RESTART_REQUIRED",
                status=409,
            )
    service = getattr(runtime, "backup", None)
    if not getattr(service, "recovery_required", False):
        return
    path = request.url.path
    read_posts = {
        "/api/getDirectory",
        "/api/getFileInfoFromUrl",
        "/api/getTransferByRequest",
        "/api/getTransferProgress",
        "/api/getSaveProgress",
        "/api/getUploadProgress",
        "/api/getFileDownloadProgress",
        "/api/logout",
    }
    if request.method in {"GET", "HEAD", "OPTIONS"} or path in read_posts:
        return
    if path == "/api/cancelUpload" or path.endswith("/cancel"):
        return
    raise RecoveryConflict(
        "This installation needs recovery before changes are allowed. "
        "Preserve its local data and recover the latest Telegram checkpoint."
    )


Admin = Annotated[None, Depends(require_admin)]


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Any | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    from tgdrive.replica_activation import ActivationError, effective_settings

    activation_unavailable = False
    try:
        settings = effective_settings(settings)
    except ActivationError:
        activation_unavailable = True
    configure_logging(settings.log_level)
    app_runtime = runtime or Runtime(settings)
    if activation_unavailable:
        app_runtime._activation_unavailable = True
    owner_credential = (
        settings.admin_password_hash.get_secret_value()
        if settings.admin_password_hash
        else settings.admin_password.get_secret_value()
    )
    demo_credential = (
        settings.demo_admin_password_hash or settings.demo_admin_password
        if settings.demo_mode
        else None
    )
    sessions = SessionManager(
        credential_session_key(settings.session_key, owner_credential),
        settings.session_ttl_seconds,
        settings.resolved_data_dir / "auth.sqlite3",
        demo_admin_key=(
            demo_admin_session_key(
                settings.session_key, demo_credential.get_secret_value()
            )
            if demo_credential
            else None
        ),
    )
    password_hash = (
        settings.admin_password_hash.get_secret_value()
        if settings.admin_password_hash
        else PasswordHasher().hash(settings.admin_password.get_secret_value())
    )
    demo_password_hash = (
        settings.demo_admin_password_hash.get_secret_value()
        if settings.demo_admin_password_hash and demo_credential
        else PasswordHasher().hash(demo_credential.get_secret_value())
        if demo_credential
        else None
    )
    limiter = LoginLimiter()
    # Argon2 is deliberately expensive. Bound its memory/CPU use and keep it off
    # the HTTP event loop so a login does not stall streams and health checks.
    login_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            await _app.state.thumbnail_repair.start()
            await app_runtime.start()
            if settings.demo_mode and getattr(app_runtime, "store", None) is not None:
                try:
                    await asyncio.to_thread(app_runtime.store.ensure_demo_files)
                except BaseException:
                    await app_runtime.stop()
                    raise
            from tgdrive.traffic import checkpoint_history, sample_performance

            sampler = asyncio.create_task(
                sample_performance(
                    _app.state.traffic,
                    getattr(app_runtime, "pool", None),
                    history=_app.state.telemetry_history,
                ),
                name="tgdrive-performance-sampler",
            )
            try:
                yield
            finally:
                sampler.cancel()
                with suppress(asyncio.CancelledError):
                    await sampler
                try:
                    await checkpoint_history(
                        _app.state.traffic,
                        getattr(app_runtime, "pool", None),
                        _app.state.telemetry_history,
                        force=True,
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Could not checkpoint performance history on shutdown"
                    )
                login_service = getattr(_app.state, "premium_login_service", None)
                if login_service is not None:
                    await login_service.close()
                connection_manager = getattr(app_runtime, "connection_manager", None)
                if connection_manager is not None:
                    await connection_manager.close()
                await _app.state.thumbnails.close()
                await app_runtime.stop()
        finally:
            sessions.close()

    app = FastAPI(
        title="TGDrive",
        version="2.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.runtime = app_runtime
    app.state.sessions = sessions
    app.state.login_limiter = limiter
    app.state.thumbnails = ThumbnailCache()
    from tgdrive.generated_thumbnails import CachedThumbnailRepair

    app.state.thumbnail_repair = CachedThumbnailRepair(
        settings.resolved_cache_dir / "thumbnails"
    )
    starts = TransferStarts(ttl_seconds=settings.transfer_ttl_seconds)
    app.state.transfer_starts = starts

    from tgdrive.mongo import DatabaseUnavailable

    @app.exception_handler(DatabaseUnavailable)
    async def database_unavailable(_request: Request, _error: DatabaseUnavailable):
        return JSONResponse(
            {
                "detail": "MongoDB is unavailable. Check its connection and retry.",
                "code": "DATABASE_UNAVAILABLE",
            },
            status_code=503,
        )

    from pymongo.errors import PyMongoError

    app.add_exception_handler(PyMongoError, database_unavailable)

    @app.exception_handler(RecoveryConflict)
    async def recovery_conflict(
        _request: Request, exc: RecoveryConflict
    ) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=409)

    @app.exception_handler(BackupPending)
    async def checkpoint_pending(_request: Request, exc: BackupPending) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=409)

    @app.exception_handler(DriveError)
    async def drive_error_handler(_request: Request, exc: DriveError) -> JSONResponse:
        if isinstance(exc, DuplicateNameError):
            code = 409
        elif isinstance(
            exc,
            (NotFoundError, NotDirectoryError, NotFileError, TrashedItemError),
        ):
            code = 404
        elif isinstance(
            exc,
            (DriveValidationError, InvalidPathError, RootMutationError),
        ):
            code = 400
        else:
            code = 500
            logger.error(
                "Unexpected drive error type: %s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        return _json_error(str(exc), code)

    @app.exception_handler(TelegramConnectionError)
    async def telegram_connection_error(
        _request: Request, exc: TelegramConnectionError
    ) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=503)

    @app.exception_handler(ClientPoolNotRunning)
    @app.exception_handler(NoClientAvailable)
    async def telegram_unavailable(_request: Request, _exc: Exception) -> JSONResponse:
        if getattr(_exc, "code", None) == "TELEGRAM_QUEUE_FULL":
            return JSONResponse(
                {
                    "detail": "Telegram is busy and its waiting queue is full. "
                    "Retry shortly.",
                    "code": "TELEGRAM_QUEUE_FULL",
                },
                status_code=503,
                headers={"Retry-After": str(getattr(_exc, "retry_after", 1))},
            )
        return _json_error(
            "No usable Telegram account is connected. Open Connections and Refresh "
            "to check bot tokens, Premium sessions and storage-channel permissions. "
            "Restore the connection, then retry.",
            503,
        )

    from tgdrive.staging import StorageUnavailable

    @app.exception_handler(StorageUnavailable)
    async def staging_error_handler(_request: Request, exc: StorageUnavailable):
        return _json_error(str(exc), exc.status)

    @app.exception_handler(RemoteError)
    async def remote_error_handler(_request: Request, exc: RemoteError) -> JSONResponse:
        if isinstance(exc, RemoteTooLarge):
            code = 413
        elif isinstance(exc, UnsafeRemoteURL):
            code = 400
        else:
            code = 502
        return _json_error(str(exc), code)

    @app.exception_handler(TransferNotFound)
    @app.exception_handler(RequestIdNotFound)
    async def transfer_not_found_handler(
        _request: Request, _exc: TransferNotFound
    ) -> JSONResponse:
        return _json_error("Transfer not found", 404)

    @app.exception_handler(RequestIdConflict)
    @app.exception_handler(RequestIdCancelled)
    async def transfer_start_conflict(
        _request: Request, exc: RequestIdConflict | RequestIdCancelled
    ) -> JSONResponse:
        return _json_error(str(exc), 409)

    @app.exception_handler(RequestCapacityError)
    async def transfer_capacity_error(
        _request: Request, exc: RequestCapacityError
    ) -> JSONResponse:
        return _json_error(str(exc), 503)

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # SecretStr values stay hidden; avoid echoing rejected input as well.
        message = "; ".join(error["msg"] for error in exc.errors())
        return _json_error(message or "Invalid request", 422)

    @app.get("/")
    async def home_page() -> Response:
        index = WEBSITE_DIR / "dist" / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-store"})
        return HTMLResponse(
            "<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>TGDrive setup</title>"
            "<link rel='icon' type='image/svg+xml' href='/static/brand/favicon.svg'>"
            "<main><p><img src='/static/brand/tgdrive-logo.svg' "
            "width='251' height='50' alt='TGDrive'></p>"
            "<h1>Build the TGDrive interface</h1>"
            "<p>In the website folder, run <code>npm ci</code>, then "
            "<code>npm run build</code>, and restart TGDrive.</p></main></html>",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/stream")
    async def video_player(
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        auth: Annotated[str | None, Query(max_length=512)] = None,
    ) -> FileResponse:
        # Authorization happens when the native player requests /file.
        del path, auth
        return FileResponse(
            WEBSITE_DIR / "dist" / "VideoPlayer.html"
            if (WEBSITE_DIR / "dist" / "VideoPlayer.html").exists()
            else WEBSITE_DIR / "public" / "VideoPlayer.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    @app.get("/healthz")
    async def health_ready(request: Request) -> JSONResponse:
        current_runtime = _runtime(request)
        ready = bool(getattr(current_runtime, "ready", False))
        pool_health = getattr(getattr(current_runtime, "pool", None), "health", None)
        if ready and callable(pool_health):
            health = await pool_health()
            ready = bool(health.ready)
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ok" if ready else "not ready"},
        )

    @app.get("/api/config")
    async def public_config(request: Request, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        access = session_access(request)
        return {
            "status": "ok",
            "max_file_size": await current_upload_limit(
                settings, getattr(_runtime(request), "pool", None)
            ),
            "timezone": settings.timezone,
            "demo_mode": settings.demo_mode,
            "demo_read_only": access["demo_read_only"],
            "demo_admin": access["demo_admin"],
            "recovery_only": bool(getattr(_runtime(request), "recovery_only", False)),
        }

    @app.post("/api/checkPassword")
    async def check_password(payload: LoginRequest, request: Request) -> JSONResponse:
        client_key = request.client.host if request.client else "unknown"
        decision = limiter.check(client_key)
        if not decision.allowed:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(decision.retry_after)},
                content={
                    "status": "Too many login attempts",
                    "detail": "Too many login attempts",
                },
            )
        expected = password_hash
        if login_lock.locked():
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": "1"},
                content={
                    "status": "Login busy; retry shortly",
                    "detail": "Login busy; retry shortly",
                    "code": "LOGIN_BUSY",
                },
            )
        async with login_lock:
            valid = await asyncio.to_thread(verify_password, payload.password, expected)
            demo_admin = False
            if not valid and demo_password_hash is not None:
                demo_admin = await asyncio.to_thread(
                    verify_password, payload.password, demo_password_hash
                )
                valid = demo_admin
        if not valid:
            limiter.failure(client_key)
            return _json_error("Invalid password", 401)
        limiter.success(client_key)
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            SESSION_COOKIE,
            await asyncio.to_thread(sessions.create, demo_admin=demo_admin),
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request) -> JSONResponse:
        # Invalid cookies must not turn this public route into database work.
        if _is_admin(request):
            await asyncio.to_thread(sessions.revoke, request.cookies[SESSION_COOKIE])
        response = JSONResponse({"status": "ok"})
        response.delete_cookie(
            SESSION_COOKIE,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/getDirectory")
    async def get_directory(
        payload: DirectoryRequest, request: Request
    ) -> JSONResponse:
        if _is_admin(request):
            await require_admin(request)
        store = _store(request)
        requested = payload.path
        auth_home_path: str | None = None
        if requested == "/trash":
            await require_admin(request)
            data: dict[str, Any] = {"contents": store.list_trash()}
        elif requested.startswith("/search_"):
            await require_admin(request)
            query = requested.removeprefix("/search_")
            page = store.search_page(query, limit=500)
            data = {
                "contents": {row["id"]: row for row in page["items"]},
                "search": {key: value for key, value in page.items() if key != "items"},
            }
        elif requested.startswith("/share_"):
            if getattr(
                getattr(_runtime(request), "backup", None), "recovery_required", False
            ):
                raise RecoveryConflict(
                    "Shared access is paused until recovery completes"
                )
            real_path = "/" + requested.removeprefix("/share_").lstrip("/")
            if real_path != "/":
                real_path = store.resolve_id(real_path.rstrip("/").rsplit("/", 1)[-1])
            auth_home_path = store.authorize_directory(real_path, payload.auth or "")
            if auth_home_path is None:
                raise HTTPException(status_code=403, detail="Invalid share link")
            data = store.get_directory(real_path)
            for item in data.get("contents", {}).values():
                item.pop("starred", None)
        else:
            await require_admin(request)
            data = store.get_directory(requested)
        if settings.demo_mode:
            annotate(data)
        return JSONResponse(
            {
                "status": "ok",
                "data": data,
                "auth_home_path": auth_home_path,
            }
        )

    @app.post("/api/createNewFolder")
    async def create_folder(
        payload: FolderCreateRequest,
        request: Request,
        _admin: Admin,
    ) -> dict[str, str]:
        check_path(_store(request), payload.path)
        await asyncio.to_thread(
            _store(request).create_folder, payload.path, payload.name
        )
        return {"status": "ok"}

    @app.post("/api/renameFileFolder")
    async def rename_item(
        payload: RenameRequest,
        request: Request,
        _admin: Admin,
    ) -> dict[str, str]:
        check_path(_store(request), payload.path, descendants=True)
        await asyncio.to_thread(_store(request).rename, payload.path, payload.name)
        return {"status": "ok"}

    @app.post("/api/trashFileFolder")
    async def trash_item(
        payload: TrashRequest,
        request: Request,
        _admin: Admin,
    ) -> dict[str, str]:
        check_path(_store(request), payload.path, descendants=True)
        await asyncio.to_thread(
            _store(request).set_trashed, payload.path, payload.trash
        )
        return {"status": "ok"}

    @app.post("/api/deleteFileFolder")
    async def delete_item(
        payload: ItemPathRequest,
        request: Request,
        _admin: Admin,
    ) -> dict[str, str]:
        store = _store(request)
        check_path(store, payload.path, descendants=True)
        manager = _transfers(request)
        normalized_path = "/" + payload.path.strip("/")
        trash_paths = {
            (
                f"{item['path'].rstrip('/')}/{item['id']}"
                if item["path"] != "/"
                else f"/{item['id']}"
            )
            for item in store.list_trash().values()
        }
        if normalized_path not in trash_paths:
            raise HTTPException(
                status_code=409,
                detail="Item must be in trash before permanent deletion",
            )
        record = manager.start_deletion(payload.path)
        return JSONResponse({"status": "ok", "id": record.id}, status_code=202)

    async def create_share(
        payload: ItemPathRequest,
        request: Request,
        expected_type: str,
    ) -> dict[str, str]:
        store = _store(request)
        check_path(store, payload.path, descendants=True)
        if expected_type == "folder":
            store.get_directory(payload.path)
        else:
            store.get_file(payload.path)
        from tgdrive.share_links import token_factory

        token = await asyncio.to_thread(
            store.create_share,
            payload.path,
            token_factory=token_factory(settings),
        )
        return {"status": "ok", "auth": token}

    @app.post("/api/getFolderShareAuth")
    async def folder_share(
        payload: ItemPathRequest,
        request: Request,
        _admin: Admin,
    ) -> dict[str, str]:
        return await create_share(payload, request, "folder")

    @app.post("/api/getFileShareAuth")
    async def file_share(
        payload: ItemPathRequest,
        request: Request,
        _admin: Admin,
    ) -> dict[str, str]:
        return await create_share(payload, request, "file")

    @app.post("/api/upload")
    async def upload_file(
        request: Request,
        _admin: Admin,
        file: Annotated[UploadFile, File()],
        path: Annotated[str, Form(min_length=1, max_length=4096)],
        client_request_id: Annotated[
            str | None,
            Form(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        ] = None,
    ) -> JSONResponse:
        check_path(_store(request), path)
        manager = _transfers(request)
        entry: TransferStart | None = None
        if client_request_id:
            entry, created = starts.claim(
                client_request_id,
                ("local", path, file.filename or "", str(file.size)),
            )
            if not created:
                await file.close()
                snapshot = entry.snapshot()
                return JSONResponse(
                    {"status": "ok", "id": snapshot["id"], "data": snapshot}
                )

        def bind_created(record: TransferRecord) -> None:
            if entry is not None and starts.bind(entry, record):
                manager.registry.cancel(record.id)

        try:
            record = await manager.receive_browser_upload(
                file,
                path,
                is_disconnected=request.is_disconnected,
                on_created=bind_created,
            )
        except TransferTooLarge as exc:
            if entry:
                starts.fail(entry, str(exc))
            return _json_error(str(exc), 413)
        except TransferEmptyFile as exc:
            if entry:
                starts.fail(entry, str(exc))
            return _json_error(str(exc), 422)
        except TransferCancelled as exc:
            if entry:
                starts.cancel(entry.request_id)
            return _json_error(str(exc), 409)
        except Exception as exc:
            if entry:
                starts.fail(entry, str(exc) or type(exc).__name__)
            raise
        if entry and entry.record is None:
            bind_created(record)
        return JSONResponse({"status": "ok", "id": record.id})

    @app.post("/api/getFileInfoFromUrl")
    async def inspect_remote(
        payload: RemoteInspectRequest,
        request: Request,
        _admin: Admin,
    ) -> dict[str, Any]:
        info = await _transfers(request).inspect_remote(str(payload.url))
        return {
            "status": "ok",
            "data": {
                "file_name": info.filename,
                "file_size": info.size if info.size is not None else -1,
                "content_type": info.content_type,
            },
        }

    @app.post("/api/startFileDownloadFromUrl")
    async def start_remote(
        payload: RemoteStartRequest,
        request: Request,
        _admin: Admin,
    ) -> dict[str, Any]:
        check_path(_store(request), payload.path)
        entry: TransferStart | None = None
        if payload.client_request_id:
            entry, created = starts.claim(
                payload.client_request_id,
                (
                    "remote",
                    payload.path,
                    str(payload.url),
                    payload.filename or "",
                    str(payload.single_threaded),
                ),
            )
            if not created:
                snapshot = entry.snapshot()
                return {"status": "ok", "id": snapshot["id"], "data": snapshot}
        try:
            manager = _transfers(request)
            # Source DNS, headers and size admission belong to the cancellable
            # server job. Return its identity without waiting on an external host.
            record = manager.start_remote(
                url=str(payload.url),
                destination=payload.path,
                filename=payload.filename,
                single_threaded=payload.single_threaded,
            )
        except Exception as exc:
            if entry:
                starts.fail(entry, str(exc) or type(exc).__name__)
            raise
        if entry:
            starts.bind(entry, record)
        return {"status": "ok", "id": record.id}

    @app.post("/api/getTransferByRequest")
    async def transfer_by_request(
        payload: TransferLookupRequest, request: Request, _admin: Admin
    ) -> dict[str, Any]:
        del request
        snapshot = starts.get(payload.client_request_id).snapshot()
        return {"status": "ok", "id": snapshot["id"], "data": snapshot}

    @app.post("/api/getTransferProgress")
    async def transfer_progress(
        payload: TransferRequest,
        request: Request,
        _admin: Admin,
    ) -> dict[str, Any]:
        record = _transfers(request).registry.get(payload.id)
        return {"status": "ok", "data": record.as_dict()}

    @app.post("/api/cancelUpload")
    async def cancel_transfer(
        payload: TransferCancelRequest,
        request: Request,
        _admin: Admin,
    ) -> dict[str, Any]:
        manager = _transfers(request)
        cancel = getattr(manager, "cancel_job", manager.registry.cancel)
        if payload.client_request_id:
            try:
                existing = starts.get(payload.client_request_id)
            except RequestIdNotFound:
                existing = None
            # Refuse a committed upload before setting any cancellation intent.
            if existing is not None and existing.record is not None:
                check_transfer(_store(request), existing.record)
                cancel(existing.record.id)
            entry = starts.cancel(payload.client_request_id)
            snapshot = entry.snapshot()
        else:
            assert payload.id is not None
            check_transfer(_store(request), manager.registry.get(payload.id))
            snapshot = cancel(payload.id).as_dict()
        return {"status": "ok", "id": snapshot["id"], "data": snapshot}

    # Compatibility aliases for older clients, now backed by one state machine.
    app.add_api_route(
        "/api/getSaveProgress",
        transfer_progress,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/api/getUploadProgress",
        transfer_progress,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/api/getFileDownloadProgress",
        transfer_progress,
        methods=["POST"],
        include_in_schema=False,
    )

    def authorized_file(
        request: Request, path: str, auth: str | None
    ) -> tuple[str, dict[str, Any]]:
        store = _store(request)
        if getattr(
            getattr(_runtime(request), "backup", None), "recovery_required", False
        ):
            raise RecoveryConflict(
                "File access is paused until this installation is recovered. "
                "Preserve local data and recover the latest Telegram checkpoint."
            )
        if path != "/":
            path = store.resolve_id(path.rstrip("/").rsplit("/", 1)[-1])
        if auth is not None or not _is_admin(request):
            try:
                authorized = store.authorize_file(path, auth or "")
            except DriveError:
                authorized = False
            if not authorized:
                raise HTTPException(status_code=403, detail="Invalid share link")
        return path, store.get_file(path)

    @app.get("/api/v1/preview")
    async def file_preview(
        request: Request,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        auth: Annotated[str | None, Query(max_length=512)] = None,
    ) -> dict[str, Any]:
        path, file_data = authorized_file(request, path, auth)
        try:
            result = await describe_preview(
                pool=_runtime(request).pool,
                channel_id=settings.storage_channel,
                message_id=file_data["message_id"],
                parts=file_data.get("parts"),
                file_name=file_data["name"],
                size=file_data["size"],
                path=path,
                auth=auth,
            )
            authorized_file(request, path, auth)
            return result
        except TelegramFileNotFound as exc:
            raise HTTPException(404, exc.public_detail) from exc

    @app.get("/api/v1/books")
    async def book_preview(
        request: Request,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        auth: Annotated[str | None, Query(max_length=512)] = None,
        entry: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
    ) -> Response:
        import base64

        from tgdrive.books import parse_book, read_range

        path, data = authorized_file(request, path, auth)
        pending = getattr(app.state, "book_requests", 0)
        if pending >= 16:
            raise HTTPException(
                503,
                "The reader is busy. Try again shortly.",
                headers={"Retry-After": "2"},
            )
        semaphore = getattr(app.state, "book_slots", None)
        if semaphore is None:
            semaphore = app.state.book_slots = asyncio.Semaphore(2)
        app.state.book_requests = pending + 1
        acquired = False

        async def authorize():
            _, current = await asyncio.to_thread(authorized_file, request, path, auth)
            if any(
                current.get(key) != data.get(key)
                for key in ("message_id", "size", "parts")
            ):
                raise HTTPException(409, "The stored book changed. Reopen it.")

        async def fetch(start, count):
            return await read_range(
                _runtime(request).pool,
                settings.storage_channel,
                data,
                start,
                count,
                authorize,
            )

        try:
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=10)
                acquired = True
            except TimeoutError as exc:
                raise HTTPException(
                    503,
                    "The reader is busy. Try again shortly.",
                    headers={"Retry-After": "2"},
                ) from exc
            await authorize()
            result = await parse_book(data, fetch, entry)
            await authorize()
            headers = {
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            }
            if result.get("kind") == "image":
                content = base64.b64decode(result["data"], validate=True)
                return Response(content, media_type=result["mime"], headers=headers)
            return JSONResponse(result, headers=headers)
        finally:
            if acquired:
                semaphore.release()
            app.state.book_requests -= 1

    @app.get("/api/v1/thumbnail")
    async def file_thumbnail(
        request: Request,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        auth: Annotated[str | None, Query(max_length=512)] = None,
    ) -> Response:
        path, file_data = authorized_file(request, path, auth)
        # Prefer upload-generated/provider covers; missing derivatives can use
        # a complete verified original already cached, without origin reads.
        headers = {"Cache-Control": "private, no-cache", "Vary": "Cookie"}

        def file_identity(data):
            return (
                data["message_id"],
                data["size"],
                tuple(
                    (part["message_id"], part["offset"], part["size"], part["sha256"])
                    for part in data.get("parts") or []
                ),
            )

        identity = file_identity(file_data)
        key = (settings.storage_channel, *identity)
        pool = _runtime(request).pool
        cache = getattr(pool, "_media_read_cache", None)
        source = {
            "channel_id": settings.storage_channel,
            "message_id": file_data["message_id"],
            "size": file_data["size"],
            "parts": file_data.get("parts"),
        }
        version = cache.cached_source_version(**source) if cache is not None else None

        def recheck():
            _current_path, current = authorized_file(request, path, auth)
            if file_identity(current) != identity:
                raise HTTPException(409, "File changed; refresh the directory")

        async def load_cover():
            from tgdrive.generated_thumbnails import read
            from tgdrive.thumbnails import Thumbnail

            content = await asyncio.to_thread(
                read,
                settings.resolved_cache_dir / "thumbnails",
                settings.storage_channel,
                file_data["message_id"],
            )
            if content:
                return Thumbnail(content, "image/jpeg")
            provider_error = None
            try:
                provider = await load_thumbnail(
                    pool=pool,
                    channel_id=settings.storage_channel,
                    message_id=file_data["message_id"],
                )
                if provider is not None:
                    return provider
            except ThumbnailBusy as exc:
                provider_error = exc
            if cache is not None:
                content = await app.state.thumbnail_repair.get(
                    cache,
                    source=source,
                    name=file_data["name"],
                    version=version,
                    authorize=recheck,
                )
                if content:
                    return Thumbnail(content, "image/jpeg")
            if provider_error is not None:
                raise provider_error
            return None

        try:
            thumbnail = await app.state.thumbnails.get(
                key,
                load_cover,
                missing_revision=version.data_revision if version is not None else None,
            )
        except ThumbnailBusy:
            recheck()
            return JSONResponse(
                {
                    "code": "THUMBNAIL_BUSY",
                    "detail": "Thumbnail temporarily unavailable",
                },
                status_code=503,
                headers={**headers, "Retry-After": "5"},
            )
        # Even a cache hit or conditional 304 must pass today's share/session
        # authorization. Nothing in the payload cache grants access to a file.
        recheck()
        if thumbnail is None:
            return JSONResponse(
                {"code": "THUMBNAIL_UNAVAILABLE", "detail": "No thumbnail available"},
                status_code=404,
                headers=headers,
            )
        headers["ETag"] = thumbnail.etag
        if request.headers.get("if-none-match") == thumbnail.etag:
            return Response(status_code=304, headers=headers)
        return Response(
            thumbnail.content, media_type=thumbnail.media_type, headers=headers
        )

    @app.get("/api/v1/media")
    async def file_media(
        request: Request,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        auth: Annotated[str | None, Query(max_length=512)] = None,
    ) -> dict[str, Any]:
        _path, file_data = authorized_file(request, path, auth)
        try:
            result = await describe_media(
                pool=_runtime(request).pool,
                channel_id=settings.storage_channel,
                message_id=file_data["message_id"],
                parts=file_data.get("parts"),
                file_name=file_data["name"],
                size=file_data["size"],
            )
            authorized_file(request, _path, auth)
            return result
        except TelegramFileNotFound as exc:
            raise HTTPException(404, exc.public_detail) from exc

    @app.api_route("/file", methods=["GET", "HEAD"])
    async def download_file(
        request: Request,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        auth: Annotated[str | None, Query(max_length=512)] = None,
        download: bool = False,
        preview: bool = False,
        quality: Annotated[str | None, Query(max_length=128)] = None,
        request_id: Annotated[
            str | None, Query(max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
        ] = None,
    ) -> Response:
        path, file_data = authorized_file(request, path, auth)

        pool = _runtime(request).pool
        if request_id:
            requests = getattr(pool, "_request_files", None)
            if requests is None:
                requests = {}
                try:
                    pool._request_files = requests
                except AttributeError:
                    requests = None
            if requests is not None:
                requests[request_id] = (
                    settings.storage_channel,
                    file_data["message_id"],
                    file_data["size"],
                )
                while len(requests) > 4096:
                    requests.pop(next(iter(requests)))

        async def authorize():
            _, current = await asyncio.to_thread(authorized_file, request, path, auth)
            if any(
                current.get(key) != file_data.get(key)
                for key in ("message_id", "size", "parts")
            ):
                raise HTTPException(
                    409, "The stored file changed. Restart the download."
                )

        if request.method == "HEAD" and quality is None:
            import mimetypes

            from tgdrive.ranges import (
                MalformedRange,
                RangeNotSatisfiable,
                parse_byte_range,
            )
            from tgdrive.streaming import (
                _content_disposition,
                _not_modified,
                _request_range,
                media_etag,
            )

            total = file_data["size"]
            tag = media_etag(
                settings.storage_channel,
                file_data["message_id"],
                total,
                parts=file_data.get("parts"),
            )
            mime = (
                mimetypes.guess_type(file_data["name"])[0] or "application/octet-stream"
            )
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Encoding": "identity",
                "ETag": tag,
                "Cache-Control": "private, no-store, no-transform",
                "Content-Disposition": _content_disposition(
                    file_data["name"], mime, download=download, preview=preview
                ),
            }
            if _not_modified(request, tag):
                return Response(status_code=304, headers=headers)
            try:
                selected = parse_byte_range(_request_range(request, tag), total)
            except MalformedRange:
                return Response(status_code=400, headers=headers)
            except RangeNotSatisfiable as exc:
                return Response(
                    status_code=416,
                    headers=headers | {"Content-Range": exc.content_range},
                )
            headers["Content-Length"] = str(selected.length)
            if selected.content_range:
                headers["Content-Range"] = selected.content_range
            return Response(
                status_code=206 if selected.is_partial else 200,
                headers=headers,
                media_type=mime,
            )
        try:
            return await stream_telegram_file(
                pool=_runtime(request).pool,
                channel_id=settings.storage_channel,
                message_id=file_data["message_id"],
                parts=file_data.get("parts"),
                file_name=file_data["name"],
                expected_size=file_data["size"],
                request=request,
                download=download,
                preview=preview,
                quality=quality,
                authorize=authorize,
                request_id=request_id,
            )
        except TelegramVariantNotFound as exc:
            raise HTTPException(404, "This media quality is unavailable") from exc
        except TelegramFileNotFound as exc:
            raise HTTPException(status_code=404, detail=exc.public_detail) from exc

    @app.get("/api/v1/file-status")
    async def file_status(
        request: Request,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        request_id: Annotated[
            str, Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
        ],
        auth: Annotated[str | None, Query(max_length=512)] = None,
    ):
        _, data = authorized_file(request, path, auth)
        pool = _runtime(request).pool
        estimate = getattr(pool, "queue_estimate", None)
        return JSONResponse(
            {
                "queue": estimate(request_id)
                if callable(estimate)
                and getattr(pool, "_request_files", {}).get(request_id)
                == (settings.storage_channel, data["message_id"], data["size"])
                else None,
                "size": data["size"],
            },
            headers={"Cache-Control": "no-store"},
        )

    from tgdrive.replica_api import register_replica_routes

    register_replica_routes(app, settings, require_admin, _runtime)
    register_routes(
        app,
        settings,
        require_admin=require_admin,
        is_admin=_is_admin,
        get_store=_store,
        get_manager=_transfers,
        get_runtime=_runtime,
    )
    if (WEBSITE_DIR / "dist" / "assets").exists():
        app.mount(
            "/assets",
            StaticFiles(directory=WEBSITE_DIR / "dist" / "assets"),
            name="assets",
        )
    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR, check_dir=True),
        name="static",
    )
    notices = WEBSITE_DIR / "dist" / "third-party"
    if not notices.is_dir():
        notices = WEBSITE_DIR / "public" / "third-party"
    if notices.is_dir():
        app.mount(
            "/third-party",
            StaticFiles(directory=notices, check_dir=True),
            name="third-party",
        )
    app.add_middleware(
        UploadGuardMiddleware,
        sessions=sessions,
        upload_limit=settings.upload_limit,
        max_concurrent=settings.max_concurrent_transfers,
    )
    from tgdrive.traffic import TrafficCounters, TrafficMiddleware

    app.state.traffic = TrafficCounters()
    from tgdrive.telemetry_history import TelemetryHistory

    app.state.telemetry_history = TelemetryHistory(settings.resolved_data_dir)
    app.add_middleware(TrafficMiddleware, counters=app.state.traffic)
    app.add_middleware(SameOriginMiddleware)
    app.add_middleware(ApiBodyGuardMiddleware, sessions=sessions)
    app.add_middleware(SecurityHeadersMiddleware, secure=settings.cookie_secure)
    return app
