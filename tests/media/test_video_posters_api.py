"""Poster reads preserve file access boundaries and never fetch original video bytes."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

import tests.support.app_fixtures as fixtures
from tests.media.test_multipart_streaming import MultipartClient
from tgdrive.media.generated_thumbnails import location, save
from tgdrive.media.posters import load_poster
from tgdrive.media.thumbnails import Thumbnail, ThumbnailCache
from tgdrive.storage.file_parts import MAX_POSTER_BYTES
from tgdrive.telegram.client_pool import ClientPool

app_client = fixtures.app_client
# Transport/signature fixture; browser tests own actual image decoding quality.
JPEG = b"\xff\xd8\xff" + b"poster-frame" * 14000 + b"\xff\xd9"
POSTER = {
    "message_id": 201,
    "size": len(JPEG),
    "sha256": hashlib.sha256(JPEG).hexdigest(),
    "document_id": 9001,
}


def setup_poster(app_client, monkeypatch, *, content=JPEG, before_read=None):
    client, store, _, _ = app_client
    item = store.add_file("/", "video.mkv", 101, 9_000_000_000, poster=POSTER)
    reads = []

    async def read(_pool, _channel, data, start, count, authorize, *, background):
        await authorize()
        reads.append((data, start, count, background))
        if before_read:
            before_read()
        return content

    monkeypatch.setattr("tgdrive.media.books.read_range", read)
    return client, store, "/" + item["id"], reads


def test_poster_owner_cache_and_conditional_response_always_require_access(
    app_client, monkeypatch
):
    client, _, path, reads = setup_poster(app_client, monkeypatch)
    assert client.get("/api/v1/poster", params={"path": path}).status_code == 403
    assert reads == []
    fixtures.login(client)
    response = client.get("/api/v1/poster", params={"path": path})
    assert response.status_code == 200
    assert response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["vary"] == "Cookie"
    assert response.headers["x-content-type-options"] == "nosniff"
    source, start, count, background = reads[0]
    assert source == {
        "message_id": 201,
        "size": len(JPEG),
        "parts": [{"index": 0, "offset": 0, **POSTER}],
    }
    assert (start, count, background) == (0, len(JPEG), True)
    headers = {"If-None-Match": response.headers["etag"]}
    assert (
        client.get("/api/v1/poster", params={"path": path}, headers=headers).status_code
        == 304
    )
    assert len(reads) == 1
    assert (
        client.get(
            "/api/v1/poster", params={"path": path, "auth": "invalid"}
        ).status_code
        == 403
    )
    client.post("/api/logout")
    assert (
        client.get("/api/v1/poster", params={"path": path}, headers=headers).status_code
        == 403
    )
    assert len(reads) == 1


def test_poster_share_scope_revocation_and_trash_recheck_cached_payload(
    app_client, monkeypatch
):
    client, store, path, reads = setup_poster(app_client, monkeypatch)
    private = store.add_file("/", "private.mkv", 102, 10, poster=POSTER)
    token = store.create_share(path)
    params = {"path": path, "auth": token}
    response = client.get("/api/v1/poster", params=params)
    assert response.status_code == 200
    assert (
        client.get(
            "/api/v1/poster", params={"path": "/" + private["id"], "auth": token}
        ).status_code
        == 403
    )
    store.revoke_share(path)
    assert (
        client.get(
            "/api/v1/poster",
            params=params,
            headers={"If-None-Match": response.headers["etag"]},
        ).status_code
        == 403
    )
    fixtures.login(client)
    store.set_trashed(path, True)
    assert client.get("/api/v1/poster", params={"path": path}).status_code == 404
    assert len(reads) == 1


def test_poster_revoked_while_reading_never_returns_the_body(app_client, monkeypatch):
    client, store, path, reads = setup_poster(app_client, monkeypatch)
    token = store.create_share(path)

    async def read(_pool, _channel, data, start, count, authorize, *, background):
        reads.append(data["message_id"])
        store.revoke_share(path)
        return JPEG

    monkeypatch.setattr("tgdrive.media.books.read_range", read)
    response = client.get("/api/v1/poster", params={"path": path, "auth": token})
    assert response.status_code == 403
    assert response.content != JPEG
    assert len(reads) == 1


def test_older_file_has_no_poster_reads_or_new_cache_writes(app_client, monkeypatch):
    client, store, _, settings = app_client
    item = store.add_file("/", "older.mkv", 101, 123)

    async def forbidden(*args, **kwargs):
        raise AssertionError("No poster reference must not fetch original bytes")

    monkeypatch.setattr("tgdrive.media.books.read_range", forbidden)
    fixtures.login(client)
    assert (
        client.get("/api/v1/poster", params={"path": "/" + item["id"]}).status_code
        == 204
    )
    assert not (settings.resolved_cache_dir / "posters").exists()


@pytest.mark.parametrize(
    "content", [JPEG[:-2], b"<svg>unsafe</svg>", b"x" * MAX_POSTER_BYTES]
)
def test_invalid_remote_poster_bytes_never_serve_as_an_image(
    app_client, monkeypatch, content
):
    client, _, path, _ = setup_poster(app_client, monkeypatch, content=content)
    fixtures.login(client)
    response = client.get("/api/v1/poster", params={"path": path})
    assert response.status_code == 204
    assert response.content == b""


def test_poster_transport_failure_is_retryable_and_not_negative_cached(
    app_client, monkeypatch
):
    client, _, path, _ = setup_poster(app_client, monkeypatch)
    fixtures.login(client)

    async def unavailable(*args, **kwargs):
        raise OSError("Disconnected")

    monkeypatch.setattr("tgdrive.media.books.read_range", unavailable)
    response = client.get("/api/v1/poster", params={"path": path})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["code"] == "POSTER_BUSY"

    async def available(*args, **kwargs):
        return JPEG

    monkeypatch.setattr("tgdrive.media.books.read_range", available)
    assert client.get("/api/v1/poster", params={"path": path}).status_code == 200


def test_corrupt_disk_poster_is_refetched_and_checked(app_client, monkeypatch):
    client, _, path, reads = setup_poster(app_client, monkeypatch)
    settings = app_client[3]
    directory = settings.resolved_cache_dir / "posters"
    save(
        directory,
        settings.storage_channel,
        201,
        JPEG[:-3] + b"x\xff\xd9",
        maximum=MAX_POSTER_BYTES,
    )
    fixtures.login(client)
    response = client.get("/api/v1/poster", params={"path": path})
    assert response.status_code == 200 and response.content == JPEG
    assert len(reads) == 1
    assert location(directory, settings.storage_channel, 201).read_bytes() == JPEG


def test_changed_file_or_recovery_pause_cannot_return_a_poster(app_client, monkeypatch):
    changed = False

    def change():
        nonlocal changed
        changed = True

    client, store, path, reads = setup_poster(
        app_client, monkeypatch, before_read=change
    )
    original = store.get_file

    def get_file(value):
        data = original(value)
        if changed:
            data["poster"] = {**data["poster"], "message_id": 202}
        return data

    monkeypatch.setattr(store, "get_file", get_file)
    fixtures.login(client)
    response = client.get("/api/v1/poster", params={"path": path})
    assert response.status_code == 409 and response.content != JPEG
    app_client[2].backup = SimpleNamespace(recovery_required=True)
    assert client.get("/api/v1/poster", params={"path": path}).status_code == 409
    assert len(reads) == 1


async def test_poster_document_identity_is_checked_before_payload_read(tmp_path):
    client = MultipartClient([JPEG])
    pool = ClientPool([client])
    await pool.start()

    async def authorize():
        pass

    try:
        result = await load_poster(
            pool=pool,
            channel_id=-100,
            poster={**POSTER, "message_id": 101, "document_id": 9999},
            directory=tmp_path / "posters",
            authorize=authorize,
        )
        assert result is None
        assert client.fetches == [101]
        assert client.reads == []
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


async def test_poster_cache_coalesces_and_accepts_images_above_grid_thumbnail_cap():
    cache = ThumbnailCache(max_image_bytes=MAX_POSTER_BYTES)
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return Thumbnail(JPEG, "image/jpeg")

    try:
        first, second = await asyncio.gather(
            cache.get((1,), load), cache.get((1,), load)
        )
        assert first is second and first.content == JPEG
        assert calls == 1 and cache.bytes == len(JPEG)
    finally:
        await cache.close()
