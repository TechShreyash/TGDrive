"""Local production-bundle UI with synthetic rebuild progress and generated manga."""

from __future__ import annotations

import os

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, CONTROL, expect, login

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width", [1440, 390])
def test_thumbnail_confirmation_progress_reload_and_cancellation(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 900})
    state = {
        "status": "idle",
        "phase": "Ready",
        "total": 0,
        "processed": 0,
        "generated": 0,
        "failed": 0,
        "skipped": 0,
        "source_bytes": 0,
    }
    starts = []

    def route(request):
        if request.request.method == "POST":
            starts.append(request.request.post_data_json)
            state.update(
                status="running",
                phase="Regenerating thumbnail",
                total=100,
                current_name="movie.mov",
            )
        request.fulfill(
            status=202 if request.request.method == "POST" else 200, json=state.copy()
        )

    def cancel(route):
        state.update(status="cancelled", phase="Stopped")
        route.fulfill(json=state.copy())

    page.route("**/api/v1/settings/thumbnails/rebuild", route)
    page.route("**/api/v1/settings/thumbnails/rebuild/cancel", cancel)
    login(page, server.url + "/?view=settings&section=cache")
    page.get_by_role("button", name="Regenerate all thumbnails", exact=True).click()
    dialog = page.get_by_role("dialog", name="Regenerate all thumbnails?")
    expect(dialog).to_contain_text("substantial bandwidth")
    assert starts == []
    expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_focused()
    dialog.get_by_role("button", name="Start background rebuild", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(
        page.get_by_role("button", name="Cancel rebuild", exact=True)
    ).to_be_visible()
    assert starts == [{"confirmed": True}]
    state.update(
        processed=50, generated=45, skipped=3, failed=2, source_bytes=5 * 1024**2
    )
    progress = page.get_by_role("progressbar", name="Thumbnail rebuild progress")
    expect(progress).to_have_attribute("value", "50")
    page.reload()
    expect(progress).to_have_attribute("value", "50")
    expect(page.locator(".thumbnail-rebuild-status")).to_contain_text("45 regenerated")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"thumbnail-rebuild-{width}.png", full_page=True)
    page.get_by_role("button", name="Cancel rebuild", exact=True).click()
    expect(
        page.get_by_role("button", name="Regenerate all thumbnails", exact=True)
    ).to_be_visible()
    expect(page.locator(".thumbnail-rebuild-status")).to_contain_text("Stopped")


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_image_based_epub_manga_fit_and_direction(server, desktop, width):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 900})
    login(page, server.url)
    folder = page.request.post(server.url + "/__test__/books", headers=CONTROL).json()[
        "path"
    ]
    page.goto(server.url + "/?path=" + folder)
    page.get_by_role("button", name="Image-based manga.epub", exact=True).press("Enter")
    image = page.locator(".book-illustrated-page img")
    expect(image).to_be_visible()
    expect(image).to_have_js_property("naturalWidth", 480)
    page.locator('summary[aria-label="Reading options"]').click()
    page.get_by_label("Page fit", exact=True).select_option("page")
    page.get_by_label("Right to left", exact=True).check()
    page.locator('summary[aria-label="Reading options"]').click()
    bounds = image.bounding_box()
    assert bounds["height"] <= 900 - 180 + 1
    assert abs(bounds["width"] / bounds["height"] - 480 / 720) < 0.01
    page.locator(".book-content").focus()
    page.keyboard.press("ArrowLeft")
    expect(page.locator(".book-position")).to_have_text("2 / 2")
    expect(image).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"manga-epub-fit-page-{width}.png")
