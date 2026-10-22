"""Production lifecycle wiring for mandatory completed-upload protection."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import tgdrive.runtime as runtime_module
from tgdrive.config import Settings
from tgdrive.runtime import Runtime
from tgdrive.store import DriveStore


def configuration(tmp_path: Path, **overrides) -> Settings:
    values = {
        "API_ID": 12345,
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30,
        "STORAGE_CHANNEL": -1001234567890,
        "ADMIN_PASSWORD": "correct horse battery staple",
        "SESSION_SECRET": "s" * 48,
        "DATA_DIR": tmp_path / "data",
        "CACHE_DIR": tmp_path / "cache",
        "COOKIE_SECURE": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class RuntimePool:
    def __init__(self, *, unavailable=False):
        self.unavailable = unavailable
        self.stopped = False

    async def set_uploads_per_bot(self, value):
        self.uploads_per_bot = value

    async def start(self):
        if self.unavailable:
            raise RuntimeError("synthetic Telegram outage")

    async def stop(self, *, drain_timeout=None):
        self.stopped = True
        return ()


async def test_runtime_upload_barrier_always_calls_its_store_backup(
    tmp_path, monkeypatch
):
    settings = configuration(tmp_path)
    settings.resolved_data_dir.mkdir(parents=True)
    store = DriveStore.create(settings.resolved_data_dir / "drive.sqlite3")
    store.mark_backed_up(store.revision)
    runtime = Runtime(settings, pool=RuntimePool())
    runtime.backup.restore = AsyncMock(return_value=store)
    runtime.backup.ensure_revision = AsyncMock(return_value=store.revision)
    runtime.backup.start = Mock()
    runtime.backup.stop = AsyncMock()
    monkeypatch.setattr(runtime_module, "build_bot_mode_client", lambda _: None)
    try:
        await runtime.start()
        assert runtime.transfers is not None
        assert runtime.transfers._ensure_backup is not None
        protected = await runtime.transfers._ensure_backup(store.revision)
        assert protected == store.revision
        runtime.backup.ensure_revision.assert_awaited_once_with(store, store.revision)
    finally:
        await runtime.stop()


async def test_missing_database_never_uses_leftover_initialize_flag(tmp_path):
    pool = RuntimePool()
    runtime = Runtime(configuration(tmp_path, INITIALIZE_EMPTY_DRIVE=True), pool=pool)

    try:
        await runtime.start()
        assert runtime.recovery_only
        assert not runtime.ready
        assert runtime.store is None
        assert runtime.transfers is None
        assert runtime.bot_mode is None
        assert not runtime._reconnector
        assert runtime._owner_lock is not None
        assert runtime.replica.status()["recovery_only"]
        assert not runtime.settings.resolved_data_dir.joinpath("drive.sqlite3").exists()
        assert not runtime.settings.resolved_data_dir.joinpath("jobs.sqlite3").exists()
        # The recovery UI may inspect preserved Telegram metadata, but stale
        # initialization intent cannot create an empty replacement catalog.
        assert any(
            "explicit recovery" in row["message"]
            for row in runtime.replica.status()["report"]
        )
    finally:
        await runtime.stop()
    assert runtime._owner_lock is None
    assert pool.stopped
    assert not runtime.settings.resolved_data_dir.joinpath("drive.sqlite3").exists()


async def test_real_existing_database_opens_during_telegram_outage(tmp_path):
    settings = configuration(tmp_path)
    settings.resolved_data_dir.mkdir(parents=True)
    store = DriveStore.create(settings.resolved_data_dir / "drive.sqlite3")
    store.create_folder("/", "Offline catalog")
    store.close()
    pool = RuntimePool(unavailable=True)
    runtime = Runtime(settings, pool=pool)
    runtime.backup.start = Mock()
    runtime.backup.stop = AsyncMock()
    try:
        await runtime.start()
        assert runtime.ready
        assert [
            item["name"] for item in runtime.store.list_directory("/").values()
        ] == ["Offline catalog"]
        assert runtime.transfers._ensure_backup is not None
    finally:
        await runtime.stop()
    assert pool.stopped
