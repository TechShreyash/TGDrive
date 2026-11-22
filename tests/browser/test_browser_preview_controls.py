"""The real viewer fit modes and native fullscreen work beyond video."""

from __future__ import annotations

import os
from urllib.parse import urlencode

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    CONTROL,
    expect,
    fixture_nodes,
    login,
)

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


def test_standalone_video_shows_filename_and_selectable_link(server, desktop):
    _, page = desktop
    page.set_viewport_size({"width": 390, "height": 844})
    login(page, server.url)
    folder = next(
        row
        for row in fixture_nodes(page, server, "/")
        if row["name"] == "Preview samples"
    )
    parent = "/" + folder["id"]
    video = next(
        row
        for row in fixture_nodes(page, server, parent)
        if row["name"] == "movtext-subtitles.mp4"
    )
    query = urlencode({"path": parent + "/" + video["id"]})
    url = server.url + "/stream?" + query
    page.goto(url)
    expect(page.get_by_role("heading", name=video["name"], exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Download original")).to_have_attribute(
        "href", server.url + "/file?" + query + "&download=1"
    )
    page.evaluate("""() => {
        navigator.clipboard.writeText = () => Promise.reject(
            new DOMException("Clipboard permission denied", "NotAllowedError")
        );
    }""")
    page.get_by_role("button", name="Copy stream link", exact=True).click()
    fallback = page.get_by_label("Stream link to copy", exact=True)
    expect(fallback).to_be_focused()
    expect(fallback).to_have_value(url)
    assert fallback.evaluate(
        "input => input.selectionEnd - input.selectionStart"
    ) == len(url)
    page.get_by_role("button", name="Copy file link", exact=True).click()
    expect(page.get_by_label("File link to copy", exact=True)).to_have_value(
        server.url + "/file?" + query
    )


def choose_pdf_fit(page, name):
    """Fit modes live in the zoom menu; open it when the item is not showing."""
    item = page.locator(f'[aria-label="{name}"]')
    if not item.is_visible():
        page.get_by_role("button", name="PDF View Options", exact=True).click()
    item.click()
    return item


def expect_collapsed_fullscreen_toolbar(page):
    expect(page.locator(".preview-fullscreen-surface:fullscreen")).to_be_visible()
    expect(page.locator(".preview-header")).to_be_hidden()
    expect(page.get_by_role("button", name="Show Toolbar", exact=True)).to_be_visible()
    assert page.locator(".preview-surface").evaluate("""surface => {
      const bounds = surface.getBoundingClientRect();
      return Math.abs(bounds.top) <= 1 && Math.abs(bounds.height - innerHeight) <= 1;
    }""")


def expect_expanded_fullscreen_toolbar(page):
    expect(page.locator(".preview-fullscreen-surface:fullscreen")).to_be_visible()
    expect(page.locator(".preview-header")).to_be_visible()
    expect(
        page.get_by_role("button", name="Hide Toolbar", exact=True)
    ).to_have_attribute("aria-expanded", "true")


def expand_fullscreen_toolbar(page):
    page.get_by_role("button", name="Show Toolbar", exact=True).click()
    expect_expanded_fullscreen_toolbar(page)


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_pdf_real_fit_width_page_and_fullscreen(server, desktop, width):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 900 if width > 720 else 844})
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    page.get_by_role("button", name="sample.pdf", exact=True).press("Enter")
    expect(page.locator(".pdf-page canvas").first).to_be_visible()
    fit_page = choose_pdf_fit(page, "Fit Page")
    expect(fit_page).to_have_attribute("aria-pressed", "true")
    page.wait_for_function(
        "() => {const p=document.querySelector('.pdf-page'),"
        "s=document.querySelector('.pdf-scroll');return p && "
        "p.clientHeight <= s.clientHeight-30 && p.clientWidth <= s.clientWidth-30}"
    )
    fit_width = choose_pdf_fit(page, "Fit Width")
    expect(fit_width).to_have_attribute("aria-pressed", "true")
    # Fit width fills the viewport up to the 960px reading width (PDF_READING_WIDTH).
    page.wait_for_function(
        "() => Math.abs(document.querySelector('.pdf-page').clientWidth-"
        "Math.min(960, document.querySelector('.pdf-scroll').clientWidth-32)) <= 2"
    )
    page.get_by_role("button", name="Zoom in", exact=True).click()
    expect(fit_width).to_have_attribute("aria-pressed", "false")
    choose_pdf_fit(page, "Fit Width")
    options = page.get_by_role("button", name="PDF View Options", exact=True)
    if options.get_attribute("aria-expanded") == "true":
        options.click()
    page.get_by_role("button", name="Enter Fullscreen", exact=True).click()
    expect_expanded_fullscreen_toolbar(page)
    hide = page.get_by_role("button", name="Hide Toolbar", exact=True)
    expect(hide).to_be_focused()
    hide.press("Enter")
    expect_collapsed_fullscreen_toolbar(page)
    page.screenshot(path=ARTIFACTS / f"pdf-fullscreen-collapsed-{width}.png")
    show = page.get_by_role("button", name="Show Toolbar", exact=True)
    show.focus()
    show.press("Enter")
    expect(page.locator(".preview-header")).to_be_visible()
    if width < 721:
        page.get_by_role("button", name="PDF View Options", exact=True).click()
    page.get_by_role("button", name="Zoom in", exact=True).click()
    expect(page.locator('[aria-label="Fit Width"]')).to_have_attribute(
        "aria-pressed", "false"
    )
    if width < 721:
        page.get_by_role("button", name="PDF View Options", exact=True).click()
    # The expanded toolbar takes its own row instead of covering the PDF.
    assert page.locator(".preview-surface").evaluate(
        "el => el.getBoundingClientRect().top >= "
        "document.querySelector('.preview-header').getBoundingClientRect().bottom - 1"
    )
    page.screenshot(path=ARTIFACTS / f"pdf-fullscreen-expanded-{width}.png")
    page.get_by_role("button", name="Hide Toolbar", exact=True).click()
    expect_collapsed_fullscreen_toolbar(page)
    expect(show).to_be_focused()
    show.press("Enter")
    page.get_by_role("button", name="Exit Fullscreen", exact=True).click()
    expect(page.locator(":fullscreen")).to_have_count(0)
    expect(page.get_by_role("dialog")).to_be_visible()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"pdf-fit-controls-{width}.png")


