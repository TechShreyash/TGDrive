"""Warm-cache cover repair never extends current file or share access."""

from types import SimpleNamespace

import pytest

from tests.media.test_thumbnails import use_thumbnail_pool
from tests.support.app_fixtures import login

pytest_plugins = ("tests.media.test_thumbnails",)

COVER = b"\xff\xd8\xffcached-cover\xff\xd9"


def install_repair(client, pool, *, before_return=None):
    version = SimpleNamespace(data_revision=("cached-original", 1))
    pool._media_read_cache = SimpleNamespace(
        cached_source_version=lambda **_source: version
    )
    calls = []

    async def repair(cache, **kwargs):
        assert cache is pool._media_read_cache
        assert kwargs["version"] is version
        kwargs["authorize"]()
        calls.append(kwargs["source"])
        if before_return is not None:
            before_return()
        return COVER

    client.app.state.thumbnail_repair = SimpleNamespace(get=repair)
    return calls


def test_repaired_thumbnail_reuses_payload_but_rechecks_access(app_client):
    client, store, pool, account, path = use_thumbnail_pool(app_client, content=None)
    calls = install_repair(client, pool)
    login(client)
    token = store.create_share(path)
    client.post("/api/logout")
    params = {"path": path, "auth": token}
    response = client.get("/api/v1/thumbnail", params=params)
    assert response.status_code == 200
    assert response.content == COVER
    assert response.headers["cache-control"] == "private, no-cache"
    assert len(calls) == 1
    assert calls[0]["message_id"] == 123
    conditional = {"If-None-Match": response.headers["etag"]}
    assert (
        client.get("/api/v1/thumbnail", params=params, headers=conditional).status_code
        == 304
    )
    assert len(calls) == account.reads == 1
    store.revoke_share(path)
    assert (
        client.get("/api/v1/thumbnail", params=params, headers=conditional).status_code
        == 403
    )
    assert len(calls) == account.reads == 1


@pytest.mark.parametrize("change", ["revoke", "replace"])
def test_repair_cannot_return_bytes_after_access_or_source_changes(
    app_client, monkeypatch, change
):
    client, store, pool, _account, path = use_thumbnail_pool(app_client, content=None)
    login(client)
    token = store.create_share(path)
    client.post("/api/logout")
    original = store.get_file
    replaced = False

    def file_data(value):
        data = original(value)
        return {**data, "message_id": 999} if replaced else data

    def change_during_repair():
        nonlocal replaced
        if change == "revoke":
            store.revoke_share(path)
        else:
            replaced = True

    monkeypatch.setattr(store, "get_file", file_data)
    calls = install_repair(client, pool, before_return=change_during_repair)
    response = client.get("/api/v1/thumbnail", params={"path": path, "auth": token})
    assert response.status_code == (403 if change == "revoke" else 409)
    assert response.content != COVER
    assert len(calls) == 1
