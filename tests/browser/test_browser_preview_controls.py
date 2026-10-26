"""The real viewer fit modes and native fullscreen work beyond video."""

from __future__ import annotations

import os

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, CONTROL, expect, login

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


def expect_collapsed_fullscreen_toolbar(page):
    page.wait_for_function(
        "document.fullscreenElement === "
        "document.querySelector('.preview-fullscreen-surface')"
    )
    expect(page.locator(".preview-header")).to_be_hidden()
    expect(page.get_by_role("button", name="Show Toolbar", exact=True)).to_be_visible()
    page.wait_for_function("""() => {
      const surface = document.querySelector('.preview-surface');
      const bounds = surface?.getBoundingClientRect();
      return bounds && Math.abs(bounds.top) <= 1 &&
        Math.abs(bounds.height - innerHeight) <= 1;
    }""")


def expect_expanded_fullscreen_toolbar(page):
    page.wait_for_function(
        "document.fullscreenElement === "
        "document.querySelector('.preview-fullscreen-surface')"
    )
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
    if width < 721:
        page.get_by_role("button", name="PDF View Options", exact=True).click()
    page.get_by_role("button", name="Fit Page", exact=True).click()
    expect(page.get_by_role("button", name="Fit Page", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.wait_for_function(
        "() => {const p=document.querySelector('.pdf-page'),"
        "s=document.querySelector('.pdf-scroll');return p && "
        "p.clientHeight <= s.clientHeight-30 && p.clientWidth <= s.clientWidth-30}"
    )
    page.get_by_role("button", name="Fit Width", exact=True).click()
    expect(page.get_by_role("button", name="Fit Width", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.wait_for_function(
        "() => Math.abs(document.querySelector('.pdf-page').clientWidth-"
        "(document.querySelector('.pdf-scroll').clientWidth-32)) <= 2"
    )
    page.get_by_role("button", name="Zoom in", exact=True).click()
    expect(page.get_by_role("button", name="Fit Width", exact=True)).to_have_attribute(
        "aria-pressed", "false"
    )
    page.get_by_role("button", name="Fit Width", exact=True).click()
    if width < 721:
        page.get_by_role("button", name="PDF View Options", exact=True).click()
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
    expect(page.get_by_role("button", name="Fit Width", exact=True)).to_have_attribute(
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
    page.wait_for_function("!document.fullscreenElement")
    expect(page.get_by_role("dialog")).to_be_visible()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"pdf-fit-controls-{width}.png")


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
