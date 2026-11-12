"""Website-managed Telegram bot destination in desktop and mobile Chromium."""
# ruff: noqa: F811 - shared pytest fixtures use imported names.

import json
import os

import pytest

pytestmark = pytest.mark.e2e
if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium tests", allow_module_level=True)

from tests.browser.node_pages import NODES  # noqa: E402
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
        NODES,
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
    page.unroute(NODES)
    dialog.get_by_role("button", name="Try again", exact=True).click()
    expect(
        dialog.get_by_role("button", name="Use this folder", exact=True)
    ).to_be_enabled()
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    assert (
        page.request.get(server.url + "/api/v1/settings/bot-mode").json()["folder_path"]
        is None
    )


@pytest.mark.parametrize("width", [1440, 390])
def test_bot_encryption_switch_saves_truthfully_and_help_returns_focus(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 900})
    login(page, server.url + "/?view=settings&section=transfers#bot-mode")
    section = page.locator("#bot-mode")
    toggle = section.get_by_role("switch", name="Encrypt bot uploads", exact=True)
    expect(toggle).to_be_checked()
    geometry = toggle.evaluate("""n=>{
        const box=x=>{
            const r=x.getBoundingClientRect();
            return {x:r.x,y:r.y,width:r.width,height:r.height};
        };
        const style=getComputedStyle(n), label=n.closest('label');
        return {id:n.id,input:box(n),bodyLabel:box(label),
            associated:!!label&&Array.from(n.labels).includes(label)&&label.contains(n),
            opacity:style.opacity,zIndex:style.zIndex,transform:style.transform};
    }""")
    assert geometry["associated"]
    assert geometry["input"]["width"] > 17
    assert geometry["bodyLabel"]["height"] >= 44
    (ARTIFACTS / f"switch-geometry-{width}.json").write_text(
        json.dumps(geometry, indent=2)
    )
    help_button = section.get_by_role(
        "button", name="About Encrypt bot uploads", exact=True
    )
    help_button.focus()
    page.keyboard.press("Enter")
    help_content = page.get_by_role(
        "dialog", name="About Encrypt bot uploads", exact=True
    )
    expect(help_content).to_contain_text("channel access")
    page.keyboard.press("Escape")
    expect(help_button).to_be_focused()
    # The visible track belongs to the associated label, not a hidden-input proxy.
    page.route(
        "**/api/v1/settings/bot-mode",
        lambda route: route.fulfill(
            status=503, json={"detail": "Bot settings temporarily unavailable"}
        ),
    )
    section.locator(".mantine-Switch-track").click()
    expect(section.get_by_role("alert")).to_contain_text("temporarily unavailable")
    expect(toggle).to_be_checked()
    page.unroute("**/api/v1/settings/bot-mode")
    held = []

    def delay_save(route):
        if route.request.method == "PATCH":
            held.append((route, route.fetch()))
        else:
            route.continue_()

    page.route("**/api/v1/settings/bot-mode", delay_save)
    toggle.focus()
    page.keyboard.press("Space")
    expect(toggle).to_be_disabled()
    assert len(held) == 1
    held[0][0].fulfill(response=held[0][1])
    expect(toggle).not_to_be_checked()
    expect(toggle).to_be_enabled()
    page.unroute("**/api/v1/settings/bot-mode")
    page.reload()
    expect(toggle).not_to_be_checked()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/api/v1/settings/bot-mode")
        )
    ):
        toggle.locator("xpath=ancestor::label").click()
    expect(toggle).to_be_checked()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(
        path=ARTIFACTS / f"final-switch-natural-{width}.png", animations="disabled"
    )
