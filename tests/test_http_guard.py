from __future__ import annotations

import asyncio
import json
from tempfile import SpooledTemporaryFile
from typing import Any

import pytest
import starlette.formparsers as formparsers
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from tgdrive.http_guard import StreamConcurrencyMiddleware, UploadGuardMiddleware
from tgdrive.security import SESSION_COOKIE, SessionManager


async def invoke(
    app: Any,
    *,
    headers: list[tuple[bytes, bytes]],
    chunks: list[bytes],
) -> tuple[list[dict[str, Any]], int]:
    sent: list[dict[str, Any]] = []
    reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal reads
        if reads >= len(chunks):
            await asyncio.Event().wait()
        index = reads
        reads += 1
        return {
            "type": "http.request",
            "body": chunks[index],
            "more_body": index < len(chunks) - 1,
        }

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/upload",
            "raw_path": b"/api/upload",
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1234),
            "headers": headers,
            "app": object(),
        },
        receive,
        send,
    )
    return sent, reads


def make_guard(app: Any, limit: int = 16) -> tuple[Any, list[tuple[bytes, bytes]]]:
    sessions = SessionManager(b"s" * 48, 3600)
    guard = UploadGuardMiddleware(
        app, sessions=sessions, upload_limit=limit, max_concurrent=1
    )
    cookie = f"{SESSION_COOKIE}={sessions.create()}".encode()
    return guard, [(b"cookie", cookie)]


@pytest.mark.asyncio
async def test_unauthenticated_body_is_never_read_or_parsed() -> None:
    async def app(*_args: Any) -> None:
        pytest.fail("Unauthenticated requests must not enter multipart parsing")

    guard, _ = make_guard(app)
    messages, reads = await invoke(
        guard,
        headers=[(b"content-type", b"multipart/form-data; boundary=test")],
        chunks=[b"x" * (3 * 1024**2)],
    )
    assert messages[0]["status"] == 401
    assert reads == 0


@pytest.mark.asyncio
async def test_chunked_multipart_limit_closes_previously_spooled_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[Any] = []

    def tracked_spool(*args: Any, **kwargs: Any) -> Any:
        file = SpooledTemporaryFile(*args, **kwargs)  # noqa: SIM115 - parser owns it
        opened.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracked_spool)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        request = Request(scope, receive)
        try:
            async with request.form():
                pytest.fail("Oversized request reached the upload handler")
        except HTTPException as exc:
            await JSONResponse({"detail": str(exc)}, status_code=exc.status_code)(
                scope, receive, send
            )

    guard, headers = make_guard(app)
    headers.append((b"content-type", b"multipart/form-data; boundary=test"))
    start = (
        b'--test\r\nContent-Disposition: form-data; name="file"; '
        b'filename="large.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
    )
    messages, _ = await invoke(
        guard,
        headers=headers,
        chunks=[start + b"x" * 1024, b"x" * (1024**2), b"x" * (1024**2)],
    )
    assert messages[0]["status"] == 413
    assert json.loads(messages[1]["body"])["detail"] == "Upload body is too large"
    assert opened
    assert all(file.closed for file in opened)


@pytest.mark.asyncio
async def test_declared_oversize_body_is_rejected_before_receive() -> None:
    async def app(*_args: Any) -> None:
        pytest.fail("Oversized requests must not reach the handler")

    guard, headers = make_guard(app)
    headers.append((b"content-length", b"99999999"))
    messages, reads = await invoke(guard, headers=headers, chunks=[b"ignored"])
    assert messages[0]["status"] == 413
    assert reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [b"-1", b"not-a-number"])
async def test_invalid_body_framing_is_rejected(length: bytes) -> None:
    async def app(*_args: Any) -> None:
        pytest.fail("Invalid framing must not reach the handler")

    guard, headers = make_guard(app)
    headers.append((b"content-length", length))
    messages, reads = await invoke(guard, headers=headers, chunks=[b"ignored"])
    assert messages[0]["status"] == 400
    assert reads == 0


@pytest.mark.asyncio
async def test_stream_capacity_covers_body_and_releases_after_disconnect() -> None:
    entered = asyncio.Queue()
    finish = asyncio.Event()

    async def stream(scope: Any, receive: Any, send: Any) -> None:
        await entered.put(scope["client"])
        if scope["client"] == "cancelled":
            await asyncio.Event().wait()
        await finish.wait()

    guard = StreamConcurrencyMiddleware(stream, max_concurrent=1)

    async def invoke_stream(label: str, path: str = "/file") -> None:
        await guard(
            {"type": "http", "method": "GET", "path": path, "client": label},
            None,
            None,
        )

    first = asyncio.create_task(invoke_stream("cancelled"))
    assert await asyncio.wait_for(entered.get(), 1) == "cancelled"
    second = asyncio.create_task(invoke_stream("waiting"))
    await asyncio.sleep(0)
    assert entered.empty()
    # The file limit must not block health/auth/metadata requests.
    unrelated = asyncio.create_task(invoke_stream("health", "/healthz"))
    assert await asyncio.wait_for(entered.get(), 1) == "health"
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await asyncio.wait_for(entered.get(), 1) == "waiting"
    finish.set()
    await asyncio.gather(second, unrelated)
