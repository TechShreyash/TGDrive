"""Demo content stays readable while its stable catalog subtree rejects writes."""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import Response

from tests.storage.test_mongodb import backend as mongo_backend_fixture
from tests.support.mongo_fixtures import FakeMongoBackend
from tests.web.test_demo_admin_auth import ADMIN, ORIGIN, VISITOR, auth_client, sign_in
from tgdrive.core.errors import DuplicateNameError
from tgdrive.storage.demo_files import (
    DEMO_FILES_ID,
    DEMO_FILES_NAME,
    DemoFilesReadOnly,
    write_policy,
)
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import ROOT_ID, DriveStore
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus
from tgdrive.web.share_links import token_factory

DEMO = "/" + DEMO_FILES_ID
backend = mongo_backend_fixture


def path(item):
    return item["path"].rstrip("/") + "/" + item["id"]


@pytest.fixture(
    params=[
        "sqlite",
        "mongo-cache",
        pytest.param(
            "mongo-live",
            marks=pytest.mark.skipif(
                os.environ.get("TGDRIVE_MONGO_TESTS") != "1",
                reason="Set TGDRIVE_MONGO_TESTS=1 for isolated MongoDB parity",
            ),
        ),
    ]
)
def catalog(tmp_path, request):
    if request.param == "sqlite":
        store = DriveStore.create(tmp_path / "catalog.sqlite3")
    elif request.param == "mongo-live":
        store = MongoStore.create(request.getfixturevalue("backend"))
    else:
        # Exercise MongoStore's actual domain methods and copy-on-write transactions;
        # this cache fixture does not claim to validate MongoDB server persistence.
        store = object.__new__(MongoStore)
        store._lock = threading.RLock()
        store._local = threading.local()
        store._closed = False
        store.backend = SimpleNamespace(check_owner=lambda: None)
        store._committed = {
            "metadata": {"revision": 1, "backed_up_revision": 1, "generation": 0},
            "nodes": {ROOT_ID: store._node(ROOT_ID, None, "/", "folder")},
            "shares": {},
            "operations": {},
            "deletions": {},
            "preparations": {},
        }
        store._commit = lambda _before: None
    try:
        yield store
    finally:
        store.close()


def test_demo_folder_created_once_and_identity_survives_owner_changes(catalog):
    first = catalog.ensure_demo_files()
    revision = catalog.revision
    assert first["id"] == DEMO_FILES_ID and first["name"] == DEMO_FILES_NAME
    assert catalog.ensure_demo_files() == first
    assert catalog.revision == revision
    parent = catalog.create_folder("/", "Owner container")
    catalog.rename(DEMO, "Owner sample library")
    catalog.move(DEMO, path(parent))
    current = catalog.ensure_demo_files()
    assert current["id"] == DEMO_FILES_ID
    assert current["name"] == "Owner sample library"
    assert current["path"] == path(parent)
    with write_policy(True), pytest.raises(DemoFilesReadOnly):
        catalog.create_folder(catalog.resolve_id(DEMO_FILES_ID), "Guest change")
    with write_policy(True), pytest.raises(DemoFilesReadOnly):
        catalog.rename(path(parent), "Guest container")


@pytest.mark.parametrize("existing_type", ["folder", "file"])
def test_demo_folder_name_conflict_preserves_existing_content(catalog, existing_type):
    if existing_type == "folder":
        original = catalog.create_folder("/", DEMO_FILES_NAME)
    else:
        original = catalog.add_file("/", DEMO_FILES_NAME, 333, 3)
    revision = catalog.revision
    before = catalog.list_directory("/")
    with pytest.raises(DuplicateNameError, match="Disable demo mode"):
        catalog.ensure_demo_files()
    assert catalog.revision == revision
    assert catalog.list_directory("/") == before
    assert original["id"] in before


