from __future__ import annotations

import os

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, CONTROL, expect, login

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_starred_page_add_find_open_and_remove(server, desktop, width):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)
    assert page.locator(".brand-logo img").first.evaluate(
        "img => img.complete && img.naturalWidth > 0"
    )
    page.locator('summary[aria-label="Actions for 資料"]').click()
    page.get_by_role("button", name="Add to Starred", exact=True).click()
    expect(
        page.locator(".file-status-label").filter(has_text="Starred")
    ).to_be_visible()
    if width < 721:
        page.get_by_role("button", name="Toggle Navigation", exact=True).click()
    page.get_by_role("link", name="Starred", exact=True).click()
    expect(page.get_by_role("heading", name="Starred", exact=True)).to_be_visible()
    expect(page.locator('.drive-nav a[aria-current="page"]')).to_have_text("Starred")
    expect(page.get_by_role("button", name="資料", exact=True)).to_be_visible()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"starred-{width}.png", animations="disabled")
    page.locator('summary[aria-label="Actions for 資料"]').click()
    page.get_by_role("button", name="Remove from Starred", exact=True).click()
    expect(
        page.get_by_role("heading", name="No Starred Items Yet", exact=True)
    ).to_be_visible()


def test_grid_uses_own_upload_derivatives_without_original_reads(server, desktop):
    _, page = desktop
    login(page, server.url)
    folder = page.request.post(
        server.url + "/__test__/thumbnails/generated", headers=CONTROL
    ).json()["path"]
    originals = []
    page.on(
        "request",
        lambda request: (
            originals.append(request.url) if "/file?" in request.url else None
        ),
    )
    page.goto(server.url + "/?path=" + folder)
    page.get_by_role("button", name="Grid view", exact=True).click()
    for name in ["Illustration.png", "Walkthrough.mp4"]:
        row = page.locator(".file-row").filter(
            has=page.get_by_role("button", name=name, exact=True)
        )
        expect(row.locator(".file-tile-preview img")).to_be_visible()
        assert (
            row.locator(".file-tile-preview img")
            .get_attribute("src")
            .startswith("data:image/jpeg")
        )
    assert originals == []
    page.screenshot(path=ARTIFACTS / "generated-thumbnail-grid.png")
