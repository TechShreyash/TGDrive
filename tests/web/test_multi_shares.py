from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

import tests.support.app_fixtures as fixtures
import tests.web.test_share_update_api as share_update_fixtures
from tgdrive.core.errors import ValidationError
from tgdrive.storage.store import DriveStore
from tgdrive.web.share_links import token_factory

app_client = fixtures.app_client
share_client = share_update_fixtures.share_client


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_same_item_copy_edit_and_revoke_preserve_other_links(share_client, kind):
    client, store, _ = share_client
    if kind == "folder":
        node = store.create_folder("/", "Client assets")
        path = "/" + node["id"]
        child = store.add_file(path, "design.pdf", 12, 42)
        file_path = path + "/" + child["id"]
    else:
        node = store.add_file("/", "design.pdf", 12, 42)
        path = file_path = "/" + node["id"]

    links = []
    for label, days in (("Client review", 1), ("Team reference", 30)):
        response = client.post(
            "/api/v1/shares",
            json={"path": path, "label": label, "expires_in_days": days},
        )
        assert response.status_code == 201
        links.append(response.json())
    first, second = links
    assert first["id"] != second["id"] and first["auth"] != second["auth"]
    rows = {row["id"]: row for row in client.get("/api/v1/shares").json()["shares"]}
    other_before = rows[second["id"]]
    assert rows[first["id"]]["node_id"] == other_before["node_id"] == node["id"]
    assert rows[first["id"]]["expires_at"] != other_before["expires_at"]
    assert rows[first["id"]]["label"] == "Client review"
    assert other_before["label"] == "Team reference"
    assert all(rows[link["id"]]["copyable"] for link in links)

    before_copy = store.export()
    for link in links:
        response = client.post("/api/v1/shares/link", json={"share_id": link["id"]})
        assert response.status_code == 200
        assert response.json()["auth"] == link["auth"]
    assert store.export() == before_copy

    assert (
        client.patch(
            "/api/v1/shares",
            json={"share_id": first["id"], "label": "Final review", "expires_at": None},
        ).status_code
        == 200
    )
    edited = {row["id"]: row for row in client.get("/api/v1/shares").json()["shares"]}
    assert edited[first["id"]]["label"] == "Final review"
    assert edited[first["id"]]["expires_at"] is None
    assert edited[second["id"]] == other_before
    assert all(store.authorize_file(file_path, link["auth"]) for link in links)

    assert (
        client.request(
            "DELETE", "/api/v1/shares", json={"share_id": first["id"]}
        ).status_code
        == 200
    )
    assert not store.authorize_file(file_path, first["auth"])
    assert store.authorize_file(file_path, second["auth"])
    remaining = {
        row["id"]: row for row in client.get("/api/v1/shares").json()["shares"]
    }
    assert first["id"] not in remaining
    assert remaining[second["id"]] == other_before
    assert (
        client.post("/api/v1/shares/link", json={"share_id": second["id"]}).json()[
            "auth"
        ]
        == second["auth"]
    )


def test_root_share_can_be_copied_and_revoked_by_path(share_client):
    client, store, independent = share_client
    response = client.post("/api/v1/shares", json={"path": "/", "label": "Whole drive"})
    assert response.status_code == 201
    share = response.json()
    copied = client.post("/api/v1/shares/link", json={"path": "/"})
    assert copied.status_code == 200
    assert copied.json()["path"] == "/"
    assert copied.json()["auth"] == share["auth"]
    assert store.authorize_directory("/", share["auth"]) == "/"

    assert client.post("/api/v1/shares/link", json={"path": "///"}).status_code == 400
    invalid = client.request("DELETE", "/api/v1/shares", json={"path": "///"})
    assert invalid.status_code == 400
    assert store.authorize_directory("/", share["auth"]) == "/"

    revoked = client.request("DELETE", "/api/v1/shares", json={"path": "/"})

    assert revoked.status_code == 200
    assert not store.authorize_directory("/", share["auth"])
    assert client.post("/api/v1/shares/link", json={"path": "/"}).status_code == 410
    remaining = {row["id"] for row in client.get("/api/v1/shares").json()["shares"]}
    assert independent["id"] in remaining
    assert share["id"] not in remaining


