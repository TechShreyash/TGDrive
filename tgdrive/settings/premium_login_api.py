"""Owner-only endpoints for a bounded Telegram Premium connection flow."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from tgdrive.core.security import SESSION_COOKIE
from tgdrive.settings.premium_login import PremiumLoginProblem, PremiumLoginService


class LoginStart(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=0)
    phone: SecretStr = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=80)


class LoginVerify(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    challenge_id: str = Field(
        min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    code: SecretStr | None = Field(default=None, max_length=16)
    password: SecretStr | None = Field(default=None, max_length=512)


def register_premium_routes(app: Any, *, require_admin: Any, get_service: Any) -> None:
    def service(request: Request) -> PremiumLoginService:
        current = getattr(app.state, "premium_login_service", None)
        if current is None:
            manager = get_service(request)
            current = PremiumLoginService(
                manager.login_api,
                manager.add_authenticated_session,
                is_session_saved=manager.session_is_saved,
            )
            app.state.premium_login_service = current
        return current

    def owner(request: Request) -> str:
        return hashlib.sha256(
            request.cookies.get(SESSION_COOKIE, "").encode()
        ).hexdigest()

    @app.exception_handler(PremiumLoginProblem)
    async def login_error(
        _request: Request, error: PremiumLoginProblem
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": str(error), "code": error.code},
            status_code=error.status,
            headers={"Retry-After": str(error.retry_after)}
            if error.retry_after
            else None,
        )

    admin = [Depends(require_admin)]

    @app.post("/api/v1/settings/premium-login/start", dependencies=admin)
    async def begin(payload: LoginStart, request: Request) -> dict[str, Any]:
        return await service(request).begin(
            owner(request),
            payload.expected_revision,
            payload.phone.get_secret_value(),
            payload.label,
        )

    @app.post("/api/v1/settings/premium-login/verify", dependencies=admin)
    async def verify(payload: LoginVerify, request: Request) -> dict[str, Any]:
        return await service(request).verify(
            owner(request),
            payload.challenge_id,
            code=payload.code.get_secret_value() if payload.code else None,
            password=payload.password.get_secret_value() if payload.password else None,
        )

    @app.delete("/api/v1/settings/premium-login/{challenge_id}", dependencies=admin)
    async def cancel(challenge_id: str, request: Request) -> dict[str, str]:
        await service(request).cancel(owner(request), challenge_id)
        return {"status": "cancelled"}
