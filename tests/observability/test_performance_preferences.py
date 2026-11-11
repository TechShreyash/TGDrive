import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import new_store, register_test_backend
from tgdrive.core.config import Settings
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.settings.performance_preferences import (
    PerformancePreferences,
    PerformanceSettings,
)
from tgdrive.web.app import create_app


def test_operator_default_hints_match_runtime_defaults():
    """Dashboard hints and typed defaults must describe the same live policy."""
    root = Path(__file__).resolve().parents[2]
    defaults = PerformancePreferences().model_dump()
    definitions = (
        (root / "website/src/features/performance/performanceApi.ts")
        .read_text()
        .split("export type PerformanceDraft", 1)[0]
    )
    displayed = dict(
        re.findall(r'key: "([a-z_]+)"[\s\S]*?appDefault: (\d+)', definitions)
    )
    from tgdrive.core.governor import MANAGED

    # Only owner choices are shown with defaults; the governor sets the rest.
    numeric = {
        key: value
        for key, value in defaults.items()
        if type(value) is int and key not in MANAGED
    }
    assert displayed.keys() == numeric.keys()
    units = {
        "cache_memory_bytes": 1024**2,
        "cache_disk_bytes": 1024**3,
        "cache_min_free_bytes": 1024**3,
        "cache_ttl_seconds": 3600,
        "max_file_size": 1024**3,
        "staging_budget_bytes": 1024**3,
        "min_free_bytes": 1024**3,
    }
    for key, expected in defaults.items():
        if key == "max_file_size":
            assert Settings.model_fields[key].default is None
        else:
            assert Settings.model_fields[key].default == expected
        if key in numeric:
            assert int(displayed[key]) * units.get(key, 1) == expected


@pytest.fixture
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path / "data")


def runtime(backend=None):
    return SimpleNamespace(
        mongo=backend,
        pool=SimpleNamespace(
            _slots=[], set_media_per_bot=AsyncMock(), set_media_queue_limit=AsyncMock()
        ),
    )


async def test_policy_persists_conflicts_and_failed_write_is_not_applied(
    tmp_path, monkeypatch, mongo_backend
):
    settings = make_settings(tmp_path)
    state = runtime(mongo_backend)
    service = PerformanceSettings(settings, state)
    before = await service.snapshot()
    after = await service.save({"timezone": "UTC"}, expected=before["revision"])
    assert after["timezone"] == "UTC"
    with pytest.raises(SettingsProblem) as conflict:
        await service.save({"timezone": "Asia/Kolkata"}, expected=before["revision"])
    assert conflict.value.status == 409

    def failed(value):
        raise OSError("synthetic")

    monkeypatch.setattr(service, "_write", failed)
    with pytest.raises(SettingsProblem):
        await service.save({"auto_preload": False})
    assert (await service.snapshot())["auto_preload"] is True
    other = runtime(mongo_backend)
    restarted = PerformanceSettings(settings, other)
    assert (await restarted.snapshot())["timezone"] == "UTC"
    await state.preloads.close()
    await other.preloads.close()


