from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.storage.mongo import for_directory
from tgdrive.storage.store import DriveStore
from tgdrive.transfers.tasks import TransferNotFound, TransferRegistry, TransferStatus
from tgdrive.web.app import create_app

pytestmark = pytest.mark.usefixtures("mongo_backend")


@pytest.fixture
def mongo_backend(tmp_path, monkeypatch):
    backend = register_test_backend(monkeypatch, tmp_path)
    register_test_backend(monkeypatch, tmp_path / "data", backend)
    return backend


def registry_for(path, **kwargs):
    if path is None:
        return TransferRegistry(**kwargs)
    backend = for_directory(path.parent)
    return TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend, **kwargs)


async def add(
    registry,
    name,
    status,
    *,
    created=1000,
    identity=None,
    kind="upload",
    folder_upload=None,
):
    record = await registry.create("uploading", total=100, transfer_id=identity)
    record.context.update(
        name=name,
        destination="/",
        kind=kind,
        local_path="/private/staged-file",
        source_url="https://private/?token=secret",
    )
    if folder_upload is not None:
        record.context["folder_upload"] = copy.deepcopy(folder_upload)
    record.created_at = created
    (
        await registry.update(
            record.id,
            status=status,
            current=100 if status is TransferStatus.COMPLETED else 20,
        )
    )
    return record


