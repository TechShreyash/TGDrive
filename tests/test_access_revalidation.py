"""Capabilities are checked again after slow metadata/preview work."""

import pytest
from test_app import app_client as client_fixture
from test_app import login

import tgdrive.app as app_module

app_client = client_fixture


@pytest.mark.parametrize(
    "endpoint,helper",
    [
        ("/api/v1/preview", "describe_preview"),
        ("/api/v1/media", "describe_media"),
    ],
)
def test_revoked_during_preview_work_is_not_delivered_even_to_owner(
    app_client, monkeypatch, endpoint, helper
):
    client, store, _, _ = app_client
    identity = store.add_file("/", "secret.txt", 123, 4)["id"]
    path = "/" + identity
    token = store.create_share(path)
    login(client)

    async def slow_result(**kwargs):
        assert store.authorize_file(path, token)
        store.revoke_share(path)
        return {"kind": "text", "text": "secret", "size": 4}

    monkeypatch.setattr(app_module, helper, slow_result)
    response = client.get(endpoint, params={"path": path, "auth": token})
    assert response.status_code == 403
    assert "secret" not in response.text
