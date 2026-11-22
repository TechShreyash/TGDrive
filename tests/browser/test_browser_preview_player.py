"""Video preview chrome stays consistent with the real playback state."""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    ROOT,
    Server,
    expect,
    login,
    preview_dialog,
)
from tests.browser.test_browser_player_controls import _serve_media

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def _seed_position(
    page: Any, server: Server, item_id: str, position_seconds: float = 1.5
) -> str:
    url = server.url + f"/api/v1/files/{item_id}/video-progress"
    begin = page.request.post(url + "/session", data={})
    assert begin.ok, begin.text()
    saved = page.request.put(
        url,
        data={
            "session_id": begin.json()["session_id"],
            "sequence": 1,
            "position_seconds": position_seconds,
            "duration_seconds": 5.0,
            "final": True,
        },
    )
    assert saved.ok, saved.text()
    return url


@pytest.mark.parametrize("name", ["movtext-subtitles.mp4", "software-mpeg4.wmv"])
def test_default_player_paints_a_new_picture_during_paused_seek(server, desktop, name):
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.route("**/api/v1/poster?*", lambda route: route.fulfill(status=204))
    page.get_by_role("button", name=name, exact=True).dblclick()
    player = preview_dialog(page).locator("movi-player")
    expect(player).to_have_attribute("engine", "wasm")
    page.evaluate("""() => {
        window.pausedPicture = () => {
            const player = document.querySelector("movi-player");
            const source = player?.getCanvas();
            if (!source?.width || !source.height || !player.paused || player.seeking)
                return null;
            const sample = document.createElement("canvas");
            sample.width = sample.height = 16;
            const context = sample.getContext("2d");
            context.drawImage(source, 0, 0, 16, 16);
            const pixels = context.getImageData(0, 0, 16, 16).data;
            return pixels.some((value, index) => index % 4 !== 3 && value > 32)
                ? sample.toDataURL() : null;
        };
    }""")
    before = page.wait_for_function(
        "() => window.pausedPicture()", timeout=30000
    ).json_value()
    target = player.evaluate("element => element.duration * 0.75")
    player.evaluate(
        "async (element, time) => { await element.player.seek(time); }", target
    )
    page.wait_for_function(
        """({before, target}) => {
            const player = document.querySelector("movi-player");
            const picture = window.pausedPicture();
            return picture && picture !== before &&
                Math.abs(player.currentTime - target) < 0.15;
        }""",
        arg={"before": before, "target": target},
        timeout=15000,
    )
    assert player.evaluate("element => element.paused")


