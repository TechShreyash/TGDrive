import asyncio
import time
from contextlib import suppress

import pytest
from starlette.responses import Response

from tgdrive.media.streaming import _LeasedStreamingResponse
from tgdrive.transfers.bandwidth import FileBandwidth
from tgdrive.web.file_bandwidth import FileBandwidthMiddleware, PacedReceive


def scope(path="/file", *, method="GET", spec="2.4"):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "asgi": {"version": "3.0", "spec_version": spec},
        "headers": [],
        "query_string": b"",
        "extensions": {"http.response.pathsend": {}},
    }


@pytest.mark.asyncio
async def test_receive_slices_preserve_multipart_bytes_flags_and_empty_final_body():
    limits = FileBandwidth()
    await limits.configure(None, 1024)
    payload = b"--boundary\r\nname=file\r\n\r\ncontent\r\n--boundary--\r\n" * 3
    upstream = asyncio.Queue()
    await upstream.put({"type": "http.request", "body": payload, "more_body": True})
    await upstream.put({"type": "http.request", "body": b"", "more_body": False})
    reader = PacedReceive(upstream.get, limits.download)
    parts = []
    while True:
        message = await reader()
        parts.append(message["body"])
        assert len(message["body"]) <= 102
        if not message["more_body"]:
            break
    assert b"".join(parts) == payload
    assert parts[-1] == b""
    assert limits.download.admitted_bytes == len(payload)


@pytest.mark.asyncio
async def test_cancelled_body_admission_does_not_deliver_partial_message():
    limits = FileBandwidth()
    await limits.configure(None, 1024)
    messages = asyncio.Queue()
    await messages.put({"type": "http.request", "body": b"x" * 1000, "more_body": True})
    reader = PacedReceive(messages.get, limits.download)
    task = asyncio.create_task(reader())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.offset == 0
    assert limits.download.admitted_bytes == 0
    assert limits.download.snapshot()["waiting"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 206])
async def test_cached_file_and_range_delivery_obey_finite_aggregate_cap(status):
    limits = FileBandwidth()
    await limits.configure(1024, None)
    payload = b"x" * 300
    response = Response(payload, status_code=status)
    received = asyncio.Queue()
    await received.put({"type": "http.request", "body": b"", "more_body": False})
    sent = []

    async def send(message):
        sent.append(message)

    started = time.monotonic()
    await asyncio.wait_for(
        FileBandwidthMiddleware(response, bandwidth=limits)(
            scope(), received.get, send
        ),
        5,
    )
    elapsed = time.monotonic() - started
    bodies = [
        message["body"] for message in sent if message["type"] == "http.response.body"
    ]
    assert b"".join(bodies) == payload
    assert max(map(len, bodies)) <= 102
    assert elapsed >= len(payload) / 1024 * 0.8
    assert limits.upload.admitted_bytes == len(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", ["2.0", "2.4"])
async def test_disconnect_releases_same_open_file_and_body_iterator(tmp_path, spec):
    path = tmp_path / "private.tgdb"
    path.write_bytes(b"x" * 1024)
    path.chmod(0o600)
    content = path.open("rb")
    limits = FileBandwidth()
    await limits.configure(1024, None)
    upstream = asyncio.Queue()
    await upstream.put({"type": "http.request", "body": b"", "more_body": False})
    closed = asyncio.Event()

    async def body():
        try:
            yield content.read()
        finally:
            closed.set()

    async def release():
        content.close()

    async def send(message):
        if message["type"] == "http.response.body":
            await upstream.put({"type": "http.disconnect"})

    response = _LeasedStreamingResponse(body(), release=release)
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(
            FileBandwidthMiddleware(response, bandwidth=limits)(
                scope("/api/v1/replica/file", spec=spec), upstream.get, send
            ),
            3,
        )
    assert closed.is_set()
    assert content.closed
    assert limits.upload.snapshot()["waiting"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,method,status",
    [
        ("/file", "HEAD", 200),
        ("/file", "GET", 304),
        ("/file", "GET", 401),
        ("/api/v1/performance", "GET", 200),
        ("/static/test.js", "GET", 200),
    ],
)
async def test_control_errors_static_head_and_not_modified_are_not_charged(
    path, method, status
):
    limits = FileBandwidth()
    await limits.configure(1024, None)
    response = Response(b"control", status_code=status)
    upstream = asyncio.Queue()
    sent = []

    async def send(message):
        sent.append(message)

    await FileBandwidthMiddleware(response, bandwidth=limits)(
        scope(path, method=method), upstream.get, send
    )
    assert limits.upload.admitted_bytes == 0
