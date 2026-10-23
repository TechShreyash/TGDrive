"""Integrated chapters and truthful decoded-readiness regression coverage."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_browser_e2e import ARTIFACTS, ROOT, Server, expect, login

pytestmark = pytest.mark.e2e
pytest_plugins = ("test_browser_e2e",)


def _serve_media(page: Any, payload: bytes) -> None:
    def serve(route: Any) -> None:
        request = route.request
        headers = {"Accept-Ranges": "bytes", "Content-Type": "video/x-matroska"}
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", request.headers.get("range", ""))
        if match:
            start = int(match[1])
            end = min(int(match[2]) if match[2] else len(payload) - 1, len(payload) - 1)
            headers["Content-Range"] = f"bytes {start}-{end}/{len(payload)}"
            route.fulfill(status=206, headers=headers, body=payload[start : end + 1])
        else:
            route.fulfill(status=200, headers=headers, body=payload)

    page.route("**/file?*", serve)


def test_chapters_belong_to_autohiding_player_controls(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path
) -> None:
    _, page = desktop
    metadata = tmp_path / "chapters.txt"
    metadata.write_text(
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\n"
        "title=Opening\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=2000\nEND=5000\n"
        "title=Second chapter\n"
    )
    output = tmp_path / "chapters.mkv"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "The media browser tests require FFmpeg"
    subprocess.run(  # noqa: S603 - fixed self-generated local media
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(ROOT / "tests/fixtures/multitrack.mkv"),
            "-i",
            str(metadata),
            "-map",
            "0",
            "-map_metadata",
            "1",
            "-c",
            "copy",
            str(output),
        ],
        check=True,
    )
    payload = output.read_bytes()

    _serve_media(page, payload)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    player = page.locator("movi-player")
    chapter = player.locator('button[data-custom-control="tgdrive-chapters"]')
    expect(chapter).to_be_visible(timeout=30000)
    assert chapter.evaluate("el=>!!el.closest('.movi-controls-bar')")
    expect(page.locator("details.advanced-chapters")).to_have_count(0)
    expect(player.locator(".advanced-chapters")).to_have_count(0)
    chapter.click()
    panel = player.get_by_role("region", name="Video chapters", exact=True)
    expect(panel).to_be_visible()
    expect(
        panel.get_by_role("button", name="Second chapter 0:02", exact=True)
    ).to_be_visible()
    page.screenshot(path=ARTIFACTS / "chapters-integrated-desktop.png", full_page=False)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    expect(panel).to_be_visible()
    page.screenshot(path=ARTIFACTS / "chapters-integrated-mobile.png", full_page=False)
    panel.get_by_role("button", name="Second chapter 0:02", exact=True).click()
    expect(panel).to_have_count(0)
    page.wait_for_function(
        "()=>document.querySelector('movi-player')?.currentTime>=1.9"
    )
    page.set_viewport_size({"width": 1440, "height": 960})
    player.hover()
    player.locator("button.movi-play-pause").click()
    page.mouse.move(0, 0)
    page.wait_for_function("""() => document.querySelector('movi-player')?.shadowRoot
      .querySelector('.movi-controls-container')?.classList.contains('movi-controls-hidden')""")
    assert chapter.evaluate("el=>!!el.closest('.movi-controls-hidden')")


@pytest.mark.parametrize("output_latency", [0.0, 0.5])
def test_seek_waits_for_delayed_decoded_audio_without_fake_buffer_percentage(
    server: Server, desktop: tuple[Any, Any], output_latency: float
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    player.locator("button.movi-play-pause").click()
    page.wait_for_function(
        "()=>document.querySelector('movi-player')?.currentTime>0.25"
    )
    player.evaluate(
        """(el,latency)=>{
      // Hold real software-decoded PCM briefly: the video source is already loaded.
      const renderer=el.player.audioRenderer, render=renderer.renderPCM.bind(renderer);
      if(latency)renderer.expectedStartLead=()=>latency;
      let holding=true, pending=[];
      renderer.renderPCM=frame=>holding
        ?pending.push({...frame,planes:frame.planes.map(plane=>plane.slice())})
        :render(frame);
      window.delayedAudio={samples:[],events:[]};
      window.delayedAudio.timer=setInterval(()=>window.delayedAudio.samples.push({
        time:el.currentTime,audio:renderer.getBufferedDuration(),
        state:el.player.getState(),frames:el.player.videoRenderer.getQueueSize()
      }),50);
      el.addEventListener('seeking',()=>window.delayedAudio.events.push('seeking'));
      setTimeout(()=>{
        holding=false;for(const frame of pending)render(frame);pending=[];
      },1800);
      el.currentTime=2;
    }""",
        output_latency,
    )
    status = page.locator(".advanced-state")
    expect(status).to_be_visible(timeout=1500)
    expect(status).not_to_contain_text("% buffered")
    expect(status).not_to_contain_text("s ahead")
    indicator = status.get_by_role("progressbar", name="Loading video")
    expect(indicator).to_be_visible()
    assert indicator.get_attribute("value") is None
    expect(status).not_to_contain_text("received")
    expect(status).not_to_contain_text("/s")
    expect(status).to_contain_text("Loading this part…")
    # Wait feedback never intercepts the controls or offers an early reload.
    expect(status.get_by_role("button", name="Try again")).to_have_count(0)
    page.screenshot(path=ARTIFACTS / "seek-waiting-for-real-audio.png", full_page=False)
    page.wait_for_function(
        "()=>document.querySelector('movi-player')?.currentTime>2.15"
    )
    expect(status).to_have_count(0)
    result = page.evaluate(
        "clearInterval(window.delayedAudio.timer);window.delayedAudio"
    )
    blocked = [
        sample
        for sample in result["samples"]
        if sample["audio"] == 0 and sample["frames"] > 0
    ]
    assert len(blocked) >= 10
    assert all(abs(sample["time"] - 2) < 0.05 for sample in blocked)
    assert result["events"] == ["seeking"], (
        "Audio refill must not re-seek to the old target"
    )


@pytest.mark.parametrize("mode", ["video-only", "muted", "audio-off", "dropping-audio"])
def test_seek_audio_exceptions_do_not_deadlock(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path, mode: str
) -> None:
    _, page = desktop
    if mode == "video-only":
        ffmpeg = shutil.which("ffmpeg")
        assert ffmpeg
        output = tmp_path / "video-only.mkv"
        subprocess.run(  # noqa: S603 - self-generated local fixture
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(ROOT / "tests/fixtures/multitrack.mkv"),
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-an",
                str(output),
            ],
            check=True,
        )
        _serve_media(page, output.read_bytes())
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    player.locator("button.movi-play-pause").click()
    page.wait_for_function(
        "()=>document.querySelector('movi-player')?.currentTime>0.25"
    )
    player.evaluate(
        """(el,mode)=>{
      el.player.audioRenderer.expectedStartLead=()=>0.5;
      if(mode==='muted'||mode==='dropping-audio')el.muted=true;
      if(mode==='audio-off')el.player.disableAudio=true;
      if(mode==='dropping-audio')el.player.audioRenderer.isDroppingAudio=()=>true;
      window.exceptionSeeks=0;el.addEventListener('seeking',()=>window.exceptionSeeks++);
      el.currentTime=2;
    }""",
        mode,
    )
    page.wait_for_function("()=>document.querySelector('movi-player')?.currentTime>2.2")
    expect(page.locator(".advanced-state")).to_have_count(0)
    assert page.evaluate("window.exceptionSeeks") == 1


@pytest.mark.parametrize("failure", ["reconnecting", "error"])
def test_playback_recovers_without_a_stale_error_panel(
    server: Server, desktop: tuple[Any, Any], failure: str
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    player.evaluate(
        """(el,failure)=>{
          if(failure==='reconnecting')el.dispatchEvent(new CustomEvent('errordisplay',{
            detail:{title:'Reconnecting…',message:'Retrying automatically…'}
          }));
          else el.dispatchEvent(new Event('error'));
        }""",
        failure,
    )
    if failure == "error":
        expect(page.get_by_role("alert")).to_contain_text("The video could not play")
    else:
        expect(page.get_by_role("alert")).to_have_count(0)
    # Genuine playback events/frames clear a temporary failure automatically.
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("()=>document.querySelector('movi-player')?.currentTime>0.3")
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(page.locator(".advanced-state")).to_have_count(0)


def test_try_again_restores_position_without_starting_a_paused_video(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    player.evaluate("el=>{window.originalPlayer=el;el.currentTime=1.5}")
    page.wait_for_function(
        "()=>Math.abs(document.querySelector('movi-player').currentTime-1.5)<0.1"
        "&&!document.querySelector('movi-player').seeking"
    )
    player.evaluate("el=>el.dispatchEvent(new Event('error'))")
    page.get_by_role("button", name="Try again", exact=True).click()
    page.wait_for_function(
        "()=>{const el=document.querySelector('movi-player');"
        "return el&&el!==window.originalPlayer&&el.duration>4"
        "&&Math.abs(el.currentTime-1.5)<0.1&&!el.seeking}"
    )
    assert player.evaluate("el=>el.paused") is True
    expect(page.get_by_role("alert")).to_have_count(0)
