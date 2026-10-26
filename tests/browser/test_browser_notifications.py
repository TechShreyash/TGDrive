"""Shared notification placement, lifetime and dialog usability in real Chromium."""

import pytest

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    expect,
    login,
    select_item,
)

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_notifications_are_single_dismissible_and_usable_in_modals(
    server, desktop, width, theme
):
    context, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script("localStorage.setItem('tgdrive.appearance','" + theme + "')")
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=server.url)
    page.clock.install()
    login(page, server.url)

    def check_notification(message):
        toast = page.locator(".toast")
        expect(toast).to_have_count(1)
        expect(toast).to_have_text(message)
        expect(toast).to_be_visible()
        assert toast.evaluate("el => el.matches(':popover-open')")
        box = toast.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width
        assert box["y"] >= 0 and box["y"] + box["height"] <= 844
        close = toast.get_by_role("button", name="Dismiss Notification")
        close_box = close.bounding_box()
        assert close_box and close_box["width"] >= (44 if width < 720 else 24)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        return toast

    def create_folder(name):
        page.locator("#compact-new-button" if width < 720 else "#new-button").click()
        page.get_by_role("button", name="New Folder", exact=True).click()
        dialog = page.get_by_role("dialog", name="New Folder", exact=True)
        expect(dialog.get_by_label("Folder Name")).to_be_focused()
        dialog.get_by_label("Folder Name").fill(name)
        dialog.get_by_role("button", name="Create Folder", exact=True).click()
        expect(dialog).to_have_count(0)

    create_folder("Popup check")
    toast = check_notification("Folder created.")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"notification-{theme}-{width}.png")
    toast.hover()
    page.clock.run_for(6500)
    expect(toast).to_be_visible()
    dismiss = toast.get_by_role("button", name="Dismiss Notification")
    dismiss.focus()
    page.mouse.move(0, 0)
    page.clock.run_for(6500)
    expect(toast).to_be_visible()
    toast.get_by_role("button", name="Dismiss Notification").click()
    expect(toast).to_have_count(0)
    create_folder("Popup next")
    check_notification("Folder created.")
    page.clock.run_for(6500)
    expect(toast).to_have_count(0)

    page.locator('summary[aria-label="Actions for 資料"]').click()
    page.get_by_role("button", name="Share Link", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Create Share Link", exact=True).click()
    copy = dialog.get_by_role("button", name="Copy Link", exact=True)
    copy.click()
    check_notification("Share Link copied.")
    expect(copy).to_be_focused()
    expect(dialog.locator(".toast")).to_have_count(1)
    page.screenshot(path=ARTIFACTS / f"notification-modal-{theme}-{width}.png")
    toast.get_by_role("button", name="Dismiss Notification").click()
    expect(copy).to_be_focused()
    copy.click()
    check_notification("Share Link copied.")
    for key in ["Tab", "Shift+Tab"]:
        for _ in range(8):
            page.keyboard.press(key)
            assert dialog.evaluate("el => el.contains(document.activeElement)")
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    expect(toast).to_have_count(0)

    # A long server error must wrap, remain dismissible and stay until read.
    detail = "Unable to update this item. Reference: " + "abcdefgh" * 240
    page.route(
        "**/api/v1/starred",
        lambda route: route.fulfill(status=503, json={"detail": detail}),
    )
    page.locator('summary[aria-label="Actions for 資料"]').click()
    page.get_by_role("button", name="Add to Starred", exact=True).click()
    check_notification(detail)
    expect(toast).to_have_attribute("role", "alert")
    message = toast.locator("span")
    assert message.evaluate("el => el.scrollHeight > el.clientHeight")
    message.focus()
    message.press("Control+End")
    expect(message).not_to_have_js_property("scrollTop", 0)
    page.clock.run_for(10000)
    expect(toast).to_be_visible()
    page.screenshot(path=ARTIFACTS / f"notification-error-{theme}-{width}.png")
    page.locator("#compact-new-button" if width < 720 else "#new-button").click()
    page.get_by_role("button", name="New Folder", exact=True).click()
    expect(toast).to_have_count(0)
    page.keyboard.press("Escape")


def test_delete_dialog_failure_is_inline_without_duplicate_popup(server, desktop):
    _, page = desktop
    login(page, server.url)
    select_item(page, "Private folder")
    page.get_by_role("button", name="Trash", exact=True).click()
    page.get_by_role("link", name="Trash", exact=True).click()
    select_item(page, "Private folder")
    page.get_by_role("button", name="Delete Permanently", exact=True).click()
    detail = "Temporary database failure. Please try again."
    page.route(
        "**/api/v1/nodes/bulk",
        lambda route: route.fulfill(
            status=503,
            json={"detail": detail},
        ),
    )
    dialog = page.get_by_role("dialog")
    confirm = dialog.get_by_role("button", name="Delete Permanently", exact=True)
    confirm.click()
    expect(dialog.get_by_role("alert")).to_have_text(detail)
    expect(confirm).to_be_enabled()
    expect(page.locator(".toast")).to_have_count(0)
    expect(
        page.get_by_role("button", name="Private folder", exact=True)
    ).to_be_visible()
