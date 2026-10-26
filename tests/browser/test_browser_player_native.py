"""Native local subtitle selection renders and cleans up without remote access."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import Server, expect, preview_dialog
from tests.browser.test_browser_player_settings import open_video

browser, server, desktop = fixtures.browser, fixtures.server, fixtures.desktop
pytestmark = pytest.mark.e2e


def test_local_subtitle_picker_renders_and_releases_its_file_on_close(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    external_requests: list[str] = []
    origin = urlsplit(server.url)

    def record_request(request: Any) -> None:
        url = request.url
        if url.startswith("data:"):
            return
        parsed = urlsplit(url.removeprefix("blob:"))
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            external_requests.append(url)

    page.on("request", record_request)
    page.add_init_script("""(() => {
        window.localSubtitleFiles = { created: [], revoked: [] };
        window.playerPolicyViolations = [];
        const create = URL.createObjectURL.bind(URL);
        const revoke = URL.revokeObjectURL.bind(URL);
        URL.createObjectURL = value => {
            const url = create(value);
            if (value instanceof File && value.name === 'local-captions.srt')
                window.localSubtitleFiles.created.push(url);
            return url;
        };
        URL.revokeObjectURL = url => {
            if (window.localSubtitleFiles.created.includes(url))
                window.localSubtitleFiles.revoked.push(url);
            return revoke(url);
        };
        addEventListener('securitypolicyviolation', event => {
            window.playerPolicyViolations.push({
                directive: event.effectiveDirective,
                blocked: event.blockedURI,
            });
        });
    })();""")
    dialog, player = open_video(page, server)

    def open_subtitles() -> Any:
        player.hover()
        menu = player.locator(".movi-settings-menu")
        opened = player.locator(".movi-settings-menu.is-open")
        if not opened.count():
            expect(menu).to_be_hidden()
            player.locator("button.movi-settings-btn").click()
        expect(opened).to_be_visible()
        if not menu.locator('.movi-settings-row[data-page="subtitles"]').is_visible():
            menu.locator("button.movi-settings-back").click()
        menu.locator('.movi-settings-row[data-page="subtitles"]').click()
        return menu

    menu = open_subtitles()
    with page.expect_file_chooser() as chooser:
        menu.get_by_role("button", name="Add subtitle…", exact=True).click()
    chooser.value.set_files(
        {
            "name": "local-captions.srt",
            "mimeType": "application/x-subrip",
            "buffer": (b"1\n00:00:00,000 --> 00:00:05,000\nLocal subtitle fixture\n"),
        }
    )
    page.wait_for_function("""() => document.querySelector('movi-player')
        ?.getSubtitleLangs().some(track =>
            track.label === 'local-captions' && track.active)
    """)
    expect(player.locator('input[type="file"]')).to_have_count(0)

    # Exercise the same native choices as embedded tracks, including Off.
    menu = open_subtitles()
    menu.locator('.movi-subtitle-track-item[data-track-id="null"]').click()
    page.wait_for_function("""() => document.querySelector('movi-player')
        ?.getSubtitleLangs().every(track => !track.active)
    """)
    menu = open_subtitles()
    choice = menu.locator('.movi-subtitle-track-item[data-subtitle-lang^="file-"]')
    expect(choice).to_contain_text("local-captions")
    choice.click()
    page.wait_for_function("""() => document.querySelector('movi-player')
        ?.getSubtitleLangs().some(track =>
            track.label === 'local-captions' && track.active)
    """)
    player.evaluate("element => { element.currentTime = 0.5; return element.play(); }")
    expect(player.locator(".movi-subtitle-overlay")).to_contain_text(
        "Local subtitle fixture", timeout=10000
    )
    player.evaluate("element => element.pause()")
    assert page.evaluate("window.localSubtitleFiles.created.length") == 1
    assert external_requests == []
    assert page.evaluate("window.playerPolicyViolations") == []

    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    expect(player).to_have_count(0)
    page.wait_for_function("""() => window.localSubtitleFiles.created.every(url =>
        window.localSubtitleFiles.revoked.includes(url))
    """)

    # A newly opened preview must not retain a revoked file or its subtitle cues.
    page.get_by_role("button", name="multitrack.mkv", exact=True).press("Enter")
    reopened = preview_dialog(page).locator("movi-player")
    expect(reopened).to_be_visible(timeout=30000)
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    assert not reopened.evaluate("""element => element.getSubtitleLangs()
        .some(track => track.label === 'local-captions')
    """)
    expect(reopened.locator(".movi-subtitle-overlay")).not_to_contain_text(
        "Local subtitle fixture"
    )
    assert external_requests == []
    assert page.evaluate("window.playerPolicyViolations") == []
