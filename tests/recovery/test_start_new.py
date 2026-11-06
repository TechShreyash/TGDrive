"""Unsafe in-place reset refuses all side effects after validating the owner."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login

ORIGIN = {"Origin": "https://testserver"}


def test_start_new_requires_password_and_confirmation(app_client, monkeypatch):
    client, store, runtime, settings = app_client
    stop = AsyncMock(wraps=runtime.stop)
    monkeypatch.setattr(runtime, "stop", stop)
    store.create_folder("/", "Preserved")
    before = store.export()
    sentinel = settings.resolved_data_dir / "reset-sentinel"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("preserved")
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
    response = client.post(route, json=body, headers=ORIGIN)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "START_NEW_UNAVAILABLE"
    assert "fresh dedicated MongoDB database" in response.json()["detail"]
    stop.assert_not_called()
    assert client.app.state.runtime is runtime
    assert store.export() == before
    assert sentinel.read_text() == "preserved"


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
