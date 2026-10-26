from __future__ import annotations

import pytest

import tests.support.app_fixtures as fixtures
from tgdrive.core.errors import ValidationError
from tgdrive.storage.store import DriveStore

app_client = fixtures.app_client


def check_starred(store):
    folder = store.create_folder("/", "Projects")
    parent = "/" + folder["id"]
    file = store.add_file(parent, "Report.pdf", 901, 20)
    target = parent + "/" + file["id"]
    store.set_starred(parent, True)
    store.set_starred(target, True)
    assert set(store.list_starred()) == {folder["id"], file["id"]}
    assert store.list_starred("REPORT")[file["id"]]["parent_names"] == ["Projects"]
    revision = store.revision
    store.set_starred(target, True)
    assert store.revision == revision
    store.rename(target, "Revised.pdf")
    destination = store.create_folder("/", "Destination")
    store.move(target, "/" + destination["id"])
    row = store.list_starred()[file["id"]]
    assert row["name"] == "Revised.pdf" and row["parent_names"] == ["Destination"]
    clone = DriveStore._from_payload(store.export())
    try:
        assert set(clone.list_starred()) == {folder["id"], file["id"]}
    finally:
        clone.close()
    store.set_trashed("/" + destination["id"], True)
    assert file["id"] not in store.list_starred()
    store.set_trashed("/" + destination["id"], False)
    assert file["id"] in store.list_starred()
    store.set_starred(store.resolve_id(file["id"]), False)
    assert set(store.list_starred()) == {folder["id"]}
    with pytest.raises(ValidationError):
        store.set_starred(parent, 1)


def test_starred_survives_paths_backup_and_reopen(tmp_path):
    path = tmp_path / "drive.sqlite3"
    store = DriveStore.create(path)
    check_starred(store)
    expected = store.export()
    store.close()
    reopened = DriveStore.load(path)
    assert reopened.export() == expected
    reopened.close()


def test_starred_api_owner_only_and_strict_boolean(app_client):
    client, store, _, _ = app_client
    folder = store.create_folder("/", "Star me")
    path = "/" + folder["id"]
    assert client.get("/api/v1/starred").status_code == 401
    fixtures.login(client)
    assert (
        client.patch(
            "/api/v1/starred", json={"path": path, "starred": True}
        ).status_code
        == 200
    )
    assert folder["id"] in client.get("/api/v1/starred").json()["data"]["contents"]
    assert client.get("/api/v1/starred?q=missing").json()["data"]["contents"] == {}
    assert (
        client.patch("/api/v1/starred", json={"path": path, "starred": 1}).status_code
        == 422
    )
    assert (
        client.patch(
            "/api/v1/starred", json={"path": path, "starred": False}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/starred").json()["data"]["contents"] == {}
