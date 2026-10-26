from __future__ import annotations

import copy
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import tgdrive.storage.store as store_module
from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tgdrive.core.errors import (
    CorruptStoreError,
    DuplicateNameError,
    InvalidNameError,
    InvalidPathError,
    NotDirectoryError,
    PersistenceError,
    TrashedItemError,
    ValidationError,
)
from tgdrive.storage.store import DriveStore, FolderTreeConflictError
from tgdrive.web.app import create_app


def prepare(store, *, request_id=None, destination="/", root="Photos", folders=None):
    return store.prepare_folder_tree(
        destination, root, folders or [], request_id or str(uuid.uuid4())
    )


def test_atomic_tree_returns_opaque_paths_and_recovers_lost_response(tmp_path):
    database = tmp_path / "drive.sqlite3"
    store = DriveStore.create(database)
    request_id = str(uuid.uuid4())
    folders = [["2025", "Trips"], ["2025"], ["Personal"]]
    before = store.revision
    first = prepare(store, request_id=request_id, folders=folders)
    root = first["root"]
    assert root["name"] == "Photos"
    assert root["path"] == "/"
    assert first["folders"][""] == "/" + root["id"]
    assert set(first["folders"]) == {"", "2025", "2025/Trips", "Personal"}
    for relative, path in first["folders"].items():
        node = store.get_directory(path)
        assert node["name"] == (relative.rsplit("/", 1)[-1] if relative else "Photos")
    assert store.revision == before + 1
    store.close()
    recovered = DriveStore.load(database)
    assert prepare(recovered, request_id=request_id, folders=folders[::-1]) == first
    assert recovered.revision == before + 1
    # Caller mutations cannot corrupt the durable replay result.
    first["folders"]["2025"] = "/not-a-real-path"
    assert (
        prepare(recovered, request_id=request_id, folders=folders)["folders"]["2025"]
        != first["folders"]["2025"]
    )
    recovered.close()


def test_fresh_root_keeps_both_without_merging_or_overwriting():
    store = DriveStore.create()
    original = store.create_folder("/", "PHOTOS")
    original_path = "/" + original["id"]
    store.create_folder(original_path, "Untouched")
    first = prepare(store, folders=[["Nested"]])
    second = prepare(store, folders=[["Nested"]])
    assert first["root"]["name"] == "Photos (1)"
    assert second["root"]["name"] == "Photos (2)"
    assert first["folders"][""] != second["folders"][""] != original_path
    assert len(store.get_directory(original_path)["contents"]) == 1
    assert (
        next(iter(store.get_directory(original_path)["contents"].values()))["name"]
        == "Untouched"
    )
    store.close()


@pytest.mark.parametrize(
    "folders",
    [
        [[".."]],
        [["."]],
        [["bad/name"]],
        [["bad\\name"]],
        [["/absolute"]],
        [["leading "]],
        [["\x00"]],
        [["line\nfeed"]],
        [["\ud800"]],
        [[]],
        [["Missing", "Child"]],
        [["Straße"], ["STRASSE"]],
        [["Résumé"], ["RE\u0301SUME\u0301"]],
        [["Same"], ["Same"]],
        [["x"] * 64],
    ],
)
def test_invalid_manifest_never_creates_partial_folders(folders):
    store = DriveStore.create()
    before = store.export()
    with pytest.raises((ValidationError, InvalidNameError, DuplicateNameError)):
        prepare(store, folders=folders)
    assert store.export() == before
    store.close()


@pytest.mark.parametrize("destination", ["relative", "/../x", "/a//b", "/a\\b"])
def test_invalid_destination_is_rejected_before_writing(destination):
    store = DriveStore.create()
    before = store.export()
    with pytest.raises(InvalidPathError):
        prepare(store, destination=destination)
    assert store.export() == before
    store.close()


def test_unicode_is_normalized_and_order_does_not_change_request_identity():
    store = DriveStore.create()
    request_id = str(uuid.uuid4())
    first = prepare(
        store,
        request_id=request_id,
        root="Re\u0301sume\u0301",
        folders=[["Re\u0301sume\u0301"]],
    )
    second = prepare(store, request_id=request_id, root="Résumé", folders=[["Résumé"]])
    assert first == second
    assert set(first["folders"]) == {"", "Résumé"}
    # Percent escapes are literal names; they are never decoded into traversal.
    literal = prepare(store, root="%2e%2e", folders=[["%2f"]])
    assert store.get_directory(literal["folders"]["%2f"])["name"] == "%2f"
    store.close()


