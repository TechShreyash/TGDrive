from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import tgdrive.runtime as runtime_module
from tgdrive.config import Settings
from tgdrive.runtime import Runtime
from tgdrive.store import DriveStore


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "API_ID": 12345,
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30,
        "STORAGE_CHANNEL": -1001234567890,
        "DATABASE_BACKUP_MSG_ID": 42,
        "ADMIN_PASSWORD": "correct horse battery staple",
        "SESSION_SECRET": "s" * 48,
        "DATA_DIR": tmp_path / "data",
        "CACHE_DIR": tmp_path / "cache",
        "COOKIE_SECURE": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


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
async def test_telegram_outage_keeps_local_metadata_available(tmp_path: Path) -> None:
    pool = FakePool(fail_start=True)
    config = settings(tmp_path, INITIALIZE_EMPTY_DRIVE=True)
    (config.resolved_data_dir / "drive.sqlite3").parent.mkdir(parents=True)
    DriveStore.create(config.resolved_data_dir / "drive.sqlite3").close()
    runtime = Runtime(config, pool=pool)
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
