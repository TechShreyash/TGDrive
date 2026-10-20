"""Contextual searches stay within the page and preserve bounded history paging."""
# ruff: noqa: F811 - pytest fixture parameters shadow imported fixture functions.

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

from test_browser_e2e import (
    ARTIFACTS,
    browser,  # noqa: F401
    desktop,  # noqa: F401
    expect,
    login,
    server,  # noqa: F401
)

pytestmark = pytest.mark.e2e


def test_context_switch_trash_shares_and_preloads_keep_search_scoped(server, desktop):
    _, page = desktop
    login(page, server.url)
    contents = page.request.post(
        server.url + "/api/getDirectory", data={"path": "/"}
    ).json()["data"]["contents"]
    folder = next(item for item in contents.values() if item["name"] == "資料 📁")
    assert page.request.post(
        server.url + "/api/trashFileFolder",
        data={"path": "/" + folder["id"], "trash": True},
    ).ok
    page.get_by_role("link", name="Trash", exact=True).click()
    search = page.get_by_role("searchbox", name="Search Trash", exact=True)
    expect(search).to_be_visible()
    assert (
        search.locator("xpath=ancestor::*[contains(@class,'file-toolbar')]").count()
        == 1
    )
    search.fill("資料")
    expect(page.get_by_role("button", name="資料 📁", exact=True)).to_be_visible()
    search.fill("not in the trash")
    expect(
        page.get_by_role("heading", name="No Matching Trashed Items")
    ).to_be_visible()
    assert "search_" not in page.url
    expect(page.get_by_role("heading", name="Trash", exact=True)).to_be_visible()
    search.press("Escape")
    expect(search).to_have_value("")
    expect(search).to_be_focused()

    shares = [
        {
            "id": name,
            "path": "/" + name,
            "name": name,
            "created_at": "2026-10-17T00:00:00Z",
            "expires_at": None,
            "type": "file",
            "copyable": True,
        }
        for name in ("Movie.mp4", "Notes.pdf")
    ]
    page.route("**/api/v1/shares", lambda route: route.fulfill(json={"shares": shares}))
    page.get_by_role("link", name="Shared Links", exact=True).click()
    search = page.get_by_role("searchbox", name="Search Shared Links", exact=True)
    expect(search).to_have_value("")
    search.fill("movie")
    expect(page.locator(".share-row")).to_have_count(1)
    expect(page.locator(".share-row")).to_contain_text("Movie.mp4")
    assert "view=shares" in page.url
    search.fill("missing")
    expect(page.get_by_role("heading", name="No Matching Shared Links")).to_be_visible()

    cache = page.request.get(server.url + "/api/v1/preloads").json()
    cache["cache"]["files"] = [
        {
            "id": name,
            "name": name,
            "size": 100,
            "ready_bytes": 100,
            "complete": True,
            "ready_ranges": [[0, 100]],
        }
        for name in ("Movie.mp4", "Interview.mkv")
    ]
    page.route("**/api/v1/preloads", lambda route: route.fulfill(json=cache))
    page.get_by_role("link", name="Preloads", exact=True).click()
    search = page.get_by_role("searchbox", name="Search cached files", exact=True)
    expect(search).to_have_value("")
    expect(page.get_by_role("searchbox")).to_have_count(1)
    search.fill("interview")
    expect(page.locator(".cached-file")).to_have_count(1)
    expect(page.locator(".cached-file")).to_contain_text("Interview.mkv")
    page.keyboard.press("Control+k")
    expect(search).to_be_focused()
    search.press("Escape")
    expect(page.locator(".cached-file")).to_have_count(2)
    page.get_by_role("link", name="My Files", exact=True).click()
    expect(
        page.get_by_role("searchbox", name="Search Files and Folders", exact=True)
    ).to_have_value("")


def test_transfer_search_resets_cursor_and_filters_active_and_old_finished_names(
    server, desktop
):
    _, page = desktop

    def job(identity, name, at, status="completed"):
        return dict(
            id=identity,
            name=name,
            kind="upload",
            status=status,
            phase=status,
            total=100,
            current=50 if status == "running" else 100,
            created_at=at,
            finished_at=at + 1 if status == "completed" else None,
        )

    records = [job(f"record-{i}", f"recent-{i}.bin", 1000 - i) for i in range(55)]
    records.append(job("old-result", "Archive video.mp4", 1))
    active = job("active", "Live video.mp4", 2000, "running")
    page.route("**/api/v1/jobs", lambda route: route.fulfill(json={"jobs": [active]}))
    seen = []

    def history(route):
        params = parse_qs(urlparse(route.request.url).query)
        seen.append(params)
        query = params.get("q", [""])[0].lower()
        before = float(params.get("before", ["inf"])[0])
        selected = [
            row
            for row in records
            if query in row["name"].lower() and row["created_at"] < before
        ]
        rows = selected[:50]
        more = len(selected) > 50
        route.fulfill(
            json={
                "jobs": rows,
                "has_more": more,
                "next_before": rows[-1]["created_at"] if more else None,
                "next_before_id": rows[-1]["id"] if more else None,
            }
        )

    page.route("**/api/v1/transfers/history?*", history)
    login(page, server.url)
    page.get_by_role("link", name="Transfers", exact=False).click()
    pager = page.get_by_role("navigation", name="Finished transfer pages")
    pager.get_by_role("button", name="Next", exact=True).click()
    expect(pager.get_by_role("status")).to_have_text("Page 2")
    search = page.get_by_role("searchbox", name="Search Transfers", exact=True)
    search.fill("Archive")
    expect(pager.get_by_role("status")).to_have_text("Page 1")
    finished = page.locator('[data-category="finished"]')
    expect(finished).to_contain_text("Archive video.mp4")
    expect(finished.locator(".transfer-item-name")).to_have_count(1)
    assert seen[-1]["q"] == ["Archive"] and "before" not in seen[-1]
    expect(page.locator('[data-category="running"]')).to_have_count(0)
    search.fill("Live")
    expect(page.locator('[data-category="running"]')).to_contain_text("Live video.mp4")
    expect(
        page.get_by_role("heading", name="No Matching Finished Transfers")
    ).to_be_visible()
    search.press("Escape")
    expect(pager.get_by_role("status")).to_have_text("Page 1")
    expect(finished.locator(".transfer-item-name")).to_have_count(50)


