"""Optional local thumbnail renderer. Original bytes are never rewritten."""

from __future__ import annotations

# Native tools run with fixed argument lists and no shell; see render().
import contextlib
import math
import os
import secrets
import struct
import subprocess  # nosec B404
import sys
import threading
import time
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
MAX_BYTES = 64 * 1024
MAX_POSTER_BYTES = 1024 * 1024
VIDEO_IMAGES_MAGIC = b"TGDRVIMG"
VIDEO_IMAGES_HEADER = struct.Struct("!8sII")
ADDRESS_SPACE_BYTES = 768 * 1024**2
LOCAL_SECONDS = 60
STALL_SECONDS = 30
# shortcut: fixed hang guard per remote attempt, replace if real renders need longer.
REMOTE_CEILING_SECONDS = 600


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


def read_counter(pid):
    """Bytes ffmpeg has read, including its socket; None off Linux."""
    try:
        with open(f"/proc/{pid}/io", "rb") as io:
            return io.readline()
    except OSError:
        return None


def run_remote(command):
    """Stop ffmpeg only after STALL_SECONDS without output, progress or reads."""
    # Fixed tool/flags and a validated loopback URL; no shell interprets input.
    with subprocess.Popen(  # noqa: S603  # nosec B603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        output = []
        last = [time.monotonic()]

        def pump(stream, keep):
            for chunk in iter(lambda: stream.read1(65536), b""):
                last[0] = time.monotonic()
                if keep:
                    output.append(chunk)

        threads = [
            threading.Thread(target=pump, args=(process.stdout, True), daemon=True),
            threading.Thread(target=pump, args=(process.stderr, False), daemon=True),
        ]
        for thread in threads:
            thread.start()
        started = time.monotonic()
        counter = None
        while True:
            try:
                process.wait(timeout=min(1, STALL_SECONDS))
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if (current := read_counter(process.pid)) != counter:
                    counter, last[0] = current, now
                if (
                    now - last[0] > STALL_SECONDS
                    or now - started > REMOTE_CEILING_SECONDS
                ):
                    process.kill()
                    process.wait()
                    for thread in threads:
                        thread.join()
                    raise subprocess.TimeoutExpired(command, STALL_SECONDS) from None
        for thread in threads:
            thread.join()
        return subprocess.CompletedProcess(
            command, process.returncode, b"".join(output)
        )


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
        if remote_url:
            # Progress lines keep the stall watchdog in run_remote() alive.
            command += ["-progress", "pipe:2", "-nostats"]
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
                else (source_size or 0)
                if remote_url
                else path.stat().st_size
            )
            > 8 * 1024**2
        ):
            # Decode large JPEGs at one-eighth resolution before scaling; a
            # full-resolution RGB allocation can exceed the worker's budget.
            command += ["-lowres", "3"]
        if suffix in VIDEO_FORMATS and suffix != ".avif":
            # A cover needs one picture; keyframes decode without reference chains.
            command += ["-skip_frame", "nokey"]
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
    # when decoding the first frame is cheap; retry from the first frame.
    for attempt in range(2 if "-ss" in command else 1):
        if attempt:
            command[command.index("-ss") + 1] = "0"
        try:
            if remote_url:
                result = run_remote(command)
            else:
                # Fixed tool/flags and a resolved staged path; no shell parses input.
                result = subprocess.run(  # noqa: S603  # nosec B603
                    command,
                    input=cover_input,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=LOCAL_SECONDS,
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

        # Shared libraries and UHD reference frames count toward virtual memory.
        resource.setrlimit(
            resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES)
        )
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except ImportError:
        pass
    with contextlib.suppress(OSError), open("/proc/self/oom_score_adj", "w") as oom:
        oom.write("1000")
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
    if isinstance(result, tuple):
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
