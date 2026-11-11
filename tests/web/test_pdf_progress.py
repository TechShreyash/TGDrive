from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login
from tests.web.test_demo_admin_auth import ADMIN, VISITOR, auth_client, sign_in

ORIGIN = {"Origin": "https://testserver"}


def path(identity):
    return "/api/v1/files/" + identity + "/pdf-progress"


def body(writer, sequence=1, page=2, count=3, **extra):
    return {
        "session_id": writer,
        "sequence": sequence,
        "page_number": page,
        "page_count": count,
        **extra,
    }


@pytest.fixture
def pdf(app_client, monkeypatch):
    client, store, _runtime, _settings = app_client
    clock = [1000.0]
    monkeypatch.setattr(
        "tgdrive.storage.mongo_store.time", SimpleNamespace(time=lambda: clock[0])
    )
    identity = store.add_file("/", "reading.pdf", 7, 3)["id"]
    return client, store, identity, clock


def test_pdf_cross_context_resume_ordering_and_final_retry(pdf):
    client, _store, identity, clock = pdf
    url = path(identity)
    assert client.get(url).status_code == 401
    login(client)
    with TestClient(client.app, base_url="https://testserver") as second:
        login(second)
        assert client.get(url).json() == {"progress": None}
        first = client.post(url + "/session", json={}, headers=ORIGIN).json()
        assert first["save_interval_seconds"] == 5
        writer = first["session_id"]
        saved = client.put(url, json=body(writer), headers=ORIGIN)
        assert saved.status_code == 200 and saved.headers["cache-control"] == "no-store"
        assert second.get(url).json() == saved.json()
        limited = client.put(url, json=body(writer, 2, 3), headers=ORIGIN)
        assert limited.status_code == 429 and limited.headers["retry-after"] == "5"
        assert limited.json()["code"] == "PDF_PROGRESS_RATE_LIMITED"
        clock[0] += 1
        newer = second.post(url + "/session", json={}, headers=ORIGIN).json()
        assert newer["progress"] == saved.json()["progress"]
        conflict = client.put(url, json=body(writer, 9, 1, final=True), headers=ORIGIN)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "PDF_PROGRESS_SESSION_CHANGED"
        writer = newer["session_id"]
        final = second.put(url, json=body(writer, 2, 3, final=True), headers=ORIGIN)
        assert final.status_code == 200
        assert (
            second.put(url, json=body(writer, 2, 3, final=True), headers=ORIGIN).json()
            == final.json()
        )
        stale = second.put(url, json=body(writer, 1, 1), headers=ORIGIN)
        assert (
            stale.status_code == 409
            and stale.json()["code"] == "PDF_PROGRESS_SEQUENCE_STALE"
        )
        assert "session_id" not in client.get(url).text


@pytest.mark.parametrize(
    "change",
    [
        {"page_number": 0},
        {"page_number": 4},
        {"page_count": 10_001},
        {"page_number": True},
        {"page_count": 3.0},
        {"sequence": True},
        {"final": 1},
        {"auth": "token"},
    ],
)
def test_invalid_pdf_request_never_writes(pdf, change):
    client, store, identity, _clock = pdf
    login(client)
    url = path(identity)
    writer = client.post(url + "/session", json={}, headers=ORIGIN).json()["session_id"]
    assert (
        client.put(url, json=body(writer) | change, headers=ORIGIN).status_code == 422
    )
    assert store.pdf_progress(identity) is None


def test_pdf_share_queries_and_public_listing_never_disclose_owner_progress(pdf):
    client, store, identity, _clock = pdf
    login(client)
    url = path(identity)
    writer = client.post(url + "/session", json={}, headers=ORIGIN).json()["session_id"]
    assert client.put(url, json=body(writer), headers=ORIGIN).status_code == 200
    folder = store.create_folder("/", "Shared")
    store.move("/" + identity, "/" + folder["id"])
    token = client.post(
        "/api/getFolderShareAuth", json={"path": "/" + folder["id"]}, headers=ORIGIN
    ).json()["auth"]
    denied = client.get(url, params={"auth": token})
    assert (
        denied.status_code == 403 and denied.json()["code"] == "PDF_PROGRESS_OWNER_ONLY"
    )
    client.post("/api/logout", headers=ORIGIN)
    assert client.get(url, params={"auth": token}).status_code == 401
    listing = client.post(
        "/api/getDirectory",
        json={"path": "/share_" + folder["id"], "auth": token},
        headers=ORIGIN,
    )
    assert listing.status_code == 200
    assert "pdf_progress" not in listing.text and writer not in listing.text


@pytest.mark.parametrize("password,allowed", [(VISITOR, False), (ADMIN, True)])
def test_pdf_demo_owner_boundary(tmp_path, password, allowed):
    with auth_client(tmp_path) as (client, _settings, runtime):
        identity = runtime.store.add_file("/", "reading.pdf", 7, 3)["id"]
        sign_in(client, password)
        assert client.get(path(identity)).status_code == (200 if allowed else 403)
        begin = client.post(path(identity) + "/session", json={}, headers=ORIGIN)
        assert begin.status_code == (200 if allowed else 403)
        if not allowed:
            assert begin.json()["code"] == "PDF_PROGRESS_OWNER_ONLY"
            node = runtime.store.db.nodes.find_one({"_id": identity})
            assert node is not None and "pdf_progress_session" not in node
