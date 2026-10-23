from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

import tgdrive.app as app_module
from tgdrive.app import create_app
from tgdrive.config import Settings
from tgdrive.store import DriveStore
from tgdrive.tasks import TransferRegistry


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        API_ID=12345,
        API_HASH="a" * 32,
        BOT_TOKENS="123456:" + "x" * 30,
        STORAGE_CHANNEL=-1001234567890,
        DATABASE_BACKUP_MSG_ID=42,
        ADMIN_PASSWORD="correct horse battery staple",
        SESSION_SECRET="s" * 48,
        DATA_DIR=tmp_path / "data",
        CACHE_DIR=tmp_path / "cache",
        COOKIE_SECURE=True,
    )


class FakeTransfers:
    def __init__(self) -> None:
        self.registry = TransferRegistry()
        self.deleted_messages: list[int] = []
        self.remote_requests: list[tuple[str, str, str | None]] = []

    def start_deletion(self, path: str):
        intent = self.store.begin_deletion(path, channel_id=-1001234567890)
        record = self.registry.create("deleting", total=len(intent["message_ids"]))

        async def complete(item):
            self.store.finish_deletion(intent["id"])

        self.registry.spawn(record.id, complete)
        return record

    async def delete_telegram_messages(self, message_ids: list[int]) -> None:
        self.deleted_messages.extend(message_ids)

    async def receive_browser_upload(
        self,
        upload: Any,
        destination: str,
        **_kwargs: Any,
    ):
        assert destination == "/"
        record = self.registry.create("uploading", total=3)
        on_created = _kwargs.get("on_created")
        if on_created:
            on_created(record)
        await upload.close()
        return record

    async def inspect_remote(self, url: str):
        return SimpleNamespace(
            filename="remote.bin",
            size=12,
            content_type="application/octet-stream",
            url=url,
        )

    def start_remote(self, *, url: str, destination: str, filename: str | None):
        self.remote_requests.append((url, destination, filename))
        return self.registry.create("downloading", total=12)


class FakeRuntime:
    def __init__(self, store: DriveStore) -> None:
        self.store = store
        self.transfers = FakeTransfers()
        self.transfers.store = store
        self.pool = object()
        self.ready = False
        self.stopped = False

    async def start(self) -> None:
        self.ready = True

    async def stop(self) -> None:
        self.ready = False
        self.stopped = True


