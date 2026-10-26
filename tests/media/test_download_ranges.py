"""Resumed and segmented HTTP downloads only fetch their requested file blocks."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from tests.media.test_multipart_streaming import build_parts
from tests.media.test_pooled_media import account
from tests.media.test_preload_api import MediaRuntime
from tests.media.test_telegram_cache import message
from tests.support.app_fixtures import login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.media.media_reader import BLOCK
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import TelegramMessage
from tgdrive.web.app import create_app


@pytest.fixture(params=[False, True], ids=["single", "multipart"])
def download_drive(tmp_path, request, monkeypatch):
    settings = make_settings(tmp_path)
    settings.storage_channel = -1000000000123
    settings.auto_preload = True
    payload = b"".join(bytes([index]) * BLOCK for index in range(32)) + b"tail"
    telegram = account("bot", payload)
    telegram.configure_transport = Mock()
    parts = None
    if request.param:
        contents = [payload[: 13 * BLOCK + 17], payload[13 * BLOCK + 17 :]]
        parts = build_parts(contents)
        telegram.native.payloads = {9001: contents[0], 9002: contents[1]}

        async def fetch(_channel, *, message_ids):
            part = parts[message_ids - 101]
            native = message(part["document_id"], part["size"])
            native.id = message_ids
            return TelegramMessage(telegram, native)

        telegram.get_messages = AsyncMock(side_effect=fetch)
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    item = store.add_file(
        "/",
        "movie.mp4",
        parts[0]["message_id"] if parts else 77,
        len(payload),
        parts=parts,
    )
    runtime = MediaRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, settings)
    runtime.pool = ClientPool([telegram], media_cache_dir=settings.cache_dir)
    app = create_app(settings, runtime=runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            login(client)
            assert client.get("/api/v1/settings/performance").status_code == 200
            yield client, runtime, telegram, "/" + item["id"], payload, parts
    finally:
        runtime.transfers.registry.close()
        store.close()


def requested_blocks(parts, size, intervals):
    allowed = set()
    manifest = parts or [{"offset": 0, "size": size, "document_id": 1}]
    for start, end in intervals:
        for part in manifest:
            lo = max(start, part["offset"])
            hi = min(end, part["offset"] + part["size"] - 1)
            if lo <= hi:
                allowed.update(
                    (part["document_id"], index)
                    for index in range(
                        (lo - part["offset"]) // BLOCK,
                        (hi - part["offset"]) // BLOCK + 1,
                    )
                )
    return allowed


def assert_no_unrequested_reads(telegram, parts, payload, intervals):
    actual = {
        (identity, offset // BLOCK) for identity, offset in telegram.native.requests
    }
    assert actual == requested_blocks(parts, len(payload), intervals)


def settle_background(client, runtime):
    async def settle():
        tasks = list(runtime.preloads._auto_tasks.values())
        tasks += list(runtime.preloads._tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)

    client.portal.call(settle)
    assert client.get("/api/v1/preloads").json()["jobs"] == []


@pytest.mark.parametrize(
    "attachment", [False, True], ids=["inline-url", "download-url"]
)
def test_resumed_overlapping_suffix_and_open_ended_ranges_do_not_warm_whole_file(
    download_drive, attachment
):
    client, runtime, telegram, path, payload, parts = download_drive
    intervals = []
    cases = [
        (4 * BLOCK + 37, 10 * BLOCK + 22, None),
        # A manager may retry overlapping bytes; that is not a second viewing.
        (5 * BLOCK + 17, 9 * BLOCK + 99, None),
        (29 * BLOCK + 5, len(payload) - 1, f"bytes={29 * BLOCK + 5}-"),
        (len(payload) - BLOCK - 9, len(payload) - 1, f"bytes=-{BLOCK + 9}"),
        # Seek backwards across a Telegram part boundary.
        (12 * BLOCK + 7, 17 * BLOCK + 31, None),
    ]
    params = {"path": path, "request_id": "download-manager-source"}
    if attachment:
        params["download"] = "1"
    for start, end, header in cases:
        response = client.get(
            "/file",
            params=params,
            headers={"Range": header or f"bytes={start}-{end}"},
        )
        assert response.status_code == 206
        assert response.headers["content-length"] == str(end - start + 1)
        assert (
            response.headers["content-range"] == f"bytes {start}-{end}/{len(payload)}"
        )
        assert response.content == payload[start : end + 1]
        intervals.append((start, end))
        settle_background(client, runtime)
        assert_no_unrequested_reads(telegram, parts, payload, intervals)


def test_parallel_missing_ranges_reassemble_original_and_reuse_overlaps(download_drive):
    client, runtime, telegram, path, payload, parts = download_drive
    # Simulate a partial device file whose other sections were saved earlier.
    device = bytearray(payload)
    intervals = [
        (19 * BLOCK + 13, 24 * BLOCK + 1),
        (2 * BLOCK + 23, 7 * BLOCK + 3),
        (12 * BLOCK + 9, 17 * BLOCK + 19),
    ]
    for start, end in intervals:
        device[start : end + 1] = b"\0" * (end - start + 1)

    def fetch(interval):
        start, end = interval
        result = client.get(
            "/file",
            params={"path": path, "download": "1"},
            headers={"Range": f"bytes={start}-{end}"},
        )
        assert result.status_code == 206
        assert result.content == payload[start : end + 1]
        return interval, result.content

    with ThreadPoolExecutor(max_workers=3) as workers:
        for (start, end), content in workers.map(fetch, intervals):
            device[start : end + 1] = content
    assert bytes(device) == payload
    settle_background(client, runtime)
    assert_no_unrequested_reads(telegram, parts, payload, intervals)
    reads = telegram.native.requests.copy()
    with ThreadPoolExecutor(max_workers=3) as workers:
        list(workers.map(fetch, reversed(intervals)))
    assert telegram.native.requests == reads


def test_range_resume_requires_matching_representation(download_drive):
    client, runtime, telegram, path, payload, parts = download_drive
    params = {"path": path, "download": "1"}
    start, end = 5 * BLOCK + 17, 10 * BLOCK + 3
    first = client.get(
        "/file", params=params, headers={"Range": f"bytes={start}-{end}"}
    )
    assert first.status_code == 206
    reads = telegram.native.requests.copy()
    resumed = client.get(
        "/file",
        params=params,
        headers={"Range": f"bytes={start}-{end}", "If-Range": first.headers["etag"]},
    )
    assert resumed.status_code == 206 and resumed.content == first.content
    assert telegram.native.requests == reads
    assert_no_unrequested_reads(telegram, parts, payload, [(start, end)])
    stale = client.get(
        "/file",
        params=params,
        headers={"Range": f"bytes={start}-{end}", "If-Range": '"old-file-version"'},
    )
    # The client must restart rather than splice bytes from different versions.
    assert stale.status_code == 200 and stale.content == payload
    assert "content-range" not in stale.headers
    settle_background(client, runtime)
