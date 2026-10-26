"""Bulk owner actions either commit complete metadata or leave it unchanged."""

import sqlite3
from contextlib import closing

import pytest

from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login


def test_move_rolls_back_earlier_item_when_later_path_is_missing(app_client):
    client, store, _, _ = app_client
    item = store.add_file("/", "original.txt", 9101, 4)
    destination = store.create_folder("/", "Destination")
    snapshot = store.export()
    login(client)

    response = client.post(
        "/api/v1/nodes/move",
        json={
            "paths": ["/" + item["id"], "/" + "missing" * 8],
            "destination": "/" + destination["id"],
        },
    )

    assert response.status_code == 404
    assert store.export() == snapshot
    assert store.get_file("/" + item["id"])["name"] == "original.txt"


def test_trash_rollback_restores_shares_and_revision(app_client):
    client, store, _, _ = app_client
    item = store.add_file("/", "shared.txt", 9102, 4)
    path = "/" + item["id"]
    token = store.create_share(path)
    snapshot = store.export()
    login(client)

    response = client.post(
        "/api/v1/nodes/bulk",
        json={"paths": [path, "/" + "missing" * 8], "action": "trash"},
    )

    assert response.status_code == 404
    assert store.export() == snapshot
    assert store.authorize_file(path, token)


def test_restore_rollback_preserves_trash_names_and_shares(app_client):
    client, store, _, _ = app_client
    first = store.add_file("/", "same.txt", 9103, 4)
    first_path = "/" + first["id"]
    store.set_trashed(first_path, True)
    store.add_file("/", "same.txt", 9104, 5)
    parent = store.create_folder("/", "Parent")
    parent_path = "/" + parent["id"]
    child = store.add_file(parent_path, "child.txt", 9105, 6)
    child_path = parent_path + "/" + child["id"]
    store.set_trashed(child_path, True)
    store.set_trashed(parent_path, True)
    snapshot = store.export()
    login(client)

    response = client.post(
        "/api/v1/nodes/bulk",
        json={"paths": [first_path, child_path], "action": "restore"},
    )

    assert response.status_code == 404
    assert store.export() == snapshot
    assert store.list_trash()[first["id"]]["name"] == "same.txt"


@pytest.mark.parametrize("action", ["move", "trash", "restore"])
def test_sqlite_failure_cannot_commit_only_part_of_metadata_batch(app_client, action):
    client, store, _, _ = app_client
    first = store.add_file("/", "first.txt", 9106, 4)
    second = store.add_file("/", "second.txt", 9107, 5)
    destination = store.create_folder("/", "Destination")
    paths = ["/" + first["id"], "/" + second["id"]]
    if action == "restore":
        for path in paths:
            store.set_trashed(path, True)
    snapshot = store.export()
    with closing(sqlite3.connect(store.database_path)) as db:
        db.execute(
            f"""CREATE TRIGGER fail_second_bulk_item BEFORE UPDATE ON nodes
            WHEN OLD.id='{second["id"]}'
            BEGIN SELECT RAISE(ABORT,'simulated bulk storage failure'); END"""
        )
        db.commit()
    login(client)

    if action == "move":
        response = client.post(
            "/api/v1/nodes/move",
            json={"paths": paths, "destination": "/" + destination["id"]},
        )
    else:
        response = client.post(
            "/api/v1/nodes/bulk", json={"paths": paths, "action": action}
        )

    assert response.status_code == 500
    assert store.export() == snapshot


@pytest.mark.parametrize("action", ["trash", "restore", "move"])
def test_complete_metadata_batch_preserves_every_selected_identity(app_client, action):
    client, store, _, _ = app_client
    first = store.add_file("/", "first.txt", 9108, 4)
    second = store.add_file("/", "second.txt", 9109, 5)
    destination = store.create_folder("/", "Destination")
    paths = ["/" + first["id"], "/" + second["id"]]
    if action == "restore":
        for path in paths:
            store.set_trashed(path, True)
    login(client)

    if action == "move":
        response = client.post(
            "/api/v1/nodes/move",
            json={"paths": paths, "destination": "/" + destination["id"]},
        )
        actual = store.list_directory("/" + destination["id"])
    else:
        response = client.post(
            "/api/v1/nodes/bulk", json={"paths": paths, "action": action}
        )
        actual = store.list_trash() if action == "trash" else store.list_directory("/")

    assert response.status_code == 200
    assert [item["path"] for item in response.json()["results"]] == paths
    assert {first["id"], second["id"]} <= set(actual)
    parent = "/" + destination["id"] if action == "move" else ""
    assert store.message_ids(parent + "/" + first["id"], include_trashed=True) == [9108]
    assert store.message_ids(parent + "/" + second["id"], include_trashed=True) == [
        9109
    ]


def test_invalid_delete_selection_does_not_start_earlier_valid_item(app_client):
    client, store, runtime, _ = app_client
    item = store.add_file("/", "trashed.txt", 9110, 4)
    path = "/" + item["id"]
    store.set_trashed(path, True)
    snapshot = store.export()
    login(client)

    response = client.post(
        "/api/v1/nodes/bulk",
        json={"paths": [path, "/" + "missing" * 8], "action": "delete"},
    )

    assert response.status_code == 404
    assert store.export() == snapshot
    assert runtime.transfers.registry.list() == []
    assert runtime.transfers.deleted_messages == []
    assert store.pending_deletions() == []


def test_delete_selection_preflights_trash_policy_before_creating_jobs(app_client):
    client, store, runtime, _ = app_client
    trashed = store.add_file("/", "trashed.txt", 9111, 4)
    visible = store.add_file("/", "visible.txt", 9112, 4)
    path = "/" + trashed["id"]
    store.set_trashed(path, True)
    snapshot = store.export()
    login(client)

    response = client.post(
        "/api/v1/nodes/bulk",
        json={"paths": [path, "/" + visible["id"]], "action": "delete"},
    )

    assert response.status_code == 404
    assert store.export() == snapshot
    assert runtime.transfers.registry.list() == []
    assert store.pending_deletions() == []
