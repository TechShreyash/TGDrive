"""Advisory activity writes never stall delivery or consume every read worker."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import pytest
from pymongo.errors import ConnectionFailure

import tgdrive.web.app as app_module
from tests.media.test_pooled_media import account
from tests.media.test_stream_delivery import request
from tests.media.test_streaming import response_body
from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.app_fixtures import app_client as app_client
from tests.support.mongo_fixtures import new_store
from tgdrive.media.media_reader import BLOCK
from tgdrive.media.streaming import stream_telegram_file
from tgdrive.telegram.client_pool import ClientPool


@pytest.mark.parametrize("first_fails", [False, True])
def test_slow_recent_write_is_advisory_and_failed_write_can_retry(
    app_client, monkeypatch, first_fails
):
    client, store, _, _ = app_client
    item = store.add_file("/", "notes.txt", 77, 3)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    original = store.mark_opened

    def mark_opened(identity):
        calls.append(identity)
        if len(calls) == 1:
            entered.set()
            release.wait(3)
            finished.set()
            if first_fails:
                raise ConnectionFailure("Synthetic activity-write failure")
        original(identity)

    async def describe(**_kwargs):
        return {"kind": "text", "text": "one", "size": 3}

    monkeypatch.setattr(store, "mark_opened", mark_opened)
    monkeypatch.setattr(app_module, "describe_preview", describe)
    login(client)
    params = {"path": "/" + item["id"]}
    with ThreadPoolExecutor(max_workers=1) as requests:
        future = requests.submit(client.get, "/api/v1/preview", params=params)
        try:
            assert entered.wait(2)
            assert future.result(timeout=1).status_code == 200
            for _ in range(3):
                assert client.get("/api/v1/preview", params=params).status_code == 200
            assert calls == [item["id"]]
        finally:
            release.set()
        assert finished.wait(2)
        if first_fails:
            # Completion of an advisory task may race a new request; a lost
            # write must not impose the successful-write throttle window.
            for _ in range(10):
                assert client.get("/api/v1/preview", params=params).status_code == 200
                if len(calls) == 2:
                    break
            assert calls == [item["id"], item["id"]]
        else:
            assert client.get("/api/v1/preview", params=params).status_code == 200
            assert calls == [item["id"]]


def test_slow_recent_writes_leave_workers_for_other_file_opens(app_client, monkeypatch):
    client, store, _, _ = app_client
    files = [
        store.add_file("/", f"file {index}.txt", index + 1, 3) for index in range(3)
    ]
    entered, release = threading.Event(), threading.Event()
    original = store.mark_opened

    def mark_opened(identity):
        entered.set()
        release.wait(3)
        original(identity)

    async def describe(**_kwargs):
        return {"kind": "text", "text": "one", "size": 3}

    monkeypatch.setattr(store, "mark_opened", mark_opened)
    monkeypatch.setattr(app_module, "describe_preview", describe)
    login(client)
    workers = ThreadPoolExecutor(max_workers=2)

    def use_small_executor():
        loop = asyncio.get_running_loop()
        original_executor = loop._default_executor
        loop.set_default_executor(workers)
        return loop, original_executor

    loop, previous = client.portal.call(use_small_executor)
    assert previous is not None
    with ThreadPoolExecutor(max_workers=1) as requests:
        try:
            for item in files:
                future = requests.submit(
                    client.get, "/api/v1/preview", params={"path": "/" + item["id"]}
                )
                assert entered.wait(2)
                assert future.result(timeout=1).status_code == 200
        finally:
            release.set()
            client.portal.call(loop.set_default_executor, previous)
            workers.shutdown(wait=True)


def test_post_preview_lookup_leaves_health_responsive(app_client, monkeypatch):
    client, store, _, _ = app_client
    item = store.add_file("/", "notes.txt", 77, 3)
    entered, release = threading.Event(), threading.Event()
    original = store.get_file
    calls = 0

    def get_file(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            release.wait(3)
        return original(*args, **kwargs)

    async def describe(**_kwargs):
        return {"kind": "text", "text": "one", "size": 3}

    monkeypatch.setattr(store, "get_file", get_file)
    monkeypatch.setattr(app_module, "describe_preview", describe)
    login(client)
    with ThreadPoolExecutor(max_workers=2) as requests:
        preview = requests.submit(
            client.get, "/api/v1/preview", params={"path": "/" + item["id"]}
        )
        try:
            assert entered.wait(2)
            health = requests.submit(client.get, "/health/live")
            assert health.result(timeout=1).status_code == 200
            assert not preview.done()
        finally:
            release.set()
        assert preview.result(timeout=2).status_code == 200


@pytest.mark.parametrize("cache_state", ["cold", "warm"])
async def test_range_adds_one_catalog_authorization_per_extra_block(
    tmp_path, monkeypatch, cache_state
):
    settings = make_settings(tmp_path)
    payload = b"x" * (8 * BLOCK)
    telegram = account("bot", payload)
    pool = ClientPool([telegram], media_cache_dir=tmp_path / "media")
    await pool.start()
    store = new_store()
    item = store.add_file("/", "movie.mkv", 77, len(payload))
    token = store.create_share("/" + item["id"])
    runtime = FakeRuntime(store)
    runtime.pool = pool
    app = app_module.create_app(settings, runtime=runtime)
    reads = []
    aggregate = store.db.nodes.aggregate

    def count(*args, **kwargs):
        reads.append(True)
        return aggregate(*args, **kwargs)

    async def receive():
        return {"type": "http.disconnect"}

    async def range_request(blocks):
        messages = []

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": "/file",
                "raw_path": b"/file",
                "query_string": urlencode(
                    {"path": "/" + item["id"], "auth": token}
                ).encode(),
                "headers": [
                    (b"host", b"testserver"),
                    (b"range", f"bytes=0-{blocks * BLOCK - 1}".encode()),
                ],
                "server": ("testserver", 443),
                "client": ("test", 123),
                "asgi": {"version": "3.0", "spec_version": "2.4"},
            },
            receive,
            send,
        )
        assert messages[0]["status"] == 206
        assert (
            b"".join(message.get("body", b"") for message in messages)
            == payload[: blocks * BLOCK]
        )

    try:
        warm = await stream_telegram_file(
            pool=pool,
            channel_id=settings.storage_channel,
            message_id=77,
            file_name="movie.mkv",
            expected_size=len(payload),
            request=request(),
        )
        assert await response_body(warm) == payload
        remote_reads = sum(telegram.native.requests.values())
        monkeypatch.setattr(store.db.nodes, "aggregate", count)
        if cache_state == "cold":
            await pool._media_read_cache.clear()
        await range_request(4)
        smaller = len(reads)
        reads.clear()
        if cache_state == "cold":
            await pool._media_read_cache.clear()
        await range_request(8)
        assert len(reads) - smaller == 4
        if cache_state == "warm":
            assert sum(telegram.native.requests.values()) == remote_reads
        else:
            assert sum(telegram.native.requests.values()) > remote_reads
    finally:
        await pool._media_read_cache.close()
        await pool.stop()
        app.state.sessions.close()
        runtime.transfers.registry.close()
        store.close()
