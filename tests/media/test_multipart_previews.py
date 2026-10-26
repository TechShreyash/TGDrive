from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.media.test_multipart_streaming import MultipartClient, build_parts
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.media.previews import (
    PDF_PREVIEW_BYTES,
    TEXT_PREVIEW_BYTES,
    describe_media,
    describe_preview,
)
from tgdrive.media.streaming import TelegramFileNotFound
from tgdrive.telegram.client_pool import ClientPool


async def preview(pool, contents, name="note.txt", **options):
    return await describe_preview(
        pool=pool,
        channel_id=-100,
        message_id=101,
        file_name=name,
        size=sum(map(len, contents)),
        path="/folder/file",
        auth="scope&token",
        parts=build_parts(contents),
        **options,
    )


@pytest.mark.asyncio
async def test_unicode_and_inert_html_join_across_part_boundaries():
    contents = [b"caf\xc3", b"\xa9 \xe2", b"\x98\x83<script>bad()</script>"]
    client = MultipartClient(contents)
    pool = ClientPool([client])
    await pool.start()
    try:
        result = await preview(pool, contents, "source.html")
        assert result["kind"] == "text" and result["language"] == "html"
        assert result["text"] == "café ☃<script>bad()</script>"
        assert not result["truncated"]
        assert client.fetches == [101, 102, 103]
        assert client.closed == [101, 102, 103]
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_bounded_prefix_crosses_parts_without_fetching_unneeded_tail():
    contents = [b"a" * (100 * 1024), b"b" * (200 * 1024), b"private-tail" * 100000]
    client = MultipartClient(contents)
    pool = ClientPool([client])
    await pool.start()
    try:
        result = await preview(pool, contents)
        assert (
            result["text"] == (contents[0] + contents[1])[:TEXT_PREVIEW_BYTES].decode()
        )
        assert result["truncated"] and result["preview_bytes"] == TEXT_PREVIEW_BYTES
        assert client.fetches == [101, 102]
        assert client.reads == [(101, 0, 1), (102, 0, 1)]
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


def transport_packets():
    return (b"\x47\x40\x00\x10" + b"\xff" * 184) * 6


@pytest.mark.asyncio
@pytest.mark.parametrize("binary", [True, False])
async def test_ts_preview_sniffs_reassembled_packets_instead_of_extension(binary):
    data = transport_packets() if binary else b"export const answer: number = 42;"
    contents = [data[:3], data[3:190], data[190:]] if binary else [data[:8], data[8:]]
    client = MultipartClient(contents)
    pool = ClientPool([client])
    await pool.start()
    try:
        result = await preview(pool, contents, "source.ts")
        assert result["kind"] == ("video" if binary else "text")
        if not binary:
            assert (
                result["language"] == "typescript" and result["text"] == data.decode()
            )
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_pdf_descriptor_has_bounded_inline_url_without_telegram_fetch():
    contents = [b"%PDF-", b"1.7 sample"]
    result = await preview(object(), contents, "manual.pdf")
    assert result["kind"] == "pdf"
    assert parse_qs(urlsplit(result["url"]).query) == {
        "path": ["/folder/file"],
        "auth": ["scope&token"],
        "preview": ["1"],
    }
    parts = build_parts(contents)
    parts[-1]["size"] += PDF_PREVIEW_BYTES
    result = await describe_preview(
        pool=object(),
        channel_id=-100,
        message_id=101,
        file_name="large.pdf",
        size=PDF_PREVIEW_BYTES + 100,
        path="/file",
        auth=None,
        parts=parts,
    )
    assert result["kind"] == "unsupported" and "url" not in result


@pytest.mark.asyncio
async def test_media_descriptor_has_only_logical_original_without_private_manifest():
    contents = [b"first-video-bytes", b"second-video-bytes"]
    result = await describe_media(
        pool=object(),
        channel_id=-100,
        message_id=101,
        file_name="movie.mp4",
        size=sum(map(len, contents)),
        parts=build_parts(contents),
    )
    assert result["variants"] == []
    assert result["original"]["id"] == "original"
    assert result["original"]["mime_type"] == "video/mp4"
    assert result["original"]["size"] == sum(map(len, contents))
    assert not {"parts", "message_id", "sha256", "document_id"}.intersection(
        result["original"]
    )


@pytest.mark.asyncio
async def test_missing_later_preview_part_releases_previous_and_failed_leases():
    contents = [b"hello ", b"world"]
    client = MultipartClient(contents)
    client.contents.pop(102)
    pool = ClientPool([client])
    await pool.start()
    try:
        with pytest.raises(TelegramFileNotFound):
            await preview(pool, contents)
        assert client.closed == [101]
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_cancelled_preview_prefix_closes_source_and_balances_pool():
    contents = [b"hello ", b"world"]
    client = MultipartClient(contents)
    entered = asyncio.Event()

    async def block(identity, _index):
        if identity == 102:
            entered.set()
            await asyncio.Event().wait()

    client.before_chunk = block
    pool = ClientPool([client])
    await pool.start()
    task = asyncio.create_task(preview(pool, contents))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed == [101, 102]
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


