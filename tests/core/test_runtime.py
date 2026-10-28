from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import tgdrive.core.runtime as runtime_module
from tgdrive.core.config import Settings
from tgdrive.core.runtime import Runtime
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.store import DriveStore


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "MONGODB_URI": "mongodb://synthetic.invalid",
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


@pytest.fixture(autouse=True)
def mongo_backend(monkeypatch):
    """Exercise lifecycle orchestration against an explicit database adapter."""
    backend = MagicMock()
    backend.transaction.side_effect = lambda callback: callback(None)
    backend.db.settings.find_one.return_value = None
    for name in ("jobs", "receipts", "bot_folders"):
        getattr(backend.db, name).find.return_value = []
    monkeypatch.setattr("tgdrive.storage.mongo.for_settings", lambda _: backend)
    monkeypatch.setattr("tgdrive.storage.mongo.initialize_if_empty", lambda *_: False)
    monkeypatch.setattr("tgdrive.storage.mongo.record_drive_open", lambda *_: None)
    monkeypatch.setattr(
        "tgdrive.replication.replica_activation.apply_activation", lambda *_: None
    )
    return backend


class FakePool:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started = False
        self.stopped = False

    async def set_uploads_per_bot(self, value: int) -> None:
        self.uploads_per_bot = value

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("bad credentials")
        self.started = True

    async def stop(self, *, drain_timeout: float | None = None):
        assert drain_timeout in {10, 20}
        self.stopped = True
        return ()


class FakeRemote:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBackup:
    def __init__(self, store: DriveStore) -> None:
        self.store = store
        self.backups = 0
        self.started = False
        self.stopped = False

    async def restore(self) -> DriveStore:
        return self.store

    async def backup(self, store: DriveStore) -> bool:
        self.backups += 1
        revision, _ = store.snapshot()
        store.mark_backed_up(revision)
        return True

    def start(self, _store: DriveStore) -> None:
        self.started = True

    async def stop(self, _store: DriveStore) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_runtime_starts_once_and_stops_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DriveStore.create(tmp_path / "drive.json")
    pool = FakePool()
    runtime = Runtime(settings(tmp_path), pool=pool)
    backup = FakeBackup(store)
    remote = FakeRemote()
    runtime.backup = backup
    runtime.remote = remote
    monkeypatch.setattr(runtime_module, "build_bot_mode_client", lambda _settings: None)

    await runtime.start()
    await runtime.start()

    assert runtime.ready
    assert runtime.store is store
    assert runtime.transfers is not None
    assert backup.backups == 1
    assert backup.started
    assert pool.started

    await runtime.stop()
    assert not runtime.ready
    assert backup.stopped
    assert remote.closed
    assert pool.stopped


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_runtime_persists_recovery_key_before_workers_without_blocking_browsing(
    tmp_path,
    monkeypatch,
    conflict,
):
    import threading

    from tgdrive.replication.replica import ChannelReplica
    from tgdrive.settings.managed_settings import SettingsProblem

    store = DriveStore.create(tmp_path / "drive.json")
    pool = FakePool()
    runtime = Runtime(settings(tmp_path), pool=pool)
    backup = FakeBackup(store)
    runtime.backup = backup
    runtime.remote = FakeRemote()
    caller = threading.get_ident()
    calls = []

    def persist(replica):
        assert replica.runtime.store is store
        assert not backup.started and not pool.started
        assert threading.get_ident() != caller
        calls.append(True)
        if conflict:
            raise SettingsProblem(
                "Recovery key conflict", code="RECOVERY_KEY_CONFLICT", status=409
            )
        return "a1" * 32

    monkeypatch.setattr(ChannelReplica, "key", persist)
    monkeypatch.setattr(runtime_module, "build_bot_mode_client", lambda _settings: None)
    try:
        await runtime.start()
        assert calls == [True]
        assert runtime.ready and backup.started and pool.started
        assert runtime.store.list_directory("/") == {}
        if conflict:
            assert runtime.replica.status()["error"]["code"] == "RECOVERY_KEY_CONFLICT"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_telegram_outage_keeps_local_metadata_available(tmp_path: Path) -> None:
    pool = FakePool(fail_start=True)
    config = settings(tmp_path)
    (config.resolved_data_dir / "drive.sqlite3").parent.mkdir(parents=True)
    store = DriveStore.create(config.resolved_data_dir / "drive.sqlite3")
    runtime = Runtime(config, pool=pool)
    runtime.backup = FakeBackup(store)
    remote = FakeRemote()
    runtime.remote = remote
    await runtime.start()
    assert runtime.ready
    assert runtime.store is not None
    assert runtime.store.list_directory("/") == {}
    await runtime.stop()
    assert pool.stopped
    assert remote.closed
    assert not runtime.ready