@pytest.mark.parametrize(
    "action",
    ["add", "rename", "trash", "star", "share", "revoke", "move-out", "move-in"],
)
def test_catalog_enforces_demo_policy_inside_mutation_transaction(catalog, action):
    catalog.ensure_demo_files()
    sample = catalog.add_file(DEMO, "sample.txt", 333, 3)
    sample_path = path(sample)
    outside = catalog.create_folder("/", "Visitor folder")
    share = catalog.create_share(sample_path)
    share_id = catalog.list_shares(sample_path)[0]["id"]
    revision = catalog.revision
    operations = {
        "add": lambda: catalog.add_file(DEMO, "changed.txt", 334, 4),
        "rename": lambda: catalog.rename(sample_path, "changed.txt"),
        "trash": lambda: catalog.set_trashed(sample_path, True),
        "star": lambda: catalog.set_starred(sample_path, True),
        "share": lambda: catalog.create_share(sample_path),
        "revoke": lambda: catalog.revoke_share(share_id),
        "move-out": lambda: catalog.move(sample_path, "/"),
        "move-in": lambda: catalog.move(path(outside), DEMO),
    }
    with write_policy(True), pytest.raises(DemoFilesReadOnly):
        operations[action]()
    assert catalog.revision == revision
    assert catalog.get_file(sample_path)["name"] == "sample.txt"
    assert catalog.authorize_file(sample_path, share)
    with write_policy(True):
        catalog.create_folder("/", "Guest can write outside")


def test_demo_deletion_checks_existing_intents_and_final_commit(catalog):
    catalog.ensure_demo_files()
    item = catalog.add_file(DEMO, "owner-trash.txt", 333, 3)
    catalog.set_trashed(path(item), True)
    intent = catalog.begin_deletion(path(item), channel_id=-1001234567890)
    revision = catalog.revision
    with write_policy(True):
        for operation in (
            lambda: catalog.validate_deletion(path(item)),
            lambda: catalog.begin_deletion(path(item), channel_id=-1001234567890),
            lambda: catalog.finish_deletion(intent["id"]),
        ):
            with pytest.raises(DemoFilesReadOnly):
                operation()
    assert catalog.revision == revision
    catalog.finish_deletion(intent["id"])
    assert catalog.get_directory(DEMO)["contents"] == {}


def test_full_owner_can_delete_and_recreate_reserved_folder(catalog):
    catalog.ensure_demo_files()
    catalog.add_file(DEMO, "disposable.txt", 333, 3)
    catalog.set_trashed(DEMO, True)
    intent = catalog.begin_deletion(DEMO, channel_id=-1001234567890)
    catalog.finish_deletion(intent["id"])
    recreated = catalog.ensure_demo_files()
    assert recreated["id"] == DEMO_FILES_ID
    assert recreated["path"] == "/" and recreated["name"] == DEMO_FILES_NAME
    assert catalog.list_directory(DEMO) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("demo_mode", [False, True])
async def test_restored_guest_job_checks_final_commit_after_owner_moves_parent(
    catalog, demo_mode
):
    catalog.ensure_demo_files()
    parent = catalog.create_folder("/", "Started outside demo")
    jobs = FakeMongoBackend()
    registry = TransferRegistry(mongo=jobs.db.jobs, mongo_backend=jobs, demo_mode=True)
    with write_policy(True):
        record = await registry.create("uploading", total=3)
    registry.close()
    registry = TransferRegistry(
        mongo=jobs.db.jobs, mongo_backend=jobs, demo_mode=demo_mode
    )
    record = registry.get(record.id)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def commit(item):
        entered.set()
        await resume.wait()
        destination = catalog.resolve_id(parent["id"])
        await asyncio.to_thread(catalog.add_file, destination, "late.txt", 333, 3)
        await registry.update(item.id, status=TransferStatus.COMPLETED)

    try:
        task = registry.spawn(record.id, commit)
        await entered.wait()
        catalog.move(path(parent), DEMO)
        resume.set()
        await task
        assert record.status == (
            TransferStatus.ERROR if demo_mode else TransferStatus.COMPLETED
        )
        assert len(catalog.list_directory(catalog.resolve_id(parent["id"]))) == int(
            not demo_mode
        )
    finally:
        resume.set()
        await registry.shutdown()
        registry.close()


@pytest.fixture
def visitor(tmp_path):
    with auth_client(tmp_path) as (client, settings, runtime):
        store = runtime.store
        child = store.create_folder(DEMO, "Nested samples")
        sample = store.add_file(path(child), "sample.txt", 333, 3)
        outside = store.create_folder("/", "Visitor files")
        store.create_share(path(sample), token_factory=token_factory(settings))
        share_id = store.list_shares(path(sample))[0]["id"]
        runtime.transfers.inspect_remote = AsyncMock()
        assert sign_in(client, VISITOR).status_code == 200
        try:
            yield client, store, runtime, child, sample, outside, share_id
        finally:
            if hasattr(runtime.transfers, "uploads"):
                runtime.transfers.uploads.close()


