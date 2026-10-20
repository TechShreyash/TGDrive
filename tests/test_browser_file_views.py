"""Sorting, grid browsing and lazy provider thumbnails on the production bundle."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import test_browser_e2e as fixtures
from test_browser_e2e import ARTIFACTS, expect, login

browser = fixtures.browser
desktop = fixtures.desktop
server = fixtures.server
pytestmark = pytest.mark.e2e


def choose_view(page, name):
    compact = page.get_by_role("button", name="View and sort", exact=True)
    if compact.count():
        compact.click()
        page.get_by_role("menuitemradio", name=name, exact=True).click()
    else:
        page.get_by_role("button", name=name, exact=True).click()


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_grid_views_preserve_selection_actions_and_bounded_thumbnails(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    thumbnails = []
    originals = []
    page.on(
        "request",
        lambda request: (
            thumbnails.append(request.url)
            if "/api/v1/thumbnail?" in request.url
            else originals.append(request.url)
            if urlparse(request.url).path == "/file"
            else None
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    expect(page.get_by_role("button", name="sample.png", exact=True)).to_be_visible()
    expect(page.locator(".file-table thead")).to_be_visible()
    assert thumbnails == []
    choose_view(page, "Grid view")
    grid = page.get_by_role("list", name="Files and folders", exact=True)
    expect(grid).to_be_visible()
    if width > 720:
        expect(page.get_by_role("button", name="Grid view")).to_have_attribute(
            "aria-pressed", "true"
        )
    page.locator(".file-grid .file-row").filter(
        has_text="sample.png"
    ).scroll_into_view_if_needed()
    expect(
        page.locator(".file-grid .file-row")
        .filter(has_text="sample.png")
        .locator(".file-tile-preview img")
    ).to_be_visible()
    assert thumbnails and not originals
    page.get_by_role("button", name="Select", exact=True).click()
    page.get_by_role("checkbox", name="Select sample.png", exact=True).check()
    expect(page.get_by_text("1 selected", exact=True)).to_be_visible()
    page.get_by_role(
        "checkbox", name="Select All Files on This Page", exact=True
    ).check()
    expect(page.get_by_text("16 selected", exact=True)).to_be_visible()
    page.get_by_role("button", name="Done", exact=True).click()
    page.locator('summary[aria-label="Actions for sample.png"]').click()
    expect(page.get_by_role("button", name="Rename", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    page.reload()
    expect(page.get_by_role("list", name="Files and folders")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=ARTIFACTS / f"grid-files-{width}.png")
    choose_view(page, "List view")
    expect(page.locator(".file-table thead")).to_be_visible()


def test_sort_direction_and_trash_original_location_work_in_both_views(server, desktop):
    _, page = desktop
    login(page, server.url)
    contents = page.request.post(
        server.url + "/api/getDirectory", data={"path": "/"}
    ).json()["data"]["contents"]
    source = next(x for x in contents.values() if x["name"] == "Preview samples")
    response = page.request.post(
        server.url + "/api/createNewFolder",
        data={"path": "/" + source["id"], "name": "Nested trash"},
    )
    assert response.ok
    nested = page.request.post(
        server.url + "/api/getDirectory", data={"path": "/" + source["id"]}
    ).json()["data"]["contents"]
    target = next(x for x in nested.values() if x["name"] == "Nested trash")
    assert page.request.post(
        server.url + "/api/trashFileFolder",
        data={"path": "/" + source["id"] + "/" + target["id"], "trash": True},
    ).ok
    page.get_by_role("link", name="Trash", exact=True).click()
    expect(
        page.get_by_text("Deleted from: My Files / Preview samples", exact=True)
    ).to_be_visible()
    choose_view(page, "Grid view")
    expect(
        page.get_by_text("Deleted from: My Files / Preview samples", exact=True)
    ).to_be_visible()
    page.get_by_role("link", name="My Files", exact=True).click()
    choose_view(page, "List view")
    page.get_by_role("button", name="Sort Files: Name", exact=True).click()
    page.get_by_role("menuitemradio", name="Descending", exact=True).click()
    names = page.locator(".file-table .item-name-button").all_text_contents()
    assert names == sorted(names, key=str.casefold, reverse=True)
    page.get_by_role("button", name="Sort by Name", exact=True).click()
    expect(page.locator(".file-table th").first).to_have_attribute(
        "aria-sort", "ascending"
    )
    search = page.get_by_role("searchbox", name="Search Files and Folders", exact=True)
    search.fill("sample")
    expect(
        page.get_by_role("heading", name="Search Results", exact=True)
    ).to_be_visible()
    page.get_by_role("button", name="Sort Files: Name", exact=True).click()
    with page.expect_response(
        lambda response: (
            "/api/v1/search?" in response.url
            and parse_qs(urlparse(response.url).query).get("order") == ["desc"]
        )
    ):
        page.get_by_role("menuitemradio", name="Descending", exact=True).click()
    choose_view(page, "Grid view")
    expect(page.get_by_role("list", name="Files and folders")).to_be_visible()


@pytest.mark.parametrize("view", ["overview", "telegram", "server"])
def test_performance_content_separates_from_toolbar(server, desktop, view):
    _, page = desktop
    login(page, server.url + "/?view=performance")
    label = {"overview": "Overview", "telegram": "Accounts", "server": "Resources"}[
        view
    ]
    page.get_by_role("tab", name=label, exact=True).click()
    panel = page.locator("#performance-panel-" + view)
    expect(panel).to_be_visible()
    gap = page.evaluate("""() => {
      const toolbar=document.querySelector('.performance-toolbar')
        .getBoundingClientRect();
      const next=document.querySelector(
        '.performance-history-availability:not([hidden])') ||
        document.querySelector('.performance-accounts-grid:not([hidden])');
      return next.getBoundingClientRect().top-toolbar.bottom;
    }""")
    assert gap >= 15


@pytest.mark.parametrize("width", [390, 320])
def test_mobile_header_stays_compact_and_search_and_view_controls_work(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)
    expect(page.get_by_role("button", name="View and sort", exact=True)).to_be_visible()
    assert page.locator(".file-toolbar").bounding_box()["height"] <= 70
    search = page.get_by_role("searchbox", name="Search Files and Folders", exact=True)
    expect(search).not_to_be_visible()
    page.get_by_role("button", name="Search Files and Folders", exact=True).click()
    expect(search).to_be_focused()
    search.press("Escape")
    expect(search).not_to_be_visible()
    expect(
        page.get_by_role("button", name="Search Files and Folders", exact=True)
    ).to_be_focused()
    page.keyboard.press("Control+k")
    expect(search).to_be_focused()
    search.press("Escape")
    choose_view(page, "Grid view")
    expect(page.get_by_role("list", name="Files and folders")).to_be_visible()
    page.get_by_role("button", name="View and sort", exact=True).click()
    menu = page.get_by_role("menu", name="View and sort", exact=True)
    expect(
        menu.get_by_role("menuitemradio", name="Grid view", exact=True)
    ).to_have_attribute("aria-checked", "true")
    bounds = menu.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 844
    page.keyboard.press("End")
    expect(
        menu.get_by_role("menuitemradio", name="Descending", exact=True)
    ).to_be_focused()
    page.keyboard.press("Escape")
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")


def test_grid_respects_data_saver(server, desktop):
    _, page = desktop
    page.add_init_script(
        "Object.defineProperty(navigator, 'connection', "
        "{value:{saveData:true},configurable:true})"
    )
    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.url)
            if "/api/v1/thumbnail?" in request.url
            else None
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    choose_view(page, "Grid view")
    expect(page.get_by_role("list", name="Files and folders")).to_be_visible()
    expect(page.locator(".file-tile-preview img")).to_have_count(0)
    page.get_by_role("button", name="sample.png", exact=True).focus()
    assert not requests