@pytest.mark.parametrize("failure", ["ping", "acquire", "transaction"])
async def test_mongodb_failure_rejects_startup_even_with_recovery_state(
    tmp_path, mongo_backend, failure, caplog
):
    getattr(mongo_backend, failure).side_effect = DatabaseUnavailable(
        "Synthetic MongoDB failure"
    )
    pool = FakePool()
    runtime = Runtime(settings(tmp_path), pool=pool)
    runtime._activation_unavailable = True
    restore = AsyncMock()
    runtime.backup = Mock(restore=restore)

    with pytest.raises(DatabaseUnavailable, match="MongoDB failure"):
        await runtime.start()

    assert not runtime.ready
    assert not hasattr(runtime, "replica")
    restore.assert_not_awaited()
    assert pool.stopped
    assert not pool.started
    assert runtime._owner_lock is None
    assert "startup rejected" in caplog.text
    assert "restart the deployment" in caplog.text


def test_runtime_rejects_dangerous_cache_root(tmp_path: Path) -> None:
    root = Path(tmp_path.anchor)
    runtime = Runtime(settings(tmp_path, CACHE_DIR=root), pool=FakePool())

    with pytest.raises(ValueError, match="CACHE_DIR"):
        runtime._prepare_runtime_directories()


@pytest.mark.parametrize("nested", ["data-in-cache", "cache-in-data", "equal"])
def test_runtime_rejects_overlapping_storage_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: str
) -> None:
    cache = tmp_path / "cache"
    data = cache / "uploads" / "metadata"
    if nested == "cache-in-data":
        cache, data = data, cache
    elif nested == "equal":
        data = cache
    removed: list[Path] = []
    monkeypatch.setattr(shutil, "rmtree", lambda path, **_: removed.append(path))
    runtime = Runtime(
        settings(tmp_path, CACHE_DIR=cache, DATA_DIR=data), pool=FakePool()
    )

    with pytest.raises(ValueError, match="overlap"):
        runtime._prepare_runtime_directories()

    assert removed == []
    assert not cache.exists()
    assert not data.exists()


def test_runtime_rejects_cleanup_symlink_outside_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    outside = tmp_path / "outside"
    cache.mkdir()
    outside.mkdir()
    (cache / "uploads").symlink_to(outside, target_is_directory=True)
    removed: list[Path] = []
    monkeypatch.setattr(shutil, "rmtree", lambda path, **_: removed.append(path))
    runtime = Runtime(settings(tmp_path, CACHE_DIR=cache), pool=FakePool())

    with pytest.raises(ValueError, match="cleanup paths"):
        runtime._prepare_runtime_directories()

    assert removed == []
    assert outside.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnecting", [False, True])
async def test_bot_mode_shares_channel_send_pacing_and_flood_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reconnecting: bool
) -> None:
    store = DriveStore.create(tmp_path / "drive.json")

    class PacedPool(FakePool):
        async def pace_send(self, channel: int) -> None:
            pass

        async def defer_channel(self, deadline: float) -> None:
            pass

    class BoundClient:
        gate = None
        defer = None

        def bind_send_gate(self, gate, defer):
            self.gate, self.defer = gate, defer

    class Bot:
        def __init__(self, *, client, **_):
            self.client = client

        async def start(self):
            assert self.client.gate == pool.pace_send
            assert self.client.defer == pool.defer_channel

        async def stop(self):
            pass

        def close(self):
            pass

    pool = PacedPool()
    client = BoundClient()
    runtime = Runtime(settings(tmp_path), pool=pool)
    runtime.backup = FakeBackup(store)
    runtime.remote = FakeRemote()
    monkeypatch.setattr(runtime_module, "BotMode", Bot)
    monkeypatch.setattr(
        runtime_module,
        "build_bot_mode_client",
        lambda _: None if reconnecting else client,
    )
    try:
        await runtime.start()
        if reconnecting:
            monkeypatch.setattr(
                runtime_module,
                "build_bot_mode_client",
                lambda _: client,
            )
            await runtime._reconnect()
        assert runtime.bot_mode.client is client
        assert client.gate == pool.pace_send
        assert client.defer == pool.defer_channel
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_bot_reconnect_retries_failed_start_without_registering_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = FakePool()
    pool.pace_send = AsyncMock()
    pool.defer_channel = AsyncMock()
    client = Mock(
        start=AsyncMock(side_effect=[ConnectionError("temporary outage"), None])
    )
    factory = Mock(return_value=client)
    runtime = Runtime(settings(tmp_path), pool=pool)
    runtime.ready = True
    runtime.backup = Mock(backup=AsyncMock())
    runtime._verify_storage_channel = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(runtime_module, "build_bot_mode_client", factory)
    monkeypatch.setattr(runtime_module.asyncio, "sleep", sleep)

    try:
        await runtime._reconnect()

        assert runtime.bot_mode is not None
        assert runtime.bot_mode._started
        assert client.start.await_count == 2
        client.register_bot.assert_called_once_with(runtime.bot_mode)
        factory.assert_called_once_with(runtime.settings)
        sleep.assert_awaited_once_with(30)
    finally:
        runtime.ready = False
        await runtime.remote.close()
        runtime.registry.close()
