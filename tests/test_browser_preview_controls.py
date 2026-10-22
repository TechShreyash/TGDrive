"""The real viewer fit modes and native fullscreen work beyond video."""

from __future__ import annotations

import os

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)
import test_browser_e2e as fixtures
from test_browser_e2e import ARTIFACTS, CONTROL, expect, login

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


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
    page.wait_for_function("!!document.fullscreenElement")
    expect(
        page.get_by_role("button", name="Exit Fullscreen", exact=True)
    ).to_be_visible()
    page.get_by_role("button", name="Exit Fullscreen", exact=True).click()
    page.wait_for_function("!document.fullscreenElement")
    expect(page.get_by_role("dialog")).to_be_visible()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"pdf-fit-controls-{width}.png")


@pytest.mark.parametrize(
    "name",
    [
        "sample.png",
        "sample.wav",
        "reports.csv",
        "multitrack.mkv",
        "The little journey.epub",
    ],
)
def test_supported_previews_enter_exit_fullscreen_and_close(server, desktop, name):
    _, page = desktop
    login(page, server.url)
    if name.endswith("epub"):
        folder = page.request.post(
            server.url + "/__test__/books", headers=CONTROL
        ).json()["path"]
        page.goto(server.url + "/?path=" + folder)
    else:
        page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    page.get_by_role("button", name=name, exact=True).press("Enter")
    enter = page.get_by_role("button", name="Enter Fullscreen", exact=True)
    expect(enter).to_be_visible()
    enter.click()
    page.wait_for_function("!!document.fullscreenElement")
    page.get_by_role("button", name="Exit Fullscreen", exact=True).click()
    page.wait_for_function("!document.fullscreenElement")
    enter.click()
    page.wait_for_function("!!document.fullscreenElement")
    page.get_by_role("button", name="Close Preview", exact=True).click()
    page.wait_for_function("!document.fullscreenElement")
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
