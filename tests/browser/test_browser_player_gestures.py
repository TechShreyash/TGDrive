"""Actual touchscreen gestures against a generated 65-second video and real decoder."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import expect, login, preview_dialog

browser, server = fixtures.browser, fixtures.server
pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def long_video_bytes(tmp_path_factory):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is needed to generate the real long-video fixture")
    path = tmp_path_factory.mktemp("player-long-video") / "long.mp4"
    subprocess.run(  # noqa: S603 - fixed local ffmpeg binary and generated sources
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=22050",
            "-t",
            "65",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "12",
            "-c:a",
            "aac",
            "-b:a",
            "16k",
            "-movflags",
            "+faststart",
            "-y",
            str(path),
        ],
        check=True,
        timeout=30,
        capture_output=True,
    )
    return path.read_bytes()


def route_long_media(page, content):
    def serve(route):
        total = len(content)
        range_header = route.request.headers.get("range", "")
        start, end, status = 0, total - 1, 200
        if range_header.startswith("bytes="):
            first, _, last = range_header[6:].partition("-")
            start = int(first) if first else max(0, total - int(last))
            end = min(total - 1, int(last)) if first and last else total - 1
            status = 206
        headers = {
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
            "Content-Length": str(max(0, end - start + 1)),
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        route.fulfill(
            status=status,
            headers=headers,
            body=b"" if route.request.method == "HEAD" else content[start : end + 1],
        )

    page.route("**/file?*", serve)


@pytest.fixture
def touch_player(server, browser, long_video_bytes):
    context = browser.new_context(
        viewport={"width": 900, "height": 500}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    page.set_default_timeout(10000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    route_long_media(page, long_video_bytes)
    try:
        login(page, server.url)
        for name in ("Preview samples", "multitrack.mkv"):
            page.get_by_role("button", name=name, exact=True).tap()
        dialog = preview_dialog(page)
        player = dialog.locator("movi-player")
        expect(player).to_be_visible(timeout=30000)
        page.wait_for_function(
            "() => document.querySelector('movi-player')?.duration > 60"
        )
        expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
        player.evaluate("element => { element.loop = false; }")
        yield page, player, context
        assert errors == []
    finally:
        context.close()


def seek_to(page, player, position):
    player.evaluate(
        """async (element, time) => {
          await element.pause(); element.currentTime = time;
        }""",
        position,
    )
    page.wait_for_function(
        """time => {
          const p=document.querySelector('movi-player');
          return !p.seeking && Math.abs(p.currentTime-time)<0.3;
        }""",
        arg=position,
    )
    # Start a new tap chain independently of any preceding seek confirmation.
    page.wait_for_timeout(1100)


def tap_surface(page, player, side, count=1):
    rect = player.bounding_box()
    fraction = {"left": 0.2, "center": 0.5, "right": 0.8}[side]
    x = rect["x"] + rect["width"] * fraction
    # Stay clear of the central Play button and bottom playback controls.
    y = rect["y"] + rect["height"] * 0.25
    for _ in range(count):
        page.touchscreen.tap(x, y)
        page.wait_for_timeout(70)


def wait_time(page, position, tolerance=0.5):
    page.wait_for_function(
        """value => {
          const p=document.querySelector('movi-player');
          return !p.seeking && Math.abs(p.currentTime-value.time)<value.tolerance;
        }""",
        arg={"time": position, "tolerance": tolerance},
    )


def test_touch_double_taps_seek_ten_seconds_and_repeated_taps_accumulate(touch_player):
    page, player, _ = touch_player
    await_pointer_events = []
    page.expose_function(
        "recordTouchPointer", lambda kind: await_pointer_events.append(kind)
    )
    player.evaluate(
        """element => element.addEventListener('pointerdown', event => {
          if(event.pointerType==='touch') window.recordTouchPointer(event.pointerType);
        }, true)"""
    )
    seek_to(page, player, 20)
    tap_surface(page, player, "right", 2)
    wait_time(page, 30)
    assert player.evaluate("element => element.paused")
    seek_to(page, player, 20)
    tap_surface(page, player, "right", 4)
    wait_time(page, 50)
    seek_to(page, player, 30)
    tap_surface(page, player, "left", 2)
    wait_time(page, 20)
    assert player.evaluate("element => element.paused")
    assert len(await_pointer_events) >= 8


def test_touch_seek_preserves_playback_and_clamps_file_bounds(touch_player):
    page, player, _ = touch_player
    seek_to(page, player, 20)
    player.locator("button.movi-play-pause").tap()
    page.wait_for_function("() => !document.querySelector('movi-player').paused")
    tap_surface(page, player, "right", 2)
    page.wait_for_function(
        "() => document.querySelector('movi-player').currentTime >= 30"
    )
    assert not player.evaluate("element => element.paused")
    seek_to(page, player, 3)
    tap_surface(page, player, "left", 2)
    wait_time(page, 0)
    seek_to(page, player, 62)
    tap_surface(page, player, "right", 2)
    wait_time(page, player.evaluate("element => element.duration"))
    assert player.evaluate("element => element.currentTime <= element.duration")


def test_touch_single_tap_toggles_controls_and_menu_taps_never_seek(touch_player):
    page, player, _ = touch_player
    seek_to(page, player, 20)
    player.evaluate("element => element.showControls()")
    tap_surface(page, player, "center")
    page.wait_for_timeout(450)
    assert not player.evaluate("element => element.areControlsVisible()")
    tap_surface(page, player, "center")
    page.wait_for_timeout(450)
    assert player.evaluate("element => element.areControlsVisible()")
    assert player.evaluate("element => element.paused")
    settings = player.locator("button.movi-settings-btn")
    if not settings.is_visible():
        player.locator("button.movi-more-btn").tap()
    settings.tap()
    expect(player.locator(".movi-settings-menu")).to_be_visible()
    tap_surface(page, player, "left", 2)
    wait_time(page, 20)
    assert player.evaluate("element => element.paused")
    expect(player.locator(".movi-settings-menu")).not_to_be_visible()
    assert not player.evaluate("element => element.isFullscreenActive()")
    page.wait_for_timeout(500)
    tap_surface(page, player, "left", 2)
    wait_time(page, 10)


def test_touch_drag_and_multitouch_do_not_become_double_tap_seeks(touch_player):
    page, player, context = touch_player
    seek_to(page, player, 20)
    rect = player.bounding_box()
    x, y = rect["x"] + rect["width"] * 0.2, rect["y"] + rect["height"] * 0.45
    cdp = context.new_cdp_session(page)
    first = {"x": x, "y": y, "id": 1}
    second = {"x": x + 30, "y": y, "id": 2}
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [first]})
    # Exceed the player's 20px tap slop without asking the browser to perform
    # its own horizontal history-back swipe (an 80px drag can navigate away).
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchMove", "touchPoints": [{**first, "x": x + 24}]},
    )
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(500)
    expect(player).to_be_visible()
    wait_time(page, 20)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [first]})
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [first, second]},
    )
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": [first]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(500)
    wait_time(page, 20)
