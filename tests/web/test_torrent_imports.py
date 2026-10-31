"""Torrent start requests keep owner access and transfer request identity."""

from unittest.mock import AsyncMock

from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login

SOURCE = "magnet:?xt=urn:btih:" + "a" * 40


def test_torrent_start_requires_owner_and_deduplicates_requests(app_client):
    client, _store, runtime, _settings = app_client

    async def create(**kwargs):
        return await runtime.transfers.registry.create(
            "torrent_metadata", context={"kind": "torrent", "name": "Torrent import"}
        )

    runtime.transfers.start_torrent = AsyncMock(side_effect=create)
    payload = {"url": SOURCE, "path": "/", "client_request_id": "torrent-request-0001"}
    endpoint = "/api/startTorrentDownload"
    assert client.post(endpoint, json=payload).status_code == 401
    login(client)
    first = client.post(endpoint, json=payload)
    assert first.status_code == 200
    assert client.post(endpoint, json=payload).json()["id"] == first.json()["id"]
    assert runtime.transfers.start_torrent.await_count == 1
    runtime.transfers.start_torrent.assert_awaited_with(
        source=SOURCE, destination="/", select_files=False
    )
    changed = payload | {"url": SOURCE.replace("a", "b")}
    assert client.post(endpoint, json=changed).status_code == 409
    lookup = client.post(
        "/api/getTransferByRequest",
        json={"client_request_id": payload["client_request_id"]},
    )
    assert lookup.json()["id"] == first.json()["id"]


def test_torrent_children_are_hidden_from_public_job_and_history_lists(app_client):
    client, _store, runtime, _settings = app_client

    async def create(**kwargs):
        parent = await runtime.transfers.registry.create(
            "torrent_metadata", context={"kind": "torrent", "name": "Album"}
        )
        await runtime.transfers.registry.create(
            "uploading", context={"kind": "upload", "torrent_parent_id": parent.id}
        )
        return parent

    runtime.transfers.start_torrent = AsyncMock(side_effect=create)
    login(client)
    response = client.post(
        "/api/startTorrentDownload", json={"url": SOURCE, "path": "/"}
    )
    identity = response.json()["id"]
    assert [job["id"] for job in client.get("/api/v1/jobs").json()["jobs"]] == [
        identity
    ]
    history = client.get("/api/v1/transfers/history").json()
    assert [job["id"] for job in history["jobs"]] == [identity]


def test_torrent_file_choice_route_validates_before_continuing(app_client):
    client, _store, runtime, _settings = app_client
    calls = []

    async def choose(job_id, files):
        calls.append(files)
        if files == [9]:
            raise ValueError("Choose one or more distinct files from this torrent")
        return runtime.transfers.registry.get(job_id)

    async def create(**kwargs):
        return await runtime.transfers.registry.create(
            "torrent_select", context={"kind": "torrent", "name": "Album"}
        )

    runtime.transfers.start_torrent = AsyncMock(side_effect=create)
    runtime.transfers.select_torrent_files = choose
    login(client)
    identity = client.post(
        "/api/startTorrentDownload",
        json={"url": SOURCE, "path": "/", "select_files": True},
    ).json()["id"]
    route = f"/api/v1/jobs/{identity}/torrent-files"
    assert client.post(route, json={"files": []}).status_code == 422
    assert client.post(route, json={"files": ["0"]}).status_code == 422
    assert client.post(route, json={"files": [9]}).status_code == 422
    assert client.post(route, json={"files": [0, 2]}).status_code == 200
    assert calls == [[9], [0, 2]]
