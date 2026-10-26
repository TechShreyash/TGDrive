"""Real document picture-in-picture remains styled under production CSP."""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    Server,
    expect,
    login,
    preview_dialog,
)

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def _watch_csp(page: Any) -> None:
    page.add_init_script("""window.pipCspViolations=[];
      document.addEventListener('securitypolicyviolation',event=>{
        window.pipCspViolations.push(event.violatedDirective+':'+event.blockedURI);
      });
      window.documentPictureInPicture?.addEventListener('enter',event=>{
        event.window.document.addEventListener('securitypolicyviolation',violation=>{
          window.pipCspViolations.push(violation.violatedDirective+':'+violation.blockedURI);
        });
      });""")


def _open_pip(context: Any, player: Any) -> Any:
    player.hover()
    with context.expect_page() as result:
        player.get_by_role("button", name="Picture in Picture", exact=True).click()
    pip = result.value
    pip.set_viewport_size({"width": 400, "height": 300})
    expect(pip.get_by_role("slider", name="Video position", exact=True)).to_be_visible()
    pip.locator("body").hover()
    return pip


def _assert_pip_layout(pip: Any) -> None:
    metrics = pip.evaluate("""()=>({
      margin:getComputedStyle(document.body).margin,
      controlsPosition:getComputedStyle(document.querySelector('.pip-controls')).position,
      inlineStyles:document.querySelectorAll('style').length,
      sheets:document.adoptedStyleSheets.length,
      overflow:document.documentElement.scrollWidth>innerWidth,
      buttons:[...document.querySelectorAll('button')].map(button=>{
        const rect=button.getBoundingClientRect();
        const hit=document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2);
        return {label:button.getAttribute('aria-label'),
          width:rect.width,height:rect.height,
          bounded:rect.left>=0&&rect.right<=innerWidth&&rect.top>=0&&rect.bottom<=innerHeight,
          reachable:hit===button||button.contains(hit)};
      })
    })""")
    assert metrics["margin"] == "0px"
    assert metrics["controlsPosition"] == "absolute"
    assert metrics["inlineStyles"] == 0
    assert metrics["sheets"] >= 3
    assert not metrics["overflow"]
    assert len(metrics["buttons"]) == 5
    assert all(
        button["label"]
        and button["width"] >= 44
        and button["height"] >= 44
        and button["bounded"]
        and button["reachable"]
        for button in metrics["buttons"]
    ), metrics