@pytest.mark.parametrize("width,height,header_limit", [(320, 568, 105), (568, 320, 64)])
def test_phone_pdf_controls_keep_pages_and_keyboard_actions_reachable(
    server, browser, width, height, header_limit
):
    with browser.new_context(
        viewport={"width": width, "height": height}, has_touch=True, is_mobile=True
    ) as context:
        page = context.new_page()
        login(page, server.url)
        page.get_by_role("button", name="Preview samples", exact=True).tap()
        file = page.get_by_role("button", name="sample.pdf", exact=True)
        file.press("Enter")
        viewer = page.get_by_role("dialog", name="sample.pdf", exact=True)
        expect(viewer.locator(".pdf-page canvas").first).to_be_in_viewport(
            timeout=20000
        )
        header = viewer.locator(".preview-header").bounding_box()
        assert header and header["height"] <= header_limit

        jump = viewer.get_by_label("Page", exact=True)
        expect(jump).to_have_value("1")
        jump.fill("1e3")
        jump.press("Enter")
        expect(jump).to_have_value("1")
        jump.fill("2")
        jump.press("Escape")
        expect(jump).to_have_value("1")
        jump.fill(" 2 ")
        jump.press("Enter")
        expect(jump).to_have_value("2")
        jump.fill("999999")
        jump.blur()
        expect(jump).to_have_value("3")

        options = viewer.get_by_role("button", name="PDF View Options", exact=True)
        options.tap()
        panel = viewer.get_by_role("dialog", name="PDF View Options", exact=True)
        expect(panel).to_be_in_viewport(ratio=1)
        assert panel.evaluate("element => element.closest('.preview-header') !== null")
        status = viewer.get_by_role("status", name="PDF zoom", exact=True)
        initial_zoom = status.inner_text()
        panel.get_by_role("button", name="Zoom in", exact=True).tap()
        expect(status).not_to_have_text(initial_zoom)
        page.keyboard.press("Escape")
        expect(panel).to_be_hidden()
        expect(options).to_be_focused()
        expect(viewer).to_be_visible()
        page.keyboard.press("Escape")
        expect(viewer).to_have_count(0)
        expect(file).to_be_focused()