def test_owner_api_resource_policy_and_share_capability(tmp_path, mongo_backend):
    settings = make_settings(tmp_path)
    store = new_store(tmp_path / "drive")
    item = store.add_file("/", "hello.txt", 123, 12)["id"]
    app_runtime = FakeRuntime(store)
    app_runtime.mongo = mongo_backend
    app_runtime.pool = runtime().pool
    app = create_app(settings, runtime=app_runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            url = "/api/v1/settings/performance"
            assert client.get(url).status_code == 401
            assert client.post("/api/v1/preloads/clear", json={}).status_code == 401
            login(client)
            result = client.get(url)
            assert result.status_code == 200
            assert result.json()["timezone"] == "Asia/Kolkata"
            assert "cache_disk_bytes" not in result.json()
            assert set(result.json()["defaults"]) == {
                "timezone",
                "auto_preload",
                "upload_split_mib",
                "cache_disk_limit_bytes",
                "cache_memory_limit_bytes",
                "staging_limit_bytes",
                "protected_free_bytes",
            }
            assert result.json()["documentation_url"].endswith("/docs/configuration.md")
            assert result.headers["cache-control"] == "no-store"
            for changes in (
                {"cache_disk_bytes": 1},
                {"unknown": 4},
                {"timezone": "INVALID"},
            ):
                assert client.patch(url, json=changes).status_code == 422
            assert (
                client.patch(
                    url,
                    json={"auto_preload": False},
                    headers={"If-Match": result.headers["etag"]},
                ).status_code
                == 200
            )
            assert (
                client.patch(
                    url,
                    json={"auto_preload": True},
                    headers={"If-Match": result.headers["etag"]},
                ).status_code
                == 409
            )
            assert client.get("/api/v1/preloads").json()["cache"]["files"] == []
            # HEAD has no provider work; revoked links fail even for owner.
            head = client.head("/file", params={"path": "/" + item})
            assert head.status_code == 200
            assert head.headers["content-length"] == "12"
            assert head.content == b""
            ranged = client.head(
                "/file", params={"path": "/" + item}, headers={"Range": "bytes=4-7"}
            )
            assert ranged.status_code == 206
            assert ranged.headers["content-range"] == "bytes 4-7/12"
            assert (
                client.head(
                    "/file", params={"path": "/" + item}, headers={"Range": "bytes=50-"}
                ).status_code
                == 416
            )
            copied = client.post(
                "/api/v1/download-links", json={"path": "/" + item}
            ).json()
            assert "auth=" in copied["url"]
            assert len(store.list_shares()) == 1
            assert (
                client.post("/api/v1/download-links", json={"path": "/" + item}).json()[
                    "url"
                ]
                == copied["url"]
            )
            client.request("DELETE", "/api/v1/shares", json={"path": "/" + item})
            assert client.head(copied["url"]).status_code == 403
    finally:
        app_runtime.transfers.registry.close()
        store.close()


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/v1/settings/performance", None),
        ("PATCH", "/api/v1/settings/performance", {"cache_disk_bytes": 0}),
        ("GET", "/api/v1/preloads", None),
        ("POST", "/api/v1/preloads", {"path": "/file-placeholder"}),
        ("POST", "/api/v1/preloads/clear", {}),
        ("POST", "/api/v1/preloads/job-placeholder/cancel", {}),
        ("GET", "/api/v1/performance", None),
    ],
)
def test_valid_public_share_cannot_inspect_or_control_operator_resources(
    tmp_path, method, path, body, mongo_backend
):
    settings = make_settings(tmp_path)
    store = new_store(tmp_path / "drive")
    item = store.add_file("/", "public.txt", 123, 12)["id"]
    token = store.create_share("/" + item)
    assert store.authorize_file("/" + item, token)
    state = FakeRuntime(store)
    state.mongo = mongo_backend
    app = create_app(settings, runtime=state)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            response = client.request(method, path, params={"auth": token}, json=body)
            assert response.status_code == 401
            assert not hasattr(state, "performance_settings")
    finally:
        state.transfers.registry.close()
        store.close()


async def test_owner_saves_choices_but_resource_values_are_automatic(
    tmp_path,
    mongo_backend,
):
    settings = make_settings(tmp_path)
    state = runtime(mongo_backend)
    service = PerformanceSettings(settings, state)
    try:
        before = await service.snapshot()
        from pydantic import ValidationError

        for changes in [
            {"timezone": "Not/AZone"},
            {"upload_split_mib": 2001},
            {"auto_preload": "false"},
            {"defaults": {"timezone": "UTC"}},
        ]:
            with pytest.raises(ValidationError):
                await service.save(changes, expected=before["revision"])
            assert await service.snapshot() == before
        for key in ("cache_memory_bytes", "telegram_request_limit", "max_file_size"):
            with pytest.raises(SettingsProblem, match="automatically") as managed:
                await service.save({key: 1}, expected=before["revision"])
            assert managed.value.status == 422
        assert await service.snapshot() == before
        saved = await service.save({"auto_preload": False, "upload_split_mib": 500})
        assert saved["auto_preload"] is False and saved["upload_split_mib"] == 500
        document = mongo_backend.db.settings.find_one(
            {"_id": "performance-preferences"}
        )["document"]
        assert "cache_memory_bytes" not in document
    finally:
        await state.preloads.close()


