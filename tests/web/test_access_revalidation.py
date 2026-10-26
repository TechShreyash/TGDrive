"""Capabilities are checked again after slow metadata/preview work."""

import pytest

import tgdrive.web.app as app_module
from tests.support.app_fixtures import app_client as client_fixture
from tests.support.app_fixtures import login

app_client = client_fixture


@pytest.mark.parametrize(
    "endpoint,helper",
    [
        ("/api/v1/preview", "describe_preview"),
        ("/api/v1/media", "describe_media"),
    ],
)
def test_revoked_during_preview_work_is_not_delivered_even_to_owner(
    app_client, monkeypatch, endpoint, helper
):
    client, store, _, _ = app_client
    identity = store.add_file("/", "secret.txt", 123, 4)["id"]
    path = "/" + identity
    token = store.create_share(path)
    login(client)

    async def slow_result(**kwargs):
        assert store.authorize_file(path, token)
        store.revoke_share(path)
        return {"kind": "text", "text": "secret", "size": 4}

    monkeypatch.setattr(app_module, helper, slow_result)
    response = client.get(endpoint, params={"path": path, "auth": token})
    assert response.status_code == 403
    assert "secret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
async def test_revoking_a_share_stops_cached_http_body_even_with_owner_cookie(
    tmp_path, owner
):
    from urllib.parse import urlencode

    from tests.media.test_pooled_media import account
    from tests.media.test_stream_delivery import request
    from tests.media.test_streaming import response_body
    from tests.support.app_fixtures import FakeRuntime, make_settings
    from tgdrive.core.security import SESSION_COOKIE
    from tgdrive.media.media_reader import BLOCK
    from tgdrive.media.streaming import stream_telegram_file
    from tgdrive.storage.store import DriveStore
    from tgdrive.telegram.client_pool import ClientPool

    settings = make_settings(tmp_path)
    payload = b"".join(bytes([index]) * BLOCK for index in range(8))
    client = account("bot", payload)
    pool = ClientPool([client], media_cache_dir=tmp_path / "media-cache")
    await pool.start()
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    identity = store.add_file("/", "private.mkv", 77, len(payload))["id"]
    path = "/" + identity
    token = store.create_share(path)
    runtime = FakeRuntime(store)
    runtime.pool = pool
    app = app_module.create_app(settings, runtime=runtime)
    headers = [(b"host", b"testserver")]
    if owner:
        cookie = app.state.sessions.create()
        headers.append((b"cookie", f"{SESSION_COOKIE}={cookie}".encode()))

    def scope(*, supplied_token=True):
        params = {"path": path}
        if supplied_token:
            params["auth"] = token
        return {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/file",
            "raw_path": b"/file",
            "query_string": urlencode(params).encode(),
            "headers": headers,
            "server": ("testserver", 443),
            "client": ("test", 123),
            "asgi": {"version": "3.0", "spec_version": "2.4"},
        }

    async def receive():
        return {"type": "http.disconnect"}

    sent = []
    try:
        warm = await stream_telegram_file(
            pool=pool,
            channel_id=settings.storage_channel,
            message_id=77,
            file_name="private.mkv",
            expected_size=len(payload),
            request=request(),
        )
        assert await response_body(warm) == payload
        reads = sum(client.native.requests.values())

        async def revoke_on_first_body(value):
            sent.append(value)
            if value["type"] == "http.response.body" and value.get("body"):
                store.revoke_share(path)

        # Once headers/body have started, ASGI cannot replace the status with
        # 403. It aborts delivery instead, keeping already-sent bytes unrecalled.
        with pytest.raises(RuntimeError, match="response already started"):
            await app(scope(), receive, revoke_on_first_body)
        delivered = b"".join(
            value.get("body", b"")
            for value in sent
            if value["type"] == "http.response.body"
        )
        assert delivered == payload[:BLOCK]
        assert sum(client.native.requests.values()) == reads
        assert pool._media_read_cache.scratch.used == 0
        assert pool._media_read_cache._active_streams == 0
        assert not pool._media_read_cache.memory.flights
        assert all(row.active_leases == 0 for row in await pool.status())

        denied = []

        async def collect(value):
            denied.append(value)

        await app(scope(), receive, collect)
        assert denied[0]["status"] == 403
        if owner:
            denied.clear()
            await app(scope(supplied_token=False), receive, collect)
            assert denied[0]["status"] == 200
            assert b"".join(value.get("body", b"") for value in denied) == payload
        assert store.get_file(path)["size"] == len(payload)
    finally:
        await pool._media_read_cache.close()
        await pool.stop()
        app.state.sessions.close()
        runtime.transfers.registry.close()
        store.close()
