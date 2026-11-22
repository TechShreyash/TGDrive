"""Integrated chapters and truthful decoded-readiness regression coverage."""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import struct
import subprocess
from pathlib import Path
from statistics import median
from typing import Any

import pytest

from tests.browser.test_browser_e2e import ARTIFACTS, ROOT, Server, expect, login

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def _serve_media(
    page: Any, payload: bytes, content_type: str = "video/x-matroska"
) -> None:
    def serve(route: Any) -> None:
        request = route.request
        headers = {"Accept-Ranges": "bytes", "Content-Type": content_type}
        if request.method == "HEAD":
            headers["Content-Length"] = str(len(payload))
            route.fulfill(status=200, headers=headers, body=b"")
            return
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", request.headers.get("range", ""))
        if match:
            start = int(match[1])
            end = min(int(match[2]) if match[2] else len(payload) - 1, len(payload) - 1)
            headers["Content-Range"] = f"bytes {start}-{end}/{len(payload)}"
            route.fulfill(status=206, headers=headers, body=payload[start : end + 1])
        else:
            route.fulfill(status=200, headers=headers, body=payload)

    page.route("**/file?*", serve)


@pytest.mark.parametrize("clipboard", ["allowed", "blocked"])
def test_playback_diagnostics_copy_or_select_without_file_identity(
    server, desktop, clipboard
):
    _, page = desktop
    page.set_viewport_size(
        {"width": 390 if clipboard == "blocked" else 1536, "height": 844}
    )
    page.add_init_script(
        "window.blockDiagnosticsClipboard = "
        + ("true" if clipboard == "blocked" else "false")
    )
    page.add_init_script("""(() => {
      Object.defineProperty(navigator, "clipboard", {value: {
        writeText: text => {
          if (window.blockDiagnosticsClipboard) {
            return Promise.reject(new DOMException("Denied", "NotAllowedError"));
          }
          window.copiedDiagnostics = text;
          return Promise.resolve();
        }
      }});
    })()""")
    _serve_media(page, (ROOT / "tests/fixtures/multitrack.mkv").read_bytes())
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("() => document.querySelector('movi-player')?.duration > 0")
    player.locator(".movi-settings-btn").click()
    action = player.get_by_role("button", name="Copy playback diagnostics", exact=True)
    action.press("Enter")
    if clipboard == "blocked":
        dialog = page.get_by_role("dialog", name="Playback diagnostics", exact=True)
        field = dialog.get_by_role("textbox", name="Playback diagnostics", exact=True)
        expect(field).to_be_focused()
        report = field.input_value()
        assert field.evaluate("node => node.selectionEnd - node.selectionStart") == len(
            report
        )
        page.screenshot(path=ARTIFACTS / "playback-diagnostics-fallback.png")
        field.press("Escape")
        expect(dialog).to_have_count(0)
        expect(player).to_be_visible()
    else:
        expect(
            page.get_by_text("Playback diagnostics copied.", exact=True)
        ).to_be_visible()
        report = page.evaluate("window.copiedDiagnostics")
    for label in (
        "Build",
        "Mode",
        "Container",
        "Video codec",
        "Position",
        "Rate",
        "Buffered time ranges",
    ):
        assert label + ":" in report
    assert re.search(r"Build: [\d.]+ \([A-Za-z0-9_-]+\)", report)
    assert "Matroska" in report
    assert "multitrack.mkv" not in report and server.url not in report
    assert "auth=" not in report and "path=" not in report


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
    chapter = player.locator(".movi-chapter-pill")
    expect(chapter).to_be_visible(timeout=30000)
    assert chapter.evaluate("el=>!!el.closest('.movi-controls-bar')")
    expect(page.locator("details.advanced-chapters")).to_have_count(0)
    expect(player.locator(".advanced-chapters")).to_have_count(0)
    player.evaluate("""element => {
      element.__previewFrameReads = 0;
      const preview = element.player.getPreviewFrame.bind(element.player);
      const released = new Promise(resolve => {
        element.__releaseChapterPreviews = resolve;
      });
      element.player.getPreviewFrame = (...args) => {
        element.__previewFrameReads++;
        return released.then(() => preview(...args));
      };
    }""")
    assert player.evaluate("element => element.__previewFrameReads") == 0
    chapter.click()
    panel = player.get_by_role("region", name="Chapters", exact=True)
    expect(panel).to_be_visible()
    expect(panel.locator(".movi-timeline-noframe")).to_have_count(2)
    expect(panel.locator("img")).to_have_count(0)
    expect(
        panel.get_by_role("button", name="Second chapter 0:02", exact=True)
    ).to_be_visible()
    page.screenshot(path=ARTIFACTS / "chapters-integrated-desktop.png", full_page=False)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    expect(panel).to_be_visible()
    page.screenshot(path=ARTIFACTS / "chapters-integrated-mobile.png", full_page=False)
    player.evaluate("element => element.__releaseChapterPreviews()")
    panel.get_by_role("button", name="Second chapter 0:02", exact=True).click()
    expect(panel).to_be_visible()
    page.wait_for_function(
        "()=>document.querySelector('movi-player')?.currentTime>=1.9"
    )
    panel.locator(".movi-timeline-close").click()
    expect(panel).not_to_be_visible()
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
    # Actual HTTP delivery remains distinct from the deliberately held audio.
    expect(status).to_contain_text("received")
    assert status.bounding_box()["height"] < 120
    expect(status.locator("p")).to_have_count(0)
    assert player.evaluate("el=>el.player.getSource().getNetworkStats().totalBytes>0")
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