def test_folder_and_absolute_depth_bounds_are_enforced_atomically(monkeypatch):
    store = DriveStore.create()
    first = prepare(store, folders=[[str(n)] for n in range(999)])
    assert len(first["folders"]) == 1000
    before = store.export()
    with pytest.raises(ValidationError, match="1000"):
        prepare(store, folders=[[str(n)] for n in range(1000)])
    assert store.export() == before
    deep = [["x"] * n for n in range(1, 64)]
    result = prepare(store, folders=deep)
    assert len(result["folders"]) == 64
    monkeypatch.setattr(store_module, "MAX_TREE_DEPTH", 3)
    before = store.export()
    with pytest.raises(ValidationError, match="depth"):
        prepare(store, destination=result["folders"]["x"], folders=[["a"], ["a", "b"]])
    assert store.export() == before
    with monkeypatch.context() as limited:
        limited.setattr(store_module, "MAX_TREE_ITEMS", 2)
        with pytest.raises(ValidationError, match="item count"):
            prepare(store)
    assert store.export() == before
    store.close()


def test_database_failure_rolls_back_every_node_and_idempotency_record(monkeypatch):
    store = DriveStore.create()
    before = store.export()
    original = store._import_folder_preparation

    def fail_after_creation(db, record):
        original(db, record)
        raise sqlite3.OperationalError("injected commit failure")

    monkeypatch.setattr(store, "_import_folder_preparation", fail_after_creation)
    with pytest.raises(PersistenceError, match="injected"):
        prepare(store, folders=[["Child"], ["Child", "Nested"]])
    assert store.export() == before
    store.close()


def test_concurrent_duplicate_request_commits_once(tmp_path):
    database = tmp_path / "drive.sqlite3"
    first = DriveStore.create(database)
    second = DriveStore.load(database)
    request_id = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as executor:
        calls = [
            executor.submit(prepare, store, request_id=request_id, folders=[["Child"]])
            for store in (first, second)
        ]
        results = [call.result() for call in calls]
    assert results[0] == results[1]
    assert len(first.get_directory("/")["contents"]) == 1
    assert first.revision == 2
    first.close()
    second.close()


def test_request_id_cannot_prepare_different_tree():
    store = DriveStore.create()
    request_id = str(uuid.uuid4())
    prepare(store, request_id=request_id, folders=[["Child"]])
    before = store.export()
    with pytest.raises(FolderTreeConflictError, match="different"):
        prepare(store, request_id=request_id, folders=[["Other"]])
    assert store.export() == before
    store.close()


@pytest.mark.parametrize(
    "change", ["move_root", "move_child", "rename", "trash", "delete", "deleting"]
)
def test_changed_tree_replay_fails_without_silent_recreation(change):
    store = DriveStore.create()
    request_id = str(uuid.uuid4())
    tree = prepare(store, request_id=request_id, folders=[["Child"]])
    root = tree["folders"][""]
    child = tree["folders"]["Child"]
    if change == "move_root":
        other = store.create_folder("/", "Other")
        store.move(root, "/" + other["id"])
    elif change == "move_child":
        store.move(child, "/")
    elif change == "rename":
        store.rename(child, "Changed")
    elif change == "trash":
        store.set_trashed(root, True)
    elif change == "deleting":
        store.set_trashed(root, True)
        store.begin_deletion(root)
    else:
        store.delete(root)
    before = store.export()
    with pytest.raises(FolderTreeConflictError, match="changed"):
        prepare(store, request_id=request_id, folders=[["Child"]])
    assert store.export() == before
    # Tombstone results remain valid metadata after nodes are removed.
    restored = DriveStore._from_payload(store.export())
    assert restored.export() == store.export()
    with pytest.raises(FolderTreeConflictError, match="changed"):
        prepare(restored, request_id=request_id, folders=[["Child"]])
    restored.close()
    store.close()


def test_portable_checkpoint_retains_idempotent_preparation():
    store = DriveStore.create()
    request_id = str(uuid.uuid4())
    tree = prepare(store, request_id=request_id, folders=[["Child"]])
    restored = DriveStore._from_payload(store.export())
    assert prepare(restored, request_id=request_id, folders=[["Child"]]) == tree
    corrupt = copy.deepcopy(store.export())
    corrupt["folder_preparations"][0]["result"]["folders"]["Child"] = tree["folders"][
        ""
    ]
    with pytest.raises(CorruptStoreError, match="ancestry"):
        DriveStore._from_payload(corrupt)
    restored.close()
    store.close()


