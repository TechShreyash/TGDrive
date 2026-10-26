"""One compact settings menu and familiar mouse/keyboard video controls."""

from __future__ import annotations

import re
from typing import Any

import pytest

import tests.browser.test_browser_player_gestures as gestures
from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    Server,
    expect,
    login,
    preview_dialog,
)

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)
long_video_bytes = gestures.long_video_bytes


def _open_video(
    page: Any,
    server: Server,
    *,
    touch: bool = False,
    filename: str = "multitrack.mkv",
) -> tuple[Any, Any]:
    login(page, server.url)
    for name in ("Preview samples", filename):
        button = page.get_by_role("button", name=name, exact=True)
        button.tap() if touch else button.press("Enter")
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    return dialog, player


def _assert_bounded(control: Any, width: int, height: int) -> None:
    expect(control).to_be_in_viewport(ratio=1)
    bounds = control.bounding_box()
    assert bounds is not None
    assert bounds["x"] >= -1 and bounds["x"] + bounds["width"] <= width + 1, bounds
    assert bounds["y"] >= -1 and bounds["y"] + bounds["height"] <= height + 1, bounds


@pytest.mark.parametrize("width,height", [(320, 740), (390, 844), (720, 360)])
def test_small_touch_players_have_one_settings_menu_and_visible_fullscreen(
    server: Server, browser: Any, width: int, height: int
) -> None:
    context = browser.new_context(
        viewport={"width": width, "height": height}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    page.set_default_timeout(10000)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _, player = _open_video(page, server, touch=True)
        player.tap(
            position={
                "x": width * 0.25,
                "y": player.evaluate("element=>element.clientHeight") * 0.35,
            }
        )
        expect(player.locator(".movi-controls-container")).to_have_class(
            re.compile("movi-controls-visible")
        )
        settings = player.get_by_role("button", name="Settings", exact=True)
        expect(settings).to_have_count(1)
        expect(settings).to_be_visible()
        expect(player.locator("button.movi-more-btn")).not_to_be_visible()
        fullscreen = player.locator("button.movi-fullscreen-btn")
        expect(fullscreen).to_be_visible()
        _assert_bounded(settings, width, height)
        _assert_bounded(fullscreen, width, height)
        settings.tap()
        menu = player.locator(".movi-settings-menu")
        expect(menu).to_be_visible()
        _assert_bounded(menu, width, height)
        fit = menu.get_by_role("button", name="Aspect", exact=False)
        fit.scroll_into_view_if_needed()
        expect(fit).to_be_in_viewport()
        if page.evaluate("'documentPictureInPicture' in window"):
            pip = menu.get_by_role("button", name="Picture in picture", exact=True)
            pip.scroll_into_view_if_needed()
            expect(pip).to_be_in_viewport()
        expect(menu.get_by_role("button", name="Chapters", exact=True)).to_have_count(0)
        # Public chapter metadata exercises discoverability without frame artwork.
        player.evaluate("""element=>{
          element.__previewFrameReads = 0;
          const preview = element.player.getPreviewFrame.bind(element.player);
          element.player.getPreviewFrame = (...args) => {
            element.__previewFrameReads++;
            return preview(...args);
          };
          element.player.setChapters([
            {title:'Opening',start:0},{title:'Next scene',start:2}
          ]);
          element.dispatchEvent(new Event('loadedmetadata'));
        }""")
        page.wait_for_function("""()=>document.querySelector('movi-player')?.shadowRoot
          ?.querySelector('.movi-chapter-pill')""")
        chapters = menu.get_by_role("button", name="Chapters", exact=True)
        chapters.scroll_into_view_if_needed()
        expect(chapters).to_be_in_viewport()
        assert player.evaluate("element=>element.scrollLeft===0&&element.scrollTop===0")
        chapters.tap()
        panel = player.get_by_role("region", name="Chapters", exact=True)
        expect(panel).to_be_visible()
        expect(panel.locator(".movi-timeline-noframe")).to_have_count(2)
        expect(panel.locator("img")).to_have_count(0)
        assert player.evaluate("element => element.__previewFrameReads") == 0
        _assert_bounded(panel, width, height)
        panel.get_by_role("button", name="Next scene 0:02", exact=True).tap()
        page.wait_for_function(
            "()=>document.querySelector('movi-player').currentTime>1.8"
        )
        expect(panel).to_be_visible()
        assert player.evaluate("element=>element.paused")
        panel.locator(".movi-timeline-close").tap()
        expect(panel).not_to_be_visible()
        expect(settings).to_be_visible()
        settings.tap()
        expect(menu).to_be_visible()
        menu.evaluate("element=>element.scrollTop=0")
        _assert_bounded(menu, width, height)
        expect(menu).to_have_css("opacity", "1")
        expect(player.locator(".movi-controls-container")).to_have_css("opacity", "1")
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=ARTIFACTS / f"single-settings-touch-{width}x{height}.png")
        assert errors == []
    finally:
        context.close()


