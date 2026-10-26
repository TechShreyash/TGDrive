"""Request permissions from server-verified demo session capabilities."""

from __future__ import annotations

from contextlib import suppress

from fastapi import Request

from tgdrive.core.security import SESSION_COOKIE, InvalidSession


def session_access(request: Request) -> dict[str, bool]:
    demo_mode = bool(request.app.state.settings.demo_mode)
    token = request.cookies.get(SESSION_COOKIE)
    claims = None
    if token:
        with suppress(InvalidSession):
            claims = request.app.state.sessions.verify(token)
    demo_admin = demo_mode and claims is not None and claims.get("demo_admin") is True
    return {
        "authenticated": claims is not None,
        "demo_mode": demo_mode,
        "demo_admin": demo_admin,
        "demo_read_only": demo_mode and not demo_admin,
    }


def is_demo_restricted(request: Request) -> bool:
    return session_access(request)["demo_read_only"]
