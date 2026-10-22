from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import test_app as fixtures

from tgdrive.errors import ValidationError
from tgdrive.share_links import token_factory
from tgdrive.store import DriveStore

app_client = fixtures.app_client


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


def test_v4_sqlite_migration_preserves_existing_tokens_and_export(tmp_path):
    target = tmp_path / "drive.sqlite3"
    store = DriveStore.create(target)
    folder = store.create_folder("/", "Legacy")
    path = "/" + folder["id"]
    token = store.create_share(path)
    original = store.export()
    store.close()
    with sqlite3.connect(target) as db:
        db.executescript("""BEGIN;
        ALTER TABLE shares RENAME TO shares_new;
        CREATE TABLE shares(id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL UNIQUE REFERENCES nodes(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, expires_at TEXT);
        INSERT INTO shares SELECT id,node_id,token_hash,created_at,expires_at
            FROM shares_new;
        DROP TABLE shares_new;
        CREATE INDEX shares_node ON shares(node_id);
        ALTER TABLE nodes DROP COLUMN starred;
        PRAGMA user_version=4; COMMIT;""")
    reopened = DriveStore.load(target)
    try:
        assert reopened.export() == original
        assert reopened.authorize_directory(path, token) == path
        other = reopened.create_share(path, label="New")
        assert reopened.authorize_directory(path, token) == path
        assert reopened.authorize_directory(path, other) == path
    finally:
        reopened.close()


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
