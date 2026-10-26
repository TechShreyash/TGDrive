from __future__ import annotations

import pytest

import tests.support.app_fixtures as fixtures
from tests.support.book_samples import cbz, epub

app_client = fixtures.app_client


def test_book_owner_share_revocation_and_passive_resources(app_client, monkeypatch):
    client, store, _, _ = app_client
    content = epub(unsafe=True)
    file = store.add_file("/", "book.epub", 250, len(content))
    path = "/" + file["id"]
    reads = []

    async def fetch(_pool, _channel, data, start, count, authorize):
        await authorize()
        reads.append((start, count))
        return content[start : start + count]

    monkeypatch.setattr("tgdrive.media.books.read_range", fetch)
    assert client.get("/api/v1/books", params={"path": path}).status_code == 403
    assert reads == []
    fixtures.login(client)
    index = client.get("/api/v1/books", params={"path": path})
    assert index.status_code == 200 and index.json()["kind"] == "epub"
    first = store.create_share(path)
    second = store.create_share(path)
    chapter = client.get(
        "/api/v1/books", params={"path": path, "auth": first, "entry": "OPS/one.xhtml"}
    )
    assert chapter.status_code == 200
    assert "bookExecuted" not in chapter.text and "external.invalid" not in chapter.text
    image = client.get(
        "/api/v1/books",
        params={"path": path, "auth": second, "entry": "OPS/images/page.png"},
    )
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "private, no-cache"
    row = store.list_shares(path)[0]
    store.revoke_share(row["id"])
    assert (
        client.get("/api/v1/books", params={"path": path, "auth": first}).status_code
        == 403
    )
    assert (
        client.get("/api/v1/books", params={"path": path, "auth": second}).status_code
        == 200
    )
    store.set_trashed(path, True)
    assert (
        client.get("/api/v1/books", params={"path": path, "auth": second}).status_code
        == 403
    )


def test_book_revoked_during_fetch_cannot_return_cached_or_parsed_data(
    app_client, monkeypatch
):
    client, store, _, _ = app_client
    content = cbz()
    file = store.add_file("/", "comic.cbz", 251, len(content))
    path = "/" + file["id"]
    token = store.create_share(path)

    async def fetch(_pool, _channel, data, start, count, authorize):
        store.revoke_share(path)
        return content[start : start + count]

    monkeypatch.setattr("tgdrive.media.books.read_range", fetch)
    response = client.get("/api/v1/books", params={"path": path, "auth": token})
    assert response.status_code == 403


@pytest.mark.parametrize("entry", ["../secret", "/etc/passwd", "missing.xhtml"])
def test_book_resource_requests_are_bounded(app_client, monkeypatch, entry):
    client, store, _, _ = app_client
    content = epub()
    file = store.add_file("/", "book.epub", 252, len(content))
    path = "/" + file["id"]

    async def fetch(_pool, _channel, data, start, count, authorize):
        return content[start : start + count]

    monkeypatch.setattr("tgdrive.media.books.read_range", fetch)
    fixtures.login(client)
    assert (
        client.get("/api/v1/books", params={"path": path, "entry": entry}).status_code
        == 422
    )


def test_cached_book_bytes_and_conditional_responses_still_require_access(
    app_client, monkeypatch
):
    client, store, _, _ = app_client
    content = cbz()
    file = store.add_file("/", "cached.cbz", 253, len(content))
    path = "/" + file["id"]
    share = store.create_share(path)
    reads = []

    async def fetch(_pool, _channel, data, start, count, authorize):
        await authorize()
        reads.append((start, count))
        return content[start : start + count]

    monkeypatch.setattr("tgdrive.media.books.read_range", fetch)
    params = {"path": path, "auth": share, "entry": "pages/2.png"}
    first = client.get("/api/v1/books", params=params)
    assert first.status_code == 200
    before = len(reads)
    second = client.get("/api/v1/books", params=params)
    assert second.content == first.content and len(reads) == before
    conditional = client.get(
        "/api/v1/books", params=params, headers={"If-None-Match": first.headers["etag"]}
    )
    assert (
        conditional.status_code == 304
        and not conditional.content
        and len(reads) == before
    )
    store.revoke_share(path)
    assert (
        client.get(
            "/api/v1/books",
            params=params,
            headers={"If-None-Match": first.headers["etag"]},
        ).status_code
        == 403
    )