@pytest.mark.parametrize(
    "width,height,fullscreen", [(1536, 695, False), (390, 844, True)]
)
def test_latest_paused_seek_paints_its_own_frame_and_remains_silent(
    server, desktop, tmp_path, width, height, fullscreen
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    media = tmp_path / "marked-frames.mp4"
    subprocess.run(  # noqa: S603 - fixed self-generated local media
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=160x90:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=green:s=160x90:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=160x90:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[marked]",
            "-map",
            "[marked]",
            "-map",
            "3:a",
            "-c:v",
            "libx264",
            "-g",
            "12",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "faststart",
            str(media),
        ],
        check=True,
        timeout=30,
    )
    _serve_media(page, media.read_bytes(), "video/mp4")
    page.route("**/api/v1/poster?*", lambda route: route.fulfill(status=204))
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="movtext-subtitles.mp4", exact=True).dblclick()
    player = preview_dialog(page).locator("movi-player")
    page.wait_for_function("""() => {
        const el = document.querySelector("movi-player");
        return el?.duration > 2.9 && el.getCanvas()?.width && el.paused && !el.seeking;
    }""")
    if fullscreen:
        preview_dialog(page).get_by_role(
            "button", name="Enter Fullscreen", exact=True
        ).click()
        page.wait_for_function("() => Boolean(document.fullscreenElement)")
        expect(player).to_have_js_property("paused", True)
    player.evaluate("""el => {
        const core = el.player;
        const decoder = core.videoDecoder;
        const flush = decoder.flush.bind(decoder);
        const hold = new Promise(resolve => { window.finishOlderSeek = resolve; });
        decoder.flush = async (...args) => {
            decoder.flush = flush;
            await flush(...args);
            window.olderSeekHeld = true;
            await hold;
        };
        const audio = core.audioRenderer;
        const context = audio.audioContext;
        const analyser = context.createAnalyser();
        const samples = new Float32Array(analyser.fftSize);
        audio.gainNode.connect(analyser);
        window.pausedSoundPeak = 0;
        const verifySilence = () => {
            if (context.state === "running") {
                analyser.getFloatTimeDomainData(samples);
                for (const sample of samples)
                    window.pausedSoundPeak = Math.max(
                        window.pausedSoundPeak, Math.abs(sample)
                    );
            }
            requestAnimationFrame(verifySilence);
        };
        requestAnimationFrame(verifySilence);
        window.markedFrame = () => {
            const canvas = document.createElement("canvas");
            canvas.width = canvas.height = 1;
            const context = canvas.getContext("2d");
            context.drawImage(el.getCanvas(), 0, 0, 1, 1);
            const [r, g, b] = context.getImageData(0, 0, 1, 1).data;
            return {
                r, g, b, time: el.currentTime, paused: el.paused, seeking: el.seeking,
            };
        };
        window.firstSeek = core.seek(0.35);
    }""")
    page.wait_for_function("() => window.olderSeekHeld")
    player.evaluate("""async el => {
        const middle = el.player.seek(1.35);
        await new Promise(resolve => setTimeout(resolve, 25));
        const latest = el.player.seek(2.35);
        await Promise.all([middle, latest]);
    }""")
    latest = page.wait_for_function(
        """() => {
        const f = window.markedFrame();
        return f.paused && !f.seeking && Math.abs(f.time - 2.35) < 0.13 &&
            f.b > f.r + 80 && f.b > f.g + 80 && f;
    }""",
        timeout=15000,
    ).json_value()
    page.evaluate("async () => { window.finishOlderSeek(); await window.firstSeek; }")
    # Give a late flush or presentation callback time to update the actual canvas.
    page.wait_for_timeout(400)
    assert page.evaluate("window.markedFrame()") == latest
    assert page.evaluate("window.pausedSoundPeak") < 0.003
    page.screenshot(
        path=ARTIFACTS / f"latest-paused-seek-{width}.png", animations="disabled"
    )


