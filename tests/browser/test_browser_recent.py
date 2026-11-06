"""Recent lists opened or uploaded files across folders, paged by cursor."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from tests.browser.test_browser_e2e import Server, expect, fixture_nodes, login
from tests.browser.test_browser_e2e import browser as browser_fixture
from tests.browser.test_browser_e2e import desktop as desktop_fixture
from tests.browser.test_browser_e2e import server as server_fixture

pytestmark = pytest.mark.e2e
browser, desktop, server = browser_fixture, desktop_fixture, server_fixture
RECENT = "**/api/v1/recent?*"


def query(request: Any) -> dict[str, list[str]]:
    return parse_qs(urlparse(request.url).query)


def test_recent_lists_activity_filters_and_shows_the_folder(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    # Opening a file as the owner records it; it moves to the top of Recent.
    roots = page.request.get(server.url + "/api/v1/nodes?parent_id=/&limit=500")
    folder = next(i for i in roots.json()["items"] if i["name"] == "Private folder")
    child = page.request.get(
        server.url + "/api/v1/nodes", params={"parent_id": "/" + folder["id"]}
    ).json()["items"][0]
    opened = page.request.get(
        server.url + "/file", params={"path": "/" + folder["id"] + "/" + child["id"]}
    )
    assert opened.ok
    requests: list[dict[str, list[str]]] = []
    page.on(
        "request",
        lambda request: (
            requests.append(query(request))
            if "/api/v1/recent?" in request.url
            else None
        ),
    )
    page.get_by_role("link", name="Recent", exact=True).click()
    expect(page).to_have_url(server.url + "/?view=recent")
    expect(page.get_by_role("heading", name="Recent", exact=True)).to_be_visible()
    first = page.locator("tr.file-row").first
    expect(first.locator(".file-name")).to_have_text("private.txt")
    expect(first.locator(".search-result-location")).to_have_text(
        "My Files / Private folder"
    )
    expect(
        page.locator(".search-result-location").filter(has_text="Preview samples")
    ).not_to_have_count(0)
    assert requests[0] == {"limit": ["100"]}
    expect(page.get_by_role("button", name="Sort by Name")).to_be_disabled()
    expect(page.locator(".file-sort-trigger")).to_have_count(0)
    expect(page.get_by_role("button", name="Folders", exact=True)).to_have_count(0)

    page.get_by_role("button", name="Documents", exact=True).click()
    expect(page.locator("tr.file-row").first).to_be_visible()
    assert requests[-1]["type"] == ["docs"]
    expect(
        page.locator("tr.file-row .file-name").filter(has_text="private.txt")
    ).to_be_visible()

    page.get_by_label("Actions for private.txt", exact=True).click()
    page.get_by_role("button", name="Show in Folder", exact=True).click()
    expect(page).to_have_url(server.url + "/?path=%2F" + folder["id"])
    expect(page.locator("tr.selected .file-name")).to_have_text("private.txt")


def test_recent_pages_by_cursor_and_shows_an_empty_state(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    # The fixture drive has fewer files than one page, so a mock supplies a
    # second page; the empty case would otherwise need a drive with no files.
    _, page = desktop
    login(page, server.url)
    rows = [
        {
            "id": f"r{i}",
            "name": f"recent-{i:03}.txt",
            "type": "file",
            "path": "/",
            "size": 10,
            "upload_date": "2026-10-01T00:00:00",
            "active_at": "2026-11-01T00:00:00Z",
            "parent_names": [],
        }
        for i in range(160)
    ]
    seen: list[dict[str, list[str]]] = []

    def recent(route: Any) -> None:
        params = query(route.request)
        seen.append(params)
        start = int(params.get("cursor", ["0"])[0])
        end = min(len(rows), start + int(params["limit"][0]))
        route.fulfill(
            json={
                "items": rows[start:end],
                "next_cursor": str(end) if end < len(rows) else None,
            }
        )

    page.route(RECENT, recent)
    page.goto(server.url + "/?view=recent")
    expect(page.locator("tr.file-row")).to_have_count(100)
    expect(page.locator(".file-item-count")).to_have_text("100 items · More available")
    assert seen == [{"limit": ["100"]}]
    page.get_by_role("button", name="Next Page", exact=True).click()
    expect(page.locator("tr.file-row")).to_have_count(60)
    expect(page.locator(".file-item-count")).to_have_text("160 items")
    assert seen[-1] == {"cursor": ["100"], "limit": ["100"]}
    assert not any("offset" in params for params in seen)
    expect(page.get_by_role("button", name="Next Page", exact=True)).to_be_disabled()

    page.unroute(RECENT)
    page.route(
        RECENT, lambda route: route.fulfill(json={"items": [], "next_cursor": None})
    )
    page.reload()
    expect(
        page.get_by_role("heading", name="No Recent Files Yet", exact=True)
    ).to_be_visible()
    expect(page.get_by_text("Files you open or upload appear here.")).to_be_visible()
    expect(page.get_by_role("button", name="Upload Files", exact=True)).to_have_count(0)


def test_recent_failed_page_has_retry_and_previous(server, desktop):
    _, page = desktop
    login(page, server.url)
    calls = 0

    def recent(route):
        nonlocal calls
        continuation = "cursor" in query(route.request)
        if continuation:
            calls += 1
            if calls == 1:
                route.fulfill(status=503, json={"detail": "Temporary listing failure"})
                return
        start = 100 if continuation else 0
        route.fulfill(
            json={
                "items": [
                    {
                        "id": f"r{i}",
                        "name": f"recent-{i:03}.txt",
                        "path": "/",
                        "type": "file",
                    }
                    for i in range(start, start + (1 if continuation else 100))
                ],
                "next_cursor": None if continuation else "100",
            }
        )

    page.route(RECENT, recent)
    page.get_by_role("link", name="Recent", exact=True).click()
    expect(page.locator("tr.file-row")).to_have_count(100)
    page.get_by_role("button", name="Next Page", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Temporary listing failure")
    expect(page.get_by_role("button", name="Previous Page", exact=True)).to_be_visible()
    page.get_by_role("button", name="Try Again", exact=True).click()
    expect(
        page.get_by_role("button", name="recent-100.txt", exact=True)
    ).to_be_visible()
    assert calls == 2
    page.get_by_role("button", name="Previous Page", exact=True).click()
    expect(page.locator("tr.file-row")).to_have_count(100)


def test_later_recent_preview_reloads_and_back_returns_to_recent(server, desktop):
    _, page = desktop
    login(page, server.url)
    folder = server.control()["seed"]["folder"]
    item = next(
        row for row in fixture_nodes(page, server, folder) if row["type"] == "file"
    )
    first = [
        {"id": f"r{i}", "name": f"recent-{i:03}.txt", "type": "file", "path": "/"}
        for i in range(100)
    ]
    page.route(
        RECENT,
        lambda route: route.fulfill(
            json={
                "items": [item] if "cursor" in query(route.request) else first,
                "next_cursor": None if "cursor" in query(route.request) else "100",
            }
        ),
    )
    page.get_by_role("link", name="Recent", exact=True).click()
    page.get_by_role("button", name="Next Page", exact=True).click()
    page.get_by_role("button", name=item["name"], exact=True).dblclick()
    assert parse_qs(urlparse(page.url).query)["path"] == [
        "/" + folder.rsplit("/", 1)[-1]
    ]
    assert "view=recent" not in page.url
    page.reload()
    preview = page.locator("dialog.file-preview[open]")
    expect(preview).to_contain_text(item["name"])
    page.get_by_role("button", name="Close Preview", exact=True).click()
    expect(page).to_have_url(server.url + "/?view=recent")
    expect(preview).to_have_count(0)
