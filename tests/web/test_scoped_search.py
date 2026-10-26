"""Search contexts must never broaden a Trash or owner-only listing."""

from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tgdrive.storage.store import DriveStore
from tgdrive.web.app import create_app


def test_trash_search_is_owner_only_and_does_not_include_active_or_hidden_children(
    tmp_path,
):
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    active = store.add_file("/", "résumé active.txt", 1, 10)
    source = store.create_folder("/", "書庫")
    nested_source = store.create_folder("/" + source["id"], "Reference")
    nested_path = "/" + source["id"] + "/" + nested_source["id"]
    deleted = store.add_file(nested_path, "résumé deleted.txt", 2, 10)
    folder = store.create_folder("/", "Old documents")
    child = store.add_file("/" + folder["id"], "résumé child.txt", 3, 10)
    store.set_trashed(nested_path + "/" + deleted["id"], True)
    store.set_trashed("/" + folder["id"], True)
    runtime = FakeRuntime(store)
    try:
        with TestClient(
            create_app(make_settings(tmp_path), runtime=runtime),
            base_url="https://testserver",
        ) as client:
            assert (
                client.get("/api/v1/trash", params={"q": "résumé"}).status_code == 401
            )
            login(client)
            result = client.get("/api/v1/trash", params={"q": "RE\u0301SUME\u0301"})
            assert result.status_code == 200
            assert result.headers["cache-control"] == "no-store"
            contents = result.json()["data"]["contents"]
            assert set(contents) == {deleted["id"]}
            assert contents[deleted["id"]]["parent_names"] == ["書庫", "Reference"]
            assert contents[deleted["id"]]["path"] == nested_path
            assert active["id"] not in result.text and child["id"] not in result.text
            assert len(client.get("/api/v1/trash").json()["data"]["contents"]) == 2
            assert (
                client.get("/api/v1/trash", params={"q": "x" * 256}).status_code == 422
            )
            assert (
                client.get("/api/v1/trash", params={"q": "missing"}).json()["data"][
                    "contents"
                ]
                == {}
            )
    finally:
        runtime.transfers.registry.close()
        store.close()
