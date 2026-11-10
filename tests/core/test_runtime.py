from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import tgdrive.core.runtime as runtime_module
from tests.support.mongo_fixtures import Collection, new_store
from tgdrive.core.config import Settings
from tgdrive.core.runtime import Runtime
from tgdrive.recovery.mongo_backup import MongoProtection
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.remote import RemoteClient


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "MONGODB_URI": "mongodb://synthetic.invalid",
        "API_ID": 12345,
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30,
        "STORAGE_CHANNEL": -1001234567890,
        "ADMIN_PASSWORD": "correct horse battery staple",
        "SESSION_SECRET": "s" * 48,
        "ENCRYPTION_KEY": "e" * 64,
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
    from tgdrive.core import encryption

    # A current drive always carries its key check; other settings are absent.
    backend.db.settings.find_one.side_effect = lambda query, **_: (
        encryption.key_record("e" * 64) if query.get("_id") == "encryption" else None
    )
    # The backup journal needs real document storage to round-trip its state.
    for name in ("replica_state", "replica_chunks", "replica_copies", "nodes"):
        setattr(backend.db, name, Collection())
    for name in ("jobs", "receipts", "bot_folders"):
        getattr(backend.db, name).find.return_value = []
    monkeypatch.setattr("tgdrive.storage.mongo.for_settings", lambda _: backend)
    monkeypatch.setattr("tgdrive.storage.mongo.initialize_if_empty", lambda *_: False)
    monkeypatch.setattr("tgdrive.storage.mongo.database_is_empty", lambda *_: False)
    monkeypatch.setattr(
        "tgdrive.replication.replica_activation.apply_activation", lambda *_: None
    )
    return backend


