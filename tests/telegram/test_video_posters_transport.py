"""Generated poster uploads reuse the chosen small image without another decode."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock

import pytest
from telethon import types

from tests.telegram.test_telethon_transport import (
    CHANNEL,
    NativeTransport,
    transport_client,
)
from tgdrive.media import generated_thumbnails

THUMBNAIL = b"\xff\xd8\xffsmall\xff\xd9"


@pytest.mark.parametrize(
    "thumbnail", [THUMBNAIL, None], ids=["video", "poster-sidecar"]
)
async def test_transport_does_not_regenerate_an_explicit_image(
    tmp_path, monkeypatch, thumbnail
):
    generate = AsyncMock(side_effect=AssertionError("unexpected repeated generation"))
    monkeypatch.setattr(generated_thumbnails, "generate", generate)
    native = NativeTransport()
    client = transport_client(native)
    client._thumbnail_directory = tmp_path / "thumbnails"
    uploaded = []

    async def upload(source, *, length, file_name, progress):
        content = source.read()
        assert len(content) == length
        uploaded.append((file_name, content))
        return types.InputFile(len(uploaded), 1, file_name, "")

    client._upload_parts = AsyncMock(side_effect=upload)
    path = tmp_path / "source.upload"
    path.write_bytes(b"abc")
    name = "clip.mp4" if thumbnail else "tgdrive-poster.jpg"
    result = await client.send_staged(
        CHANNEL,
        path,
        file_name=name,
        random_id=123,
        thumbnail=thumbnail,
        generate_thumbnail=False,
        expected_sha256=hashlib.sha256(b"abc").hexdigest(),
    )
    assert result.id == 77
    assert uploaded == [(name, b"abc")] + (
        [("thumbnail.jpg", THUMBNAIL)] if thumbnail else []
    )
    assert bool(native.requests[-1].media.thumb) is bool(thumbnail)
    assert (
        generated_thumbnails.read(client._thumbnail_directory, CHANNEL, result.id)
        == thumbnail
    )
    generate.assert_not_awaited()
    assert path.read_bytes() == b"abc"