@pytest.fixture
def client(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    runtime = FakeRuntime(store)
    app = create_app(settings, runtime=runtime)
    with TestClient(app, base_url="https://testserver") as http:
        yield http, store
    runtime.transfers.registry.close()
    store.close()


def payload(**overrides):
    return {
        "destination": "/",
        "root_name": "Photos",
        "folders": [["Child"]],
        "client_request_id": str(uuid.uuid4()),
        **overrides,
    }


def test_tree_route_auth_body_schema_replay_and_conflict(client):
    http, store = client
    request = payload()
    assert http.post("/api/v1/folders/tree", json=request).status_code == 401
    login(http)
    first = http.post("/api/v1/folders/tree", json=request)
    assert first.status_code == 201
    assert http.post("/api/v1/folders/tree", json=request).json() == first.json()
    conflict = http.post("/api/v1/folders/tree", json=request | {"root_name": "Other"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "FOLDER_TREE_CONFLICT"
    bad = http.post("/api/v1/folders/tree", json=payload(folders=[[".."]]))
    assert bad.status_code == 400
    for invalid in (
        payload(folders="Child"),
        payload(folders=[[]]),
        payload(root_name=12),
        payload(extra="ignored?"),
    ):
        assert http.post("/api/v1/folders/tree", json=invalid).status_code == 422
    # Verbatim whitespace reaches store validation; the API must not strip it.
    assert (
        http.post(
            "/api/v1/folders/tree", json=payload(root_name=" leading")
        ).status_code
        == 400
    )
    oversized = payload(folders=[[str(n) + "x" * 100] for n in range(999)])
    assert http.post("/api/v1/folders/tree", json=oversized).status_code == 413
    assert len(store.get_directory("/")["contents"]) == 1


def test_tree_inherits_existing_folder_share_without_new_credentials(client):
    http, store = client
    parent = store.create_folder("/", "Shared")
    destination = "/" + parent["id"]
    token = store.create_share(destination)
    login(http)
    response = http.post("/api/v1/folders/tree", json=payload(destination=destination))
    assert response.status_code == 201
    child_path = response.json()["folders"]["Child"]
    assert store.authorize_directory(child_path, token) == destination
    assert len(store.list_shares()) == 1
    # A shared bearer token alone cannot mutate the tree.
    http.cookies.clear()
    denied = http.post(
        "/api/v1/folders/tree",
        json=payload(destination=destination),
        headers={"Authorization": "Bearer " + token},
    )
    assert denied.status_code == 401


@pytest.mark.parametrize("state", ["trashed", "deleting", "file"])
def test_new_tree_rejects_invalid_or_hidden_destination_without_writing(state):
    store = DriveStore.create()
    if state == "file":
        item = store.add_file("/", "file.txt", 123, 4)
    else:
        item = store.create_folder("/", "Destination")
        store.set_trashed("/" + item["id"], True)
        if state == "deleting":
            store.begin_deletion("/" + item["id"])
    before = store.export()
    with pytest.raises((ValidationError, NotDirectoryError, TrashedItemError)):
        prepare(store, destination="/" + item["id"], folders=[["Child"]])
    assert store.export() == before
    store.close()


@pytest.mark.parametrize("bad_path", ["/", "/a/", "/wrong/parent/child"])
def test_portable_tree_rejects_malformed_result_paths(bad_path):
    store = DriveStore.create()
    prepare(store, folders=[["Child"]])
    corrupt = copy.deepcopy(store.export())
    corrupt["folder_preparations"][0]["result"]["folders"]["Child"] = bad_path
    with pytest.raises(CorruptStoreError, match="ancestry"):
        DriveStore._from_payload(corrupt)
    store.close()


def test_existing_preparation_stays_replayable_after_files_are_uploaded():
    store = DriveStore.create()
    request_id = str(uuid.uuid4())
    tree = prepare(store, request_id=request_id, folders=[["Child"]])
    store.add_file(tree["folders"]["Child"], "photo.jpg", 123, 10)
    before = store.revision
    assert prepare(store, request_id=request_id, folders=[["Child"]]) == tree
    assert store.revision == before
    store.close()


def test_route_detects_changed_tree_and_does_not_create_another_root(client):
    http, store = client
    login(http)
    request = payload()
    tree = http.post("/api/v1/folders/tree", json=request).json()
    store.rename(tree["folders"]["Child"], "Changed")
    before = store.export()
    response = http.post("/api/v1/folders/tree", json=request)
    assert response.status_code == 409
    assert response.json()["code"] == "FOLDER_TREE_CONFLICT"
    assert store.export() == before
