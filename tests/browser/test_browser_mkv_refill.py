"""Integration coverage for enhanced MKV playback across a real range wait."""

import base64
import re
import shutil
import subprocess

import pytest

from tests.browser.test_browser_e2e import expect, login
from tests.browser.test_browser_player_controls import _serve_media

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


@pytest.mark.parametrize("refill_timing", ["delayed-pause", "stale-wait", "user-pause"])
def test_mkv_range_refill_keeps_wait_visible_until_frames_present(
    server, desktop, tmp_path, refill_timing
):
    _, page = desktop
    media = tmp_path / "range-refill.mkv"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(  # noqa: S603 - fixed self-generated media fixture
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "14",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-b:v",
            "10M",
            "-minrate",
            "10M",
            "-maxrate",
            "10M",
            "-bufsize",
            "10M",
            "-x264-params",
            "nal-hrd=cbr",
            "-g",
            "24",
            "-c:a",
            "eac3",
            "-cluster_time_limit",
            "1000",
            "-cues_to_front",
            "1",
            str(media),
        ],
        check=True,
        timeout=30,
    )
    _serve_media(page, media.read_bytes())
    held = []
    blocked = True

    def hold_after_first_range(route):
        match = re.match(r"bytes=(\d+)-", route.request.headers.get("range", ""))
        if blocked and match and int(match[1]) >= 4 * 1024 * 1024:
            held.append(route)
        else:
            route.fallback()

    page.route("**/file?*", hold_after_first_range)
    login(page, server.url)
    poster = page.evaluate(
        "()=>document.createElement('canvas').toDataURL('image/jpeg').split(',')[1]"
    )
    page.route(
        "**/api/v1/poster?*",
        lambda route: route.fulfill(
            body=base64.b64decode(poster), content_type="image/jpeg"
        ),
    )
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>13")
    player = page.locator("movi-player")
    if refill_timing == "stale-wait":
        player.evaluate("el=>{el.playbackRate=1.5}")
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("()=>document.querySelector('movi-player')?.currentTime>0.4")
    status = page.locator('.advanced-state[data-phase="buffering"]')
    expect(status).to_be_visible(timeout=12000)
    assert held, "Playback must be waiting on an actual HTTP range"

    if refill_timing == "user-pause":
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        player.locator(".movi-settings-btn").click()
        player.get_by_role(
            "button", name="Copy playback diagnostics", exact=True
        ).click()
        expect(
            page.get_by_text("Playback diagnostics copied.", exact=True)
        ).to_be_visible()
        report = page.evaluate("navigator.clipboard.readText()")
        assert "Last buffering transition: entered at" in report
        assert "Audio context: suspended" in report
        player.locator("button.movi-play-pause").click()
        expect(player).to_have_js_property("paused", True)
        paused = player.evaluate("el => el.currentTime")
        blocked = False
        for route in held:
            route.fallback()
        page.wait_for_timeout(750)
        assert abs(player.evaluate("el => el.currentTime") - paused) < 0.1
        expect(player).to_have_js_property("paused", True)
        assert (
            player.evaluate("el => el.player.audioRenderer.audioContext.state")
            == "suspended"
        )
        player.locator("button.movi-play-pause").click()
        page.wait_for_function(
            "time => document.querySelector('movi-player')?.currentTime > time + 0.5",
            arg=paused,
            timeout=12000,
        )
        return

    player.evaluate(
        """(el, timing)=>{
      const core=el.player;
      let presented=0;
      const frame=()=>{presented++;el.requestVideoFrameCallback(frame)};
      el.requestVideoFrameCallback(frame);
      const observer=new MutationObserver(()=>{
        if(document.querySelector('.advanced-state'))return;
        window.framesWhenWaitCleared=presented;
        observer.disconnect();
      });
      observer.observe(document.body,{childList:true,subtree:true});

      if(timing==="delayed-pause"){
        const audio=core.audioRenderer;
        const play=audio.play.bind(audio);
        audio.play=async(...args)=>{
          // Give the compatibility pause event time to arrive during the refill.
          await new Promise(resolve=>setTimeout(resolve,240));
          return play(...args);
        };
        return;
      }

      window.refillStates=[];
      el.addEventListener("statechange",event=>{
        if(!presented)window.refillStates.push(event.detail);
      });

      // Let exactly one wait scheduled during the network stall fall due immediately
      // after the real renderer restart. Respect cancellation of the original timer.
      const schedule=window.setTimeout;
      const cancel=window.clearTimeout;
      let stale;
      window.setTimeout=(callback,delay,...args)=>{
        if(delay!==700 || core.getState()!=="buffering")
          return schedule(callback,delay,...args);
        window.setTimeout=schedule;
        const run=()=>callback(...args);
        stale={id:schedule(run,15000),run};
        window.staleWaitArmed=true;
        return stale.id;
      };
      window.clearTimeout=id=>{
        if(id===stale?.id){
          stale=undefined;
          window.clearTimeout=cancel;
        }
        return cancel(id);
      };
      const renderer=core.videoRenderer;
      const start=renderer.startPresentationLoop;
      renderer.startPresentationLoop=function(...args){
        renderer.startPresentationLoop=start;
        schedule(()=>{
          if(stale){
            const {id,run}=stale;
            cancel(id);
            window.clearTimeout=cancel;
            run();
          }
          if(core.getState()==="playing")start.apply(renderer,args);
        },180);
      };
    }""",
        refill_timing,
    )
    if refill_timing == "stale-wait":
        page.wait_for_function("()=>window.staleWaitArmed")
    blocked = False
    for route in held:
        route.fallback()
    expect(page.locator(".advanced-state")).to_have_count(0, timeout=12000)
    assert page.evaluate("window.framesWhenWaitCleared") > 0
    if refill_timing == "stale-wait":
        states = page.evaluate("window.refillStates")
        assert "playing" in states
        assert "buffering" not in states[states.index("playing") + 1 :], states
