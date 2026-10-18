from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from test_app import FakeRuntime, make_settings
from test_transfers import make_manager

import tgdrive.transfers as transfer_module
from tgdrive.app import create_app
from tgdrive.managed_settings import SettingsProblem
from tgdrive.store import DriveStore


@pytest.mark.asyncio
async def test_settings_saved_during_async_multipart_admission_never_create_a_job(
    tmp_path,
    monkeypatch,
):
    manager, store, transport, pool = await make_manager(tmp_path)
    blocked = False

    async def changed_during_health(*_args):
        nonlocal blocked
        blocked = True
        return 1024

    def check():
        if blocked:
            raise SettingsProblem(
                "Restart required",
                code="SETTINGS_RESTART_REQUIRED",
                status=409,
            )

    manager._check_upload_allowed = check
    monkeypatch.setattr(transfer_module, "current_upload_limit", changed_during_health)
    upload = UploadFile(BytesIO(b"hello"), size=5, filename="hello.txt")
    try:
        with pytest.raises(SettingsProblem):
            await manager.receive_browser_upload(upload, "/")
        assert upload.file.closed
        assert manager.registry.list() == []
        assert store.get_directory("/")["contents"] == {}
        assert not transport.started_upload.is_set()
    finally:
        manager.registry.close()
        store.close()
        await pool.stop()


def test_settings_saved_during_remote_inspection_never_enqueue_a_job(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.json")
    runtime = FakeRuntime(store)
    runtime.connection_manager = SimpleNamespace(
        restart_required=False, close=AsyncMock()
    )

    async def changed_during_inspection(_url):
        runtime.connection_manager.restart_required = True
        return SimpleNamespace(size=12)

    runtime.transfers.inspect_remote = changed_during_inspection
    app = create_app(settings, runtime=runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post(
                "/api/checkPassword",
                json={
                    "pass": settings.admin_password.get_secret_value(),
                },
            )
            result = client.post(
                "/api/startFileDownloadFromUrl",
                json={
                    "url": "https://example.com/source.bin",
                    "path": "/",
                },
            )
            assert result.status_code == 409
            assert result.json()["code"] == "SETTINGS_RESTART_REQUIRED"
            assert runtime.transfers.remote_requests == []
            assert runtime.transfers.registry.list() == []
    finally:
        store.close()
