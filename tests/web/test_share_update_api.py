"""Partial share edits preserve expiry and concurrent edits in both catalogs."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import ROOT_ID, SCHEMA_VERSION, DriveStore
from tgdrive.web.app import create_app


@pytest.fixture(params=["sqlite", "mongo-cache"])
def share_client(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        store = DriveStore.create()
    else:
        # Exercise MongoStore's real transaction lock and share operations;
        # replace only cloud loading/indexes/persistence, with no network access.
        def load(instance):
            instance._committed = {
                "metadata": {
                    "schema_version": SCHEMA_VERSION,
                    "drive_id": "share-update-test",
                    "revision": 1,
                    "backed_up_revision": 1,
                    "generation": 0,
                    "storage_channel_id": None,
                },
                "nodes": {ROOT_ID: MongoStore._node(ROOT_ID, None, "/", "folder")},
                "shares": {},
                "operations": {},
                "deletions": {},
                "preparations": {},
            }

        monkeypatch.setattr(MongoStore, "_reload", load)
        monkeypatch.setattr(MongoStore, "_indexes", Mock())
        monkeypatch.setattr(MongoStore, "_commit", Mock())
        store = MongoStore(SimpleNamespace(check_owner=Mock()))
    runtime = FakeRuntime(store)
    try:
        app = create_app(make_settings(tmp_path), runtime=runtime)
        with TestClient(app, base_url="https://testserver") as client:
            login(client)
            node = store.add_file("/", "shared.txt", 123, 4)
            made = client.post(
                "/api/v1/shares",
                json={
                    "path": "/" + node["id"],
                    "label": "Recipient",
                    "expires_in_days": 1,
                },
            )
            assert made.status_code == 201
            yield client, store, made.json()
    finally:
        runtime.transfers.registry.close()
        store.close()


def current_share(client, share_id):
    response = client.get("/api/v1/shares")
    assert response.status_code == 200
    return next(row for row in response.json()["shares"] if row["id"] == share_id)


def test_label_only_edit_preserves_expiry_and_eventual_access_denial(
    share_client, monkeypatch
):
    client, _, share = share_client
    edited = client.patch(
        "/api/v1/shares", json={"share_id": share["id"], "label": "Renamed"}
    )
    assert edited.status_code == 200
    current = current_share(client, share["id"])
    assert current["label"] == "Renamed"
    assert current["expires_at"] == share["expires_at"]

    after_expiry = (
        (
            datetime.fromisoformat(share["expires_at"].replace("Z", "+00:00"))
            + timedelta(seconds=1)
        )
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    monkeypatch.setattr("tgdrive.storage.store._utc_now", lambda: after_expiry)
    monkeypatch.setattr("tgdrive.storage.mongo_store._utc_now", lambda: after_expiry)
    denied = client.head("/file", params={"path": share["path"], "auth": share["auth"]})
    assert denied.status_code == 403


@pytest.mark.parametrize("never", [False, True])
def test_expiry_only_edit_preserves_label_and_explicit_null_removes_expiry(
    share_client, never
):
    client, _, share = share_client
    expiry = None if never else (datetime.now(UTC) + timedelta(days=30)).isoformat()
    edited = client.patch(
        "/api/v1/shares", json={"share_id": share["id"], "expires_at": expiry}
    )
    assert edited.status_code == 200
    current = current_share(client, share["id"])
    assert current["label"] == "Recipient"
    if never:
        assert current["expires_at"] is None
    else:
        assert current["expires_at"] == (
            datetime.fromisoformat(expiry)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )


def test_explicit_empty_label_clears_only_the_label(share_client):
    client, _, share = share_client
    edited = client.patch("/api/v1/shares", json={"share_id": share["id"], "label": ""})
    assert edited.status_code == 200
    current = current_share(client, share["id"])
    assert current["label"] == ""
    assert current["expires_at"] == share["expires_at"]


def test_omitting_both_fields_does_not_change_share_or_revision(share_client):
    client, store, share = share_client
    before = current_share(client, share["id"])
    revision = store.revision
    assert (
        client.patch("/api/v1/shares", json={"share_id": share["id"]}).status_code
        == 200
    )
    assert current_share(client, share["id"]) == before
    assert store.revision == revision


def test_concurrent_label_and_expiry_edits_preserve_both_changes(
    share_client, monkeypatch
):
    client, store, share = share_client
    ready = Barrier(2)
    update = store.update_share

    def together(share_id, **changes):
        # Both requests reach the store before either writes. Merging old values
        # in the route would overwrite one request's change at this point.
        ready.wait(timeout=5)
        return update(share_id, **changes)

    monkeypatch.setattr(store, "update_share", together)
    expiry = (datetime.now(UTC) + timedelta(days=30)).isoformat(timespec="seconds")
    patches = [{"label": "Renamed"}, {"expires_at": expiry}]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(
                lambda patch: client.patch(
                    "/api/v1/shares", json={"share_id": share["id"], **patch}
                ),
                patches,
            )
        )
    assert [result.status_code for result in results] == [200, 200]
    current = current_share(client, share["id"])
    assert current["label"] == "Renamed"
    assert current["expires_at"] == expiry.replace("+00:00", "Z")
