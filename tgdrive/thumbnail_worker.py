"""Optional local thumbnail renderer. Original bytes are never rewritten."""

from __future__ import annotations

# Native tools run with fixed argument lists and no shell; see render().
import os
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
MAX_BYTES = 64 * 1024
# Shared libraries, HEVC reference frames and thread stacks all count toward
# virtual address space. 512 MiB rejects ordinary UHD HEVC before scaling it.
ADDRESS_SPACE_BYTES = 768 * 1024**2


def render(path: Path, suffix: str) -> bytes:
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
    elif suffix in IMAGE_CODECS or suffix in VIDEO_FORMATS:
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
            "file,pipe",
        ]
        if suffix in IMAGE_CODECS:
            command += [
                "-f",
                "image2",
                "-pattern_type",
                "none",
                "-c:v",
                IMAGE_CODECS[suffix],
            ]
        else:
            command += ["-f", VIDEO_FORMATS[suffix]]
            if suffix != ".avif":
                command += ["-ss", "1"]
        command += [
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            "scale=320:320:force_original_aspect_ratio=decrease",
            "-threads",
            "1",
            "-q:v",
            "6",
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
    for attempt, timeout in enumerate((8, 3) if "-ss" in command else (8,)):
        if attempt:
            command[command.index("-ss") + 1] = "0"
        try:
            # Fixed tool/flags and a resolved staged path; no shell interprets input.
            result = subprocess.run(  # noqa: S603  # nosec B603
                command,
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
            and 0 < len(data) <= MAX_BYTES
            and data.startswith(b"\xff\xd8\xff")
            and data.endswith(b"\xff\xd9")
        ):
            return data
    return b""


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
    sys.stdout.buffer.write(render(Path(sys.argv[1]), sys.argv[2]))


if __name__ == "__main__":
    main()