@pytest.mark.parametrize("early_wait", [False, True], ids=["watchdog", "waiting-event"])
def test_frozen_picture_holds_audio_and_recovers_on_real_frames(
    server: Server, desktop: tuple[Any, Any], early_wait: bool
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    player.locator("button.movi-play-pause").click()
    page.wait_for_function(
        """() => {
            const el = document.querySelector('movi-player');
            return el?.currentTime > 0.3 && el.player.videoRenderer.getQueueSize() > 0;
        }"""
    )
    player.evaluate(
        """(el, earlyWait)=>{
      // Withhold decoded pictures without intercepting the engine's refill recovery.
      const renderer=el.player.videoRenderer;
      // Keep one decoded picture pending for the early event. An empty renderer
      // after a full file read can legitimately be treated as an audio-only tail.
      if(earlyWait)renderer.stopPresentationLoop();
      const frames=renderer.frameQueue.splice(earlyWait ? 1 : 0);
      const queueFrame=renderer.queueFrame.bind(renderer);
      renderer.queueFrame=frame=>frames.push(frame);
      window.releaseVideoFrames=()=>{
        renderer.queueFrame=queueFrame;
        for(const frame of frames.splice(0))queueFrame(frame);
      };
      // Movi's quality totals include dropped frames and can reset on refill.
      window.presentedSinceFreeze=0;
      const presented=()=>{
        window.presentedSinceFreeze++;
        el.requestVideoFrameCallback(presented);
      };
      el.requestVideoFrameCallback(presented);
      if(earlyWait)el.dispatchEvent(new Event('waiting'));
    }""",
        early_wait,
    )
    status = page.locator(".advanced-state")
    expect(status).to_be_visible(timeout=4000)
    expect(status).to_contain_text("received")
    assert status.bounding_box()["height"] < 120
    expect(status.locator("p")).to_have_count(0)
    expect(status).not_to_contain_text("%")
    assert page.evaluate("window.presentedSinceFreeze") == 0
    playback_clocks = """el=>({
        state: el.player.getState(),
        time: el.currentTime,
        audioTime: el.player.audioRenderer.audioContext.currentTime,
        audioState: el.player.audioRenderer.audioContext.state
    })"""
    held = player.evaluate(playback_clocks)
    assert held["state"] == "buffering"
    page.wait_for_timeout(300)
    after_hold = player.evaluate(playback_clocks)
    assert after_hold["audioState"] == "suspended"
    assert player.evaluate("el=>!el.paused"), "Buffering must preserve play intent"
    assert abs(after_hold["time"] - held["time"]) < 0.1
    assert abs(after_hold["audioTime"] - held["audioTime"]) < 0.1
    expect(page.locator(".advanced-player-host")).to_have_attribute("aria-busy", "true")
    assert page.evaluate("window.presentedSinceFreeze") == 0
    # The existing refill path must restart presentation and sound together.
    page.evaluate("window.releaseVideoFrames()")
    expect(status).to_have_count(0, timeout=5000)
    assert page.evaluate("window.presentedSinceFreeze") > 0
    assert player.evaluate(playback_clocks)["audioState"] == "running"
    expect(page.locator(".advanced-player-host")).to_have_attribute(
        "aria-busy", "false"
    )


def test_sparse_video_frames_do_not_stall_playback(server, desktop, tmp_path):
    _, page = desktop
    media = tmp_path / "sparse.mp4"
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
            "testsrc2=size=160x90:rate=1/3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "9",
            "-c:v",
            "libx264",
            "-g",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(media),
        ],
        check=True,
    )
    _serve_media(page, media.read_bytes(), "video/mp4")
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
    page.get_by_role("button", name="movtext-subtitles.mp4", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>8")
    player.evaluate("""el=>{
        window.sparseVideoStates=[];
        window.sparseVideoBuffering=false;
        el.addEventListener('statechange',event=>{
            if (event.detail === 'playing' || window.sparseVideoStates.length)
                window.sparseVideoStates.push(event.detail);
        });
        new MutationObserver(()=>{
            if (window.sparseVideoStates.length &&
                document.querySelector('.advanced-state[data-phase="buffering"]'))
                window.sparseVideoBuffering=true;
        }).observe(document.body, {
            childList:true,subtree:true,attributeFilter:['data-phase']
        });
    }""")
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>1.5")
    expect(page.locator('.advanced-state[data-phase="seeking"]')).to_have_count(
        0, timeout=500
    )
    page.wait_for_function(
        """()=>{const el=document.querySelector('movi-player');
        return el.currentTime>6.2 &&
            el.getVideoPlaybackQuality().totalVideoFrames>=3}""",
        timeout=11000,
    )
    expect(page.locator(".advanced-state")).to_have_count(0)
    assert "seeking" not in player.evaluate("()=>window.sparseVideoStates")
    assert not player.evaluate("()=>window.sparseVideoBuffering")


def test_pitch_preserving_speed_tracks_presented_picture(server, desktop, tmp_path):
    _, page = desktop
    media = tmp_path / "sync-cues.mp4"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    # Six tones align with matching picture flashes at source seconds 1 to 6.
    cue = "gte(t,1)*lt(t,7)*lt(mod(t,1),0.16)"
    subprocess.run(  # noqa: S603 - fixed self-generated local media
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=30:d=8",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc=exprs='if({cue},0.30*sin(2*PI*997*t),0)':s=48000:d=8",
            "-vf",
            f"drawbox=color=white:t=fill:enable='{cue}'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(media),
        ],
        check=True,
    )
    _serve_media(page, media.read_bytes(), "video/mp4")
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    page.get_by_role("button", name="movtext-subtitles.mp4", exact=True).press("Enter")
    player = page.locator("movi-player")
    page.wait_for_function("() => document.querySelector('movi-player')?.duration > 7")
    player.evaluate("""element => {
        element.playbackRate = 1.5;
        const probe = window.cueProbe = { pictures: [], tones: [], ended: false };
        function frame(time, metadata) {
            probe.pictures.push({ time, mediaTime: metadata.mediaTime });
            if (!probe.ended) element.requestVideoFrameCallback(frame);
        }
        element.requestVideoFrameCallback(frame);
        let samples;
        function sample(time) {
            if (probe.ended) return;
            if (!element.paused) {
                element.player.getAudioLevels(12);
                const analyser = element.player.audioRenderer?._analyser;
                if (analyser) {
                    samples ??= new Float32Array(analyser.fftSize);
                    analyser.getFloatTimeDomainData(samples);
                    const rms = Math.sqrt(samples.reduce(
                        (total, sample) => total + sample ** 2, 0) / samples.length);
                    if (rms > 0.035 && time - (probe.tones.at(-1) ?? -Infinity) > 420)
                        probe.tones.push(time);
                }
            }
            requestAnimationFrame(sample);
        }
        requestAnimationFrame(sample);
        element.addEventListener("ended", () => { probe.ended = true; },
            { once: true });
    }""")
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("() => window.cueProbe.ended", timeout=20000)
    probe = page.evaluate("() => window.cueProbe")
    assert len(probe["tones"]) == 6
    errors = []
    for cue_time, tone_time in enumerate(probe["tones"], start=1):
        presented = min(
            probe["pictures"], key=lambda picture: abs(picture["time"] - tone_time)
        )
        errors.append(abs(presented["mediaTime"] - cue_time))
    assert median(errors) < 0.08, errors


