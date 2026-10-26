from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import tgdrive.core.config as config_module
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.core.config import Settings
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool, current_upload_limit
from tgdrive.transfers.tasks import TransferRegistry
from tgdrive.web.app import create_app


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path / "data")


class Account:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.is_connected = False

    async def start(self) -> None:
        if self.fail:
            raise ConnectionError("synthetic optional account unavailable")
        self.is_connected = True

    async def stop(self) -> None:
        self.is_connected = False


class Runtime:
    def __init__(self, store: DriveStore, pool: ClientPool, user: Account, state: str):
        self.store = store
        self.pool = pool
        self.user = user
        self.state = state
        self.transfers = SimpleNamespace(store=store, registry=TransferRegistry())
        self.ready = False

    async def start(self) -> None:
        await self.pool.start()
        if self.state == "disconnected":
            self.user.is_connected = False
        if self.state == "unknown":
            del self.user.is_connected
        self.ready = True

    async def stop(self) -> None:
        self.ready = False
        if hasattr(self.transfers, "uploads"):
            self.transfers.uploads.close()
        await self.pool.stop()
        self.store.close()


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        MONGODB_URI="mongodb://synthetic.invalid",
        API_ID=12345,
        API_HASH="a" * 32,
        BOT_TOKENS="123456:" + "x" * 30,
        STRING_SESSIONS="synthetic-user-session",
        STORAGE_CHANNEL=-1001234567890,
        ADMIN_PASSWORD="synthetic capacity password",
        SESSION_SECRET="s" * 48,
        DATA_DIR=tmp_path / "data",
        CACHE_DIR=tmp_path / "cache",
        MIN_FREE_BYTES=0,
    )


@pytest.mark.parametrize("state", ["healthy", "failed", "disconnected", "unknown"])
def test_api_logical_capacity_is_independent_of_optional_premium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, mongo_backend
) -> None:
    monkeypatch.setattr(config_module, "_DEFAULT_BOT_LIMIT", 3)
    config = settings(tmp_path)
    user = Account("premium", fail=state == "failed")
    pool = ClientPool([Account("bot")], [user], allow_partial_start=True)
    runtime = Runtime(DriveStore.create(), pool, user, state)
    runtime.mongo = mongo_backend
    app = create_app(config, runtime=runtime)
    expected = config.upload_limit

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/config").json()["max_file_size"] == expected
        assert (
            client.post(
                "/api/checkPassword", json={"pass": "synthetic capacity password"}
            ).status_code
            == 200
        )
        status = client.get("/api/v1/status")
        assert status.status_code == 200
        assert status.json()["max_file_size"] == expected
        assert status.json()["telegram_ready"] is True

        receipt = client.post(
            "/api/v1/uploads",
            json={"name": "four.bin", "parent_id": "/", "total_size": 4},
        )
        assert receipt.status_code == 201
        identity = receipt.json()["id"]
        user.is_connected = False
        assert client.get("/api/config").json()["max_file_size"] == expected
        appended = client.patch(
            f"/api/v1/uploads/{identity}",
            content=b"four",
            headers={"Content-Type": "application/octet-stream", "Upload-Offset": "0"},
        )
        assert appended.status_code == 204
        assert appended.headers["upload-offset"] == "4"
        assert client.post(f"/api/v1/uploads/{identity}/cancel").status_code == 200
        rejected = client.post(
            "/api/v1/uploads",
            json={
                "name": "too-big.bin",
                "parent_id": "/",
                "total_size": expected + 1,
            },
        )
        assert rejected.status_code == 413


@pytest.mark.asyncio
async def test_unstarted_and_synthetic_pools_keep_logical_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "_DEFAULT_BOT_LIMIT", 3)
    config = settings(tmp_path)
    pool = ClientPool([Account("bot")], [Account("premium")])
    assert config.upload_limit == 3 * 4096
    assert await current_upload_limit(config, pool) == config.upload_limit
    assert await current_upload_limit(config, object()) == config.upload_limit
    assert await current_upload_limit(config, None) == config.upload_limit


@pytest.mark.asyncio
async def test_remote_and_legacy_upload_admission_enforces_logical_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from io import BytesIO

    from fastapi import UploadFile

    from tgdrive.transfers.manager import TransferManager, TransferTooLarge
    from tgdrive.transfers.remote import RemoteTooLarge

    monkeypatch.setattr(config_module, "_DEFAULT_BOT_LIMIT", 3)
    config = settings(tmp_path)
    pool = ClientPool(
        [Account("bot")], [Account("premium", fail=True)], allow_partial_start=True
    )
    await pool.start()

    class Remote:
        async def inspect(self, _url: str):
            return SimpleNamespace(size=config.upload_limit + 1)

        async def close(self) -> None:
            pass

    store = DriveStore.create()
    manager = TransferManager(
        settings=config,
        pool=pool,
        store=store,
        remote=Remote(),
        registry=TransferRegistry(),
    )
    try:
        with pytest.raises(RemoteTooLarge) as captured:
            await manager.inspect_remote("https://example.com/four.bin")
        assert captured.value.limit == config.upload_limit
        upload = UploadFile(
            BytesIO(b"four"), size=config.upload_limit + 1, filename="four.bin"
        )
        with pytest.raises(TransferTooLarge):
            await manager.receive_browser_upload(upload, "/")
        assert upload.file.closed
        assert not (config.resolved_cache_dir / "uploads").exists()
        assert manager.registry.list() == []
    finally:
        await manager.close()
        await pool.stop()
        store.close()
