from __future__ import annotations

from datetime import UTC, datetime
from threading import local

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, make_settings
from tgdrive.core.errors import ValidationError
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import MAX_SEARCH_OFFSET, DriveStore
from tgdrive.web.app import create_app


@pytest.fixture
def store():
    result = DriveStore.create()
    yield result
    result.close()


@pytest.fixture
def search_client(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore.create()
    runtime = FakeRuntime(store)
    app = create_app(settings, runtime=runtime)
    with TestClient(app, base_url="https://testserver") as client:
        yield client, store, runtime
    store.close()
    runtime.transfers.registry.close()


def full(record):
    return record["path"].rstrip("/") + "/" + record["id"]


def owner(client):
    assert (
        client.post(
            "/api/checkPassword", json={"pass": "correct horse battery staple"}
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    ("name", "query"),
    [
        ("Straße.txt", "STRASSE"),
        ("Café.txt", "CAFE\u0301"),
        ("План.txt", "ПЛАН"),
        ("κόσμος.txt", "ΚΌΣΜΟΣ"),
        ("100% ready.txt", "%"),
        ("under_score.txt", "_"),
        ("literal[?]*.txt", "[?]*"),
        ("payload' OR 1=1 --.txt", "' OR 1=1 --"),
    ],
)
def test_unicode_normalized_casefold_and_sql_special_characters_are_literal(
    store, name, query
):
    match = store.add_file("/", name, 1, 3)
    store.add_file("/", "Unrelated plain file.txt", 2, 4)
    page = store.search_page(query)
    assert [row["id"] for row in page["items"]] == [match["id"]]
    assert page["items"][0]["parent_names"] == []
    assert not page["has_more"] and page["next_offset"] is None


def test_filename_only_search_prunes_entire_trash_branch_and_builds_human_parents(
    store,
):
    parent = store.create_folder("/", "Projects")
    nested = store.create_folder(full(parent), "Café reports")
    live = store.add_file(full(nested), "Quarterly report.csv", 1, 3)
    trash = store.create_folder("/", "Old report folder")
    hidden = store.add_file(full(trash), "Quarterly report private.csv", 2, 4)
    deeper = store.create_folder(full(trash), "Nested report folder")
    store.add_file(full(deeper), "report deep.txt", 3, 5)
    store.set_trashed(full(trash), True)
    page = store.search_page("report")
    assert {row["id"] for row in page["items"]} == {nested["id"], live["id"]}
    result = next(row for row in page["items"] if row["id"] == live["id"])
    assert result["parent_names"] == ["Projects", "Café reports"]
    assert result["path"] == full(nested)
    assert {row["id"] for row in store.search_page("Projects")["items"]} == {
        parent["id"]
    }
    assert hidden["id"] in store.search("report", include_trashed=True)


def search_implementation(store, implementation):
    if implementation == "sqlite":
        return store
    # Mongo search reads its consistent catalog cache. Exercise that real read
    # path without requiring a running MongoDB service for ordering unit tests.
    reader = object.__new__(MongoStore)
    reader._local = local()
    reader._closed = False
    with store._transaction() as db:
        reader._committed = {
            "nodes": {
                row["id"]: dict(row) for row in db.execute("SELECT * FROM nodes")
            },
            "deletions": {},
        }
    return reader


@pytest.mark.parametrize("implementation", ["sqlite", "mongo-cache"])
def test_sort_and_pagination_apply_globally_with_deterministic_ties(
    store, implementation
):
    folder = store.create_folder("/", "z report folder")
    records = [
        store.add_file("/", name, index + 1, size)
        for index, (name, size) in enumerate(
            [
                ("z report.txt", 1),
                ("a report.txt", 100),
                ("m report.txt", 100),
            ]
        )
    ]
    with store._transaction(write=True) as db:
        for index, row in enumerate(records):
            timestamp = datetime(2020 + index, 1, 1, tzinfo=UTC).isoformat()
            db.execute(
                "UPDATE nodes SET upload_date=? WHERE id=?", (timestamp, row["id"])
            )
    target = search_implementation(store, implementation)
    expected = {
        ("name", None): [1, 2, 0],
        ("name", "asc"): [1, 2, 0],
        ("name", "desc"): [0, 2, 1],
        ("size", None): [1, 2, 0],
        ("size", "desc"): [1, 2, 0],
        ("size", "asc"): [0, 1, 2],
        ("date", None): [2, 1, 0],
        ("date", "desc"): [2, 1, 0],
        ("date", "asc"): [0, 1, 2],
    }
    for (sort, order), indices in expected.items():
        identities = [folder["id"], *[records[index]["id"] for index in indices]]
        first = target.search_page("report", limit=2, sort=sort, order=order)
        assert first["has_more"] and first["next_offset"] == 2
        second = target.search_page(
            "report", offset=first["next_offset"], limit=2, sort=sort, order=order
        )
        assert not second["has_more"] and second["next_offset"] is None
        assert [row["id"] for row in first["items"] + second["items"]] == identities
    assert target.search_page("report", offset=MAX_SEARCH_OFFSET)["items"] == []


@pytest.mark.parametrize("implementation", ["sqlite", "mongo-cache"])
@pytest.mark.parametrize("sort", ["name", "date", "size"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_equal_search_keys_keep_stable_id_order_and_hidden_branches_stay_hidden(
    store, implementation, sort, order
):
    parents = [store.create_folder("/", name) for name in ("One", "Two", "Private")]
    records = [
        store.add_file(full(parent), "report.txt", index + 1, 10)
        for index, parent in enumerate(parents)
    ]
    store.set_trashed(full(parents[2]), True)
    with store._transaction(write=True) as db:
        db.execute("UPDATE nodes SET upload_date='2025-01-01T00:00:00Z'")
    target = search_implementation(store, implementation)
    pages = [
        target.search_page("report", sort=sort, order=order, limit=1, offset=offset)
        for offset in range(2)
    ]
    assert [page["items"][0]["id"] for page in pages] == sorted(
        row["id"] for row in records[:2]
    )
    assert pages[0]["has_more"] and not pages[1]["has_more"]


@pytest.mark.parametrize("implementation", ["sqlite", "mongo-cache"])
def test_trash_lists_human_parent_names_without_hidden_children(store, implementation):
    parent = store.create_folder("/", "Books")
    nested = store.create_folder(full(parent), "Reference")
    root_file = store.add_file("/", "root-deleted.txt", 1, 10)
    nested_file = store.add_file(full(nested), "nested-deleted.txt", 2, 10)
    hidden = store.add_file(full(nested), "active.txt", 3, 10)
    trashed_folder = store.create_folder(full(parent), "Old")
    child = store.add_file(full(trashed_folder), "hidden-deleted.txt", 4, 10)
    for row in (root_file, nested_file, trashed_folder, child):
        store.set_trashed(full(row), True)
    target = search_implementation(store, implementation)
    items = target.list_trash()
    assert set(items) == {root_file["id"], nested_file["id"], trashed_folder["id"]}
    assert items[root_file["id"]]["parent_names"] == []
    assert items[nested_file["id"]]["parent_names"] == ["Books", "Reference"]
    assert items[nested_file["id"]]["path"] == full(nested)
    assert items[trashed_folder["id"]]["parent_names"] == ["Books"]
    assert hidden["id"] not in items and child["id"] not in items
    assert list(target.list_trash("nested")) == [nested_file["id"]]


def test_search_has_one_data_query_instead_of_per_result_ancestry_queries(
    store, monkeypatch
):
    folder = store.create_folder("/", "Nested")
    for index in range(40):
        store.add_file(full(folder), f"Report {index:02d}.txt", index + 1, index)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Search must not resolve ancestry per result")

    monkeypatch.setattr(store, "_path_for_id", forbidden)
    monkeypatch.setattr(store, "_resolve", forbidden)
    statements = []
    store._db.set_trace_callback(statements.append)
    try:
        page = store.search_page("report", limit=10)
    finally:
        store._db.set_trace_callback(None)
    assert len(page["items"]) == 10 and page["has_more"]
    assert len(statements) == 3  # BEGIN, one recursive read, COMMIT.


@pytest.mark.parametrize(
    ("query", "options"),
    [
        ("", {}),
        ("   ", {}),
        ("x" * 256, {}),
        ("a\x00b", {}),
        ("a\nb", {}),
        ("file", {"offset": -1}),
        ("file", {"offset": MAX_SEARCH_OFFSET + 1}),
        ("file", {"limit": 0}),
        ("file", {"limit": 501}),
        ("file", {"limit": True}),
        ("file", {"sort": "name; DROP TABLE nodes"}),
        ("file", {"order": "ASC"}),
        ("file", {"order": "desc; DROP TABLE nodes"}),
        ("file", {"order": []}),
    ],
)
def test_search_validates_query_pagination_and_sort(store, query, options):
    with pytest.raises(ValidationError):
        store.search_page(query, **options)


def test_api_owner_only_search_does_not_accept_public_share_tokens(search_client):
    client, store, _ = search_client
    shared = store.create_folder("/", "Public")
    public = store.add_file(full(shared), "report shared.txt", 1, 3)
    private = store.add_file("/", "report private.txt", 2, 4)
    token = store.create_share(full(shared))
    assert (
        client.get("/api/v1/search", params={"q": "report", "auth": token}).status_code
        == 401
    )
    denied = client.post(
        "/api/getDirectory", json={"path": "/search_report", "auth": token}
    )
    assert denied.status_code == 401
    assert "report private" not in denied.text
    owner(client)
    response = client.get("/api/v1/search", params={"q": "REPORT", "limit": 1})
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    first = response.json()
    assert first["has_more"] and first["next_offset"] == 1
    second = client.get(
        "/api/v1/search", params={"q": "REPORT", "offset": 1, "limit": 1}
    ).json()
    assert {item["id"] for item in first["items"] + second["items"]} == {
        public["id"],
        private["id"],
    }
    assert not second["has_more"]
    assert "message_id" not in str(first)


@pytest.mark.parametrize(
    "parameters",
    [
        {"q": ""},
        {"q": " "},
        {"q": "x" * 256},
        {"q": "a\x00b"},
        {"q": "report", "limit": 201},
        {"q": "report", "offset": -1},
        {"q": "report", "sort": "modified"},
        {"q": "report", "order": "ascending"},
    ],
)
def test_api_rejects_invalid_search_inputs(search_client, parameters):
    client, _, _ = search_client
    owner(client)
    assert client.get("/api/v1/search", params=parameters).status_code == 422


def test_api_direction_applies_before_offset_pagination(search_client):
    client, store, _ = search_client
    folder = store.create_folder("/", "a report folder")
    first = store.add_file("/", "a report.txt", 1, 10)
    last = store.add_file("/", "z report.txt", 2, 20)
    owner(client)
    for sort in ("name", "size"):
        response = client.get(
            "/api/v1/search",
            params={"q": "report", "sort": sort, "order": "desc", "limit": 2},
        )
        assert response.status_code == 200
        page = response.json()
        assert [item["id"] for item in page["items"]] == [folder["id"], last["id"]]
        response = client.get(
            "/api/v1/search",
            params={
                "q": "report",
                "sort": sort,
                "order": "desc",
                "limit": 2,
                "offset": page["next_offset"],
            },
        )
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == [first["id"]]


def test_legacy_search_is_bounded_and_exposes_more_results_instead_of_fake_total(
    search_client,
):
    client, store, _ = search_client
    for index in range(502):
        store.add_file("/", f"Report {index:03d}.txt", index + 1, index)
    owner(client)
    response = client.post("/api/getDirectory", json={"path": "/search_Report"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["contents"]) == 500
    assert data["search"] == {"query": "Report", "has_more": True, "next_offset": 500}
    assert "total" not in data["search"]
    assert len(store.search("report")) == 500


def test_search_remains_readable_while_restart_required(search_client):
    client, store, runtime = search_client
    store.add_file("/", "report.txt", 1, 3)
    owner(client)
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    runtime.connection_manager = SimpleNamespace(
        restart_required=True, close=AsyncMock()
    )
    assert client.get("/api/v1/search", params={"q": "report"}).status_code == 200
