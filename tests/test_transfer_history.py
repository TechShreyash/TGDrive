from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_app import FakeRuntime, login, make_settings

from tgdrive.app import create_app
from tgdrive.store import DriveStore
from tgdrive.tasks import TransferNotFound, TransferRegistry, TransferStatus


def add(
    registry,
    name,
    status,
    *,
    created=1000,
    identity=None,
    kind="upload",
    folder_upload=None,
):
    record = registry.create("uploading", total=100, transfer_id=identity)
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
    registry.update(
        record.id,
        status=status,
        current=100 if status is TransferStatus.COMPLETED else 20,
    )
    return record


@pytest.mark.asyncio
async def test_history_survives_prune_and_reopen_without_private_context(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    registry = TransferRegistry(database_path=path, ttl_seconds=60)
    record = add(registry, "kept.bin", TransferStatus.COMPLETED)
    registry.prune(now=time.time() + 61)
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
    reopened = TransferRegistry(database_path=path)
    assert reopened.list() == []
    assert (await reopened.history())["jobs"][0]["id"] == record.id
    reopened.close()


@pytest.mark.asyncio
async def test_name_search_filters_before_cursor_pagination_across_archives(tmp_path):
    registry = TransferRegistry(
        database_path=tmp_path / "search.sqlite3", ttl_seconds=1
    )
    for index in range(75):
        add(
            registry,
            f"other-{index}.bin",
            TransferStatus.COMPLETED,
            created=2000 + index,
        )
    for index in range(3):
        add(
            registry,
            f"Résumé [.*] {index}.txt",
            TransferStatus.COMPLETED,
            created=1000 + index,
            identity=f"match-{index}",
        )
    registry.prune(now=time.time() + 2)
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
    registry = TransferRegistry(database_path=tmp_path / "jobs.sqlite3", ttl_seconds=1)
    for index in range(5):
        add(
            registry,
            str(index),
            TransferStatus.COMPLETED,
            created=1000 + index,
            identity="history-" + str(index),
        )
    add(registry, "not-a-transfer", TransferStatus.COMPLETED, kind="delete")
    registry.prune(now=time.time() + 2)
    queued = add(registry, "queued", TransferStatus.QUEUED, created=1006)
    add(registry, "running", TransferStatus.RUNNING, created=1007)
    add(registry, "failed", TransferStatus.ERROR, created=1008)
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
    registry.retry((await registry.history(status="error"))["jobs"][0]["id"])
    assert (await registry.history(status="error"))["jobs"] == []
    registry.close()


def test_history_http_owner_boundary_and_validation(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "catalog.sqlite3")
    runtime = FakeRuntime(store)
    record = add(runtime.transfers.registry, "owner-only.bin", TransferStatus.COMPLETED)
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
    registry = TransferRegistry(
        database_path=tmp_path / "jobs.sqlite3" if persistent else None,
        ttl_seconds=1,
    )
    try:
        add(registry, "success", TransferStatus.COMPLETED, created=1000)
        add(registry, "cancelled", TransferStatus.CANCELLED, created=1001)
        registry.prune(now=time.time() + 2)
        add(registry, "failed", TransferStatus.ERROR, created=1002)
        add(registry, "running", TransferStatus.RUNNING, created=1003)
        add(registry, "queued", TransferStatus.QUEUED, created=1004)
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
        registry.retry(first["jobs"][0]["id"])
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

    registry = TransferRegistry(
        database_path=tmp_path / "concurrent.sqlite3", ttl_seconds=1
    )
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
            add(
                registry,
                f"Résumé {index}",
                TransferStatus.COMPLETED,
                created=1000 + index,
            )
            registry.prune(now=time.time() + 2)
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


@pytest.fixture(params=["memory", "sqlite", "mongo"])
def task_registry(request, tmp_path):
    registry = TransferRegistry(
        database_path=tmp_path / "tasks.sqlite3" if request.param == "sqlite" else None,
        mongo=HistoryMongo() if request.param == "mongo" else None,
        ttl_seconds=1,
    )
    try:
        yield registry
    finally:
        registry.close()


def folder_metadata(identity="folder-session-0001", name="Holiday photos", created=1000):
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
        TransferStatus.COMPLETED, TransferStatus.ERROR, TransferStatus.CANCELLED
    ):
        add(registry, "archived-" + status, status, kind="legacy")
    add(registry, "active-child", TransferStatus.COMPLETED, folder_upload=active_folder)
    add(registry, "browser-child", TransferStatus.COMPLETED, folder_upload=browser_folder)
    running = add(
        registry, "running", TransferStatus.RUNNING, folder_upload=active_folder
    )
    registry.prune(now=time.time() + 2)
    for status in (TransferStatus.COMPLETED, TransferStatus.CANCELLED):
        add(registry, "live-" + status, status)
    failed = add(registry, "retryable", TransferStatus.ERROR)
    queued = add(registry, "queued", TransferStatus.QUEUED)
    deletion = add(registry, "deletion", TransferStatus.COMPLETED, kind="delete")

    assert registry.clear_history(exclude_folders=[browser_folder["id"]]) == 6
    assert registry.clear_history(exclude_folders=[browser_folder["id"]]) == 0
    names = {row["name"] for row in (await registry.history())["jobs"]}
    assert names == {"active-child", "browser-child", "running", "queued"}
    assert registry.get(failed.id).context["source_url"].endswith("token=secret")
    assert registry.get(failed.id).history_hidden
    assert not registry.get(running.id).history_hidden
    assert not registry.get(queued.id).history_hidden
    assert not registry.get(deletion.id).history_hidden
    assert (await registry.task_history(status="finished"))["total_tasks"] == 1

    registry.retry(failed.id)
    assert not registry.get(failed.id).history_hidden
    assert failed.id in {row["id"] for row in (await registry.history())["jobs"]}
    registry.prune(now=time.time() + 2)
    assert not any(
        row["name"].startswith(("archived-", "live-"))
        for row in (await registry.history())["jobs"]
    )