def test_http_multipart_owner_share_scope_stable_move_trash_and_revocation(
    tmp_path, monkeypatch
):
    import hashlib

    from fastapi.testclient import TestClient

    from tests.support.app_fixtures import FakeRuntime, make_settings
    from tgdrive.storage.store import DriveStore
    from tgdrive.web.app import create_app

    configured = make_settings(tmp_path)
    store = DriveStore.create()
    public = store.create_folder("/", "Shared")
    shared_path = "/" + public["id"]
    nested = store.create_folder(shared_path, "Nested")
    nested_path = shared_path + "/" + nested["id"]
    private = store.create_folder("/", "Private")
    private_path = "/" + private["id"]
    contents = [b"hello ", b"world"]
    whole = b"".join(contents)
    item = store.add_file(
        shared_path,
        "note.txt",
        101,
        len(whole),
        parts=build_parts(contents),
        sha256=hashlib.sha256(whole).hexdigest(),
    )
    original_path = shared_path + "/" + item["id"]
    folder_token = store.create_share(shared_path)
    file_token = store.create_share(original_path)
    transport = MultipartClient(contents)
    runtime = FakeRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, configured)
    runtime.pool = ClientPool([transport])
    app = create_app(configured, runtime=runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.portal.call(runtime.pool.start)
            try:
                assert (
                    client.get("/file", params={"path": original_path}).status_code
                    == 403
                )
                for route in ("/api/v1/preview", "/api/v1/media"):
                    assert (
                        client.get(route, params={"path": original_path}).status_code
                        == 403
                    )
                params = {"path": original_path, "auth": folder_token}
                result = client.get("/file", params=params | {"download": "1"})
                assert result.status_code == 200 and result.content == whole
                assert (
                    hashlib.sha256(result.content).hexdigest()
                    == hashlib.sha256(whole).hexdigest()
                )
                assert result.headers["content-disposition"].startswith("attachment;")
                descriptor = client.get("/api/v1/preview", params=params).json()
                assert (
                    descriptor["kind"] == "text" and descriptor["text"] == "hello world"
                )
                media = client.get("/api/v1/media", params=params).json()
                assert (
                    media["original"]["size"] == len(whole) and media["variants"] == []
                )
                assert not any(
                    key in str(media)
                    for key in ("message_id", "document_id", "parts", "sha256")
                )
                head = client.head(
                    "/file", params=params, headers={"Range": "bytes=5-7"}
                )
                assert head.status_code == 206 and head.content == b""
                assert head.headers["content-range"] == "bytes 5-7/11"
                assert (
                    client.get(
                        "/file", params=params, headers={"Range": "bytes=5-7"}
                    ).content
                    == b" wo"
                )
                client.post(
                    "/api/checkPassword", json={"pass": "correct horse battery staple"}
                )
                assert (
                    client.get("/file", params={"path": original_path}).content == whole
                )
                store.move(original_path, nested_path)
                # UUID resolution keeps old item URLs valid inside the shared subtree.
                assert (
                    client.get("/file", params={"path": original_path}).content == whole
                )
                client.post("/api/logout", json={})
                assert client.get("/file", params=params).content == whole
                current_path = nested_path + "/" + item["id"]
                store.set_trashed(current_path, True)
                assert client.get("/file", params=params).status_code == 403
                store.set_trashed(current_path, False)
                assert client.get("/file", params=params).content == whole
                store.move(current_path, private_path)
                for route in ("/file", "/api/v1/preview", "/api/v1/media"):
                    assert client.get(route, params=params).status_code == 403
                # A separate explicit file share follows that same file only.
                assert (
                    client.get(
                        "/file", params={"path": original_path, "auth": file_token}
                    ).status_code
                    == 403
                )
                # Trash revoked the explicit file token; create a fresh scoped link.
                moved_path = private_path + "/" + item["id"]
                fresh = store.create_share(moved_path)
                assert (
                    client.get(
                        "/file", params={"path": original_path, "auth": fresh}
                    ).content
                    == whole
                )
                store.revoke_share(moved_path)
                assert (
                    client.get(
                        "/file", params={"path": original_path, "auth": fresh}
                    ).status_code
                    == 403
                )
                assert client.portal.call(runtime.pool.health).active_leases == 0
            finally:
                client.portal.call(runtime.pool.stop)
    finally:
        store.close()
        runtime.transfers.registry.close()
