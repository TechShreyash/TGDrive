from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from telethon import types

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.media.thumbnails import (
    THUMBNAIL_BYTES,
    Thumbnail,
    ThumbnailBusy,
    ThumbnailCache,
    image_type,
    load_thumbnail,
)
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.clients import Client
from tgdrive.web.app import create_app

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZkA"
    "AAAASUVORK5CYII="
)


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.json")
    runtime = FakeRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, settings)
    with TestClient(
        create_app(settings, runtime=runtime), base_url="https://testserver"
    ) as client:
        yield client, store, runtime, settings
    store.close()


class ThumbnailClient:
    def __init__(self, content=PNG, before_download=None):
        self.content = content
        self.before_download = before_download
        self.reads = 0
        self.metadata_reads = 0

    async def get_messages(self, _channel, *, message_ids):
        self.metadata_reads += 1
        return SimpleNamespace(id=message_ids, empty=False)

    async def download_thumbnail(self, _message, **kwargs):
        self.reads += 1
        assert kwargs == dict(
            max_bytes=THUMBNAIL_BYTES, target_size=320, max_dimension=640
        )
        if self.before_download:
            self.before_download()
        return self.content

    async def stream_media(self, *_args, **_kwargs):
        raise AssertionError("Thumbnails must never fetch original-file windows")


class ThumbnailPool:
    def __init__(self, client):
        self.client = client
        self.active = 0
        self.leases = []

    @asynccontextmanager
    async def lease(self, **kwargs):
        self.leases.append(kwargs)
        self.active += 1
        try:
            yield self.client
        finally:
            self.active -= 1


def use_thumbnail_pool(app_client, *, content=PNG, before_download=None):
    client, store, runtime, _settings = app_client
    account = ThumbnailClient(content, before_download)
    pool = ThumbnailPool(account)
    runtime.pool = pool
    file = store.add_file("/", "cover.jpg", 123, 100_000_000)
    return client, store, pool, account, "/" + file["id"]


