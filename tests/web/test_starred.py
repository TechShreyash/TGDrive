from __future__ import annotations

import pytest

import tests.support.app_fixtures as fixtures
from tests.support.mongo_fixtures import new_store, reopen_store, store_from_payload
from tgdrive.core.errors import ValidationError

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
    clone = store_from_payload(store.export())
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
    path = tmp_path / "drive"
    store = new_store(path)
    check_starred(store)
    expected = store.export()
    store.close()
    reopened = reopen_store(path)
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


def test_owner_listings_page_without_losing_tied_rows(app_client):
    client, store, _, _ = app_client
    fixtures.login(client)
    for index in range(3):
        folder = store.create_folder("/", f"Folder {index}")
        base = "/" + folder["id"]
        node = store.add_file(base, "same.txt", index + 1, 3)
        path = base + "/" + node["id"]
        store.set_starred(path, True)
        store.create_share(path)
        trashed = store.add_file(base, "trashed.txt", index + 10, 3)
        store.set_trashed(base + "/" + trashed["id"], True)

    for view in ("starred", "trash", "shares"):
        endpoint = "/api/v1/" + view

        def identifiers(payload, view=view):
            if view == "shares":
                return [share["id"] for share in payload["shares"]]
            return list(payload["data"]["contents"])

        legacy = client.get(endpoint).json()
        assert "next_cursor" not in legacy
        params = {"limit": "1"}
        seen = []
        for page_number in range(3):
            response = client.get(endpoint, params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            items = identifiers(page)
            assert len(items) == 1
            seen.extend(items)
            if page_number < 2:
                assert page["next_cursor"]
                params["cursor"] = page["next_cursor"]
            else:
                assert page["next_cursor"] is None
        assert seen == identifiers(legacy)
        assert client.get(endpoint, params={"cursor": "invalid"}).status_code == 422
