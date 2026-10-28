"""Demand-driven real preview decoding shares cached bytes with playback."""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.test_browser_e2e import ARTIFACTS, Server, expect
from tests.browser.test_browser_e2e import browser as browser_fixture
from tests.browser.test_browser_e2e import desktop as desktop_fixture
from tests.browser.test_browser_e2e import server as server_fixture
from tests.browser.test_browser_player_settings import open_video

pytestmark = pytest.mark.e2e
browser, desktop, server = browser_fixture, desktop_fixture, server_fixture


def _chapters(player: Any) -> None:
    player.evaluate("""el => {
      el.__chapterCalls = [];
      el.__chapterMisses = [];
      el.__chapterActive = 0;
      el.__chapterPeak = 0;
      el.__chapterHoverCalls = 0;
      const core = el.player;
      const preview = core.getPreviewFrame.bind(core);
      core.getPreviewFrame = async (...args) => {
        el.__chapterCalls.push(args[0]);
        if (args[2]!==false) el.__chapterHoverCalls++;
        if (!core.hasPreviewFor(args[0])) el.__chapterMisses.push(args[0]);
        el.__chapterActive++;
        el.__chapterPeak = Math.max(el.__chapterPeak, el.__chapterActive);
        try { return await preview(...args); }
        finally { el.__chapterActive--; }
      };
      core.setChapters(Array.from({length:24}, (_,index) => ({
        title:'Chapter '+(index+1), start:index * el.duration / 24
      })));
      el.dispatchEvent(new Event('loadedmetadata'));
    }""")


def _has_pixels(image: Any) -> bool:
    return image.evaluate("""img => {
      if (!img.complete || !img.naturalWidth) return false;
      const canvas = document.createElement('canvas');
      canvas.width=16; canvas.height=9;
      const ctx=canvas.getContext('2d'); ctx.drawImage(img,0,0,16,9);
      const bytes=ctx.getImageData(0,0,16,9).data;
      const rgb=[...bytes].filter((_,index)=>index%4!==3);
      return Math.max(...rgb)-Math.min(...rgb)>30;
    }""")


