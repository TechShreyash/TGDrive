"""Actual Movi controls play bounded compatible output from offline original bytes."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.browser.test_browser_e2e import Server, expect, login, preview_dialog

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def picture(player):
    return player.evaluate("""el=>{
        const canvas=document.createElement('canvas');
        canvas.width=64;canvas.height=36;
        const context=canvas.getContext('2d');
        context.drawImage(el.shadowRoot.querySelector('canvas'),0,0,64,36);
        const bytes=context.getImageData(0,0,64,36).data;
        let hash=0,colored=0;
        for(let i=0;i<bytes.length;i++){
            hash=(Math.imul(hash,31)+bytes[i])>>>0;
            if(i%4!==3&&bytes[i]>24)colored++;
        }
        return {hash,colored};
    }""")


def test_short_video_only_wmv_uses_compatible_playback(server, desktop):
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="unsupported-msmpeg4.wmv", exact=True).dblclick()
    dialog = preview_dialog(page)
    compatible = dialog.get_by_role(
        "button", name="Play compatible version", exact=True
    )
    with page.expect_response(
        lambda response: "/api/v1/playback/segment.ts?" in response.url
    ) as segment:
        compatible.click()
    assert segment.value.ok, segment.value.text()
    page.wait_for_function("""() => {
        const player = document.querySelector("movi-player");
        return player?.player?.streamWrapper?._framesRendered > 0;
    }""")
    player = dialog.locator("movi-player")
    first_frame = picture(player)
    assert first_frame["colored"] > 100
    page.wait_for_function("""() => {
        const player = document.querySelector("movi-player");
        return player && !player.paused && player.currentTime > 0.25 &&
            player.player?.streamWrapper?._framesRendered > 2;
    }""")
    assert picture(player)["hash"] != first_frame["hash"]


@pytest.mark.parametrize(
    ("kind", "width"),
    [("wmv", 1440), ("wmv", 390), ("prores", 1440), ("transport", 1440)],
)
def test_real_unsupported_video_compatible_switch_seek_and_original_progress(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path, kind: str, width: int
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 960 if width > 720 else 844})
    login(page, server.url)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required for the synthetic codec samples")
    suffix, codec, audio = {
        "prores": ("mov", "prores_ks", "pcm_s16le"),
        "wmv": ("wmv", "msmpeg4v3", "wmav2"),
        "transport": ("ts", "mpeg2video", "mp2"),
    }[kind]
    source = tmp_path / f"synthetic-{kind}.{suffix}"
    subprocess.run(  # noqa: S603 - fixed encoder and self-generated samples
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "14",
            "-c:v",
            codec,
            "-threads",
            "1",
            "-c:a",
            audio,
            str(source),
        ],
        check=True,
        timeout=30,
    )
    page.locator("#new-button:visible, #compact-new-button:visible").click()
    with page.expect_file_chooser() as chooser:
        page.locator("#file-upload-action").click()
    chooser.value.set_files(str(source))
    started = server.wait_started(1)
    server.finish(started[0]["id"])
    item = page.get_by_role("button", name=source.name, exact=True)
    expect(item).to_be_visible(timeout=20000)
    original_bytes = source.read_bytes()
    progress_requests = []
    page.on(
        "request",
        lambda request: (
            progress_requests.append(request.url)
            if "/video-progress" in request.url
            else None
        ),
    )
    item.dblclick()
    dialog = preview_dialog(page)
    if kind == "transport":
        # Keep this deterministic when the original decoder supports MPEG-2.
        player = dialog.locator("movi-player")
        expect(player).to_have_attribute("engine", "wasm")
        player.evaluate("el=>el.dispatchEvent(new Event('error'))")
    compatible = dialog.get_by_role(
        "button", name="Play compatible version", exact=True
    )
    expect(compatible).to_be_visible(timeout=20000)
    assert dialog.locator("movi-player").evaluate(
        "el=>el.player?.getState()!=='playing'"
    )
    if width < 720:
        box = compatible.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width
    expect(
        dialog.get_by_text("The original file stays unchanged.", exact=False)
    ).to_be_visible()
    with page.expect_request("**/api/v1/playback/compatibility.m3u8?**") as requested:
        compatible.click()
    original_id = parse_qs(urlsplit(requested.value.url).query)["path"][0].rsplit(
        "/", 1
    )[-1]
    player = dialog.locator("movi-player")
    page.wait_for_function(
        """()=>{
        const p=document.querySelector('movi-player');
        return p?.player?.streamWrapper && p.duration>6;
    }""",
        timeout=30000,
    )
    if kind == "wmv" and width == 1440:
        player.evaluate("el=>el.pause()")
        page.wait_for_function("""()=>{
            const p=document.querySelector('movi-player');
            const w=p.player.streamWrapper;
            return p.paused&&w.videoElement.readyState>=2&&w._framesRendered>0;
        }""")
        assert picture(player)["colored"] > 100
        position = page.request.get(
            server.url + f"/api/v1/files/{original_id}/video-progress"
        ).json()["progress"]
        assert position is None or position["position_seconds"] < 2
    if player.evaluate("el=>el.paused"):
        player.locator("button.movi-play-pause").click()
    page.wait_for_function(
        """()=>{
        const p=document.querySelector('movi-player');
        return p.currentTime>.2 && p.player.streamWrapper._framesRendered>2;
    }""",
        timeout=30000,
    )
    assert player.evaluate("""el=>{
        const s=el.player.streamWrapper.player.getConfiguration().streaming;
        return s.bufferingGoal===6 && s.bufferBehind===6 && s.segmentPrefetchLimit===0;
    }""")
    before = picture(player)
    frames = player.evaluate("el=>el.player.streamWrapper._framesRendered")
    player.evaluate("async el=>{await el.pause();await el.player.seek(6.5)}")
    page.wait_for_function(
        """before=>{
        const p=document.querySelector('movi-player');
        const w=p.player.streamWrapper;
        return p.paused&&!w.videoElement.seeking&&Math.abs(p.currentTime-6.5)<.15
          &&w._framesRendered>before;
    }""",
        arg=frames,
        timeout=30000,
    )
    after = picture(player)
    assert after["colored"] > 100
    assert before["hash"] != after["hash"]
    if width == 390:
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        player.locator(".movi-settings-btn").click()
        player.get_by_role(
            "button", name="Copy playback diagnostics", exact=True
        ).click()
        expect(
            dialog.get_by_text("Playback diagnostics copied.", exact=True)
        ).to_be_visible()
        diagnostics = page.evaluate("navigator.clipboard.readText()")
        assert "Mode: Compatible (browser decoder)" in diagnostics
        assert "Position: 6.50 s" in diagnostics
        assert original_id not in diagnostics and source.name not in diagnostics
        assert page.evaluate("document.querySelector('movi-player').paused")
    player.evaluate("el=>el.play()")
    page.wait_for_function(
        "()=>document.querySelector('movi-player').currentTime>6.8", timeout=30000
    )
    expect(
        dialog.get_by_role("button", name="Play compatible version", exact=True)
    ).to_have_count(0)
    with page.expect_download() as downloaded:
        dialog.get_by_role("link", name="Download Original", exact=True).click()
    download = tmp_path / "original-download"
    downloaded.value.save_as(download)
    assert (
        hashlib.sha256(download.read_bytes()).digest()
        == hashlib.sha256(original_bytes).digest()
    )
    if kind == "wmv" and width == 1440:
        with page.expect_response(
            lambda response: (
                response.url.endswith("/video-progress")
                and response.request.method == "PUT"
                and response.request.post_data_json.get("final") is True
            )
        ) as final_save:
            page.get_by_role("button", name="Close Preview", exact=True).click()
        assert final_save.value.ok
        saved = page.request.get(
            server.url + f"/api/v1/files/{original_id}/video-progress"
        ).json()["progress"]
        assert saved["position_seconds"] > 6
        with page.expect_response(
            lambda response: (
                response.url.endswith("/video-progress")
                and response.request.method == "GET"
            )
        ):
            item.dblclick()
        reopened = preview_dialog(page)
        reopened.get_by_role(
            "button", name="Play compatible version", exact=True
        ).click()
        page.wait_for_function(
            "time=>document.querySelector('movi-player')?.currentTime>=time-.2",
            arg=saved["position_seconds"],
            timeout=30000,
        )
        page.get_by_role("button", name="Close Preview", exact=True).click()
    else:
        page.get_by_role("button", name="Close Preview", exact=True).click()
    expect(page.locator("movi-player")).to_have_count(0)
    assert progress_requests
    assert all(
        f"/api/v1/files/{original_id}/video-progress" in url
        for url in progress_requests
    )
