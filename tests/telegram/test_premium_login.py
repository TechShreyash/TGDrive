from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from tests.support.mongo_fixtures import register_test_backend
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.settings.premium_login import (
    LOGIN_TTL,
    PremiumLoginProblem,
    PremiumLoginService,
)
from tgdrive.settings.premium_login_api import LoginStart, LoginVerify

PHONE = "+919876543210"
SESSION = "new-private-authorized-session"


class LoginClient:
    def __init__(self, *, two_factor=False, premium=True):
        self.two_factor = two_factor
        self.premium = premium
        self.disconnected = False
        self.logged_out = False
        self.calls = []
        self.send_failure = None
        self.session = SimpleNamespace(save=lambda: SESSION)

    async def connect(self):
        return None

    async def send_code_request(self, phone):
        self.calls.append(("send", phone))
        if self.send_failure:
            raise self.send_failure
        return SimpleNamespace(phone_code_hash="private-code-hash")

    async def sign_in(self, **kwargs):
        self.calls.append(("sign_in", kwargs))
        if "password" in kwargs:
            if kwargs["password"] != " correct password ":
                raise PasswordHashInvalidError(request=None)
        elif kwargs.get("code") != "12345":
            raise PhoneCodeInvalidError(request=None)
        elif self.two_factor:
            raise SessionPasswordNeededError(request=None)

    async def get_me(self):
        return SimpleNamespace(bot=False, premium=self.premium)

    async def log_out(self):
        self.logged_out = True

    async def disconnect(self):
        self.disconnected = True


def login_service(client=None, *, clock=None, saver=None):
    client = client or LoginClient()
    saved = saver or AsyncMock(return_value={"revision": 2, "restart_required": True})
    service = PremiumLoginService(
        lambda: (12345, SecretStr("a" * 32), 1),
        saved,
        client_factory=lambda _id, _hash: client,
        **({"clock": clock} if clock else {}),
    )
    return service, client, saved


async def begin(service, *, owner="owner", phone=PHONE):
    return await service.begin(owner, 1, phone, "Personal account")