async def test_cleared_history_stays_cleared_after_restart_and_pruning(tmp_path):
    path = tmp_path / "clear-history.sqlite3"
    registry = TransferRegistry(database_path=path, ttl_seconds=1)
    completed = add(registry, "completed", TransferStatus.COMPLETED)
    failed = add(registry, "failed", TransferStatus.ERROR)
    assert registry.clear_history() == 2
    registry.close()
    reopened = TransferRegistry(database_path=path, ttl_seconds=1)
    try:
        assert reopened.get(failed.id).history_hidden
        assert (await reopened.history())["jobs"] == []
        reopened.prune(now=time.time() + 2)
        assert (await reopened.task_history())["total_tasks"] == 0
        with pytest.raises(TransferNotFound):
            reopened.get(completed.id)
    finally:
        reopened.close()


def test_clear_history_api_requires_login_and_keeps_live_jobs(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore(tmp_path / "clear-api.sqlite3")
    runtime = FakeRuntime(store)
    finished = add(runtime.transfers.registry, "finished", TransferStatus.COMPLETED)
    queued = add(runtime.transfers.registry, "queued", TransferStatus.QUEUED)
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
        add(
            registry,
            f"image-{index}.jpg",
            TransferStatus.COMPLETED,
            identity=f"image-{index}",
            created=1000 + index,
            folder_upload=folder,
        ).id
        for index in range(72)
    ]
    registry.prune(now=time.time() + 2)
    for index in range(21):
        add(
            registry,
            f"separate-{index}.bin",
            TransferStatus.COMPLETED,
            created=2000 + index,
            kind="remote" if index % 2 else "upload",
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
        add(
            registry,
            f"photo-{index}.jpg",
            TransferStatus.COMPLETED,
            folder_upload=folder,
        )
    registry.prune(now=time.time() + 2)
    running = add(
        registry,
        "special-child.jpg",
        TransferStatus.RUNNING,
        created=1100,
        folder_upload=folder,
    )
    add(registry, "standalone.bin", TransferStatus.COMPLETED, created=1200)
    finished = await registry.task_history(status="finished", limit=1)
    assert finished["total_tasks"] == 1
    assert [row["name"] for row in finished["jobs"]] == ["standalone.bin"]
    for term in ["RÉSUMÉ [.*]", "special-child"]:
        matching = await registry.task_history(status="running", query=term, limit=1)
        assert matching["total_tasks"] == 1
        assert len(matching["jobs"]) == 4
    registry.update(running.id, status=TransferStatus.COMPLETED)
    assert (await registry.task_history(status="running"))["jobs"] == []
    assert (await registry.task_history(status="finished"))["total_tasks"] == 2


async def test_legacy_folder_hints_are_ephemeral_and_durable_metadata_wins(task_registry):
    registry = task_registry
    legacy = [
        add(registry, f"legacy-{index}", TransferStatus.COMPLETED).id
        for index in range(3)
    ]
    registry.prune(now=time.time() + 2)
    durable = folder_metadata("durable-folder-0001", created=1200)
    recent = add(
        registry, "durable-child", TransferStatus.COMPLETED, folder_upload=durable
    )
    hint = folder_metadata("legacy-folder-0001", created=1100)
    hints = [{"folder_upload": hint, "transfer_ids": [*legacy, recent.id]}]
    page = await registry.task_history(limit=1, page=2, folders=hints)
    assert page["total_tasks"] == 2
    assert {row["id"] for row in page["jobs"]} == set(legacy)
    assert all(row["folder_upload"] == hint for row in page["jobs"])
    newer = await registry.task_history(limit=1, folders=hints)
    assert newer["jobs"][0]["folder_upload"] == durable
    assert (await registry.task_history())["total_tasks"] == 4
    excluded = await registry.task_history(folders=hints, exclude_folders=[hint["id"]])
    assert excluded["total_tasks"] == 1
    assert [row["id"] for row in excluded["jobs"]] == [recent.id]
    offset = await registry.task_history(folders=hints, offset=1, limit=1)
    assert {row["id"] for row in offset["jobs"]} == set(legacy)
    assert offset["offset"] == 1 and offset["next_offset"] == 2


async def test_folder_task_metadata_survives_sqlite_history_reopen(tmp_path):
    path = tmp_path / "folder-history.sqlite3"
    registry = TransferRegistry(database_path=path, ttl_seconds=1)
    folder = folder_metadata()
    for index in range(3):
        add(registry, f"child-{index}", TransferStatus.COMPLETED, folder_upload=folder)
    registry.prune(now=time.time() + 2)
    registry.close()
    reopened = TransferRegistry(database_path=path)
    try:
        page = await reopened.task_history(limit=1)
        assert page["total_tasks"] == 1
        assert len(page["jobs"]) == 3
        assert all(row["folder_upload"] == folder for row in page["jobs"])
        assert all(not row["retained"] for row in page["jobs"])
    finally:
        reopened.close()


def test_task_history_http_hints_bounds_and_owner_boundary(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "task-catalog.sqlite3")
    runtime = FakeRuntime(store)
    registry = runtime.transfers.registry
    children = [
        add(registry, str(index), TransferStatus.COMPLETED).id for index in range(3)
    ]
    route = "/api/v1/transfers/history/tasks"
    payload = {
        "status": "finished",
        "page": 1,
        "limit": 1,
        "folders": [{"folder_upload": folder_metadata(), "transfer_ids": children}],
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
            assert client.get("/api/v1/transfers/history", params={"page": 1}).json()[
                "total_tasks"
            ] == 3
            assert client.post(route, json=payload | {"offset": -1}).status_code == 422
            assert client.post(route, json=payload | {"page": 0}).status_code == 422
            oversized = payload | {
                "folders": [
                    {"folder_upload": folder_metadata(), "transfer_ids": ["a"] * 2000},
                    {"folder_upload": folder_metadata(), "transfer_ids": ["b"]},
                ]
            }
            assert client.post(route, json=oversized).status_code == 422
            large_hints = payload | {
                "folders": [
                    {
                        "folder_upload": folder_metadata(),
                        "transfer_ids": ["x" * 128] * 1000,
                    }
                ]
            }
            assert len(json.dumps(large_hints)) > 65536
            assert client.post(route, json=large_hints).status_code == 200
            assert client.post(route, content=b"x" * (256 * 1024 + 1)).status_code == 413
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