@pytest.mark.parametrize(
    ("name", "width"),
    [
        ("sample.png", 1440),
        ("sample.wav", 1440),
        ("reports.csv", 1440),
        ("The little journey.epub", 1440),
        ("The little journey.epub", 390),
        ("The little journey.epub", 320),
    ],
)
def test_supported_previews_enter_exit_fullscreen_and_close(
    server, desktop, name, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 900 if width > 720 else 844})
    if name == "sample.png":
        page.route(
            "**/api/v1/media?*",
            lambda route: route.fulfill(
                json={"variants": [{"id": "small", "label": "Small"}]}
            ),
        )
    login(page, server.url)
    if name.endswith("epub"):
        folder = page.request.post(
            server.url + "/__test__/books", headers=CONTROL
        ).json()["path"]
        page.goto(server.url + "/?path=" + folder)
    else:
        page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    page.get_by_role("button", name=name, exact=True).press("Enter")
    if name == "sample.png":
        expect(page.locator(".preview-quality")).to_be_visible()
    if name.endswith("epub"):
        expect(
            page.get_by_role("group", name="Reader controls", exact=True)
        ).to_be_visible()
    enter = page.get_by_role("button", name="Enter Fullscreen", exact=True)
    expect(enter).to_be_visible()
    enter.click()
    expect_expanded_fullscreen_toolbar(page)
    if name == "sample.png":
        expect(page.locator(".preview-quality")).to_be_visible()
    page.get_by_role("button", name="Hide Toolbar", exact=True).click()
    expect_collapsed_fullscreen_toolbar(page)
    if name == "sample.png":
        expect(page.locator(".preview-quality")).to_be_hidden()
    expand_fullscreen_toolbar(page)
    if name == "sample.png":
        expect(page.locator(".preview-quality")).to_be_visible()
    if name.endswith("epub"):
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if width <= 720:
            toolbar = page.get_by_role("group", name="Reader controls", exact=True)
            bounds = toolbar.bounding_box()
            assert bounds is not None
            assert bounds["x"] + bounds["width"] >= width - 16
        page.screenshot(path=ARTIFACTS / f"book-fullscreen-expanded-{width}.png")
        expect(
            page.get_by_role("group", name="Reader controls", exact=True)
        ).to_be_visible()
        page.get_by_label("Reading options", exact=True).click()
        page.get_by_role("button", name="Increase text size", exact=True).click()
        expect(page.locator(".book-chapter")).to_have_css("font-size", "20px")
        page.get_by_label("Reading options", exact=True).click()
    page.get_by_role("button", name="Hide Toolbar", exact=True).click()
    expect_collapsed_fullscreen_toolbar(page)
    page.keyboard.press("Escape")
    page.wait_for_function("!document.fullscreenElement")
    expect(page.locator(".preview-header")).to_be_visible()
    expect(enter).to_be_focused()
    expect(page.get_by_role("button", name="Show Toolbar", exact=True)).to_have_count(0)
    enter.click()
    expect_expanded_fullscreen_toolbar(page)
    page.get_by_role("button", name="Close Preview", exact=True).click()
    page.wait_for_function("!document.fullscreenElement")
    expect(page.locator(".file-preview")).to_have_count(0)


def test_video_fullscreen_buttons_share_player_surface(server, desktop):
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    page.get_by_role("button", name="multitrack.mkv", exact=True).press("Enter")
    player = page.locator("movi-player")
    expect(player).to_be_visible(timeout=30000)
    expect(page.locator(".advanced-state")).to_have_count(0, timeout=30000)
    page.get_by_role("button", name="Enter Fullscreen", exact=True).click()
    page.wait_for_function(
        "document.fullscreenElement === document.querySelector('movi-player')"
    )
    # Both routes give the entire viewport to the player, excluding preview chrome.
    assert player.evaluate(
        "el => !el.contains(document.querySelector('.preview-header')) && "
        "Math.abs(el.getBoundingClientRect().top) <= 1 && "
        "Math.abs(el.getBoundingClientRect().height - innerHeight) <= 1"
    )
    page.keyboard.press("Escape")
    page.wait_for_function("!document.fullscreenElement")
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(
        page.get_by_role("button", name="Enter Fullscreen", exact=True)
    ).to_be_visible()
    player.hover()
    fullscreen = player.locator("button.movi-fullscreen-btn")
    fullscreen.click()
    page.wait_for_function(
        "document.fullscreenElement === document.querySelector('movi-player')"
    )
    player.hover()
    fullscreen.click()
    page.wait_for_function("!document.fullscreenElement")
    expect(page.get_by_role("dialog")).to_be_visible()
    page.get_by_role("button", name="Close Preview", exact=True).click()
    expect(page.locator(".file-preview")).to_have_count(0)


def test_settings_discard_icon_has_shared_gap(server, desktop):
    _, page = desktop
    login(page, server.url + "/?view=settings&section=regional")
    button = page.get_by_role("button", name="Discard changes", exact=True)
    expect(button).to_be_visible()
    assert (
        button.locator(".mantine-Button-label").evaluate(
            "e=>parseFloat(getComputedStyle(e).gap)"
        )
        >= 8
    )
    page.screenshot(path=ARTIFACTS / "settings-button-spacing.png")
