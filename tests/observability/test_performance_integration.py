from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from tests.core.test_runtime import FakePool
from tests.observability.test_performance_preferences import (
    mongo_backend as mongo_backend,
)
from tests.observability.test_performance_preferences import (
    runtime as performance_runtime,
)
from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tgdrive.core.runtime import Runtime
from tgdrive.observability.performance_api import system_snapshot
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.settings.performance_preferences import PerformanceSettings
from tgdrive.storage.store import DriveStore
from tgdrive.web.app import create_app

pytestmark = pytest.mark.usefixtures("mongo_backend")


@pytest.mark.asyncio
async def test_failed_live_policy_apply_restores_saved_policy_and_revision(
    tmp_path, mongo_backend
):
    settings = make_settings(tmp_path)
    runtime = performance_runtime(mongo_backend)
    service = PerformanceSettings(settings, runtime)
    before = await service.snapshot()
    runtime.pool.set_media_per_bot = AsyncMock(side_effect=[OSError("temporary"), None])
    try:
        with pytest.raises(SettingsProblem) as failed:
            await service.save({"timezone": "UTC", "cache_memory_bytes": 0})
        assert failed.value.status == 503
        assert "restored" in str(failed.value).lower()
        assert (await service.snapshot())["revision"] == before["revision"]
        assert settings.timezone == before["timezone"]
        assert settings.cache_memory_bytes == before["cache_memory_bytes"]
        saved = mongo_backend.db.settings.find_one({"_id": "performance-preferences"})[
            "document"
        ]
        assert saved["timezone"] == before["timezone"]
        assert (
            runtime.preloads.cache.policy.memory_max_bytes
            == before["cache_memory_bytes"]
        )
    finally:
        await runtime.preloads.close()


@pytest.mark.asyncio
async def test_existing_manager_applies_cache_policy_only_once(tmp_path, mongo_backend):
    runtime = performance_runtime(mongo_backend)
    service = PerformanceSettings(make_settings(tmp_path), runtime)
    await service.initialize()
    cache = runtime.preloads.cache
    reconfigure = AsyncMock(wraps=cache.reconfigure)
    cache.reconfigure = reconfigure
    try:
        await service.save({"auto_preload": False})
        assert reconfigure.await_count == 1
    finally:
        await runtime.preloads.close()


@pytest.mark.asyncio
async def test_zero_tier_settings_initialize_and_report_disabled_cache(
    tmp_path, mongo_backend
):
    settings = make_settings(tmp_path)
    settings.cache_memory_bytes = settings.cache_disk_bytes = 0
    runtime = performance_runtime(mongo_backend)
    service = PerformanceSettings(settings, runtime)
    try:
        await service.initialize()
        snapshot = await runtime.preloads.snapshot()
        assert snapshot["cache"]["memory"]["bytes"] == 0
        assert snapshot["cache"]["memory"]["effective_max_bytes"] == 0
        assert snapshot["cache"]["disk"]["max_bytes"] == 0
        assert snapshot["cache"]["files"] == []
    finally:
        if hasattr(runtime, "preloads"):
            await runtime.preloads.close()


def test_lazy_resource_api_without_telegram_runtime_is_controlled(
    tmp_path, mongo_backend
):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    runtime = FakeRuntime(store)
    runtime.mongo = mongo_backend
    app = create_app(settings, runtime=runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            login(client)
            assert client.get("/api/v1/settings/performance").status_code == 200
            performance = client.get("/api/v1/performance")
            assert performance.status_code == 200
            assert performance.json()["cache"]["disk"]["available"] is False
            assert performance.json()["cache"]["files"] == []
            assert client.get("/api/v1/preloads").status_code == 503
            assert client.post("/api/v1/preloads/clear", json={}).status_code == 503
    finally:
        runtime.transfers.registry.close()
        store.close()


def test_resource_snapshot_survives_unavailable_data_directory(tmp_path):
    settings = make_settings(tmp_path)
    state = system_snapshot(settings)
    assert state["disk"]["available"] is False
    assert state["disk"]["free_bytes"] == 0
    assert "memory" in state and "load_average" in state


@pytest.mark.asyncio
async def test_media_cleanup_failure_still_stops_pool_and_releases_owner(tmp_path):
    pool = FakePool()
    runtime = Runtime(make_settings(tmp_path), pool=pool)
    runtime.preloads = SimpleNamespace(
        close=AsyncMock(side_effect=OSError("cache failed"))
    )
    owner = SimpleNamespace(close=Mock())
    runtime._owner_lock = owner
    await runtime.stop()
    assert pool.stopped
    owner.close.assert_called_once()
    assert runtime._owner_lock is None
    runtime.preloads.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_manager_owns_cache_close_without_double_close(tmp_path):
    runtime = Runtime(make_settings(tmp_path), pool=FakePool())
    cache = SimpleNamespace(close=AsyncMock())
    runtime.pool._media_read_cache = cache
    runtime.preloads = SimpleNamespace(close=AsyncMock(side_effect=cache.close))
    await runtime.stop()
    runtime.preloads.close.assert_awaited_once()
    cache.close.assert_awaited_once()


def test_prepare_rejects_catalog_generation_change_before_origin_work(
    tmp_path, monkeypatch, mongo_backend
):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    file_id = store.add_file("/", "movie.mp4", 77, 1024)["id"]
    runtime = FakeRuntime(store)
    runtime.mongo = mongo_backend
    runtime.pool = performance_runtime().pool
    app = create_app(settings, runtime=runtime)
    original = store.get_file
    calls = 0

    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        value = original(*args, **kwargs)
        return value if calls == 1 else value | {"message_id": 88}

    monkeypatch.setattr(store, "get_file", changed)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            login(client)
            result = client.post("/api/v1/preloads", json={"path": "/" + file_id})
            assert result.status_code == 409
            assert "changed" in result.json()["detail"].lower()
            assert not runtime.preloads._jobs
    finally:
        runtime.transfers.registry.close()
        store.close()
