"""Optional local thumbnail renderer. Original bytes are never rewritten."""

from __future__ import annotations

# Native tools run with fixed argument lists and no shell; see render().
import math
import os
import secrets
import struct
import subprocess  # nosec B404
import sys
from pathlib import Path

IMAGE_CODECS = {
    ".jpg": "mjpeg",
    ".jpeg": "mjpeg",
    ".png": "png",
    ".webp": "webp",
    ".gif": "gif",
    ".bmp": "bmp",
    ".tif": "tiff",
    ".tiff": "tiff",
}
VIDEO_FORMATS = {
    ".mp4": "mov",
    ".m4v": "mov",
    ".mov": "mov",
    ".avif": "mov",
    ".mkv": "matroska",
    ".webm": "matroska",
    ".avi": "avi",
    ".wmv": "asf",
    ".asf": "asf",
    ".mpeg": "mpeg",
    ".mpg": "mpeg",
}
AUDIO_FORMATS = {
    ".mp3": "mp3",
    ".flac": "flac",
    ".m4a": "mov",
    ".ogg": "ogg",
    ".opus": "ogg",
    ".wav": "wav",
    ".aac": "aac",
}
OFFICE_FORMATS = {".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods"}
MAX_BYTES = 64 * 1024
MAX_POSTER_BYTES = 1024 * 1024
VIDEO_IMAGES_MAGIC = b"TGDRVIMG"
VIDEO_IMAGES_HEADER = struct.Struct("!8sII")
# Shared libraries, HEVC reference frames and thread stacks all count toward
# virtual address space. 512 MiB rejects ordinary UHD HEVC before scaling it.
ADDRESS_SPACE_BYTES = 768 * 1024**2


def archive_cover(path: Path, suffix: str) -> tuple[bytes, str] | None:
    from tgdrive.media.book_worker import RemoteFile

    with path.open("rb") as source:

        def fetch(start, length):
            source.seek(start)
            return source.read(length)

        return archive_cover_from_source(RemoteFile(path.stat().st_size, fetch), suffix)


def archive_cover_from_source(source, suffix: str) -> tuple[bytes, str] | None:
    from tgdrive.media.book_worker import (
        IMAGES,
        MAX_ENTRY,
        image_type,
        local_ref,
        natural,
        open_zip,
        xml,
        zip_bytes,
    )

    if suffix == ".cbr":
        import base64

        from tgdrive.media.book_worker import read_book

        index = read_book(source, "cbr")
        result = read_book(source, "cbr", index["entries"][0]["name"])
        data = base64.b64decode(result["data"], validate=True)
        kind = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/avif": ".avif",
        }.get(result["mime"])
        return (data, kind) if kind else None
    with open_zip(source) as archive:
        candidates = []
        if suffix in OFFICE_FORMATS:
            candidates = [
                name
                for name in (
                    "docProps/thumbnail.jpeg",
                    "docProps/thumbnail.jpg",
                    "docProps/thumbnail.png",
                    "Thumbnails/thumbnail.png",
                )
                if name in archive.namelist()
            ]
            if not candidates:
                return None
        elif suffix == ".epub":
            if "META-INF/encryption.xml" in archive.namelist():
                return None
            container = xml(zip_bytes(archive, "META-INF/container.xml"))
            package_name = next(
                node.get("full-path")
                for node in container.iter()
                if node.tag.rsplit("}", 1)[-1] == "rootfile"
            )
            package = xml(zip_bytes(archive, package_name))
            cover_ids = {
                node.get("content")
                for node in package.iter()
                if node.tag.rsplit("}", 1)[-1] == "meta" and node.get("name") == "cover"
            }
            for node in package.iter():
                if node.tag.rsplit("}", 1)[-1] != "item":
                    continue
                name = local_ref(package_name, node.get("href", ""))
                if name and Path(name).suffix.lower() in IMAGES:
                    if (
                        "cover-image" in node.get("properties", "").split()
                        or node.get("id") in cover_ids
                    ):
                        candidates.insert(0, name)
                    else:
                        candidates.append(name)
        else:
            candidates = sorted(
                (
                    name
                    for name in archive.namelist()
                    if Path(name).suffix.lower() in IMAGES
                    and not name.startswith("__MACOSX/")
                ),
                key=natural,
            )
        if not candidates:
            candidates = sorted(
                (
                    name
                    for name in archive.namelist()
                    if Path(name).suffix.lower() in IMAGES
                    and not name.startswith("__MACOSX/")
                ),
                key=natural,
            )
        if not candidates:
            return None
        data = zip_bytes(archive, candidates[0], maximum=MAX_ENTRY)
        mime = image_type(data)
        kind = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/avif": ".avif",
        }.get(mime)
        return (data, kind) if kind is not None else None


