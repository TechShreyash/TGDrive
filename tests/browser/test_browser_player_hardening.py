"""Real media regressions for the locally shipped player corrections."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.browser.test_browser_e2e import ROOT, Server, expect, login
from tests.browser.test_browser_player_controls import _serve_media

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def _open(page: Any, url: str, name: str) -> Any:
    login(page, url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name=name, exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    return player


def test_audio_only_mp4_plays_seeks_and_keeps_loading_feedback_clear(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path
) -> None:
    _, page = desktop
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is needed for the audio-only container")
    media = tmp_path / "audio-only.mp4"
    subprocess.run(  # noqa: S603 - fixed local encoder and generated tone
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "12",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(media),
        ],
        check=True,
        timeout=30,
    )
    _serve_media(page, media.read_bytes(), "video/mp4")
    player = _open(page, server.url, "movtext-subtitles.mp4")
    assert player.evaluate("el=>el.player.hasPicture") is False
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>1")
    expect(page.locator(".advanced-state")).to_have_count(0)
    player.evaluate(
        "async el=>{await el.pause();await el.player.seek(6);await el.play()}"
    )
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>6.5")
    expect(page.locator(".advanced-state")).to_have_count(0)


def test_container_track_titles_stay_text_without_creating_requests(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path
) -> None:
    _, page = desktop
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is needed to remux track metadata")
    title = "<img src=/audit-probe onerror=window.__auditInjected=1>"
    media = tmp_path / "metadata.mkv"
    subprocess.run(  # noqa: S603 - fixed fixture with deliberately hostile metadata
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(ROOT / "tests/fixtures/multitrack.mkv"),
            "-map",
            "0",
            "-c",
            "copy",
            "-metadata:s:a:0",
            "title=" + title,
            "-metadata:s:s:0",
            "title=" + title,
            str(media),
        ],
        check=True,
        timeout=30,
    )
    requested: list[str] = []
    page.on("request", lambda request: requested.append(request.url))
    page.route("**/audit-probe", lambda route: route.fulfill(status=204))
    _serve_media(page, media.read_bytes())
    player = _open(page, server.url, "multitrack.mkv")
    page.wait_for_function("""()=>document.querySelector('movi-player').player
      .getAudioTracks().some(track=>track.label?.startsWith('<img'))""")
    assert player.evaluate(
        "el=>el.shadowRoot.textContent.includes('<img src=/audit-probe')"
    )
    expect(
        player.locator(".movi-audio-track-label img,.movi-subtitle-track-label img")
    ).to_have_count(0)
    assert not any("/audit-probe" in url for url in requested)
    assert page.evaluate("window.__auditInjected") is None


def test_repeated_playing_close_releases_global_handlers_and_ui_timers(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    for _ in range(3):
        page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
        player = page.locator("movi-player")
        page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
        player.locator("button.movi-play-pause").click()
        page.wait_for_function(
            "()=>document.querySelector('movi-player').currentTime>0.2"
        )
        player.evaluate("el=>window.auditClosedPlayer=el")
        page.get_by_role("button", name="Close Preview", exact=True).click()
        page.wait_for_function("()=>!document.querySelector('movi-player')")
        assert page.evaluate("""()=>{
          const p=window.auditClosedPlayer;
          return !p.player && p._tgGlobalsDetached===true &&
            p._stutterInterval===null && p._qoeHeartbeat===null &&
            p._stutterCooldownTimer===null;
        }""")
        assert page.evaluate("navigator.mediaSession.playbackState") == "none"
    page.evaluate("delete window.auditClosedPlayer")
