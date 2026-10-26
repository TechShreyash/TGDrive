from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import Response

import tests.support.app_fixtures as fixtures
from tgdrive.media.streaming import _prime_response
from tgdrive.telegram.client_pool import MediaQueueFull

app_client = fixtures.app_client


def test_file_status_keeps_retry_guidance_scoped_to_authorized_file(
    app_client, monkeypatch
):
    client, store, runtime, _settings = app_client
    file = store.add_file("/", "image.png", 77, 5)
    path = "/" + file["id"]
    auth = store.create_share(path)
    params = {"path": path, "auth": auth, "request_id": "viewer"}
    runtime.pool = SimpleNamespace(queue_estimate=lambda _id: None)

    async def overloaded(**kwargs):
        assert kwargs["request_status"]["phase"] == "checking_file"
        raise MediaQueueFull(retry_after=17)

    monkeypatch.setattr("tgdrive.web.app.stream_telegram_file", overloaded)
    response = client.get("/file", params=params)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "17"
    status = client.get("/api/v1/file-status", params=params).json()
    assert status["phase"] == "retry_wait"
    assert status["code"] == "TELEGRAM_QUEUE_FULL"
    assert 0 < status["retry_after"] <= 17
    assert status["queue"] is None

    other = store.add_file("/", "other.png", 78, 8)
    other_path = "/" + other["id"]
    unrelated = client.get(
        "/api/v1/file-status",
        params=params | {"path": other_path, "auth": store.create_share(other_path)},
    ).json()
    assert unrelated["phase"] is None and unrelated["code"] is None
    assert unrelated["retry_after"] is None and unrelated["queue"] is None
    assert (
        client.get(
            "/api/v1/file-status", params={"path": path, "request_id": "viewer"}
        ).status_code
        == 403
    )

    async def conditional(**kwargs):
        assert kwargs["request_status"]["phase"] == "checking_file"
        assert kwargs["request_status"]["code"] is None
        return Response(status_code=304)

    monkeypatch.setattr("tgdrive.web.app.stream_telegram_file", conditional)
    assert client.get("/file", params=params).status_code == 304
    status = client.get("/api/v1/file-status", params=params).json()
    assert status["phase"] is None and status["retry_after"] is None
    store.revoke_share(path)
    assert client.get("/api/v1/file-status", params=params).status_code == 403


def test_file_status_reports_metadata_and_origin_admission_separately(app_client):
    client, store, runtime, settings = app_client
    file = store.add_file("/", "image.png", 77, 5)
    path = "/" + file["id"]
    params = {"path": path, "auth": store.create_share(path), "request_id": "viewer"}
    queue = None
    runtime.pool = SimpleNamespace(
        queue_estimate=lambda _id: queue,
        _request_files={
            "viewer": dict(
                file=(settings.storage_channel, 77, 5),
                phase="checking_file",
                code=None,
                retry_at=None,
            )
        },
    )
    status = client.get("/api/v1/file-status", params=params).json()
    assert status["phase"] == "checking_file" and status["queue"] is None
    queue = dict(position=2, queued_operations=1, active_operations=0)
    status = client.get("/api/v1/file-status", params=params).json()
    assert status["phase"] == "waiting_for_telegram" and status["queue"] == queue
    runtime.pool._request_files["viewer"]["phase"] = "receiving"
    queue = None
    assert (
        client.get("/api/v1/file-status", params=params).json()["phase"] == "receiving"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("after_headers", [False, True])
@pytest.mark.parametrize("asgi_version", ["2.3", "2.4"])
async def test_stream_overload_preserves_retry_status_before_and_after_headers(
    after_headers, asgi_version
):
    progress = dict(phase="checking_file", code=None, retry_at=None)
    release = AsyncMock()

    async def source():
        if after_headers:
            yield b"first"
        raise MediaQueueFull(retry_after=9)

    response = await _prime_response(
        source(),
        release=release,
        status=200,
        mime_type="image/png",
        headers={},
        request_status=progress,
    )
    if after_headers:
        assert progress["phase"] == "receiving"

        async def receive():
            await asyncio.Event().wait()

        with pytest.raises(MediaQueueFull):
            await response(
                {"type": "http", "asgi": {"spec_version": asgi_version}},
                receive,
                AsyncMock(),
            )
    else:
        assert response.status_code == 503 and response.headers["retry-after"] == "9"
    assert progress["phase"] == "retry_wait"
    assert progress["code"] == "TELEGRAM_QUEUE_FULL"
    assert progress["retry_at"] is not None
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_response_clears_receiving_status():
    progress = dict(phase="checking_file", code=None, retry_at=None)

    async def source():
        yield b"image"

    response = await _prime_response(
        source(),
        release=AsyncMock(),
        status=200,
        mime_type="image/png",
        headers={},
        request_status=progress,
    )
    assert progress["phase"] == "receiving"
    await response(
        {"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), AsyncMock()
    )
    assert progress["phase"] is None


@pytest.mark.asyncio
async def test_cancelled_lease_cleanup_still_clears_receiving_status():
    progress = dict(phase="checking_file", code=None, retry_at=None)

    async def source():
        yield b"image"

    response = await _prime_response(
        source(),
        release=AsyncMock(side_effect=asyncio.CancelledError()),
        status=200,
        mime_type="image/png",
        headers={},
        request_status=progress,
    )
    with pytest.raises(asyncio.CancelledError):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            AsyncMock(),
            AsyncMock(),
        )
    assert progress["phase"] is None
