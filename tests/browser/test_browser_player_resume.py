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
    ARTIFACTS,
    Server,
    expect,
    login,
    open_admin,
    preview_dialog,
    set_appearance,
)
from tests.browser.test_browser_file_views import choose_view

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


def _save_seek(page: Any, player: Any, item_id: str, seconds: float = 2.25) -> None:
    player.evaluate("el=>el.play()")
    page.wait_for_function("()=>document.querySelector('movi-player')?.currentTime>0.1")
    _seek(page, player, seconds)
    _wait_saved(page, item_id, seconds)


def _resumed_paused(page: Any, player: Any, seconds: float) -> None:
    duration = player.evaluate("el=>el.duration")
    target = seconds if 2 < seconds < duration - 2 else 0
    page.wait_for_function(
        """target=>{const el=document.querySelector('movi-player');
          return el&&!el.seeking&&el.paused&&Math.abs(el.currentTime-target)<0.1}""",
        arg=target,
    )
    expect(page.get_by_role("button", name="Continue from", exact=False)).to_have_count(
        0
    )


def _samples(page: Any, server: Server) -> None:
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")


def test_default_resume_survives_close_reload_and_waits_for_play(server, desktop):
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
            if request.url.endswith(("/video-progress", "/video-progress/session"))
            and request.method in {"POST", "PUT"}
            else None
        ),
    )
    dialog, player, item_id = _open_video(page)
    assert writes == []
    _seek(page, player, 0.5)
    assert writes == []
    _save_seek(page, player, item_id)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    _wait_saved(page, item_id, 2.25)
    before = _position(page, server, item_id)
    before_writes = len(writes)
    dialog, player, _ = _open_video(page)
    assert player.evaluate("el=>el.duration>4.25"), (
        "The fixture must have a resumable interior beyond both two-second margins"
    )
    _resumed_paused(page, player, 2.25)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    assert _position(page, server, item_id) == before
    assert len(writes) == before_writes
    assert page.evaluate("key=>localStorage.getItem(key)", legacy) == "3"
    _open_video(page)
    page.reload()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    _resumed_paused(page, player, 2.25)
    player.hover()
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("""()=>{const el=document.querySelector('movi-player');
      return el&&!el.paused&&!el.seeking&&el.currentTime>=2.2}""")
    expect(dialog.get_by_text("Resumed at 0:02", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Start over", exact=True).click()
    page.wait_for_function("""()=>{const el=document.querySelector('movi-player');
      return el&&!el.seeking&&el.currentTime<2}""")
    _wait_saved(page, item_id, 0)


def test_pagehide_then_play_creates_one_replacement_writer_and_closes_it(
    server, desktop
):
    _, page = desktop
    _samples(page, server)
    writes = []
    page.on(
        "request",
        lambda request: (
            writes.append((request.method, request.post_data_json))
            if request.url.endswith(("/video-progress", "/video-progress/session"))
            and request.method in {"POST", "PUT"}
            else None
        ),
    )
    dialog, player, item_id = _open_video(page)

    def is_final_save(response):
        return (
            response.request.method == "PUT"
            and response.url.endswith("/video-progress")
            and response.request.post_data_json["final"] is True
        )

    _save_seek(page, player, item_id)
    with page.expect_response(is_final_save) as hidden_save:
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    assert hidden_save.value.ok
    _wait_saved(page, item_id, 2.25)
    _save_seek(page, player, item_id, 2.5)
    with page.expect_response(is_final_save) as closing_save:
        dialog.get_by_role("button", name="Close Preview", exact=True).click()
    assert closing_save.value.ok
    _wait_saved(page, item_id, 2.5)
    page.wait_for_function("()=>!document.querySelector('movi-player')")
    assert sum(method == "POST" for method, _ in writes) == 2
    assert sum(method == "PUT" and body["final"] for method, body in writes) == 2
    _, player, _ = _open_video(page)
    _resumed_paused(page, player, 2.5)


def test_normal_play_resumes_and_completion_clears_position(server, desktop):
    _, page = desktop
    _samples(page, server)
    dialog, player, item_id = _open_video(page)
    _save_seek(page, player, item_id)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    dialog, player, _ = _open_video(page)
    _resumed_paused(page, player, 2.25)
    player.evaluate(
        "el=>el.addEventListener('ended',()=>{el.dataset.finished='true'},{once:true})"
    )
    player.hover()
    player.locator("button.movi-play-pause").click()
    page.wait_for_function("""()=>{const el=document.querySelector('movi-player');
      return el&&!el.paused&&el.currentTime>=2.2}""")
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
        _resumed_paused(other, other_player, 2.25)
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
            "localStorage.setItem('tgdrive.video.position.v1:" + item_id + "','2.25')"
        )
        url = server.url + "/embed?path=/" + item_id + "&auth=" + share["auth"]
        other.goto(url)
        local_player = other.locator("movi-player")
        _resumed_paused(other, local_player, 2.25)
        _seek(other, local_player, 2.5)
        key = "tgdrive.video.position.v2:share:" + item_id
        other.wait_for_function(
            "key=>Math.abs(Number(localStorage.getItem(key))-2.5)<0.1", arg=key
        )
        other.reload()
        _resumed_paused(other, other.locator("movi-player"), 2.5)
        assert progress_requests == []
        assert _position(page, server, item_id)["position_seconds"] == pytest.approx(
            2.25, abs=0.1
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
        _resumed_paused(page, page.locator("movi-player"), 2.25)
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
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
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
        context.tracing.stop(path=ARTIFACTS / "video-delayed-final-second-context.zip")
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
    current, current_player, _ = _open_video(page, "movtext-subtitles.mp4")
    expect(
        current.get_by_text("Checking saved video position…", exact=True)
    ).to_have_count(0)
    assert len(pending) == 1
    pending[0].fulfill(
        json={
            "progress": {
                "position_seconds": 2.25,
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
    _resumed_paused(page, current_player, 0)


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


def _seed_saved_position(page, server):
    dialog, player, identity = _open_video(page)
    duration = player.evaluate("el=>el.duration")
    assert duration > 4.25
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    url = server.url + f"/api/v1/files/{identity}/video-progress"
    session = page.request.post(url + "/session", data={})
    assert session.ok, session.text()
    response = page.request.put(
        url,
        data={
            "session_id": session.json()["session_id"],
            "sequence": 1,
            "position_seconds": 2.25,
            "duration_seconds": duration,
            "final": True,
        },
    )
    assert response.ok, response.text()
    return identity, response.json()["progress"]


@pytest.mark.parametrize("choice", ["Continue from 0:02", "Keep playing from here"])
def test_play_before_saved_read_preserves_bookmark_until_explicit_choice(
    server, desktop, choice
):
    _, page = desktop
    _samples(page, server)
    identity, saved = _seed_saved_position(page, server)
    pending, writes = [], []

    def hold_read(route):
        if route.request.method == "GET":
            pending.append(route)
        else:
            writes.append(route.request)
            route.continue_()

    page.route("**/api/v1/files/*/video-progress", hold_read)
    dialog, player, _ = _open_video(page)
    player.evaluate("el=>el.play()")
    page.wait_for_function(
        """()=>{const el=document.querySelector('movi-player');
          return el&&!el.seeking&&!el.paused&&el.currentTime>0.2}"""
    )
    assert _position(page, server, identity) == saved
    assert writes == []
    player.evaluate(
        "el=>{window.lateResumeSeeks=0;el.addEventListener('seeking',()=>window.lateResumeSeeks++)}"
    )
    assert pending
    pending.pop().fulfill(json={"progress": saved})
    expect(
        dialog.get_by_role("button", name="Continue from 0:02", exact=True)
    ).to_be_visible()
    expect(
        dialog.get_by_role("button", name="Keep playing from here", exact=True)
    ).to_be_visible()
    assert page.evaluate("window.lateResumeSeeks") == 0
    assert _position(page, server, identity) == saved
    with page.expect_response(
        lambda response: (
            response.request.method == "PUT"
            and response.url.endswith("/video-progress")
        )
    ) as response:
        dialog.get_by_role("button", name=choice, exact=True).click()
    assert response.value.ok, response.value.text()
    expect(
        dialog.get_by_role("button", name="Keep playing from here", exact=True)
    ).to_have_count(0)
    if choice.startswith("Continue"):
        assert page.evaluate("window.lateResumeSeeks") > 0


@pytest.mark.parametrize("result", ["empty", "error", "manual"])
def test_pending_progress_null_error_retry_and_manual_seek_remain_safe(
    server, desktop, result
):
    _, page = desktop
    _samples(page, server)
    identity, saved = _seed_saved_position(page, server)
    pending = []

    def hold_read(route):
        if route.request.method == "GET":
            pending.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/files/*/video-progress", hold_read)
    dialog, player, _ = _open_video(page)
    player.evaluate("el=>el.play()")
    page.wait_for_function("()=>document.querySelector('movi-player')?.currentTime>0.2")
    assert pending
    if result == "manual":
        _seek(page, player, 0.75)
        _wait_saved(page, identity, 0.75)
        pending.pop().fulfill(json={"progress": saved})
        expect(
            dialog.get_by_role("button", name="Keep playing from here", exact=True)
        ).to_have_count(0)
        assert player.evaluate("el=>el.paused&&Math.abs(el.currentTime-.75)<.1")
    elif result == "empty":
        with page.expect_response(
            lambda response: (
                response.request.method == "PUT"
                and response.url.endswith("/video-progress")
            )
        ) as response:
            pending.pop().fulfill(json={"progress": None})
        assert response.value.ok
        expect(
            dialog.get_by_role("button", name="Keep playing from here", exact=True)
        ).to_have_count(0)
    else:
        pending.pop().fulfill(status=503, json={"detail": "Synthetic read unavailable"})
        expect(
            dialog.get_by_text("your saved position is unchanged", exact=False)
        ).to_be_visible()
        assert _position(page, server, identity) == saved
        with page.expect_request(
            lambda request: (
                request.method == "GET" and request.url.endswith("/video-progress")
            )
        ):
            dialog.get_by_role(
                "button", name="Retry saved position", exact=True
            ).click()
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert pending
        pending.pop().fulfill(json={"progress": saved})
        expect(
            dialog.get_by_role("button", name="Keep playing from here", exact=True)
        ).to_be_visible()
        assert _position(page, server, identity) == saved


def test_resumed_notice_waits_while_start_over_has_keyboard_focus(server, desktop):
    _, page = desktop
    _samples(page, server)
    _seed_saved_position(page, server)
    dialog, player, _ = _open_video(page)
    _resumed_paused(page, player, 2.25)
    player.evaluate("el=>el.play()")
    start_over = dialog.get_by_role("button", name="Start over", exact=True)
    expect(start_over).to_be_visible()
    player.evaluate("el=>el.pause()")
    start_over.focus()
    page.clock.install()
    page.clock.fast_forward(11000)
    expect(start_over).to_be_focused()
    start_over.press("Tab")
    page.clock.fast_forward(11000)
    expect(start_over).to_have_count(0)


@pytest.mark.parametrize("layout", ["Grid view", "List view"])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("new_position", [90, 0])
def test_thumbnail_position_uses_one_batch_and_keeps_newer_confirmed_event(
    server, desktop, layout, new_position, theme
):
    _, page = desktop
    held, media = [], []
    page.route("**/api/v1/video-progress/summaries", lambda route: held.append(route))
    page.on(
        "request",
        lambda request: media.append(request.url) if "/file?" in request.url else None,
    )
    login(page, server.url)
    set_appearance(page, theme)
    choose_view(page, layout)
    with page.expect_request("**/api/v1/video-progress/summaries"):
        page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    file = page.get_by_role("button", name="multitrack.mkv", exact=True)
    card = file.locator("xpath=ancestor::*[@data-item-id][1]")
    identity = card.get_attribute("data-item-id")
    assert identity
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(held) == 1
    query = held[0].request.post_data_json
    assert identity in query["file_ids"] and len(query["file_ids"]) <= 128
    expect(card.locator(".file-playback-progress")).to_have_count(0)
    height = card.bounding_box()["height"]
    page.evaluate(
        """({id,value})=>window.dispatchEvent(
        new CustomEvent('tgdrive:video-position-changed',{
      detail:{fileId:id,mode:'server',position:{position_seconds:value,duration_seconds:120,updated_at:100}}
    }))""",
        {"id": identity, "value": new_position},
    )
    meter = card.get_by_role(
        "meter", name="Saved playback position for multitrack.mkv", exact=True
    )
    if new_position:
        expect(meter).to_have_attribute("value", "90")
        expect(meter).to_have_attribute("max", "120")
        expect(meter).to_have_attribute("aria-valuetext", "1:30 of 2:00")
    else:
        expect(meter).to_have_count(0)
    assert card.bounding_box()["height"] == pytest.approx(height)
    with page.expect_response("**/api/v1/video-progress/summaries") as response:
        held.pop().fulfill(
            json={
                "progress": {
                    identity: {
                        "position_seconds": 30,
                        "duration_seconds": 120,
                        "updated_at": 100,
                    }
                }
            }
        )
    response.value.finished()
    page.evaluate(
        """() => new Promise(resolve =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
    )
    if new_position:
        expect(meter).to_have_attribute("value", "90")
        expect(card.locator(".file-playback-progress > span")).to_have_attribute(
            "style", "width: 75%;"
        )
        contrast = card.locator(".file-playback-progress").evaluate("""rail => {
          const luminance = color => {
            const rgb = color.match(/[\\d.]+/g).slice(0,3).map(Number)
              .map(value => value / 255)
              .map(value => value <= .04045 ? value / 12.92
                : ((value + .055) / 1.055) ** 2.4);
            return rgb.reduce((sum,value,index) =>
              sum + value * [.2126,.7152,.0722][index],0);
          };
          const style = getComputedStyle(rail);
          const track = style.backgroundColor;
          const fillStyle = getComputedStyle(rail.firstElementChild);
          const fill = fillStyle.backgroundColor;
          return {ratio:(luminance(fill)+.05)/(luminance(track)+.05),track,fill,
                height:rail.getBoundingClientRect().height,
                cssHeight:parseFloat(style.height),
                left:parseFloat(style.left),right:parseFloat(style.right),
                bottom:parseFloat(style.bottom),radius:parseFloat(style.borderRadius),
                fillRadius:parseFloat(fillStyle.borderRadius),shadow:style.boxShadow,
                overflow:style.overflow};
        }""")
        assert contrast["track"] == "rgb(24, 27, 34)"
        assert contrast["fill"] == "rgb(122, 167, 255)"
        assert contrast["ratio"] >= 7
        inset = 4 if layout == "List view" else 8
        assert contrast["left"] == inset
        assert contrast["right"] == inset
        assert contrast["bottom"] == (4 if layout == "List view" else 7)
        assert contrast["radius"] >= 3
        assert contrast["fillRadius"] >= 3
        assert contrast["shadow"] == "none"
        assert contrast["overflow"] == "hidden"
        thickness = 4 if layout == "List view" else 5
        assert contrast["cssHeight"] == thickness
        assert contrast["height"] == pytest.approx(thickness, abs=0.15)
        page.screenshot(
            path=ARTIFACTS / f"thumbnail-position-{theme}-{layout.split()[0]}.png",
            full_page=True,
        )
    else:
        expect(meter).to_have_count(0)
        expect(card.locator(".file-playback-progress")).to_have_count(0)
    assert media == []


def test_public_thumbnails_ignore_owner_cookie_and_owner_save_events(server, desktop):
    _, page = desktop
    login(page, server.url)
    folder = page.get_by_role("button", name="Preview samples", exact=True)
    folder_id = folder.locator("xpath=ancestor::*[@data-item-id][1]").get_attribute(
        "data-item-id"
    )
    assert folder_id
    folder.press("Enter")
    identity, saved = _seed_saved_position(page, server)
    response = page.request.post(
        server.url + "/api/v1/shares", data={"path": "/" + folder_id}
    )
    assert response.ok
    token = response.json()["auth"]
    page.evaluate(
        """({id,duration})=>{
      localStorage.setItem('tgdrive.video.position.v2:share:'+id,'1');
      localStorage.setItem('tgdrive.video.summaries.v1:share',JSON.stringify({
        [id]:{position_seconds:1,duration_seconds:duration,updated_at:100}
      }));
    }""",
        {"id": identity, "duration": saved["duration_seconds"]},
    )
    batches = []
    page.on(
        "request",
        lambda request: (
            batches.append(request.url)
            if "/video-progress/summaries" in request.url
            else None
        ),
    )
    page.goto(server.url + "/?path=/share_" + folder_id + "&auth=" + token)
    card = page.locator('[data-item-id="' + identity + '"]')
    meter = card.get_by_role(
        "meter", name="Saved playback position for multitrack.mkv", exact=True
    )
    expect(meter).to_have_attribute("value", "1")
    page.evaluate(
        """({id,duration})=>window.dispatchEvent(
        new CustomEvent('tgdrive:video-position-changed',{
      detail:{fileId:id,mode:'server',position:{position_seconds:2.25,duration_seconds:duration,updated_at:101}}
    }))""",
        {"id": identity, "duration": saved["duration_seconds"]},
    )
    expect(meter).to_have_attribute("value", "1")
    assert batches == []