@pytest.mark.parametrize("playing", [False, True])
def test_hevc_capability_never_replaces_the_custom_player(
    server: Server, desktop: tuple[Any, Any], playing: bool
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    if playing:
        player.locator("button.movi-play-pause").click()
        page.wait_for_function(
            "()=>document.querySelector('movi-player').currentTime>0.25"
        )
    player.evaluate("""el=>{
      window.originalCustomPlayer=el;
      HTMLMediaElement.prototype.canPlayType=()=>"probably";
      el.player.getMediaInfo=()=>({formatName:'mov,mp4',tracks:[{
        type:'video',codecString:'hvc1.1.6.L150.b0'
      }]});
      el.dispatchEvent(new Event('canplay'));
      el.dispatchEvent(new Event('progress'));
    }""")
    # Exercise observer ticks as well as readiness; native support cannot opt out
    # of the custom demux/decoder/render pipeline or replace its feature state.
    page.wait_for_timeout(1500)
    assert player.evaluate("el=>el===window.originalCustomPlayer&&el.engine==='wasm'")
    assert player.evaluate("el=>el.paused") is not playing
    expect(player.locator("button.movi-play-pause")).to_have_count(1)


def test_http_window_is_sized_before_loading_and_keeps_received_bytes(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    page.add_init_script("Object.defineProperty(navigator,'deviceMemory',{get:()=>8})")
    original = (ROOT / "tests/fixtures/movtext-subtitles.mp4").read_bytes()
    padding = 40 * 1024**2 - len(original)
    payload = original + struct.pack(">I4s", padding, b"free") + bytes(padding - 8)
    _serve_media(page, payload, "video/mp4")
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="movtext-subtitles.mp4", exact=True).dblclick()
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>5")
    page.wait_for_function("""()=>{
      const s=document.querySelector('movi-player')?.player?.getSource();
      return s?.bufferSize===40*1024**2 && s.isFullyCached();
    }""")
    result = page.locator("movi-player").evaluate("""async el=>{
      const s=el.player.getSource(),bytes=s.dataView||s.fallbackBuffer;
      const hash=await crypto.subtle.digest('SHA-256',bytes.slice(0,s.getKnownSize()));
      return {size:s.bufferSize,start:s.getBufferedStart(),
        head:Array.from(bytes.slice(0,64)),
        digest:Array.from(new Uint8Array(hash),
          b=>b.toString(16).padStart(2,'0')).join('')
      }
    }""")
    assert result["size"] == len(payload)
    assert result["start"] == 0
    assert result["head"] == list(payload[:64])
    assert result["digest"] == hashlib.sha256(payload).hexdigest()


def test_time_buffer_survives_network_loss_and_ten_second_skips(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path
) -> None:
    _, page = desktop
    media = tmp_path / "long-video.mp4"
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
            "-t",
            "180",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-g",
            "48",
            "-movflags",
            "+faststart",
            str(media),
        ],
        check=True,
    )
    payload = media.read_bytes()
    _serve_media(page, payload, "video/mp4")
    login(page, server.url)
    # Isolate playback read-ahead from the separate representative-poster seek.
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
    page.get_by_role("button", name="movtext-subtitles.mp4", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function(
        "()=>document.querySelector('movi-player')?.duration>170", timeout=30000
    )
    assert player.get_attribute("postertime") is None
    player.locator("button.movi-play-pause").click()
    page.wait_for_function(
        """()=>{
      const p=document.querySelector('movi-player')?.player,s=p?.getSource();
      return s?.playbackPrefetchHeld && p.getBufferEndTime()>65;
    }""",
        timeout=30000,
    )
    coverage = player.evaluate("""el=>({
      time:el.currentTime,end:el.player.getBufferEndTime(),
      queued:el.player.videoRenderer.getQueueSize(),
      bytes:el.player.getSource().getNetworkStats().totalBytes,
      goal:el.player.getSource().playbackAheadBytes
    })""")
    assert coverage["end"] - coverage["time"] > 60
    assert coverage["queued"] < 150  # Source buffering must not decode 90s of frames.
    assert coverage["bytes"] < len(payload)
    failed_requests: list[str] = []

    def offline(route: Any) -> None:
        failed_requests.append(route.request.url)
        route.abort("internetdisconnected")

    page.route("**/file?*", offline)
    # Two forward skips use the prefetched compressed bytes during an outage.
    player.evaluate("el=>{el.currentTime=10}")
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>10.2")
    player.evaluate("el=>{el.currentTime=20}")
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>20.2")
    assert failed_requests == []
    expect(page.get_by_role("alert")).to_have_count(0)


def test_video_only_backpressure_does_not_search_ahead_for_nonexistent_audio(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path
) -> None:
    _, page = desktop
    media = tmp_path / "video-only.mkv"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(  # noqa: S603 - fixed self-generated media fixture
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
            str(media),
        ],
        check=True,
        timeout=30,
    )
    _serve_media(page, media.read_bytes())
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>0.25")
    player.evaluate("""el=>{
      const p=el.player,d=p.videoDecoder,r=p.videoRenderer,m=p.demuxer;
      if(p.trackManager.getActiveAudioTrack())throw Error('Fixture must be video-only');
      const read=m.readPacket.bind(m),size=r.getQueueSize.bind(r);
      window.pressuredReads=0;
      const eof=p.eofReached,stash=p._videoAheadStash,bytes=p._videoAheadStashBytes;
      const pending=p.pendingPrebufferPackets;
      p.eofReached=false;p._videoAheadStash=[];p._videoAheadStashBytes=0;
      p.pendingPrebufferPackets=[];
      m.readPacket=async()=>{window.pressuredReads++;return {
        streamIndex:p.trackManager.getActiveVideoTrack().id,
        timestamp:el.currentTime+5,dts:el.currentTime+5,duration:1/30,
        data:new Uint8Array([0]),keyframe:false,isIdr:false
      }};
      Object.defineProperty(d,'queueSize',{configurable:true,get:()=>160});
      r.getQueueSize=()=>100;
      window.releasePressure=()=>{
        delete d.queueSize;r.getQueueSize=size;m.readPacket=read;
        p.eofReached=eof;p._videoAheadStash=stash;p._videoAheadStashBytes=bytes;
        p.pendingPrebufferPackets=pending};
    }""")
    page.wait_for_timeout(500)
    assert page.evaluate("window.pressuredReads") <= 1
    player.evaluate("()=>window.releasePressure()")
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>0.9")


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


