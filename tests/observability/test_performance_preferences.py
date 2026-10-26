import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.core.config import Settings
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.settings.performance_preferences import (
    PerformancePreferences,
    PerformanceSettings,
)
from tgdrive.storage.store import DriveStore
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
    numeric = {key: value for key, value in defaults.items() if type(value) is int}
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
    after = await service.save(
        {"cache_disk_bytes": 16 * 1024**3, "timezone": "UTC"},
        expected=before["revision"],
    )
    assert after["timezone"] == "UTC"
    with pytest.raises(SettingsProblem) as conflict:
        await service.save({"timezone": "Asia/Kolkata"}, expected=before["revision"])
    assert conflict.value.status == 409

    def failed(value):
        raise OSError("synthetic")

    monkeypatch.setattr(service, "_write", failed)
    with pytest.raises(SettingsProblem):
        await service.save({"cache_disk_bytes": 1})
    assert (await service.snapshot())["cache_disk_bytes"] == 16 * 1024**3
    other = runtime(mongo_backend)
    restarted = PerformanceSettings(settings, other)
    assert (await restarted.snapshot())["timezone"] == "UTC"
    await state.preloads.close()
    await other.preloads.close()


def test_owner_api_resource_policy_and_share_capability(tmp_path, mongo_backend):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.sqlite3")
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
            assert result.json()["defaults"] == PerformancePreferences().model_dump()
            assert result.json()["documentation_url"].endswith("/docs/configuration.md")
            assert result.headers["cache-control"] == "no-store"
            for changes in (
                {"cache_disk_bytes": -1},
                {"cache_memory_bytes": True},
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
    store = DriveStore.create(tmp_path / "drive.sqlite3")
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


async def test_defaults_are_built_in_and_invalid_updates_never_change_saved_policy(
    tmp_path,
    mongo_backend,
):
    settings = make_settings(tmp_path)
    settings.cache_memory_bytes = 512 * 1024**2
    state = runtime(mongo_backend)
    service = PerformanceSettings(settings, state)
    try:
        before = await service.snapshot()
        assert before["cache_memory_bytes"] == 512 * 1024**2
        assert before["defaults"]["cache_memory_bytes"] == 256 * 1024**2
        from pydantic import ValidationError

        for changes in [
            {"cache_memory_bytes": 9 * 1024**3, "timezone": "UTC"},
            {"telegram_upload_pipeline": 17},
            {"telegram_request_limit": 33},
            {"telegram_parts_per_second": 129},
            {"telegram_media_per_bot": 17},
            {"telegram_max_queue": 4097},
            {"preload_max_jobs": 0},
            {"upload_split_mib": 2001},
            {"cache_ttl_seconds": 0},
            {"cache_memory_bytes": 1.5},
            {"auto_preload": "false"},
            {"defaults": {"cache_memory_bytes": 1}},
        ]:
            with pytest.raises(ValidationError):
                await service.save(changes, expected=before["revision"])
            assert await service.snapshot() == before
        with pytest.raises(SettingsProblem) as too_low:
            await service.save({"cache_min_free_bytes": 0}, expected=before["revision"])
        assert too_low.value.status == 422
        assert await service.snapshot() == before
        disabled = await service.save(
            {"cache_disk_bytes": 0, "cache_min_free_bytes": 0}
        )
        assert disabled["cache_disk_bytes"] == 0
        restored = await service.save(before["defaults"], expected=disabled["revision"])
        assert all(restored[key] == value for key, value in before["defaults"].items())
    finally:
        await state.preloads.close()


async def test_url_concurrency_applies_live_and_persists(tmp_path, mongo_backend):
    from tgdrive.transfers.remote_scheduler import URLDownloadScheduler

    settings = make_settings(tmp_path)
    state = runtime(mongo_backend)
    state.remote = SimpleNamespace(scheduler=URLDownloadScheduler())
    service = PerformanceSettings(settings, state)
    before = await service.snapshot()
    assert before["remote_download_concurrency"] == 2
    for invalid in (0, 9, True, 2.5):
        with pytest.raises(ValueError):
            await service.save({"remote_download_concurrency": invalid})
    await service.save({"remote_download_concurrency": 8})
    assert state.remote.scheduler.full_speed_limit == 8
    other = runtime(mongo_backend)
    restarted = PerformanceSettings(settings, other)
    assert (await restarted.snapshot())["remote_download_concurrency"] == 8
    await state.preloads.close()
    await other.preloads.close()
