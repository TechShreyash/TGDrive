"""Authenticate and bound multipart bodies before FastAPI starts parsing them."""

from __future__ import annotations

import asyncio
import logging
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tgdrive.core.security import SESSION_COOKIE, InvalidSession, SessionManager

logger = logging.getLogger(__name__)


def _authenticated(scope: Scope, sessions: SessionManager) -> bool:
    try:
        cookies: SimpleCookie[str] = SimpleCookie()
        for key, value in scope.get("headers", []):
            if key.lower() == b"cookie":
                cookies.load(value.decode("latin-1"))
        token = cookies.get(SESSION_COOKIE)
        if token is None:
            return False
        sessions.verify(token.value)
    except InvalidSession, CookieError, ValueError:
        return False
    return True


class ApiBodyGuardMiddleware:
    """Bound JSON requests and authenticate private routes before body decoding."""

    _PUBLIC_POST_PATHS = frozenset(
        {
            "/api/checkPassword",
            "/api/logout",
            "/api/getDirectory",
        }
    )

    def __init__(
        self, app: ASGIApp, *, sessions: SessionManager, body_limit: int = 65536
    ) -> None:
        self.app = app
        self.sessions = sessions
        self.body_limit = body_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or not path.startswith("/api/")
            or path == "/api/upload"
            or (path.startswith("/api/v1/uploads/") and scope.get("method") == "PATCH")
            or scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            await self.app(scope, receive, send)
            return
        if path not in self._PUBLIC_POST_PATHS and not _authenticated(
            scope, self.sessions
        ):
            await UploadGuardMiddleware._reject(
                scope, receive, send, "Not authenticated", 401
            )
            return
        body_limit = self.body_limit
        if path == "/api/v1/backup/restore" and scope.get("method") == "POST":
            from tgdrive.recovery.mongo_backup import MAX_EXPANDED

            body_limit = MAX_EXPANDED
        elif path == "/api/v1/replica/import" and scope.get("method") == "POST":
            from tgdrive.replication.replica_state import MAX_BUNDLE

            body_limit = MAX_BUNDLE
        elif (
            path == "/api/v1/transfers/history/tasks" and scope.get("method") == "POST"
        ):
            body_limit = max(body_limit, 256 * 1024)
        lengths = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == b"content-length"
        ]
        if lengths:
            try:
                parsed = [int(value) for value in lengths]
                if len(set(parsed)) != 1 or parsed[0] < 0:
                    raise ValueError("Invalid Content-Length")
            except ValueError:
                await UploadGuardMiddleware._reject(
                    scope, receive, send, "Invalid Content-Length", 400
                )
                return
            if parsed[0] > body_limit:
                if path == "/api/v1/backup/restore":
                    logger.warning(
                        "Database archive restore rejected: request exceeds "
                        "the 1 GiB limit"
                    )
                await UploadGuardMiddleware._reject(
                    scope, receive, send, "API request body is too large", 413
                )
                return
        count = 0
        too_large = False
        rejected = False

        async def limited_receive() -> Message:
            nonlocal count, too_large
            message = await receive()
            if message["type"] == "http.request":
                count += len(message.get("body", b""))
                if count > body_limit:
                    too_large = True
                    if path == "/api/v1/backup/restore":
                        logger.warning(
                            "Database archive restore rejected: upload exceeds "
                            "the 1 GiB limit"
                        )
                    raise HTTPException(413, "API request body is too large")
            return message

        async def limited_send(message: Message) -> None:
            nonlocal rejected
            if too_large:
                if not rejected:
                    rejected = True
                    await UploadGuardMiddleware._reject(
                        scope, receive, send, "API request body is too large", 413
                    )
                return
            await send(message)

        await self.app(scope, limited_receive, limited_send)