async def test_resource_updates_preserve_provider_feedback_and_owner_save_budgets(
    tmp_path, mongo_backend
):
    from tgdrive.media.media_scheduler import MediaScheduler

    settings = make_settings(tmp_path)
    state = runtime(mongo_backend)
    scheduler = MediaScheduler(limit=8, rate=48)

    def configure_transport(**values):
        scheduler.configure(
            limit=values["request_limit"], rate=values["parts_per_second"]
        )

    client = SimpleNamespace(configure_transport=Mock(side_effect=configure_transport))
    state.pool._slots = [SimpleNamespace(client=client)]
    state.remote = SimpleNamespace(scheduler=SimpleNamespace(configure=AsyncMock()))
    service = PerformanceSettings(settings, state)
    await service.initialize()
    try:
        revision = service.etag()
        await scheduler.flood(0)
        assert scheduler.effective_limit == 4 and scheduler.effective_rate == 24
        client.configure_transport.reset_mock()
        state.remote.scheduler.configure.reset_mock()
        state.pool.set_media_per_bot.reset_mock()
        state.pool.set_media_queue_limit.reset_mock()
        state.preloads.reconfigure = AsyncMock(wraps=state.preloads.reconfigure)
        policy = service.value.model_copy(
            update={
                "cache_disk_bytes": 2 * 1024**3,
                "staging_budget_bytes": 6 * 1024**3,
                "max_file_size": 6 * 1024**3,
            }
        )
        await service._apply(policy)
        assert scheduler.effective_limit == 4 and scheduler.effective_rate == 24
        assert service.etag() == revision
        assert service.value == policy
        await service.save({"timezone": "UTC"}, expected=revision)
        assert (
            service.value.staging_budget_bytes
            == settings.staging_budget_bytes
            == 6 * 1024**3
        )
        assert service.value.max_file_size == settings.max_file_size == 6 * 1024**3
        client.configure_transport.assert_not_called()
        state.remote.scheduler.configure.assert_not_awaited()
        state.pool.set_media_per_bot.assert_not_awaited()
        state.pool.set_media_queue_limit.assert_not_awaited()
        state.preloads.reconfigure.assert_awaited_once()
    finally:
        await state.preloads.close()


async def test_governor_retries_resource_policy_after_partial_apply_failure(
    tmp_path, mongo_backend, monkeypatch
):
    from tgdrive.core import governor

    settings = make_settings(tmp_path)
    state = runtime(mongo_backend)
    service = PerformanceSettings(settings, state)
    state.performance_settings = service
    await service.initialize()
    previous = service.value
    original = state.preloads.reconfigure
    attempts = 0

    async def reconfigure(policy):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary cache failure")
        await original(policy)

    state.preloads.reconfigure = AsyncMock(side_effect=reconfigure)
    monkeypatch.setattr(
        governor,
        "measure",
        lambda *_: {
            "memory_available": None,
            "cpus": 1,
            "disk_total": 100 * governor.GIB,
            "disk_free": 50 * governor.GIB,
            "cache_disk_total": 20 * governor.GIB,
            "cache_disk_free": 5 * governor.GIB,
        },
    )
    item = governor.Governor(settings, state)
    try:
        with pytest.raises(OSError, match="temporary cache failure"):
            await item.tick()
        assert settings.cache_disk_bytes == 2 * governor.GIB
        assert service.value == previous
        await item.tick()
        assert state.preloads.reconfigure.await_count == 2
        assert service.value.cache_disk_bytes == settings.cache_disk_bytes
        assert service.value.staging_budget_bytes == settings.staging_budget_bytes
        assert state.preloads.policy.disk_max_bytes == 2 * governor.GIB
    finally:
        await state.preloads.close()


