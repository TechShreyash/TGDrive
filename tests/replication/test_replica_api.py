"""Recovery remains owner-only and reachable when the live catalog is missing."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from tests.core.test_runtime import mongo_backend as mongo_backend
from tests.replication.test_replica import TOKEN, Telegram
from tests.replication.test_replica_activation import NEW, candidate
from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.core.errors import CorruptStoreError
from tgdrive.core.runtime import Runtime
from tgdrive.replication.replica import ChannelReplica
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.store import DriveStore
from tgdrive.web.app import create_app


@pytest.fixture
def owner(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    backend = register_test_backend(monkeypatch, settings)
    monkeypatch.setattr("tgdrive.storage.mongo.for_settings", lambda _: backend)
    store = DriveStore.create(tmp_path / "catalog.sqlite3")
    store.bind_storage_channel(settings.storage_channel)
    runtime = FakeRuntime(store)
    runtime.mongo = backend
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
    for path in ("", "/key", "/file", "/report"):
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


def test_file_restore_policy_is_explicit_in_saved_status(owner):
    client, _, replica = owner
    login(client)
    assert client.get("/api/v1/replica").json()["file_restore_allowed"] is True
    replica.settings.demo_mode = True
    response = client.get("/api/v1/replica")
    assert response.status_code == 200
    assert response.json()["file_restore_allowed"] is False


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


@pytest.mark.parametrize("confirmation", [None, "", "-100999888777"])
def test_activation_requires_pending_destination_and_preserves_plan_on_failure(
    owner, confirmation
):
    client, runtime, replica = owner
    runtime.store.add_file("/", "preserved.bin", 123, 42)
    pending = candidate(runtime.store.export())
    replica.journal.set("candidate", pending)
    recovery = {"target": NEW, "phase": "ready_to_activate"}
    replica.journal.set("recovery", recovery)
    manifest = replica.settings.resolved_data_dir / "channel-activation.json"
    original_catalog = runtime.store.export()
    login(client)

    payload = {"activate": True, "target_channel": -100999888777}
    if confirmation is not None:
        payload["confirm_channel"] = confirmation
    rejected = client.post("/api/v1/replica/recover", json=payload)
    assert rejected.status_code == 422
    assert "confirm activation" in rejected.json()["detail"]
    assert replica.journal.get("candidate") == pending
    assert replica.journal.get("recovery") == recovery
    assert not replica.journal.get("activation")
    assert not manifest.exists()
    assert runtime.store.export() == original_catalog

    accepted = client.post(
        "/api/v1/replica/recover",
        json={"activate": True, "confirm_channel": str(NEW)},
    )
    assert accepted.status_code == 200
    assert accepted.json()["phase"] == "restart_required"
    assert replica.journal.get("activation") is True
    assert replica.journal.get("candidate") == pending
    assert replica.journal.get("recovery") == recovery
    assert (
        json.loads(manifest.read_text())["candidate"]["catalog"]["storage_channel_id"]
        == NEW
    )
    assert runtime.store.export() == original_catalog


def test_matching_destination_cannot_activate_incomplete_recovery(owner):
    client, runtime, replica = owner
    pending = candidate(runtime.store.export())
    pending["complete"] = False
    replica.journal.set("candidate", pending)
    login(client)
    response = client.post(
        "/api/v1/replica/recover",
        json={"activate": True, "confirm_channel": str(NEW)},
    )
    assert response.status_code == 409
    assert replica.journal.get("candidate") == pending
    assert not replica.journal.get("activation")
    assert not (replica.settings.resolved_data_dir / "channel-activation.json").exists()


def test_missing_catalog_starts_authenticated_recovery_shell_without_telegram_writes(
    tmp_path, mongo_backend
):
    settings = make_settings(tmp_path)
    runtime = Runtime(settings)
    runtime.backup.restore = AsyncMock(
        side_effect=CorruptStoreError(
            "Saved MongoDB catalog requires explicit recovery"
        )
    )
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


def test_mongo_ownership_unavailable_rejects_startup_without_local_fallback(
    tmp_path, mongo_backend
):
    settings = make_settings(tmp_path)
    runtime = Runtime(settings)

    mongo_backend.acquire.side_effect = DatabaseUnavailable(
        "synthetic offline database"
    )
    with (
        pytest.raises(DatabaseUnavailable, match="synthetic offline database"),
        TestClient(
            create_app(settings, runtime=runtime), base_url="https://testserver"
        ),
    ):
        pytest.fail("An unavailable database must reject application startup")
    assert not runtime.ready
    assert runtime.store is None and runtime.transfers is None
    assert not runtime._mongo_acquired
    assert runtime._owner_lock is None
    assert not (settings.resolved_data_dir / "drive.sqlite3").exists()
    mongo_backend.release.assert_not_called()


def test_corrupt_activation_manifest_preserves_owner_recovery_shell(
    tmp_path, mongo_backend
):
    settings = make_settings(tmp_path)
    from tgdrive.replication.replica_state import atomic_private

    atomic_private(settings.resolved_data_dir / "channel-activation.json", b"{}")
    runtime = Runtime(settings)
    with TestClient(
        create_app(settings, runtime=runtime), base_url="https://testserver"
    ) as client:
        assert runtime.recovery_only and runtime.store is None
        login(client)
        assert client.get("/api/v1/replica").status_code == 200


def test_recovery_file_import_accepts_binary_body_larger_than_json_limit(owner):
    client, _, replica = owner
    key, content = "a1" * 32, b"encrypted recovery file" * 4096
    observed = {}

    async def inspect(path, saved_key, source, token):
        observed.update(
            content=path.read_bytes(),
            key=saved_key,
            source=source,
            token=token,
            path=path,
        )
        return {"phase": "paused", "recovery_candidate": {"complete": False}}

    replica.inspect_file = AsyncMock(side_effect=inspect)
    login(client)
    response = client.post(
        "/api/v1/replica/import",
        content=content,
        headers={
            "Content-Type": "application/octet-stream",
            "X-TGDrive-Recovery-Key": key,
        },
    )
    assert response.status_code == 200
    assert response.json()["phase"] == "paused"
    assert observed["content"] == content and observed["key"] == key
    assert observed["source"] is None and observed["token"] is None
    assert not observed["path"].exists()
    assert key not in response.text


def test_recovery_file_import_accepts_replacement_channel_credentials(owner):
    client, _, replica = owner
    replica.inspect_file = AsyncMock(return_value={"phase": "paused"})
    login(client)
    response = client.post(
        "/api/v1/replica/import",
        content=b"ciphertext",
        headers={
            "Content-Type": "application/octet-stream",
            "X-TGDrive-Recovery-Key": "a1" * 32,
            "X-TGDrive-Source-Channel": "-1001234567890",
            "X-TGDrive-Bot-Token": TOKEN,
        },
    )
    assert response.status_code == 200
    args = replica.inspect_file.await_args.args
    assert args[1:] == ("a1" * 32, -1001234567890, TOKEN)
    assert not args[0].exists()


@pytest.mark.parametrize(
    "headers,status",
    [
        (
            {"Content-Type": "application/json", "X-TGDrive-Recovery-Key": "a1" * 32},
            415,
        ),
        (
            {
                "Content-Type": "application/octet-stream",
                "X-TGDrive-Recovery-Key": "private-key-must-not-echo",
            },
            422,
        ),
        ({"Content-Type": "application/octet-stream"}, 422),
        (
            {
                "Content-Type": "application/octet-stream",
                "X-TGDrive-Recovery-Key": "a1" * 32,
                "X-TGDrive-Source-Channel": "private-channel-must-not-echo",
            },
            422,
        ),
        (
            {
                "Content-Type": "application/octet-stream",
                "X-TGDrive-Recovery-Key": "a1" * 32,
                "X-TGDrive-Bot-Token": "private-token-must-not-echo",
            },
            422,
        ),
    ],
)
def test_recovery_file_import_rejects_invalid_headers_without_echoing_secrets(
    owner, headers, status
):
    client, _, replica = owner
    replica.inspect_file = AsyncMock()
    login(client)
    response = client.post(
        "/api/v1/replica/import", content=b"ciphertext", headers=headers
    )
    assert response.status_code == status
    assert "private-" not in response.text
    assert not list(replica.directory.glob(".replica-import-*"))
    replica.inspect_file.assert_not_awaited()


def test_recovery_file_import_requires_authentication_and_same_origin(owner):
    client, _, replica = owner
    replica.inspect_file = AsyncMock()
    headers = {
        "Content-Type": "application/octet-stream",
        "X-TGDrive-Recovery-Key": "a1" * 32,
    }
    assert (
        client.post(
            "/api/v1/replica/import", content=b"x" * 70000, headers=headers
        ).status_code
        == 401
    )
    login(client)
    response = client.post(
        "/api/v1/replica/import",
        content=b"ciphertext",
        headers=headers | {"Origin": "https://foreign.example"},
    )
    assert response.status_code == 403
    replica.inspect_file.assert_not_awaited()


def test_recovery_file_import_body_limit_applies_with_and_without_content_length(
    owner, monkeypatch
):
    client, _, replica = owner
    replica.inspect_file = AsyncMock()
    login(client)
    monkeypatch.setattr("tgdrive.replication.replica_state.MAX_BUNDLE", 128)
    headers = {
        "Content-Type": "application/octet-stream",
        "X-TGDrive-Recovery-Key": "a1" * 32,
    }
    response = client.post(
        "/api/v1/replica/import",
        content=b"ciphertext",
        headers=headers | {"Content-Length": "129"},
    )
    assert response.status_code == 413
    response = client.post(
        "/api/v1/replica/import",
        content=iter([b"x" * 65, b"x" * 64]),
        headers=headers,
    )
    assert response.status_code == 413
    assert not list(replica.directory.glob(".replica-import-*"))
    replica.inspect_file.assert_not_awaited()


def test_recovery_file_import_remains_available_during_recovery_and_connection_repair(
    owner,
):
    client, runtime, replica = owner
    runtime.backup = SimpleNamespace(recovery_required=True)
    runtime.connection_manager = SimpleNamespace(
        restart_required=True, close=AsyncMock()
    )
    replica.inspect_file = AsyncMock(return_value={"phase": "paused"})
    login(client)
    response = client.post(
        "/api/v1/replica/import",
        content=b"ciphertext",
        headers={
            "Content-Type": "application/octet-stream",
            "X-TGDrive-Recovery-Key": "a1" * 32,
        },
    )
    assert response.status_code == 200
    assert (
        client.post(
            "/api/createNewFolder", json={"path": "/", "name": "unsafe"}
        ).status_code
        == 409
    )


def test_recovery_file_download_is_private_and_not_cached(owner):
    client, _, replica = owner
    from tgdrive.replication.replica_state import atomic_private

    content = b"encrypted recovery file"
    digest = hashlib.sha256(content).hexdigest()
    path = replica.directory / "replica-bundles" / (digest + ".tgdb")
    atomic_private(path, content)
    replica.journal.set(
        "backup_file",
        {
            "digest": digest,
            "sha256": digest,
            "key": "a1" * 32,
            "created_at": "2026-10-28T12:00:00+00:00",
        },
    )
    assert client.get("/api/v1/replica/file").status_code == 401
    login(client)
    response = client.get("/api/v1/replica/file")
    assert (
        response.status_code == 200 and response.content == b"encrypted recovery file"
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/octet-stream"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="tgdrive-backup.tgdb"'
    )


def test_recovery_file_download_and_import_are_denied_in_read_only_demo(owner):
    client, _, replica = owner
    replica.inspect_file = AsyncMock()
    login(client)
    replica.settings.demo_mode = True
    assert client.get("/api/v1/replica/file").status_code == 403
    response = client.post(
        "/api/v1/replica/import",
        content=b"ciphertext",
        headers={
            "Content-Type": "application/octet-stream",
            "X-TGDrive-Recovery-Key": "a1" * 32,
        },
    )
    assert response.status_code == 403
    replica.inspect_file.assert_not_awaited()


def test_back_up_now_uses_one_independent_recovery_checkpoint(owner):
    client, runtime, replica = owner
    runtime.backup = SimpleNamespace(
        ensure_revision=AsyncMock(),
        snapshot_now=AsyncMock(),
        protection_status=lambda _store: {"state": "protected"},
    )
    config = {"channel": -100222333444, "token": TOKEN, "enabled": True}
    replica.journal.set("config", config)

    async def confirm(_config, *, force_snapshot=False):
        replica.update(protected_revision=runtime.store.revision)

    replica._sync = AsyncMock(side_effect=confirm)
    login(client)
    response = client.post("/api/v1/backup", json={})
    assert response.status_code == 200
    replica._sync.assert_awaited_once_with(config, force_snapshot=True)
    runtime.backup.snapshot_now.assert_not_awaited()


@pytest.mark.parametrize(
    "condition,status",
    [("paused", 409), ("recovering", 409), ("source-unavailable", 503)],
)
def test_back_up_now_does_not_fall_back_when_independent_protection_cannot_run(
    owner, condition, status
):
    from tgdrive.replication.replica_transport import ReplicaTransportError

    client, runtime, replica = owner
    runtime.backup = SimpleNamespace(
        ensure_revision=AsyncMock(), snapshot_now=AsyncMock()
    )
    replica.journal.set(
        "config", {"channel": -100222333444, "token": TOKEN, "enabled": True}
    )
    replica._sync = AsyncMock(
        side_effect=ReplicaTransportError(
            "The source channel cannot be verified",
            "REPLICA_CHANNEL_UNAVAILABLE",
            retryable=True,
        )
    )
    if condition == "paused":
        replica.update(phase="paused")
    elif condition == "recovering":
        replica.journal.set("recovery", {"target": -100222333444})
    before = runtime.store.export()
    login(client)
    response = client.post("/api/v1/backup", json={})
    assert response.status_code == status
    runtime.backup.snapshot_now.assert_not_awaited()
    assert runtime.store.export() == before
    if condition != "source-unavailable":
        replica._sync.assert_not_awaited()
    if condition == "recovering":
        assert (
            client.post(
                "/api/createNewFolder", json={"path": "/", "name": "unsafe"}
            ).status_code
            == 409
        )
        assert runtime.store.export() == before