def test_thumbnail_requires_access_before_fetch_or_cached_response(app_client):
    client, _store, pool, account, path = use_thumbnail_pool(app_client)
    assert client.get("/api/v1/thumbnail", params={"path": path}).status_code == 403
    assert account.reads == 0
    login(client)
    response = client.get("/api/v1/thumbnail", params={"path": path})
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["vary"] == "Cookie"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert account.reads == 1
    assert pool.active == 0
    assert pool.leases == [
        dict(download=True, allow_users=True, respect_cooldown=True, priority="preload")
    ]
    conditional = client.get(
        "/api/v1/thumbnail",
        params={"path": path},
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert account.reads == 1
    client.post("/api/logout")
    assert (
        client.get(
            "/api/v1/thumbnail",
            params={"path": path},
            headers={"If-None-Match": response.headers["etag"]},
        ).status_code
        == 403
    )
    assert account.reads == 1


def test_thumbnail_share_scope_revocation_and_trash_do_not_leak_cached_payload(
    app_client,
):
    client, store, _pool, account, path = use_thumbnail_pool(app_client)
    other = store.add_file("/", "private.jpg", 124, 50)
    login(client)
    token = client.post("/api/getFileShareAuth", json={"path": path}).json()["auth"]
    client.post("/api/logout")
    shared = {"path": path, "auth": token}
    response = client.get("/api/v1/thumbnail", params=shared)
    assert response.status_code == 200
    assert (
        client.get(
            "/api/v1/thumbnail",
            params={
                "path": "/" + other["id"],
                "auth": token,
            },
        ).status_code
        == 403
    )
    assert account.reads == 1
    store.revoke_share(path)
    assert (
        client.get(
            "/api/v1/thumbnail",
            params=shared,
            headers={"If-None-Match": response.headers["etag"]},
        ).status_code
        == 403
    )
    login(client)
    store.set_trashed(path, True)
    assert client.get("/api/v1/thumbnail", params={"path": path}).status_code == 404
    assert account.reads == 1


def test_thumbnail_share_revoked_during_fetch_is_not_returned(app_client):
    client, store, pool, _account, path = use_thumbnail_pool(app_client)
    login(client)
    token = client.post("/api/getFileShareAuth", json={"path": path}).json()["auth"]
    client.post("/api/logout")
    pool.client.before_download = lambda: store.revoke_share(path)
    response = client.get("/api/v1/thumbnail", params={"path": path, "auth": token})
    assert response.status_code == 403
    assert response.content != PNG
    assert pool.active == 0


def test_thumbnail_recovery_and_changed_file_abort_before_payload(
    app_client, monkeypatch
):
    client, store, pool, account, path = use_thumbnail_pool(app_client)
    login(client)
    original = store.get_file
    changed = False

    def file_data(value):
        data = original(value)
        return {**data, "message_id": 999} if changed else data

    def replace_during_read():
        nonlocal changed
        changed = True

    monkeypatch.setattr(store, "get_file", file_data)
    pool.client.before_download = replace_during_read
    response = client.get("/api/v1/thumbnail", params={"path": path})
    assert response.status_code == 409
    assert response.content != PNG
    assert account.reads == 1
    app_client[2].backup = SimpleNamespace(recovery_required=True)
    assert client.get("/api/v1/thumbnail", params={"path": path}).status_code == 409
    assert account.reads == 1


def test_explicit_invalid_share_cannot_use_owner_cookie_or_cached_image(app_client):
    client, _store, _pool, account, path = use_thumbnail_pool(app_client)
    login(client)
    assert client.get("/api/v1/thumbnail", params={"path": path}).status_code == 200
    response = client.get(
        "/api/v1/thumbnail", params={"path": path, "auth": "invalid-share"}
    )
    assert response.status_code == 403
    assert response.content != PNG
    assert account.reads == 1


@pytest.mark.parametrize(
    "content", [None, b"<svg>unsafe</svg>", b"x" * (THUMBNAIL_BYTES + 1)]
)
def test_missing_or_unsafe_thumbnail_is_small_cached_fallback(app_client, content):
    client, _store, _pool, account, path = use_thumbnail_pool(
        app_client, content=content
    )
    login(client)
    for _ in range(2):
        response = client.get("/api/v1/thumbnail", params={"path": path})
        assert response.status_code == 204
        assert response.content == b""
    assert account.reads == 1


def test_thumbnail_transient_transport_failure_is_retryable_not_negative_cached(
    app_client,
):
    client, _store, pool, account, path = use_thumbnail_pool(app_client)
    login(client)

    def unavailable():
        raise OSError("disconnected")

    account.before_download = unavailable
    response = client.get("/api/v1/thumbnail", params={"path": path})
    assert response.status_code == 503
    assert response.json()["code"] == "THUMBNAIL_BUSY"
    assert response.headers["retry-after"] == "5"
    assert pool.active == 0
    account.before_download = None
    assert client.get("/api/v1/thumbnail", params={"path": path}).status_code == 200
    assert account.reads == 2


@pytest.mark.asyncio
async def test_thumbnail_loader_adapter_without_capability_does_not_read_original():
    pool = ThumbnailPool(SimpleNamespace())
    assert await load_thumbnail(pool=pool, channel_id=-100, message_id=1) is None
    assert pool.active == 0


@pytest.mark.asyncio
async def test_thumbnail_cache_singleflight_lru_bytes_and_negative_ttl():
    cache = ThumbnailCache(max_bytes=len(PNG) * 2, max_entries=2, missing_ttl=0)
    entered, release = asyncio.Event(), asyncio.Event()
    reads = 0

    async def loader():
        nonlocal reads
        reads += 1
        entered.set()
        await release.wait()
        return Thumbnail(PNG, "image/png")

    first = asyncio.create_task(cache.get((1,), loader))
    await entered.wait()
    second = asyncio.create_task(cache.get((1,), loader))
    release.set()
    assert (await first).content == (await second).content == PNG
    assert reads == 1
    await cache.get((2,), loader)
    await cache.get((3,), loader)
    assert len(cache._entries) == 2
    assert (1,) not in cache._entries
    assert cache.bytes == len(PNG) * 2
    misses = 0

    async def missing():
        nonlocal misses
        misses += 1
        return None

    await cache.get((4,), missing)
    await cache.get((4,), missing)
    assert misses == 2
    await cache.close()
    assert cache.bytes == 0
    assert not cache._entries


@pytest.mark.asyncio
async def test_thumbnail_cache_limits_work_and_recovers_after_timeout():
    cache = ThumbnailCache(max_pending=2, timeout=0.02)
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    a = asyncio.create_task(cache.get((1,), blocked))
    await entered.wait()
    b = asyncio.create_task(cache.get((2,), blocked))
    await asyncio.sleep(0)
    with pytest.raises(ThumbnailBusy):
        await cache.get((3,), blocked)
    for task in (a, b):
        with pytest.raises(ThumbnailBusy):
            await task

    async def ready():
        return Thumbnail(PNG, "image/png")

    assert (await cache.get((3,), ready)).content == PNG
    await cache.close()


@pytest.mark.asyncio
async def test_cancelled_last_waiter_releases_thumbnail_fill_without_poisoning_next():
    cache = ThumbnailCache()
    entered, finished = asyncio.Event(), asyncio.Event()

    async def blocked():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    first = asyncio.create_task(cache.get((1,), blocked))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await finished.wait()
    assert not cache._flights

    async def ready():
        return Thumbnail(PNG, "image/png")

    assert (await cache.get((1,), ready)).content == PNG
    await cache.close()


@pytest.mark.asyncio
async def test_thumbnail_fill_concurrency_is_two_and_close_releases_waiters():
    cache = ThumbnailCache()
    started = asyncio.Event()
    active = peak = 0

    async def blocked():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    tasks = [asyncio.create_task(cache.get((i,), blocked)) for i in range(4)]
    await started.wait()
    assert active == peak == 2
    await cache.close()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert active == 0
    assert not cache._flights
    with pytest.raises(ThumbnailBusy):
        await cache.get((1,), blocked)


class DownloadIterator:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False
        self.reads = 0

    async def __anext__(self):
        self.reads += 1
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        self.closed = True


class NativeDownload:
    def __init__(self, chunks):
        self.iterator = DownloadIterator(chunks)
        self.calls = []

    def iter_download(self, location, **kwargs):
        self.calls.append((location, kwargs))
        return self.iterator


def transport_message(sizes, *, photo=False):
    kwargs = dict(
        id=7, access_hash=8, file_reference=b"ref", date=datetime.now(UTC), dc_id=2
    )
    if photo:
        media = types.Photo(**kwargs, sizes=sizes)
        return SimpleNamespace(photo=media, document=None)
    media = types.Document(
        **kwargs, mime_type="video/mp4", size=2_000_000_000, attributes=[], thumbs=sizes
    )
    return SimpleNamespace(photo=None, document=media)


@pytest.mark.asyncio
@pytest.mark.parametrize("photo", [False, True])
async def test_transport_chooses_small_explicit_thumb_and_closes_iterator(photo):
    native = NativeDownload([PNG])
    client = Client.__new__(Client)
    client.native = native
    message = transport_message(
        [
            types.PhotoSize(type="s", w=90, h=90, size=1024),
            types.PhotoSize(type="m", w=320, h=180, size=32_000),
            types.PhotoSize(type="x", w=1280, h=720, size=250_000),
            types.VideoSize(type="v", w=320, h=180, size=1000),
        ],
        photo=photo,
    )
    assert await client.download_thumbnail(message) == PNG
    location, args = native.calls[0]
    assert location.thumb_size == "m"
    assert isinstance(
        location,
        types.InputPhotoFileLocation if photo else types.InputDocumentFileLocation,
    )
    assert args == dict(request_size=32768, chunk_size=32768, limit=5, dc_id=2)
    assert native.iterator.closed


@pytest.mark.asyncio
async def test_transport_refuses_large_missing_or_vector_thumbs_without_download():
    client = Client.__new__(Client)
    client.native = NativeDownload([b"never"])
    for sizes in [
        [],
        [types.PhotoSize(type="x", w=320, h=180, size=THUMBNAIL_BYTES + 1)],
        [types.PhotoSize(type="x", w=1280, h=720, size=1000)],
        [types.PhotoPathSize(type="j", bytes=b"svg")],
        [types.PhotoSize(type="", w=320, h=180, size=1000)],
    ]:
        assert await client.download_thumbnail(transport_message(sizes)) is None
    assert client.native.calls == []


@pytest.mark.asyncio
async def test_transport_rejects_oversized_response_and_embedded_thumb_uses_no_rpc():
    client = Client.__new__(Client)
    native = NativeDownload([b"x" * 32768] * 10)
    client.native = native
    message = transport_message([types.PhotoSize(type="m", w=320, h=180, size=1000)])
    assert await client.download_thumbnail(message) is None
    assert native.iterator.reads == 5
    assert native.iterator.closed
    native.calls.clear()
    cached = types.PhotoCachedSize(type="m", w=320, h=180, bytes=PNG)
    assert await client.download_thumbnail(transport_message([cached])) == PNG
    assert native.calls == []


def test_thumbnail_signature_rejects_svg_and_recognizes_only_small_raster_types():
    assert image_type(PNG) == "image/png"
    assert image_type(b"\xff\xd8\xffjpeg") == "image/jpeg"
    assert image_type(b"RIFF0000WEBPdata") == "image/webp"
    assert image_type(b"<svg/>") is None


def test_own_generated_cover_precedes_provider_and_is_authorized(app_client):
    from tgdrive.media.generated_thumbnails import save

    client, store, _runtime, settings = app_client
    cover = b"\xff\xd8\xff" + b"small generated cover"
    file = store.add_file(
        "/",
        "split.mp4",
        650,
        2,
        parts=[
            {"index": 0, "offset": 0, "size": 1, "message_id": 650, "sha256": "a" * 64},
            {"index": 1, "offset": 1, "size": 1, "message_id": 651, "sha256": "b" * 64},
        ],
    )
    path = "/" + file["id"]
    save(
        settings.resolved_cache_dir / "thumbnails", settings.storage_channel, 650, cover
    )
    assert client.get("/api/v1/thumbnail", params={"path": path}).status_code == 403
    login(client)
    token = store.create_share(path)
    client.post("/api/logout")
    response = client.get("/api/v1/thumbnail", params={"path": path, "auth": token})
    assert response.status_code == 200 and response.content == cover
    store.revoke_share(path)
    assert (
        client.get(
            "/api/v1/thumbnail",
            params={"path": path, "auth": token},
            headers={"If-None-Match": response.headers["etag"]},
        ).status_code
        == 403
    )
