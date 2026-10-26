from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.storage.store import DriveStore
from tgdrive.transfers.transfer_locations import locate_transfer
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
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    try:
        check_locations(store)
    finally:
        store.close()


async def test_owner_location_api_and_history_enrichment(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.sqlite3")
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