@pytest.mark.parametrize("boundary", ["write", "apply", "rollback"])
async def test_cancelled_policy_mutation_serializes_commit_apply_and_rollback(
    tmp_path, mongo_backend, monkeypatch, boundary
):
    import asyncio
    import threading

    settings = make_settings(tmp_path)
    state = runtime(mongo_backend)
    service = PerformanceSettings(settings, state)
    await service.initialize()
    previous = service.value.timezone
    entered, release, finished = (threading.Event() for _ in range(3))
    original_write, original_apply = service._write, service._apply

    def wait():
        entered.set()
        assert release.wait(5), "Performance settings worker was not released"
        finished.set()

    def write(value):
        if (boundary == "write" and value.timezone == "UTC") or (
            boundary == "rollback" and value.timezone == previous
        ):
            wait()
        return original_write(value)

    async def apply(value):
        if value.timezone == "UTC":
            if boundary == "apply":
                await asyncio.to_thread(wait)
            elif boundary == "rollback":
                raise RuntimeError("Synthetic live-apply failure")
        await original_apply(value)

    monkeypatch.setattr(service, "_write", write)
    monkeypatch.setattr(service, "_apply", apply)
    first = asyncio.create_task(service.save({"timezone": "UTC"}))
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        following = asyncio.create_task(service.save({"timezone": "Europe/London"}))
        await asyncio.sleep(0)
        assert not first.done() and not following.done()
        assert service._lock.locked()
        release.set()
        with pytest.raises(
            SettingsProblem if boundary == "rollback" else asyncio.CancelledError
        ):
            await first
        await following
        saved = mongo_backend.db.settings.find_one({"_id": "performance-preferences"})
        assert saved is not None
        assert (
            saved["document"]["timezone"]
            == service.value.timezone
            == settings.timezone
            == "Europe/London"
        )
    finally:
        release.set()
        await asyncio.gather(
            first, *([following] if following else []), return_exceptions=True
        )
        assert await asyncio.to_thread(finished.wait, 2)
        await state.preloads.close()


async def test_policy_loading_stays_cancellable_before_commit(
    tmp_path, mongo_backend, monkeypatch
):
    import asyncio
    import threading

    service = PerformanceSettings(make_settings(tmp_path), runtime(mongo_backend))
    entered, release, finished = (threading.Event() for _ in range(3))
    original = service._read

    def read():
        entered.set()
        assert release.wait(5)
        try:
            return original()
        finally:
            finished.set()

    monkeypatch.setattr(service, "_read", read)
    task = asyncio.create_task(service.save({"timezone": "UTC"}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not service._lock.locked()
        assert (
            mongo_backend.db.settings.find_one({"_id": "performance-preferences"})
            is None
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 2)


async def test_owner_resource_limits_persist_apply_and_restart(tmp_path, mongo_backend):
    settings = make_settings(tmp_path)
    state = runtime(mongo_backend)
    service = PerformanceSettings(settings, state)
    restarted = None
    try:
        before = await service.snapshot()
        assert before["auto_resource_limits"] == {
            "cache_disk_limit_bytes": 10 * 1024**3,
            "cache_memory_limit_bytes": 256 * 1024**2,
            "staging_limit_bytes": 16 * 1024**3,
        }
        assert before["auto_free_space_reserve"] == {
            "minimum_bytes": 1024**3,
            "fraction": 0.05,
        }
        limits = dict(
            cache_disk_limit_bytes=0,
            cache_memory_limit_bytes=0,
            staging_limit_bytes=12345,
            protected_free_bytes=0,
        )
        saved = await service.save(limits, expected=before["revision"])
        assert all(saved[key] == value for key, value in limits.items())
        assert saved["revision"] != before["revision"]
        assert all(saved["defaults"][key] is None for key in limits)
        assert settings.cache_disk_bytes == settings.cache_memory_bytes == 0
        assert settings.staging_budget_bytes == settings.max_file_size == 12345
        assert settings.min_free_bytes == settings.cache_min_free_bytes == 0
        assert state.preloads.policy.disk_max_bytes == 0
        persisted = mongo_backend.db.settings.find_one(
            {"_id": "performance-preferences"}
        )["document"]
        assert all(persisted[key] == value for key, value in limits.items())
        assert "cache_disk_bytes" not in persisted
        other = runtime(mongo_backend)
        restarted = PerformanceSettings(make_settings(tmp_path), other)
        restored = await restarted.snapshot()
        assert all(restored[key] == value for key, value in limits.items())
        assert restarted.settings.cache_disk_bytes == 0
        assert restarted.settings.staging_budget_bytes == 12345
    finally:
        await state.preloads.close()
        if restarted and hasattr(restarted.runtime, "preloads"):
            await restarted.runtime.preloads.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"cache_disk_limit_bytes": -1},
        {"cache_memory_limit_bytes": True},
        {"staging_limit_bytes": 0},
        {"protected_free_bytes": -1},
        {"cache_disk_limit_bytes": 2**53},
        {"staging_limit_bytes": "100"},
    ],
)
def test_resource_limit_validation_is_strict(changes):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PerformancePreferences.model_validate(changes)