@pytest.mark.asyncio
async def test_success_saves_private_session_and_returns_only_redacted_settings():
    service, client, save = login_service()
    try:
        challenge = await begin(service)
        assert (
            challenge["stage"] == "code"
            and challenge["expires_in_seconds"] <= LOGIN_TTL
        )
        response = await service.verify(
            "owner", challenge["challenge_id"], code="12345"
        )
        assert response == {
            "stage": "complete",
            "settings": {"revision": 2, "restart_required": True},
        }
        save.assert_awaited_once_with(1, "Personal account", SESSION)
        assert SESSION not in str(response) and PHONE not in str(challenge)
        assert client.disconnected and not client.logged_out
        assert not service.challenges
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_two_step_password_is_conditional_and_whitespace_is_preserved():
    service, client, save = login_service(LoginClient(two_factor=True))
    try:
        identity = (await begin(service))["challenge_id"]
        response = await service.verify("owner", identity, code="12345")
        assert response["stage"] == "password"
        save.assert_not_awaited()
        with pytest.raises(PremiumLoginProblem, match="password is incorrect"):
            await service.verify("owner", identity, password="incorrect secret")
        response = await service.verify(
            "owner", identity, password=" correct password "
        )
        assert response["stage"] == "complete"
        assert client.calls[-1] == ("sign_in", {"password": " correct password "})
        assert "correct password" not in str(response)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_wrong_owner_cannot_inspect_verify_or_cancel_another_login():
    service, client, _ = login_service()
    try:
        identity = (await begin(service))["challenge_id"]
        with pytest.raises(PremiumLoginProblem) as result:
            await service.verify("other-owner", identity, code="12345")
        assert result.value.status == 410
        await service.cancel("other-owner", identity)
        assert identity in service.challenges and not client.disconnected
        await service.cancel("owner", identity)
        assert identity not in service.challenges and client.disconnected
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_expired_challenge_disconnects_without_storing_authentication():
    now = [100.0]
    service, client, save = login_service(clock=lambda: now[0])
    try:
        identity = (await begin(service))["challenge_id"]
        now[0] += LOGIN_TTL + 1
        with pytest.raises(PremiumLoginProblem) as result:
            await service.verify("owner", identity, code="12345")
        assert result.value.status == 410 and client.disconnected
        save.assert_not_awaited()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_bad_codes_have_bounded_attempts_and_never_echo_code():
    service, client, save = login_service()
    try:
        identity = (await begin(service))["challenge_id"]
        for index in range(5):
            with pytest.raises(PremiumLoginProblem) as result:
                await service.verify("owner", identity, code="98765")
            assert "98765" not in str(result.value)
            assert result.value.status == (429 if index == 4 else 422)
        assert not service.challenges and client.disconnected
        save.assert_not_awaited()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_non_premium_login_is_revoked_instead_of_saving_an_unusable_account():
    service, client, save = login_service(LoginClient(premium=False))
    try:
        identity = (await begin(service))["challenge_id"]
        with pytest.raises(PremiumLoginProblem, match="does not have Telegram Premium"):
            await service.verify("owner", identity, code="12345")
        assert client.logged_out and client.disconnected and not service.challenges
        save.assert_not_awaited()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_settings_conflict_after_authentication_revokes_new_session():
    save = AsyncMock(
        side_effect=SettingsProblem(
            "Reload settings", code="SETTINGS_CONFLICT", status=409
        )
    )
    service, client, _ = login_service(saver=save)
    try:
        identity = (await begin(service))["challenge_id"]
        with pytest.raises(SettingsProblem):
            await service.verify("owner", identity, code="12345")
        assert client.logged_out and client.disconnected and not service.challenges
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_send_flood_wait_is_preserved_and_blocks_further_requests():
    client = LoginClient()
    client.send_failure = FloodWaitError(request=None, capture=123)
    service, client, _ = login_service(client)
    try:
        with pytest.raises(PremiumLoginProblem) as result:
            await begin(service)
        assert result.value.status == 429 and result.value.retry_after == 123
        assert client.disconnected and not service.challenges
        with pytest.raises(PremiumLoginProblem) as waiting:
            await begin(service, phone="+441234567890")
        assert waiting.value.status == 429 and waiting.value.retry_after >= 122
        assert len(client.calls) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_send_rate_capacity_and_revision_checks_do_not_start_extra_clients():
    service, _, _ = login_service()
    try:
        await begin(service)
        with pytest.raises(PremiumLoginProblem, match="Wait a minute"):
            await begin(service)
        await begin(service, phone="+441234567890")
        with pytest.raises(PremiumLoginProblem, match="Finish or cancel"):
            await begin(service, phone="+331234567890")
        assert len(service.challenges) == 2
    finally:
        await service.close()
    other, client, _ = login_service()
    try:
        with pytest.raises(PremiumLoginProblem) as conflict:
            await other.begin("owner", 0, PHONE, "Personal")
        assert conflict.value.code == "SETTINGS_CONFLICT" and client.calls == []
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_concurrent_submission_cannot_save_or_revoke_a_saved_session_twice():
    service, client, save = login_service()
    try:
        identity = (await begin(service))["challenge_id"]
        results = await asyncio.gather(
            service.verify("owner", identity, code="12345"),
            service.verify("owner", identity, code="12345"),
            return_exceptions=True,
        )
        assert sum(isinstance(value, dict) for value in results) == 1
        save.assert_awaited_once()
        assert not client.logged_out
    finally:
        await service.close()


def test_authentication_payload_representations_hide_every_secret():
    start = LoginStart(expected_revision=1, phone=PHONE, label="Personal")
    verification = LoginVerify(
        challenge_id="a" * 43, code="12345", password="private-password"
    )
    assert PHONE not in repr(start)
    assert "12345" not in repr(verification) and "private-password" not in repr(
        verification
    )