@pytest.mark.parametrize("kind", ["file", "folder"])
@pytest.mark.parametrize("unavailable", ["random", "changed-secret", "expired"])
def test_uncopyable_and_expired_links_never_mint_or_replace_links(
    share_client, monkeypatch, kind, unavailable
):
    client, store, _ = share_client
    if kind == "folder":
        node = store.create_folder("/", "Recipient files")
        path = "/" + node["id"]
        child = store.add_file(path, "notes.txt", 12, 42)
        file_path = path + "/" + child["id"]
    else:
        node = store.add_file("/", "notes.txt", 12, 42)
        path = file_path = "/" + node["id"]
    current_settings = client.app.state.settings
    original_factory = token_factory(current_settings)
    factory = (
        None
        if unavailable == "random"
        else token_factory(
            current_settings.model_copy(
                update={"session_secret": SecretStr("previous-owner-secret-" * 3)}
            )
        )
        if unavailable == "changed-secret"
        else original_factory
    )
    old_token = store.create_share(path, token_factory=factory, label="Recipient")
    old_row = store.list_shares(path, token_factory=original_factory)[0]
    surviving_response = client.post(
        "/api/v1/shares",
        json={"path": path, "label": "Independent recipient", "expires_in_days": None},
    )
    assert surviving_response.status_code == 201
    surviving = surviving_response.json()
    if unavailable == "expired":
        later = (
            (
                datetime.fromisoformat(old_row["expires_at"].replace("Z", "+00:00"))
                + timedelta(seconds=1)
            )
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        monkeypatch.setattr("tgdrive.storage.store._utc_now", lambda: later)
        monkeypatch.setattr("tgdrive.storage.mongo_store._utc_now", lambda: later)

    before = store.export()
    response = client.post("/api/v1/shares/link", json={"share_id": old_row["id"]})
    assert response.status_code == (410 if unavailable == "expired" else 409)
    if unavailable != "expired":
        assert response.json()["detail"] == (
            "The original link URL is unavailable. Existing copies still "
            "work until this link expires or is revoked."
        )
    assert store.export() == before
    assert store.authorize_file(file_path, old_token) == (unavailable != "expired")
    assert store.authorize_file(file_path, surviving["auth"])
    assert (
        client.post("/api/v1/shares/link", json={"share_id": surviving["id"]}).json()[
            "auth"
        ]
        == surviving["auth"]
    )
    assert store.export() == before


@pytest.mark.asyncio
async def test_showcase_seeds_named_copyable_links_with_distinct_expiries(tmp_path):
    import asyncio

    from tests.support.showcase_data import seed

    async def load_cache(_key, _size, loader):
        return await loader()

    store = DriveStore.create()
    settings = fixtures.make_settings(tmp_path)
    transfers = fixtures.FakeTransfers()
    runtime = SimpleNamespace(
        store=store,
        payloads={113: b"Synthetic PDF content"},
        transfers=transfers,
        preloads=SimpleNamespace(
            cache=SimpleNamespace(
                register_file=Mock(), get=load_cache, flush=AsyncMock()
            )
        ),
    )
    try:
        await seed(runtime, settings)
        factory = token_factory(settings)
        rows = store.list_shares(token_factory=factory)
        assert {row["label"] for row in rows} == {"Project review", "Design handoff"}
        assert len({row["expires_at"] for row in rows}) == 2
        assert all(row["copyable"] for row in rows)
        assert all(
            store.authorize_file(row["path"], factory(row["id"])) for row in rows
        )
    finally:
        task = getattr(runtime, "showcase_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        transfers.registry.close()
        store.close()


def test_independent_links_copy_edit_revoke_and_restore(app_client):
    client, store, _, settings = app_client
    fixtures.login(client)
    folder = store.create_folder("/", "Shared")
    path = "/" + folder["id"]
    file = store.add_file(path, "book.epub", 10, 42)
    file_path = path + "/" + file["id"]
    first = client.post(
        "/api/v1/shares", json={"path": path, "label": "Alex", "expires_in_days": 1}
    ).json()
    second = client.post(
        "/api/v1/shares",
        json={"path": path, "label": "Design team", "expires_in_days": 30},
    ).json()
    assert first["auth"] != second["auth"] and first["id"] != second["id"]
    assert store.authorize_file(file_path, first["auth"])
    assert store.authorize_file(file_path, second["auth"])
    assert client.post("/api/v1/shares/link", json={"path": path}).status_code == 409
    copied = client.post("/api/v1/shares/link", json={"share_id": first["id"]})
    assert copied.json()["auth"] == first["auth"]
    expiry = (datetime.now(UTC) + timedelta(days=90)).isoformat()
    assert (
        client.patch(
            "/api/v1/shares",
            json={
                "share_id": first["id"],
                "label": "Alex review",
                "expires_at": expiry,
            },
        ).status_code
        == 200
    )
    rows = client.get("/api/v1/shares").json()["shares"]
    assert next(r for r in rows if r["id"] == first["id"])["label"] == "Alex review"
    assert next(r for r in rows if r["id"] == second["id"])["label"] == "Design team"
    restored = DriveStore._from_payload(store.export())
    try:
        assert restored.authorize_file(file_path, first["auth"])
        assert restored.authorize_file(file_path, second["auth"])
        assert restored.list_shares(token_factory=token_factory(settings))[0][
            "copyable"
        ]
    finally:
        restored.close()
    assert (
        client.request(
            "DELETE", "/api/v1/shares", json={"share_id": first["id"]}
        ).status_code
        == 200
    )
    assert not store.authorize_file(file_path, first["auth"])
    assert store.authorize_file(file_path, second["auth"])
    assert (
        client.post("/api/v1/shares/link", json={"share_id": first["id"]}).status_code
        == 410
    )
    store.set_trashed(path, True)
    assert not store.authorize_file(file_path, second["auth"])


def test_custom_expiry_labels_authorization_and_download_link_scope(app_client):
    client, store, _, _ = app_client
    file = store.add_file("/", "report.pdf", 11, 10)
    path = "/" + file["id"]
    assert client.post("/api/v1/shares", json={"path": path}).status_code == 401
    fixtures.login(client)
    expiry = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    response = client.post(
        "/api/v1/shares", json={"path": path, "label": "Pat", "expires_at": expiry}
    )
    assert response.status_code == 201
    personal = response.json()
    download = client.post("/api/v1/download-links", json={"path": path})
    assert download.status_code == 200
    assert personal["auth"] not in download.json()["url"]
    assert (
        client.post("/api/v1/download-links", json={"path": path}).json()["url"]
        == download.json()["url"]
    )
    assert store.authorize_file(path, personal["auth"])
    assert (
        client.post(
            "/api/v1/shares", json={"path": path, "label": "x" * 81}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/shares", json={"path": path, "expires_at": "2000-01-01T00:00:00Z"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/shares", json={"path": path, "expires_at": "2035-01-01T00:00:00"}
        ).status_code
        == 400
    )
    assert client.request("DELETE", "/api/v1/shares", json={}).status_code == 422
    client.post("/api/logout")
    assert (
        client.patch(
            "/api/v1/shares", json={"share_id": personal["id"], "label": "stolen"}
        ).status_code
        == 401
    )


def test_invalid_labels_and_legacy_rotation_are_explicit():
    store = DriveStore.create()
    folder = store.create_folder("/", "Share")
    path = "/" + folder["id"]
    first = store.create_share(path)
    second = store.create_share(path)
    with pytest.raises(ValidationError):
        store.create_share(path, label="x\x00y")
    replacement = store.create_share(path, replace_existing=True)
    assert not store.authorize_directory(path, first)
    assert not store.authorize_directory(path, second)
    assert store.authorize_directory(path, replacement) == path
