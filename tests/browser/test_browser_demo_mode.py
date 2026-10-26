"""Public demo controls remain inspectable in the production interface."""

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


@pytest.fixture
def demo_server(monkeypatch, request):
    monkeypatch.setenv("DEMO_MODE", "true")
    return request.getfixturevalue("server")


@pytest.mark.parametrize("config_failure", [False, True])
def test_management_waits_for_deployment_policy(demo_server, desktop, config_failure):
    _, page = desktop
    config = page.request.get(demo_server.url + "/api/config").json()
    pending = []

    def deployment_config(route):
        if config_failure:
            route.abort("failed")
        else:
            pending.append(route)

    page.route("**/api/config", deployment_config)
    login(page, demo_server.url + "/?view=settings&section=cache")
    state = page.locator(".deployment-config-state")
    expect(state).to_be_visible()
    expect(page.get_by_label("Memory cache (MiB)", exact=True)).to_have_count(0)
    expect(page.locator(".demo-mode-notice")).to_have_count(0)

    if config_failure:
        expect(state).to_have_attribute("role", "alert")
        page.unroute("**/api/config", deployment_config)
        state.get_by_role("button", name="Try Again", exact=True).click()
    else:
        # Login refreshes session-specific permissions. An earlier response
        # must never apply owner capability to the newly authenticated visitor.
        deadline = time.monotonic() + 5
        while len(pending) < 2 and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert len(pending) >= 2, "Expected a fresh configuration after login"
        pending[0].fulfill(json={**config, "demo_read_only": False, "demo_admin": True})
        # Let the fetch body and React state updates settle before asserting
        # that the stale response leaves the management gate in place.
        page.evaluate("() => new Promise(requestAnimationFrame)")
        expect(state).to_be_visible()
        expect(page.get_by_label("Memory cache (MiB)", exact=True)).to_have_count(0)
        for route in pending[1:]:
            route.fulfill(json=config)

    expect(state).to_have_count(0)
    expect(page.get_by_label("Memory cache (MiB)", exact=True)).to_be_disabled()
    expect(page.locator(".demo-mode-notice")).to_be_visible()


@pytest.mark.parametrize("width", [1440, 390])
def test_demo_pages_lock_changes_keep_navigation_and_file_features(
    demo_server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 960})
    management_writes = []
    page.on(
        "request",
        lambda request: (
            management_writes.append(request.url)
            if request.method not in {"GET", "HEAD", "OPTIONS"}
            and re.search(r"/api/v1/(settings|replica|backup)(/|$)", request.url)
            else None
        ),
    )
    login(page, demo_server.url + "/?view=settings&section=cache")
    assert page.request.get(demo_server.url + "/api/config").json()["demo_mode"]

    def navigate(label):
        fixtures.open_admin(page)
        page.get_by_role("link", name=label, exact=True).click()
        expect(page.locator(".demo-mode-notice")).to_contain_text("read-only")

    expect(page.locator(".demo-mode-notice")).to_be_visible()
    expect(page.get_by_label("Memory cache (MiB)", exact=True)).to_be_disabled()
    expect(page.get_by_label("Memory cache (MiB)", exact=True)).not_to_have_value("")
    expect(
        page.get_by_role("switch", name=re.compile(r"^Preload during playback"))
    ).to_be_disabled()
    expect(
        page.get_by_role("button", name="Save Performance Settings", exact=True)
    ).to_be_disabled()
    expect(
        page.get_by_role("button", name="Restore defaults", exact=True)
    ).to_be_disabled()

    navigate("Transfer limits")
    expect(page.get_by_label("More Uploads per Bot", exact=True)).to_be_disabled()
    expect(page.get_by_label("Maximum file size (GiB)", exact=True)).to_be_disabled()
    expect(
        page.get_by_role("button", name="Change folder", exact=True)
    ).to_be_disabled()
    page.get_by_text("Advanced Telegram limits", exact=True).click()
    expect(page.get_by_label("Requests per account", exact=True)).to_be_visible()
    expect(page.get_by_label("Requests per account", exact=True)).to_be_disabled()

    navigate("Timezone")
    expect(page.get_by_label("Timezone", exact=True)).to_be_disabled()

    navigate("Connections")
    expect(
        page.get_by_role("heading", name="Storage Clients", exact=True)
    ).to_be_visible()
    for name in [
        "Refresh",
        "Add Storage Bot",
        "Connect Premium Account",
        "Edit API Credentials",
    ]:
        expect(page.get_by_role("button", name=name, exact=True)).to_be_disabled()
    for control in page.get_by_role("button", name=re.compile(r"^Manage ")).all():
        expect(control).to_be_disabled()

    navigate("Storage & Backups")
    expect(page.get_by_role("button", name="Back Up Now", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Check Again", exact=True)).to_be_enabled()
    expect(
        page.get_by_role("button", name="Refresh channel backup status", exact=True)
    ).to_be_enabled()
    expect(
        page.get_by_role("button", name="Set up backup channel", exact=True)
    ).to_be_disabled()
    page.get_by_text("Recover a channel or catalog", exact=True).click()
    expect(page.get_by_label("Surviving channel ID", exact=True)).to_be_visible()
    expect(page.get_by_label("Surviving channel ID", exact=True)).to_be_disabled()
    expect(page.locator('a[href="/api/v1/replica/key"]')).to_have_count(0)
    expect(page.locator('a[href="/api/v1/backup/recovery-key"]')).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / f"demo-management-{width}.png", full_page=True)
    assert management_writes == []

    # Regular visitor features still use real UI actions and the normal API.
    fixtures.open_admin(page)
    page.get_by_role("link", name="My Files", exact=True).click()
    expect(page.locator(".demo-mode-notice")).to_have_count(0)
    folder = page.request.post(
        demo_server.url + "/api/createNewFolder",
        data={"path": "/", "name": "Public demo folder"},
    )
    assert folder.ok
    fixtures.upload(page, ["public-demo-upload.bin"])
    assert demo_server.wait_started(1)[0]["name"] == "public-demo-upload.bin"