class SameOriginMiddleware:
    """Reject browser-originated mutations from another origin, including subdomains."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _matches_origin(raw: bytes, scope: Scope, *, referrer: bool = False) -> bool:
        headers = scope.get("headers", [])
        hosts = [value for name, value in headers if name.lower() == b"host"]
        if len(hosts) != 1:
            return False
        try:
            origin = urlsplit(raw.decode("ascii"))
            host = urlsplit("//" + hosts[0].decode("ascii"))
            scheme = scope.get("scheme", "http")
            default_port = 443 if scheme == "https" else 80
            return (
                origin.scheme == scheme
                and origin.hostname == host.hostname
                and (origin.port if origin.port is not None else default_port)
                == (host.port if host.port is not None else default_port)
                and (referrer or (origin.path in {"", "/"} and not origin.query))
                and not origin.fragment
                and origin.username is None
                and host.username is None
                and not host.path
                and not host.query
                and not host.fragment
                and host.hostname is not None
            )
        except UnicodeError, ValueError:
            return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("path", "").startswith("/api/")
            and scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            raw_headers = scope.get("headers", [])
            headers = dict(raw_headers)
            origins = [
                value for name, value in raw_headers if name.lower() == b"origin"
            ]
            referrers = [
                value for name, value in raw_headers if name.lower() == b"referer"
            ]
            if origins:
                same = len(origins) == 1 and self._matches_origin(origins[0], scope)
            elif headers.get(b"sec-fetch-site") in {b"cross-site", b"same-site"}:
                # SameSite cookies include sibling subdomains. Without Origin,
                # Fetch Metadata must not treat those siblings as trusted.
                same = False
            elif referrers:
                same = len(referrers) == 1 and self._matches_origin(
                    referrers[0], scope, referrer=True
                )
            else:
                # CLI/API clients do not send browser-origin headers.
                same = True
            if not same:
                await UploadGuardMiddleware._reject(
                    scope, receive, send, "Cross-origin requests are not allowed", 403
                )
                return
        await self.app(scope, receive, send)


class UploadGuardMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        sessions: SessionManager,
        upload_limit: int,
        max_concurrent: int,
    ) -> None:
        self.app = app
        self.sessions = sessions
        self.body_limit = upload_limit + 2 * 1024**2
        self.slots = asyncio.Semaphore(max_concurrent)

    @staticmethod
    async def _reject(
        scope: Scope, receive: Receive, send: Send, detail: str, code: int
    ) -> None:
        response = JSONResponse(
            {"status": detail, "detail": detail},
            status_code=code,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("path") != "/api/upload"
            or scope.get("method") != "POST"
        ):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        if not _authenticated(scope, self.sessions):
            await self._reject(scope, receive, send, "Not authenticated", 401)
            return

        lengths = [value for key, value in headers if key.lower() == b"content-length"]
        if lengths:
            try:
                parsed = [int(value) for value in lengths]
                if len(set(parsed)) != 1 or parsed[0] < 0:
                    raise ValueError("Invalid Content-Length")
            except ValueError:
                await self._reject(scope, receive, send, "Invalid Content-Length", 400)
                return
            if parsed[0] > self.body_limit:
                await self._reject(
                    scope, receive, send, "Upload body is too large", 413
                )
                return

        body_bytes = 0
        too_large = False
        response_started = False
        rejected = False

        async def bounded_receive() -> Message:
            nonlocal body_bytes, too_large
            message = await receive()
            if message["type"] == "http.request":
                body_bytes += len(message.get("body", b""))
                if body_bytes > self.body_limit:
                    too_large = True
                    # Starlette's multipart parser closes all files already
                    # opened when this exact exception type is raised.
                    raise MultiPartException("Upload body is too large")
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started, rejected
            if too_large:
                if not rejected:
                    rejected = True
                    await self._reject(
                        scope, receive, send, "Upload body is too large", 413
                    )
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async with self.slots:
            try:
                await self.app(scope, bounded_receive, guarded_send)
            except MultiPartException:
                if too_large and not response_started and not rejected:
                    await self._reject(
                        scope, receive, send, "Upload body is too large", 413
                    )
                else:
                    raise


class StreamConcurrencyMiddleware:
    """Hold a slot until a file response finishes or its client disconnects."""

    def __init__(self, app: ASGIApp, *, max_concurrent: int) -> None:
        self.app = app
        self.slots = asyncio.Semaphore(max_concurrent)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("path") in {"/file", "/api/v1/preview", "/api/v1/media"}
            and scope.get("method") in {"GET", "HEAD"}
        ):
            async with self.slots:
                await self.app(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Set headers directly in ASGI without buffering streaming response bodies."""

    def __init__(self, app: ASGIApp, *, secure: bool) -> None:
        self.app = app
        self.secure = secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"alt-svc"
                ]
                headers = MutableHeaders(scope=message)
                # Keep the established HTTPS route instead of advertising an
                # alternative whose UDP path may be unavailable. The deployment
                # edge must preserve this policy after proxy-generated headers.
                headers["Alt-Svc"] = "clear"
                headers.setdefault("X-Content-Type-Options", "nosniff")
                embeddable = (
                    getattr(scope.get("route"), "path", None) == "/embed"
                    and message["status"] == 200
                )
                if not embeddable:
                    headers.setdefault("X-Frame-Options", "DENY")
                frame_ancestors = "*" if embeddable else "'none'"
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault(
                    "Permissions-Policy",
                    "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
                )
                headers.setdefault(
                    "Content-Security-Policy",
                    "default-src 'self'; base-uri 'none'; object-src 'none'; "
                    f"frame-ancestors {frame_ancestors}; "
                    "form-action 'self'; "
                    "script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; "
                    "style-src 'self'; style-src-elem 'self'; "
                    "style-src-attr 'unsafe-inline'; "
                    "img-src 'self' data: blob:; media-src 'self' blob:; "
                    # Player frame conversion and local subtitle files use
                    # in-memory data/blob URLs. No remote origin is permitted.
                    "connect-src 'self' data: blob:",
                )
                if self.secure:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
                path = scope.get("path", "")
                if path.startswith("/api/"):
                    headers.setdefault("Cache-Control", "no-store")
                elif path.startswith("/static/"):
                    headers.setdefault("Cache-Control", "public, max-age=3600")
            await send(message)

        await self.app(scope, receive, with_headers)
