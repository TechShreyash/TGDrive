from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import new_store, register_test_backend
from tgdrive.transfers.transfer_locations import enrich_locations, locate_transfer
from tgdrive.web.app import create_app


def check_locations(store):
    folder = store.create_folder("/", "Upload Folder")
    saved = store.create_folder("/", "Saved")
    folder["path"] = store.resolve_id(folder["id"])
    saved["path"] = store.resolve_id(saved["id"])
    original = store.add_file(
        folder["path"], "same.txt", 101, 20, operation_id="upload-exact"
    )
    duplicate = store.add_file(
        folder["path"], "same.txt", 102, 20, operation_id="other", keep_both=True
    )
    original["path"] = store.resolve_id(original["id"])
    first = locate_transfer(store, {"id": "upload-exact", "status": "completed"})
    assert first["item_id"] == original["id"] != duplicate["id"]
    assert first["display_path"] == "My Files / Upload Folder / same.txt"
    store.move(original["path"], saved["path"])
    current = store.resolve_id(original["id"])
    store.rename(current, "renamed.txt")
    moved = locate_transfer(
        store,
        {"id": "upload-exact", "status": "completed", "destination": folder["path"]},
    )
    assert moved["item_id"] == original["id"]
    assert moved["folder_path"] == saved["path"]
    assert moved["display_path"] == "My Files / Saved / renamed.txt"
    store.set_trashed(current, True)
    assert locate_transfer(store, {"id": "upload-exact", "status": "completed"}) == {
        "available": False,
        "reason": "Item is in Trash",
    }
    deletion = store.begin_deletion(current, channel_id=-1001234567890)
    store.confirm_deletion_messages(deletion["id"], deletion["message_ids"])
    store.finish_deletion(deletion["id"])
    assert not locate_transfer(store, {"id": "upload-exact", "status": "completed"})[
        "available"
    ]
    queued = locate_transfer(store, {"id": "unfinished", "destination": folder["path"]})
    assert queued["available"] and queued["item_id"] is None
    assert queued["folder_path"] == folder["path"]
    assert not locate_transfer(store, {"id": "unknown", "status": "completed"})[
        "available"
    ]


def test_exact_locations_follow_moves_and_renames_without_matching_names(tmp_path):
    store = new_store(tmp_path / "drive")
    try:
        check_locations(store)
    finally:
        store.close()


def test_history_locations_batch_current_operations_and_ancestors(
    tmp_path, monkeypatch
):
    store = new_store(tmp_path / "drive")
    try:
        folder = store.create_folder("/", "Destination")
        saved = store.create_folder("/", "Moved")
        items = [
            store.add_file(
                "/" + folder["id"],
                f"file-{index}.txt",
                101 + index,
                2,
                operation_id=f"operation-{index}",
            )
            for index in range(3)
        ]
        store.move("/" + items[0]["id"], "/" + saved["id"])
        store.set_trashed("/" + items[1]["id"], True)
        jobs = [
            dict(id=f"operation-{index}", status="completed", destination="/")
            for index in range(3)
        ] + [
            dict(id="missing-completed", status="completed", destination="/"),
            dict(id="queued", status="queued", destination="/" + folder["id"]),
            dict(id="torrent", kind="torrent", status="completed", destination="/"),
            dict(id="extract", kind="extract", status="completed", destination="/"),
        ]
        expected = [locate_transfer(store, row) for row in jobs]
        operations, ancestors = [], []
        one, find, aggregate = store._one, store._find, store.db.nodes.aggregate

        def observed_one(collection, *args, **kwargs):
            if collection == "operations":
                operations.append("one")
            return one(collection, *args, **kwargs)

        def observed_find(collection, *args, **kwargs):
            if collection == "operations":
                operations.append("batch")
            return find(collection, *args, **kwargs)

        def observed_ancestors(*args, **kwargs):
            ancestors.append(True)
            return aggregate(*args, **kwargs)

        monkeypatch.setattr(store, "_one", observed_one)
        monkeypatch.setattr(store, "_find", observed_find)
        monkeypatch.setattr(store.db.nodes, "aggregate", observed_ancestors)
        assert enrich_locations(store, jobs) is jobs
        assert [row["location"] for row in jobs] == expected
        assert operations == ["batch"]
        assert len(ancestors) == 1
        # A new request must see the latest Trash state, including a former hit.
        store.set_trashed("/" + items[0]["id"], True)
        assert not enrich_locations(store, [jobs[0]])[0]["location"]["available"]
    finally:
        store.close()


def test_empty_location_batch_reads_nothing_and_corrupt_chain_keeps_other_rows(
    tmp_path, monkeypatch
):
    store = new_store(tmp_path / "drive")
    try:
        healthy = store.create_folder("/", "Healthy")
        broken = store.create_folder("/", "Broken")
        store.db.nodes.update_one(
            {"_id": broken["id"]}, {"$set": {"parent_id": "a" * 32}}
        )
        find = store._find
        calls = []

        def observed(*args, **kwargs):
            calls.append(True)
            return find(*args, **kwargs)

        monkeypatch.setattr(store, "_find", observed)
        assert enrich_locations(store, []) == [] and calls == []
        jobs = [
            dict(id="good", destination="/" + healthy["id"]),
            dict(id="bad", destination="/" + broken["id"]),
        ]
        result = enrich_locations(store, jobs)
        assert result[0]["location"]["folder_path"] == "/" + healthy["id"]
        assert result[0]["location"]["available"]
        assert not result[1]["location"]["available"]
        invalid = enrich_locations(store, [dict(id="invalid/id", destination="/")])
        assert not invalid[0]["location"]["available"]
    finally:
        store.close()


def test_location_batches_are_bounded_and_keep_order(tmp_path, monkeypatch):
    import tgdrive.storage.mongo_store as module

    store = new_store(tmp_path / "drive")
    try:
        monkeypatch.setattr(module, "_BATCH", 2)
        find, batches = store._find, []

        def observed(collection, query, *args, **kwargs):
            if collection == "operations":
                batches.append(query["_id"]["$in"])
            return find(collection, query, *args, **kwargs)

        monkeypatch.setattr(store, "_find", observed)
        jobs = [dict(id=f"job-{index}", destination="/") for index in range(5)]
        result = enrich_locations(store, jobs)
        assert [row["id"] for row in result] == [f"job-{index}" for index in range(5)]
        assert all(row["location"]["available"] for row in result)
        assert [len(batch) for batch in batches] == [2, 2, 1]
    finally:
        store.close()


async def test_owner_location_api_and_history_enrichment(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    store = new_store(tmp_path / "drive")
    runtime = FakeRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, settings)
    row = await runtime.transfers.registry.create(
        "queued", transfer_id="committed-upload"
    )
    row.context.update(name="original.txt", kind="upload", destination="/")
    saved = store.add_file("/", "original.txt", 101, 2, operation_id=row.id)
    app = create_app(settings, runtime=runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            endpoint = "/api/v1/transfers/location"
            payload = {"transfer_id": row.id, "completed": True}
            assert client.post(endpoint, json=payload).status_code == 401
            login(client)
            response = client.post(endpoint, json=payload)
            assert response.headers["Cache-Control"] == "no-store"
            assert response.json()["item_id"] == saved["id"]
            history = client.get("/api/v1/transfers/history").json()
            assert (
                history["jobs"][0]["location"]["display_path"]
                == "My Files / original.txt"
            )
            assert "message_id" not in str(history["jobs"][0]["location"])
            assert (
                client.post(endpoint, json={"transfer_id": "invalid/id"}).status_code
                == 422
            )
    finally:
        runtime.transfers.registry.close()
        store.close()