def test_touch_activation_tracks_item_identity_and_ignores_management_actions(
    server, browser
):
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    try:
        page = context.new_page()
        login(page, server.url)
        first = page.get_by_role("button", name="Preview samples", exact=True)
        second = page.get_by_role("button", name="Private folder", exact=True)
        first.tap()
        second.tap()
        # A tap on a different row selects it, even within the double-tap interval.
        expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
        expect(page.get_by_label("Select Private folder", exact=True)).to_be_checked()
        # An intervening action cannot become the first tap of a later activation.
        page.get_by_label("Actions for Private folder", exact=True).tap()
        page.keyboard.press("Escape")
        second.tap()
        expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
        second.tap()
        expect(
            page.get_by_role("heading", name="Private folder", exact=True)
        ).to_be_visible()
        expect(
            page.get_by_role("button", name="private.txt", exact=True)
        ).to_be_visible()
    finally:
        context.close()


def test_touch_file_activation_after_selection_layout_change(server, browser):
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    try:
        page = context.new_page()
        login(page, server.url)
        page.get_by_role("button", name="Preview samples", exact=True).focus()
        page.keyboard.press("Enter")
        primary = page.get_by_role("button", name="sample.json", exact=True)
        primary.tap()
        expect(page.get_by_label("Select sample.json", exact=True)).to_be_checked()
        primary.tap()
        expect(page.locator("dialog.file-preview")).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844), (320, 720)])
def test_compact_fixed_directory_controls_leave_room_for_files(
    server, desktop, width, height
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    login(page, server.url)
    folder = page.get_by_role("button", name="資料 📁", exact=True)
    folder.focus()
    folder.press("Enter")
    expect(page.get_by_role("heading", name="資料 📁", exact=True)).to_be_visible()
    expect(
        page.get_by_role("navigation", name="Breadcrumb").get_by_text(
            "資料 📁", exact=True
        )
    ).to_have_count(0)
    search = page.get_by_role("searchbox", name="Search Files and Folders", exact=True)
    expect(search).to_be_visible()
    ratio = page.locator(".file-list-scroll").evaluate(
        "node => node.clientHeight / node.closest('.workspace').clientHeight"
    )
    assert ratio > 0.60
    bounds = page.locator(".workspace").bounding_box()
    viewport = page.viewport_size
    assert abs(bounds["x"] + bounds["width"] - viewport["width"]) <= 1
    assert abs(bounds["y"] + bounds["height"] - viewport["height"]) <= 1
    assert bounds["y"] == (48 if width < 700 else 0)
    if width < 700:
        assert bounds["x"] == 0
    else:
        sidebar = page.locator(".sidebar").bounding_box()
        assert abs(bounds["x"] - sidebar["x"] - sidebar["width"]) <= 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".file-toolbar").evaluate(
        "node => node.scrollWidth <= node.clientWidth + 1"
    )
    page.keyboard.press("Control+k")
    expect(search).to_be_focused()
    if width in (1440, 390):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=ARTIFACTS / f"directory-compact-search-{width}.png")
    if width < 700:
        page.get_by_role("button", name="Toggle Navigation", exact=True).click()
        sidebar = page.locator(".sidebar")
        assert sidebar.evaluate("node => getComputedStyle(node).overflowY") == "auto"
        logout = page.get_by_role("button", name="Log Out", exact=True)
        logout.scroll_into_view_if_needed()
        expect(logout).to_be_in_viewport()
        documentation = page.get_by_role("link", name="Documentation", exact=True)
        documentation.scroll_into_view_if_needed()
        expect(documentation).to_be_in_viewport()
        page.get_by_role("link", name="Storage & Backups", exact=True).focus()
        expect(
            page.get_by_role("link", name="Storage & Backups", exact=True)
        ).to_be_in_viewport()