def test_video_stored_poster_is_sharp_and_released_when_preview_closes(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    image = page.evaluate("""()=>{
        const canvas=document.createElement('canvas');
        canvas.width=1600; canvas.height=900;
        const context=canvas.getContext('2d');
        context.fillStyle='#1c74bb'; context.fillRect(0,0,1600,900);
        return canvas.toDataURL('image/jpeg').split(',')[1];
    }""")
    page.route(
        "**/api/v1/poster?*",
        lambda route: route.fulfill(
            body=base64.b64decode(image), content_type="image/jpeg"
        ),
    )
    page.evaluate("""()=>{
        window.releasedVideoPosters=[];
        const revoke=URL.revokeObjectURL.bind(URL);
        URL.revokeObjectURL=url=>{window.releasedVideoPosters.push(url);revoke(url)};
    }""")
    page.get_by_role("button", name="movtext-subtitles.mp4", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    poster = player.locator(".movi-poster-overlay")
    expect(poster).to_be_visible()
    expect(poster).to_have_js_property("naturalWidth", 1600)
    assert poster.evaluate("image=>getComputedStyle(image).objectFit") == "contain"
    assert player.evaluate("element=>element.paused&&element.currentTime<0.1")
    assert player.get_attribute("postertime") is None
    source = poster.get_attribute("src")
    assert source and source.startswith("blob:")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    player.evaluate("""element=>{
        window.storedPosterBeforeRetry=element;
        element.dispatchEvent(new Event('error'));
    }""")
    dialog.get_by_role("button", name="Try again", exact=True).click()
    page.wait_for_function("""()=>{
        const player=document.querySelector('movi-player');
        return player&&player!==window.storedPosterBeforeRetry&&player.duration>4;
    }""")
    expect(poster).to_be_visible()
    expect(poster).to_have_js_property("naturalWidth", 1600)
    assert player.evaluate("element=>element.paused&&element.currentTime<0.1")
    assert page.evaluate("url=>window.releasedVideoPosters.includes(url)", source)
    source = poster.get_attribute("src")
    assert source and source.startswith("blob:")
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("()=>document.querySelector('movi-player')?.currentTime>0.5")
    expect(poster).to_be_hidden()
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    page.wait_for_function("url=>window.releasedVideoPosters.includes(url)", arg=source)


def test_video_missing_poster_decodes_representative_frame_without_watching(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.route("**/api/v1/poster?*", lambda route: route.fulfill(status=204))
    file = page.get_by_role("button", name="movtext-subtitles.mp4", exact=True)
    row = page.locator(".file-row").filter(has=file)
    item_id = row.get_attribute("data-item-id")
    assert item_id
    # Track player requests after the file list finishes loading its own thumbnail.
    expect(row.locator(".file-list-thumbnail img")).to_be_visible()
    thumbnails: list[str] = []
    page.on(
        "request",
        lambda request: (
            thumbnails.append(request.url)
            if "/api/v1/thumbnail?" in request.url
            and parse_qs(urlsplit(request.url).query)
            .get("path", [""])[0]
            .rsplit("/", 1)[-1]
            == item_id
            else None
        ),
    )
    position_url = _seed_position(page, server, item_id)
    file.dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    # The fixture starts with a color test pattern; inspect the video canvas,
    # not metadata readiness or the play button drawn over a black surface.
    page.wait_for_function(
        """()=>{
          const player=document.querySelector('movi-player');
          const source=player?.getCanvas();
          if (!source?.width||!source.height||!player.paused||player.seeking
              ||player.currentTime>=0.1) return false;
          const sample=document.createElement('canvas');
          sample.width=sample.height=16;
          const context=sample.getContext('2d');
          context.drawImage(source,0,0,16,16);
          const pixels=context.getImageData(0,0,16,16).data;
          return pixels.some((value,index)=>index%4!==3&&value>32);
        }""",
        timeout=30000,
    )
    assert player.evaluate("element=>element.paused&&element.currentTime<0.1")
    expect(player).to_have_attribute("postertime", "50%")
    assert thumbnails == []
    expect(player.locator(".movi-poster-overlay")).to_be_hidden()
    # A bookmark in the first two seconds does not offer resume.
    expect(
        dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_have_count(0)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    assert page.request.get(position_url).json()["progress"]["position_seconds"] == 1.5

    file.dblclick()
    player = preview_dialog(page).locator("movi-player")
    page.wait_for_function("""()=>{
        const player=document.querySelector('movi-player');
        return player?.player.hasPicture&&!player.seeking&&player.currentTime<0.1;
    }""")
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("""()=>{
        const player=document.querySelector('movi-player');
        return player.currentTime>0.5&&player.currentTime<1.5;
    }""")


def test_closing_preview_cancels_pending_stored_poster_lookup(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    # Keep the optional poster pending long enough to verify close owns the abort.
    page.add_init_script("""(() => {
        const timeout = AbortSignal.timeout.bind(AbortSignal);
        AbortSignal.timeout = delay => timeout(Math.max(3000, delay));
    })()""")
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.route("**/api/v1/poster?*", lambda route: None)
    with page.expect_request("**/api/v1/poster?*"):
        page.get_by_role("button", name="movtext-subtitles.mp4", exact=True).dblclick()
    dialog = preview_dialog(page)
    with page.expect_event(
        "requestfailed", predicate=lambda request: "/api/v1/poster?" in request.url
    ):
        dialog.get_by_role("button", name="Close Preview", exact=True).click()
    expect(page.locator("movi-player")).to_have_count(0)


@pytest.mark.parametrize("action", ["play", "resume", "seek"])
def test_representative_seek_yields_to_early_user_intent(
    server: Server, desktop: tuple[Any, Any], action: str
) -> None:
    _, page = desktop
    page.add_init_script("""(()=>{
        document.addEventListener('loadedmetadata', event=>{
            const player=event.target;
            if (player.tagName!=='MOVI-PLAYER'||player.__posterFlushHeld) return;
            player.__posterFlushHeld=true;
            const decoder=player.player.videoDecoder;
            const flush=decoder.flush.bind(decoder);
            const gate=new Promise(resolve=>{window.releasePosterFlush=resolve});
            let held=false;
            decoder.flush=async(...args)=>{
                const result=await flush(...args);
                if (!held) {
                    held=true;
                    player.dataset.posterFlushWaiting='true';
                    await gate;
                }
                return result;
            };
        },true);
    })()""")
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.route("**/api/v1/poster?*", lambda route: route.fulfill(status=204))
    file = page.get_by_role("button", name="movtext-subtitles.mp4", exact=True)
    item_id = file.locator("xpath=ancestor::*[@data-item-id][1]").get_attribute(
        "data-item-id"
    )
    assert item_id
    saved_position = 2.5 if action == "resume" else 1.5
    position_url = _seed_position(page, server, item_id, saved_position)
    file.dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(player).to_have_attribute("data-poster-flush-waiting", "true")
    assert (
        page.request.get(position_url).json()["progress"]["position_seconds"]
        == saved_position
    )
    if action == "play":
        player.locator("button.movi-play-pause").click()
        page.wait_for_function("""()=>{
            const player=document.querySelector('movi-player');
            return !player.paused&&player.currentTime>0.2&&player.currentTime<1;
        }""")
    elif action == "resume":
        player.locator("button.movi-play-pause").click()
        page.wait_for_function("""()=>{
            const player=document.querySelector('movi-player');
            return !player.paused&&player.currentTime>=2.5&&player.currentTime<3.2;
        }""")
    else:
        player.evaluate(
            "async element=>{await element.pause();await element.player.seek(2)}"
        )
        page.wait_for_function("""()=>{
            const player=document.querySelector('movi-player');
            return player.paused&&!player.seeking&&Math.abs(player.currentTime-2)<0.1;
        }""")
    page.evaluate("window.releasePosterFlush()")
    if action == "seek":
        assert player.evaluate(
            "element=>element.paused&&Math.abs(element.currentTime-2)<0.1"
        )
    else:
        assert player.evaluate("element=>!element.paused")


def test_video_preview_does_not_poll_or_display_a_stale_server_queue(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    diagnostics: list[str] = []

    def queued(route: Any) -> None:
        diagnostics.append(route.request.url)
        route.fulfill(json={"queue": {"position": 7, "estimated_wait_seconds": 60}})

    page.route("**/api/v1/file-status?*", queued)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    # Other preview types still show an actual provider queue when necessary.
    page.get_by_role("button", name="sample.png", exact=True).dblclick()
    image = preview_dialog(page)
    expect(image.get_by_role("status")).to_contain_text(
        "Waiting for Telegram capacity · About 60 seconds"
    )
    assert diagnostics
    image.get_by_role("button", name="Close Preview", exact=True).click()
    expect(image).to_have_count(0)
    diagnostics.clear()

    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    expect(dialog.locator(".preview-limit")).to_have_count(0)
    player.locator("button.movi-play-pause").click()
    # Real playback advances beyond the former diagnostic polling interval.
    page.wait_for_function("()=>document.querySelector('movi-player')?.currentTime>3")
    assert diagnostics == []
    expect(dialog.get_by_text("Waiting for Telegram", exact=False)).to_have_count(0)


@pytest.mark.parametrize("close_while_waiting", [False, True])
def test_image_preview_retries_busy_capacity_and_cancels_on_close(
    server: Server, desktop: tuple[Any, Any], close_while_waiting: bool
) -> None:
    _, page = desktop
    media_requests: list[str] = []

    def busy_file(route: Any) -> None:
        media_requests.append(route.request.url)
        route.fulfill(
            status=503,
            headers={"Retry-After": "3"},
            json={"code": "TELEGRAM_QUEUE_FULL", "detail": "Telegram is busy"},
        )

    page.route("**/file?*", busy_file)
    page.route(
        "**/api/v1/file-status?*",
        lambda route: route.fulfill(
            json={
                "queue": None,
                "phase": "retry_wait",
                "code": "TELEGRAM_QUEUE_FULL",
                "retry_after": 3,
            }
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.clock.install()
    page.get_by_role("button", name="sample.png", exact=True).dblclick()
    dialog = preview_dialog(page)
    expect(dialog.get_by_role("status")).to_contain_text("will retry automatically")
    assert len(media_requests) == 1
    expect(dialog.get_by_text("Preview Unavailable", exact=True)).to_have_count(0)
    if close_while_waiting:
        dialog.get_by_role("button", name="Close Preview", exact=True).click()
        page.clock.run_for(10000)
        assert len(media_requests) == 1
        return

    page.clock.run_for(1000)
    assert len(media_requests) == 1
    page.clock.run_for(2500)
    expect(dialog.get_by_role("status")).to_contain_text("will retry automatically")
    assert len(media_requests) == 2
    page.clock.run_for(3500)
    expect(
        dialog.get_by_role("button", name="Try Preview Again", exact=True)
    ).to_be_visible()
    expect(dialog.get_by_role("status")).to_contain_text("waiting queue is full")
    assert len(media_requests) == 3
    page.clock.run_for(10000)
    assert len(media_requests) == 3


@pytest.mark.parametrize("width,height", [(1536, 695), (820, 1180), (390, 844)])
def test_video_header_fullscreen_download_and_close_stay_reachable(
    server: Server, desktop: tuple[Any, Any], width: int, height: int
) -> None:
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    file_button = page.get_by_role("button", name="multitrack.mkv", exact=True)
    file_button.dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    fullscreen = dialog.get_by_role("button", name="Enter Fullscreen", exact=True)
    fullscreen.click()
    page.wait_for_function(
        "()=>document.fullscreenElement===document.querySelector('movi-player')"
    )
    expect(player).to_be_focused()
    # Exiting the player returns keyboard focus to its visible toolbar action.
    page.evaluate("document.exitFullscreen()")
    page.wait_for_function("()=>!document.fullscreenElement")
    expect(fullscreen).to_be_focused()
    expect(dialog.get_by_role("heading", name="multitrack.mkv")).to_be_visible()
    expect(dialog.locator(".preview-file-meta")).to_contain_text("MKV ·")
    download = dialog.get_by_role("link", name="Download Original", exact=True)
    close = dialog.get_by_role("button", name="Close Preview", exact=True)
    if width <= 720:
        expect(dialog.locator(".preview-file-date")).to_be_hidden()
        for action in (fullscreen, download, close):
            expect(action).to_be_in_viewport(ratio=1)
            bounds = action.bounding_box()
            assert bounds is not None
            assert bounds["width"] >= 44 and bounds["height"] >= 44
    else:
        expect(dialog.locator(".preview-file-date")).to_be_visible()
    header = dialog.locator(".preview-header").bounding_box()
    assert header and header["height"] <= 54
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=ARTIFACTS / f"video-preview-header-{width}.png")
    with page.expect_download() as result:
        download.click()
    assert hashlib.sha256(Path(result.value.path()).read_bytes()).digest() == (
        hashlib.sha256((ROOT / "tests/fixtures/multitrack.mkv").read_bytes()).digest()
    )
    close.click()
    expect(dialog).to_have_count(0)
    expect(file_button).to_be_focused()
    expect(page.locator("movi-player")).to_have_count(0)


def _open_settings(player: Any, *, touch: bool) -> Any:
    if not touch:
        player.hover()
    settings = player.locator("button.movi-settings-btn")
    if not settings.is_visible():
        more = player.locator("button.movi-more-btn")
        more.tap() if touch else more.click()
    settings.tap() if touch else settings.click()
    menu = player.locator(".movi-settings-menu")
    expect(menu).to_be_visible()
    return menu


@pytest.mark.parametrize("touch", [False, True], ids=["desktop", "touch-phone"])
def test_video_quality_uses_bounded_player_settings_without_a_preview_bar(
    server: Server, browser: Any, touch: bool
) -> None:
    width, height = (390, 844) if touch else (1440, 960)
    context = browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        login(page, server.url)
        for name in ("Preview samples", "multitrack.mkv"):
            button = page.get_by_role("button", name=name, exact=True)
            button.tap() if touch else button.press("Enter")
        dialog = preview_dialog(page)
        player = dialog.locator("movi-player")
        page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
        expect(dialog.locator(".preview-quality")).to_have_count(0)
        expect(
            dialog.get_by_role("combobox", name="Preview Quality", exact=True)
        ).to_have_count(0)
        download = dialog.get_by_role("link", name="Download Original", exact=True)
        original_download = download.get_attribute("href")
        player.evaluate(
            "async element=>{await element.pause();await element.player.seek(2)}"
        )
        page.wait_for_function(
            "()=>Math.abs(document.querySelector('movi-player').currentTime-2)<0.2"
        )
        menu = _open_settings(player, touch=touch)
        for name in ("Ambient mode", "Stable volume"):
            expect(menu.get_by_role("button", name=name, exact=True)).to_be_visible()
        quality = menu.locator('.movi-settings-row[data-page="quality"]')
        quality.tap() if touch else quality.click()
        expect(menu.locator(".movi-settings-page-title")).to_have_text("Quality")
        original = menu.get_by_role("button", name="Original", exact=True)
        expect(original).to_be_visible()
        expect(original).to_have_attribute("aria-pressed", "true")
        back = menu.locator(".movi-settings-back")
        back.tap() if touch else back.click()
        expect(quality).to_be_visible()
        quality.tap() if touch else quality.click()
        expect(
            menu.get_by_role("group", name="Video quality", exact=True)
        ).to_be_visible()
        alternate = menu.get_by_role(
            "button", name="Fixture alternate source", exact=True
        )
        expect(alternate).to_be_visible()
        assert player.evaluate("element=>element.scrollLeft===0&&element.scrollTop===0")
        bounds = menu.bounding_box()
        assert bounds is not None
        assert bounds["x"] >= -1 and bounds["x"] + bounds["width"] <= width + 1
        assert bounds["y"] >= -1 and bounds["y"] + bounds["height"] <= height + 1
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        kind = "touch-phone" if touch else "desktop"
        page.screenshot(path=ARTIFACTS / f"video-quality-settings-{kind}.png")
        player.evaluate("element=>window.previousQualityPlayer=element")
        alternate.tap() if touch else alternate.click()
        page.wait_for_function("""()=>{
          const player=document.querySelector('movi-player');
          return player&&player!==window.previousQualityPlayer&&player.duration>4;
        }""")
        page.wait_for_function(
            "()=>Math.abs(document.querySelector('movi-player').currentTime-2)<0.2"
        )
        assert player.evaluate("element=>element.paused")
        expect(player.locator(".movi-poster-overlay")).to_be_hidden()
        assert player.get_attribute("poster") is None
        assert player.get_attribute("postertime") is None
        expect(download).to_have_attribute("href", original_download)
        expect(dialog.locator(".preview-quality")).to_have_count(0)
        menu = _open_settings(player, touch=touch)
        quality.tap() if touch else quality.click()
        expect(alternate).to_have_attribute("aria-pressed", "true")
        player.evaluate("element=>window.previousQualityPlayer=element")
        original.tap() if touch else original.click()
        page.wait_for_function("""()=>{
          const player=document.querySelector('movi-player');
          return player&&player!==window.previousQualityPlayer&&player.duration>4;
        }""")
        page.wait_for_function(
            "()=>Math.abs(document.querySelector('movi-player').currentTime-2)<0.2"
        )
        assert player.evaluate("element=>element.paused")
        expect(download).to_have_attribute("href", original_download)
        assert errors == []
    finally:
        context.close()


def test_video_without_alternate_sources_has_no_quality_row_or_preview_bar(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    page.get_by_role("button", name="movtext-subtitles.mp4", exact=True).press("Enter")
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    expect(dialog.locator(".preview-quality")).to_have_count(0)
    menu = _open_settings(player, touch=False)
    expect(menu.locator('.movi-settings-row[data-page="quality"]')).to_have_count(0)


def test_quality_switch_in_fullscreen_keeps_video_position_and_f_exits(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    media_requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            media_requests.append(request.url)
            if "/file?" in request.url and request.method == "GET"
            else None
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    page.get_by_role("button", name="multitrack.mkv", exact=True).press("Enter")
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    player.evaluate(
        "async element=>{await element.pause();await element.player.seek(2)}"
    )
    page.wait_for_function(
        "()=>Math.abs(document.querySelector('movi-player').currentTime-2)<0.2"
    )
    dialog.get_by_role("button", name="Enter Fullscreen", exact=True).click()
    page.wait_for_function(
        "()=>document.fullscreenElement===document.querySelector('movi-player')"
    )
    menu = _open_settings(player, touch=False)
    menu.locator('.movi-settings-row[data-page="quality"]').click()
    player.evaluate("element=>window.fullscreenQualityPlayer=element")
    menu.get_by_role("button", name="Fixture alternate source", exact=True).click()
    page.wait_for_function("""()=>{
      const player=document.querySelector('movi-player');
      return player&&player!==window.fullscreenQualityPlayer&&player.duration>4;
    }""")
    page.wait_for_function(
        "()=>Math.abs(document.querySelector('movi-player').currentTime-2)<0.2"
    )
    assert player.evaluate("element=>element.paused")
    assert player.evaluate("element=>!!document.fullscreenElement?.contains(element)")
    assert any("quality=fixture-same-bytes" in url for url in media_requests)
    expect(player).to_be_focused()
    player.press("f")
    page.wait_for_function("()=>!document.fullscreenElement")
    expect(
        dialog.get_by_role("button", name="Enter Fullscreen", exact=True)
    ).to_be_focused()
