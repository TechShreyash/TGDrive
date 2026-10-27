"""Owner backup status/actions and cancellation boundaries through real routes."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.recovery.backup import RecoveryConflict
from tgdrive.storage.store import DriveStore
from tgdrive.transfers.manager import BackupPending
from tgdrive.web.app import create_app


class Backup:
    def __init__(self):
        self.calls = []
        self.history_limits = []
        self.latest = []
        self.confirmed = 0
        self.fail = False

    def history(self, *, limit=None):
        self.history_limits.append(limit)
        return []

    def latest_history(self):
        return self.latest

    def protection_status(self, store):
        return {
            "state": "protected" if self.confirmed >= store.revision else "pending",
            "metadata_revision": store.revision,
            "protected_revision": self.confirmed,
        }

    async def ensure_revision(self, store, revision):
        self.calls.append(revision)
        if self.fail:
            raise RuntimeError("private-provider-token-must-never-be-returned")
        self.confirmed = revision
        return revision

    async def snapshot_now(self, store):
        return await self.ensure_revision(store, store.revision)


@pytest.fixture
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path / "data")


@pytest.fixture
def owner(tmp_path, mongo_backend):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    runtime = FakeRuntime(store)
    runtime.mongo = mongo_backend
    runtime.pool = None
    runtime.backup = Backup()
    with TestClient(
        create_app(settings, runtime=runtime), base_url="https://testserver"
    ) as client:
        yield client, store, runtime, settings
    store.close()


def test_backup_actions_require_owner_and_same_origin(owner):
    client, _, runtime, _ = owner
    assert client.get("/api/v1/status").status_code == 401
    assert client.post("/api/v1/backup", json={}).status_code == 401
    login(client)
    response = client.post(
        "/api/v1/backup", json={}, headers={"Origin": "https://other.example"}
    )
    assert response.status_code == 403
    assert runtime.backup.calls == []


def test_manual_backup_confirms_current_revision_without_querying_status_history(owner):
    client, store, runtime, _ = owner
    login(client)
    store.create_folder("/", "Recoverable")
    pending = client.get("/api/v1/status")
    assert pending.status_code == 200
    assert pending.json()["recovery"]["state"] == "pending"
    assert runtime.backup.history_limits == []
    protected = client.post("/api/v1/backup", json={})
    assert protected.status_code == 200
    assert runtime.backup.calls == [store.revision]
    assert protected.json()["recovery"] == {
        "state": "protected",
        "metadata_revision": store.revision,
        "protected_revision": store.revision,
    }
    store.create_folder("/", "New changes")
    assert client.get("/api/v1/status").json()["recovery"]["state"] == "pending"


def test_status_uses_observed_backup_without_reading_history(owner):
    client, _, runtime, _ = owner
    login(client)
    runtime.backup.latest = [{"id": "observed", "status": "pending"}]
    runtime.backup.history = Mock(
        side_effect=AssertionError("Unexpected history query")
    )
    for _ in range(3):
        response = client.get("/api/v1/status")
        assert response.status_code == 200
        assert response.json()["backup"] == runtime.backup.latest
    runtime.backup.history.assert_not_called()


def test_jobs_calculate_one_queue_observation_for_the_whole_response(owner):
    client, _, runtime, _ = owner
    login(client)
    registry = runtime.transfers.registry
    records = [client.portal.call(lambda: registry.create("queued")) for _ in range(3)]
    batches = []

    def estimates(identities):
        batches.append(set(identities))
        return {records[0].id: {"position": 1, "queued_operations": 2}}

    single = Mock(return_value={"position": 1, "queued_operations": 2})
    runtime.pool = SimpleNamespace(queue_estimates=estimates, queue_estimate=single)
    response = client.get("/api/v1/jobs")
    assert response.status_code == 200
    assert batches == [{record.id for record in records}]
    jobs = {job["id"]: job for job in response.json()["jobs"]}
    assert jobs[records[0].id]["queue"] == {"position": 1, "queued_operations": 2}
    assert jobs[records[1].id]["queue"] is None
    assert jobs[records[2].id]["queue"] is None
    single.assert_not_called()
    assert client.get("/api/v1/jobs/" + records[0].id).json()["queue"] == {
        "position": 1,
        "queued_operations": 2,
    }
    single.assert_called_once_with(records[0].id)


def test_manual_backup_failure_never_claims_confirmation_or_exposes_provider_error(
    owner,
):
    client, _, runtime, _ = owner
    login(client)
    runtime.backup.fail = True
    response = client.post("/api/v1/backup", json={})
    assert response.status_code == 503
    assert "not confirmed" in response.json()["detail"]
    assert "private-provider" not in response.text
    assert client.get("/api/v1/status").json()["recovery"]["state"] == "pending"


def test_backup_unavailable_is_explicit(owner):
    client, _, runtime, _ = owner
    login(client)
    runtime.backup = None
    assert client.post("/api/v1/backup", json={}).status_code == 503
    assert client.get("/api/v1/status").json()["recovery"] is None


@pytest.mark.parametrize("route", ["job", "legacy-id", "legacy-request"])
def test_no_cancellation_route_bypasses_checkpoint_guard(owner, route):
    client, _, runtime, _ = owner
    login(client)
    manager = runtime.transfers
    record = client.portal.call(
        lambda: manager.registry.create("backup_pending", total=10)
    )
    request_id = "checkpoint-cancel-request"
    starts = client.app.state.transfer_starts
    entry, _ = starts.claim(request_id, ("test",))
    starts.bind(entry, record)

    async def protected(_job_id):
        raise BackupPending("File stored; recovery checkpoint is pending")

    manager.cancel_job = protected
    if route == "job":
        response = client.post(f"/api/v1/jobs/{record.id}/cancel", json={})
    else:
        payload = (
            {"id": record.id}
            if route == "legacy-id"
            else {"client_request_id": request_id}
        )
        response = client.post("/api/cancelUpload", json=payload)
    assert response.status_code == 409
    assert response.json()["code"] == "BACKUP_PENDING"
    assert not record.cancel_event.is_set()
    assert not record.context.get("cancel_requested")
    assert not entry.cancelled


async def test_receipt_cancellation_checks_manager_before_mutating_job_or_receipt(
    tmp_path,
    mongo_backend,
):
    from tests.transfers.test_resumable_uploads import make

    uploads, manager = make(tmp_path)
    try:
        receipt = uploads.create("one.txt", "/", 4)
        await uploads.append(receipt["id"], 0, b"safe")
        record = await uploads.complete(receipt["id"])
        before = uploads.get_unchecked(receipt["id"]).copy()

        async def protected(_job_id):
            raise BackupPending("File stored; recovery checkpoint is pending")

        manager.cancel_job = protected
        with pytest.raises(BackupPending):
            await uploads.cancel(receipt["id"])
        assert uploads.get_unchecked(receipt["id"]) == before
        assert not record.cancel_event.is_set()
    finally:
        uploads.close()
        manager.registry.close()
        manager.store.close()


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        ("/api/createNewFolder", {"path": "/", "name": "Unsafe"}),
        ("/api/v1/uploads", {"name": "new.txt", "parent_id": "/", "total_size": 4}),
        ("/api/v1/nodes/bulk", {"paths": ["/anything"], "action": "delete"}),
        (
            "/api/v1/folders/tree",
            {
                "destination": "/",
                "root_name": "Unsafe",
                "folders": [],
                "client_request_id": "root-should-not-be-created",
            },
        ),
        ("/api/v1/backup", {}),
    ],
)
def test_recovery_conflict_blocks_writes_but_preserves_owner_catalog(
    owner,
    route,
    payload,
):
    client, store, runtime, _ = owner
    original = store.create_folder("/", "Preserved")
    runtime.backup.recovery_required = True
    login(client)
    response = client.post(route, json=payload)
    assert response.status_code == 409
    assert response.json()["code"] == "RECOVERY_REQUIRED"
    assert list(store.list_directory("/")) == [original["id"]]
    catalog = client.post("/api/getDirectory", json={"path": "/"})
    assert catalog.status_code == 200
    assert client.get("/api/v1/status").status_code == 200
    assert client.post("/api/logout", json={}).status_code == 200


def test_recovery_conflict_blocks_media_before_using_wrong_channel(owner):
    client, store, runtime, _ = owner
    item = store.add_file("/", "private.txt", 123, 4)
    runtime.backup.recovery_required = True
    login(client)
    for path in ("/file", "/api/v1/preview", "/api/v1/media"):
        response = client.get(path, params={"path": "/" + item["id"]})
        assert response.status_code == 409
        assert response.json()["code"] == "RECOVERY_REQUIRED"


def test_manual_backup_discovery_returns_structured_recovery_conflict(owner):
    client, _, runtime, _ = owner
    login(client)

    async def conflict(_store):
        raise RecoveryConflict("A newer Telegram catalog needs explicit recovery")

    runtime.backup.snapshot_now = conflict
    response = client.post("/api/v1/backup", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "RECOVERY_REQUIRED"


def test_recovery_conflict_also_pauses_previously_issued_public_links(owner):
    client, store, runtime, _ = owner
    folder = store.create_folder("/", "Shared before conflict")
    path = "/" + folder["id"]
    token = store.create_share(path)
    runtime.backup.recovery_required = True
    response = client.post(
        "/api/getDirectory", json={"path": "/share_" + folder["id"], "auth": token}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "RECOVERY_REQUIRED"