def test_document_pip_styles_controls_subtitles_and_canvas_restore(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    context, page = desktop
    _watch_csp(page)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    player.get_by_role("button", name="Subtitles/Captions", exact=True).click()
    track = player.evaluate("element=>element.player.getSubtitleTracks()[0].id")
    player.locator(f'.movi-subtitle-track-item[data-track-id="{track}"]').click()
    player.locator("button.movi-play-pause").click()
    expect(player.locator(".movi-subtitle-line").first).to_contain_text(
        "TGDrive subtitle", timeout=15000
    )
    player.locator("button.movi-play-pause").click()
    pip = _open_pip(context, player)
    _assert_pip_layout(pip)
    subtitle = pip.locator(".movi-subtitle-line").first
    expect(subtitle).to_contain_text("TGDrive subtitle")
    assert subtitle.evaluate("element=>getComputedStyle(element).display") == "block"
    assert subtitle.evaluate("element=>getComputedStyle(element).color") == (
        "rgb(255, 255, 255)"
    )
    pip.screenshot(path=ARTIFACTS / "document-pip-styled.png")

    # Pointer and keyboard controls operate the same real playback engine.
    pip.get_by_role("button", name="Play", exact=True).click()
    page.wait_for_function("()=>!document.querySelector('movi-player').paused")
    pip.get_by_role("button", name="Pause", exact=True).click()
    page.wait_for_function("()=>document.querySelector('movi-player').paused")
    mute = pip.get_by_role("button", name="Mute", exact=True)
    mute.focus()
    mute.press("Space")
    assert player.evaluate("element=>element.muted")
    assert player.evaluate("element=>element.paused")
    pip.get_by_role("button", name="Unmute", exact=True).press("Enter")
    assert not player.evaluate("element=>element.muted")
    assert player.evaluate("element=>element.paused")
    slider = pip.get_by_role("slider", name="Video position", exact=True)
    slider.click(position={"x": 150, "y": 8})
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>1")
    slider.focus()
    slider.press("Home")
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime<0.1")
    slider.press("ArrowRight")
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime>4")
    assert player.evaluate("element=>element.paused")
    slider.press("Home")
    page.wait_for_function("()=>document.querySelector('movi-player').currentTime<0.1")
    with pip.expect_event("close"):
        pip.get_by_role("button", name="Back to video", exact=True).press("Space")
    expect(player).to_be_focused()
    assert player.evaluate("element=>element.paused")
    assert player.evaluate("element=>element.canvas.getRootNode()===element.shadowRoot")
    assert page.evaluate("window.pipCspViolations") == []

    # Reopening keeps the adapter; closing the preview also closes its PiP window.
    reopened = _open_pip(context, player)
    _assert_pip_layout(reopened)
    with reopened.expect_event("close"):
        dialog.get_by_role("button", name="Close Preview", exact=True).click()
    expect(dialog).to_have_count(0)
    assert page.evaluate("window.pipCspViolations") == []


def test_document_pip_preserves_custom_mp4_caption_styles(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    context, page = desktop
    _watch_csp(page)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="movtext-subtitles.mp4", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    player.get_by_role("button", name="Subtitles/Captions", exact=True).click()
    track = player.evaluate("element=>element.player.getSubtitleTracks()[0].id")
    player.locator(f'.movi-subtitle-track-item[data-track-id="{track}"]').click()
    player.locator("button.movi-play-pause").click()
    expect(player.locator(".tgdrive-movtext-cue")).to_be_visible(timeout=15000)
    player.locator("button.movi-play-pause").click()
    pip = _open_pip(context, player)
    cue = pip.locator(".tgdrive-movtext-cue")
    expect(cue).to_be_visible()
    assert cue.evaluate("element=>getComputedStyle(element).color") == (
        "rgb(255, 255, 255)"
    )
    assert cue.evaluate("element=>getComputedStyle(element).backgroundColor") == (
        "rgba(8, 10, 13, 0.84)"
    )
    assert (
        pip.locator(".tgdrive-movtext-renderer").evaluate(
            "element=>getComputedStyle(element).position"
        )
        == "absolute"
    )
    assert (
        cue.bounding_box()["y"] + cue.bounding_box()["height"]
        <= (pip.locator(".pip-controls").bounding_box()["y"])
    )
    assert page.evaluate("window.pipCspViolations") == []
    pip.screenshot(path=ARTIFACTS / "document-pip-mp4-captions.png")
    with pip.expect_event("close"):
        pip.get_by_role("button", name="Back to video", exact=True).click()
    expect(player.locator(".tgdrive-movtext-cue")).to_be_visible()


def test_closing_preview_during_pip_request_restores_api_and_closes_child(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    context, page = desktop
    _watch_csp(page)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    # Delay delivery of a real native child window, keeping the browser gesture.
    page.evaluate("""()=>{
      const api=window.documentPictureInPicture,request=api.requestWindow;
      window.delayedPipRequest=function(options){
        return request.call(api,options).then(child=>new Promise(resolve=>{
          window.releasePipRequest=()=>resolve(child);
        }));
      };
      api.requestWindow=window.delayedPipRequest;
    }""")
    player.hover()
    with context.expect_page() as result:
        player.get_by_role("button", name="Picture in Picture", exact=True).click()
    child = result.value
    page.wait_for_function("()=>!!window.releasePipRequest")
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    expect(dialog).to_have_count(0)
    assert page.evaluate("""()=>window.documentPictureInPicture.requestWindow===
      window.delayedPipRequest""")
    with child.expect_event("close"):
        page.evaluate("window.releasePipRequest()")
    expect(page.locator("movi-player")).to_have_count(0)
    assert page.evaluate("window.pipCspViolations") == []