class FakePool:
    pace_send: Callable[[int], Awaitable[None]]
    defer_channel: Callable[[float], Awaitable[None]]

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
    def __init__(self, store: MongoStore) -> None:
        self.store = store
        self.backups = 0
        self.started = False
        self.stopped = False

    async def restore(self) -> MongoStore:
        return self.store

    async def backup(self, store: MongoStore) -> bool:
        self.backups += 1
        store.export()
        return True

    def start(self, _store: MongoStore) -> None:
        self.started = True

    async def stop(self, _store: MongoStore) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_startup_tunes_restored_staging_before_transfer_or_bot_admission(
    tmp_path, monkeypatch, mongo_backend
):
    from tgdrive.core import governor
    from tgdrive.transfers.manager import TransferManager
    from tgdrive.transfers.staging import check_staging, staging_usage

    gib = governor.GIB
    config = settings(tmp_path, PRELOAD_MAX_JOBS=8)
    directory = config.resolved_cache_dir / "torrents" / "restored-job"
    directory.mkdir(parents=True)
    mongo_backend.db.jobs = Collection()
    mongo_backend.db.jobs.insert_one(
        {
            "_id": "restored-job",
            "data": {
                "id": "restored-job",
                "phase": "downloading",
                "status": "queued",
                "current": 4 * gib,
                "total": 6 * gib,
                "created_at": 1.0,
                "updated_at": 1.0,
                "context": {
                    "kind": "torrent",
                    "staging_directory": str(directory),
                    "staging_size": 6 * gib,
                    "staging_written": 4 * gib,
                },
            },
        }
    )
    mongo_backend.db.receipts = Collection()
    mongo_backend.db.receipts.insert_one(
        {
            "_id": "b" * 32,
            "data": {
                "id": "b" * 32,
                "state": "receiving",
                "total_size": 4 * gib,
                "offset": 3 * gib,
                "expires_at": 1e12,
            },
        }
    )
    runtime = Runtime(config, pool=cast(ClientPool, FakePool()))
    runtime.backup = cast(
        MongoProtection, FakeBackup(new_store(tmp_path / "drive.json"))
    )
    runtime.remote = cast(RemoteClient, FakeRemote())
    admissions = []

    def measure(path, media_path):
        assert path == config.resolved_cache_dir
        assert media_path == config.resolved_data_dir / "media-cache"
        return {
            "memory_limit": None,
            "memory_available": None,
            "cpus": 1,
            "disk_total": 100 * gib,
            "disk_free": 8 * gib,
        }

    async def recover(manager):
        assert manager is runtime.transfers
        assert staging_usage(manager) == (10 * gib, 3 * gib)
        assert config.staging_budget_bytes == config.max_file_size == 10 * gib
        assert config.min_free_bytes == 5 * gib
        assert config.preload_max_jobs == 4
        check_staging(config, manager, 0)
        assert (
            runtime.governor.snapshot()["measurements"]["staging_allocated_bytes"]
            == 7 * gib
        )
        admissions.append("recover")

    def bot_client(_settings):
        assert admissions == ["recover"]
        assert config.staging_budget_bytes == 10 * gib
        admissions.append("bot")

    monkeypatch.setattr(governor, "measure", measure)
    monkeypatch.setattr(governor.Governor, "start", lambda _: None)
    monkeypatch.setattr(TransferManager, "recover", recover)
    monkeypatch.setattr(runtime_module, "build_bot_mode_client", bot_client)
    monkeypatch.setattr(
        "tgdrive.transfers.staging.shutil.disk_usage",
        lambda _: SimpleNamespace(free=8 * gib),
    )
    try:
        await runtime.start()
        assert runtime.ready
        assert admissions == ["recover", "bot"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_failed_receipt_restore_preserves_error_and_closes_started_resources(
    tmp_path, monkeypatch
):
    store = new_store(tmp_path / "drive.json")
    store.close = Mock(wraps=store.close)
    pool = FakePool()
    remote = FakeRemote()
    runtime = Runtime(settings(tmp_path), pool=cast(ClientPool, pool))
    runtime.backup = cast(MongoProtection, FakeBackup(store))
    runtime.remote = cast(RemoteClient, remote)

    def fail(*_):
        raise RuntimeError("receipt catalog unavailable")

    monkeypatch.setattr("tgdrive.transfers.uploads.ResumableUploads", fail)
    with pytest.raises(RuntimeError, match="receipt catalog unavailable"):
        await runtime.start()
    assert remote.closed and pool.stopped
    store.close.assert_called_once_with()
    assert runtime._owner_lock is None
    assert not runtime._mongo_acquired


@pytest.mark.asyncio
async def test_runtime_starts_once_and_stops_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = new_store(tmp_path / "drive.json")
    pool = FakePool()
    runtime = Runtime(settings(tmp_path), pool=cast(ClientPool, pool))
    backup = FakeBackup(store)
    remote = FakeRemote()
    runtime.backup = cast(MongoProtection, backup)
    runtime.remote = cast(RemoteClient, remote)
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

    store = new_store(tmp_path / "drive.json")
    pool = FakePool()
    runtime = Runtime(settings(tmp_path), pool=cast(ClientPool, pool))
    backup = FakeBackup(store)
    runtime.backup = cast(MongoProtection, backup)
    runtime.remote = cast(RemoteClient, FakeRemote())
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
        assert runtime.store is not None
        assert runtime.store.list_directory("/") == {}
        if conflict:
            assert runtime.replica.status()["error"]["code"] == "RECOVERY_KEY_CONFLICT"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_telegram_outage_keeps_local_metadata_available(tmp_path: Path) -> None:
    pool = FakePool(fail_start=True)
    config = settings(tmp_path)
    config.resolved_data_dir.mkdir(parents=True)
    store = new_store(config.resolved_data_dir / "drive")
    runtime = Runtime(config, pool=cast(ClientPool, pool))
    runtime.backup = cast(MongoProtection, FakeBackup(store))
    remote = FakeRemote()
    runtime.remote = cast(RemoteClient, remote)
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
    from tgdrive.replication.replica_activation import ActivationError

    getattr(mongo_backend, failure).side_effect = DatabaseUnavailable(
        "Synthetic MongoDB failure"
    )
    pool = FakePool()
    runtime = Runtime(settings(tmp_path), pool=cast(ClientPool, pool))
    runtime._activation_unavailable = ActivationError("Pending recovery activation")
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


async def test_activation_resource_error_survives_web_setup_and_recovery_startup(
    tmp_path, monkeypatch
):
    import threading

    from tgdrive.replication.replica import ChannelReplica
    from tgdrive.replication.replica_activation import MANIFEST, ActivationError
    from tgdrive.web.app import create_app

    config = settings(tmp_path)
    path = config.resolved_data_dir / MANIFEST
    path.parent.mkdir(parents=True)
    with path.open("wb") as record:
        record.write(b"preserve pending activation")
        record.truncate(64 * 1024**2 + 1)
    path.chmod(0o600)
    pool = FakePool()
    runtime = Runtime(config, pool=cast(ClientPool, pool))
    runtime.remote = cast(RemoteClient, FakeRemote())
    caller, failures = threading.get_ident(), []
    original = ChannelReplica._record_failure

    def record_failure(replica, error):
        assert threading.get_ident() != caller
        failures.append(error)
        return original(replica, error)

    monkeypatch.setattr(ChannelReplica, "_record_failure", record_failure)
    monkeypatch.setattr(ChannelReplica, "start", lambda _self: None)
    app = create_app(config, runtime=runtime)
    preserved = runtime._activation_unavailable
    assert isinstance(preserved, ActivationError)
    assert preserved.code == "ACTIVATION_RESOURCE_LIMIT"
    try:
        await runtime.start()
        assert len(failures) == 1
        assert failures[0].code == preserved.code
        assert str(failures[0]) == str(preserved)
        assert runtime.recovery_only and runtime.store is None
        assert runtime.transfers is None and not pool.started
        status = await asyncio.to_thread(runtime.replica.status)
        assert status["error"]["code"] == preserved.code
        assert status["error"]["detail"] == str(preserved)
        assert "64 MiB" in status["error"]["detail"]
        from tgdrive.settings.managed_settings import SettingsProblem

        for action in ("retry", "resume", "disable"):
            with pytest.raises(SettingsProblem) as failure:
                await runtime.replica.action(action)
            assert failure.value.code == "ACTIVATION_RESOURCE_LIMIT"
            assert failure.value.status == 409
            after = await asyncio.to_thread(runtime.replica.status)
            assert after["error"] == status["error"]
        assert path.stat().st_size == 64 * 1024**2 + 1
        assert not (path.parent / "recovery-archive").exists()
    finally:
        app.state.sessions.close()
        await runtime.stop()


@pytest.mark.parametrize("key", ["candidate", "job", "config", "status"])
async def test_oversize_legacy_journal_stops_startup_without_redecoding_or_erasure(
    tmp_path, monkeypatch, mongo_backend, key
):
    import copy
    import json
    import threading

    from tgdrive.replication.replica import ChannelReplica

    config = settings(tmp_path)
    raw = json.dumps([{}] * 65_537)
    mongo_backend.db.replica_state.insert_one({"_id": key, "length": len(raw)})
    mongo_backend.db.replica_chunks.insert_one(
        {"_id": key + "\x000", "key": key, "index": 0, "data": raw}
    )
    preserved = copy.deepcopy(
        (mongo_backend.db.replica_state.rows, mongo_backend.db.replica_chunks.rows)
    )
    find = mongo_backend.db.replica_chunks.find
    caller, reads = threading.get_ident(), []

    def counted_read(query, *args, **kwargs):
        if query.get("key") == key:
            assert threading.get_ident() != caller
            reads.append(query)
        return find(query, *args, **kwargs)

    monkeypatch.setattr(mongo_backend.db.replica_chunks, "find", counted_read)
    initialize = Mock(side_effect=AssertionError("Must preserve pending recovery"))
    monkeypatch.setattr("tgdrive.storage.mongo.initialize_if_empty", initialize)
    monkeypatch.setattr(ChannelReplica, "start", lambda _self: None)
    pool = FakePool()
    runtime = Runtime(config, pool=cast(ClientPool, pool))
    runtime.remote = cast(RemoteClient, FakeRemote())
    try:
        await runtime.start()
        assert runtime.recovery_only and runtime.store is None
        assert runtime.transfers is None and not pool.started
        for _ in range(2):
            status = await asyncio.to_thread(runtime.replica.status)
            assert status["error"]["code"] == "ACTIVATION_RESOURCE_LIMIT"
            assert "preserv" in status["error"]["action"].lower()
        assert len(reads) == 1
        assert preserved == (
            mongo_backend.db.replica_state.rows,
            mongo_backend.db.replica_chunks.rows,
        )
        initialize.assert_not_called()
    finally:
        await runtime.stop()


@pytest.mark.parametrize("filename", ["drive.json", "tgdrive-legacy.tgdb"])
async def test_known_legacy_pin_stops_fresh_initialization(
    tmp_path, monkeypatch, mongo_backend, filename
):
    from telethon.tl.types import DocumentAttributeFilename

    from tgdrive.replication.replica import ChannelReplica
    from tgdrive.replication.replica_transport import ReplicaTransport

    class LegacyPin:
        async def start(self):
            pass

        async def close(self):
            pass

        async def inspect(self, *_args, **_kwargs):
            return {}

        async def pinned_message(self, _channel):
            return 77

        async def download(self, *_args):
            ReplicaTransport._capsule(
                SimpleNamespace(
                    size=123,
                    attributes=[DocumentAttributeFilename(filename)],
                )
            )
            raise AssertionError("Known legacy content must not be imported")

    initialize = Mock(side_effect=AssertionError("Must preserve the legacy drive"))
    monkeypatch.setattr("tgdrive.storage.mongo.initialize_if_empty", initialize)
    monkeypatch.setattr("tgdrive.storage.mongo.database_is_empty", lambda *_: True)
    monkeypatch.setattr("tgdrive.storage.upgrade.upgrade", lambda *_: None)
    constructor = ChannelReplica.__init__

    def replica_with_legacy_transport(self, *args, **kwargs):
        constructor(self, *args, **kwargs, transport_factory=lambda *_: LegacyPin())

    monkeypatch.setattr(ChannelReplica, "__init__", replica_with_legacy_transport)
    monkeypatch.setattr(ChannelReplica, "start", lambda _self: None)
    pool = FakePool()
    runtime = Runtime(settings(tmp_path), pool=cast(ClientPool, pool))
    runtime.remote = cast(RemoteClient, FakeRemote())
    try:
        await runtime.start()
        assert runtime.recovery_only and runtime.store is None
        assert runtime.transfers is None and not pool.started
        status = await asyncio.to_thread(runtime.replica.status)
        assert status["error"]["code"] == "LEGACY_BACKUP_REQUIRES_UPGRADE"
        initialize.assert_not_called()
    finally:
        await runtime.stop()


def test_runtime_rejects_dangerous_cache_root(tmp_path: Path) -> None:
    root = Path(tmp_path.anchor)
    runtime = Runtime(
        settings(tmp_path, CACHE_DIR=root), pool=cast(ClientPool, FakePool())
    )

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
        settings(tmp_path, CACHE_DIR=cache, DATA_DIR=data),
        pool=cast(ClientPool, FakePool()),
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
    runtime = Runtime(
        settings(tmp_path, CACHE_DIR=cache), pool=cast(ClientPool, FakePool())
    )

    with pytest.raises(ValueError, match="cleanup paths"):
        runtime._prepare_runtime_directories()

    assert removed == []
    assert outside.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnecting", [False, True])
async def test_bot_mode_shares_channel_send_pacing_and_flood_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reconnecting: bool
) -> None:
    store = new_store(tmp_path / "drive.json")

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
    runtime = Runtime(settings(tmp_path), pool=cast(ClientPool, pool))
    runtime.backup = cast(MongoProtection, FakeBackup(store))
    runtime.remote = cast(RemoteClient, FakeRemote())
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
        assert runtime.bot_mode is not None
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
    runtime = Runtime(settings(tmp_path), pool=cast(ClientPool, pool))
    store = new_store(tmp_path / "drive")
    runtime.store = store
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
        store.close()


