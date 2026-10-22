"""Recovery remains owner-only and reachable when the live catalog is missing."""

import pytest
from fastapi.testclient import TestClient
from test_app import FakeRuntime, login, make_settings
from test_replica import TOKEN, Telegram

from tgdrive.app import create_app
from tgdrive.mongo import DatabaseUnavailable
from tgdrive.replica import ChannelReplica
from tgdrive.runtime import Runtime
from tgdrive.store import DriveStore


@pytest.fixture
def owner(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "catalog.sqlite3")
    store.bind_storage_channel(settings.storage_channel)
    runtime = FakeRuntime(store)
    remote = Telegram()
    replica = ChannelReplica(settings, runtime, transport_factory=remote.factory)
    runtime.replica = replica
    original_stop = runtime.stop

    async def stop():
        await replica.close()
        await original_stop()

    runtime.stop = stop
    with TestClient(
        create_app(settings, runtime=runtime), base_url="https://testserver"
    ) as client:
        yield client, runtime, replica
    store.close()


def test_replica_is_owner_only_and_cross_origin_mutations_are_rejected(owner):
    client, _, _ = owner
    for path in ("", "/key", "/report"):
        assert client.get("/api/v1/replica" + path).status_code == 401
    login(client)
    response = client.post(
        "/api/v1/replica/configure",
        json={
            "channel_id": -100222333444,
            "bot_token": TOKEN,
            "independent_accounts": True,
        },
        headers={"Origin": "https://foreign.example"},
    )
    assert response.status_code == 403


def test_status_and_reports_never_expose_private_replica_credentials(owner):
    client, _, replica = owner
    replica.journal.set(
        "config", {"channel": -100222333444, "token": TOKEN, "enabled": False}
    )
    replica.journal.set("candidate", None)
    login(client)
    for path in ("", "/report"):
        response = client.get("/api/v1/replica" + path)
        assert response.status_code == 200
        assert TOKEN not in response.text
        assert replica.key() not in response.text
    response = client.get("/api/v1/replica/key")
    assert response.status_code == 200
    assert response.json()["key"] == replica.key()
    assert response.headers["cache-control"] == "no-store"


def test_recovery_blocks_file_mutations_but_keeps_owner_controls_available(owner):
    client, _, replica = owner
    replica.journal.set("recovery", {"target": -100222333444})
    login(client)
    assert (
        client.post(
            "/api/createNewFolder", json={"path": "/", "name": "unsafe"}
        ).status_code
        == 409
    )
    assert client.post("/api/v1/backup", json={}).status_code == 409
    assert client.get("/api/v1/replica").status_code == 200
    assert client.post("/api/logout", json={}).status_code == 200


def test_request_validation_hides_rejected_secrets_and_no_unverified_activation(owner):
    client, _, _ = owner
    login(client)
    response = client.post(
        "/api/v1/replica/configure",
        json={
            "channel_id": "private-token-that-must-not-echo",
            "bot_token": TOKEN,
            "independent_accounts": True,
        },
    )
    assert response.status_code == 422
    assert TOKEN not in response.text
    assert "private-token" not in response.text
    assert (
        client.post("/api/v1/replica/recover", json={"activate": True}).status_code
        == 409
    )


def test_missing_catalog_starts_authenticated_recovery_shell_without_telegram_writes(
    tmp_path,
):
    settings = make_settings(tmp_path)
    runtime = Runtime(settings)
    with TestClient(
        create_app(settings, runtime=runtime), base_url="https://testserver"
    ) as client:
        assert not runtime.ready
        assert runtime.recovery_only
        assert runtime.store is None
        assert runtime.transfers is None
        assert client.get("/").status_code == 200
        assert client.get("/api/config").json()["recovery_only"] is True
        assert client.get("/api/v1/replica").status_code == 401
        login(client)
        assert client.get("/api/v1/status").json()["recovery_only"] is True
        assert client.get("/api/v1/replica").json()["recovery_only"] is True
        assert client.post("/api/getDirectory", json={"path": "/"}).status_code == 409


def test_mongo_ownership_unavailable_keeps_owner_recovery_without_local_fallback(
    tmp_path,
):
    settings = make_settings(tmp_path)
    runtime = Runtime(settings)

    class UnavailableMongo:
        def acquire(self):
            raise DatabaseUnavailable("synthetic offline database")

    runtime.mongo = UnavailableMongo()
    with TestClient(
        create_app(settings, runtime=runtime), base_url="https://testserver"
    ) as client:
        assert runtime.recovery_only and not runtime.ready
        assert runtime.store is None and runtime.transfers is None
        assert not runtime._mongo_acquired
        assert not (settings.resolved_data_dir / "drive.sqlite3").exists()
        login(client)
        assert client.get("/api/v1/replica").status_code == 200
        assert (
            client.post(
                "/api/createNewFolder", json={"path": "/", "name": "blocked"}
            ).status_code
            == 409
        )


def test_corrupt_activation_manifest_preserves_owner_recovery_shell(tmp_path):
    settings = make_settings(tmp_path)
    from tgdrive.replica_state import atomic_private

    atomic_private(settings.resolved_data_dir / "channel-activation.json", b"{}")
    runtime = Runtime(settings)
    with TestClient(
        create_app(settings, runtime=runtime), base_url="https://testserver"
    ) as client:
        assert runtime.recovery_only and runtime.store is None
        login(client)
        assert client.get("/api/v1/replica").status_code == 200