def video_seek_time(path: Path | str, suffix: str, *, remote=False) -> str:
    """Pick between 40% and 60%; unavailable duration keeps a bounded fallback."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-max_alloc",
        "67108864",
        "-protocol_whitelist",
        "http,tcp" if remote else "file,pipe",
        "-f",
        VIDEO_FORMATS[suffix],
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        "-i",
        str(path),
    ]
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10 if remote else 1.5,
            check=False,
        )
        duration = float(result.stdout) if result.returncode == 0 else 0
        if math.isfinite(duration) and duration > 0:
            return f"{duration * (40 + secrets.randbelow(21)) / 100:.3f}"
    except OSError, ValueError, subprocess.TimeoutExpired:
        pass
    return "10"


def render(
    path: Path,
    suffix: str,
    *,
    remote_url=None,
    source_size=None,
    poster=False,
    image_data=None,
) -> bytes:
    source_name = remote_url or str(path)
    cover_input = image_data
    if suffix in {".epub", ".cbz", ".cbr"} | OFFICE_FORMATS:
        try:
            cover = archive_cover(path, suffix)
        except Exception:
            return b""
        if cover is None:
            return b""
        cover_input, suffix = cover
    # Force known demuxers/codecs. A disguised HLS/concat playlist must not turn
    # an uploaded file into arbitrary file or network reads.
    if suffix == ".pdf":
        command = [
            "pdftoppm",
            "-f",
            "1",
            "-l",
            "1",
            "-singlefile",
            "-scale-to",
            "320",
            "-jpeg",
            "-jpegopt",
            "quality=70",
            "-hide-annotations",
            str(path),
        ]
    elif suffix in IMAGE_CODECS or suffix in VIDEO_FORMATS or suffix in AUDIO_FORMATS:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-max_alloc",
            "67108864",
            "-protocol_whitelist",
            "http,tcp" if remote_url else "file,pipe",
        ]
        if suffix in IMAGE_CODECS:
            command += ["-f", "image2pipe" if cover_input is not None else "image2"]
            if cover_input is None:
                command += ["-pattern_type", "none"]
            command += ["-c:v", IMAGE_CODECS[suffix]]
        else:
            command += ["-f", (VIDEO_FORMATS | AUDIO_FORMATS)[suffix]]
            if suffix in VIDEO_FORMATS and suffix != ".avif":
                command += [
                    "-ss",
                    video_seek_time(
                        source_name if remote_url else path,
                        suffix,
                        **({"remote": True} if remote_url else {}),
                    ),
                ]
        if (
            suffix in {".jpg", ".jpeg"}
            and (
                len(cover_input)
                if cover_input is not None
                else source_size
                if remote_url
                else path.stat().st_size
            )
            > 8 * 1024**2
        ):
            # Decode large JPEGs at one-eighth resolution before scaling; a
            # full-resolution RGB allocation can exceed the worker's budget.
            command += ["-lowres", "3"]
        command += [
            "-i",
            "pipe:0" if cover_input is not None else source_name,
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            (
                "scale=w='min(iw,1920)':h='min(ih,1080)':"
                "force_original_aspect_ratio=decrease"
                if poster
                else "scale=320:320:force_original_aspect_ratio=decrease"
            ),
            "-threads",
            "1",
            "-q:v",
            "3" if poster else "6",
            "-f",
            "image2pipe",
            "-c:v",
            "mjpeg",
            "pipe:1",
        ]
    else:
        return b""
    # A costly or damaged seek can time out or emit an incomplete frame even
    # when decoding the first frame is cheap. Keep that fallback inside the
    # parent's existing 12-second deadline instead of abandoning the cover.
    timeouts = (70, 20) if remote_url else (6, 3)
    for attempt, timeout in enumerate(
        timeouts if "-ss" in command else ((90,) if remote_url else (8,))
    ):
        if attempt:
            command[command.index("-ss") + 1] = "0"
        try:
            # Fixed tool/flags and a resolved staged path; no shell interprets input.
            result = subprocess.run(  # noqa: S603  # nosec B603
                command,
                input=cover_input,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return b""
        data = result.stdout
        if (
            result.returncode == 0
            and 0 < len(data) <= (MAX_POSTER_BYTES if poster else MAX_BYTES)
            and data.startswith(b"\xff\xd8\xff")
            and data.endswith(b"\xff\xd9")
        ):
            return data
    return b""


def render_video_images(
    path: Path, suffix: str, *, remote_url=None, source_size=None
) -> tuple[bytes, bytes]:
    if suffix not in VIDEO_FORMATS or suffix == ".avif":
        return render(path, suffix, remote_url=remote_url, source_size=source_size), b""
    poster = render(
        path, suffix, remote_url=remote_url, source_size=source_size, poster=True
    )
    if not poster:
        return render(path, suffix, remote_url=remote_url, source_size=source_size), b""
    # Reuse this exact picture without reading or decoding the movie a second time.
    thumbnail = render(Path("poster.jpg"), ".jpg", image_data=poster)
    return thumbnail, poster


def main():
    try:
        import resource

        resource.setrlimit(
            resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES)
        )
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except ImportError:
        pass
    # Native tools inherit only a minimal environment, not Telegram credentials.
    os.environ.clear()
    os.environ.update(PATH=os.defpath, LANG="C.UTF-8")
    mode = sys.argv[3] if len(sys.argv) > 3 else ""
    images = len(sys.argv) > 3 and sys.argv[-1] == "images"
    renderer = render_video_images if images else render
    if mode in {"remote", "background"} and hasattr(os, "nice"):
        os.nice(10)
    if mode == "remote":
        from urllib.parse import urlsplit

        url = urlsplit(sys.argv[1])
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or not url.port
            or url.username
            or url.password
        ):
            return
        result = renderer(
            Path("remote"),
            sys.argv[2],
            remote_url=sys.argv[1],
            source_size=int(sys.argv[4]),
        )
    else:
        result = renderer(Path(sys.argv[1]), sys.argv[2])
    if images:
        thumbnail, poster = result
        sys.stdout.buffer.write(
            VIDEO_IMAGES_HEADER.pack(VIDEO_IMAGES_MAGIC, len(thumbnail), len(poster))
            + thumbnail
            + poster
        )
    else:
        sys.stdout.buffer.write(result)


if __name__ == "__main__":
    main()
