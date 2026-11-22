"""Startup must not wait for the unused remainder of a source response."""

import json

import pytest

from tests.browser.test_browser_e2e import ARTIFACTS, Server, login, preview_dialog

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def test_video_plays_while_the_rest_of_its_response_is_held(server: Server, desktop):
    _, page = desktop
    source = server.control("/progressive-video", method="POST")
    page.route(
        "**/file?*",
        lambda route: route.continue_(url=server.url + "/__test__/progressive-video"),
    )
    posters = []
    page.route("**/api/v1/poster?*", lambda route: posters.append(route))
    try:
        login(page, server.url)
        page.get_by_role("button", name="Preview samples", exact=True).dblclick()
        with page.expect_request("**/api/v1/poster?*"):
            page.get_by_role(
                "button", name="movtext-subtitles.mp4", exact=True
            ).dblclick()
        assert posters
        dialog = preview_dialog(page)
        player = dialog.locator("movi-player")
        player.locator("button.movi-play-pause").click(timeout=3000)
        page.wait_for_function(
            """()=>{const p=document.querySelector('movi-player');
              return p?.currentTime>.3 && p.player?.hasPicture}""",
            timeout=15000,
        )
        assert player.evaluate("el=>!el.player.getSource().isFullyCached()")
        assert dialog.locator(".advanced-state[data-hidden='false']").count() == 0
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / "progressive-video.json").write_text(
            json.dumps(
                {
                    "source_bytes": source["size"],
                    "maximum_delivered_prefix": 2 * 1024**2,
                    "played_before_release": True,
                    "poster_response_held": True,
                }
            )
        )
    finally:
        server.control("/progressive-video?release=1", method="POST")
