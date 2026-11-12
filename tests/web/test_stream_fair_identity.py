"""Real owner/share authorization to fair-group binding; pending check approval."""

from starlette.responses import StreamingResponse

import tgdrive.web.app as app_module
from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login
from tgdrive.media.media_scheduler import current_fair_group


def test_verified_session_groups_one_files_parallel_ranges_without_reusing_request_id(
    app_client, monkeypatch
):
    client, store, _runtime, _settings = app_client
    first = store.add_file("/", "first.txt", 123, 4)["id"]
    second = store.add_file("/", "second.txt", 124, 4)["id"]
    captured = []

    async def stream(**kwargs):
        if kwargs.get("authorize") is not None:
            await kwargs["authorize"]()
        group = current_fair_group()
        assert group is not None
        captured.append((kwargs.get("request_id"), group))

        async def body():
            assert current_fair_group() == group
            if kwargs.get("authorize") is not None:
                await kwargs["authorize"]()
            yield b"data"

        return StreamingResponse(body())

    monkeypatch.setattr(app_module, "stream_telegram_file", stream)
    login(client)
    for identity, request_id in [
        (first, "range-a"),
        (first, "range-b"),
        (second, "other-file"),
    ]:
        response = client.get(
            "/file", params={"path": "/" + identity, "request_id": request_id}
        )
        assert response.status_code == 200
        assert response.content == b"data"
    assert [row[0] for row in captured] == ["range-a", "range-b", "other-file"]
    assert captured[0][1] == captured[1][1]
    assert captured[0][1] != captured[2][1]
    assert current_fair_group() is None


def test_authorized_share_groups_public_viewers_together(app_client, monkeypatch):
    client, store, _runtime, _settings = app_client
    identity = store.add_file("/", "shared.txt", 125, 4)["id"]
    path = "/" + identity
    share = store.create_share(path)
    groups = []

    async def stream(**kwargs):
        groups.append(current_fair_group())

        async def body():
            yield b"data"

        return StreamingResponse(body())

    monkeypatch.setattr(app_module, "stream_telegram_file", stream)
    invalid = client.get(
        "/file", params={"path": path, "auth": "unverified", "request_id": "invented"}
    )
    assert invalid.status_code == 403
    assert not groups
    for request_id in ("public-range-a", "public-range-b"):
        response = client.get(
            "/file", params={"path": path, "auth": share, "request_id": request_id}
        )
        assert response.status_code == 200
    assert groups[0] is not None and groups[0] == groups[1]
    assert share not in groups[0]
    store.revoke_share(path)
    assert client.get("/file", params={"path": path, "auth": share}).status_code == 403
    assert len(groups) == 2
    assert current_fair_group() is None
