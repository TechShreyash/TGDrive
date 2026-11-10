"""Owner positions cross browser contexts; public progress stays device-local."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin

import pytest

from tests.browser.test_browser_demo_access import (
    FULL_PASSWORD,
    full_demo_server,  # noqa: F401 - pytest fixture
)
from tests.browser.test_browser_e2e import (
    Server,
    expect,
    login,
    open_admin,
    preview_dialog,
)

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def _open_video(page: Any, name: str = "multitrack.mkv") -> tuple[Any, Any, str]:
    file = page.get_by_role("button", name=name, exact=True)
    item_id = file.locator("xpath=ancestor::*[@data-item-id][1]").get_attribute(
        "data-item-id"
    )
    assert item_id
    file.press("Enter")
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    return dialog, player, item_id


def _position(page: Any, server: Server, item_id: str):
    response = page.request.get(server.url + f"/api/v1/files/{item_id}/video-progress")
    assert response.ok, response.text()
    return response.json()["progress"]


def _wait_saved(page: Any, item_id: str, seconds: float | None) -> None:
    deadline = time.monotonic() + 15
    value = None
    while time.monotonic() < deadline:
        response = page.request.get(
            urljoin(page.url, f"/api/v1/files/{item_id}/video-progress")
        )
        assert response.ok, response.text()
        value = response.json()["progress"]
        if value is not None and (
            value["position_seconds"] > 0
            if seconds is None
            else abs(value["position_seconds"] - seconds) < 0.1
        ):
            return
        time.sleep(0.1)
    pytest.fail(f"Saved position did not reach {seconds}: {value}")


def _seek(page: Any, player: Any, seconds: float) -> None:
    player.evaluate(
        "async (el,time)=>{await el.pause();await el.player.seek(time)}", seconds
    )
    page.wait_for_function(
        """time=>{const el=document.querySelector('movi-player');
      return el&&!el.seeking&&el.paused&&Math.abs(el.currentTime-time)<0.1}
    """,
        arg=seconds,
    )


def _save_seek(page: Any, player: Any, item_id: str, seconds: float = 1.5) -> None:
    player.evaluate("el=>el.play()")
    page.wait_for_function("()=>document.querySelector('movi-player')?.currentTime>0.1")
    _seek(page, player, seconds)
    _wait_saved(page, item_id, seconds)


def _samples(page: Any, server: Server) -> None:
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")


def test_continue_survives_close_and_reload_without_seeking_automatically(
    server, desktop
):
    _, page = desktop
    _samples(page, server)
    file = page.get_by_role("button", name="multitrack.mkv", exact=True)
    item_id = file.locator("xpath=ancestor::*[@data-item-id][1]").get_attribute(
        "data-item-id"
    )
    legacy = "tgdrive.video.position.v1:" + item_id
    page.evaluate("key=>localStorage.setItem(key,'3')", legacy)
    writes = []
    page.on(
        "request",
        lambda request: (
            writes.append(request)
            if "/video-progress" in request.url and request.method in {"POST", "PUT"}
            else None
        ),
    )
    dialog, player, item_id = _open_video(page)
    expect(
        dialog.get_by_role("button", name="Continue from 0:03", exact=True)
    ).to_have_count(0)
    assert writes == []  # Loading/poster seeks do not acquire a writer.
    _seek(page, player, 0.5)
    assert writes == []  # A paused user seek also acquires no server writer.
    _save_seek(page, player, item_id)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    _wait_saved(page, item_id, 1.5)
    before = _position(page, server, item_id)
    before_writes = len(writes)
    dialog, player, _ = _open_video(page)
    expect(
        dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_be_visible()
    assert player.evaluate("el=>el.paused&&el.currentTime<0.1")
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    assert _position(page, server, item_id) == before
    assert len(writes) == before_writes
    assert page.evaluate("key=>localStorage.getItem(key)", legacy) == "3"
    _open_video(page)
    page.reload()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(
        dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_be_visible()
    assert player.evaluate("el=>el.paused&&el.currentTime<0.1")
    dialog.get_by_role("button", name="Continue from 0:01", exact=True).click()
    page.wait_for_function("""()=>{const el=document.querySelector('movi-player');
      return el&&!el.paused&&!el.seeking&&el.currentTime>=1.4}""")
    expect(
        dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_have_count(0)


def test_native_play_starts_at_beginning_and_completion_clears_position(
    server, desktop
):
    _, page = desktop
    _samples(page, server)
    dialog, player, item_id = _open_video(page)
    _save_seek(page, player, item_id)
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
    page.wait_for_function("""()=>{const el=document.querySelector('movi-player');
      return el&&!el.paused&&el.currentTime>0.1&&el.currentTime<1.5}""")
    page.wait_for_function(
        "()=>document.querySelector('movi-player')?.dataset.finished==='true'",
        timeout=30000,
    )
    _wait_saved(page, item_id, 0)
    player.evaluate(
        "el=>{window.finishedPlayer=el;el.dispatchEvent(new Event('error'))}"
    )
    dialog.get_by_role("button", name="Try again", exact=True).click()
    page.wait_for_function("""()=>{const el=document.querySelector('movi-player');
      return el&&el!==window.finishedPlayer&&el.duration>4
        &&!el.seeking&&el.paused&&el.currentTime<0.1}""")
    assert _position(page, server, item_id)["position_seconds"] == 0
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    dialog, _, _ = _open_video(page)
    expect(
        dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_have_count(0)


def test_same_owner_in_another_context_resumes_and_old_writer_cannot_replace_it(
    server, browser, desktop
):
    _, page = desktop
    _samples(page, server)
    dialog, player, item_id = _open_video(page)
    _save_seek(page, player, item_id)
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, reduced_motion="reduce"
    )
    other = context.new_page()
    try:
        _samples(other, server)
        other_dialog, other_player, other_id = _open_video(other)
        assert other_id == item_id
        expect(
            other_dialog.get_by_role("button", name="Continue from 0:01", exact=True)
        ).to_be_visible()
        assert (
            other.evaluate(
                "Object.keys(localStorage).filter(key=>key.startsWith('tgdrive.video.position'))"
            )
            == []
        )
        _save_seek(other, other_player, item_id, 2.5)
        _seek(page, player, 3)
        expect(
            dialog.get_by_text("Another playback session", exact=False)
        ).to_be_visible()
        dialog.get_by_role("button", name="Close Preview", exact=True).click()
        assert _position(other, server, item_id)["position_seconds"] == pytest.approx(
            2.5, abs=0.1
        )
        other_dialog.get_by_role("button", name="Close Preview", exact=True).click()
    finally:
        context.close()


def test_anonymous_share_never_reads_or_writes_owner_history(server, browser, desktop):
    _, page = desktop
    _samples(page, server)
    dialog, player, item_id = _open_video(page)
    _save_seek(page, player, item_id)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    share = page.request.post(
        server.url + "/api/v1/shares",
        data={"path": "/" + item_id, "label": "Resume viewer"},
    ).json()
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, reduced_motion="reduce"
    )
    other = context.new_page()
    progress_requests = []
    other.on(
        "request",
        lambda request: (
            progress_requests.append(request.url)
            if "/video-progress" in request.url
            else None
        ),
    )
    try:
        other.add_init_script(
            "localStorage.setItem('tgdrive.video.position.v1:" + item_id + "','3')"
        )
        url = server.url + "/embed?path=/" + item_id + "&auth=" + share["auth"]
        other.goto(url)
        expect(
            other.get_by_role("button", name="Continue from 0:03", exact=True)
        ).to_be_visible()
        local_player = other.locator("movi-player")
        _seek(other, local_player, 2.5)
        key = "tgdrive.video.position.v2:share:" + item_id
        other.wait_for_function(
            "key=>Math.abs(Number(localStorage.getItem(key))-2.5)<0.1", arg=key
        )
        other.reload()
        expect(
            other.get_by_role("button", name="Continue from 0:02", exact=True)
        ).to_be_visible()
        assert progress_requests == []
        assert _position(page, server, item_id)["position_seconds"] == pytest.approx(
            1.5, abs=0.1
        )
        assert (
            other.request.get(
                server.url + f"/api/v1/files/{item_id}/video-progress"
            ).status
            == 401
        )
        # The owner proves identity independently on the same share.
        owner_progress = []
        page.on(
            "request",
            lambda request: (
                owner_progress.append(request.url)
                if "/video-progress" in request.url
                else None
            ),
        )
        page.goto(url)
        expect(
            page.get_by_role("button", name="Continue from 0:01", exact=True)
        ).to_be_visible()
        assert owner_progress and all("auth=" not in url for url in owner_progress)
    finally:
        context.close()


@pytest.mark.parametrize("status", [401, 503])
def test_server_read_failure_does_not_fall_back_to_legacy_owner_storage(
    server, desktop, status
):
    _, page = desktop
    _samples(page, server)
    file = page.get_by_role("button", name="multitrack.mkv", exact=True)
    item_id = file.locator("xpath=ancestor::*[@data-item-id][1]").get_attribute(
        "data-item-id"
    )
    page.evaluate(
        "id=>localStorage.setItem('tgdrive.video.position.v1:'+id,'3')", item_id
    )
    page.route(
        "**/api/v1/files/*/video-progress",
        lambda route: route.fulfill(
            status=status, json={"detail": "Synthetic progress unavailable"}
        ),
    )
    dialog, _, _ = _open_video(page)
    expect(
        dialog.get_by_text("Saved video progress is unavailable.", exact=False)
    ).to_be_visible()
    expect(
        dialog.get_by_role("button", name="Continue from 0:03", exact=True)
    ).to_have_count(0)


def test_close_while_begin_response_is_pending_sends_one_owned_final_save(
    server, desktop
):
    _, page = desktop
    _samples(page, server)
    pending = []
    writes = []

    def hold_begin(route):
        pending.append((route, route.fetch()))

    page.route("**/api/v1/files/*/video-progress/session", hold_begin)
    page.on(
        "request",
        lambda request: (
            writes.append(request.post_data_json)
            if "/video-progress" in request.url and request.method == "PUT"
            else None
        ),
    )
    dialog, player, item_id = _open_video(page)
    player.evaluate("el=>el.play()")
    page.wait_for_function("()=>document.querySelector('movi-player')?.currentTime>0.2")
    assert len(pending) == 1
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    pending[0][0].fulfill(response=pending[0][1])
    _wait_saved(page, item_id, None)
    assert len(writes) == 1 and writes[0]["final"] is True
    assert _position(page, server, item_id)["position_seconds"] > 0


def test_delayed_final_save_cannot_replace_a_new_device_writer(
    server, browser, desktop
):
    _, page = desktop
    _samples(page, server)
    dialog, player, item_id = _open_video(page)
    _save_seek(page, player, item_id)
    pending = []

    def hold_final(route):
        if route.request.method == "PUT" and route.request.post_data_json["final"]:
            pending.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/files/*/video-progress", hold_final)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    # Wait for browser request delivery without advancing or guessing its timing.
    page.wait_for_function("()=>!document.querySelector('movi-player')")
    context = browser.new_context(
        viewport={"width": 1440, "height": 960}, reduced_motion="reduce"
    )
    other = context.new_page()
    try:
        _samples(other, server)
        _, other_player, _ = _open_video(other)
        _save_seek(other, other_player, item_id, 2.5)
        assert len(pending) == 1
        with page.expect_response(
            lambda response: (
                response.request.method == "PUT"
                and response.url.endswith("/video-progress")
                and response.status == 409
            )
        ) as rejected:
            pending[0].continue_()
        assert rejected.value.json()["code"] == "VIDEO_PROGRESS_SESSION_CHANGED"
        assert _position(other, server, item_id)["position_seconds"] == pytest.approx(
            2.5, abs=0.1
        )
    finally:
        context.close()


def test_late_position_response_does_not_replace_a_different_preview(server, desktop):
    _, page = desktop
    _samples(page, server)
    file = page.get_by_role("button", name="multitrack.mkv", exact=True)
    item_id = file.locator("xpath=ancestor::*[@data-item-id][1]").get_attribute(
        "data-item-id"
    )
    # A transport that ignores cancellation must still respect preview identity.
    page.evaluate("""()=>{
      const original=window.fetch;
      window.fetch=(url,options)=>String(url).endsWith('/video-progress')
        &&(!options?.method||options.method==='GET')
        ?original(url,{...options,signal:undefined}):original(url,options);
    }""")
    pending = []
    page.route(
        "**/api/v1/files/" + item_id + "/video-progress",
        lambda route: pending.append(route),
    )
    dialog, _, _ = _open_video(page)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    current, _, _ = _open_video(page, "movtext-subtitles.mp4")
    expect(
        current.get_by_text("Checking saved video position…", exact=True)
    ).to_have_count(0)
    assert len(pending) == 1
    pending[0].fulfill(
        json={
            "progress": {
                "position_seconds": 1.5,
                "duration_seconds": 5,
                "updated_at": 1000,
            }
        }
    )
    page.evaluate(
        """async()=>{
          await new Promise(requestAnimationFrame);
          await new Promise(requestAnimationFrame);
        }"""
    )
    expect(
        current.get_by_role("heading", name="movtext-subtitles.mp4", exact=True)
    ).to_be_visible()
    expect(
        current.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_have_count(0)


def test_read_only_demo_share_progress_is_not_imported_for_demo_admin(
    full_demo_server,  # noqa: F811 - pytest fixture shadows its import
    desktop,
):
    _, page = desktop
    server = full_demo_server
    page.goto(server.url)
    expect(page.locator("#login-dialog")).to_be_visible()
    page.get_by_label("Password", exact=True).fill(FULL_PASSWORD)
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.locator("#login-dialog")).not_to_be_visible()
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    file = page.get_by_role("button", name="multitrack.mkv", exact=True)
    item_id = file.locator("xpath=ancestor::*[@data-item-id][1]").get_attribute(
        "data-item-id"
    )
    share_response = page.request.post(
        server.url + "/api/v1/shares",
        data={"path": "/" + item_id, "label": "Demo reader"},
    )
    assert share_response.ok, share_response.text()
    share = share_response.json()
    open_admin(page)
    page.get_by_role("button", name="Log Out", exact=True).click()
    login(page)
    progress_requests = []
    page.on(
        "request",
        lambda request: (
            progress_requests.append(request.url)
            if "/video-progress" in request.url
            else None
        ),
    )
    page.goto(server.url + "/embed?path=/" + item_id + "&auth=" + share["auth"])
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    _seek(page, player, 1.5)
    key = "tgdrive.video.position.v2:demo:" + item_id
    page.wait_for_function(
        "key=>Math.abs(Number(localStorage.getItem(key))-1.5)<0.1", arg=key
    )
    assert (
        page.evaluate(
            "id=>localStorage.getItem('tgdrive.video.position.v2:share:'+id)", item_id
        )
        is None
    )
    assert progress_requests == []
    page.goto(server.url)
    # Switch the actual server-verified capability, preserving the same local profile.
    open_admin(page)
    page.get_by_role("button", name="Log Out", exact=True).click()
    expect(page.locator("#login-dialog")).to_be_visible()
    page.get_by_label("Password", exact=True).fill(FULL_PASSWORD)
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.locator("#login-dialog")).not_to_be_visible()
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    admin_dialog, admin_player, _ = _open_video(page)
    expect(
        admin_dialog.get_by_text("Checking saved video position…", exact=True)
    ).to_have_count(0)
    expect(
        admin_dialog.get_by_role("button", name="Continue from 0:01", exact=True)
    ).to_have_count(0)
    assert _position(page, server, item_id) is None
    _save_seek(page, admin_player, item_id)
    assert progress_requests
    assert page.evaluate("key=>Number(localStorage.getItem(key))", key) == 1.5


def test_unverified_share_session_does_not_publish_or_import_progress(server, desktop):
    _, page = desktop
    _samples(page, server)
    file = page.get_by_role("button", name="multitrack.mkv", exact=True)
    item_id = file.locator("xpath=ancestor::*[@data-item-id][1]").get_attribute(
        "data-item-id"
    )
    share = page.request.post(
        server.url + "/api/v1/shares",
        data={"path": "/" + item_id, "label": "Identity check"},
    ).json()
    page.evaluate(
        "id=>localStorage.setItem('tgdrive.video.position.v1:'+id,'3')", item_id
    )
    page.route(
        "**/api/v1/session", lambda route: route.fulfill(json={"authenticated": "true"})
    )
    progress_requests = []
    page.on(
        "request",
        lambda request: (
            progress_requests.append(request.url)
            if "/video-progress" in request.url
            else None
        ),
    )
    page.goto(server.url + "/embed?path=/" + item_id + "&auth=" + share["auth"])
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    notice = page.get_by_text("Video resume access could not be checked.", exact=False)
    expect(notice).to_be_visible()
    expect(
        page.get_by_role("button", name="Continue from 0:03", exact=True)
    ).to_have_count(0)
    player.evaluate("el=>el.play()")
    page.wait_for_function("()=>document.querySelector('movi-player')?.currentTime>0.2")
    expect(notice).to_be_visible()
    assert progress_requests == []
