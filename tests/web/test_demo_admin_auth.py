"""Demo visitor and administrator sessions have independent signed authority."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.core.test_config import settings_values
from tests.support.app_fixtures import FakeRuntime, make_settings
from tests.support.mongo_fixtures import FakeMongoBackend, register_test_backend
from tgdrive.core.config import Settings
from tgdrive.core.security import (
    SESSION_COOKIE,
    InvalidSession,
    SessionManager,
    credential_session_key,
    demo_admin_session_key,
)
from tgdrive.storage.mongo import bootstrap_values
from tgdrive.storage.store import DriveStore
from tgdrive.web.app import create_app

VISITOR = "correct horse battery staple"
ADMIN = "private demonstration administrator password"
ORIGIN = {"Origin": "https://testserver"}


@contextmanager
def auth_client(tmp_path, **overrides):
    values = make_settings(tmp_path).model_dump(by_alias=True)
    values.update(DEMO_MODE=True, DEMO_ADMIN_PASSWORD=ADMIN)
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    runtime = FakeRuntime(store)
    runtime.replica = SimpleNamespace(
        blocked=False,
        recovery_kit=Mock(return_value={"key": "synthetic offline recovery key"}),
    )
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            register_test_backend(monkeypatch, settings, runtime.mongo)
            with TestClient(
                create_app(settings, runtime=runtime), base_url="https://testserver"
            ) as client:
                yield client, settings, runtime
    finally:
        runtime.transfers.registry.close()
        store.close()


def sign_in(client, password, **extra):
    return client.post("/api/checkPassword", json={"pass": password, **extra})


@pytest.mark.parametrize("password,privileged", [(VISITOR, False), (ADMIN, True)])
def test_same_login_form_grants_only_the_passwords_signed_access(
    tmp_path, password, privileged, monkeypatch
):
    preference_save = AsyncMock(return_value={"uploads_per_bot": 2})
    monkeypatch.setattr(
        "tgdrive.settings.management_api.get_transfer_preferences",
        lambda *_args: SimpleNamespace(save=preference_save),
    )
    with auth_client(tmp_path) as (client, settings, runtime):
        assert sign_in(client, password, demo_admin=True).status_code == 200
        token = client.cookies.get(SESSION_COOKIE)
        assert (
            client.app.state.sessions.verify(token).get("demo_admin", False)
            is privileged
        )
        config = client.get("/api/config")
        session = client.get("/api/v1/session")
        for response in (config, session):
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["demo_mode"] is True
            assert response.json()["demo_admin"] is privileged
            assert response.json()["demo_read_only"] is not privileged
            assert ADMIN not in response.text
        assert session.json()["authenticated"] is True
        saved = client.patch(
            "/api/v1/settings/uploads", json={"uploads_per_bot": 2}, headers=ORIGIN
        )
        key = client.get("/api/v1/replica/key")
        assert saved.status_code == (200 if privileged else 403)
        assert key.status_code == (200 if privileged else 403)
        assert preference_save.await_count == int(privileged)
        assert runtime.replica.recovery_kit.call_count == int(privileged)
        assert settings.demo_mode is True
        assert client.post("/api/logout", json={}, headers=ORIGIN).status_code == 200
        assert client.get("/api/v1/session").json() == {
            "authenticated": False,
            "demo_mode": True,
            "demo_admin": False,
            "demo_read_only": True,
        }
        with pytest.raises(InvalidSession, match="revoked"):
            client.app.state.sessions.verify(token)


def test_legacy_demo_without_second_password_remains_restricted(tmp_path):
    with auth_client(tmp_path, DEMO_ADMIN_PASSWORD=None) as (client, _, _):
        assert sign_in(client, ADMIN).status_code == 401
        assert sign_in(client, VISITOR).status_code == 200
        assert client.get("/api/config").json()["demo_read_only"] is True
        assert client.get("/api/v1/replica/key").status_code == 403
        with pytest.raises(ValueError, match="not configured"):
            client.app.state.sessions.create(demo_admin=True)


def test_second_password_is_inactive_outside_demo_deployments(tmp_path):
    with auth_client(tmp_path, DEMO_MODE=False) as (client, _, _):
        assert sign_in(client, ADMIN).status_code == 401
        assert sign_in(client, VISITOR).status_code == 200
        assert client.get("/api/v1/session").json() == {"authenticated": True}
        config = client.get("/api/config").json()
        assert config["demo_mode"] is False
        assert config["demo_read_only"] is False
        assert config["demo_admin"] is False
        assert client.get("/api/v1/replica/key").status_code == 200


@pytest.mark.parametrize("plaintext", [None, "unused private fallback password"])
def test_demo_administrator_hash_takes_precedence_and_supports_login(
    tmp_path, plaintext
):
    hashed = PasswordHasher().hash(ADMIN)
    with auth_client(
        tmp_path,
        DEMO_ADMIN_PASSWORD=plaintext,
        DEMO_ADMIN_PASSWORD_HASH=hashed,
    ) as (client, _, _):
        assert sign_in(client, "unused private fallback password").status_code == 401
        assert sign_in(client, ADMIN).status_code == 200
        assert client.get("/api/config").json()["demo_admin"] is True


def test_invalid_login_uses_existing_shared_rate_limit(tmp_path):
    with auth_client(tmp_path) as (client, _, _):
        for _attempt in range(5):
            assert sign_in(client, "invalid password").status_code == 401
        limited = sign_in(client, ADMIN)
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0


def test_ordinary_sessions_are_never_promoted_by_configuring_demo_admin(tmp_path):
    owner_key = credential_session_key(b"s" * 32, VISITOR)
    legacy = SessionManager(owner_key, 300)
    token = legacy.create(now=1000)
    current = SessionManager(
        owner_key,
        300,
        demo_admin_key=demo_admin_session_key(b"s" * 32, ADMIN),
    )
    assert "demo_admin" not in current.verify(token, now=1001)
    encoded, _signature = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["demo_admin"] = True
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    signature = base64.urlsafe_b64encode(
        hmac.new(owner_key, encoded, hashlib.sha256).digest()
    ).rstrip(b"=")
    forged = (encoded + b"." + signature).decode()
    with pytest.raises(InvalidSession, match="signature"):
        current.verify(forged, now=1001)


def test_demo_admin_rotation_or_removal_does_not_revoke_ordinary_sessions(monkeypatch):
    monkeypatch.setattr("tgdrive.core.security.time.time", lambda: 1000)
    key = credential_session_key(b"s" * 32, VISITOR)
    backend = FakeMongoBackend()
    original = SessionManager(
        key,
        300,
        mongo_backend=backend,
        demo_admin_key=demo_admin_session_key(b"s" * 32, ADMIN),
    )
    visitor = original.create(now=1000)
    elevated = original.create(now=1000, demo_admin=True)
    original.close()
    for credential in (ADMIN, "rotated private administrator password", None):
        current = SessionManager(
            key,
            300,
            mongo_backend=backend,
            demo_admin_key=(
                demo_admin_session_key(b"s" * 32, credential) if credential else None
            ),
        )
        try:
            assert "demo_admin" not in current.verify(visitor, now=1001)
            if credential == ADMIN:
                assert current.verify(elevated, now=1001)["demo_admin"] is True
            else:
                with pytest.raises(InvalidSession):
                    current.verify(elevated, now=1001)
        finally:
            current.close()


def test_unsigned_deeply_nested_session_payload_is_rejected_before_decoding():
    manager = SessionManager(b"s" * 32, 300, demo_admin_key=b"d" * 32)
    encoded = base64.urlsafe_b64encode(b"[" * 2000 + b"0" + b"]" * 2000)
    with pytest.raises(InvalidSession, match="signature"):
        manager.verify(encoded.decode() + ".invalid-signature")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_optional_demo_administrator_credentials_are_absent(value):
    settings = Settings(
        _env_file=None,
        **settings_values(DEMO_ADMIN_PASSWORD=value, DEMO_ADMIN_PASSWORD_HASH=value),
    )
    assert settings.demo_admin_password is None
    assert settings.demo_admin_password_hash is None


@pytest.mark.parametrize("value", ["short", "<replace-with-password>"])
def test_demo_administrator_password_rejects_unsafe_values_without_echo(value):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **settings_values(DEMO_ADMIN_PASSWORD=value))
    assert value not in str(caught.value)


@pytest.mark.parametrize("variant", ["malformed", "weak", "excessive", "argon2i"])
def test_demo_administrator_hash_uses_the_owner_security_limits(variant):
    value = PasswordHasher().hash(ADMIN)
    value = {
        "malformed": "not-a-password-hash",
        "weak": value.replace("m=65536,t=3,p=4", "m=19455,t=1,p=4"),
        "excessive": value.replace("m=65536,t=3,p=4", "m=262145,t=3,p=4"),
        "argon2i": value.replace("$argon2id$", "$argon2i$"),
    }[variant]
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **settings_values(DEMO_ADMIN_PASSWORD_HASH=value))
    assert value not in str(caught.value)


@pytest.mark.parametrize(
    "hashed_owner,hashed_demo",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_identical_effective_passwords_are_rejected_when_detectable(
    hashed_owner, hashed_demo
):
    encoded = PasswordHasher().hash(VISITOR)
    with pytest.raises(ValidationError, match="must differ"):
        Settings(
            _env_file=None,
            **settings_values(
                DEMO_MODE=True,
                ADMIN_PASSWORD_HASH=encoded if hashed_owner else None,
                DEMO_ADMIN_PASSWORD=None if hashed_demo else VISITOR,
                DEMO_ADMIN_PASSWORD_HASH=encoded if hashed_demo else None,
            ),
        )


def test_differently_salted_equal_passwords_resolve_to_restricted_access(tmp_path):
    with auth_client(
        tmp_path,
        ADMIN_PASSWORD_HASH=PasswordHasher().hash(VISITOR),
        DEMO_ADMIN_PASSWORD_HASH=PasswordHasher().hash(VISITOR),
    ) as (client, _, _):
        assert sign_in(client, VISITOR).status_code == 200
        assert client.get("/api/config").json()["demo_read_only"] is True


def test_demo_credentials_are_not_saved_or_restored_as_mongo_bootstrap(
    tmp_path, monkeypatch
):
    encoded = PasswordHasher().hash(ADMIN)
    settings = Settings(
        _env_file=None,
        **settings_values(DEMO_ADMIN_PASSWORD=ADMIN, DEMO_ADMIN_PASSWORD_HASH=encoded),
    )
    collection = Mock()
    saved = bootstrap_values(settings)
    assert "DEMO_ADMIN_PASSWORD" not in saved
    assert "DEMO_ADMIN_PASSWORD_HASH" not in saved
    bootstrap = settings_values(
        DEMO_ADMIN_PASSWORD=ADMIN, DEMO_ADMIN_PASSWORD_HASH=encoded, DEMO_MODE=True
    )
    for key in (*bootstrap, "ADMIN_PASSWORD_HASH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MONGODB_URI", "mongodb://synthetic.invalid")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TGDRIVE_AUTO_SECRETS", "false")
    collection.find_one.return_value = {"document": bootstrap}
    monkeypatch.setattr(
        "tgdrive.storage.mongo.configure",
        lambda *_args: SimpleNamespace(db=SimpleNamespace(settings=collection)),
    )
    monkeypatch.setattr(
        "tgdrive.settings.managed_settings.read_document", lambda _dir: None
    )
    restored = Settings(_env_file=None)
    assert restored.demo_mode is False
    assert restored.demo_admin_password is None
    assert restored.demo_admin_password_hash is None
    assert restored.api_id == bootstrap["API_ID"]


def test_demo_credentials_survive_validated_settings_copy_for_replica_startup():
    original = Settings(_env_file=None, **settings_values(DEMO_ADMIN_PASSWORD=ADMIN))
    copied = Settings(_env_file=None, **original.model_dump(by_alias=True))
    assert copied.demo_admin_password == original.demo_admin_password