@pytest.mark.parametrize(
    "action",
    [
        "folder",
        "rename",
        "trash",
        "delete",
        "share-file",
        "share-folder",
        "share-new",
        "share-update",
        "share-revoke",
        "share-revoke-path",
        "download-link",
        "star",
        "folder-tree",
        "upload-receipt",
        "remote",
        "move-in",
        "move-out",
        "bulk-trash",
        "bulk-restore",
        "bulk-delete",
        "root-share",
    ],
)
def test_direct_visitor_mutations_reject_before_catalog_or_job_changes(visitor, action):
    client, store, runtime, child, sample, outside, share_id = visitor
    sample_path, child_path, outside_path = path(sample), path(child), path(outside)
    requests = {
        "folder": ("POST", "/api/createNewFolder", {"path": child_path, "name": "No"}),
        "rename": ("POST", "/api/renameFileFolder", {"path": DEMO, "name": "No"}),
        "trash": ("POST", "/api/trashFileFolder", {"path": child_path, "trash": True}),
        "delete": ("POST", "/api/deleteFileFolder", {"path": sample_path}),
        "share-file": ("POST", "/api/getFileShareAuth", {"path": sample_path}),
        "share-folder": ("POST", "/api/getFolderShareAuth", {"path": DEMO}),
        "share-new": ("POST", "/api/v1/shares", {"path": sample_path}),
        "share-update": (
            "PATCH",
            "/api/v1/shares",
            {"share_id": share_id, "label": "No"},
        ),
        "share-revoke": ("DELETE", "/api/v1/shares", {"share_id": share_id}),
        "share-revoke-path": ("DELETE", "/api/v1/shares", {"path": sample_path}),
        "download-link": ("POST", "/api/v1/download-links", {"path": sample_path}),
        "star": ("PATCH", "/api/v1/starred", {"path": sample_path, "starred": True}),
        "folder-tree": (
            "POST",
            "/api/v1/folders/tree",
            {
                "destination": child_path,
                "root_name": "No",
                "folders": [["Nested"]],
                "client_request_id": "forbidden-demo-folder-tree",
            },
        ),
        "upload-receipt": (
            "POST",
            "/api/v1/uploads",
            {"parent_id": child_path, "name": "No.txt", "total_size": 3},
        ),
        "remote": (
            "POST",
            "/api/startFileDownloadFromUrl",
            {
                "path": child_path,
                "url": "https://example.com/no.bin",
                "client_request_id": "forbidden-demo-url-upload",
            },
        ),
        "move-in": (
            "POST",
            "/api/v1/nodes/move",
            {"paths": [outside_path], "destination": child_path},
        ),
        "move-out": (
            "POST",
            "/api/v1/nodes/move",
            {"paths": [sample_path], "destination": "/"},
        ),
        "bulk-trash": (
            "POST",
            "/api/v1/nodes/bulk",
            {"paths": [outside_path, DEMO], "action": "trash"},
        ),
        "bulk-restore": (
            "POST",
            "/api/v1/nodes/bulk",
            {"paths": [sample_path], "action": "restore"},
        ),
        "bulk-delete": (
            "POST",
            "/api/v1/nodes/bulk",
            {"paths": [sample_path], "action": "delete"},
        ),
        "root-share": ("POST", "/api/getFolderShareAuth", {"path": "/"}),
    }
    method, address, payload = requests[action]
    before = store.export()
    response = client.request(method, address, json=payload, headers=ORIGIN)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "DEMO_FILES_READ_ONLY"
    assert store.export() == before
    assert runtime.transfers.registry.list() == []
    runtime.transfers.inspect_remote.assert_not_called()