@pytest.mark.asyncio
async def test_empty_database_with_pinned_backup_opens_restore_not_a_new_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tgdrive.replication.replica import ChannelReplica

    monkeypatch.setattr("tgdrive.storage.mongo.database_is_empty", lambda *_: True)
    created = []
    monkeypatch.setattr(
        "tgdrive.storage.mongo.initialize_if_empty", lambda *_: created.append(1)
    )

    async def found(self):
        return True

    monkeypatch.setattr(ChannelReplica, "discover_backup", found)
    monkeypatch.setattr(ChannelReplica, "start", lambda self: None)
    runtime = Runtime(settings(tmp_path), pool=cast(ClientPool, FakePool()))
    try:
        await runtime.start()
        assert runtime.recovery_only and runtime.store is None
        assert created == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_empty_database_without_backup_creates_a_new_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tgdrive.replication.replica import ChannelReplica

    monkeypatch.setattr("tgdrive.storage.mongo.database_is_empty", lambda *_: True)

    async def none(self):
        return False

    monkeypatch.setattr(ChannelReplica, "discover_backup", none)
    runtime = Runtime(settings(tmp_path), pool=cast(ClientPool, FakePool()))
    assert await runtime._offer_backup_restore() is False
    assert not getattr(runtime, "recovery_only", False)
