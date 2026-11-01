from __future__ import annotations

from io import BytesIO

import pytest
from fastapi import UploadFile

import tgdrive.transfers.manager as transfer_module
from tests.transfers.test_transfers import make_manager
from tgdrive.settings.managed_settings import SettingsProblem


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
                "Recovery has paused file changes",
                code="RECOVERY_REQUIRED",
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
