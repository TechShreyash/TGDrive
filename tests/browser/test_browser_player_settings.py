"""Native player settings preserve playback state and button activation."""

from __future__ import annotations

import os

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, expect, login, preview_dialog

browser, server, desktop = fixtures.browser, fixtures.server, fixtures.desktop
pytestmark = pytest.mark.e2e


def open_video(page, server, filename="multitrack.mkv", *, touch=False):
    login(page, server.url)
    for name in ("Preview samples", filename):
        button = page.get_by_role("button", name=name, exact=True)
        if touch:
            button.tap()
        else:
            # Exercise player layout independently of mouse folder selection.
            button.press("Enter")
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(player).to_be_visible(timeout=30000)
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    return dialog, player


@pytest.mark.parametrize("width,height", [(1440, 960), (390, 844), (740, 360)])
def test_native_settings_labels_stay_within_player(server, desktop, width, height):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    dialog, player = open_video(page, server)
    player.hover()
    # Compact controls can put Settings behind the extra-controls button.
    settings = player.locator("button.movi-settings-btn")
    if not settings.is_visible():
        player.locator("button.movi-more-btn").click()
    settings.click()
    menu = player.locator(".movi-settings-menu")
    expect(menu).to_be_visible()
    for label in ("Ambient mode", "Stable volume", "Loop", "Crop black bars"):
        expect(menu.get_by_role("button", name=label, exact=True)).to_be_visible()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"video-settings-{width}x{height}.png")
    menu.get_by_role("button", name="Aspect", exact=False).click()
    expect(menu.locator(".movi-settings-page-title")).to_have_text("Aspect")
    expect(menu.get_by_role("button", name="Fill", exact=True)).to_be_visible()
    expect(menu.get_by_role("button", name="Stretch", exact=True)).to_be_visible()
    bounds = menu.bounding_box()
    assert bounds["x"] >= -1 and bounds["x"] + bounds["width"] <= width + 1, bounds
    assert bounds["y"] >= -1 and bounds["y"] + bounds["height"] <= height + 1, bounds
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"video-fit-settings-{width}x{height}.png")
    dialog.get_by_role("button", name="Close Preview", exact=True).click()


def test_space_activates_focused_settings_and_mute_without_toggling_play(
    server, desktop
):
    _, page = desktop
    _, player = open_video(page, server)
    assert player.evaluate("element => element.paused")
    player.hover()
    settings = player.locator("button.movi-settings-btn")
    settings.focus()
    page.keyboard.press("Space")
    expect(player.locator(".movi-settings-menu")).to_be_visible()
    assert player.evaluate("element => element.paused"), (
        "Space on Settings must open Settings"
    )
    settings.focus()
    page.keyboard.press("Space")
    expect(player.locator(".movi-settings-menu")).not_to_be_visible()
    mute = player.locator("button.movi-volume-btn")
    before = player.evaluate("element => element.muted")
    mute.focus()
    page.keyboard.press("Space")
    assert player.evaluate("element => element.muted") is not before
    assert player.evaluate("element => element.paused"), (
        "Space on Mute must keep video paused"
    )
    # Player-level Space retains the familiar play/pause shortcut.
    player.focus()
    page.keyboard.press("Space")
    expect(player).to_have_js_property("paused", False)


@pytest.mark.parametrize("playing", [False, True])
def test_native_settings_switches_preserve_playback_state(server, desktop, playing):
    _, page = desktop
    _, player = open_video(page, server)
    if playing:
        player.locator("button.movi-play-pause").click()
        page.wait_for_function("() => !document.querySelector('movi-player').paused")
    player.hover()
    player.locator("button.movi-settings-btn").click()
    menu = player.locator(".movi-settings-menu")
    for label, property_name in [
        ("Ambient mode", "ambientMode"),
        ("Stable volume", "stableVolume"),
        ("Loop", "loop"),
    ]:
        row = menu.get_by_role("button", name=label, exact=True)
        before = player.evaluate(
            "(element, property) => element[property]", property_name
        )
        row.focus()
        page.keyboard.press("Space")
        expect(row).to_have_attribute("aria-pressed", str(not before).lower())
        assert player.evaluate("element => element.paused") is not playing
        page.keyboard.press("Enter")
        expect(row).to_have_attribute("aria-pressed", str(before).lower())
        assert player.evaluate("element => element.paused") is not playing
    # A keyboard shortcut without a menu still provides a clear confirmation.
    player.locator("button.movi-settings-btn").click()
    player.focus()
    page.keyboard.press("u")
    expect(player.locator(".movi-osd-text")).to_contain_text("Stable Volume")


