"""Navigation reuses listing data and upload refreshes keep share badges stable."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import pytest

from tests.browser.test_browser_e2e import Server, expect, fixture_nodes, login
from tests.browser.test_browser_e2e import browser as browser_fixture
from tests.browser.test_browser_e2e import desktop as desktop_fixture
from tests.browser.test_browser_e2e import server as server_fixture

pytestmark = pytest.mark.e2e
browser, desktop, server = browser_fixture, desktop_fixture, server_fixture


def test_folder_navigation_does_not_fetch_current_listing_for_breadcrumbs(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    folder = page.get_by_role("button", name="Preview samples", exact=True)
    expect(folder).to_be_visible()
    requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.post_data_json["path"])
            if request.url.endswith("/api/getDirectory")
            else None
        ),
    )
    folder.dblclick()
    expect(page.get_by_role("button", name="sample.json", exact=True)).to_be_visible()
    expect(page.get_by_role("navigation", name="Breadcrumb")).to_contain_text(
        "My Files"
    )
    assert len(requests) == 1


@pytest.mark.parametrize("navigate_away", [False, True])
def test_breadcrumbs_come_with_the_listing_and_obsolete_requests_cancel(
    server: Server, desktop: tuple[Any, Any], navigate_away: bool
) -> None:
    _, page = desktop
    login(page, server.url)
    parent = next(
        item
        for item in fixture_nodes(page, server, "/")
        if item["name"] == "Preview samples"
    )
    parent_path = "/" + parent["id"]
    created = page.request.post(
        server.url + "/api/createNewFolder",
        data={"path": parent_path, "name": "Nested folder"},
    )
    assert created.ok
    child = next(
        item
        for item in fixture_nodes(page, server, parent_path)
        if item["name"] == "Nested folder"
    )
    requests: list[str] = []
    held: list[Any] = []

    def directory(route: Any) -> None:
        path = route.request.post_data_json["path"]
        requests.append(path)
        if navigate_away and path == "/" + child["id"]:
            held.append(route.request)
        else:
            route.continue_()

    page.route("**/api/getDirectory", directory)
    # The URL carries only the folder's own ID, at any depth.
    page.goto(server.url + "/?" + urlencode({"path": "/" + child["id"]}))
    if navigate_away:
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(25)
        assert len(held) == 1
        page.goto(server.url + "/")
        expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    else:
        expect(
            page.get_by_role("heading", name="Nested folder", exact=True)
        ).to_be_visible()
        expect(page.get_by_role("navigation", name="Breadcrumb")).to_contain_text(
            "Preview samples"
        )
        # One listing request returns the breadcrumbs; ancestors are not fetched.
        assert requests == ["/" + child["id"]]


@pytest.mark.parametrize("removal", ["revoke", "partial-trash"])
def test_upload_refreshes_skip_shares_but_metadata_changes_refresh_badges(
    server: Server, desktop: tuple[Any, Any], removal: str
) -> None:
    _, page = desktop
    shares: list[Any] = []
    page.on(
        "request",
        lambda request: (
            shares.append(request)
            if request.method == "GET" and request.url.endswith("/api/v1/shares")
            else None
        ),
    )
    login(page, server.url)
    page.get_by_label("Actions for 資料", exact=True).click()
    page.get_by_role("button", name="Share Link", exact=True).click()
    dialog = page.get_by_role("dialog", name="Share “資料”", exact=True)
    dialog.get_by_role("button", name="Create Share Link", exact=True).click()
    expect(dialog.get_by_label("Share Link", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Done", exact=True).click()
    expect(page.get_by_label("Shared by Link", exact=True)).to_have_count(1)
    before = len(shares)
    for _ in range(3):
        with page.expect_response(
            lambda response: response.url.endswith("/api/getDirectory")
        ):
            page.evaluate("window.dispatchEvent(new Event('tgdrive:files-changed'))")
        page.evaluate("""async () => {
          await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
        }""")
    assert len(shares) == before
    expect(page.get_by_label("Shared by Link", exact=True)).to_have_count(1)

    page.get_by_label("Actions for 資料", exact=True).click()
    page.get_by_role("button", name="Rename", exact=True).click()
    dialog = page.get_by_role("dialog", name="Rename", exact=True)
    dialog.get_by_label("Name", exact=True).fill("Renamed shared folder")
    with page.expect_response(
        lambda response: (
            response.request.method == "GET" and response.url.endswith("/api/v1/shares")
        )
    ):
        dialog.get_by_role("button", name="Save Name", exact=True).click()
    folder = page.get_by_role("button", name="Renamed shared folder", exact=True)
    expect(folder).to_be_visible()
    expect(page.get_by_label("Shared by Link", exact=True)).to_have_count(1)
    if removal == "partial-trash":

        def partial_trash(route: Any) -> None:
            response = route.fetch()
            payload = response.json()
            payload["results"].append({"path": "/missing", "error": "Missing item"})
            route.fulfill(response=response, json=payload)

        page.route("**/api/v1/nodes/bulk", partial_trash)
        page.get_by_label("Actions for Renamed shared folder", exact=True).click()
        with page.expect_response(
            lambda response: (
                response.request.method == "GET"
                and response.url.endswith("/api/v1/shares")
            )
        ):
            page.get_by_role("button", name="Move to Trash", exact=True).click()
        expect(folder).to_have_count(0)
        expect(page.get_by_label("Shared by Link", exact=True)).to_have_count(0)
        return
    page.get_by_role("link", name="Shared Links", exact=True).click()
    page.get_by_role(
        "button", name="Revoke Link for Renamed shared folder", exact=True
    ).click()
    page.get_by_role("dialog", name="Revoke This Link?", exact=True).get_by_role(
        "button", name="Revoke Link", exact=True
    ).click()
    expect(
        page.get_by_role("dialog", name="Revoke This Link?", exact=True)
    ).to_have_count(0)
    page.get_by_role("link", name="My Files", exact=True).click()
    expect(folder).to_be_visible()
    expect(page.get_by_label("Shared by Link", exact=True)).to_have_count(0)


def test_deep_folder_opens_from_a_short_url_with_its_full_breadcrumb(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    parent, folder = "/", None
    for level in range(150):  # Deeper than the former 128-level limit.
        created = page.request.post(
            server.url + "/api/createNewFolder",
            data={"path": parent, "name": f"Depth {level}"},
        )
        assert created.ok
        folder = next(
            item
            for item in fixture_nodes(page, server, parent)
            if item["name"] == f"Depth {level}"
        )
        parent = (
            (parent.rstrip("/") + "/" + folder["id"])
            if parent != "/"
            else ("/" + folder["id"])
        )
    page.goto(server.url + "/?" + urlencode({"path": "/" + folder["id"]}))
    expect(page.get_by_role("heading", name="Depth 149", exact=True)).to_be_visible()
    crumbs = page.get_by_role("navigation", name="Breadcrumb")
    expect(crumbs).to_contain_text("My Files")
    expect(crumbs).to_contain_text("Depth 148")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # The address bar carries only the folder's own ID.
    assert len(page.url) < len(server.url) + 60
    # Deep paths collapse to the first and last folders until expanded.
    expect(crumbs.get_by_role("link")).to_have_count(3)
    crumbs.get_by_role("button", name="Show 147 hidden folders", exact=True).click()
    expect(crumbs.get_by_role("link")).to_have_count(150)
    expect(crumbs).to_contain_text("Depth 0")
    crumbs.get_by_role("link", name="Depth 100", exact=True).click()
    expect(page.get_by_role("heading", name="Depth 100", exact=True)).to_be_visible()
