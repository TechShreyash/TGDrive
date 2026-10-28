"""Sorting, grid browsing and lazy provider thumbnails on the production bundle."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, expect, login

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
    # Ensure the fixture can scroll fully outside the thumbnail observer margin.
    columns = grid.evaluate(
        """element => {
            const previous = element.style.gridTemplateColumns;
            element.style.gridTemplateColumns = 'repeat(2, minmax(0, 1fr))';
            return previous;
        }"""
    )
    sample = grid.locator(".file-row").filter(has_text="sample.png")
    sample.scroll_into_view_if_needed()
    preview = sample.locator(".file-tile-preview")
    image = preview.locator("img")
    expect(image).to_be_visible()
    assert thumbnails and not originals
    source = image.get_attribute("src")
    item_id = sample.get_attribute("data-item-id")
    assert source and item_id
    request = next(
        url
        for url in thumbnails
        if parse_qs(urlparse(url).query)["path"][0].endswith("/" + item_id)
    )
    request_count = thumbnails.count(request)
    observer = preview.evaluate_handle(
        """element => {
            const observer = new IntersectionObserver(([entry]) => {
                element.dataset.inView = String(entry.isIntersecting);
            }, {rootMargin: '120px'});
            observer.observe(element);
            return observer;
        }"""
    )
    grid.locator(".file-row").first.scroll_into_view_if_needed()
    expect(preview).to_have_attribute("data-in-view", "false")
    expect(image).to_have_attribute("src", source)
    sample.scroll_into_view_if_needed()
    expect(preview).to_have_attribute("data-in-view", "true")
    preview.evaluate(
        """() => new Promise(resolve =>
            requestAnimationFrame(() => requestAnimationFrame(resolve))
        )"""
    )
    expect(sample.locator(".thumbnail-loading")).to_have_count(0)
    expect(image).to_have_attribute("src", source)
    assert thumbnails.count(request) == request_count
    observer.evaluate("observer => observer.disconnect()")
    grid.evaluate(
        "(element, value) => element.style.gridTemplateColumns = value", columns
    )
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


@pytest.mark.parametrize(
    "width,theme", [(1440, "light"), (390, "dark"), (320, "light")]
)
def test_item_status_is_quiet_metadata_without_interrupting_names(
    server, desktop, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(color_scheme=theme)
    login(page, server.url)
    contents = page.request.post(
        server.url + "/api/getDirectory", data={"path": "/"}
    ).json()["data"]["contents"]
    target = next(
        item for item in contents.values() if item["name"] == "Preview samples"
    )
    path = "/" + target["id"]
    assert page.request.patch(
        server.url + "/api/v1/starred", data={"path": path, "starred": True}
    ).ok
    assert page.request.post(
        server.url + "/api/v1/shares", data={"path": path, "expires_in_days": 7}
    ).ok
    page.reload()
    row = page.locator(".file-row").filter(
        has=page.get_by_role("button", name="Preview samples", exact=True)
    )
    status = row.locator(".file-item-status")
    expect(status).to_have_text("StarredShared")
    expect(status.locator("svg")).to_have_count(0)
    expect(status.get_by_label("Shared by Link", exact=True)).to_have_text("Shared")
    name = row.locator(".file-name").bounding_box()
    state = status.bounding_box()
    assert name and state
    if width > 720:
        assert state["x"] >= name["x"] + name["width"]
    else:
        assert state["y"] >= name["y"] + name["height"] - 1
    height = row.bounding_box()["height"]
    sibling = page.locator(".file-row").filter(
        has=page.get_by_role("button", name="Private folder", exact=True)
    )
    assert abs(height - sibling.bounding_box()["height"]) <= 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=ARTIFACTS / f"item-status-list-{theme}-{width}.png",
        animations="disabled",
    )
    choose_view(page, "Grid view")
    expect(status).to_be_visible()
    assert status.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=ARTIFACTS / f"item-status-grid-{theme}-{width}.png",
        animations="disabled",
    )


@pytest.mark.parametrize("width,view", [(1440, "Grid view"), (390, "List view")])
def test_escape_selection_returns_focus_to_file_control_not_workspace(
    server, desktop, width, view
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)
    choose_view(page, view)
    page.get_by_role("button", name="Select", exact=True).click()
    checkbox = page.get_by_role("checkbox", name="Select Preview samples", exact=True)
    checkbox.check()
    expect(page.get_by_text("1 selected", exact=True)).to_be_visible()
    checkbox.focus()
    page.keyboard.press("Escape")
    item = page.get_by_role("button", name="Preview samples", exact=True)
    expect(item).to_be_focused()
    expect(page.locator(".file-row.selected")).to_have_count(0)
    expect(page.locator(".file-list-scroll")).not_to_be_focused()
    assert not page.locator(".file-list-scroll").evaluate(
        "e => e.matches(':focus-visible')"
    )
    assert item.evaluate("e => e.matches(':focus-visible')")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=ARTIFACTS / f"selection-escape-focus-{width}.png", animations="disabled"
    )
    item.press("Enter")
    expect(
        page.get_by_role("heading", name="Preview samples", exact=True)
    ).to_be_visible()
