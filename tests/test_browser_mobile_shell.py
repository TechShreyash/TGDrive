"""Phone-specific navigation and upload state cannot cover file content."""

from __future__ import annotations

import os

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)
import test_browser_e2e as fixtures
from test_browser_e2e import ARTIFACTS, expect, login, upload

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(
    "width,height,theme", [(320, 640, "light"), (390, 844, "dark"), (600, 800, "light")]
)
def test_phone_browsing_drawer_and_upload_use_separate_surfaces(
    server, desktop, width, height, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    page.add_init_script("localStorage.setItem('tgdrive.appearance','" + theme + "')")

    def listing(route):
        response = route.fetch()
        payload = response.json()
        if route.request.post_data_json["path"] == "/":
            payload["data"]["contents"].update(
                {
                    f"phone-{i}": {
                        "id": f"phone-{i}",
                        "name": f"Project notes {i:03}.txt",
                        "type": "file",
                        "path": "/",
                        "size": 1024,
                    }
                    for i in range(60)
                }
            )
        route.fulfill(response=response, json=payload)

    page.route("**/api/getDirectory", listing)
    login(page, server.url)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    expect(page.locator("#compact-new-button")).to_be_visible()
    assert page.locator("#compact-new-button").evaluate(
        "e => getComputedStyle(e).position !== 'fixed'"
    )
    page.screenshot(path=ARTIFACTS / f"phone-files-idle-{width}.png")
    upload(page, ["Project walkthrough.mp4"])
    server.wait_started(1)
    status = page.get_by_role("button", name="Open transfer progress", exact=True)
    expect(status).to_be_visible()
    expect(page.locator("#transfer-dialog")).to_have_count(0)
    assert (
        page.locator(".workspace").bounding_box()["y"]
        + page.locator(".workspace").bounding_box()["height"]
        <= status.bounding_box()["y"] + 1
    )
    assert page.locator(".file-list-scroll").evaluate(
        "e => getComputedStyle(e).overflowY === 'visible'"
    )
    assert page.locator(".file-toolbar").evaluate(
        "e => e.scrollWidth <= e.clientWidth+1"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / f"phone-files-upload-{width}.png")
    page.locator(".workspace").evaluate("e => e.scrollTop=500")
    assert page.locator(".workspace").evaluate("e=>e.scrollTop") == 500
    expect(
        page.get_by_role("button", name="Toggle Navigation", exact=True)
    ).to_be_in_viewport()
    page.get_by_role("button", name="Toggle Navigation", exact=True).click()
    drawer = page.get_by_role("dialog", name="Drive navigation", exact=True)
    expect(drawer).to_be_visible()
    rect = drawer.bounding_box()
    assert rect["y"] == 0 and rect["height"] == height
    assert rect["width"] <= width - 39
    assert drawer.evaluate("e=>e.scrollTop") == 0
    assert page.locator("main").get_attribute("inert") is not None
    page.screenshot(path=ARTIFACTS / f"phone-drawer-{width}.png")
    page.keyboard.press("Escape")
    expect(drawer).to_have_count(0)
    expect(
        page.get_by_role("button", name="Toggle Navigation", exact=True)
    ).to_be_focused()
    status.click()
    expect(page.get_by_role("heading", name="Transfers", exact=True)).to_be_visible()
    expect(page.locator(".transfer-page")).to_be_visible()
    expect(status).to_have_count(0)
    page.screenshot(path=ARTIFACTS / f"phone-transfers-{width}.png")


@pytest.mark.parametrize(
    "width,theme", [(1440, "light"), (390, "dark"), (320, "light")]
)
def test_sidebar_keeps_rare_tools_collapsed_and_exposes_project_link(
    server, desktop, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(color_scheme=theme)
    login(page, server.url)

    def open_drawer():
        if page.locator(".sidebar").get_attribute("inert") is not None:
            page.get_by_role("button", name="Toggle Navigation", exact=True).click()

    open_drawer()
    tools = page.locator(".sidebar-tools")
    summary = tools.locator("summary")
    expect(tools).not_to_have_attribute("open", "")
    expect(page.get_by_role("link", name="Performance", exact=True)).not_to_be_visible()
    for label in ["My Files", "Starred", "Transfers", "Shared Links", "Trash"]:
        expect(page.get_by_role("link", name=label, exact=True)).to_be_in_viewport()
    github = page.get_by_role("link", name="Star on GitHub", exact=True)
    expect(github).to_have_attribute("href", "https://github.com/TechShreyash/TGDrive")
    expect(github).to_have_attribute("target", "_blank")
    expect(github).to_have_attribute("rel", "noopener noreferrer")
    expect(github).to_be_in_viewport()
    expect(page.get_by_role("button", name="Log Out", exact=True)).to_be_in_viewport()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=ARTIFACTS / f"sidebar-simple-{theme}-{width}.png", animations="disabled"
    )
    summary.focus()
    page.keyboard.press("Enter")
    expect(tools).to_have_attribute("open", "")
    page.get_by_role("link", name="Performance", exact=True).click()
    expect(page.get_by_role("heading", name="Performance", exact=True)).to_be_visible()
    open_drawer()
    expect(tools).to_have_attribute("open", "")
    expect(page.locator('.drive-nav a[aria-current="page"]')).to_have_text(
        "Performance"
    )
    page.screenshot(
        path=ARTIFACTS / f"sidebar-tools-{theme}-{width}.png", animations="disabled"
    )
    page.get_by_role("link", name="My Files", exact=True).click()
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    open_drawer()
    expect(tools).not_to_have_attribute("open", "")
    page.goto(server.url + "/?view=settings&section=regional")
    expect(
        page.get_by_role("heading", name="Regional settings", exact=True)
    ).to_be_visible()
    open_drawer()
    expect(tools).to_have_attribute("open", "")
    expect(page.get_by_role("link", name="Timezone", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    summary.click()
    expect(tools).not_to_have_attribute("open", "")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
