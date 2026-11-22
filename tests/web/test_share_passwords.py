from fastapi.testclient import TestClient

from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login


def test_share_password_unlock_scopes_all_public_reads_and_expires(
    app_client, monkeypatch
):
    owner, store, _, _ = app_client
    login(owner)
    folder = store.create_folder("/", "Hidden reports")
    parent = "/" + folder["id"]
    file = store.add_file(parent, "report.pdf", 100, 100)
    other = store.add_file("/", "private.pdf", 101, 100)
    password = "fixture-share-password"
    creation = owner.post("/api/v1/shares", json={"path": parent, "password": password})
    assert creation.status_code == 201, creation.text
    share = creation.json()
    assert share["password_required"]
    assert password not in creation.text
    assert "password_hash" not in owner.get("/api/v1/shares").text
    assert password not in repr(store.export())
    auth = share["auth"]
    file_params = {"path": "/" + file["id"], "auth": auth}
    folder_params = {"parent_id": parent, "auth": auth}
    guest = TestClient(owner.app, base_url="https://testserver")
    try:
        for route in (
            "/api/v1/embed",
            "/api/v1/preview",
            "/api/v1/thumbnail",
            "/api/v1/books",
            "/api/v1/file-status",
            "/file",
        ):
            response = guest.get(
                route,
                params={**file_params, "request_id": "fixture-request"}
                if route == "/api/v1/file-status"
                else file_params,
            )
            assert response.status_code == 403, (route, response.text)
            assert response.json()["code"] == "SHARE_PASSWORD_REQUIRED"
        assert guest.head("/file", params=file_params).status_code == 403
        assert (
            guest.get("/api/v1/shared/nodes", params=folder_params).status_code == 403
        )
        assert (
            guest.post(
                "/api/getDirectory",
                json={"path": "/share_" + folder["id"], "auth": auth},
            ).status_code
            == 403
        )

        def unlock(value):
            return guest.post(
                "/api/v1/shared/unlock", json={"auth": auth, "password": value}
            )

        wrong = unlock("wrong")
        assert wrong.status_code == 403
        assert "set-cookie" not in wrong.headers
        correct = unlock(password)
        assert correct.status_code == 200, correct.text
        for attribute in ("httponly", "secure", "partitioned", "samesite=none"):
            assert attribute in correct.headers["set-cookie"].lower()
        assert (
            guest.get("/api/v1/shared/nodes", params=folder_params).status_code == 200
        )
        assert guest.get("/api/v1/embed", params=file_params).status_code == 200
        assert guest.get("/api/v1/preview", params=file_params).status_code == 200
        assert guest.head("/file", params=file_params).status_code == 200
        assert (
            guest.post(
                "/api/getDirectory",
                json={"path": "/share_" + folder["id"], "auth": auth},
            ).status_code
            == 200
        )
        assert (
            guest.head(
                "/file", params={"path": "/" + other["id"], "auth": auth}
            ).status_code
            == 403
        )

        second = owner.post(
            "/api/v1/shares", json={"path": parent, "password": password}
        ).json()
        assert (
            guest.head(
                "/file", params={**file_params, "auth": second["auth"]}
            ).status_code
            == 403
        )
        assert (
            owner.patch(
                "/api/v1/shares",
                json={"share_id": share["id"], "password": "replacement"},
            ).status_code
            == 200
        )
        assert guest.head("/file", params=file_params).status_code == 403
        assert unlock(password).status_code == 403
        assert unlock("replacement").status_code == 200
        assert guest.head("/file", params=file_params).status_code == 200
        with monkeypatch.context() as clock:
            clock.setattr("tgdrive.web.share_links.SHARE_UNLOCK_TTL", -1)
            assert unlock("replacement").status_code == 200
            assert guest.head("/file", params=file_params).status_code == 403
        assert unlock("replacement").status_code == 200
        assert (
            owner.request(
                "DELETE", "/api/v1/shares", json={"share_id": share["id"]}
            ).status_code
            == 200
        )
        assert guest.head("/file", params=file_params).status_code == 403
        assert unlock("replacement").status_code == 403

        assert (
            owner.patch(
                "/api/v1/shares", json={"share_id": second["id"], "password": None}
            ).status_code
            == 200
        )
        assert (
            guest.head(
                "/file", params={**file_params, "auth": second["auth"]}
            ).status_code
            == 200
        )
    finally:
        guest.close()


def test_open_count_uses_public_scope_without_exposing_owner_statistics(
    app_client, monkeypatch
):
    owner, store, _, _ = app_client
    login(owner)
    folder = store.create_folder("/", "Protected")
    parent = "/" + folder["id"]
    child = store.add_file(parent, "public.pdf", 101, 32)
    outside = store.add_file("/", "outside.pdf", 102, 32)
    share = owner.post(
        "/api/v1/shares", json={"path": parent, "password": "original"}
    ).json()
    token = share["auth"]
    revision = store.revision
    opens = "/api/v1/shared/opens"
    with TestClient(owner.app, base_url="https://testserver") as guest:

        def event(path=parent, identity="a" * 32):
            return guest.post(
                opens, json={"path": path, "auth": token, "event_id": identity}
            )

        assert guest.get("/api/v1/shares").status_code == 401
        assert event().status_code == 403
        assert event(identity="not-an-id").status_code == 422
        assert store.list_shares()[0]["open_count"] == 0
        assert guest.post(
            "/api/v1/shared/unlock", json={"auth": token, "password": "original"}
        ).is_success
        nodes = guest.get(
            "/api/v1/shared/nodes", params={"parent_id": parent, "auth": token}
        )
        assert nodes.is_success
        assert "open_count" not in nodes.text and "opens_since" not in nodes.text
        assert event(path="/" + outside["id"]).status_code == 403
        assert event().status_code == 204
        assert event(path="/" + child["id"]).status_code == 204
        assert event(identity="b" * 32).status_code == 204
        assert store.list_shares()[0]["open_count"] == 2
        assert store.revision == revision
        assert owner.patch(
            "/api/v1/shares", json={"share_id": share["id"], "password": "replacement"}
        ).is_success
        assert event(identity="c" * 32).status_code == 403
        assert store.list_shares()[0]["open_count"] == 2
        assert guest.post(
            "/api/v1/shared/unlock", json={"auth": token, "password": "replacement"}
        ).is_success
        with monkeypatch.context() as clock:
            expiry = store.list_shares()[0]["expires_at"]
            clock.setattr("tgdrive.storage.mongo_store._utc_now", lambda: expiry)
            assert event(identity="c" * 32).status_code == 403
            assert store.list_shares(include_expired=True)[0]["open_count"] == 2
        assert owner.request(
            "DELETE", "/api/v1/shares", json={"share_id": share["id"]}
        ).is_success
        assert event(identity="d" * 32).status_code == 403
        assert not store.list_shares()
