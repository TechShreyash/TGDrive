from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

import tgdrive.web.app as app_module
from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.app_fixtures import app_client as app_client
from tests.support.mongo_fixtures import FakeMongoBackend, register_test_backend
from tgdrive.storage.store import DriveStore
from tgdrive.web.app import create_app


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

    traversal = client.get("/static/%2e%2e/%2e%2e/.env")
    assert traversal.status_code == 404
    assert traversal.json() == {"detail": "Not Found"}


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


@pytest.mark.parametrize(
    "path,method",
    [
        ("/", "get_directory"),
        ("/trash", "list_trash"),
        ("/search_report", "search_page"),
        ("shared", "get_directory"),
    ],
)
def test_directory_reads_and_serialization_run_off_the_event_loop(
    app_client, monkeypatch, path, method
):
    client, store, _, _ = app_client
    login(client)
    payload = {"path": path}
    if path == "shared":
        folder = store.create_folder("/", "Shared")
        folder_path = "/" + folder["id"]
        payload = {
            "path": "/share_" + folder["id"],
            "auth": store.create_share(folder_path),
        }
        client.post("/api/logout")
    assert client.portal is not None
    loop_thread = client.portal.call(threading.get_ident)
    read = getattr(store, method)
    response_type = app_module.JSONResponse
    calls = []

    def worker_read(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        calls.append("read")
        return read(*args, **kwargs)

    def worker_response(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        calls.append("response")
        return response_type(*args, **kwargs)

    monkeypatch.setattr(store, method, worker_read)
    monkeypatch.setattr(app_module, "JSONResponse", worker_response)
    response = client.post("/api/getDirectory", json=payload)
    assert response.status_code == 200
    assert response.json()["data"]["contents"] == {}
    assert calls == ["read", "response"]


def test_wrong_password_and_known_folder_ids_cannot_browse_private_directories(
    app_client,
):
    client, store, _, _ = app_client
    private = store.create_folder("/", "Private directory")
    private_path = "/" + private["id"]
    child = store.create_folder(private_path, "Private subdirectory")
    child_path = private_path + "/" + child["id"]
    store.add_file(child_path, "private-document.txt", 123, 4)

    wrong = client.post("/api/checkPassword", json={"pass": "wrong"})
    assert wrong.status_code == 401
    assert "tgdrive_session" not in client.cookies

    for path in ("/", private_path, child_path):
        for auth in (None, "wrong"):
            denied = client.post(
                "/api/getDirectory",
                json={"path": path, "auth": auth, "password": "wrong"},
            )
            assert denied.status_code == 401
            assert "data" not in denied.json()
            assert "private-document.txt" not in denied.text

    for path in (private_path, child_path):
        for auth in (None, "wrong"):
            denied = client.post(
                "/api/getDirectory",
                json={"path": "/share_" + path.lstrip("/"), "auth": auth},
            )
            assert denied.status_code == 403
            assert "data" not in denied.json()
            assert "private-document.txt" not in denied.text


def test_revoked_client_returns_actionable_503_and_owner_status(app_client):
    from telethon.errors import SessionRevokedError

    from tests.telegram.test_client_pool import FakeClient
    from tgdrive.telegram.client_pool import ClientPool

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


def test_legacy_delete_accepts_hidden_pending_intent(
    app_client: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store, runtime, _ = app_client
    login(client)
    node = store.create_folder("/", "Deleting")
    path = "/" + node["id"]
    store.set_trashed(path, True)
    intent = store.begin_deletion(path)
    assert store.list_trash() == {}

    async def pending_deletion(selected_path):
        assert selected_path == path
        return SimpleNamespace(id=intent["id"])

    monkeypatch.setattr(runtime.transfers, "start_deletion", pending_deletion)
    response = client.post("/api/deleteFileFolder", json={"path": path})
    assert response.status_code == 202
    assert response.json()["id"] == intent["id"]
    assert len(store.pending_deletions()) == 1


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


def test_upload_remote_and_transfer_progress_endpoints(
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

    progress = client.post("/api/getTransferProgress", json={"id": transfer_id})
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


@pytest.mark.parametrize("single_threaded", [False, True])
def test_url_start_returns_job_without_contacting_source(
    app_client, monkeypatch, single_threaded
):
    from unittest.mock import AsyncMock

    client, _, runtime, _ = app_client
    login(client)
    inspect = AsyncMock(
        side_effect=AssertionError("source lookup blocked start request")
    )
    monkeypatch.setattr(runtime.transfers, "inspect_remote", inspect)
    response = client.post(
        "/api/startFileDownloadFromUrl",
        json={
            "url": "https://example.com/slow-source.mov",
            "path": "/",
            "singleThreaded": single_threaded,
        },
    )
    assert response.status_code == 200
    assert response.json()["id"]
    assert runtime.transfers.remote_modes == [single_threaded]
    inspect.assert_not_awaited()


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


async def test_devices_share_owner_access_but_keep_independent_sessions(
    tmp_path, monkeypatch
):
    import asyncio
    from contextlib import AsyncExitStack

    import httpx2

    from tgdrive.core.security import SESSION_COOKIE

    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.json")
    runtime = FakeRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, settings)
    app = create_app(settings, runtime=runtime)
    collection = runtime.mongo.db.sessions

    try:
        async with app.router.lifespan_context(app), AsyncExitStack() as stack:
            devices = [
                await stack.enter_async_context(
                    httpx2.AsyncClient(
                        transport=httpx2.ASGITransport(app=app),
                        base_url="https://testserver",
                    )
                )
                for _ in range(3)
            ]
            password = {"pass": "correct horse battery staple"}
            simultaneous_logins = await asyncio.gather(
                *(
                    device.post("/api/checkPassword", json=password)
                    for device in devices[:2]
                )
            )
            assert sorted(response.status_code for response in simultaneous_logins) == [
                200,
                429,
            ]
            for device, response in zip(devices, simultaneous_logins, strict=False):
                if response.status_code == 429:
                    assert response.headers["retry-after"] == "1"
                    assert response.json()["detail"] == "Login busy; retry shortly"
                    assert response.json()["code"] == "LOGIN_BUSY"
                    assert (
                        await device.post("/api/checkPassword", json=password)
                    ).status_code == 200

            original_tokens = [
                device.cookies.get(SESSION_COOKIE) for device in devices[:2]
            ]
            assert all(original_tokens) and original_tokens[0] != original_tokens[1]
            changes = await asyncio.gather(
                *(
                    device.post(
                        "/api/createNewFolder",
                        json={"path": "/", "name": f"Device {index}"},
                    )
                    for index, device in enumerate(devices[:2])
                )
            )
            assert [response.status_code for response in changes] == [200, 200]
            assert (
                await devices[2].post("/api/checkPassword", json=password)
            ).status_code == 200
            assert [
                device.cookies.get(SESSION_COOKIE) for device in devices[:2]
            ] == original_tokens
            assert len({device.cookies.get(SESSION_COOKIE) for device in devices}) == 3
            assert all(
                response.status_code == 200
                for response in await asyncio.gather(
                    *(device.get("/api/v1/nodes") for device in devices)
                )
            )
            assert collection.count_documents({}) == 3

            assert (await devices[0].post("/api/logout")).status_code == 200
            assert devices[0].cookies.get(SESSION_COOKIE) is None
            devices[0].cookies.set(SESSION_COOKIE, original_tokens[0])
            assert (await devices[0].get("/api/v1/nodes")).status_code == 401
            assert (
                await devices[0].post(
                    "/api/createNewFolder", json={"path": "/", "name": "Revoked device"}
                )
            ).status_code == 401
            assert collection.count_documents({}) == 2
            remaining = await asyncio.gather(
                *(
                    device.post(
                        "/api/createNewFolder",
                        json={"path": "/", "name": f"Still active {index}"},
                    )
                    for index, device in enumerate(devices[1:], 1)
                )
            )
            assert [response.status_code for response in remaining] == [200, 200]
            for device in devices[1:]:
                listing = await device.get("/api/v1/nodes")
                assert listing.status_code == 200
                assert {item["name"] for item in listing.json()["items"]} == {
                    "Device 0",
                    "Device 1",
                    "Still active 1",
                    "Still active 2",
                }
    finally:
        store.close()


@pytest.mark.parametrize(
    "cookie_state", ["missing", "invalid", "expired", "revoked", "valid"]
)
def test_logout_only_writes_for_active_owner_sessions(
    app_client, monkeypatch, cookie_state
):
    import hashlib
    import time
    from unittest.mock import Mock

    from tgdrive.core.security import SESSION_COOKIE, InvalidSession

    client, _, _, _ = app_client
    sessions = client.app.state.sessions
    backend = FakeMongoBackend()
    collection = backend.db.sessions
    backend.write = Mock(wraps=backend.write)
    # Exercise the production MongoDB session path without any external service.
    monkeypatch.setattr(sessions, "_mongo", collection)
    monkeypatch.setattr(sessions, "_mongo_backend", backend)
    token = None
    if cookie_state == "invalid":
        token = "forged-owner-session"
    elif cookie_state == "expired":
        token = sessions.create(now=int(time.time()) - sessions._ttl_seconds - 1)
    elif cookie_state in {"revoked", "valid"}:
        token = sessions.create()
        if cookie_state == "revoked":
            sessions.revoke(token)
    if token is not None:
        client.cookies.set(SESSION_COOKIE, token)
    backend.write.reset_mock()
    revoke = Mock(wraps=sessions.revoke)
    monkeypatch.setattr(sessions, "revoke", revoke)

    response = client.post("/api/logout")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "Max-Age=0" in response.headers["set-cookie"]
    if cookie_state == "valid":
        revoke.assert_called_once_with(token)
        backend.write.assert_called_once_with(
            collection,
            "delete_one",
            {"_id": hashlib.sha256(token.encode()).hexdigest()},
        )
        with pytest.raises(InvalidSession, match="revoked"):
            sessions.verify(token)
    else:
        revoke.assert_not_called()
        backend.write.assert_not_called()


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


def test_mongodb_outage_refuses_app_auth_without_leaking_provider_error(tmp_path):
    from unittest.mock import Mock

    from pymongo.errors import ConnectionFailure

    from tgdrive.storage.mongo import DatabaseUnavailable

    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.json")
    sessions = SimpleNamespace(
        find=Mock(side_effect=ConnectionFailure("synthetic-private-database-token"))
    )
    backend = SimpleNamespace(db=SimpleNamespace(sessions=sessions), write=Mock())
    runtime = FakeRuntime(store)
    runtime.mongo = backend
    try:
        with pytest.raises(
            DatabaseUnavailable, match="Restore database access, then restart TGDrive"
        ) as captured:
            create_app(settings, runtime=runtime)
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
    backend = FakeMongoBackend()

    def app(config):
        runtime = FakeRuntime(store)
        runtime.mongo = backend
        return create_app(config, runtime=runtime)

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
            assert busy.json()["code"] == "LOGIN_BUSY"
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
    conflict = client.patch(
        address,
        content=b"abc",
        headers={
            "Content-Type": "application/octet-stream",
            "Upload-Offset": "0",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "UPLOAD_OFFSET_CHANGED"


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
    monkeypatch.setattr(
        "tgdrive.storage.store._utc_now", lambda: "9999-01-01T00:00:00Z"
    )
    assert store.list_shares() == []
    assert len(client.get("/api/v1/shares").json()["shares"]) == 1
    assert client.post("/api/v1/shares/link", json={"path": path}).status_code == 410
    assert not store.authorize_file(path, token)