@pytest.mark.parametrize("network", ["growing", "stalled", "unavailable"])
def test_retry_tracks_delivery_stalls_and_bounds_decode_waits(server, desktop, network):
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    player.evaluate("el=>el.pause()")
    page.wait_for_function("()=>document.querySelector('movi-player').paused")
    expect(page.locator(".advanced-player-host")).to_have_attribute(
        "aria-busy", "false"
    )
    page.clock.install()
    player.evaluate(
        """(el,network)=>{
      const core=el.player, source=core.getSource(), began=performance.now();
      core.pictureRunwaySeconds=()=>0;
      source.getNetworkStats=()=>network==='unavailable'?{}:{
        totalBytes:1000+(network==='growing'?performance.now()-began:0),
        currentSpeed:network==='growing'?1000:0
      };
      el.dispatchEvent(new Event('seeking'));
    }""",
        network,
    )
    page.clock.run_for(17000)
    retry = page.get_by_role("button", name="Try again", exact=True)
    if network == "growing":
        expect(retry).to_have_count(0)
        page.clock.run_for(46000)
    expect(retry).to_be_visible()
    # Another seek gets its own wait even when the phase was already seeking.
    player.evaluate("el=>el.dispatchEvent(new Event('seeking'))")
    page.clock.run_for(1000)
    expect(retry).to_have_count(0)
    player.evaluate("el=>el.dispatchEvent(new Event('error'))")
    expect(retry).to_be_visible()
