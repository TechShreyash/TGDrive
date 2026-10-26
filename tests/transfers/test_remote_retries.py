"""Offline recovery for incomplete HTTP bodies, backoff and admission fairness."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import aiohttp
import pytest

from tests.transfers.test_remote import (
    PUBLIC_DNS,
    FakeResolver,
    FakeResponse,
    FakeSession,
)
from tests.transfers.test_remote_ranges import RangeSource, client_for
from tgdrive.transfers.remote import (
    RemoteCancelled,
    RemoteClient,
    RemoteHTTPError,
    RemoteRetriesExhausted,
    RemoteTimeout,
    UnsafeRemoteURL,
    source_retryable,
)


def client_for_responses(responses, monkeypatch, **kwargs):
    session = FakeSession(responses)
    client = RemoteClient(session=session, resolver=FakeResolver(PUBLIC_DNS), **kwargs)
    wait = AsyncMock()
    monkeypatch.setattr(client, "_wait_for_retry", wait)
    return client, session, wait


def broken_body():
    response = FakeResponse(200, headers={"Content-Length": "6"}, chunks=[b"old"])
    original = response.content.read
    calls = 0

    async def read(size):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise aiohttp.ClientPayloadError("body interrupted with a private URL")
        return await original(size)

    response.content.read = read
    return response


async def test_payload_failure_restarts_cleanly_and_publishes_only_complete_bytes(
    tmp_path, monkeypatch
):
    first = broken_body()
    second = FakeResponse(200, headers={"Content-Length": "6"}, chunks=[b"newnew"])
    client, session, wait = client_for_responses([first, second], monkeypatch)
    scheduled = []

    async def on_retry(attempt, delay, error):
        assert not list(tmp_path.iterdir())
        assert client.scheduler._tickets == []
        assert first.released
        scheduled.append((attempt, delay, type(error).__name__))

    result = await client.download(
        "https://files.example/book.epub", tmp_path, retry=on_retry
    )
    assert result.path.read_bytes() == b"newnew"
    assert scheduled == [(2, 2, "ClientPayloadError")]
    assert len(session.calls) == 2
    wait.assert_awaited_once()
    assert not list(tmp_path.glob("*.part"))


async def test_short_declared_body_also_recovers_without_a_payload_exception(
    tmp_path, monkeypatch
):
    client, _, _ = client_for_responses(
        [
            FakeResponse(200, headers={"Content-Length": "6"}, chunks=[b"old"]),
            FakeResponse(200, headers={"Content-Length": "6"}, chunks=[b"abcdef"]),
        ],
        monkeypatch,
    )
    result = await client.download("https://files.example/a", tmp_path)
    assert result.path.read_bytes() == b"abcdef"


async def test_retry_budget_exhausts_after_four_attempts_with_bounded_backoff(
    tmp_path, monkeypatch
):
    client, session, wait = client_for_responses(
        [broken_body() for _ in range(4)], monkeypatch
    )
    with pytest.raises(RemoteRetriesExhausted, match="after 4 attempts") as error:
        await client.download("https://files.example/a", tmp_path)
    assert isinstance(error.value.__cause__, aiohttp.ClientPayloadError)
    assert len(session.calls) == 4
    assert [call.args[0] for call in wait.await_args_list] == [2, 4, 8]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_temporary_http_errors_retry_and_respect_retry_after(
    tmp_path, monkeypatch, status
):
    client, session, wait = client_for_responses(
        [
            FakeResponse(status, headers={"Retry-After": "120"}),
            FakeResponse(200, headers={"Content-Length": "1"}, chunks=[b"x"]),
        ],
        monkeypatch,
    )
    result = await client.download("https://files.example/a", tmp_path)
    assert result.path.read_bytes() == b"x"
    assert len(session.calls) == 2
    assert wait.await_args.args[0] == 120


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 501])
async def test_permanent_http_errors_do_not_retry(tmp_path, monkeypatch, status):
    client, session, wait = client_for_responses([FakeResponse(status)], monkeypatch)
    with pytest.raises(RemoteHTTPError):
        await client.download("https://files.example/a", tmp_path)
    assert len(session.calls) == 1
    wait.assert_not_awaited()


async def test_cancellation_during_backoff_stops_without_another_request(tmp_path):
    session = FakeSession([broken_body()])
    client = RemoteClient(session=session, resolver=FakeResolver(PUBLIC_DNS))
    cancel = asyncio.Event()
    scheduled = asyncio.Event()

    def on_retry(*_args):
        scheduled.set()

    task = asyncio.create_task(
        client.download(
            "https://files.example/a", tmp_path, cancel_event=cancel, retry=on_retry
        )
    )
    await asyncio.wait_for(scheduled.wait(), 3)
    cancel.set()
    with pytest.raises(RemoteCancelled):
        await asyncio.wait_for(task, 3)
    assert len(session.calls) == 1
    assert client.scheduler._tickets == []


async def test_total_timeout_includes_backoff_and_does_not_start_a_fresh_budget(
    tmp_path,
):
    from tgdrive.transfers.remote import RemoteTimeouts

    client = RemoteClient(
        session=FakeSession([broken_body()]),
        resolver=FakeResolver(PUBLIC_DNS),
        timeouts=RemoteTimeouts(total=0.1),
    )
    with pytest.raises(RemoteTimeout):
        await client.download("https://files.example/a", tmp_path)
    assert client.scheduler._tickets == []
    assert not list(tmp_path.iterdir())


async def test_initial_parallel_stream_payload_error_uses_a_fresh_single_response(
    tmp_path, monkeypatch
):
    source = RangeSource()
    original = source.request

    async def request(*args, **kwargs):
        response = await original(*args, **kwargs)
        if "Range" not in kwargs["headers"] and source.full_requests == 1:

            async def broken_read(_size):
                raise aiohttp.ClientPayloadError("initial range stream interrupted")

            response.content.read = broken_read
        return response

    source.request = request
    client = client_for(source)
    wait = AsyncMock()
    monkeypatch.setattr(client, "_wait_for_retry", wait)
    result = await client.download("https://files.example/a", tmp_path)
    assert result.path.read_bytes() == source.data
    assert source.full_requests == 2
    assert all(response.released for response in source.responses)
    assert not list(tmp_path.glob("*.part"))


def test_unsafe_destinations_and_tls_failures_are_never_retryable():
    assert not source_retryable(UnsafeRemoteURL("blocked"))
    assert not source_retryable(aiohttp.ClientSSLError(None, OSError("TLS error")))


async def test_pipeline_publishes_retry_status_releases_staging_and_uploads_once(
    tmp_path, monkeypatch
):
    from tests.transfers.test_transfers import make_manager
    from tgdrive.transfers.tasks import TransferStatus

    manager, store, _transport, pool = await make_manager(tmp_path, max_size=100)
    client, session, _ = client_for_responses(
        [
            broken_body(),
            FakeResponse(200, headers={"Content-Length": "6"}, chunks=[b"abcdef"]),
        ],
        monkeypatch,
    )
    manager.remote = client
    observed = []

    async def waiting(delay, cancel_event):
        snapshot = record.as_dict()
        assert snapshot["phase"] == "source_retry_wait"
        assert snapshot["status"] == "running"
        assert snapshot["source_attempt"] == 2
        assert snapshot["source_max_attempts"] == 4
        assert snapshot["retry_at"] is not None
        assert snapshot["eta_seconds"] is None
        assert snapshot["error"] is None
        assert record.context["staging_size"] == record.context["staging_written"] == 0
        assert client.scheduler._tickets == []
        observed.append(delay)

    monkeypatch.setattr(client, "_wait_for_retry", waiting)
    record = await manager.start_remote(
        url="https://files.example/book.epub", destination="/", filename="book.epub"
    )
    try:
        await record.task
        assert record.status == TransferStatus.COMPLETED, record.error
        assert observed == [2]
        assert len(session.calls) == 2
        assert len(store.list_directory("/")) == 1
        assert "source_retry_at" not in record.context
    finally:
        await manager.close()
        await pool.stop()
        store.close()


async def test_explicit_saved_retry_deadline_is_waited_before_contacting_source(
    tmp_path, monkeypatch
):
    import time

    client, session, _wait = client_for_responses(
        [FakeResponse(200, headers={"Content-Length": "1"}, chunks=[b"x"])], monkeypatch
    )

    async def delayed(delay, cancel_event):
        assert 118 <= delay <= 120
        assert session.calls == []

    monkeypatch.setattr(client, "_wait_for_retry", delayed)
    await client.download(
        "https://files.example/a", tmp_path, retry_not_before=time.time() + 120
    )
    assert len(session.calls) == 1
