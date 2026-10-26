"""Manual whole-file preload discovery, actual caching and recoverable errors."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium tests", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, expect, login

browser = fixtures.browser
desktop = fixtures.desktop
server = fixtures.server
pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("filename", ["multitrack.mkv", "sample.wav"])
def test_media_playback_identifies_preview_but_original_download_does_not(
    server, desktop, filename
):
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    with page.expect_response(
        lambda response: (
            "/file?" in response.url
            and parse_qs(urlsplit(response.url).query).get("preview") == ["1"]
            and response.status in {200, 206}
        )
    ) as playback:
        page.get_by_role("button", name=filename, exact=True).dblclick()
    playback_params = parse_qs(urlsplit(playback.value.url).query)
    assert "download" not in playback_params
    original = page.get_by_role("link", name="Download Original", exact=True)
    download_params = parse_qs(urlsplit(original.get_attribute("href")).query)
    assert download_params["download"] == ["1"]
    assert "preview" not in download_params
    assert download_params["path"] == playback_params["path"]
    with page.expect_download() as downloaded:
        original.click()
    assert downloaded.value.suggested_filename == filename
    assert downloaded.value.failure() is None


def open_preload(page):
    page.get_by_label("Actions for archive.bin", exact=True).click()
    action = page.get_by_role("menuitem", name="Preload file", exact=True)
    expect(action).to_be_visible()
    action.click()
    return page.get_by_role("dialog", name="Preload file", exact=True)


@pytest.mark.parametrize("width", [1440, 390])
def test_file_menu_preloads_whole_file_and_reuses_cached_bytes(server, desktop, width):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    directory_url = page.url
    dialog = open_preload(page)
    expect(dialog).to_contain_text("Cache the whole file on the server")
    expect(dialog).to_contain_text("requested chunks")
    expect(dialog).to_contain_text("bounded read-ahead")
    expect(dialog).to_contain_text("internet connection")
    expect(dialog.get_by_role("link", name="Preloads", exact=True)).to_have_attribute(
        "href", "/?view=preloads"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / f"preload-file-dialog-{width}.png")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/api/v1/preloads")
        )
    ) as response:
        dialog.get_by_role("button", name="Preload file", exact=True).click()
    assert response.value.ok, response.value.text()
    job = response.value.json()
    assert job["mode"] == "manual"
    assert job["target_bytes"] == job["size"] == 256
    assert job["start"] == 0 and job["end"] == 256
    expect(dialog).not_to_be_visible()
    page.goto(server.url + "/?view=preloads")
    help_panel = page.locator(".preload-help")
    expect(help_panel).not_to_have_attribute("open", "")
    help_panel.get_by_text("About preloading", exact=True).click()
    expect(help_panel).to_contain_text("Preload file")
    expect(help_panel).to_contain_text("downloads fetch their requested ranges")
    help_panel.get_by_text("About preloading", exact=True).click()
    cached = page.locator(".cached-file").filter(has_text="archive.bin")
    expect(cached.locator(".cache-ready-state")).to_have_text("Ready", timeout=15000)
    expect(cached.locator(".cache-coverage")).to_contain_text("256 / 256 B cached")
    expect(cached.locator(".cache-coverage-ready")).to_have_attribute("width", "1000")
    page.screenshot(path=ARTIFACTS / f"preload-file-ready-{width}.png")
    page.goto(directory_url)
    dialog = open_preload(page)
    dialog.get_by_role("button", name="Preload file", exact=True).click()
    expect(
        page.get_by_text("Preload queued. View progress in Preloads.", exact=True)
    ).to_be_visible()
    expect(dialog).not_to_be_visible()
    page.goto(server.url + "/?view=preloads")
    expect(cached.locator(".cache-ready-state")).to_have_text("Ready")


def test_preload_capacity_error_keeps_dialog_open_and_allows_retry(server, desktop):
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    attempts = []

    def capacity_once(route):
        if route.request.method != "POST":
            route.continue_()
            return
        attempts.append(route.request.post_data_json)
        if len(attempts) == 1:
            route.fulfill(
                status=507,
                json={
                    "detail": (
                        "Increase cache capacity or clear cached files, then retry."
                    ),
                    "code": "PRELOAD_CAPACITY",
                },
            )
        else:
            route.continue_()

    page.route("**/api/v1/preloads", capacity_once)
    dialog = open_preload(page)
    dialog.get_by_role("button", name="Preload file", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("Increase cache capacity")
    expect(
        dialog.get_by_role("button", name="Preload file", exact=True)
    ).to_be_enabled()
    expect(dialog.get_by_role("link", name="Preloads", exact=True)).to_be_visible()
    page.screenshot(path=ARTIFACTS / "preload-file-capacity-error.png")
    dialog.get_by_role("button", name="Preload file", exact=True).click()
    expect(dialog).not_to_be_visible()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
