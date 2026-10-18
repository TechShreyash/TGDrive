from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_app import FakeRuntime, make_settings

from tgdrive.app import create_app
from tgdrive.errors import ValidationError
from tgdrive.store import MAX_SEARCH_OFFSET, DriveStore


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


def test_sort_and_pagination_apply_globally_with_deterministic_ties(store):
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
    expected = {
        "name": [folder["id"], records[1]["id"], records[2]["id"], records[0]["id"]],
        "size": [folder["id"], records[1]["id"], records[2]["id"], records[0]["id"]],
        "date": [folder["id"], records[2]["id"], records[1]["id"], records[0]["id"]],
    }
    for sort, identities in expected.items():
        first = store.search_page("report", limit=2, sort=sort)
        assert first["has_more"] and first["next_offset"] == 2
        second = store.search_page(
            "report", offset=first["next_offset"], limit=2, sort=sort
        )
        assert not second["has_more"] and second["next_offset"] is None
        assert [row["id"] for row in first["items"] + second["items"]] == identities
    assert store.search_page("report", offset=MAX_SEARCH_OFFSET)["items"] == []


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
    ],
)
def test_api_rejects_invalid_search_inputs(search_client, parameters):
    client, _, _ = search_client
    owner(client)
    assert client.get("/api/v1/search", params=parameters).status_code == 422


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
