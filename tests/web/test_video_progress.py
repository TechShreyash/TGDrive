from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login
from tests.web.test_demo_admin_auth import ADMIN, VISITOR, auth_client, sign_in
from tgdrive.web.video_progress import _worker

ORIGIN = {"Origin": "https://testserver"}


def path(identity):
    return "/api/v1/files/" + identity + "/video-progress"


def body(session, sequence=1, position=20, duration=200, **extra):
    return {
        "session_id": session,
        "sequence": sequence,
        "position_seconds": position,
        "duration_seconds": duration,
        **extra,
    }


@pytest.fixture
def video(app_client, monkeypatch):
    client, store, _runtime, _settings = app_client
    clock = [1000.0]
    monkeypatch.setattr(
        "tgdrive.storage.mongo_store.time", SimpleNamespace(time=lambda: clock[0])
    )
    item = store.add_file("/", "movie.mp4", 7, 3)
    return client, store, item["id"], clock


def test_two_authenticated_contexts_resume_but_old_writer_cannot_overwrite(video):
    client, _store, identity, clock = video
    url = path(identity)
    login(client)
    with TestClient(client.app, base_url="https://testserver") as second:
        login(second)
        assert client.get(url).json() == {"progress": None}
        first = client.post(url + "/session", json={}, headers=ORIGIN).json()
        assert first["save_interval_seconds"] == 5 and first["progress"] is None
        response = client.put(url, json=body(first["session_id"]), headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert second.get(url).json()["progress"]["position_seconds"] == 20
        clock[0] += 1
        writer = second.post(url + "/session", json={}, headers=ORIGIN).json()
        assert writer["progress"]["position_seconds"] == 20
        stale = client.put(
            url, json=body(first["session_id"], 100, 10, final=True), headers=ORIGIN
        )
        assert (
            stale.status_code == 409
            and stale.json()["code"] == "VIDEO_PROGRESS_SESSION_CHANGED"
        )
        assert (
            second.put(
                url, json=body(writer["session_id"], 2, 80), headers=ORIGIN
            ).status_code
            == 200
        )
        older = second.put(url, json=body(writer["session_id"], 1, 20), headers=ORIGIN)
        assert (
            older.status_code == 409
            and older.json()["code"] == "VIDEO_PROGRESS_SEQUENCE_STALE"
        )
        assert client.get(url).json()["progress"]["position_seconds"] == 80
        final = second.put(
            url, json=body(writer["session_id"], 3, 0, final=True), headers=ORIGIN
        )
        assert (
            final.status_code == 200
            and final.json()["progress"]["position_seconds"] == 0
        )
        assert "session_id" not in client.get(url).text


def test_pacing_errors_do_not_consume_sequence_and_final_retry_is_idempotent(video):
    client, _store, identity, _clock = video
    login(client)
    url = path(identity)
    writer = client.post(url + "/session", json={}, headers=ORIGIN).json()["session_id"]
    limited = client.post(url + "/session", json={}, headers=ORIGIN)
    assert limited.status_code == 429 and limited.headers["retry-after"] == "1"
    assert limited.json()["code"] == "VIDEO_PROGRESS_RATE_LIMITED"
    assert client.put(url, json=body(writer), headers=ORIGIN).status_code == 200
    limited = client.put(url, json=body(writer, 2, 30), headers=ORIGIN)
    assert limited.status_code == 429 and limited.headers["retry-after"] == "5"
    assert limited.json()["retry_after"] == 5
    final = client.put(url, json=body(writer, 2, 30, final=True), headers=ORIGIN)
    assert final.status_code == 200
    duplicate = client.put(url, json=body(writer, 2, 30, final=True), headers=ORIGIN)
    assert duplicate.json() == final.json()
    assert client.put(url, json=body(writer, 3, 40), headers=ORIGIN).status_code == 409


@pytest.mark.parametrize(
    "change",
    [
        {"position_seconds": -1},
        {"position_seconds": 201},
        {"duration_seconds": 0},
        {"sequence": True},
        {"position_seconds": True},
        {"final": 1},
        {"auth": "share-token"},
    ],
)
def test_invalid_progress_body_does_not_write(video, change):
    client, store, identity, _clock = video
    login(client)
    url = path(identity)
    writer = client.post(url + "/session", json={}, headers=ORIGIN).json()["session_id"]
    response = client.put(url, json=body(writer) | change, headers=ORIGIN)
    assert response.status_code == 422, response.text
    assert store.video_progress(identity) is None


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1e999"])
def test_nonfinite_json_seconds_are_rejected_without_echo(video, number):
    client, store, identity, _clock = video
    login(client)
    url = path(identity)
    writer = client.post(url + "/session", json={}, headers=ORIGIN).json()["session_id"]
    raw = json.dumps(body(writer)).replace(
        '"position_seconds": 20', '"position_seconds": ' + number
    )
    response = client.put(
        url, content=raw, headers=ORIGIN | {"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    assert store.video_progress(identity) is None


def test_share_authority_never_grants_progress_or_discloses_node_metadata(video):
    client, store, identity, _clock = video
    login(client)
    url = path(identity)
    writer = client.post(url + "/session", json={}, headers=ORIGIN).json()["session_id"]
    client.put(url, json=body(writer), headers=ORIGIN)
    folder = store.create_folder("/", "Shared")
    store.move("/" + identity, "/" + folder["id"])
    share = client.post(
        "/api/getFolderShareAuth", json={"path": "/" + folder["id"]}, headers=ORIGIN
    ).json()["auth"]
    assert client.get(url, params={"auth": share}).status_code == 403
    client.post("/api/logout", headers=ORIGIN)
    assert client.get(url, params={"auth": share}).status_code == 401
    assert client.post(url + "/session", json={}, headers=ORIGIN).status_code == 401
    listing = client.post(
        "/api/getDirectory",
        json={"path": "/share_" + folder["id"], "auth": share},
        headers=ORIGIN,
    )
    assert listing.status_code == 200, listing.text
    assert "video_progress" not in listing.text and writer not in listing.text


@pytest.mark.parametrize("password,allowed", [(VISITOR, False), (ADMIN, True)])
def test_public_demo_cannot_read_or_write_owner_progress(tmp_path, password, allowed):
    with auth_client(tmp_path) as (client, _settings, runtime):
        identity = runtime.store.add_file("/", "movie.mp4", 7, 3)["id"]
        assert sign_in(client, password).status_code == 200
        url = path(identity)
        read = client.get(url)
        begin = client.post(url + "/session", json={}, headers=ORIGIN)
        assert read.status_code == begin.status_code == (200 if allowed else 403)
        if not allowed:
            assert read.json()["code"] == "VIDEO_PROGRESS_OWNER_ONLY"
            assert (
                runtime.store.db.nodes.find_one({"_id": identity}).get(
                    "video_progress_session"
                )
                is None
            )


@pytest.mark.asyncio
async def test_cancelled_progress_commit_is_drained_before_return(
    tmp_path, monkeypatch
):
    from tests.support.mongo_fixtures import new_store

    store = new_store(tmp_path / "drive")
    identity = store.add_file("/", "movie.mp4", 7, 3)["id"]
    writer = store.begin_video_progress(identity)["session_id"]
    entered, release = threading.Event(), threading.Event()
    update = store.db.nodes.update_one

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return update(*args, **kwargs)

    monkeypatch.setattr(store.db.nodes, "update_one", delayed)
    task = asyncio.create_task(
        _worker(store.save_video_progress, identity, writer, 1, 20, 200, final=True)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.video_progress(identity)["position_seconds"] == 20
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


def test_shared_browser_fixture_exposes_production_progress_routes():
    from tests.support import browser_transfer_app as fixture

    with TestClient(fixture.app, base_url="https://testserver") as client:
        assert (
            client.post(
                "/api/checkPassword", json={"pass": fixture.PASSWORD}
            ).status_code
            == 200
        )
        seed = client.get(
            "/__test__/state", headers={"x-fixture-control": fixture.CONTROL_HEADER}
        ).json()["seed"]
        identity = seed["video"].rsplit("/", 1)[-1]
        url = path(identity)
        writer = client.post(url + "/session", json={}, headers=ORIGIN)
        assert writer.status_code == 200, writer.text
        session = writer.json()["session_id"]
        assert (
            client.put(
                url,
                json=body(session, position=2, duration=8, final=True),
                headers=ORIGIN,
            ).status_code
            == 200
        )
        assert client.get(url).json()["progress"]["position_seconds"] == 2
