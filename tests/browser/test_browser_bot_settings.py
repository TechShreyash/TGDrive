"""Website-managed Telegram bot destination in desktop and mobile Chromium."""
# ruff: noqa: F811 - shared pytest fixtures use imported names.

import os

import pytest

pytestmark = pytest.mark.e2e
if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium tests", allow_module_level=True)

from tests.browser.test_browser_e2e import (  # noqa: E402
    ARTIFACTS,
    browser,  # noqa: F401
    desktop,  # noqa: F401
    expect,
    login,
    server,  # noqa: F401
)


@pytest.mark.parametrize("width,height", [(1440, 960), (390, 844)])
def test_bot_folder_deep_link_select_save_reload_and_default(
    server, desktop, width, height
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    login(page, server.url + "/?view=settings&section=transfers#bot-mode")
    section = page.locator("#bot-mode")
    expect(
        section.get_by_role("heading", name="Bot uploads", exact=True)
    ).to_be_visible()
    expect(section.locator(".bot-upload-destination")).to_contain_text(
        "TGDriveBotModeFiles"
    )
    assert page.request.post(
        server.url + "/api/createNewFolder", data={"path": "/", "name": "Bot inbox"}
    ).ok
    page.get_by_role("button", name="Change folder", exact=True).click()
    dialog = page.get_by_role("dialog", name="Choose bot upload folder")
    dialog.get_by_role("button", name="Bot inbox", exact=True).click()
    expect(dialog.get_by_role("navigation")).to_contain_text("Bot inbox")
    dialog.get_by_role("button", name="Use this folder", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(section.get_by_role("status")).to_contain_text("Bot upload folder saved")
    snapshot = page.request.get(server.url + "/api/v1/settings/bot-mode").json()
    assert snapshot["folder_label"] == "My Files / Bot inbox"
    assert (
        snapshot["settings_url"]
        == server.url + "/?view=settings&section=transfers#bot-mode"
    )
    page.reload()
    expect(section.locator(".bot-upload-destination")).to_contain_text(
        "My Files / Bot inbox"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(
        path=str(ARTIFACTS / f"bot-upload-settings-{width}.png"), full_page=True
    )
    section.get_by_role("button", name="Use default folder", exact=True).click()
    expect(section.locator(".bot-upload-destination")).to_contain_text(
        "TGDriveBotModeFiles"
    )
    assert (
        page.request.get(server.url + "/api/v1/settings/bot-mode").json()["folder_path"]
        is None
    )


def test_folder_picker_read_failure_does_not_save_an_accidental_destination(
    server, desktop
):
    _, page = desktop
    login(page, server.url + "/?view=settings&section=transfers#bot-mode")
    page.route(
        "**/api/getDirectory",
        lambda route: route.fulfill(
            status=503, json={"detail": "Folder list unavailable"}
        ),
    )
    page.get_by_role("button", name="Change folder", exact=True).click()
    dialog = page.get_by_role("dialog", name="Choose bot upload folder")
    expect(dialog.get_by_role("alert")).to_contain_text("Folder list unavailable")
    expect(
        dialog.get_by_role("button", name="Use this folder", exact=True)
    ).to_be_disabled()
    page.unroute("**/api/getDirectory")
    dialog.get_by_role("button", name="Try again", exact=True).click()
    expect(
        dialog.get_by_role("button", name="Use this folder", exact=True)
    ).to_be_enabled()
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    assert (
        page.request.get(server.url + "/api/v1/settings/bot-mode").json()["folder_path"]
        is None
    )