def test_browser_upload_is_rejected_before_transfer_or_request_claim(visitor):
    client, store, runtime, child, *_ = visitor
    revision = store.revision
    response = client.post(
        "/api/upload",
        data={
            "path": path(child),
            "client_request_id": "forbidden-demo-browser-upload",
        },
        files={"file": ("no.txt", b"abc", "text/plain")},
        headers=ORIGIN,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "DEMO_FILES_READ_ONLY"
    assert store.revision == revision
    assert runtime.transfers.registry.list() == []


def test_demo_contents_stay_readable_and_full_password_can_modify(visitor, monkeypatch):
    client, store, _, child, sample, _, share_id = visitor
    root = client.post("/api/getDirectory", json={"path": "/"}).json()["data"]
    assert root["contents"][DEMO_FILES_ID]["read_only"]
    assert root["contents"][DEMO_FILES_ID]["demo_files"]
    directory = client.post("/api/getDirectory", json={"path": path(child)}).json()[
        "data"
    ]
    assert directory["read_only"] and directory["contents"][sample["id"]]["read_only"]
    assert client.get("/api/v1/search", params={"q": "sample"}).json()["items"][0][
        "read_only"
    ]
    assert (
        client.post("/api/v1/shares/link", json={"share_id": share_id}).status_code
        == 200
    )

    async def original(**_kwargs):
        return Response(b"abc", media_type="text/plain")

    monkeypatch.setattr("tgdrive.web.app.stream_telegram_file", original)
    download = client.get("/file", params={"path": path(sample), "download": True})
    assert download.status_code == 200 and download.content == b"abc"
    assert sign_in(client, ADMIN).status_code == 200
    response = client.post(
        "/api/createNewFolder",
        json={"path": path(child), "name": "Owner changes"},
        headers=ORIGIN,
    )
    assert response.status_code == 200
    full_directory = client.post(
        "/api/getDirectory", json={"path": path(child)}
    ).json()["data"]
    assert not full_directory.get("read_only", False)
    assert any(
        item["name"] == "Owner changes"
        for item in store.list_directory(path(child)).values()
    )


@pytest.mark.parametrize("operation", ["append", "complete", "cancel"])
def test_existing_full_access_upload_receipt_cannot_be_mutated_after_downgrade(
    visitor, operation
):
    client, _, runtime, child, *_ = visitor
    assert sign_in(client, ADMIN).status_code == 200
    created = client.post(
        "/api/v1/uploads",
        json={"parent_id": path(child), "name": "owner.txt", "total_size": 3},
    )
    assert created.status_code == 201
    address = "/api/v1/uploads/" + created.json()["id"]
    assert sign_in(client, VISITOR).status_code == 200
    before = runtime.transfers.uploads.get_unchecked(created.json()["id"])
    if operation == "append":
        response = client.patch(
            address,
            content=b"abc",
            headers=ORIGIN
            | {"Content-Type": "application/octet-stream", "Upload-Offset": "0"},
        )
    else:
        response = client.post(address + "/" + operation, json={}, headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["code"] == "DEMO_FILES_READ_ONLY"
    assert runtime.transfers.uploads.get_unchecked(created.json()["id"]) == before
    assert client.get(address).status_code == 200
    assert client.head(address).headers["upload-offset"] == "0"


def test_owner_moving_upload_destination_into_demo_cannot_bypass_receipt_binding(
    visitor,
):
    client, store, _, _, _, outside, _ = visitor
    created = client.post(
        "/api/v1/uploads",
        json={"parent_id": path(outside), "name": "visitor.txt", "total_size": 3},
    )
    assert created.status_code == 201
    store.move(path(outside), DEMO)
    address = "/api/v1/uploads/" + created.json()["id"]
    response = client.patch(
        address,
        content=b"abc",
        headers=ORIGIN
        | {"Content-Type": "application/octet-stream", "Upload-Offset": "0"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "DEMO_FILES_READ_ONLY"


@pytest.mark.parametrize("operation", ["retry", "cancel", "legacy-cancel"])
def test_full_jobs_into_demo_cannot_be_retried_or_cancelled_by_visitor(
    visitor, operation
):
    client, _, runtime, child, *_ = visitor
    record = client.portal.call(
        lambda: runtime.transfers.registry.create("uploading", total=3)
    )
    record.context.update(
        kind="remote", destination=path(child), destination_id=child["id"]
    )
    runtime.transfers.retry_job = lambda _identity: pytest.fail(
        "Protected job was retried"
    )
    runtime.transfers.cancel_job = lambda _identity: pytest.fail(
        "Protected job was cancelled"
    )
    if operation == "legacy-cancel":
        response = client.post(
            "/api/cancelUpload", json={"id": record.id}, headers=ORIGIN
        )
    else:
        response = client.post(
            f"/api/v1/jobs/{record.id}/{operation}", json={}, headers=ORIGIN
        )
    assert response.status_code == 403
    assert response.json()["code"] == "DEMO_FILES_READ_ONLY"
    assert not record.context.get("cancel_requested")