def test_original_only_phone_settings_menu_is_clear_and_has_no_quality_row(
    server: Server, browser: Any
) -> None:
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    try:
        _, player = _open_video(
            page, server, touch=True, filename="movtext-subtitles.mp4"
        )
        player.tap(position={"x": 100, "y": 200})
        expect(player.locator(".movi-controls-container")).to_have_class(
            re.compile("movi-controls-visible")
        )
        settings = player.get_by_role("button", name="Settings", exact=True)
        settings.tap()
        menu = player.locator(".movi-settings-menu")
        expect(menu).to_have_css("opacity", "1")
        expect(player.locator(".movi-controls-container")).to_have_css("opacity", "1")
        _assert_bounded(menu, 390, 844)
        expect(menu.locator('[data-page="quality"]')).to_have_count(0)
        expect(player.locator("button.movi-more-btn")).not_to_be_visible()
        expect(settings).to_have_count(1)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=ARTIFACTS / "single-settings-original-touch-390x844.png")
    finally:
        context.close()


def test_desktop_picture_clicks_and_standard_keyboard_shortcuts(
    server: Server, desktop: tuple[Any, Any], long_video_bytes: bytes
) -> None:
    _, page = desktop
    gestures.route_long_media(page, long_video_bytes)
    dialog, player = _open_video(page, server)
    page.wait_for_function("()=>document.querySelector('movi-player').duration>60")
    picture = {"x": 500, "y": 300}
    player.click(position=picture)
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>0.3")
    player.click(position=picture)
    expect(player).to_have_js_property("paused", True)
    player.dblclick(position=picture)
    page.wait_for_function("()=>!!document.fullscreenElement")
    assert player.evaluate("element=>element.paused")
    player.press("f")
    page.wait_for_function("()=>!document.fullscreenElement")
    player.focus()
    player.press("Space")
    expect(player).to_have_js_property("paused", False)
    player.press("k")
    expect(player).to_have_js_property("paused", True)
    gestures.seek_to(page, player, 20)
    player.focus()
    player.press("ArrowRight")
    page.wait_for_function("""()=>{
      const player=document.querySelector('movi-player');
      return !player.seeking&&Math.abs(player.currentTime-30)<0.4;
    }""")
    player.press("ArrowLeft")
    page.wait_for_function("""()=>{
      const player=document.querySelector('movi-player');
      return !player.seeking&&Math.abs(player.currentTime-20)<0.4;
    }""")
    before = player.evaluate("element=>element.muted")
    player.press("m")
    assert player.evaluate("element=>element.muted") is not before
    player.press("m")
    assert player.evaluate("element=>element.muted") is before
    player.press("f")
    page.wait_for_function("()=>!!document.fullscreenElement")
    player.press("Escape")
    page.wait_for_function("()=>!document.fullscreenElement")
    expect(dialog).to_be_visible()


def test_settings_keyboard_activation_and_escape_preserve_playback_and_preview(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    dialog, player = _open_video(page, server)
    settings = player.get_by_role("button", name="Settings", exact=True)
    settings.focus()
    settings.press("Space")
    menu = player.locator(".movi-settings-menu")
    expect(menu).to_be_visible()
    assert player.evaluate("element=>element.paused")
    repeat = menu.get_by_role("button", name="Loop", exact=True)
    volume = player.evaluate("element=>element.volume")
    time = player.evaluate("element=>element.currentTime")
    repeat.focus()
    repeat.press("ArrowUp")
    expect(repeat).not_to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(repeat).to_be_focused()
    page.keyboard.press("Home")
    expect(menu.locator("button:visible").first).to_be_focused()
    page.keyboard.press("End")
    expect(menu.locator("button:visible").last).to_be_focused()
    assert player.evaluate("element=>element.volume") == volume
    assert player.evaluate("element=>element.currentTime") == time
    repeat.focus()
    repeat.press("Space")
    expect(repeat).to_have_attribute("aria-pressed", "true")
    repeat.press("Enter")
    expect(repeat).to_have_attribute("aria-pressed", "false")
    assert player.evaluate("element=>element.paused")

    def open_page(name: str) -> None:
        if not menu.is_visible():
            settings.focus()
            settings.press("Space")
        row = menu.locator(f'.movi-settings-row[data-page="{name}"]')
        row.focus()
        row.press("Enter")

    open_page("speed")
    speed = menu.locator('.movi-speed-item[data-speed="1.5"]')
    speed.focus()
    speed.press("Space")
    expect(player).to_have_js_property("playbackRate", 1.5)
    assert player.evaluate("element=>element.paused")
    expect(menu).not_to_be_visible()
    open_page("audio")
    audio = player.evaluate("element=>element.player.getAudioTracks()[1].id")
    audio_choice = menu.locator(f'.movi-audio-track-item[data-track-id="{audio}"]')
    audio_choice.focus()
    audio_choice.press("Enter")
    page.wait_for_function(
        "id=>document.querySelector('movi-player').player.trackManager.getActiveAudioTrack()?.id===id",
        arg=audio,
    )
    assert player.evaluate("element=>element.paused")
    expect(menu).not_to_be_visible()
    open_page("subtitles")
    subtitle = player.evaluate("element=>element.player.getSubtitleTracks()[0].id")
    subtitle_choice = menu.locator(
        f'.movi-subtitle-track-item[data-track-id="{subtitle}"]'
    )
    subtitle_choice.focus()
    subtitle_choice.press("Space")
    page.wait_for_function(
        "id=>document.querySelector('movi-player').player.trackManager.getActiveSubtitleTrack()?.id===id",
        arg=subtitle,
    )
    assert player.evaluate("element=>element.paused")
    expect(menu).not_to_be_visible()
    settings.focus()
    settings.press("Space")
    menu.get_by_role("button", name="Aspect", exact=False).click()
    expect(menu.locator(".movi-settings-page-title")).to_have_text("Aspect")
    page.keyboard.press("Escape")
    expect(menu).to_be_visible()
    expect(menu.get_by_role("button", name="Aspect", exact=False)).to_be_visible()
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu).not_to_be_visible()
    expect(dialog).to_be_visible()
    expect(settings).to_be_focused()
    assert player.evaluate("element=>element.paused")
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)


