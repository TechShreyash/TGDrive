"""Fixed-input, resource-limited native decoding for short compatible playback."""

from __future__ import annotations

import math
import os
import re
import sys
from urllib.parse import urlsplit

from tgdrive.media.thumbnail_worker import VIDEO_FORMATS

COMPATIBILITY_FORMATS = {
    suffix: demuxer for suffix, demuxer in VIDEO_FORMATS.items() if suffix != ".avif"
} | {
    ".3gp": "mov",
    ".flv": "flv",
    ".m2ts": "mpegts",
    ".mts": "mpegts",
    ".ts": "mpegts",
    ".ogv": "ogg",
    ".vob": "mpeg",
}
SEGMENT_SECONDS = 6
MAX_DURATION_SECONDS = 24 * 60 * 60
MAX_SEGMENT_BYTES = 8 * 1024**2
ADDRESS_SPACE_BYTES = 512 * 1024**2


def command(url: str, suffix: str, index: int | None) -> list[str]:
    source = urlsplit(url)
    if (
        source.scheme != "http"
        or source.hostname != "127.0.0.1"
        or source.username is not None
        or source.password is not None
        or source.port is None
        or not re.fullmatch(r"/[a-f0-9]{32}", source.path)
        or source.query
        or source.fragment
        or suffix not in COMPATIBILITY_FORMATS
    ):
        raise ValueError("Invalid private playback source")
    native = "ffprobe" if index is None else "ffmpeg"
    args = [native, "-cpucount", "1", "-v", "error", "-max_alloc", "67108864"]
    if index is not None:
        if type(index) is not int or not 0 <= index < (
            MAX_DURATION_SECONDS // SEGMENT_SECONDS
        ):
            raise ValueError("Invalid playback segment")
        args += ["-nostdin", "-filter_threads", "1"]
    args += [
        "-threads",
        "1",
        "-max_pixels",
        "8294400",
        "-protocol_whitelist",
        "http,tcp",
        "-rw_timeout",
        "15000000",
        "-probesize",
        "4194304",
        "-analyzeduration",
        "5000000",
        "-f",
        COMPATIBILITY_FORMATS[suffix],
    ]
    if COMPATIBILITY_FORMATS[suffix] == "mov":
        args += ["-enable_drefs", "0", "-use_absolute_path", "0"]
    if index is None:
        return [
            *args,
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            "-i",
            url,
        ]
    start = index * SEGMENT_SECONDS
    return [
        *args,
        "-ss",
        str(start),
        "-i",
        url,
        "-t",
        str(SEGMENT_SECONDS),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-vf",
        "scale=w='min(1280,iw)':h='min(720,ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "1",
        "-b:v",
        "1200k",
        "-maxrate",
        "1500k",
        "-bufsize",
        "3000k",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-b:a",
        "128k",
        "-output_ts_offset",
        str(start),
        "-muxdelay",
        "0",
        "-f",
        "mpegts",
        "pipe:1",
    ]


def duration(metadata: dict) -> float:
    value = float(metadata.get("format", {}).get("duration", 0))
    streams = metadata.get("streams", [])
    if (
        not math.isfinite(value)
        or not 0 < value <= MAX_DURATION_SECONDS
        or len(streams) != 1
        or not 0 < int(streams[0].get("width", 0)) <= 3840
        or not 0 < int(streams[0].get("height", 0)) <= 3840
        or int(streams[0]["width"]) * int(streams[0]["height"]) > 3840 * 2160
    ):
        raise ValueError("Unsupported playback dimensions or duration")
    return value


def main():
    import resource

    args = command(
        sys.argv[1], sys.argv[2], None if sys.argv[3] == "probe" else int(sys.argv[3])
    )
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
    # Bound native thread stacks without raising the total address-space ceiling.
    resource.setrlimit(resource.RLIMIT_STACK, (2 * 1024**2, 2 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    # Limit allocator reservations so FFmpeg can start threads within RLIMIT_AS.
    os.execvpe(  # noqa: S606  # nosec B606
        args[0],
        args,
        {"PATH": os.defpath, "LANG": "C.UTF-8", "MALLOC_ARENA_MAX": "2"},
    )


if __name__ == "__main__":
    main()