async def test_governor_waiting_for_policy_lock_uses_latest_owner_ceiling(
    tmp_path, mongo_backend, monkeypatch
):
    import asyncio

    from tgdrive.core import governor

    settings = make_settings(tmp_path)
    state = runtime(mongo_backend)
    service = PerformanceSettings(settings, state)
    state.performance_settings = service
    await service.initialize()
    item = governor.Governor(settings, state)
    monkeypatch.setattr(
        governor,
        "measure",
        lambda *_: {
            "memory_available": 8 * governor.GIB,
            "cpus": 1,
            "disk_total": 100 * governor.GIB,
            "disk_free": 50 * governor.GIB,
        },
    )
    waiting = asyncio.Event()

    class ObservedLock(asyncio.Lock):
        async def __aenter__(self):
            waiting.set()
            return await super().__aenter__()

    service._lock = ObservedLock()
    await service._lock.acquire()
    tick = asyncio.create_task(item.tick())
    try:
        async with asyncio.timeout(2):
            await waiting.wait()
        # The tick has measured but must not decide/apply until the saver releases
        # this same lock. Mimic the saver's apply before that release.
        await service._apply(
            service.value.model_copy(
                update={
                    "cache_disk_limit_bytes": 0,
                    "cache_memory_limit_bytes": 1024,
                    "staging_limit_bytes": 2048,
                }
            )
        )
    finally:
        service._lock.release()
    try:
        await tick
        assert settings.cache_disk_bytes == 0
        assert settings.cache_memory_bytes <= 1024
        assert settings.staging_budget_bytes <= 2048
        assert item.state is not None
        assert item.state["values"]["cache_disk_bytes"] == 0
    finally:
        await state.preloads.close()


async def test_automatic_reserve_restores_each_filesystem_floor(
    tmp_path, mongo_backend, monkeypatch
):
    from tgdrive.core import governor

    state = runtime(mongo_backend)
    service = PerformanceSettings(make_settings(tmp_path), state)
    monkeypatch.setattr(
        governor,
        "measure",
        lambda *_: {
            "disk_total": 100 * governor.GIB,
            "cache_disk_total": 20 * governor.GIB,
        },
    )
    try:
        await service.save({"protected_free_bytes": 0})
        assert service.settings.min_free_bytes == 0
        await service.save({"protected_free_bytes": None})
        assert service.settings.min_free_bytes == 5 * governor.GIB
        assert service.settings.cache_min_free_bytes == governor.GIB
    finally:
        await state.preloads.close()


async def test_legacy_effective_resource_values_never_seed_owner_ceilings(
    tmp_path, mongo_backend
):
    mongo_backend.write(
        mongo_backend.db.settings,
        "replace_one",
        {"_id": "performance-preferences"},
        {
            "_id": "performance-preferences",
            "document": {
                "cache_disk_bytes": 400 * 1024**3,
                "cache_memory_bytes": 2 * 1024**3,
                "staging_budget_bytes": 400 * 1024**3,
                "timezone": "UTC",
            },
        },
        upsert=True,
    )
    settings = make_settings(tmp_path)
    settings.cache_disk_bytes = 400 * 1024**3
    state = runtime(mongo_backend)
    service = PerformanceSettings(settings, state)
    try:
        snapshot = await service.snapshot()
        assert snapshot["cache_disk_limit_bytes"] is None
        assert snapshot["cache_memory_limit_bytes"] is None
        assert snapshot["staging_limit_bytes"] is None
        assert snapshot["timezone"] == "UTC"
        assert settings.cache_disk_bytes == 10 * 1024**3
    finally:
        await state.preloads.close()
