"""Startup must not wait for the unused remainder of a source response."""

import json

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


@pytest.mark.parametrize(
    "route,chunk,heading",
    [
        ("connections", "SettingsManagement", "Storage Clients"),
        ("storage", "StorageView", "MongoDB catalog"),
        ("performance", "PerformancePanel", "TGDrive library"),
    ],
)
def test_owner_tools_load_only_when_opened(server, desktop, route, chunk, heading):
    _, page = desktop
    requested = []
    page.on("request", lambda request: requested.append(request.url))
    page.goto(server.url)
    expect(page.locator("#login-dialog")).to_be_visible()
    owner_chunks = (
        "SettingsManagement-",
        "StorageView-",
        "PerformancePanel-",
        "ChannelBackup-",
    )
    assert not any(any(name in url for name in owner_chunks) for url in requested)
    login(page)
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    assert not any(any(name in url for name in owner_chunks) for url in requested)

    page.goto(server.url + "/?view=" + route)
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    assert any("/assets/" + chunk + "-" in url for url in requested)
    page.reload()
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()


def test_owner_tool_chunk_failure_offers_reload_without_losing_route(server, desktop):
    _, page = desktop
    page.route("**/assets/SettingsManagement-*.js", lambda route: route.abort("failed"))
    login(page, server.url + "/?view=connections")
    expect(page.get_by_text("This page could not load.", exact=False)).to_be_visible()
    page.unroute("**/assets/SettingsManagement-*.js")
    with page.expect_navigation():
        page.get_by_role("button", name="Reload Page", exact=True).click()
    expect(
        page.get_by_role("heading", name="Storage Clients", exact=True)
    ).to_be_visible()
    assert page.url.endswith("?view=connections")
