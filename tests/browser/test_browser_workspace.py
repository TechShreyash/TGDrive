"""Scroll ownership and settings navigation in the production interface."""

from __future__ import annotations

import os
import re
import time

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 to run Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, expect, login

browser = fixtures.browser
desktop = fixtures.desktop
server = fixtures.server

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width", [1440, 390])
def test_directory_controls_stay_visible_while_only_files_scroll(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})

    def listing(route):
        response = route.fetch()
        payload = response.json()
        payload["data"]["contents"].update(
            {
                f"scroll-{i}": {
                    "id": f"scroll-{i}",
                    "name": f"scroll-{i:03}.txt",
                    "type": "file",
                    "path": "/",
                    "size": 10,
                }
                for i in range(80)
            }
        )
        route.fulfill(response=response, json=payload)

    page.route("**/api/getDirectory", listing)
    login(page, server.url)
    page.get_by_role("button", name="Select", exact=True).click()
    page.get_by_label("Select scroll-000.txt", exact=True).check()
    # Compact selection actions replace the visible heading in the same slot.
    heading = page.locator(".file-toolbar h1")
    toolbar = page.locator(".file-toolbar")
    before = [heading.bounding_box()["y"], toolbar.bounding_box()["y"]]
    scroll = (
        page.locator(".workspace")
        if width < 721
        else page.get_by_label("File list", exact=True)
    )
    scroll.evaluate("element => element.scrollTop = 700")
    assert scroll.evaluate("element => element.scrollTop") == 700
    if width > 720:
        expect(page.get_by_role("button", name="Move", exact=True)).to_be_in_viewport()
        expect(page.get_by_role("button", name="Trash", exact=True)).to_be_in_viewport()
        assert page.locator(".workspace").evaluate("element => element.scrollTop") == 0
        assert [heading.bounding_box()["y"], toolbar.bounding_box()["y"]] == before
        table_header = page.locator(".file-table thead th").first.bounding_box()
        assert abs(table_header["y"] - scroll.bounding_box()["y"]) <= 1
    else:
        assert heading.bounding_box()["y"] < 0
        assert (
            page.locator(".file-list-scroll").evaluate("element => element.scrollTop")
            == 0
        )
    scroll.focus()
    page.keyboard.press("PageDown")
    page.wait_for_function(
        "selector => document.querySelector(selector).scrollTop > 700",
        arg=".workspace" if width < 721 else ".file-list-scroll",
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / f"directory-fixed-controls-{width}.png")


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_admin_navigation_separates_categories_preserves_drafts_and_supports_keyboard(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)

    def open_drawer():
        fixtures.open_admin(page)

    def navigate(label):
        open_drawer()
        page.get_by_role("link", name=label, exact=True).click()

    open_drawer()
    expect(page.get_by_role("button", name="Admin panel", exact=True)).to_have_count(0)
    monitor = page.get_by_role("group", name="Monitor", exact=True)
    expect(monitor.get_by_role("link")).to_have_count(2)
    expect(monitor.get_by_role("link", name="Performance", exact=True)).to_be_visible()
    expect(monitor.get_by_role("link", name="Preloads", exact=True)).to_be_visible()
    config = page.get_by_role("group", name="Configuration", exact=True)
    expect(config.get_by_role("link")).to_have_count(3)
    manage = page.get_by_role("group", name="Management", exact=True)
    expect(manage.get_by_role("link")).to_have_count(2)
    page.get_by_role("link", name="Preloads", exact=True).focus()
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Cache settings", exact=True)).to_be_focused()
    for label in ["Storage & Backups", "Cache settings", "Transfer limits", "Timezone"]:
        link = page.get_by_role("link", name=label, exact=True)
        assert link.evaluate("node => node.scrollWidth <= node.clientWidth")
        assert (
            link.locator("span").evaluate("node => getComputedStyle(node).whiteSpace")
            == "nowrap"
        )
    navigate("Cache settings")
    expect(
        page.get_by_role("heading", name="Cache & preloading", exact=True)
    ).to_be_visible()
    memory = page.get_by_label("Memory cache (MiB)", exact=True)
    memory.fill("512")
    expect(page.get_by_label("Timezone", exact=True)).not_to_be_visible()
    navigate("Timezone")
    timezone = page.get_by_label("Timezone", exact=True)
    expect(timezone).to_be_visible()
    timezone.fill("Europe/London")
    expect(memory).not_to_be_visible()
    expect(page.get_by_role("link", name="Settings", exact=True)).to_have_count(0)
    navigate("Transfer limits")
    expect(
        page.get_by_label("Simultaneous Uploads per Bot", exact=True)
    ).to_be_visible()
    expect(timezone).not_to_be_visible()
    page.go_back()
    expect(timezone).to_have_value("Europe/London")
    navigate("Cache settings")
    expect(memory).to_have_value("512")
    navigate("Transfer limits")
    expect(
        page.get_by_label("Simultaneous Uploads per Bot", exact=True)
    ).to_be_visible()
    expect(memory).not_to_be_visible()
    expect(timezone).not_to_be_visible()
    page.screenshot(path=ARTIFACTS / f"admin-transfers-category-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_cache_settings_deep_link_opens_cache_category(server, desktop):
    _, page = desktop
    login(page, server.url + "/?view=settings&section=cache")
    expect(
        page.get_by_role("heading", name="Cache & preloading", exact=True)
    ).to_be_visible()
    expect(
        page.get_by_role("link", name="Cache settings", exact=True)
    ).to_have_attribute("aria-current", "page")


@pytest.mark.parametrize("refresh_failure", [False, True])
def test_save_only_current_category_preserves_unrelated_invalid_draft(
    server, desktop, refresh_failure
):
    _, page = desktop
    patches = []

    def policy(route):
        if route.request.method == "PATCH":
            patches.append(route.request.post_data_json)
        route.continue_()

    page.route("**/api/v1/settings/performance", policy)
    login(page, server.url + "/?view=settings&section=cache")
    page.get_by_label("Memory cache (MiB)", exact=True).fill("")
    page.get_by_role("link", name="Timezone", exact=True).click()
    page.get_by_label("Timezone", exact=True).fill("UTC")
    if refresh_failure:
        page.route(
            "**/api/config",
            lambda route: route.fulfill(
                status=503, json={"detail": "Temporary configuration outage"}
            ),
        )
    page.get_by_role("button", name="Save Performance Settings", exact=True).click()
    expect(page.locator(".toast")).to_have_text("Performance settings saved.")
    assert patches == [{"timezone": "UTC"}]
    if refresh_failure:
        state = page.locator(".deployment-config-state")
        expect(state).to_have_attribute("role", "alert")
        expect(page.get_by_label("Timezone", exact=True)).to_be_disabled()
        expect(page.locator(".workspace > fieldset")).to_have_attribute("inert", "")
        page.screenshot(path=ARTIFACTS / "config-refresh-preserved-draft.png")
        page.unroute("**/api/config")
        state.get_by_role("button", name="Try Again", exact=True).click()
        expect(state).to_have_count(0)
        expect(page.get_by_label("Timezone", exact=True)).to_have_value("UTC")
    page.get_by_role("link", name="Cache settings", exact=True).click()
    expect(page.get_by_label("Memory cache (MiB)", exact=True)).to_have_value("")
    page.get_by_role("button", name="Save Performance Settings", exact=True).click()
    expect(page.locator(".performance-settings").get_by_role("alert")).to_contain_text(
        "Memory cache (MiB) must be"
    )
    assert len(patches) == 1


@pytest.mark.parametrize("query", ["?view=settings", "?view=settings&section=general"])
def test_missing_or_unknown_settings_section_uses_cache_without_url_rewrite(
    server, desktop, query
):
    _, page = desktop
    login(page, server.url + "/" + query)
    expect(
        page.get_by_role("heading", name="Cache & preloading", exact=True)
    ).to_be_visible()
    assert page.url == server.url + "/" + query
    expect(page.get_by_role("link", name="Settings", exact=True)).to_have_count(0)
    expect(
        page.get_by_role("button", name="Change Appearance", exact=True)
    ).to_be_visible()


@pytest.mark.parametrize("width", [1440, 390])
def test_sidebar_logout_busy_error_retry_and_server_transfer_survives(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)
    fixtures.upload(page, ["logout-keeps-server-transfer.bin"])
    started = server.wait_started(1)
    if page.locator(".sidebar").get_attribute("inert") is not None:
        page.get_by_role("button", name="Toggle Navigation", exact=True).click()
    logout = page.get_by_role("button", name="Log Out", exact=True)
    logout.scroll_into_view_if_needed()
    sidebar = page.locator(".sidebar")
    expect(logout).to_be_in_viewport()
    assert sidebar.evaluate("e => getComputedStyle(e).overflowY") == "auto"
    assert page.locator(".drive-nav").evaluate("e => e.scrollTop") == 0
    assert (
        page.locator(".sidebar-bottom").evaluate("e => getComputedStyle(e).position")
        == "static"
    )
    page.screenshot(path=ARTIFACTS / f"sidebar-logout-{width}.png")
    held = []
    page.route("**/api/logout", lambda route: held.append(route))
    logout.focus()
    page.keyboard.press("Enter")
    busy = page.get_by_role("button", name="Logging out…", exact=True)
    expect(busy).to_be_disabled()
    expect(busy).to_have_attribute("aria-busy", "true")
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(held) == 1
    held.pop().fulfill(status=503, json={"message": "Please retry logout"})
    expect(page.locator(".sidebar").get_by_role("alert")).to_be_visible()
    expect(logout).to_be_focused()
    page.unroute("**/api/logout")
    logout.click()
    expect(page.locator("#login-dialog")).to_be_visible()
    expect(page.get_by_label("Password", exact=True)).to_be_focused()
    expect(page.get_by_role("button", name="Log Out", exact=True)).to_have_count(0)
    assert server.control()["started"] == started
    server.finish(started[0]["id"])
    login(page)
    if page.locator(".sidebar").get_attribute("inert") is not None:
        page.get_by_role("button", name="Toggle Navigation", exact=True).click()
    page.get_by_role("link", name="Transfers", exact=True).click()
    expect(
        page.get_by_text("logout-keeps-server-transfer.bin", exact=True)
    ).to_be_visible()


@pytest.mark.parametrize("width", [1440, 390])
def test_restore_defaults_is_reviewable_scoped_and_durable(server, desktop, width):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url + "/?view=settings&section=cache")
    initial = page.request.get(server.url + "/api/v1/settings/performance").json()
    assert page.request.patch(
        server.url + "/api/v1/settings/performance",
        data={"cache_memory_bytes": 512 * 1024**2, "timezone": "UTC"},
        headers={"If-Match": initial["revision"]},
    ).ok
    page.reload()
    memory = page.get_by_label("Memory cache (MiB)", exact=True)
    expect(memory).to_have_value("512")
    page.get_by_role("button", name="Restore defaults", exact=True).click()
    expect(memory).to_have_value("256")
    assert (
        page.request.get(server.url + "/api/v1/settings/performance").json()[
            "cache_memory_bytes"
        ]
        == 512 * 1024**2
    )
    expect(
        page.get_by_role("link", name="Configuration guide", exact=True)
    ).to_have_attribute("href", re.compile(r"/docs/configuration\.md$"))
    page.get_by_role("button", name="Discard changes", exact=True).click()
    expect(memory).to_have_value("512")
    page.get_by_role("button", name="Restore defaults", exact=True).click()
    page.get_by_role("button", name="Save Performance Settings", exact=True).click()
    expect(page.locator(".toast")).to_have_text("Performance settings saved.")
    policy = page.request.get(server.url + "/api/v1/settings/performance").json()
    assert policy["cache_memory_bytes"] == 256 * 1024**2
    assert policy["timezone"] == "UTC"
    page.get_by_label("Cache free-space floor (GiB)", exact=True).fill("0")
    page.get_by_role("button", name="Save Performance Settings", exact=True).click()
    expect(page.locator(".performance-settings").get_by_role("alert")).to_contain_text(
        "at least 64 MiB"
    )
    assert (
        page.request.get(server.url + "/api/v1/settings/performance").json()["revision"]
        == policy["revision"]
    )
    page.get_by_role("button", name="Restore defaults", exact=True).click()
    page.screenshot(path=ARTIFACTS / f"configuration-defaults-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [1440, 390])
def test_owner_upload_storage_limits_validate_save_and_refresh_sidebar(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url + "/?view=settings&section=transfers")
    maximum = page.get_by_label("Maximum file size (GiB)", exact=True)
    budget = page.get_by_label("Temporary upload budget (GiB)", exact=True)
    reserve = page.get_by_label("Upload free-space reserve (GiB)", exact=True)
    maximum.fill("32")
    page.get_by_role("button", name="Save Performance Settings", exact=True).click()
    expect(page.locator(".performance-settings").get_by_role("alert")).to_contain_text(
        "cannot exceed"
    )
    expect(maximum).to_have_value("32")
    budget.fill("64")
    reserve.fill("1")
    page.get_by_role("button", name="Save Performance Settings", exact=True).click()
    expect(page.locator(".toast")).to_have_text("Performance settings saved.")
    assert (
        page.request.get(server.url + "/api/config").json()["max_file_size"]
        == 32 * 1024**3
    )
    page.reload()
    expect(maximum).to_have_value("32")
    expect(budget).to_have_value("64")
    page.screenshot(path=ARTIFACTS / f"owner-upload-storage-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
