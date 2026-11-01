from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, make_settings
from tests.support.mongo_fixtures import new_store
from tgdrive.core.errors import NotFoundError, ValidationError
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.web.app import create_app


@pytest.fixture
def store():
    result = new_store()
    yield result
    result.close()


@pytest.fixture
def search_client(tmp_path):
    settings = make_settings(tmp_path)
    store = new_store()
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
    assert hidden["id"] in {
        row["id"] for row in store.search_page("report", include_trashed=True)["items"]
    }


def set_upload_date(store, value, identities=None):
    """Rewrite stored timestamps directly; reads always come from MongoDB."""
    for row in store.backend.db.nodes.rows:
        if identities is None or row["id"] in identities:
            row["upload_date"] = value


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
    for index, row in enumerate(records):
        timestamp = datetime(2020 + index, 1, 1, tzinfo=UTC).isoformat()
        set_upload_date(store, timestamp, {row["id"]})
    target = store
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
    assert target.search_page("report", offset=10**9)["items"] == []


@pytest.mark.parametrize("sort", ["name", "date", "size"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_equal_search_keys_keep_stable_id_order_and_hidden_branches_stay_hidden(
    store, sort, order
):
    parents = [store.create_folder("/", name) for name in ("One", "Two", "Private")]
    records = [
        store.add_file(full(parent), "report.txt", index + 1, 10)
        for index, parent in enumerate(parents)
    ]
    store.set_trashed(full(parents[2]), True)
    set_upload_date(store, "2025-01-01T00:00:00Z")
    target = store
    pages = [
        target.search_page("report", sort=sort, order=order, limit=1, offset=offset)
        for offset in range(2)
    ]
    assert [page["items"][0]["id"] for page in pages] == sorted(
        row["id"] for row in records[:2]
    )
    assert pages[0]["has_more"] and not pages[1]["has_more"]


def test_trash_lists_human_parent_names_without_hidden_children(store):
    parent = store.create_folder("/", "Books")
    nested = store.create_folder(full(parent), "Reference")
    root_file = store.add_file("/", "root-deleted.txt", 1, 10)
    nested_file = store.add_file(full(nested), "nested-deleted.txt", 2, 10)
    hidden = store.add_file(full(nested), "active.txt", 3, 10)
    trashed_folder = store.create_folder(full(parent), "Old")
    child = store.add_file(full(trashed_folder), "hidden-deleted.txt", 4, 10)
    for row in (root_file, nested_file, trashed_folder, child):
        store.set_trashed(full(row), True)
    target = store
    items = target.list_trash()
    assert set(items) == {root_file["id"], nested_file["id"], trashed_folder["id"]}
    assert items[root_file["id"]]["parent_names"] == []
    assert items[nested_file["id"]]["parent_names"] == ["Books", "Reference"]
    assert items[nested_file["id"]]["path"] == full(nested)
    assert items[trashed_folder["id"]]["parent_names"] == ["Books"]
    assert hidden["id"] not in items and child["id"] not in items
    assert list(target.list_trash("nested")) == [nested_file["id"]]


@pytest.mark.parametrize(
    ("query", "options"),
    [
        ("", {}),
        ("   ", {}),
        ("x" * 256, {}),
        ("a\x00b", {}),
        ("a\nb", {}),
        ("file", {"offset": -1}),
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
    assert len(store.search_page("report", limit=500)["items"]) == 500


def test_deep_folders_open_by_id_with_server_breadcrumbs_and_share_boundary(
    search_client,
):
    client, store, _ = search_client
    path, chain = "/", []
    for level in range(400):  # Far beyond the former 128-level limit.
        folder = store.create_folder(path, f"Level {level}")
        path = full(folder)
        chain.append(folder)
    owner(client)
    deepest = "/" + chain[-1]["id"]  # A short URL at any depth.
    response = client.post("/api/getDirectory", json={"path": deepest})
    assert response.status_code == 200
    crumbs = response.json()["data"]["breadcrumbs"]
    assert len(crumbs) == 401 and crumbs[-1]["name"] == "Level 399"
    page = client.get("/api/v1/nodes", params={"parent_id": "/" + chain[-2]["id"]})
    assert [row["id"] for row in page.json()["items"]] == [chain[-1]["id"]]

    token = store.create_share("/" + chain[200]["id"], expires_at=None)
    client.post("/api/logout")
    shared = client.post(
        "/api/getDirectory",
        json={"path": "/share_" + chain[300]["id"], "auth": token},
    )
    assert shared.status_code == 200
    trail = shared.json()["data"]["breadcrumbs"]
    # Folder names above the shared folder stay private.
    assert trail[0]["id"] == chain[200]["id"] and len(trail) == 101
    outside = client.post(
        "/api/getDirectory",
        json={"path": "/share_" + chain[100]["id"], "auth": token},
    )
    assert outside.status_code == 403


@pytest.mark.parametrize("sort", ["name", "size", "date", "type"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_folder_cursor_pages_match_offset_pages(store, sort, order):
    for index in range(5):
        store.create_folder("/", f"Folder {index}")
    for index in range(9):
        store.add_file("/", f"file {index}.bin", index + 1, index % 3)
    expected = store.list_page("/", limit=100, sort=sort, order=order)["items"]
    seen, cursor = [], None
    while True:
        page = store.list_page("/", limit=4, sort=sort, order=order, after=cursor)
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [row["id"] for row in seen] == [row["id"] for row in expected]
    assert len(seen) == 14 and all(row["type"] == "folder" for row in seen[:5])


@pytest.mark.parametrize(
    "cursor", ["%%%", "e30", "WyJzaXplIiwiZmlsZSIsIngiLCJ5Il0", "x" * 2000]
)
def test_folder_cursor_rejects_tampered_values(store, cursor):
    with pytest.raises(ValidationError):
        store.list_page("/", sort="size", after=cursor)


def test_api_folder_cursor_continues_listing(search_client):
    client, store, _ = search_client
    for index in range(3):
        store.add_file("/", f"page {index}.txt", index + 1, 1)
    owner(client)
    first = client.get("/api/v1/nodes", params={"limit": 2}).json()
    second = client.get(
        "/api/v1/nodes", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    names = [row["name"] for row in first["items"] + second["items"]]
    assert names == ["page 0.txt", "page 1.txt", "page 2.txt"]
    assert second["next_cursor"] is None
    bad = client.get("/api/v1/nodes", params={"cursor": "%%%"})
    assert bad.status_code == 422


def test_folder_type_filter_matches_ui_groups_and_survives_rename(store):
    store.create_folder("/", "Albums")
    names = ["a.JPG", "b.mkv", "c.pdf", "d.epub", "e.flac", "f.zip", "g", "h.xlsx"]
    for index, name in enumerate(names):
        store.add_file("/", name, index + 1, 1)

    def names_in(group):
        return [row["name"] for row in store.list_page("/", group=group)["items"]]

    assert names_in("folder") == ["Albums"]
    assert names_in("media") == ["a.JPG", "b.mkv"]
    assert names_in("docs") == ["c.pdf", "d.epub", "h.xlsx"]
    assert names_in("audio") == ["e.flac"]
    assert names_in("other") == ["f.zip", "g"]
    assert store.list_page("/", group="media")["total"] == 2
    page = store.list_page("/", group="docs", limit=2)
    rest = store.list_page("/", group="docs", limit=2, after=page["next_cursor"])
    assert [row["name"] for row in rest["items"]] == ["h.xlsx"]
    item = next(row for row in store.list_page("/")["items"] if row["name"] == "g")
    store.rename(full(item), "g.mp3")  # A file without an extension may gain one.
    assert names_in("audio") == ["e.flac", "g.mp3"]
    with pytest.raises(ValidationError):
        store.list_page("/", group="videos")


def test_type_field_is_backfilled_for_existing_nodes(store):
    store.add_file("/", "song.mp3", 1, 1)
    store.db.nodes.update_many({}, {"$unset": {"group": ""}})
    MongoStore._indexes(store.backend)
    assert [row["name"] for row in store.list_page("/", group="audio")["items"]] == [
        "song.mp3"
    ]


def test_shared_folder_pages_need_the_share_and_hide_starred(search_client):
    client, store, _ = search_client
    shared = store.create_folder("/", "Public")
    inner = store.create_folder(full(shared), "Inner")
    for index in range(3):
        store.add_file(full(inner), f"photo {index}.jpg", index + 1, 1)
    store.add_file(full(inner), "notes.txt", 9, 1)
    store.add_file("/", "private.jpg", 10, 1)
    store.set_starred(
        full(inner) + "/" + store.list_page(full(inner))["items"][0]["id"], True
    )
    token = store.create_share(full(shared))
    route = "/api/v1/shared/nodes"
    first = client.get(
        route,
        params={"auth": token, "parent_id": full(inner), "limit": 2, "type": "media"},
    )
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-store"
    body = first.json()
    assert [row["name"] for row in body["items"]] == ["photo 0.jpg", "photo 1.jpg"]
    assert body["share_home"] == full(shared) and body["total"] == 3
    assert all("starred" not in row for row in body["items"])
    second = client.get(
        route,
        params={
            "auth": token,
            "parent_id": full(inner),
            "type": "media",
            "cursor": body["next_cursor"],
        },
    ).json()
    assert [row["name"] for row in second["items"]] == ["photo 2.jpg"]
    # Outside the share, a wrong token, a file or a missing ID all look alike.
    for parent, auth in [
        ("/", token),
        (full(inner), "wrong-token"),
        ("/" + "x" * 22, token),
    ]:
        denied = client.get(route, params={"auth": auth, "parent_id": parent})
        assert denied.status_code == 403
        assert "private.jpg" not in denied.text
    assert client.get(route, params={"parent_id": full(inner)}).status_code == 422
    owner(client)
    mine = client.get("/api/v1/nodes", params={"type": "media"}).json()
    assert [row["name"] for row in mine["items"]] == ["private.jpg"]


@pytest.mark.parametrize("sort", ["name", "size", "date", "type"])
@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("group", [None, "media"])
def test_around_starts_at_the_item_and_reports_its_index(store, sort, order, group):
    for index in range(3):
        store.create_folder("/", f"Folder {index}")
    for index in range(8):
        store.add_file("/", f"clip {index}.mp4", index + 1, index % 3)
    store.add_file("/", "notes.txt", 20, 5)
    every = store.list_page("/", limit=100, sort=sort, order=order, group=group)
    ids = [row["id"] for row in every["items"]]
    for position, identity in enumerate(ids):
        page = store.list_page(
            "/", limit=3, sort=sort, order=order, group=group, around=identity
        )
        assert page["index"] == position
        assert [row["id"] for row in page["items"]] == ids[position : position + 3]
        rest, cursor = [], page["next_cursor"]
        while cursor:
            more = store.list_page(
                "/", limit=3, sort=sort, order=order, group=group, after=cursor
            )
            rest.extend(row["id"] for row in more["items"])
            cursor = more["next_cursor"]
        assert rest == ids[position + 3 :]


def test_first_page_carries_the_folder_and_breadcrumbs(store):
    parent = store.create_folder("/", "Parent")
    child = store.create_folder(full(parent), "Child")
    store.add_file(full(child), "a.txt", 1, 1)
    page = store.list_page(full(child), limit=1)
    assert page["folder"]["id"] == child["id"] and page["folder"]["name"] == "Child"
    assert [crumb["name"] for crumb in page["folder"]["breadcrumbs"]] == [
        "/",
        "Parent",
        "Child",
    ]
    assert (
        page["folder"]["breadcrumbs"] == store.get_directory(full(child))["breadcrumbs"]
    )
    store.add_file(full(child), "b.txt", 2, 1)
    first = store.list_page(full(child), limit=1)
    assert "folder" not in store.list_page(
        full(child), limit=1, after=first["next_cursor"]
    )
    assert "folder" in store.list_page(full(child), around=first["items"][0]["id"])
    with pytest.raises(ValidationError):
        store.list_page(full(child), offset=1, around=first["items"][0]["id"])
    with pytest.raises(NotFoundError):
        store.list_page("/", around=first["items"][0]["id"])  # Not in this folder.


def test_shared_pages_trim_breadcrumbs_to_the_share(search_client):
    client, store, _ = search_client
    above = store.create_folder("/", "Private parent")
    shared = store.create_folder(full(above), "Public")
    inner = store.create_folder(full(shared), "Inner")
    item = store.add_file(full(inner), "photo.jpg", 1, 1)
    token = store.create_share(full(shared))
    body = client.get(
        "/api/v1/shared/nodes",
        params={"auth": token, "parent_id": full(inner), "around": item["id"]},
    ).json()
    assert body["index"] == 0 and body["items"][0]["id"] == item["id"]
    assert [crumb["name"] for crumb in body["folder"]["breadcrumbs"]] == [
        "Public",
        "Inner",
    ]
    assert "Private parent" not in json.dumps(body)
    outside = client.get(
        "/api/v1/shared/nodes",
        params={"auth": token, "parent_id": full(above), "around": shared["id"]},
    )
    assert outside.status_code == 403