@pytest.fixture
def app_client(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.json")
    runtime = FakeRuntime(store)
    app = create_app(settings, runtime=runtime)
    with TestClient(app, base_url="https://testserver") as client:
        yield client, store, runtime, settings
    assert runtime.stopped


def login(client: TestClient) -> None:
    response = client.post(
        "/api/checkPassword", json={"pass": "correct horse battery staple"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_security_headers_and_static_traversal(
    app_client: tuple[Any, ...],
) -> None:
    client, _, _, _ = app_client

    home = client.get("/")
    assert home.status_code == 200
    assert "default-src 'self'" in home.headers["content-security-policy"]
    policy = home.headers["content-security-policy"]
    assert "script-src 'self' 'wasm-unsafe-eval';" in policy
    assert "style-src-elem 'self';" in policy
    assert "style-src-attr 'unsafe-inline';" in policy
    assert "media-src 'self' blob:;" in policy
    assert "script-src 'self' 'unsafe-inline'" not in policy
    assert home.headers["x-content-type-options"] == "nosniff"
    assert "localStorage" not in home.text

    traversal = client.get("/static/%2e%2e/%2e%2e/config.py")
    assert traversal.status_code == 404
    assert "Backward-compatible imports" not in traversal.text


def test_login_cookie_and_directory_access_control(app_client: tuple[Any, ...]) -> None:
    client, _, _, _ = app_client

    denied = client.post("/api/getDirectory", json={"path": "/", "auth": None})
    assert denied.status_code == 401

    wrong = client.post("/api/checkPassword", json={"pass": "wrong"})
    assert wrong.status_code == 401
    assert "tgdrive_session" not in wrong.cookies

    response = client.post(
        "/api/checkPassword", json={"pass": "correct horse battery staple"}
    )
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie

    directory = client.post("/api/getDirectory", json={"path": "/"})
    assert directory.status_code == 200
    assert directory.json()["data"]["contents"] == {}


def test_revoked_client_returns_actionable_503_and_owner_status(app_client):
    from telethon.errors import SessionRevokedError
    from test_client_pool import FakeClient

    from tgdrive.client_pool import ClientPool

    client, _, runtime, _ = app_client
    runtime.pool = ClientPool([FakeClient("bot")])

    @client.app.get("/__test__/revoked")
    async def revoked():
        await runtime.pool.start()
        try:
            async with runtime.pool.lease():
                raise SessionRevokedError(request="secret-request")
        finally:
            await runtime.pool.stop()

    response = client.get("/__test__/revoked")
    assert response.status_code == 503
    assert response.json()["code"] == "TELEGRAM_CREDENTIALS_REVOKED"
    assert "Connections" in response.json()["detail"]
    assert "secret-request" not in response.text
    login(client)
    status = client.get("/api/v1/status").json()
    assert status["telegram_ready"] is False
    assert status["telegram_issues"][0]["code"] == "TELEGRAM_CREDENTIALS_REVOKED"


def test_folder_lifecycle_search_trash_and_permanent_delete(
    app_client: tuple[Any, ...],
) -> None:
    client, store, runtime, _ = app_client
    login(client)

    created = client.post(
        "/api/createNewFolder", json={"path": "/", "name": "Projects"}
    )
    assert created.status_code == 200
    folder = next(iter(store.list_directory("/").values()))
    folder_path = f"/{folder['id']}"
    store.add_file(folder_path, "report.txt", 777, 12)

    search = client.post(
        "/api/getDirectory", json={"path": "/search_report", "auth": None}
    )
    assert search.status_code == 200
    result = next(iter(search.json()["data"]["contents"].values()))
    assert result["name"] == "report.txt"

    trashed = client.post(
        "/api/trashFileFolder", json={"path": folder_path, "trash": True}
    )
    assert trashed.status_code == 200
    trash = client.post("/api/getDirectory", json={"path": "/trash"})
    trashed_item = next(iter(trash.json()["data"]["contents"].values()))
    assert trashed_item["name"] == "Projects"

    deleted = client.post("/api/deleteFileFolder", json={"path": folder_path})
    assert deleted.status_code == 202

    async def wait_for_deletion() -> None:
        record = runtime.transfers.registry.get(deleted.json()["id"])
        assert record.task is not None
        await record.task

    assert client.portal is not None
    client.portal.call(wait_for_deletion)
    assert client.get("/api/v1/jobs").headers["cache-control"] == "no-store"
    assert (
        client.get("/api/v1/jobs/" + deleted.json()["id"]).headers["cache-control"]
        == "no-store"
    )
    assert runtime.transfers.deleted_messages == []
    assert store.list_directory("/") == {}


def test_share_token_is_scoped_and_file_access_accepts_folder_token(
    app_client: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store, _, _ = app_client
    first = store.create_folder("/", "Shared")
    second = store.create_folder("/", "Private")
    first_path = f"/{first['id']}"
    second_path = f"/{second['id']}"
    file_data = store.add_file(first_path, "movie.mp4", 123, 4)
    file_path = f"{first_path}/{file_data['id']}"
    login(client)

    token_response = client.post("/api/getFolderShareAuth", json={"path": first_path})
    token = token_response.json()["auth"]
    client.post("/api/logout")

    allowed = client.post(
        "/api/getDirectory",
        json={"path": f"/share_{first_path.lstrip('/')}", "auth": token},
    )
    assert allowed.status_code == 200
    assert allowed.json()["auth_home_path"] == first_path

    denied = client.post(
        "/api/getDirectory",
        json={"path": f"/share_{second_path.lstrip('/')}", "auth": token},
    )
    assert denied.status_code == 403

    async def fake_stream(**kwargs: Any) -> Response:
        request = kwargs["request"]
        assert isinstance(request, Request)
        return Response(b"data", media_type="video/mp4")

    monkeypatch.setattr(app_module, "stream_telegram_file", fake_stream)
    shared_file = client.get("/file", params={"path": file_path, "auth": token})
    assert shared_file.status_code == 200
    assert shared_file.content == b"data"

    other_file = store.add_file(second_path, "private.txt", 456, 1)
    private_path = f"{second_path}/{other_file['id']}"
    denied_file = client.get("/file", params={"path": private_path, "auth": token})
    assert denied_file.status_code == 403


def test_health_and_logout_are_stable(app_client: tuple[Any, ...]) -> None:
    client, _, _, _ = app_client
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.post("/api/logout").json() == {"status": "ok"}


def test_upload_remote_and_progress_compatibility_endpoints(
    app_client: tuple[Any, ...],
) -> None:
    client, _, runtime, settings = app_client
    assert client.get("/api/config").json()["max_file_size"] == settings.upload_limit
    login(client)

    upload = client.post(
        "/api/upload",
        data={"path": "/"},
        files={"file": ("hello.txt", b"abc", "text/plain")},
    )
    assert upload.status_code == 200
    transfer_id = upload.json()["id"]

    for endpoint in (
        "/api/getTransferProgress",
        "/api/getSaveProgress",
        "/api/getUploadProgress",
        "/api/getFileDownloadProgress",
    ):
        progress = client.post(endpoint, json={"id": transfer_id})
        assert progress.status_code == 200
        assert progress.json()["data"]["phase"] == "uploading"

    cancelled = client.post("/api/cancelUpload", json={"id": transfer_id})
    assert cancelled.status_code == 200
    cancelled_record = runtime.transfers.registry.get(transfer_id)
    assert cancelled_record.cancel_event.is_set()
    assert cancelled_record.status.value == "cancelled"
    assert cancelled_record.phase == "cancelling"

    info = client.post(
        "/api/getFileInfoFromUrl", json={"url": "https://example.com/file.bin"}
    )
    assert info.json()["data"] == {
        "file_name": "remote.bin",
        "file_size": 12,
        "content_type": "application/octet-stream",
    }
    started = client.post(
        "/api/startFileDownloadFromUrl",
        json={
            "url": "https://example.com/file.bin",
            "path": "/",
            "filename": "renamed.bin",
            "singleThreaded": False,
        },
    )
    assert started.status_code == 200
    assert runtime.transfers.remote_requests == [
        ("https://example.com/file.bin", "/", "renamed.bin")
    ]


@pytest.mark.parametrize(
    ("original", "name", "supported"),
    [
        ("earth.mp4", "earth", "planet.mp4"),
        ("earth.mp4", "earth.pdf", "planet.mp4"),
        ("earth.mp4", "earth.mp4.exe", "planet.mp4"),
        ("image.svg", "image.\u017fvg", "renamed.svg"),
        ("file.ss", "file.\u00df", "renamed.ss"),
    ],
)
def test_rename_api_rejects_file_extension_changes(
    app_client, original, name, supported
):
    client, store, _, _ = app_client
    item = store.add_file("/", original, 5, 2)
    path = "/" + item["id"]
    login(client)
    before = store.export()

    response = client.post("/api/renameFileFolder", json={"path": path, "name": name})

    assert response.status_code == 400
    assert "extension cannot be changed" in response.json()["detail"]
    assert store.export() == before

    renamed = client.post(
        "/api/renameFileFolder", json={"path": path, "name": supported}
    )
    assert renamed.status_code == 200
    assert store.get_file(path)["name"] == supported


def test_mutation_validation_and_file_share_scope(
    app_client: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store, _, _ = app_client
    folder = store.create_folder("/", "Original")
    folder_path = f"/{folder['id']}"
    file_data = store.add_file(folder_path, "report.txt", 5, 2)
    file_path = f"{folder_path}/{file_data['id']}"
    login(client)

    delete_visible = client.post("/api/deleteFileFolder", json={"path": file_path})
    assert delete_visible.status_code == 409

    renamed = client.post(
        "/api/renameFileFolder", json={"path": folder_path, "name": "Renamed"}
    )
    assert renamed.status_code == 200
    duplicate = client.post(
        "/api/createNewFolder", json={"path": "/", "name": "renamed"}
    )
    assert duplicate.status_code == 409
    invalid = client.post(
        "/api/createNewFolder", json={"path": "/", "name": "bad/name"}
    )
    assert invalid.status_code == 400

    token = client.post("/api/getFileShareAuth", json={"path": file_path}).json()[
        "auth"
    ]
    client.post("/api/logout")
    folder_denied = client.post(
        "/api/getDirectory",
        json={"path": f"/share_{folder_path.lstrip('/')}", "auth": token},
    )
    assert folder_denied.status_code == 403

    async def fake_stream(**_kwargs: Any) -> Response:
        return Response(b"ok")

    monkeypatch.setattr(app_module, "stream_telegram_file", fake_stream)
    assert client.get("/file", params={"path": file_path}).status_code == 403
    shared = client.get("/file", params={"path": file_path, "auth": token})
    assert shared.status_code == 200


def test_video_player_requires_a_path(app_client: tuple[Any, ...]) -> None:
    client, _, _, _ = app_client
    assert client.get("/stream").status_code == 422
    response = client.get("/stream", params={"path": "/abc"})
    assert response.status_code == 200
    assert "video-player.js" in response.text


def test_remote_start_idempotency_and_request_recovery(
    app_client: tuple[Any, ...],
) -> None:
    client, _, runtime, _ = app_client
    login(client)
    payload = {
        "url": "https://example.com/file.bin",
        "path": "/",
        "filename": "file.bin",
        "client_request_id": "remote-request-0001",
    }
    first = client.post("/api/startFileDownloadFromUrl", json=payload)
    second = client.post("/api/startFileDownloadFromUrl", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(runtime.transfers.remote_requests) == 1

    lookup = client.post(
        "/api/getTransferByRequest",
        json={"client_request_id": payload["client_request_id"]},
    )
    assert lookup.status_code == 200
    assert lookup.json()["data"]["id"] == first.json()["id"]

    changed = client.post(
        "/api/startFileDownloadFromUrl", json={**payload, "filename": "different.bin"}
    )
    assert changed.status_code == 409
    assert len(runtime.transfers.remote_requests) == 1


def test_cancel_request_before_start_prevents_late_remote_job(
    app_client: tuple[Any, ...],
) -> None:
    client, _, runtime, _ = app_client
    login(client)
    request_id = "cancel-before-start-0001"
    cancelled = client.post("/api/cancelUpload", json={"client_request_id": request_id})
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"
    started = client.post(
        "/api/startFileDownloadFromUrl",
        json={
            "url": "https://example.com/file.bin",
            "path": "/",
            "client_request_id": request_id,
        },
    )
    assert started.status_code == 409
    assert runtime.transfers.remote_requests == []


def test_upload_request_reuses_record_and_cancel_tombstone_blocks_late_body(
    app_client: tuple[Any, ...],
) -> None:
    client, _, runtime, _ = app_client
    login(client)
    request_id = "browser-request-0001"
    form = {"path": "/", "client_request_id": request_id}
    first = client.post("/api/upload", data=form, files={"file": ("hello.txt", b"abc")})
    second = client.post(
        "/api/upload", data=form, files={"file": ("hello.txt", b"abc")}
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(runtime.transfers.registry._records) == 1
    cancelled = client.post("/api/cancelUpload", json={"client_request_id": request_id})
    assert cancelled.json()["data"]["status"] == "cancelled"

    blocked_id = "browser-cancel-before-body"
    client.post("/api/cancelUpload", json={"client_request_id": blocked_id})
    blocked = client.post(
        "/api/upload",
        data={"path": "/", "client_request_id": blocked_id},
        files={"file": ("hello.txt", b"abc")},
    )
    assert blocked.status_code == 409
    assert len(runtime.transfers.registry._records) == 1


def test_request_lookup_and_cancellation_are_authenticated(
    app_client: tuple[Any, ...],
) -> None:
    client, _, _, _ = app_client
    payload = {"client_request_id": "browser-request-0001"}
    assert client.post("/api/getTransferByRequest", json=payload).status_code == 401
    assert client.post("/api/cancelUpload", json=payload).status_code == 401
    login(client)
    assert client.post("/api/getTransferByRequest", json=payload).status_code == 404
    assert client.post("/api/cancelUpload", json={}).status_code == 422


def test_cookie_mutations_reject_cross_origin_including_sibling_subdomain(
    app_client: tuple[Any, ...],
) -> None:
    client, store, _, _ = app_client
    login(client)
    for origin in ("https://evil.example", "https://sub.testserver", "null"):
        denied = client.post(
            "/api/createNewFolder",
            json={"path": "/", "name": "Unwanted"},
            headers={"Origin": origin},
        )
        assert denied.status_code == 403
    assert store.list_directory("/") == {}
    allowed = client.post(
        "/api/createNewFolder",
        json={"path": "/", "name": "Allowed"},
        headers={"Origin": "https://testserver:443"},
    )
    assert allowed.status_code == 200


def test_json_body_limit_applies_before_decode_and_to_chunked_requests(
    app_client: tuple[Any, ...],
) -> None:
    client, _, _, _ = app_client
    oversized = b'{"pass":"' + b"x" * 70000 + b'"}'
    declared = client.post(
        "/api/checkPassword",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert declared.status_code == 413

    def chunks():
        yield b'{"pass":"'
        yield b"x" * 70000
        yield b'"}'

    chunked = client.post(
        "/api/checkPassword",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert chunked.status_code == 413
    private = client.post(
        "/api/createNewFolder",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert private.status_code == 401


def test_owner_logout_revokes_copied_session_cookie(app_client):
    client, _, _, _ = app_client
    login(client)
    token = client.cookies.get("tgdrive_session")
    assert client.get("/api/v1/session").json()["authenticated"] is True
    assert client.post("/api/logout").status_code == 200
    client.cookies.set("tgdrive_session", token)
    assert client.get("/api/v1/session").json()["authenticated"] is False
    assert client.get("/api/v1/jobs").status_code == 401


def test_local_http_owner_session_and_origin_protection(tmp_path):
    settings = make_settings(tmp_path).model_copy(update={"cookie_secure": False})
    store = DriveStore.create(tmp_path / "drive.json")
    app = create_app(settings, runtime=FakeRuntime(store))
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.post(
            "/api/checkPassword",
            json={"pass": "correct horse battery staple"},
            headers={"Origin": "http://localhost:8000"},
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "secure" not in cookie
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert "strict-transport-security" not in response.headers
        assert client.get("/api/v1/session").json()["authenticated"] is True

        for origin in ("http://localhost:8001", "http://127.0.0.1:8000"):
            denied = client.post(
                "/api/createNewFolder",
                json={"path": "/", "name": "Unwanted"},
                headers={
                    "Origin": origin,
                    "X-Forwarded-Host": origin.removeprefix("http://"),
                    "X-Forwarded-Proto": "https",
                },
            )
            assert denied.status_code == 403
        assert store.list_directory("/") == {}
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/v1/session").json()["authenticated"] is False


def test_cross_origin_login_and_logout_do_not_change_owner_session(app_client):
    client, _, _, _ = app_client
    response = client.post(
        "/api/checkPassword",
        json={"pass": "correct horse battery staple"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert client.get("/api/v1/session").json()["authenticated"] is False
    login(client)
    response = client.post("/api/logout", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.get("/api/v1/session").json()["authenticated"] is True


def test_mongodb_outage_refuses_app_auth_without_leaking_provider_error(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from pymongo.errors import ConnectionFailure

    from tgdrive.mongo import DatabaseUnavailable

    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.json")
    sessions = SimpleNamespace(
        find=Mock(side_effect=ConnectionFailure("synthetic-private-database-token"))
    )
    backend = SimpleNamespace(db=SimpleNamespace(sessions=sessions), write=Mock())
    monkeypatch.setattr("tgdrive.mongo.for_directory", lambda _directory: backend)
    try:
        with pytest.raises(
            DatabaseUnavailable, match="Restore database access, then restart TGDrive"
        ) as captured:
            create_app(settings, runtime=FakeRuntime(store))
        assert "synthetic-private-database-token" not in str(captured.value)
        assert not (settings.resolved_data_dir / "auth.sqlite3").exists()
        backend.write.assert_not_called()
    finally:
        store.close()


def test_owner_credential_rotation_preserves_existing_share_links(tmp_path):
    from pydantic import SecretStr

    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.json")
    item = store.add_file("/", "shared.pdf", 7, 100)
    path = "/" + item["id"]
    with TestClient(
        create_app(settings, runtime=FakeRuntime(store)),
        base_url="https://testserver",
    ) as client:
        login(client)
        token = client.post("/api/v1/shares", json={"path": path}).json()["auth"]

    rotated = settings.model_copy(
        update={"admin_password": SecretStr("a replacement owner password")}
    )
    with TestClient(
        create_app(rotated, runtime=FakeRuntime(store)),
        base_url="https://testserver",
    ) as client:
        shared = client.get("/api/v1/preview", params={"path": path, "auth": token})
        assert shared.status_code == 200
        assert client.get("/api/v1/preview", params={"path": path}).status_code == 403


@pytest.mark.parametrize("hashed", [False, True])
def test_restarts_preserve_sessions_but_password_changes_revoke_them(tmp_path, hashed):
    from argon2 import PasswordHasher
    from pydantic import SecretStr

    settings = make_settings(tmp_path)
    if hashed:
        settings = settings.model_copy(
            update={
                "admin_password_hash": SecretStr(
                    PasswordHasher().hash("correct horse battery staple")
                )
            }
        )
    store = DriveStore.create(tmp_path / "drive.json")

    def app(config):
        return create_app(config, runtime=FakeRuntime(store))

    with TestClient(app(settings), base_url="https://testserver") as client:
        login(client)
        token = client.cookies.get("tgdrive_session")
    with TestClient(app(settings), base_url="https://testserver") as client:
        client.cookies.set("tgdrive_session", token)
        assert client.get("/api/v1/session").json()["authenticated"] is True

    replacement = "a replacement owner password"
    changes = {"admin_password": SecretStr(replacement)}
    if hashed:
        changes["admin_password_hash"] = SecretStr(PasswordHasher().hash(replacement))
    rotated = settings.model_copy(update=changes)
    with TestClient(app(rotated), base_url="https://testserver") as client:
        client.cookies.set("tgdrive_session", token)
        assert client.get("/api/v1/session").json()["authenticated"] is False
        assert client.get("/api/v1/jobs").status_code == 401
        assert (
            client.post("/api/checkPassword", json={"pass": replacement}).status_code
            == 200
        )


def test_hashed_password_preserves_intentional_whitespace(tmp_path):
    from argon2 import PasswordHasher
    from pydantic import SecretStr

    password = "  correct horse battery staple  "
    settings = make_settings(tmp_path).model_copy(
        update={"admin_password_hash": SecretStr(PasswordHasher().hash(password))}
    )
    store = DriveStore.create(tmp_path / "drive.json")
    app = create_app(settings, runtime=FakeRuntime(store))
    with TestClient(app, base_url="https://testserver") as client:
        assert (
            client.post(
                "/api/checkPassword", json={"pass": password.strip()}
            ).status_code
            == 401
        )
        assert (
            client.post("/api/checkPassword", json={"pass": password}).status_code
            == 200
        )


def test_password_hashing_does_not_stall_health_or_spawn_unbounded_work(
    app_client, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    client, _, _, _ = app_client
    checking = Event()
    release = Event()

    def slow_password_check(_candidate, _expected):
        checking.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(app_module, "verify_password", slow_password_check)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(client.post, "/api/checkPassword", json={"pass": "test"})
        try:
            assert checking.wait(5)
            assert client.get("/health/live").status_code == 200
            busy = client.post("/api/checkPassword", json={"pass": "test"})
            assert busy.status_code == 429
            assert busy.headers["retry-after"] == "1"
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Referer": "https://sub.testserver/settings"},
        {"Referer": "https://evil.example/settings"},
        {"Origin": "https://testserver:0"},
        [("Origin", "https://evil.example"), ("Origin", "https://testserver")],
    ],
)
def test_browser_mutations_reject_missing_or_ambiguous_origin(app_client, headers):
    client, store, _, _ = app_client
    login(client)
    denied = client.post(
        "/api/createNewFolder",
        json={"path": "/", "name": "Unwanted"},
        headers=headers,
    )
    assert denied.status_code == 403
    assert store.list_directory("/") == {}


def test_legacy_same_origin_referrer_and_headerless_api_clients_are_allowed(app_client):
    client, _, _, _ = app_client
    login(client)
    for index, headers in enumerate(
        (
            {"Referer": "https://testserver/settings?tab=files"},
            {},
            {"Sec-Fetch-Site": "same-origin"},
        )
    ):
        response = client.post(
            "/api/createNewFolder",
            json={"path": "/", "name": f"Allowed {index}"},
            headers=headers,
        )
        assert response.status_code == 200


def test_v1_chunk_receipt_requires_owner_and_durable_offsets(app_client):
    import hashlib

    client, _, _, _ = app_client
    assert (
        client.post(
            "/api/v1/uploads",
            json={
                "name": "receipt.bin",
                "parent_id": "/",
                "total_size": 3,
            },
        ).status_code
        == 401
    )
    login(client)
    created = client.post(
        "/api/v1/uploads",
        json={
            "name": "../../receipt.bin",
            "parent_id": "/",
            "total_size": 3,
        },
    )
    assert created.status_code == 201
    session = created.json()
    assert session["name"] == "receipt.bin"
    address = "/api/v1/uploads/" + session["id"]
    assert client.head(address).headers["upload-offset"] == "0"
    assert (
        client.patch(
            address,
            content=b"abc",
            headers={
                "Content-Type": "application/octet-stream",
                "Upload-Offset": "0",
            },
        ).status_code
        == 204
    )
    assert client.head(address).headers["upload-offset"] == "3"
    item = client.get(address).json()
    assert item["checksums"] == [
        {
            "offset": 0,
            "size": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
        }
    ]
    assert (
        client.patch(
            address,
            content=b"abc",
            headers={
                "Content-Type": "application/octet-stream",
                "Upload-Offset": "0",
            },
        ).status_code
        == 409
    )


def test_v1_move_to_shared_folder_requires_confirmed_exposure(app_client):
    client, store, _, _ = app_client
    source = store.create_folder("/", "Private")
    destination = store.create_folder("/", "Published")
    login(client)
    share = client.post(
        "/api/v1/shares",
        json={
            "path": "/" + destination["id"],
            "expires_in_days": None,
        },
    )
    assert share.status_code == 201
    assert share.json()["expires_at"] is None
    listing = client.get("/api/v1/shares").json()["shares"]
    assert listing[0]["name"] == "Published"
    move = {"paths": ["/" + source["id"]], "destination": "/" + destination["id"]}
    rejected = client.post("/api/v1/nodes/move", json=move)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED"
    assert store.get_directory("/" + source["id"])["name"] == "Private"
    move["allow_public_exposure"] = True
    assert client.post("/api/v1/nodes/move", json=move).status_code == 200


def test_explicit_item_share_survives_move_but_inherited_share_does_not(
    app_client,
    monkeypatch,
):
    client, store, _, _ = app_client
    shared = store.create_folder("/", "Shared")
    private = store.create_folder("/", "Private")
    item = store.add_file("/" + shared["id"], "report.txt", 343, 4)
    old_path = "/" + shared["id"] + "/" + item["id"]
    inherited_token = store.create_share("/" + shared["id"])
    own_token = store.create_share(old_path)
    login(client)
    moved = client.post(
        "/api/v1/nodes/move",
        json={
            "paths": [old_path],
            "destination": "/" + private["id"],
        },
    )
    assert moved.status_code == 200

    async def content(**kwargs):
        return Response(b"safe")

    monkeypatch.setattr(app_module, "stream_telegram_file", content)
    client.cookies.clear()
    assert (
        client.get(
            "/file",
            params={
                "path": old_path,
                "auth": inherited_token,
            },
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/file",
            params={
                "path": old_path,
                "auth": own_token,
            },
        ).content
        == b"safe"
    )


def test_preview_and_media_require_owner_or_in_scope_share(app_client):
    client, store, _runtime, _settings = app_client
    folder = store.create_folder("/", "Shared")
    folder_path = "/" + folder["id"]
    file = store.add_file(folder_path, "manual.pdf", 7, 100)
    path = folder_path + "/" + file["id"]
    other = store.add_file("/", "private.pdf", 8, 100)
    for endpoint in ("/api/v1/preview", "/api/v1/media"):
        assert client.get(endpoint, params={"path": path}).status_code == 403
    token = store.create_share(folder_path)
    preview = client.get("/api/v1/preview", params={"path": path, "auth": token})
    assert preview.status_code == 200
    assert preview.json()["kind"] == "pdf"
    assert "preview=1" in preview.json()["url"]
    assert (
        client.get(
            "/api/v1/preview", params={"path": "/" + other["id"], "auth": token}
        ).status_code
        == 403
    )
    store.revoke_share(folder_path)
    assert (
        client.get("/api/v1/preview", params={"path": path, "auth": token}).status_code
        == 403
    )
    login(client)
    assert client.get("/api/v1/preview", params={"path": path}).status_code == 200


def test_unsupported_preview_does_not_open_telegram(app_client):
    client, store, _runtime, _settings = app_client
    item = store.add_file("/", "archive.zip", 7, 123)
    login(client)
    response = client.get("/api/v1/preview", params={"path": "/" + item["id"]})
    assert response.status_code == 200
    assert response.json()["kind"] == "unsupported"
    assert "Download" in response.json()["reason"]


def test_bundled_viewer_license_is_readable_without_owner_session(
    app_client: tuple[Any, ...],
) -> None:
    client, _, _, _ = app_client
    response = client.get("/third-party/movi-player-LICENSE.txt")
    assert response.status_code == 200
    assert "Apache License" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    denied = client.get("/third-party/%2e%2e/%2e%2e/.env")
    assert denied.status_code == 404


def test_recopied_share_retains_token_after_rename_move_and_revokes(
    app_client, monkeypatch
):
    client, store, _, _ = app_client
    item = store.add_file("/", "original.txt", 42, 4)
    original = "/" + item["id"]
    target = store.create_folder("/", "Reports")
    login(client)
    created = client.post(
        "/api/v1/shares", json={"path": original, "expires_in_days": None}
    )
    assert created.status_code == 201
    token = created.json()["auth"]
    listing = client.get("/api/v1/shares")
    assert listing.headers["cache-control"] == "no-store"
    share = listing.json()["shares"][0]
    assert share["copyable"] and share["folder_label"] == "My Files"
    assert "auth" not in share and "token_hash" not in share
    assert token not in str(store.export())
    store.rename(original, "renamed.txt")
    store.move(original, "/" + target["id"])
    result = client.post("/api/v1/shares/link", json={"path": original})
    assert result.status_code == 200
    assert result.json()["auth"] == token
    assert result.headers["cache-control"] == "no-store"
    listed = client.get("/api/v1/shares").json()["shares"][0]
    assert listed["name"] == "renamed.txt"
    assert listed["folder_label"] == "My Files / Reports"

    async def content(**kwargs):
        return Response(b"safe")

    monkeypatch.setattr(app_module, "stream_telegram_file", content)
    client.cookies.clear()
    assert (
        client.post("/api/v1/shares/link", json={"path": original}).status_code == 401
    )
    assert (
        client.get("/file", params={"path": original, "auth": token}).content == b"safe"
    )
    login(client)
    assert (
        client.request("DELETE", "/api/v1/shares", json={"path": original}).status_code
        == 200
    )
    assert (
        client.post("/api/v1/shares/link", json={"path": original}).status_code == 410
    )
    client.cookies.clear()
    assert (
        client.get("/file", params={"path": original, "auth": token}).status_code == 403
    )


def test_expired_shares_listed_and_legacy_links_not_rotated(app_client, monkeypatch):
    client, store, _, _settings = app_client
    item = store.add_file("/", "legacy.txt", 52, 4)
    path = "/" + item["id"]
    token = store.create_share(path)
    login(client)
    assert client.get("/api/v1/shares").json()["shares"][0]["copyable"] is False
    assert client.post("/api/v1/shares/link", json={"path": path}).status_code == 409
    assert store.authorize_file(path, token)
    assert len(store.list_shares()) == 1
    monkeypatch.setattr("tgdrive.store._utc_now", lambda: "9999-01-01T00:00:00Z")
    assert store.list_shares() == []
    assert len(client.get("/api/v1/shares").json()["shares"]) == 1
    assert client.post("/api/v1/shares/link", json={"path": path}).status_code == 410
    assert not store.authorize_file(path, token)