def test_hover_and_visible_chapters_decode_pixels_without_refetching_cached_file(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    page.set_viewport_size({"width": 390, "height": 844})
    dialog, player = open_video(page, server)
    page.wait_for_function("""()=>document.querySelector('movi-player')
      ?.player.getSource().isFullyCached()""")
    file_reads: list[int] = []

    def record(response: Any) -> None:
        if "/file?" in response.url and response.request.method == "GET":
            file_reads.append(int(response.headers.get("content-length", "0")))

    page.on("response", record)
    assert player.get_attribute("thumb") is not None
    player.hover()
    page.wait_for_timeout(350)  # Let the native controls finish moving the seek bar.
    progress = player.locator(".movi-progress-bar")
    box = progress.bounding_box()
    assert box
    progress.hover(position={"x": box["width"] * 0.3, "y": box["height"] / 2})
    hover = player.locator(".movi-thumbnail-img")
    expect(hover).to_be_visible(timeout=30000)
    page.wait_for_function("""()=>{const img=document.querySelector('movi-player')
      ?.shadowRoot.querySelector('.movi-thumbnail-img');
      return img?.complete&&img.naturalWidth>0}""")
    assert _has_pixels(hover)
    page.screenshot(path=ARTIFACTS / "video-hover-preview-mobile.png")
    assert player.evaluate("""async el=>{
      const core=el.player, entry=[...core.previewCache][0];
      if (!entry) return false;
      const time=entry[0]*.2, cached=entry[1];
      if (!core.hasPreviewFor(time)) return false;
      return await core.getPreviewFrame(time)===cached;
    }""")
    page.mouse.move(5, 5)
    progress.hover(position={"x": box["width"] * 0.3, "y": box["height"] / 2})
    expect(hover).to_be_visible()
    assert _has_pixels(hover)
    assert sum(file_reads) == 0
    page.mouse.move(5, 5)

    page.evaluate("""()=>{
      window.revokedChapterUrls=[];
      const revoke=URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL=url=>{window.revokedChapterUrls.push(url);revoke(url)};
    }""")
    _chapters(player)
    player.focus()
    player.press("t")
    panel = player.locator(".movi-timeline-panel")
    cards = panel.locator(".movi-timeline-chapter")
    expect(cards).to_have_count(24)
    expect(cards.first).to_contain_text("Chapter 1")
    page.wait_for_function("""()=>{const imgs=document.querySelector('movi-player')
      ?.shadowRoot.querySelectorAll('.movi-timeline-chapter img');
      return [...(imgs||[])].filter(img=>img.complete&&img.naturalWidth).length>=3}""")
    assert _has_pixels(cards.first.locator("img"))
    page.screenshot(path=ARTIFACTS / "video-chapter-previews-mobile.png")
    assert player.evaluate("el=>el.__chapterPeak") == 1
    assert player.evaluate("el=>el.__chapterCalls.length") < 8
    expect(cards.last.locator("img")).to_have_count(0)
    strip = panel.locator(".movi-timeline-strip")
    strip.evaluate("el=>{el.scrollLeft=el.scrollWidth}")
    expect(cards.last.locator("img")).to_be_visible(timeout=30000)
    page.wait_for_function("""()=>{const img=document.querySelector('movi-player')
      ?.shadowRoot.querySelector('.movi-timeline-chapter:last-child img');
      return img?.complete&&img.naturalWidth>0}""")
    assert _has_pixels(cards.last.locator("img"))
    expect(cards.first.locator("img")).to_have_count(0)
    page.wait_for_function(
        "()=>document.querySelector('movi-player').__chapterActive===0"
    )
    misses = player.evaluate("el=>el.__chapterMisses.length")
    panel.locator(".movi-timeline-close").click()
    player.press("t")
    expect(cards.first.locator("img")).to_be_visible(timeout=30000)
    assert player.evaluate("el=>el.__chapterMisses.length") == misses
    assert sum(file_reads) == 0
    chapter_urls = panel.locator("img").evaluate_all("imgs=>imgs.map(img=>img.src)")
    assert chapter_urls
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    assert page.evaluate(
        "urls=>urls.every(url=>window.revokedChapterUrls.includes(url))", chapter_urls
    )


def test_chapter_work_waits_for_playback_and_stops_when_panel_closes(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    _, player = open_video(page, server)
    _chapters(player)
    player.evaluate("""el=>{
      const state=el.player.getState.bind(el.player);
      el.player.getState=()=>el.__busyState||state();
      el.__busyState='loading';
    }""")
    player.focus()
    player.press("t")
    panel = player.locator(".movi-timeline-panel")
    expect(panel.locator(".movi-timeline-chapter")).to_have_count(24)
    for phase in ("loading", "buffering", "seeking"):
        player.evaluate("(el,phase)=>{el.__busyState=phase}", phase)
        page.wait_for_timeout(300)
        assert player.evaluate("el=>el.__chapterCalls.length") == 0
    player.evaluate("""el=>{
      el.__busyState=null;
      Object.defineProperty(document,'hidden',{
        configurable:true, get:()=>window.previewHidden
      });
      window.previewHidden=true;
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_timeout(300)
    assert player.evaluate("el=>el.__chapterCalls.length") == 0
    player.evaluate("""el=>{
      const preview=el.player.getPreviewFrame.bind(el.player);
      el.player.getPreviewFrame=async(...args)=>{
        await new Promise(resolve=>{el.__releasePreview=resolve});
        return preview(...args);
      };
      delete document.hidden;
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_function(
        "()=>!!document.querySelector('movi-player').__releasePreview"
    )
    panel.locator(".movi-timeline-close").click()
    player.evaluate("el=>el.__releasePreview()")
    page.wait_for_function(
        "()=>document.querySelector('movi-player').__chapterCalls.length===1"
    )
    page.wait_for_function(
        "()=>document.querySelector('movi-player').__chapterActive===0"
    )
    page.wait_for_timeout(500)
    assert player.evaluate("el=>el.__chapterCalls.length") == 1
    expect(panel.locator("img")).to_have_count(0)


def test_stationary_hover_retries_after_an_in_flight_chapter_preview(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    _, player = open_video(page, server)
    _chapters(player)
    player.evaluate("""el=>{
      const core=el.player, preview=core.getPreviewFrame.bind(core);
      let first=true;
      core.getPreviewFrame=async(...args)=>{
        if(first){
          first=false;
          core.isPreviewGenerating=true;
          await new Promise(resolve=>{el.__releaseChapter=resolve});
          core.isPreviewGenerating=false;
        }
        return preview(...args);
      };
    }""")
    player.focus()
    player.press("t")
    page.wait_for_function(
        "()=>!!document.querySelector('movi-player').__releaseChapter"
    )
    # Movi deliberately hides hover cards while its chapter panel is open.
    player.locator(".movi-timeline-close").click()
    player.hover()
    page.wait_for_timeout(350)
    progress = player.locator(".movi-progress-bar")
    box = progress.bounding_box()
    assert box
    progress.hover(position={"x": box["width"] * 0.85, "y": box["height"] / 2})
    page.wait_for_function(
        "()=>document.querySelector('movi-player').__chapterCalls.length>=1"
    )
    player.evaluate("el=>el.__releaseChapter()")
    # Keep the real pointer still: finishing the chapter hands the decoder to hover.
    hover = player.locator(".movi-thumbnail-img")
    expect(hover).to_be_visible(timeout=30000)
    page.wait_for_function("""()=>{const img=document.querySelector('movi-player')
      ?.shadowRoot.querySelector('.movi-thumbnail-img');
      return img?.complete&&img.naturalWidth>0}""")
    assert _has_pixels(hover)


def test_keyboard_chapters_ignore_hidden_hover_and_preserve_seek_dragging(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    _, player = open_video(page, server)
    _chapters(player)
    player.hover()
    page.wait_for_timeout(350)
    progress = player.locator(".movi-progress-bar")
    box = progress.bounding_box()
    assert box
    progress.hover(position={"x": box["width"] * 0.3, "y": box["height"] / 2})
    hover = player.locator(".movi-thumbnail-img")
    expect(hover).to_be_visible(timeout=30000)
    page.wait_for_function("""()=>{const el=document.querySelector('movi-player');
      const img=el?.shadowRoot.querySelector('.movi-thumbnail-img');
      return img?.complete&&img.naturalWidth>0&&!el.player.isPreviewGenerating;
    }""")
    assert _has_pixels(hover)
    colors = player.evaluate("""el=>{
      const root=el.shadowRoot;
      return {
        hover:getComputedStyle(root.querySelector('.movi-progress-hover')).backgroundColor,
        buffer:getComputedStyle(root.querySelector('.movi-progress-buffer')).backgroundColor
      };
    }""")
    assert colors["hover"] == "rgba(0, 0, 0, 0)"
    assert colors["buffer"] != "rgba(0, 0, 0, 0)"

    # Focus/keyboard input leaves the real pointer over the seek bar.
    player.focus()
    player.press("t")
    panel = player.locator(".movi-timeline-panel")
    expect(panel.locator(".movi-timeline-chapter")).to_have_count(24)
    page.wait_for_function("""()=>{const imgs=document.querySelector('movi-player')
      ?.shadowRoot.querySelectorAll('.movi-timeline-chapter img');
      return [...(imgs||[])].filter(img=>img.complete&&img.naturalWidth).length>=2;
    }""")
    hover_calls = player.evaluate("el=>el.__chapterHoverCalls")
    box = progress.bounding_box()
    assert box
    progress.hover(position={"x": box["width"] * 0.8, "y": box["height"] / 2})
    page.wait_for_timeout(250)
    assert player.evaluate("el=>el.__chapterHoverCalls") == hover_calls

    # Idle movement is suppressed; a held mouse drag still reaches Movi's scrubber.
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] / 2)
    page.mouse.up()
    page.wait_for_function("""()=>{const el=document.querySelector('movi-player');
      return el&&!el.seeking&&Math.abs(el.currentTime-el.duration*.6)<.25;
    }""")
    assert player.evaluate("el=>el.paused")
    expect(panel).to_be_visible()
    panel.locator(".movi-timeline-close").click()
    player.hover()
    page.wait_for_timeout(350)
    box = progress.bounding_box()
    assert box
    progress.hover(position={"x": box["width"] * 0.8, "y": box["height"] / 2})
    expect(hover).to_be_visible(timeout=30000)
    page.wait_for_function("""()=>{const img=document.querySelector('movi-player')
      ?.shadowRoot.querySelector('.movi-thumbnail-img');
      return img?.complete&&img.naturalWidth>0;
    }""")
    assert _has_pixels(hover)
