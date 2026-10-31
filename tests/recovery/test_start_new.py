"""Start new drive erases only drive data and swaps in a fresh runtime."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tgdrive.recovery.start_new as start_new
from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login
from tests.support.mongo_fixtures import FakeMongoBackend
from tgdrive.recovery.start_new import erase_database, erase_local

ORIGIN = {"Origin": "https://testserver"}


def test_erase_keeps_ownership_lease_secrets_and_lock(tmp_path):
    backend = FakeMongoBackend()
    backend.db.list_collection_names = lambda: ["catalog", "nodes", "runtime"]
    for name in ("catalog", "nodes", "runtime"):
        backend.db[name].insert_one({"_id": name})
    assert erase_database(backend) == ["catalog", "nodes"]
    assert backend.db.catalog.find_one({}) is None
    assert backend.db.runtime.find_one({}) == {"_id": "runtime"}

    data, cache = tmp_path / "data", tmp_path / "cache"
    for directory in (data / "replica-bundles", cache / "torrents" / "job"):
        directory.mkdir(parents=True)
    for name in ("deployment-secrets.json", ".instance.lock", "mongodb-drive.json"):
        (data / name).write_text("x")
    (cache / "uploads.bin").write_text("x")
    settings = type("S", (), {"resolved_data_dir": data, "resolved_cache_dir": cache})()
    erase_local(settings)
    assert sorted(p.name for p in data.iterdir()) == [
        ".instance.lock",
        "deployment-secrets.json",
    ]
    assert list(cache.iterdir()) == []


def test_start_new_requires_password_and_confirmation(app_client, monkeypatch):
    client, _store, runtime, settings = app_client
    calls = []
    monkeypatch.setattr(start_new, "unpin_backups", AsyncMock(return_value=True))
    monkeypatch.setattr(
        start_new, "erase_database", lambda b: calls.append("db") or ["catalog"]
    )
    monkeypatch.setattr(start_new, "erase_local", lambda s: calls.append("local"))
    started = []

    class Fresh:
        ready = True

        def __init__(self, configured):
            assert configured is settings

        async def start(self):
            started.append(True)

        async def stop(self):
            pass

    monkeypatch.setattr("tgdrive.core.runtime.Runtime", Fresh)
    runtime.mongo.acquire = runtime.mongo.release = lambda: None
    route = "/api/v1/start-new"
    body = {"confirm": "START NEW", "password": "correct horse battery staple"}
    assert client.post(route, json=body, headers=ORIGIN).status_code == 401
    login(client)
    wrong = body | {"password": "wrong password"}
    assert client.post(route, json=wrong, headers=ORIGIN).status_code == 403
    assert (
        client.post(route, json=body | {"confirm": "yes"}, headers=ORIGIN).status_code
        == 422
    )
    assert calls == []
    response = client.post(route, json=body, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "unpinned": True, "ready": True}
    assert calls == ["db", "local"] and started == [True]
    assert runtime.stopped
    assert isinstance(client.app.state.runtime, Fresh)


@pytest.mark.parametrize("demo", [True])
def test_start_new_is_disabled_in_demo_mode(app_client, demo):
    client, _store, _runtime, settings = app_client
    settings.demo_mode = demo
    login(client)
    response = client.post(
        "/api/v1/start-new",
        json={"confirm": "START NEW", "password": "correct horse battery staple"},
        headers=ORIGIN,
    )
    assert response.status_code == 403, response.text
