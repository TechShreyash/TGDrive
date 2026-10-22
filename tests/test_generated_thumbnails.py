from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from book_samples import png
from telethon import types
from telethon.errors import RPCError

from tgdrive.generated_thumbnails import MAX_BYTES, generate, read, save
from tgdrive.telegram_clients import Client


@pytest.mark.parametrize("name", ["cover.png", "clip.mp4"])
async def test_native_generation_is_small_and_preserves_original(tmp_path, name):
    if not shutil.which("ffmpeg"):
        pytest.skip("native ffmpeg not installed")
    path = tmp_path / name
    original = (
        png(3)
        if name.endswith("png")
        else Path("tests/fixtures/movtext-subtitles.mp4").read_bytes()
    )
    path.write_bytes(original)
    cover = await generate(path, name)
    assert cover and cover.startswith(b"\xff\xd8\xff") and cover.endswith(b"\xff\xd9")
    assert len(cover) <= MAX_BYTES
    assert path.read_bytes() == original


async def test_disguised_playlist_and_invalid_images_do_not_render(tmp_path):
    image = tmp_path / "private.png"
    image.write_bytes(png(2))
    path = tmp_path / "fake.mp4"
    path.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n"
        + str(image)
        + "\n#EXT-X-ENDLIST"
    )
    assert await generate(path, path.name) is None
    path.write_bytes(b"bad source")
    assert await generate(path, "bad.png") is None
    assert await generate(path, "script.svg") is None


def test_disk_cover_cache_is_small_private_and_channel_scoped(tmp_path, monkeypatch):
    import tgdrive.generated_thumbnails as module

    cover = b"\xff\xd8\xff" + b"cover" * 100
    monkeypatch.setattr(module, "MAX_ENTRIES", 2)
    for message in [10, 11, 12]:
        save(tmp_path, -100, message, cover)
    assert len(list(tmp_path.glob("*.jpg"))) == 2
    assert read(tmp_path, -100, 12) == cover
    assert read(tmp_path, -200, 12) is None
    save(tmp_path, -100, 13, b"x" * (MAX_BYTES + 1))
    assert read(tmp_path, -100, 13) is None
    if __import__("os").name == "posix":
        assert next(iter(tmp_path.glob("*.jpg"))).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("reject", ["none", "thumbnail_upload", "thumbnail_publish"])
async def test_native_upload_keeps_original_and_own_cover_when_thumbnail_is_optional(
    tmp_path, reject
):
    if not shutil.which("ffmpeg"):
        pytest.skip("native ffmpeg not installed")
    path = tmp_path / "photo.png"
    original = png(1)
    path.write_bytes(original)
    calls = []
    published = []

    class Native:
        async def upload_file(self, source, **kwargs):
            payload = source if isinstance(source, bytes) else source.read()
            calls.append(payload)
            if isinstance(source, bytes) and reject == "thumbnail_upload":
                raise OSError("optional transport failed")
            return types.InputFile(1, len(payload), kwargs["file_name"], "")

        async def get_input_entity(self, channel):
            return channel

        async def __call__(self, request):
            published.append(request)
            if request.media.thumb and reject == "thumbnail_publish":
                raise RPCError(None, "THUMB_INVALID", 400)
            return SimpleNamespace()

    client = Client.__new__(Client)
    client.native = Native()
    client._thumbnail_directory = tmp_path / "covers"
    client._sent = AsyncMock(return_value=SimpleNamespace(id=50))
    result = await client.send_staged(
        -100,
        path,
        file_name="photo.png.tgdrive-part-0001",
        preview_name="photo.png",
        random_id=22,
        size=len(original) // 2,
    )
    assert result.id == 50 and calls[0] == original[: len(original) // 2]
    assert calls[1].startswith(b"\xff\xd8\xff")
    assert read(client._thumbnail_directory, -100, 50) == calls[1]
    assert published[-1].random_id == 22
    assert bool(published[-1].media.thumb) == (reject == "none")
    assert path.read_bytes() == original
