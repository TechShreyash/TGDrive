from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tgdrive.media.previews import (
    PDF_PREVIEW_BYTES,
    TEXT_PREVIEW_BYTES,
    describe_media,
    describe_preview,
)
from tgdrive.telegram.client_pool import ClientPool


class PreviewClient:
    def __init__(self, content: bytes):
        self.content = content
        self.is_connected = False
        self.name = "preview-test"
        self.reads = 0
        self.closed = False

    async def start(self):
        self.is_connected = True

    async def stop(self):
        self.is_connected = False

    async def get_messages(self, _channel, *, message_ids):
        return SimpleNamespace(
            id=message_ids, document=SimpleNamespace(file_size=len(self.content))
        )

    async def stream_media(self, _message, *, limit, offset):
        try:
            for index in range(offset, offset + limit):
                self.reads += 1
                yield self.content[index * 1024**2 : (index + 1) * 1024**2]
        finally:
            self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "content", "language", "expected"),
    [
        ("readme.md", b"# hello", "markdown", "# hello"),
        (
            "unsafe.html",
            b"<script>window.bad=true</script>",
            "html",
            "<script>window.bad=true</script>",
        ),
        ("unicode.txt", "hello ☃".encode("utf-16"), "text", "hello ☃"),
        ("config.json", b'{"answer":42}', "json", '{"answer":42}'),
        ("table.csv", b"name,value\nalpha,1", "csv", "name,value\nalpha,1"),
    ],
)
async def test_text_previews_return_inert_content_and_release(
    name, content, language, expected
):
    client = PreviewClient(content)
    pool = ClientPool([client])
    await pool.start()
    try:
        result = await describe_preview(
            pool=pool,
            channel_id=-100,
            message_id=7,
            file_name=name,
            size=len(content),
            path="/file",
            auth=None,
        )
        assert result["kind"] == "text"
        assert result["text"] == expected
        assert result["language"] == language
        assert not result["truncated"]
        assert client.closed
        assert all(status.active_leases == 0 for status in await pool.status())
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_large_text_reads_one_bounded_window_and_reports_truncation():
    content = b"x" * (5 * 1024**2)
    client = PreviewClient(content)
    pool = ClientPool([client])
    await pool.start()
    try:
        result = await describe_preview(
            pool=pool,
            channel_id=-100,
            message_id=7,
            file_name="large.log",
            size=len(content),
            path="/file",
            auth=None,
        )
        assert result["text"] == "x" * TEXT_PREVIEW_BYTES
        assert result["truncated"]
        assert result["preview_bytes"] == TEXT_PREVIEW_BYTES
        assert client.reads == 1
        assert client.closed
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_binary_text_name_does_not_send_binary_to_text_renderer():
    client = PreviewClient(b"binary\x00data")
    pool = ClientPool([client])
    await pool.start()
    try:
        result = await describe_preview(
            pool=pool,
            channel_id=-100,
            message_id=7,
            file_name="not-text.txt",
            size=11,
            path="/file",
            auth=None,
        )
        assert result["kind"] == "unsupported"
        assert "text" not in result
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_pdf_descriptor_encodes_share_and_bounds_memory_without_fetch():
    kwargs = dict(
        pool=object(),
        channel_id=-100,
        message_id=7,
        file_name="manual.pdf",
        path="/folder/file",
        auth="share&token",
    )
    result = await describe_preview(size=1024, **kwargs)
    assert result["kind"] == "pdf"
    assert parse_qs(urlsplit(result["url"]).query) == {
        "path": ["/folder/file"],
        "auth": ["share&token"],
        "preview": ["1"],
    }
    for size in (21 * 1024**2, PDF_PREVIEW_BYTES):
        assert (await describe_preview(size=size, **kwargs))["kind"] == "pdf"
    result = await describe_preview(size=PDF_PREVIEW_BYTES + 1, **kwargs)
    assert result["kind"] == "unsupported"
    assert "url" not in result


@pytest.mark.asyncio
async def test_media_metadata_fallback_has_no_fake_quality_variants():
    client = PreviewClient(b"original")
    pool = ClientPool([client])
    await pool.start()
    try:
        result = await describe_media(
            pool=pool,
            channel_id=-100,
            message_id=7,
            file_name="movie.mp4",
            size=8,
        )
        assert result["original"]["id"] == "original"
        assert result["original"]["mime_type"] == "video/mp4"
        assert result["variants"] == []
        assert client.reads == 0
        assert all(status.active_leases == 0 for status in await pool.status())
    finally:
        await pool.stop()
