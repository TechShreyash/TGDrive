"""Public demo keeps drive features usable while owner management is read-only."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.core.test_config import settings_values
from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.core.config import Settings
from tgdrive.storage.mongo import bootstrap_values
from tgdrive.storage.store import DriveStore
from tgdrive.web.app import create_app

ORIGIN = {"Origin": "https://testserver"}
TOKEN = "654321:" + "y" * 30
CHALLENGE = "a" * 32
MANAGEMENT_REQUESTS = [
    ("POST", "/api/v1/settings/thumbnails/rebuild", {"confirmed": True}),
    ("POST", "/api/v1/settings/thumbnails/rebuild/cancel", {}),
    ("PATCH", "/api/v1/settings", {"expected_revision": 0, "api_id": 99999}),
    ("PATCH", "/api/v1/settings/uploads", {"uploads_per_bot": 2}),
    ("PATCH", "/api/v1/settings/performance", {"timezone": "UTC"}),
    ("PATCH", "/api/v1/settings/bot-mode", {"folder_path": "/"}),
    ("POST", "/api/v1/settings/refresh", {}),
    (
        "POST",
        "/api/v1/settings/premium-login/start",
        {"expected_revision": 0, "phone": "+15555550100", "label": "Demo account"},
    ),
    (
        "POST",
        "/api/v1/settings/premium-login/verify",
        {"challenge_id": CHALLENGE, "code": "12345"},
    ),
    ("DELETE", "/api/v1/settings/premium-login/" + CHALLENGE, None),
    (
        "POST",
        "/api/v1/replica/configure",
        {"channel_id": -100222333444, "bot_token": TOKEN, "independent_accounts": True},
    ),
    *[
        ("POST", "/api/v1/replica/action", {"action": action})
        for action in ("pause", "resume", "disable", "retry", "sync")
    ],
    ("POST", "/api/v1/replica/recover", {"activate": True}),
    (
        "POST",
        "/api/v1/replica/recover",
        {"source_channel": -100222333444, "bot_token": TOKEN, "key": "b" * 64},
    ),
    (
        "POST",
        "/api/v1/replica/recover",
        {
            "target_channel": -100222333444,
            "target_bot_token": TOKEN,
            "confirm_channel": "-100222333444",
        },
    ),
    ("POST", "/api/v1/backup", {}),
    ("GET", "/api/v1/replica/key", None),
    ("GET", "/api/v1/backup/recovery-key", None),
]


@contextmanager
def owner_client(tmp_path, *, demo_mode=True, root_path=""):
    settings = make_settings(tmp_path).model_copy(update={"demo_mode": demo_mode})
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    runtime = FakeRuntime(store)
    runtime.pool = SimpleNamespace(
        status=AsyncMock(return_value=()),
        health=AsyncMock(return_value=None),
        set_uploads_per_bot=AsyncMock(),
    )
    runtime.preloads = SimpleNamespace(
        reconfigure=AsyncMock(),
        snapshot=AsyncMock(return_value={"jobs": [], "cache": {"files": []}}),
        prepare=AsyncMock(return_value={"id": "demo-preload", "state": "queued"}),
        cancel=AsyncMock(),
        clear=AsyncMock(),
    )
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            register_test_backend(monkeypatch, settings, runtime.mongo)
            with TestClient(
                create_app(settings, runtime=runtime),
                base_url="https://testserver",
                root_path=root_path,
            ) as client:
                yield client, store, runtime, settings
    finally:
        if hasattr(runtime.transfers, "uploads"):
            runtime.transfers.uploads.close()
        runtime.transfers.registry.close()
        store.close()


@pytest.fixture
def demo_owner(tmp_path):
    with owner_client(tmp_path) as owner:
        yield owner


def test_demo_mode_defaults_off_and_is_loaded_from_environment(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert Settings(_env_file=None, **settings_values()).demo_mode is False
    monkeypatch.setenv("DEMO_MODE", "true")
    assert Settings(_env_file=None, **settings_values()).demo_mode is True
    monkeypatch.setenv("DEMO_MODE", "false")
    assert Settings(_env_file=None, **settings_values()).demo_mode is False


@pytest.mark.parametrize("demo_mode", [False, True])
def test_public_config_reports_deployment_demo_mode(tmp_path, demo_mode):
    with owner_client(tmp_path, demo_mode=demo_mode) as (client, _, _, _):
        response = client.get("/api/config")
        assert response.status_code == 200
        assert response.json()["demo_mode"] is demo_mode
        access = client.get("/api/v1/session").json()
        assert access["authenticated"] is False
        if demo_mode:
            assert access["demo_read_only"] is True
            assert access["demo_admin"] is False


@pytest.mark.parametrize("method,path,payload", MANAGEMENT_REQUESTS)
def test_demo_rejects_direct_management_requests_before_any_side_effect(
    demo_owner, monkeypatch, method, path, payload
):
    client, store, runtime, settings = demo_owner
    forbidden = Mock(side_effect=AssertionError("Management service must not run"))
    for name in (
        "get_connection_manager",
        "get_bot_settings",
        "get_transfer_preferences",
    ):
        monkeypatch.setattr("tgdrive.settings.management_api." + name, forbidden)
    monkeypatch.setattr(
        "tgdrive.observability.performance_api.get_performance_settings", forbidden
    )
    runtime.replica = SimpleNamespace(blocked=False, recovery_kit=forbidden)
    runtime.backup = SimpleNamespace(ensure_revision=forbidden)
    client.app.state.premium_login_service = SimpleNamespace(
        begin=forbidden, verify=forbidden, cancel=forbidden, close=AsyncMock()
    )
    settings.mongodb_backup_key = SecretStr("b" * 64)
    runtime.mongo = object()
    login(client)
    revision = store.revision

    response = client.request(method, path, json=payload, headers=ORIGIN)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "DEMO_MODE_READ_ONLY"
    assert "read-only" in response.json()["detail"]
    assert "b" * 64 not in response.text
    assert TOKEN not in response.text
    forbidden.assert_not_called()
    assert store.revision == revision
    assert settings.demo_mode is True
    for filename in (
        "telegram-settings.json",
        "transfer-preferences.json",
        "performance-preferences.json",
        "bot-preferences.json",
        "channel-activation.json",
    ):
        assert not (settings.resolved_data_dir / filename).exists()


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("PATCH", "/api/v1/settings/uploads", {"uploads_per_bot": 2}),
        ("GET", "/api/v1/replica/key", None),
        ("GET", "/api/v1/backup/recovery-key", None),
    ],
)
def test_demo_management_still_requires_authentication(
    demo_owner, method, path, payload
):
    client, _, _, _ = demo_owner
    assert client.request(method, path, json=payload, headers=ORIGIN).status_code == 401


def test_demo_preserves_same_origin_protection(demo_owner):
    client, store, _, _ = demo_owner
    login(client)
    contents = store.list_directory("/")
    response = client.post(
        "/api/createNewFolder",
        json={"path": "/", "name": "Cross-origin"},
        headers={"Origin": "https://other.example"},
    )
    assert response.status_code == 403
    assert store.list_directory("/") == contents


def test_demo_setting_cannot_be_changed_through_the_settings_api(demo_owner):
    client, _, _, settings = demo_owner
    login(client)
    for payload in (
        {"expected_revision": 0, "demo_mode": False},
        {"expected_revision": 0, "DEMO_MODE": False},
    ):
        response = client.patch("/api/v1/settings", json=payload, headers=ORIGIN)
        assert response.status_code == 403
        assert response.json()["code"] == "DEMO_MODE_READ_ONLY"
    assert settings.demo_mode is True
    assert client.get("/api/config?demo_mode=false").json()["demo_mode"] is True


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("PATCH", "/demo/api/v1/settings/uploads", {"uploads_per_bot": 2}),
        ("POST", "/demo/api/v1/replica/action", {"action": "sync"}),
        ("POST", "/demo/api/v1/backup", {}),
        ("GET", "/demo/api/v1/replica/key", None),
        ("GET", "/demo/api/v1/backup/recovery-key", None),
    ],
)
def test_demo_guard_uses_matched_route_under_proxy_root_path(
    tmp_path, method, path, payload
):
    with owner_client(tmp_path, root_path="/demo") as (client, _, runtime, settings):
        login(client)
        runtime.replica = SimpleNamespace(blocked=False)
        runtime.mongo = object()
        settings.mongodb_backup_key = SecretStr("b" * 64)
        response = client.request(method, path, json=payload, headers=ORIGIN)
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "DEMO_MODE_READ_ONLY"
        assert "b" * 64 not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/settings/uploads/",
        "/api/v1/%73ettings/uploads",
        "/api/v1/settings%2Fuploads",
    ],
)
def test_demo_guard_survives_redirects_and_encoded_management_paths(demo_owner, path):
    client, _, runtime, _ = demo_owner
    login(client)
    response = client.patch(path, json={"uploads_per_bot": 2}, headers=ORIGIN)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "DEMO_MODE_READ_ONLY"
    runtime.pool.set_uploads_per_bot.assert_not_called()


def test_demo_keeps_settings_status_and_sanitized_reports_readable(demo_owner):
    client, _, runtime, _ = demo_owner
    runtime.replica = SimpleNamespace(
        blocked=False, status=Mock(return_value={"enabled": False, "state": "disabled"})
    )
    login(client)
    for path in (
        "/api/v1/settings",
        "/api/v1/settings/uploads",
        "/api/v1/settings/performance",
        "/api/v1/settings/bot-mode",
        "/api/v1/status",
        "/api/v1/performance",
        "/api/v1/replica",
        "/api/v1/replica/report",
        "/api/v1/preloads",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert TOKEN not in response.text
    assert client.get("/api/v1/settings/uploads").json()["uploads_per_bot"] == 1
    assert (
        client.get("/api/v1/settings/performance").json()["timezone"] == "Asia/Kolkata"
    )


def test_normal_deployment_can_still_change_preferences_and_back_up(tmp_path):
    with owner_client(tmp_path, demo_mode=False) as (client, store, runtime, _):
        login(client)
        changed = client.patch(
            "/api/v1/settings/uploads", json={"uploads_per_bot": 2}, headers=ORIGIN
        )
        assert changed.status_code == 200
        assert changed.json()["uploads_per_bot"] == 2
        runtime.pool.set_uploads_per_bot.assert_awaited_with(2)
        runtime.backup = SimpleNamespace(
            ensure_revision=AsyncMock(),
            snapshot_now=AsyncMock(),
            protection_status=Mock(return_value={"state": "protected"}),
        )
        response = client.post("/api/v1/backup", json={}, headers=ORIGIN)
        assert response.status_code == 200
        runtime.backup.snapshot_now.assert_awaited_once_with(store)
        runtime.backup.ensure_revision.assert_not_awaited()


def test_demo_allows_file_crud_uploads_shares_and_preload_actions(demo_owner):
    client, store, runtime, _ = demo_owner
    login(client)
    created = client.post(
        "/api/createNewFolder",
        json={"path": "/", "name": "Demo folder"},
        headers=ORIGIN,
    )
    assert created.status_code == 200
    folder = next(
        item
        for item in store.list_directory("/").values()
        if item["name"] == "Demo folder"
    )
    path = "/" + folder["id"]
    assert (
        client.post(
            "/api/renameFileFolder",
            json={"path": path, "name": "Renamed"},
            headers=ORIGIN,
        ).status_code
        == 200
    )
    assert store.get_directory(path)["name"] == "Renamed"
    share = client.post("/api/v1/shares", json={"path": path}, headers=ORIGIN)
    assert share.status_code == 201
    assert client.get("/api/v1/shares").json()["shares"][0]["name"] == "Renamed"
    assert (
        client.request(
            "DELETE", "/api/v1/shares", json={"path": path}, headers=ORIGIN
        ).status_code
        == 200
    )
    uploaded = client.post(
        "/api/upload",
        data={"path": "/"},
        files={"file": ("demo.txt", b"abc", "text/plain")},
        headers=ORIGIN,
    )
    assert uploaded.status_code == 200
    assert runtime.transfers.registry.get(uploaded.json()["id"]).total == 3
    receipt = client.post(
        "/api/v1/uploads",
        json={"name": "resumable.txt", "parent_id": "/", "total_size": 3},
        headers=ORIGIN,
    )
    assert receipt.status_code == 201
    upload_path = "/api/v1/uploads/" + receipt.json()["id"]
    chunk = client.patch(
        upload_path,
        content=b"abc",
        headers=ORIGIN
        | {"Content-Type": "application/octet-stream", "Upload-Offset": "0"},
    )
    assert chunk.status_code == 204
    assert client.head(upload_path).headers["upload-offset"] == "3"
    file = store.add_file("/", "preload.txt", 777, 3)
    prepared = client.post(
        "/api/v1/preloads", json={"path": "/" + file["id"]}, headers=ORIGIN
    )
    assert prepared.status_code == 202
    runtime.preloads.prepare.assert_awaited_once()
    assert (
        client.post(
            "/api/v1/preloads/demo-preload/cancel", json={}, headers=ORIGIN
        ).status_code
        == 200
    )
    runtime.preloads.cancel.assert_awaited_once_with("demo-preload")
    assert (
        client.post("/api/v1/preloads/clear", json={}, headers=ORIGIN).status_code
        == 200
    )
    runtime.preloads.clear.assert_awaited_once_with(None)
    for trash in (True, False, True):
        assert (
            client.post(
                "/api/trashFileFolder",
                json={"path": path, "trash": trash},
                headers=ORIGIN,
            ).status_code
            == 200
        )
    deleted = client.post("/api/deleteFileFolder", json={"path": path}, headers=ORIGIN)
    assert deleted.status_code == 202

    async def finish_deletion():
        await runtime.transfers.registry.get(deleted.json()["id"]).task

    client.portal.call(finish_deletion)
    assert folder["id"] not in store.list_directory("/")
    assert client.post("/api/logout", json={}, headers=ORIGIN).status_code == 200
    access = client.get("/api/v1/session").json()
    assert access["authenticated"] is False
    assert access["demo_read_only"] is True
    assert access["demo_admin"] is False


@pytest.mark.parametrize("demo_mode", [False, True])
def test_mongo_bootstrap_never_saves_demo_policy(demo_mode):
    settings = Settings(_env_file=None, **settings_values(DEMO_MODE=demo_mode))
    document = bootstrap_values(settings)
    assert "DEMO_MODE" not in document
    assert document["API_ID"] == settings.api_id
    assert document["API_HASH"] == settings.api_hash.get_secret_value()
    assert document["SESSION_SECRET"] == settings.session_secret.get_secret_value()


@pytest.mark.parametrize("saved_mode", [False, True])
@pytest.mark.parametrize("env_mode", [None, "true", "false"])
def test_legacy_mongo_bootstrap_cannot_supply_demo_policy(
    tmp_path, monkeypatch, saved_mode, env_mode
):
    document = settings_values(DEMO_MODE=saved_mode)
    for key in (*document, "ADMIN_PASSWORD_HASH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MONGODB_URI", "mongodb://synthetic.invalid")
    monkeypatch.setenv("MONGODB_DATABASE", "demo_policy_test")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TGDRIVE_AUTO_SECRETS", "false")
    if env_mode is not None:
        monkeypatch.setenv("DEMO_MODE", env_mode)
    collection = Mock()
    collection.find_one.return_value = {"document": document}
    backend = SimpleNamespace(db=SimpleNamespace(settings=collection))
    configure = Mock(return_value=backend)
    monkeypatch.setattr("tgdrive.storage.mongo.configure", configure)
    monkeypatch.setattr(
        "tgdrive.settings.managed_settings.read_document", lambda _dir: None
    )

    settings = Settings(_env_file=None)

    assert settings.demo_mode is (env_mode == "true")
    assert settings.api_id == document["API_ID"]
    assert settings.api_hash.get_secret_value() == document["API_HASH"]
    assert settings.session_secret.get_secret_value() == document["SESSION_SECRET"]
    configure.assert_called_once_with(
        tmp_path, "mongodb://synthetic.invalid", "demo_policy_test"
    )
