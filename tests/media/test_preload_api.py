from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from tests.media.test_multipart_streaming import build_parts
from tests.media.test_pooled_media import account
from tests.media.test_telegram_cache import message
from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.media.media_reader import BLOCK
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import TelegramMessage
from tgdrive.web.app import create_app


class MediaRuntime(FakeRuntime):
    async def start(self):
        await self.pool.start()
        await super().start()

    async def stop(self):
        try:
            if hasattr(self, "preloads"):
                await self.preloads.close()
        finally:
            await self.pool.stop()
            await super().stop()


@pytest.mark.parametrize("multipart", [False, True])
def test_manual_preload_api_caches_entire_original_for_download_and_seek(
    tmp_path, multipart, monkeypatch
):
    settings = make_settings(tmp_path)
    settings.storage_channel = -1000000000123
    settings.auto_preload = False
    contents = [b"a" * (2 * BLOCK + 77), b"b" * (BLOCK + 13)]
    payload = b"".join(contents)
    telegram = account("bot", payload)
    telegram.configure_transport = Mock()
    parts = build_parts(contents) if multipart else None
    if parts:
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
    path = "/" + item["id"]
    runtime = MediaRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, settings)
    runtime.pool = ClientPool([telegram], media_cache_dir=settings.cache_dir)
    app = create_app(settings, runtime=runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            login(client)
            prepared = client.post("/api/v1/preloads", json={"path": path})
            assert prepared.status_code == 202, prepared.text
            assert prepared.headers["cache-control"] == "no-store"
            job = prepared.json()
            assert job["mode"] == "manual"
            assert job["start"] == 0 and job["end"] == len(payload)
            assert job["target_bytes"] == len(payload)

            async def finish():
                task = runtime.preloads._tasks.get(job["id"])
                if task is not None:
                    await asyncio.wait_for(asyncio.shield(task), 5)

            client.portal.call(finish)
            state = client.get("/api/v1/preloads").json()
            assert state["jobs"][0]["state"] == "ready", state
            cached = state["cache"]["files"][0]
            assert cached["complete"]
            assert cached["ready_ranges"] == [[0, len(payload)]]
            reads = telegram.native.requests.copy()
            assert reads
            # Preloading is private server work and never creates a public link.
            assert store.list_shares() == []
            downloaded = client.get("/file", params={"path": path, "download": True})
            assert downloaded.status_code == 200
            assert downloaded.content == payload
            assert "attachment" in downloaded.headers["content-disposition"]
            start, end = len(contents[0]) - 10, len(contents[0]) + 9
            seek = client.get(
                "/file",
                params={"path": path},
                headers={"Range": f"bytes={start}-{end}"},
            )
            assert seek.status_code == 206
            assert (
                seek.headers["content-range"] == f"bytes {start}-{end}/{len(payload)}"
            )
            assert seek.content == payload[start : end + 1]
            assert telegram.native.requests == reads
    finally:
        runtime.transfers.registry.close()
        store.close()


def test_preload_rejects_disabled_cache_but_direct_download_still_works(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path)
    settings.storage_channel = -1000000000123
    settings.cache_memory_bytes = settings.cache_disk_bytes = 0
    payload = b"original file bytes"
    telegram = account("bot", payload)
    telegram.configure_transport = Mock()
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    item = store.add_file("/", "hello.txt", 77, len(payload))
    path = "/" + item["id"]
    runtime = MediaRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, settings)
    runtime.pool = ClientPool([telegram], media_cache_dir=settings.cache_dir)
    app = create_app(settings, runtime=runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            login(client)
            prepared = client.post("/api/v1/preloads", json={"path": path})
            assert prepared.status_code == 409
            assert "budget" in prepared.json()["detail"]
            assert not telegram.native.requests
            assert client.get("/api/v1/preloads").json()["jobs"] == []
            download = client.get("/file", params={"path": path, "download": True})
            assert download.status_code == 200
            assert download.content == payload
    finally:
        runtime.transfers.registry.close()
        store.close()