def test_playing_controls_hide_when_idle_and_remain_visible_for_keyboard_focus(
    server: Server, desktop: tuple[Any, Any], long_video_bytes: bytes
) -> None:
    _, page = desktop
    gestures.route_long_media(page, long_video_bytes)
    _, player = _open_video(page, server)
    page.wait_for_function("()=>document.querySelector('movi-player').duration>60")
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>0.3")
    page.mouse.move(20, 20)
    controls = player.locator(".movi-controls-container")
    expect(controls).to_have_class(re.compile("movi-controls-hidden"), timeout=6000)
    settings = player.get_by_role("button", name="Settings", exact=True)
    for _ in range(20):
        page.keyboard.press("Tab")
        if settings.evaluate("element=>element.getRootNode().activeElement===element"):
            break
    expect(settings).to_be_focused()
    assert settings.evaluate("element=>element.matches(':focus-visible')")
    page.mouse.move(20, 20)
    start = player.evaluate("element=>element.currentTime")
    page.wait_for_function(
        "start=>document.querySelector('movi-player').currentTime>start+3.5", arg=start
    )
    expect(settings).to_be_focused()
    expect(controls).to_have_class(re.compile("movi-controls-visible"))
    settings.press("Space")
    menu = player.locator(".movi-settings-menu")
    expect(menu).to_be_visible()
    assert not player.evaluate("element=>element.paused")
    settings.press("Escape")
    expect(menu).not_to_be_visible()
    player.focus()
    page.mouse.move(20, 20)
    expect(controls).to_have_class(re.compile("movi-controls-hidden"), timeout=6000)


@pytest.mark.parametrize(
    "kind,button_name,menu_class,choice_class,key",
    [
        (
            "audio",
            "Audio Track",
            ".movi-audio-track-menu",
            ".movi-audio-track-item",
            "Enter",
        ),
        (
            "subtitles",
            "Subtitles/Captions",
            ".movi-subtitle-track-menu",
            ".movi-subtitle-track-item",
            "Space",
        ),
    ],
)
def test_direct_track_menu_keyboard_selection_keeps_video_paused(
    server: Server,
    desktop: tuple[Any, Any],
    kind: str,
    button_name: str,
    menu_class: str,
    choice_class: str,
    key: str,
) -> None:
    _, page = desktop
    _, player = _open_video(page, server)
    button = player.get_by_role("button", name=button_name, exact=True)
    button.focus()
    button.press("Enter")
    menu = player.locator(menu_class)
    expect(menu).to_be_visible()
    if kind == "audio":
        track = player.evaluate("element=>element.player.getAudioTracks()[1].id")
        selected = "getActiveAudioTrack"
    else:
        track = player.evaluate("element=>element.player.getSubtitleTracks()[0].id")
        selected = "getActiveSubtitleTrack"
    choice = menu.locator(f'{choice_class}[data-track-id="{track}"]')
    for _ in range(menu.locator(choice_class).count() + 1):
        page.keyboard.press("ArrowDown")
        if choice.evaluate("element=>element.getRootNode().activeElement===element"):
            break
    expect(choice).to_be_focused()
    assert player.evaluate("element=>element.paused")
    page.keyboard.press(key)
    page.wait_for_function(
        """([method,id])=>document.querySelector('movi-player').player
          .trackManager[method]()?.id===id""",
        arg=[selected, track],
    )
    assert player.evaluate("element=>element.paused")
