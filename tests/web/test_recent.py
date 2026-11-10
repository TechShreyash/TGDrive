"""Recent lists files the owner opened or uploaded, newest activity first."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, make_settings
from tests.support.mongo_fixtures import new_store
from tgdrive.core.errors import ValidationError
from tgdrive.web import app as app_module
from tgdrive.web.app import create_app


@pytest.fixture
def store():
    result = new_store()
    yield result
    result.close()


def full(record):
    return record["path"].rstrip("/") + "/" + record["id"]


def set_activity(store, identity, minutes_ago):
    store.db.nodes.update_one(
        {"_id": identity},
        {"$set": {"active_at": datetime.now(UTC) - timedelta(minutes=minutes_ago)}},
    )


def names(page):
    return [row["name"] for row in page["items"]]


def test_uploads_and_opens_order_recent_and_folders_are_left_out(store):
    folder = store.create_folder("/", "Anime")
    old = store.add_file(full(folder), "episode 1.mkv", 1, 1)
    new = store.add_file("/", "notes.txt", 2, 1)
    set_activity(store, old["id"], 30)
    set_activity(store, new["id"], 10)
    page = store.list_recent()
    assert names(page) == ["notes.txt", "episode 1.mkv"]
    assert page["items"][1]["parent_names"] == ["Anime"]
    assert page["items"][1]["path"] == full(folder)
    assert page["items"][0]["active_at"].endswith("Z")
    store.mark_opened(old["id"])
    assert names(store.list_recent()) == ["episode 1.mkv", "notes.txt"]
    store.mark_opened(folder["id"])  # Folders are never recorded.
    assert "active_at" not in store.db.nodes.find_one({"_id": folder["id"]})


def test_trashed_files_and_trashed_folders_are_hidden(store):
    folder = store.create_folder("/", "Old")
    hidden = store.add_file(full(folder), "inside.mkv", 1, 1)
    trashed = store.add_file("/", "gone.txt", 2, 1)
    kept = store.add_file("/", "kept.txt", 3, 1)
    store.set_trashed(full(folder), True)
    store.set_trashed(full(trashed), True)
    assert names(store.list_recent()) == ["kept.txt"]
    assert hidden["id"] and kept["id"]


def test_recent_pages_by_cursor_and_filters_by_type(store):
    ids = []
    for index in range(60):
        name = f"clip {index}.mp4" if index % 2 else f"doc {index}.pdf"
        ids.append(store.add_file("/", name, index + 1, 1)["id"])
        set_activity(store, ids[-1], index)
    first = store.list_recent(limit=25)
    second = store.list_recent(limit=25, after=first["next_cursor"])
    third = store.list_recent(limit=25, after=second["next_cursor"])
    seen = [row["id"] for row in first["items"] + second["items"] + third["items"]]
    assert seen == ids and third["next_cursor"] is None
    media = store.list_recent(limit=25, group="media")
    assert all(row["name"].endswith(".mp4") for row in media["items"])
    assert len(media["items"]) == 25
    for limit in (1, 30, 200):
        with pytest.raises(ValidationError):
            store.list_recent(limit=limit)
    with pytest.raises(ValidationError):
        store.list_recent(after="%%%")


def test_a_trashed_folder_does_not_shorten_the_page(store):
    folder = store.create_folder("/", "Trash me")
    for index in range(30):
        item = store.add_file(full(folder), f"hidden {index}.txt", index + 1, 1)
        set_activity(store, item["id"], index)
    for index in range(30):
        item = store.add_file("/", f"visible {index}.txt", 100 + index, 1)
        set_activity(store, item["id"], 100 + index)
    store.set_trashed(full(folder), True)
    page = store.list_recent(limit=25)
    assert len(page["items"]) == 25
    assert all(row["name"].startswith("visible") for row in page["items"])


def test_owner_opens_are_recorded_once_per_window_and_shares_never(
    tmp_path, monkeypatch
):
    store = new_store()
    runtime = FakeRuntime(store)
    app = create_app(make_settings(tmp_path), runtime=runtime)
    folder = store.create_folder("/", "Shared")
    item = store.add_file(full(folder), "movie.mkv", 1, 1)
    set_activity(store, item["id"], 60)
    token = store.create_share(full(folder))
    calls = []
    original = store.mark_opened
    monkeypatch.setattr(store, "mark_opened", lambda i: (calls.append(i), original(i)))
    clock = [1000.0]
    monkeypatch.setattr(app_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def drain_recent_writes():
        tasks = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "record-file-open"
        ]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

    with TestClient(app, base_url="https://testserver") as client:
        assert client.portal is not None
        params = {"path": full(item)}
        client.get("/api/v1/preview", params=params | {"auth": token})
        assert calls == []  # Share visitors are not owner activity.
        assert (
            client.post(
                "/api/checkPassword", json={"pass": "correct horse battery staple"}
            ).status_code
            == 200
        )
        for _ in range(3):
            client.get("/api/v1/preview", params=params)
        client.portal.call(drain_recent_writes)
        assert calls == [item["id"]]
        client.get("/api/v1/preview", params=params)
        client.portal.call(drain_recent_writes)
        assert calls == [item["id"]]
        clock[0] += app_module.RECENT_WRITE_SECONDS + 1
        client.get("/api/v1/preview", params=params)
        client.portal.call(drain_recent_writes)
        assert calls == [item["id"], item["id"]]
        with pytest.raises(AttributeError):
            client.get("/api/v1/thumbnail", params=params)  # Fake pool; not an open.
        assert len(calls) == 2
        page = client.get("/api/v1/recent", params={"limit": 25}).json()
        assert [row["name"] for row in page["items"]] == ["movie.mkv"]
        assert client.get("/api/v1/recent", params={"limit": 30}).status_code == 422
    with TestClient(app, base_url="https://testserver") as stranger:
        assert stranger.get("/api/v1/recent").status_code == 401
    runtime.transfers.registry.close()
    store.close()
