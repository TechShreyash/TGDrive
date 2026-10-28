"""Video watch positions remain local to each browser profile."""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.test_browser_e2e import Server, expect, login, preview_dialog

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def _open_video(page: Any) -> tuple[Any, Any, str]:
    file = page.get_by_role("button", name="multitrack.mkv", exact=True)
    item_id = file.locator("xpath=ancestor::*[@data-item-id][1]").get_attribute(
        "data-item-id"
    )
    assert item_id
    file.press("Enter")
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    return dialog, player, "tgdrive.video.position.v1:" + item_id


def _save_seek(page: Any, player: Any, key: str) -> None:
    player.evaluate("async el=>{await el.pause();await el.player.seek(1.5)}")
    page.wait_for_function(
        "()=>{const el=document.querySelector('movi-player');"
        "return el&&!el.seeking&&el.paused&&Math.abs(el.currentTime-1.5)<0.1}"
    )
    page.wait_for_function(
        "key=>Math.abs(Number(localStorage.getItem(key))-1.5)<0.1", arg=key
    )


def test_continue_survives_close_and_reload_without_seeking_automatically(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    dialog, player, key = _open_video(page)
    _save_seek(page, player, key)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()

    dialog, player, _ = _open_video(page)
    expect(
        dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_be_visible()
    assert player.evaluate("el=>el.paused&&el.currentTime<0.1")
    # Opening and closing an unwatched preview must preserve its existing offer.
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    assert page.evaluate(
        "key=>Number(localStorage.getItem(key))", key
    ) == pytest.approx(1.5, abs=0.1)
    _open_video(page)
    page.reload()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    assert player.evaluate("el=>el.paused&&el.currentTime<0.1")
    dialog.get_by_role("button", name="Continue from 0:01", exact=True).click()
    page.wait_for_function(
        "()=>{const el=document.querySelector('movi-player');"
        "return el&&!el.paused&&!el.seeking&&el.currentTime>=1.4}"
    )
    expect(
        dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_have_count(0)


def test_native_play_starts_at_beginning_and_completion_clears_position(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    dialog, player, key = _open_video(page)
    _save_seek(page, player, key)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    dialog, player, _ = _open_video(page)
    expect(
        dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_be_visible()
    player.evaluate(
        "el=>el.addEventListener('ended',()=>{el.dataset.finished='true'},{once:true})"
    )
    player.hover()
    player.locator("button.movi-play-pause").click()
    expect(
        dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_have_count(0)
    page.wait_for_function(
        "()=>{const el=document.querySelector('movi-player');"
        "return el&&!el.paused&&el.currentTime>0.1&&el.currentTime<1.5}"
    )
    page.wait_for_function(
        "()=>document.querySelector('movi-player')?.dataset.finished==='true'",
        timeout=30000,
    )
    page.wait_for_function("key=>localStorage.getItem(key)===null", arg=key)
    # Replacing a completed engine must not restore and re-save its final time.
    player.evaluate(
        "el=>{window.finishedPlayer=el;el.dispatchEvent(new Event('error'))}"
    )
    dialog.get_by_role("button", name="Try again", exact=True).click()
    page.wait_for_function(
        "()=>{const el=document.querySelector('movi-player');"
        "return el&&el!==window.finishedPlayer&&el.duration>4"
        "&&!el.seeking&&el.paused&&el.currentTime<0.1}"
    )
    assert page.evaluate("key=>localStorage.getItem(key)", key) is None
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    assert page.evaluate("key=>localStorage.getItem(key)", key) is None
    dialog, _, _ = _open_video(page)
    expect(
        dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_have_count(0)


def test_same_login_in_another_browser_profile_has_no_saved_position(
    server: Server, browser: Any, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    _, player, key = _open_video(page)
    _save_seek(page, player, key)

    context = browser.new_context(viewport={"width": 1440, "height": 960})
    errors: list[str] = []
    other = context.new_page()
    other.on("pageerror", lambda error: errors.append(str(error)))
    try:
        login(other, server.url)
        other.get_by_role("button", name="Preview samples", exact=True).press("Enter")
        dialog, player, other_key = _open_video(other)
        assert other_key == key
        assert other.evaluate("key=>localStorage.getItem(key)", key) is None
        expect(
            dialog.get_by_role("button", name="Continue from 0:01", exact=True)
        ).to_have_count(0)
        other.wait_for_function("""()=>{
            const player=document.querySelector('movi-player');
            return player?.paused&&!player.seeking&&player.currentTime<0.1;
        }""")
        assert page.evaluate(
            "key=>Number(localStorage.getItem(key))", key
        ) == pytest.approx(1.5, abs=0.1)
        assert errors == []
    finally:
        context.close()
