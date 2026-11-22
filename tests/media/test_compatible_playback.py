"""Compatible playback stays selective, authorized and owned through cancellation."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from fastapi import HTTPException

from tests.support import app_fixtures as fixtures
from tgdrive.media import compatible_playback as playback
from tgdrive.media.compatibility_worker import command, duration

app_client = fixtures.app_client


def test_native_commands_disable_external_references_and_bound_output_window():
    source = "http://127.0.0.1:12345/" + "a" * 32
    args = command(source, ".mov", 7)
    assert args[args.index("-ss") + 1] == "42"
    assert args[args.index("-t") + 1] == "6"
    assert args[args.index("-output_ts_offset") + 1] == "42"
    for option in ("-enable_drefs", "-use_absolute_path"):
        assert args[args.index(option) + 1] == "0"
    assert args[args.index("-protocol_whitelist") + 1] == "http,tcp"
    assert args[args.index("-f") + 1] == "mov"
    assert args[-2:] == ["mpegts", "pipe:1"]
    assert "-max_pixels" in args


@pytest.mark.parametrize(
    "source",
    [
        "http://example.invalid/" + "a" * 32,
        "http://localhost:12345/" + "a" * 32,
        "file:///etc/passwd",
        "http://user@127.0.0.1:12345/" + "a" * 32,
        "http://127.0.0.1:12345/" + "a" * 32 + "?next=http://example.invalid",
    ],
)
def test_worker_rejects_non_private_source_reference(source):
    with pytest.raises(ValueError):
        command(source, ".mov", None)


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "86401"])
def test_probe_requires_finite_bounded_duration(value):
    with pytest.raises(ValueError):
        duration(
            {"format": {"duration": value}, "streams": [{"width": 320, "height": 180}]}
        )


@pytest.mark.asyncio
async def test_private_source_exact_token_range_lru_and_cached_reauthorization():
    reads = []
    allowed = True

    async def authorize():
        if not allowed:
            raise HTTPException(403, "Revoked")

    async def fetch(start, count):
        reads.append((start, count))
        return bytes([start // playback.BLOCK_BYTES]) * count

    source = playback.PlaybackSource(
        {"size": 6 * playback.BLOCK_BYTES}, fetch, authorize
    )
    async with source.open() as url, aiohttp.ClientSession(trust_env=False) as client:
        async with client.get(url + "?wrong=1") as response:
            assert response.status == 404
        assert not reads
        async with client.get(url, headers={"Range": "bytes=9-12"}) as response:
            assert response.status == 206
            assert (
                response.headers["Content-Range"] == f"bytes 9-12/{source.data['size']}"
            )
            assert await response.read() == b"\0" * 4
        async with client.head(url) as response:
            assert response.status == 200
        assert reads == [(0, playback.BLOCK_BYTES)]
        for index in range(1, 6):
            await source.block(index * playback.BLOCK_BYTES)
        assert len(source.cache) == 4
        allowed = False
        with pytest.raises(HTTPException, match="Revoked"):
            await source.block(5 * playback.BLOCK_BYTES)
    assert not source.cache
    assert not source.tasks


@pytest.mark.asyncio
async def test_source_budget_counts_whole_fetched_blocks(monkeypatch):
    monkeypatch.setattr(playback, "MAX_SOURCE_BYTES", 2 * playback.BLOCK_BYTES)
    calls = []

    async def authorize():
        pass

    async def fetch(start, count):
        calls.append(start)
        return bytes(count)

    source = playback.PlaybackSource(
        {"size": 9 * playback.BLOCK_BYTES}, fetch, authorize
    )
    await source.block(0)
    await source.block(playback.BLOCK_BYTES)
    with pytest.raises(HTTPException) as error:
        await source.block(8 * playback.BLOCK_BYTES)
    assert error.value.status_code == 413
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [None, playback.MIN_AVAILABLE_BYTES - 1])
async def test_memory_admission_preserves_worker_and_server_headroom(
    monkeypatch, available
):
    monkeypatch.setattr("tgdrive.core.governor.memory", lambda: (1024**3, available))
    jobs = playback.PlaybackJobs()
    with pytest.raises(HTTPException) as error:
        await jobs.native(None, ".mov", 0)
    assert error.value.status_code == 503
    assert jobs.pending == 0
    assert jobs.slot._value == 1


@pytest.mark.asyncio
async def test_bounded_pending_queue_does_not_fetch_or_spawn():
    jobs = playback.PlaybackJobs()
    jobs.pending = 5
    with pytest.raises(HTTPException) as error:
        await jobs.native(None, ".mov", None)
    assert error.value.status_code == 503
    assert jobs.pending == 5


def setup_api(app_client, monkeypatch):
    client, store, _, _ = app_client
    fixtures.login(client)
    item = store.add_file("/", "sample.mov", 101, 1024)
    calls = []

    async def native(self, source, suffix, index):
        await source.authorize()
        calls.append(index)
        if index is None:
            return json.dumps(
                {
                    "format": {"duration": "14"},
                    "streams": [{"width": 320, "height": 180}],
                }
            ).encode()
        return b"\x47" + b"\0" * 187

    monkeypatch.setattr(playback.PlaybackJobs, "native", native)
    return client, store, "/" + item["id"], calls


def segment_url(response, index=0):
    return [line for line in response.text.splitlines() if line.startswith("/api/")][
        index
    ]


def test_vod_manifest_signed_duration_and_invalid_indices_do_no_native_work(
    app_client, monkeypatch
):
    client, _, path, calls = setup_api(app_client, monkeypatch)
    response = client.get("/api/v1/playback/compatibility.m3u8", params={"path": path})
    assert response.status_code == 200, response.text
    assert "#EXT-X-ENDLIST" in response.text
    assert response.text.count("#EXTINF:") == 3
    assert "#EXTINF:2.000000" in response.text
    assert response.headers["cache-control"] == "private, no-store"
    query = {
        key: values[0]
        for key, values in parse_qs(urlsplit(segment_url(response)).query).items()
    }
    assert client.get(segment_url(response, 1)).status_code == 200
    expected = list(calls)
    for change, status in [
        ({"index": 3}, 416),
        ({"index": -1}, 422),
        ({"seconds": 20}, 403),
        ({"signature": "0" * 64}, 403),
    ]:
        refused = client.get("/api/v1/playback/segment.ts", params={**query, **change})
        assert refused.status_code == status, refused.text
        assert calls == expected
    assert calls == [None, 1]
    query["index"] = "3"
    assert client.get("/api/v1/playback/segment.ts", params=query).status_code == 416
    query["index"] = "0"
    query["seconds"] = "1000"
    assert client.get("/api/v1/playback/segment.ts", params=query).status_code == 403
    assert calls == [None, 1]


def test_manifest_and_segments_preserve_share_scope_revocation_and_owner_logout(
    app_client, monkeypatch
):
    client, store, path, calls = setup_api(app_client, monkeypatch)
    private = store.add_file("/", "private.mov", 102, 1024)
    token = store.create_share(path)
    manifest = client.get(
        "/api/v1/playback/compatibility.m3u8", params={"path": path, "auth": token}
    )
    assert manifest.status_code == 200
    url = segment_url(manifest)
    assert parse_qs(urlsplit(url).query)["auth"] == [token]
    assert (
        client.get(
            "/api/v1/playback/compatibility.m3u8",
            params={"path": "/" + private["id"], "auth": token},
        ).status_code
        == 403
    )
    store.revoke_share(path)
    assert client.get(url).status_code == 403
    client.post("/api/logout")
    assert (
        client.get(
            "/api/v1/playback/compatibility.m3u8", params={"path": path}
        ).status_code
        == 403
    )
    assert calls == [None]


def test_changed_file_cannot_reuse_manifest_ticket(app_client, monkeypatch):
    client, store, path, calls = setup_api(app_client, monkeypatch)
    manifest = client.get("/api/v1/playback/compatibility.m3u8", params={"path": path})
    original = store.get_file

    def changed(*args, **kwargs):
        return {**original(*args, **kwargs), "size": 1025}

    monkeypatch.setattr(store, "get_file", changed)
    assert client.get(segment_url(manifest)).status_code == 409
    assert calls == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["disconnect", "revocation"])
async def test_request_stop_kills_and_joins_owned_native_process(monkeypatch, cancel):
    monkeypatch.setattr("tgdrive.core.governor.memory", lambda: (2 * 1024**3, 1024**3))
    spawn = asyncio.create_subprocess_exec
    opened = asyncio.Event()
    processes = []

    async def slow(*args, **kwargs):
        process = await spawn(
            sys.executable, "-c", "import time;time.sleep(60)", **kwargs
        )
        processes.append(process)
        opened.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow)
    allowed = True

    async def authorize():
        if not allowed and cancel == "revocation":
            raise HTTPException(403, "Revoked")

    async def disconnected():
        return not allowed and cancel == "disconnect"

    async def fetch(start, count):
        return bytes(count)

    source = playback.PlaybackSource({"size": 1024}, fetch, authorize)
    jobs = playback.PlaybackJobs()
    task = asyncio.create_task(
        playback.watch_request(
            SimpleNamespace(is_disconnected=disconnected),
            authorize,
            jobs.native(source, ".mov", 0),
        )
    )
    await asyncio.wait_for(opened.wait(), 3)
    allowed = False
    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(task, 5)
    assert error.value.status_code == (499 if cancel == "disconnect" else 403)
    assert processes[0].returncode is not None
    assert not source.tasks
    assert jobs.pending == 0
    assert jobs.slot._value == 1


@pytest.fixture(
    params=[("prores_ks", "pcm_s16le", ".mov"), ("msmpeg4v3", "wmav2", ".wmv")]
)
def native_sample(tmp_path, request):
    video, audio, suffix = request.param
    path = tmp_path / ("synthetic" + suffix)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required for native codec coverage")
    subprocess.run(  # noqa: S603 - fixed executable and generated inputs
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "14",
            "-c:v",
            video,
            "-threads",
            "1",
            "-c:a",
            audio,
            str(path),
        ],
        check=True,
        timeout=30,
    )
    return path.read_bytes(), suffix


@pytest.mark.asyncio
async def test_real_native_unsupported_samples_probe_seek_and_short_encode(
    native_sample, monkeypatch
):
    content, suffix = native_sample
    monkeypatch.setattr("tgdrive.core.governor.memory", lambda: (2 * 1024**3, 1024**3))
    reads = []

    async def authorize():
        pass

    async def fetch(start, count):
        reads.append((start, count))
        return content[start : start + count]

    jobs = playback.PlaybackJobs()

    def source():
        return playback.PlaybackSource({"size": len(content)}, fetch, authorize)

    metadata = await jobs.native(source(), suffix, None)
    assert 14 <= duration(json.loads(metadata)) < 15
    output = await jobs.native(source(), suffix, 1)
    assert len(output) <= playback.MAX_SEGMENT_BYTES
    assert len(output) % 188 == 0
    assert output[::188] == b"\x47" * (len(output) // 188)
    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None
    inspected = await asyncio.to_thread(
        subprocess.run,
        [
            ffprobe,
            "-v",
            "error",
            "-f",
            "mpegts",
            "-i",
            "pipe:0",
            "-show_entries",
            "stream=codec_name,start_time,width,height",
            "-of",
            "json",
        ],
        input=output,
        capture_output=True,
        check=True,
        timeout=10,
    )
    streams = json.loads(inspected.stdout)["streams"]
    assert {stream["codec_name"] for stream in streams} == {"h264", "aac"}
    video = next(stream for stream in streams if stream["codec_name"] == "h264")
    assert video["width"] <= 1280 and video["height"] <= 720
    assert 5.9 <= float(video["start_time"]) <= 6.1
    assert sum(count for _, count in reads) < 2 * playback.MAX_SOURCE_BYTES
