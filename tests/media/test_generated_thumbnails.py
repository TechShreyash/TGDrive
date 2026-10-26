from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types
from telethon.errors import RPCError

from tests.support.book_samples import png
from tgdrive.media.generated_thumbnails import MAX_BYTES, generate, read, save
from tgdrive.telegram.clients import Client


@pytest.mark.parametrize("name", ["cover.png", "clip.mp4", "clip.mov"])
async def test_native_generation_is_small_and_preserves_original(tmp_path, name):
    if not shutil.which("ffmpeg"):
        pytest.skip("native ffmpeg not installed")
    path = tmp_path / name
    original = (
        png(3)
        if name.endswith("png")
        else (
            Path(__file__).resolve().parents[1] / "fixtures/movtext-subtitles.mp4"
        ).read_bytes()
    )
    path.write_bytes(original)
    cover = await generate(path, name)
    assert cover and cover.startswith(b"\xff\xd8\xff") and cover.endswith(b"\xff\xd9")
    assert len(cover) <= MAX_BYTES
    assert path.read_bytes() == original


async def test_native_uhd_hevc_mov_has_a_cover_with_bounded_worker_memory(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("native ffmpeg not installed")
    path = tmp_path / "uhd-hevc.mov"
    encoded = await asyncio.to_thread(
        subprocess.run,
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=3840x2160:r=24",
            "-frames:v",
            "12",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-threads",
            "1",
            "-x265-params",
            "pools=none:frame-threads=1:log-level=error",
            "-tag:v",
            "hvc1",
            str(path),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if b"Unknown encoder" in encoded.stderr:
        pytest.skip("native HEVC encoder not installed")
    assert encoded.returncode == 0, encoded.stderr.decode(errors="replace")
    original = path.read_bytes()
    cover = await generate(path, path.name)
    assert cover and cover.startswith(b"\xff\xd8\xff") and cover.endswith(b"\xff\xd9")
    assert len(cover) <= MAX_BYTES
    assert path.read_bytes() == original


@pytest.mark.parametrize("failure", ["timeout", "error", "partial", "oversized"])
def test_video_first_frame_fallback_after_unsuccessful_seek(monkeypatch, failure):
    from tgdrive.media import thumbnail_worker

    cover = b"\xff\xd8\xffcover\xff\xd9"
    calls = []

    def run(command, **kwargs):
        calls.append((command.copy(), kwargs["timeout"]))
        if len(calls) == 1:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return SimpleNamespace(
                returncode=1 if failure == "error" else 0,
                stdout=(
                    cover + b"x" * MAX_BYTES
                    if failure == "oversized"
                    else cover
                    if failure == "error"
                    else cover[:-2]
                ),
            )
        return SimpleNamespace(returncode=0, stdout=cover)

    monkeypatch.setattr(thumbnail_worker, "video_seek_time", lambda *_: "600")
    monkeypatch.setattr(thumbnail_worker.subprocess, "run", run)
    assert thumbnail_worker.render(Path("staged.mov"), ".mov") == cover
    assert [command[command.index("-ss") + 1] for command, _ in calls] == ["600", "0"]
    assert [timeout for _, timeout in calls] == [6, 3]


def test_video_first_frame_fallback_remains_bounded(monkeypatch):
    from tgdrive.media import thumbnail_worker

    timeouts = []

    def run(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(thumbnail_worker, "video_seek_time", lambda *_: "600")
    monkeypatch.setattr(thumbnail_worker.subprocess, "run", run)
    assert thumbnail_worker.render(Path("staged.mov"), ".mov") == b""
    assert timeouts == [6, 3]


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
    import tgdrive.media.generated_thumbnails as module

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
        async def upload_parts(self, source, *, length, file_name, progress):
            payload = source.read()
            assert len(payload) == length
            calls.append(payload)
            if file_name == "thumbnail.jpg" and reject == "thumbnail_upload":
                raise OSError("optional transport failed")
            return types.InputFile(1, len(payload), file_name, "")

        async def get_input_entity(self, channel):
            return channel

        async def __call__(self, request):
            published.append(request)
            if request.media.thumb and reject == "thumbnail_publish":
                raise RPCError(None, "THUMB_INVALID", 400)
            return SimpleNamespace()

    client = Client.__new__(Client)
    client.native = Native()
    client._upload_parts = AsyncMock(side_effect=client.native.upload_parts)
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


@pytest.mark.parametrize("kind", ["epub", "cbz", "cbr", "docx"])
async def test_archive_cover_generation_is_bounded_and_preserves_original(
    tmp_path, kind
):
    from tests.support.book_samples import cbr, cbz, epub

    if kind == "cbr":
        try:
            import libarchive  # noqa: F401
        except ImportError, OSError, AttributeError:
            pytest.skip("Native libarchive is not installed")

    if not shutil.which("ffmpeg"):
        pytest.skip("native ffmpeg not installed")
    if kind == "docx":
        import io
        from zipfile import ZipFile

        archive = io.BytesIO()
        with ZipFile(archive, "w") as output:
            output.writestr("docProps/thumbnail.png", png())
        original = archive.getvalue()
    else:
        original = epub() if kind == "epub" else cbr() if kind == "cbr" else cbz()
    path = tmp_path / ("book." + kind)
    path.write_bytes(original)
    cover = await generate(path, path.name)
    assert cover and cover.startswith(b"\xff\xd8\xff") and cover.endswith(b"\xff\xd9")
    assert len(cover) <= MAX_BYTES
    assert path.read_bytes() == original


def test_large_jpeg_uses_decoder_downsampling_before_scaling(tmp_path, monkeypatch):
    from tgdrive.media import thumbnail_worker

    path = tmp_path / "large.jpg"
    with path.open("wb") as source:
        source.truncate(9 * 1024**2)
    commands = []
    cover = b"\xff\xd8\xffcover\xff\xd9"

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=cover)

    monkeypatch.setattr(thumbnail_worker.subprocess, "run", run)
    assert thumbnail_worker.render(path, ".jpg") == cover
    assert commands[0][commands[0].index("-lowres") + 1] == "3"
    assert commands[0].index("-lowres") < commands[0].index("-i")


@pytest.mark.parametrize("duration", [60, 7200])
@pytest.mark.parametrize("offset", [0, 10, 20])
def test_video_thumbnail_chooses_middle_duration_with_bounded_probe(
    monkeypatch, duration, offset
):
    from tgdrive.media import thumbnail_worker

    def run(command, **kwargs):
        assert command[0] == "ffprobe"
        assert command[command.index("-f") + 1] == "mov"
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert kwargs["timeout"] == 1.5
        return SimpleNamespace(returncode=0, stdout=str(duration).encode())

    monkeypatch.setattr(thumbnail_worker.subprocess, "run", run)
    monkeypatch.setattr(thumbnail_worker.secrets, "randbelow", lambda limit: offset)
    position = float(thumbnail_worker.video_seek_time(Path("staged.mov"), ".mov"))
    assert position == duration * (40 + offset) / 100
    assert duration * 0.4 <= position <= duration * 0.6


@pytest.mark.parametrize("duration", [b"N/A", b"nan", b"inf", b"-1", b"0"])
def test_video_unknown_duration_uses_safe_seek_fallback(monkeypatch, duration):
    from tgdrive.media import thumbnail_worker

    monkeypatch.setattr(
        thumbnail_worker.subprocess,
        "run",
        lambda *_, **__: SimpleNamespace(returncode=0, stdout=duration),
    )
    assert thumbnail_worker.video_seek_time(Path("staged.mov"), ".mov") == "10"


async def test_audio_embedded_artwork_generates_cover_without_altering_audio(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("native encoder unavailable")
    image = tmp_path / "cover.png"
    image.write_bytes(png())
    audio = tmp_path / "music.flac"
    encoded = await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-i",
            str(image),
            "-map",
            "0:a",
            "-map",
            "1:v",
            "-t",
            "1",
            "-c:a",
            "flac",
            "-c:v",
            "png",
            "-disposition:v",
            "attached_pic",
            str(audio),
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert encoded.returncode == 0, encoded.stderr
    original = audio.read_bytes()
    cover = await generate(audio, audio.name)
    assert cover and cover.startswith(b"\xff\xd8\xff")
    assert audio.read_bytes() == original
