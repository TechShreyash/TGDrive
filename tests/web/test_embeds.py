"""Only public embed pages may be framed; file metadata always needs a capability."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login


def test_only_successful_embed_page_allows_external_frames(app_client):
    client, _, _, _ = app_client
    embedded = client.get("/embed")
    assert embedded.status_code == 200
    assert embedded.text == client.get("/").text
    assert "frame-ancestors *;" in embedded.headers["content-security-policy"]
    assert "x-frame-options" not in embedded.headers
    assert embedded.headers["referrer-policy"] == "no-referrer"
    assert embedded.headers["cache-control"] == "no-store"

    for path in ("/", "/?embed=1", "/embed/missing", "/api/v1/embed"):
        response = client.get(path)
        assert "frame-ancestors 'none';" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY"
    rejected = client.post("/embed")
    assert rejected.status_code == 405
    assert rejected.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_file_embed_metadata_is_minimal_and_requires_a_capability(app_client, kind):
    client, store, _, _ = app_client
    folder = store.create_folder("/", "Private parent name")
    folder_path = "/" + folder["id"]
    node = store.add_file(folder_path, "report.pdf", 123, 456)
    file_path = folder_path + "/" + node["id"]
    auth = store.create_share(folder_path if kind == "folder" else file_path)
    private = store.add_file("/", "private.txt", 124, 8)
    params = {"path": "/" + node["id"], "auth": auth}
    login(client)

    for token, status in ((None, 422), ("", 422), ("invalid", 403)):
        invalid = {"path": params["path"]}
        if token is not None:
            invalid["auth"] = token
        assert client.get("/api/v1/embed", params=invalid).status_code == status

    for authenticated in (True, False):
        if not authenticated:
            client.cookies.clear()
        response = client.get("/api/v1/embed", params=params)
        assert response.status_code == 200
        assert response.json() == {
            "id": node["id"],
            "name": "report.pdf",
            "path": "",
            "type": "file",
            "size": 456,
            "upload_date": node["upload_date"],
        }
        assert response.headers["cache-control"] == "no-store"
        assert (
            client.get(
                "/api/v1/embed", params={"path": "/" + private["id"], "auth": auth}
            ).status_code
            == 403
        )


@pytest.mark.parametrize("kind", ["file", "folder"])
@pytest.mark.parametrize("unavailable", ["expired", "revoked", "trashed"])
def test_embed_reuses_share_expiry_revocation_and_trash_rules(
    app_client, monkeypatch, kind, unavailable
):
    client, store, _, _ = app_client
    folder = store.create_folder("/", "Shared folder")
    folder_path = "/" + folder["id"]
    node = store.add_file(folder_path, "report.pdf", 123, 456)
    file_path = folder_path + "/" + node["id"]
    share_path = folder_path if kind == "folder" else file_path
    auth = store.create_share(share_path)
    assert (
        client.get(
            "/api/v1/embed", params={"path": "/" + node["id"], "auth": auth}
        ).status_code
        == 200
    )

    if unavailable == "expired":
        later = (datetime.now(UTC) + timedelta(days=8)).isoformat()
        monkeypatch.setattr("tgdrive.storage.store._utc_now", lambda: later)
    elif unavailable == "revoked":
        store.revoke_share(store.list_shares(share_path)[0]["id"])
    else:
        store.set_trashed(share_path, True)

    login(client)
    assert (
        client.get(
            "/api/v1/embed", params={"path": "/" + node["id"], "auth": auth}
        ).status_code
        == 403
    )
    assert (
        client.head(
            "/file", params={"path": "/" + node["id"], "auth": auth}
        ).status_code
        == 403
    )
    if kind == "folder":
        assert (
            client.post(
                "/api/getDirectory",
                json={"path": "/share_" + folder["id"], "auth": auth},
            ).status_code
            == 403
        )
