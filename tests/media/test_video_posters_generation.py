from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tgdrive.media import generated_thumbnails as covers
from tgdrive.media import thumbnail_worker as worker
from tgdrive.media.book_worker import image_dimensions

THUMBNAIL = b"\xff\xd8\xffsmall\xff\xd9"
POSTER = b"\xff\xd8\xfflarge\xff\xd9"


def pair(small=THUMBNAIL, poster=POSTER, *, magic=worker.VIDEO_IMAGES_MAGIC):
    return (
        worker.VIDEO_IMAGES_HEADER.pack(magic, len(small), len(poster)) + small + poster
    )


@pytest.mark.parametrize(
    "width,height,expected",
    [(320, 180, (320, 180)), (2560, 1440, (1920, 1080)), (1200, 1600, (810, 1080))],
)
async def test_native_video_images_are_bounded_and_do_not_upscale(
    tmp_path, width, height, expected
):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("native video tools unavailable")
    path = tmp_path / "clip.mp4"
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
            f"color=c=blue:s={width}x{height}:r=1",
            "-frames:v",
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "1",
            str(path),
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    if b"Unknown encoder" in encoded.stderr:
        pytest.skip("native H264 encoder unavailable")
    assert encoded.returncode == 0, encoded.stderr.decode(errors="replace")
    original = path.read_bytes()
    thumbnail, poster = await covers.generate_video_images(path, path.name)
    assert thumbnail and poster
    assert len(thumbnail) <= covers.MAX_BYTES
    assert len(poster) <= covers.MAX_POSTER_BYTES
    assert image_dimensions(poster, "image/jpeg") == [expected]
    assert max(image_dimensions(thumbnail, "image/jpeg")[0]) <= 320
    assert path.read_bytes() == original


def test_thumbnail_uses_the_same_decoded_picture_without_reading_video_again(
    monkeypatch,
):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=POSTER if len(calls) == 1 else THUMBNAIL
        )

    monkeypatch.setattr(worker, "video_seek_time", lambda *_: "600")
    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.render_video_images(Path("clip.mp4"), ".mp4") == (
        THUMBNAIL,
        POSTER,
    )
    assert len(calls) == 2
    first, second = [command for command, _ in calls]
    assert first[first.index("-i") + 1] == "clip.mp4"
    assert first[first.index("-ss") + 1] == "600"
    assert "min(iw,1920)" in first[first.index("-vf") + 1]
    assert "min(ih,1080)" in first[first.index("-vf") + 1]
    assert second[second.index("-i") + 1] == "pipe:0"
    assert "-ss" not in second
    assert calls[1][1]["input"] == POSTER
    assert first[first.index("-q:v") + 1] == "3"
    assert second[second.index("-q:v") + 1] == "6"


def test_optional_poster_failure_retains_the_small_cover_path(monkeypatch):
    render = Mock(side_effect=[b"", THUMBNAIL])
    monkeypatch.setattr(worker, "render", render)
    assert worker.render_video_images(Path("clip.mp4"), ".mp4") == (THUMBNAIL, b"")
    assert render.call_args_list[0].kwargs["poster"] is True
    assert "poster" not in render.call_args_list[1].kwargs


@pytest.mark.parametrize(
    "payload,expected",
    [
        (pair(), (THUMBNAIL, POSTER)),
        (pair(poster=b""), (THUMBNAIL, None)),
        (pair(small=b""), (None, POSTER)),
        (pair(small=b"", poster=b""), (None, None)),
        (b"", (None, None)),
        (b"short", (None, None)),
        (pair(magic=b"BADMAGIC"), (None, None)),
        (pair()[:-1], (None, None)),
        (pair() + b"trailing", (None, None)),
        (pair(small=b"not JPEG"), (None, None)),
        (pair(poster=POSTER[:-2]), (None, None)),
        (pair(small=THUMBNAIL + b"x" * covers.MAX_BYTES), (None, None)),
        (pair(poster=POSTER + b"x" * covers.MAX_POSTER_BYTES), (None, None)),
    ],
)
async def test_parent_accepts_only_complete_bounded_image_pairs(
    monkeypatch, payload, expected
):
    generate = AsyncMock(return_value=payload)
    monkeypatch.setattr(covers, "_generate", generate)
    assert await covers.generate_video_images(Path("clip.mp4"), "clip.mp4") == expected
    assert generate.call_args.kwargs["images"] is True


@pytest.mark.parametrize("name", ["cover.png", "cover.avif", "book.epub"])
async def test_nonvideo_callers_keep_one_thumbnail(monkeypatch, name):
    generate = AsyncMock(return_value=THUMBNAIL)
    monkeypatch.setattr(covers, "_generate", generate)
    assert await covers.generate_video_images(Path(name), name) == (THUMBNAIL, None)
    assert generate.call_args.kwargs["images"] is False
    assert await covers.generate(Path(name), name) == THUMBNAIL


async def test_oversized_worker_output_is_killed_and_reaped(monkeypatch, tmp_path):
    process = SimpleNamespace(
        pid=123,
        returncode=None,
        stdout=SimpleNamespace(
            read=AsyncMock(
                return_value=b"x"
                * (
                    worker.VIDEO_IMAGES_HEADER.size
                    + covers.MAX_BYTES
                    + covers.MAX_POSTER_BYTES
                    + 1
                )
            )
        ),
        wait=AsyncMock(return_value=0),
        kill=lambda: None,
    )
    opening = AsyncMock(return_value=process)
    monkeypatch.setattr(covers.asyncio, "create_subprocess_exec", opening)
    monkeypatch.setattr(covers.os, "killpg", lambda *_: None, raising=False)
    assert await covers.generate_video_images(tmp_path / "clip.mp4", "clip.mp4") == (
        None,
        None,
    )
    assert "images" in opening.call_args.args
    process.wait.assert_awaited_once()


def test_poster_cache_size_is_explicit_and_keeps_the_thumbnail_default(tmp_path):
    poster = b"\xff\xd8\xff" + b"x" * covers.MAX_BYTES + b"\xff\xd9"
    posters = tmp_path / "posters"
    thumbnails = tmp_path / "thumbnails"
    covers.save(posters, -100, 1, poster, maximum=covers.MAX_POSTER_BYTES)
    assert covers.read(posters, -100, 1, maximum=covers.MAX_POSTER_BYTES) == poster
    assert covers.read(posters, -100, 1) is None
    covers.save(thumbnails, -100, 1, poster)
    assert covers.read(thumbnails, -100, 1) is None
    covers.save(
        posters,
        -100,
        2,
        b"x" * (covers.MAX_POSTER_BYTES + 1),
        maximum=covers.MAX_POSTER_BYTES,
    )
    assert covers.read(posters, -100, 2, maximum=covers.MAX_POSTER_BYTES) is None