def test_http_login_requires_owner_same_origin_and_hides_validation_secrets(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from tests.support.app_fixtures import FakeRuntime, make_settings
    from tgdrive.storage.store import DriveStore
    from tgdrive.web.app import create_app

    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.json")
    runtime = FakeRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, settings)
    app = create_app(settings, runtime=runtime)
    service, transport, save = login_service()
    app.state.premium_login_service = service
    with TestClient(app, base_url="https://testserver") as client:
        start_path = "/api/v1/settings/premium-login/start"
        payload = {"expected_revision": 1, "phone": PHONE, "label": "Personal"}
        assert client.post(start_path, json=payload).status_code == 401
        assert (
            client.post(
                "/api/checkPassword",
                json={
                    "pass": settings.admin_password.get_secret_value(),
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                start_path, json=payload, headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        secret = "private-phone-secret-" * 5
        invalid = client.post(start_path, json=payload | {"phone": secret})
        assert invalid.status_code == 422 and secret not in invalid.text
        response = client.post(start_path, json=payload)
        assert response.status_code == 200
        identity = response.json()["challenge_id"]
        assert PHONE not in response.text
        verified = client.post(
            "/api/v1/settings/premium-login/verify",
            json={
                "challenge_id": identity,
                "code": "12345",
            },
        )
        assert verified.status_code == 200
        assert verified.json()["stage"] == "complete"
        assert SESSION not in verified.text and "12345" not in verified.text
        save.assert_awaited_once()
    assert transport.disconnected


def test_new_owner_session_cannot_finish_previous_browser_login(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.support.app_fixtures import FakeRuntime, make_settings
    from tgdrive.storage.store import DriveStore
    from tgdrive.web.app import create_app

    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.json")
    runtime = FakeRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, settings)
    app = create_app(settings, runtime=runtime)
    service, transport, save = login_service()
    app.state.premium_login_service = service
    with TestClient(app, base_url="https://testserver") as client:
        password = {"pass": settings.admin_password.get_secret_value()}
        client.post("/api/checkPassword", json=password)
        identity = client.post(
            "/api/v1/settings/premium-login/start",
            json={
                "expected_revision": 1,
                "phone": PHONE,
                "label": "Personal",
            },
        ).json()["challenge_id"]
        client.post("/api/logout", json={})
        client.post("/api/checkPassword", json=password)
        result = client.post(
            "/api/v1/settings/premium-login/verify",
            json={
                "challenge_id": identity,
                "code": "12345",
            },
        )
        assert result.status_code == 410
        save.assert_not_awaited()
    assert transport.disconnected


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [True, False, None])
async def test_ambiguous_save_never_revokes_committed_or_unreadable_private_session(
    committed,
):
    service, client, _ = login_service(
        saver=AsyncMock(
            side_effect=OSError("failure after private settings replacement"),
        )
    )
    service.is_session_saved = lambda value: committed if value == SESSION else False
    try:
        identity = (await begin(service))["challenge_id"]
        with pytest.raises(PremiumLoginProblem):
            await service.verify("owner", identity, code="12345")
        assert client.disconnected
        assert client.logged_out is (committed is False)
        assert not service.challenges
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_lost_sign_in_confirmation_still_revokes_the_owned_new_authorization():
    service, client, save = login_service()
    client.sign_in = AsyncMock(side_effect=TimeoutError("server may have authorized"))
    try:
        identity = (await begin(service))["challenge_id"]
        with pytest.raises(PremiumLoginProblem):
            await service.verify("owner", identity, code="12345")
        assert client.logged_out and client.disconnected
        save.assert_not_awaited()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_close_during_expired_reaper_logout_drains_disconnect_after_map_removal():
    from contextlib import suppress

    now = [100.0]
    service, client, _ = login_service(clock=lambda: now[0])
    await begin(service)
    service._reaper.cancel()
    with suppress(asyncio.CancelledError):
        await service._reaper
    entered, release = asyncio.Event(), asyncio.Event()

    async def logout():
        entered.set()
        await release.wait()
        client.logged_out = True

    client.log_out = logout
    now[0] += LOGIN_TTL + 1
    service._reaper = asyncio.create_task(service._expire())
    await entered.wait()
    assert not service.challenges
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 1)
    assert client.logged_out and client.disconnected
    assert not service._cleanups


@pytest.mark.asyncio
async def test_close_cancels_provider_verification_before_waiting_for_challenge_lock():
    service, client, save = login_service()
    identity = (await begin(service))["challenge_id"]
    entered = asyncio.Event()

    async def blocked_sign_in(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    client.sign_in = blocked_sign_in
    verifying = asyncio.create_task(service.verify("owner", identity, code="12345"))
    await entered.wait()
    await asyncio.wait_for(service.close(), 1)
    assert verifying.cancelled()
    assert client.logged_out and client.disconnected
    assert not service.challenges and not service._operations and not service._cleanups
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_http_cleanup_retains_ownership_until_disconnect():
    service, client, _ = login_service()
    identity = (await begin(service))["challenge_id"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def logout():
        entered.set()
        await release.wait()
        client.logged_out = True

    client.log_out = logout
    cancelling = asyncio.create_task(service.cancel("owner", identity))
    try:
        await entered.wait()
        cancelling.cancel()
        await asyncio.sleep(0)
        cancelling.cancel()  # Repeated cancellation cannot abandon cleanup.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cancelling, 1)
        assert client.logged_out and client.disconnected
        assert not service.challenges and not service._cleanups
    finally:
        await service.close()