@pytest.mark.asyncio
async def test_history_survives_prune_and_reopen_without_private_context(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    registry = registry_for(path, ttl_seconds=60)
    record = await add(registry, "kept.bin", TransferStatus.COMPLETED)
    (await registry.prune(now=time.time() + 61))
    with pytest.raises(TransferNotFound):
        registry.get(record.id)
    assert registry.list() == []
    page = await registry.history(status="completed")
    assert page["jobs"][0]["name"] == "kept.bin"
    assert page["jobs"][0]["retained"] is False
    assert "secret" not in json.dumps(page)
    assert "context" not in json.dumps(page)
    assert "staged-file" not in json.dumps(page)
    registry.close()
    reopened = registry_for(path)
    assert reopened.list() == []
    assert (await reopened.history())["jobs"][0]["id"] == record.id
    reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("name", [None, ""])
async def test_new_transfer_archives_expired_unnamed_url_job(
    tmp_path, persistent, name
):
    registry = registry_for(
        tmp_path / "jobs.sqlite3" if persistent else None, ttl_seconds=1
    )
    try:
        previous = await registry.create(
            "checking_source", context={"kind": "remote", "name": name}
        )
        await registry.update(previous.id, status=TransferStatus.CANCELLED)
        previous.updated_at = time.time() - 2
        await registry.persist(previous)

        current = await registry.create(
            "torrent_metadata", context={"kind": "torrent", "name": "Torrent import"}
        )

        assert registry.get(current.id) is current
        with pytest.raises(TransferNotFound):
            registry.get(previous.id)
        archived = await registry.history(status="cancelled")
        assert archived["jobs"][0]["name"] == "Transfer"
        assert archived["jobs"][0]["id"] == previous.id
        assert archived["jobs"][0]["retained"] is False
    finally:
        registry.close()


@pytest.mark.asyncio
async def test_name_search_filters_before_cursor_pagination_across_archives(tmp_path):
    registry = registry_for(tmp_path / "search.sqlite3", ttl_seconds=1)
    for index in range(75):
        (
            await add(
                registry,
                f"other-{index}.bin",
                TransferStatus.COMPLETED,
                created=2000 + index,
            )
        )
    for index in range(3):
        (
            await add(
                registry,
                f"Résumé [.*] {index}.txt",
                TransferStatus.COMPLETED,
                created=1000 + index,
                identity=f"match-{index}",
            )
        )
    (await registry.prune(now=time.time() + 2))
    first = await registry.history(status="finished", query="RÉSUMÉ [.*]", limit=2)
    assert [row["id"] for row in first["jobs"]] == ["match-2", "match-1"]
    assert first["has_more"]
    second = await registry.history(
        status="finished",
        query="résumé [.*]",
        limit=2,
        before=(first["next_before"], first["next_before_id"]),
    )
    assert [row["id"] for row in second["jobs"]] == ["match-0"]
    assert not second["has_more"]
    assert (await registry.history(query="never present"))["jobs"] == []
    registry.close()


@pytest.mark.asyncio
async def test_history_cursor_filters_and_deletions_are_independent_of_live_eviction(
    tmp_path,
):
    registry = registry_for(tmp_path / "jobs.sqlite3", ttl_seconds=1)
    for index in range(5):
        (
            await add(
                registry,
                str(index),
                TransferStatus.COMPLETED,
                created=1000 + index,
                identity="history-" + str(index),
            )
        )
    (await add(registry, "not-a-transfer", TransferStatus.COMPLETED, kind="delete"))
    (await registry.prune(now=time.time() + 2))
    queued = await add(registry, "queued", TransferStatus.QUEUED, created=1006)
    (await add(registry, "running", TransferStatus.RUNNING, created=1007))
    (await add(registry, "failed", TransferStatus.ERROR, created=1008))
    first = await registry.history(limit=3)
    assert [job["name"] for job in first["jobs"]] == ["failed", "running", "queued"]
    assert first["has_more"]
    second = await registry.history(
        limit=3, before=(first["next_before"], first["next_before_id"])
    )
    assert [job["name"] for job in second["jobs"]] == ["4", "3", "2"]
    assert second["has_more"]
    third = await registry.history(
        limit=3, before=(second["next_before"], second["next_before_id"])
    )
    assert [job["name"] for job in third["jobs"]] == ["1", "0"]
    assert not third["has_more"]
    assert (await registry.history(status="queued"))["jobs"][0]["id"] == queued.id
    assert len((await registry.history(status="completed"))["jobs"]) == 5
    assert (await registry.history(status="running"))["jobs"][0]["name"] == "running"
    (await registry.retry((await registry.history(status="error"))["jobs"][0]["id"]))
    assert (await registry.history(status="error"))["jobs"] == []
    registry.close()


async def test_history_http_owner_boundary_and_validation(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "catalog.sqlite3")
    runtime = FakeRuntime(store)
    runtime.mongo = for_directory(tmp_path)
    record = await add(
        runtime.transfers.registry, "owner-only.bin", TransferStatus.COMPLETED
    )
    try:
        with TestClient(
            create_app(settings, runtime=runtime), base_url="https://testserver"
        ) as client:
            route = "/api/v1/transfers/history"
            assert client.get(route).status_code == 401
            login(client)
            response = client.get(route, params={"status": "completed", "limit": 1})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["jobs"][0]["id"] == record.id
            assert (
                client.get(route, params={"status": "finished"}).json()["jobs"][0]["id"]
                == record.id
            )
            assert client.get(route, params={"limit": 201}).status_code == 422
            assert client.get(route, params={"status": "unknown"}).status_code == 422
            assert client.get(route, params={"before": 1000}).status_code == 400
            assert (
                client.get(
                    route, params={"before": "nan", "before_id": "safe"}
                ).status_code
                == 422
            )
    finally:
        runtime.transfers.registry.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_finished_history_pages_mix_terminal_outcomes_without_active_jobs(
    tmp_path, persistent
):
    registry = registry_for(
        tmp_path / "jobs.sqlite3" if persistent else None,
        ttl_seconds=1,
    )
    try:
        (await add(registry, "success", TransferStatus.COMPLETED, created=1000))
        (await add(registry, "cancelled", TransferStatus.CANCELLED, created=1001))
        (await registry.prune(now=time.time() + 2))
        (await add(registry, "failed", TransferStatus.ERROR, created=1002))
        (await add(registry, "running", TransferStatus.RUNNING, created=1003))
        (await add(registry, "queued", TransferStatus.QUEUED, created=1004))
        first = await registry.history(status="finished", limit=2)
        assert [job["name"] for job in first["jobs"]] == ["failed", "cancelled"]
        assert first["has_more"]
        second = await registry.history(
            status="finished",
            limit=2,
            before=(first["next_before"], first["next_before_id"]),
        )
        assert [job["name"] for job in second["jobs"]] == ["success"]
        assert not second["has_more"]
        (await registry.retry(first["jobs"][0]["id"]))
        assert [
            job["name"] for job in (await registry.history(status="finished"))["jobs"]
        ] == ["cancelled", "success"]
    finally:
        registry.close()


@pytest.mark.asyncio
async def test_history_search_reads_can_overlap_archive_writes_without_connection_races(
    tmp_path,
):
    import asyncio

    registry = registry_for(tmp_path / "concurrent.sqlite3", ttl_seconds=1)
    done = asyncio.Event()
    reads = 0

    async def reader():
        nonlocal reads
        while not done.is_set():
            page = await registry.history(status="finished", query="résumé", limit=200)
            assert all("Résumé" in item["name"] for item in page["jobs"])
            reads += 1

    workers = [asyncio.create_task(reader()) for _ in range(8)]
    try:
        for index in range(100):
            (
                await add(
                    registry,
                    f"Résumé {index}",
                    TransferStatus.COMPLETED,
                    created=1000 + index,
                )
            )
            (await registry.prune(now=time.time() + 2))
            await asyncio.sleep(0)
        done.set()
        await asyncio.gather(*workers)
        assert reads >= 8
        assert (
            len(
                (await registry.history(status="finished", query="RÉSUMÉ", limit=200))[
                    "jobs"
                ]
            )
            == 100
        )
    finally:
        done.set()
        await asyncio.gather(*workers)
        registry.close()


class HistoryCursor(list):
    def batch_size(self, _size):
        return self

    def sort(self, fields):
        for key, direction in reversed(fields):
            super().sort(key=lambda row: row.get(key), reverse=direction < 0)
        return self

    def limit(self, count):
        return HistoryCursor(self[:count])


class HistoryMongo:
    """The archive operations used by task pages, without a MongoDB service."""

    def __init__(self):
        self.rows = {}

    def find(self, query):
        rows = list(self.rows.values())
        if query.get("archived") is True:
            rows = [row for row in rows if row.get("archived") is True]
            if "$or" in query:
                folders = query["$or"][0]["summary.folder_upload.id"]["$in"]
                identities = query["$or"][1]["_id"]["$in"]
                rows = [
                    row
                    for row in rows
                    if row["_id"] in identities
                    or row["summary"].get("folder_upload", {}).get("id") in folders
                ]
        else:
            rows = [row for row in rows if row.get("archived") is not True]
        return HistoryCursor(copy.deepcopy(rows))

    def replace_one(self, query, document, **_kwargs):
        self.rows[query["_id"]] = copy.deepcopy(document)

    def delete_one(self, query):
        self.rows.pop(query["_id"], None)

    def delete_many(self, query):
        removed = [
            identity
            for identity, row in self.rows.items()
            if row.get("archived") is True
            and row["status"] in query["status"]["$in"]
            and row["summary"].get("folder_upload", {}).get("id")
            not in query["summary.folder_upload.id"]["$nin"]
        ]
        for identity in removed:
            del self.rows[identity]
        return SimpleNamespace(deleted_count=len(removed))


@pytest.fixture(params=["memory", "mongo"])
def task_registry(request, tmp_path):
    registry = TransferRegistry(
        mongo=HistoryMongo() if request.param == "mongo" else None,
        ttl_seconds=1,
    )
    try:
        yield registry
    finally:
        registry.close()


def folder_metadata(
    identity="folder-session-0001", name="Holiday photos", created=1000
):
    return {
        "id": identity,
        "name": name,
        "root_path": "/prepared-folder",
        "created_at": created,
    }


async def test_clear_history_preserves_active_folders_and_recovery(task_registry):
    registry = task_registry
    active_folder = folder_metadata()
    browser_folder = folder_metadata("browser-folder-0001")
    for status in (
        TransferStatus.COMPLETED,
        TransferStatus.ERROR,
        TransferStatus.CANCELLED,
    ):
        (await add(registry, "archived-" + status, status, kind="legacy"))
    (
        await add(
            registry,
            "active-child",
            TransferStatus.COMPLETED,
            folder_upload=active_folder,
        )
    )
    (
        await add(
            registry,
            "browser-child",
            TransferStatus.COMPLETED,
            folder_upload=browser_folder,
        )
    )
    running = await add(
        registry, "running", TransferStatus.RUNNING, folder_upload=active_folder
    )
    (await registry.prune(now=time.time() + 2))
    for status in (TransferStatus.COMPLETED, TransferStatus.CANCELLED):
        (await add(registry, "live-" + status, status))
    failed = await add(registry, "retryable", TransferStatus.ERROR)
    queued = await add(registry, "queued", TransferStatus.QUEUED)
    deletion = await add(registry, "deletion", TransferStatus.COMPLETED, kind="delete")

    assert (await registry.clear_history(exclude_folders=[browser_folder["id"]])) == 6
    assert (await registry.clear_history(exclude_folders=[browser_folder["id"]])) == 0
    names = {row["name"] for row in (await registry.history())["jobs"]}
    assert names == {"active-child", "browser-child", "running", "queued"}
    assert registry.get(failed.id).context["source_url"].endswith("token=secret")
    assert registry.get(failed.id).history_hidden
    assert not registry.get(running.id).history_hidden
    assert not registry.get(queued.id).history_hidden
    assert not registry.get(deletion.id).history_hidden
    assert (await registry.task_history(status="finished"))["total_tasks"] == 1

    (await registry.retry(failed.id))
    assert not registry.get(failed.id).history_hidden
    assert failed.id in {row["id"] for row in (await registry.history())["jobs"]}
    (await registry.prune(now=time.time() + 2))
    assert not any(
        row["name"].startswith(("archived-", "live-"))
        for row in (await registry.history())["jobs"]
    )


async def test_cleared_history_stays_cleared_after_restart_and_pruning(tmp_path):
    path = tmp_path / "clear-history.sqlite3"
    registry = registry_for(path, ttl_seconds=1)
    completed = await add(registry, "completed", TransferStatus.COMPLETED)
    failed = await add(registry, "failed", TransferStatus.ERROR)
    assert (await registry.clear_history()) == 2
    registry.close()
    reopened = registry_for(path, ttl_seconds=1)
    try:
        assert reopened.get(failed.id).history_hidden
        assert (await reopened.history())["jobs"] == []
        (await reopened.prune(now=time.time() + 2))
        assert (await reopened.task_history())["total_tasks"] == 0
        with pytest.raises(TransferNotFound):
            reopened.get(completed.id)
    finally:
        reopened.close()


async def test_clear_history_api_requires_login_and_keeps_live_jobs(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "clear-api.sqlite3")
    runtime = FakeRuntime(store)
    runtime.mongo = for_directory(tmp_path)
    finished = await add(
        runtime.transfers.registry, "finished", TransferStatus.COMPLETED
    )
    queued = await add(runtime.transfers.registry, "queued", TransferStatus.QUEUED)
    try:
        with TestClient(
            create_app(settings, runtime=runtime), base_url="https://testserver"
        ) as client:
            route = "/api/v1/transfers/history"
            assert client.request("DELETE", route, json={}).status_code == 401
            login(client)
            response = client.request("DELETE", route, json={"exclude_folders": []})
            assert response.status_code == 200
            assert response.json() == {"cleared": 1}
            assert response.headers["cache-control"] == "no-store"
            jobs = {row["id"]: row for row in client.get("/api/v1/jobs").json()["jobs"]}
            assert jobs[finished.id]["history_cleared"] is True
            assert jobs[queued.id]["history_cleared"] is False
            assert client.get(route).json()["jobs"][0]["id"] == queued.id
            # Existing receipt/job polling still works after its display row is cleared.
            assert client.get("/api/v1/jobs/" + finished.id).status_code == 200
            assert client.request("DELETE", route, json={}).json() == {"cleared": 0}
    finally:
        runtime.transfers.registry.close()
        store.close()


async def test_numbered_pages_count_folders_once_and_keep_every_member(task_registry):
    registry = task_registry
    folder = folder_metadata()
    children = [
        (
            await add(
                registry,
                f"image-{index}.jpg",
                TransferStatus.COMPLETED,
                identity=f"image-{index}",
                created=1000 + index,
                folder_upload=folder,
            )
        ).id
        for index in range(72)
    ]
    (await registry.prune(now=time.time() + 2))
    for index in range(21):
        (
            await add(
                registry,
                f"separate-{index}.bin",
                TransferStatus.COMPLETED,
                created=2000 + index,
                kind="remote" if index % 2 else "upload",
            )
        )
    first = await registry.task_history(status="finished", page=1, limit=20)
    assert first["total_tasks"] == 22
    assert first["total_pages"] == 2
    assert len(first["jobs"]) == 20
    assert first["has_more"]
    second = await registry.task_history(status="finished", page=2, limit=20)
    assert len(second["jobs"]) == 73
    assert {row["id"] for row in second["jobs"] if row.get("folder_upload")} == set(
        children
    )
    assert not second["has_more"]
    assert second["page"] == 2
    assert (await registry.task_history(page=99, limit=20))["page"] == 2


async def test_task_status_and_search_apply_before_pagination_across_live_archive(
    task_registry,
):
    registry = task_registry
    folder = folder_metadata(name="Résumé [.*] album")
    for index in range(3):
        (
            await add(
                registry,
                f"photo-{index}.jpg",
                TransferStatus.COMPLETED,
                folder_upload=folder,
            )
        )
    (await registry.prune(now=time.time() + 2))
    running = await add(
        registry,
        "special-child.jpg",
        TransferStatus.RUNNING,
        created=1100,
        folder_upload=folder,
    )
    (await add(registry, "standalone.bin", TransferStatus.COMPLETED, created=1200))
    finished = await registry.task_history(status="finished", limit=1)
    assert finished["total_tasks"] == 1
    assert [row["name"] for row in finished["jobs"]] == ["standalone.bin"]
    for term in ["RÉSUMÉ [.*]", "special-child"]:
        matching = await registry.task_history(status="running", query=term, limit=1)
        assert matching["total_tasks"] == 1
        assert len(matching["jobs"]) == 4
    (await registry.update(running.id, status=TransferStatus.COMPLETED))
    assert (await registry.task_history(status="running"))["jobs"] == []
    assert (await registry.task_history(status="finished"))["total_tasks"] == 2


async def test_folder_task_metadata_survives_mongo_history_reopen(tmp_path):
    path = tmp_path / "folder-history.sqlite3"
    registry = registry_for(path, ttl_seconds=1)
    folder = folder_metadata()
    for index in range(3):
        (
            await add(
                registry,
                f"child-{index}",
                TransferStatus.COMPLETED,
                folder_upload=folder,
            )
        )
    (await registry.prune(now=time.time() + 2))
    registry.close()
    reopened = registry_for(path)
    try:
        page = await reopened.task_history(limit=1)
        assert page["total_tasks"] == 1
        assert len(page["jobs"]) == 3
        assert all(row["folder_upload"] == folder for row in page["jobs"])
        assert all(not row["retained"] for row in page["jobs"])
    finally:
        reopened.close()


async def test_task_history_http_bounds_owner_boundary_and_current_folder_metadata(
    tmp_path,
):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "task-catalog.sqlite3")
    runtime = FakeRuntime(store)
    runtime.mongo = for_directory(tmp_path)
    registry = runtime.transfers.registry
    folder = folder_metadata()
    children = [
        (
            await add(
                registry, str(index), TransferStatus.COMPLETED, folder_upload=folder
            )
        ).id
        for index in range(3)
    ]
    route = "/api/v1/transfers/history/tasks"
    payload = {
        "status": "finished",
        "page": 1,
        "limit": 1,
    }
    try:
        with TestClient(
            create_app(settings, runtime=runtime), base_url="https://testserver"
        ) as client:
            assert client.post(route, json=payload).status_code == 401
            login(client)
            response = client.post(route, json=payload)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["total_tasks"] == 1
            assert len(response.json()["jobs"]) == 3
            assert (
                client.get("/api/v1/transfers/history", params={"page": 1}).json()[
                    "total_tasks"
                ]
                == 1
            )
            assert client.post(route, json=payload | {"offset": -1}).status_code == 422
            assert client.post(route, json=payload | {"page": 0}).status_code == 422
            unsupported = payload | {
                "folders": [{"folder_upload": folder, "transfer_ids": children}]
            }
            assert client.post(route, json=unsupported).status_code == 422
            excluded = client.post(
                route, json=payload | {"exclude_folders": [folder["id"]]}
            )
            assert excluded.json()["jobs"] == []
            assert excluded.json()["total_tasks"] == 0
            response = client.post(route, content=b"x" * (256 * 1024 + 1))
            assert response.status_code == 413
            assert (
                client.get(
                    "/api/v1/transfers/history",
                    params={"page": 1, "before": 1000, "before_id": "safe"},
                ).status_code
                == 400
            )
    finally:
        registry.close()
        store.close()
