"""Shared synthetic application resources and authenticated HTTP fixtures."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.support.mongo_fixtures import FakeMongoBackend, register_test_backend
from tgdrive.core.config import Settings
from tgdrive.storage.store import DriveStore
from tgdrive.transfers.tasks import TransferRegistry
from tgdrive.web.app import create_app


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        MONGODB_URI="mongodb://synthetic.invalid",
        API_ID=12345,
        API_HASH="a" * 32,
        BOT_TOKENS="123456:" + "x" * 30,
        STORAGE_CHANNEL=-1001234567890,
        ADMIN_PASSWORD="correct horse battery staple",
        SESSION_SECRET="s" * 48,
        DATA_DIR=tmp_path / "data",
        CACHE_DIR=tmp_path / "cache",
        COOKIE_SECURE=True,
    )


class FakeTransfers:
    def __init__(self) -> None:
        self.registry = TransferRegistry()
        self.deleted_messages: list[int] = []
        self.remote_requests: list[tuple[str, str, str | None]] = []
        self.remote_modes: list[bool] = []

    async def start_deletion(self, path: str):
        intent = self.store.begin_deletion(path, channel_id=-1001234567890)
        record = await self.registry.create(
            "deleting", total=len(intent["message_ids"])
        )

        async def complete(item):
            self.store.finish_deletion(intent["id"])

        self.registry.spawn(record.id, complete)
        return record

    async def start_deletions(self, paths: list[str]):
        return [await self.start_deletion(path) for path in paths]

    async def delete_telegram_messages(self, message_ids: list[int]) -> None:
        self.deleted_messages.extend(message_ids)

    async def receive_browser_upload(
        self,
        upload: Any,
        destination: str,
        **_kwargs: Any,
    ):
        assert destination == "/"
        record = await self.registry.create("uploading", total=3)
        on_created = _kwargs.get("on_created")
        if on_created:
            created = on_created(record)
            if inspect.isawaitable(created):
                await created
        await upload.close()
        return record

    async def inspect_remote(self, url: str):
        return SimpleNamespace(
            filename="remote.bin",
            size=12,
            content_type="application/octet-stream",
            url=url,
        )

    async def start_remote(
        self,
        *,
        url: str,
        destination: str,
        filename: str | None,
        single_threaded: bool = False,
    ):
        self.remote_requests.append((url, destination, filename))
        self.remote_modes.append(single_threaded)
        return await self.registry.create("checking_source")


class FakeRuntime:
    def __init__(self, store: DriveStore) -> None:
        self.store = store
        self.mongo = FakeMongoBackend()
        self.transfers = FakeTransfers()
        self.transfers.store = store
        self.pool = object()
        self.ready = False
        self.stopped = False

    async def start(self) -> None:
        self.ready = True

    async def stop(self) -> None:
        uploads = getattr(self.transfers, "uploads", None)
        if uploads is not None:
            uploads.close()
        self.ready = False
        self.stopped = True


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.json")
    runtime = FakeRuntime(store)
    register_test_backend(monkeypatch, settings, runtime.mongo)
    app = create_app(settings, runtime=runtime)
    with TestClient(app, base_url="https://testserver") as client:
        yield client, store, runtime, settings
    assert runtime.stopped


def login(client: TestClient) -> None:
    response = client.post(
        "/api/checkPassword", json={"pass": "correct horse battery staple"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
