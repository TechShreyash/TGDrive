from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from test_app import FakeRuntime, login, make_settings

from tgdrive.app import create_app
from tgdrive.store import DriveStore
from tgdrive.tasks import TransferNotFound, TransferRegistry, TransferStatus


def add(registry, name, status, *, created=1000, identity=None, kind="upload"):
    record = registry.create("uploading", total=100, transfer_id=identity)
    record.context.update(
        name=name,
        destination="/",
        kind=kind,
        local_path="/private/staged-file",
        source_url="https://private/?token=secret",
    )
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