def test_glow_and_crop_keep_their_shared_frame_sampler_when_either_is_enabled(
    server, desktop
):
    _, page = desktop
    _, player = open_video(page, server)
    player.evaluate("""element => {
      element.ambientMode = false; element.cropbars = false;
      element.cropbars = true; element.ambientMode = true;
      element.ambientMode = false;
    }""")
    state = player.evaluate("""element => ({
      glow:element.ambientMode,crop:element.cropbars,
      sampler:element.player.videoRenderer.ambientEnabled,
      detector:element.player.videoRenderer.barCropEnabled
    })""")
    assert state == {"glow": False, "crop": True, "sampler": True, "detector": True}, (
        state
    )
    player.evaluate("element => { element.loop = true; return element.play(); }")
    page.wait_for_function("""() => {
      const renderer = document.querySelector('movi-player').player.videoRenderer;
      return renderer.barCropLastCheck > 0;
    }""")
    player.evaluate("""element => {
      element.ambientMode = true; element.cropbars = false;
    }""")
    assert player.evaluate("element => element.player.videoRenderer.ambientEnabled")
    page.wait_for_function("""() => {
      const color = document.querySelector('movi-player').currentAmbientColors;
      return color.r + color.g + color.b > 0;
    }""")
    player.evaluate("element => { element.ambientMode = false; }")
    assert not player.evaluate("element => element.player.videoRenderer.ambientEnabled")


def test_software_video_does_not_offer_an_inactive_glow_switch(server, desktop):
    _, page = desktop
    _, player = open_video(page, server, "software-mpeg4.wmv")
    assert player.evaluate("element => element.player.isSoftwareDecoding()")
    player.hover()
    player.locator("button.movi-settings-btn").click()
    expect(player.get_by_role("button", name="Ambient mode", exact=True)).to_have_count(
        0
    )
    before = player.evaluate("element => element.ambientMode")
    player.focus()
    page.keyboard.press("g")
    assert player.evaluate("element => element.ambientMode") is before
    player.locator("button.movi-settings-btn").click()
    player.click(button="right", position={"x": 120, "y": 120})
    context = page.locator(".movi-context-menu")
    expect(context).to_be_visible()
    expect(context.locator('[data-action="ambient-toggle"]')).not_to_be_visible()


def test_touch_settings_scroll_and_switches_are_reachable(server, browser):
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _, player = open_video(page, server, touch=True)
        settings = player.locator("button.movi-settings-btn")
        if not settings.is_visible():
            player.locator("button.movi-more-btn").tap()
        settings.tap()
        menu = player.locator(".movi-settings-menu")
        expect(menu).to_be_visible()
        repeat = menu.get_by_role("button", name="Loop", exact=True)
        repeat.tap()
        expect(repeat).to_have_attribute("aria-pressed", "true")
        assert player.evaluate("element => element.paused")
        menu.get_by_role("button", name="Aspect", exact=False).tap()
        fill = menu.get_by_role("button", name="Fill", exact=True)
        expect(fill).to_be_visible()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=ARTIFACTS / "video-touch-settings-390x844.png")
        fill.tap()
        expect(menu).not_to_be_visible()
        assert player.evaluate("element => element.paused")
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("selected", ["Original", "Fixture alternate source"])
def test_external_quality_label_and_same_choice_keep_keyboard_playback_working(
    server, desktop, selected
):
    _, page = desktop
    _, player = open_video(page, server)
    player.hover()
    settings = player.locator("button.movi-settings-btn")
    settings.click()
    menu = player.locator(".movi-settings-menu")
    quality = menu.locator('.movi-settings-row[data-page="quality"]')
    if selected != "Original":
        quality.click()
        player.evaluate("element => { window.previousQualityPlayer = element; }")
        menu.get_by_role("button", name=selected, exact=True).click()
        page.wait_for_function("""() => {
          const player = document.querySelector('movi-player');
          return player && player !== window.previousQualityPlayer &&
            player.duration > 4;
        }""")
        player.hover()
        settings.click()
    expect(quality.locator(".movi-settings-row-value")).to_have_text(selected)
    menu.get_by_role("button", name="Loop", exact=True).click()
    # Native state sync must keep the external server source label, rather than
    # replacing it with the decoded resolution of the current file.
    expect(quality.locator(".movi-settings-row-value")).to_have_text(selected)
    assert player.evaluate("element => element.paused")
    quality.click()
    choice = menu.get_by_role("button", name=selected, exact=True)
    expect(choice).to_have_attribute("aria-pressed", "true")
    player.evaluate("element => { window.sameQualityPlayer = element; }")
    choice.focus()
    page.keyboard.press("Enter")
    expect(menu).not_to_be_visible()
    assert player.evaluate("element => element === window.sameQualityPlayer")
    assert player.evaluate("""element => document.activeElement === element &&
      !element.shadowRoot.activeElement?.closest('.movi-settings-menu')""")
    page.keyboard.press("k")
    expect(player).to_have_js_property("paused", False)
    page.wait_for_function("""() => {
      const player = document.querySelector('movi-player');
      return player.player.getState() === 'playing' && player.currentTime > 0.1;
    }""")
    page.keyboard.press("Space")
    expect(player).to_have_js_property("paused", True)
