"""Real browser regressions for playback, modal notices and device receipts."""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    UPLOAD_RECOVERY_RECORDS,
    Server,
    expect,
    login,
    preview_dialog,
)
from tests.browser.test_browser_e2e import (
    browser as browser_fixture,
)
from tests.browser.test_browser_e2e import (
    desktop as desktop_fixture,
)
from tests.browser.test_browser_e2e import (
    server as server_fixture,
)

pytestmark = pytest.mark.e2e
browser = browser_fixture
desktop = desktop_fixture
server = server_fixture


def test_playing_clock_clears_stale_wait_and_keyboard_works(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(player).to_be_visible(timeout=30000)
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    player.focus()
    player.press("k")
    page.wait_for_function(
        "()=>document.querySelector('movi-player').currentTime>0.25", timeout=20000
    )
    before = player.evaluate("element=>element.currentTime")
    player.evaluate("element=>element.dispatchEvent(new Event('waiting'))")
    page.wait_for_function(
        "before=>document.querySelector('movi-player').currentTime>before+0.25",
        arg=before,
    )
    expect(dialog.locator(".advanced-state-buffering")).to_have_count(0)
    player.focus()
    player.press("k")
    page.wait_for_function("()=>document.querySelector('movi-player').paused")
    player.press("m")
    assert player.evaluate("element=>element.muted") is True


def test_chapters_are_immediate_keyboard_accessible_and_mobile_bounded(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    page.wait_for_function("()=>!!document.querySelector('movi-player')?.player")
    # Public chapters API supplies real metadata without requesting remote artwork.
    player.evaluate("""element=>{
      element.__previewFrameReads = 0;
      const preview = element.player.getPreviewFrame.bind(element.player);
      element.player.getPreviewFrame = (...args) => {
        element.__previewFrameReads++;
        return preview(...args);
      };
      element.player.setChapters([
        {title:'Opening',start:0},
        {title:'A very long chapter title for a narrow phone screen',start:1}
      ]);
      element.dispatchEvent(new Event('loadedmetadata'));
    }""")
    control = player.locator(".movi-chapter-pill")
    expect(control).to_be_visible()
    assert control.evaluate("element=>!!element.closest('.movi-controls-bar')")
    chapters = player.locator(".movi-timeline-panel")
    expect(chapters).not_to_be_visible()
    control.focus()
    control.press("Enter")
    opening = chapters.get_by_role("button", name="Opening 0:00", exact=True)
    expect(opening).to_be_visible()
    expect(opening).to_be_focused()
    expect(chapters).to_have_attribute("role", "region")
    expect(chapters).to_have_attribute("aria-label", "Chapters")
    expect(chapters.locator(".movi-timeline-item")).to_have_count(2)
    # Titles and keyboard targets are ready before optional visible-card previews.
    assert player.evaluate("element => element.__previewFrameReads") <= 2
    page.screenshot(path=ARTIFACTS / "chapters-immediate-desktop.png")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(chapters).to_be_visible()
    button = chapters.get_by_role(
        "button", name="A very long chapter title", exact=False
    )
    expect(button).to_be_in_viewport()
    assert chapters.evaluate("el=>el.getBoundingClientRect().right<=innerWidth")
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=ARTIFACTS / "chapters-immediate-mobile.png")
    opening.focus()
    opening.press("ArrowRight")
    expect(button).to_be_focused()
    button.press("ArrowLeft")
    expect(opening).to_be_focused()
    opening.press("Space")
    assert player.evaluate("element => element.paused")
    button.focus()
    button.press("Enter")
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>=0.9")
    expect(chapters).to_be_visible()
    assert player.evaluate("element => element.paused")
    chapters.locator(".movi-timeline-close").click()
    expect(chapters).not_to_be_visible()
    player.evaluate("element=>element.requestFullscreen()")
    page.wait_for_function("()=>!!document.fullscreenElement")
    expect(chapters).not_to_be_visible()
    player.hover()
    # Compact players expose the native chapter pill through Settings.
    if control.is_visible():
        control.focus()
        control.press("Space")
    else:
        settings = player.locator("button.movi-settings-btn")
        settings.focus()
        settings.press("Enter")
        player.locator(".movi-settings-menu").get_by_role(
            "button", name="Chapters", exact=True
        ).click()
    expect(chapters).to_be_visible()
    assert chapters.evaluate("element=>element.getRootNode() instanceof ShadowRoot")
    expect(opening).to_be_visible()
    expect(button).to_have_class(__import__("re").compile("movi-timeline-current"))
    expect(button).to_be_focused()
    assert player.evaluate("element=>element.paused")
    button.press("Escape")
    expect(chapters).not_to_be_visible()
    expect(player).to_be_focused()
    assert page.evaluate("!!document.fullscreenElement")
    page.evaluate("document.exitFullscreen()")
    page.wait_for_function("()=>!document.fullscreenElement")
    player.focus()
    player.press("Space")
    page.wait_for_function(
        """()=>{
          const player=document.querySelector('movi-player');
          return !player.paused && player.currentTime>1.2;
        }"""
    )


@pytest.mark.parametrize("copy_label", ["Copy Link", "Copy Embed Link"])
def test_share_copy_notice_is_above_native_dialog_without_stealing_focus(
    server: Server, desktop: tuple[Any, Any], copy_label: str
) -> None:
    context, page = desktop
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    login(page, server.url)
    page.get_by_label("Actions for 資料", exact=True).click()
    page.get_by_role("button", name="Share Link", exact=True).click()
    page.get_by_role("button", name="Create Share Link", exact=True).click()
    copy = page.get_by_role("button", name=copy_label, exact=True)
    copy.click()
    notice = page.locator(".toast")
    expect(notice).to_be_visible()
    assert notice.evaluate("el=>el.matches(':popover-open')")
    assert notice.evaluate("el=>!!el.closest('dialog[open]')")
    assert notice.evaluate("""el=>{
      const r=el.getBoundingClientRect();
      return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
    }""")
    expect(copy).to_be_focused()
    page.screenshot(path=ARTIFACTS / "share-copy-notice-top-layer.png")


def test_other_device_transfers_stay_in_history_and_do_not_open_popup(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    page.route(
        "**/api/v1/jobs",
        lambda route: route.fulfill(
            json={
                "jobs": [
                    {
                        "id": "another-device",
                        "name": "Other device upload.bin",
                        "kind": "upload",
                        "status": "running",
                        "phase": "uploading",
                        "current": 1,
                        "total": 4,
                    }
                ]
            }
        ),
    )
    login(page, server.url)
    expect(page.locator(".transfer-center")).to_have_count(0)
    page.get_by_role("link", name="Transfers", exact=False).click()
    expect(
        page.get_by_text("Other device upload.bin", exact=True).first
    ).to_be_visible()


def test_url_receipt_remains_a_server_job_after_refresh(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    page.add_init_script("""localStorage.setItem('tgdrive.uploads.v2',JSON.stringify([{
      key:'url-receipt',name:'URL import.bin',size:4,total:4,current:1,
      lastModified:0,destination:'/',batchId:'this-device-url',kind:'remote',
      jobId:'url-server-job',state:'uploading',phase:'Sending to Telegram',
      initiatedHere:true
    }]));""")
    # Older backends can call the URL job 'upload' after local staging completes.
    page.route(
        "**/api/v1/jobs",
        lambda route: route.fulfill(
            json={
                "jobs": [
                    {
                        "id": "url-server-job",
                        "name": "URL import.bin",
                        "kind": "upload",
                        "status": "running",
                        "phase": "uploading",
                        "current": 2,
                        "total": 4,
                    }
                ]
            }
        ),
    )
    login(page, server.url)
    page.reload()
    expect(page.locator(".transfer-center")).to_be_visible()
    toggle = page.get_by_role("button", name="Show Transfer Details", exact=True)
    if toggle.count():
        toggle.click()
    expect(page.get_by_text("URL import.bin", exact=True).first).to_be_visible()
    expect(
        page.get_by_text("Choose the original file to resume", exact=True)
    ).to_have_count(0)
    saved = page.evaluate(UPLOAD_RECOVERY_RECORDS)
    receipt = next(item for item in saved if item["jobId"] == "url-server-job")
    assert receipt["kind"] == "remote"
    assert receipt["state"] == "uploading"


def test_custom_player_timeline_shows_engine_buffered_regions_after_real_seek(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    for target in (0, 3):
        player.evaluate(
            """async (element,time)=>{
              await element.pause();await element.player.seek(time);
            }""",
            target,
        )
        # Read real engine TimeRanges rather than projecting server byte cache
        # offsets onto VBR media time. A fully fetched tiny fixture may retain all.
        page.wait_for_function(
            """()=>{
          const player=document.querySelector('movi-player');
          const buffer=player?.shadowRoot?.querySelector('[part="progress-buffer"]');
          if(!player?.buffered.length||!buffer||!player.duration) return false;
          const start=player.buffered.start(0);
          const end=player.buffered.end(0),duration=player.duration;
          const left=parseFloat(buffer.style.left),width=parseFloat(buffer.style.width);
          return width>0 && Math.abs(left-start/duration*100)<1 &&
            Math.abs(width-(end-start)/duration*100)<1;
        }""",
            timeout=15000,
        )
    segment = player.locator('[part="progress-buffer"]')
    expect(segment).to_be_visible()
    color = segment.evaluate("element=>getComputedStyle(element).backgroundColor")
    assert color == "rgb(171, 183, 206)"
    page.set_viewport_size({"width": 390, "height": 844})
    player.hover()
    expect(segment).to_be_visible()
    expect(player.locator("button.movi-play-pause")).to_be_visible()
    expect(dialog.locator(".preview-surface > video")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
