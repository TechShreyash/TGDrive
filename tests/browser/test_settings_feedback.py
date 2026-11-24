"""Mobile settings validation stays close to the controls that need correction."""

import pytest

from tests.browser.test_browser_e2e import browser as browser_fixture
from tests.browser.test_browser_e2e import expect, login
from tests.browser.test_browser_e2e import server as server_fixture

pytestmark = pytest.mark.e2e
browser, server = browser_fixture, server_fixture


def test_timezone_validation_focuses_and_describes_the_field(server, browser):
    context = browser.new_context(
        viewport={"width": 320, "height": 568}, has_touch=True, is_mobile=True
    )
    try:
        page = context.new_page()
        login(page, server.url + "/?view=settings&section=regional")
        section = page.locator(".performance-settings")
        timezone = section.get_by_label("Timezone", exact=True)
        save = section.get_by_role("button", name="Save Performance Settings")
        patches = []
        page.on(
            "request",
            lambda request: (
                patches.append(request.post_data_json)
                if request.method == "PATCH"
                and request.url.endswith("/api/v1/settings/performance")
                else None
            ),
        )

        timezone.fill("Invalid/Zone")
        save.tap()
        expect(timezone).to_be_focused()
        expect(timezone).to_have_attribute("aria-invalid", "true")
        expect(section.get_by_role("alert")).to_be_in_viewport()
        expect(timezone).to_have_accessible_description(
            "Use a valid timezone, such as Asia/Kolkata. Used for dates and "
            "share expiry. Examples: Asia/Kolkata, Europe/London or UTC."
        )
        assert not patches

        def reject_unsupported_timezone(route):
            if route.request.method == "PATCH" and route.request.post_data_json == {
                "timezone": "Europe/London"
            }:
                route.fulfill(
                    status=422, json={"detail": "Invalid performance settings"}
                )
            else:
                route.continue_()

        page.route("**/api/v1/settings/performance", reject_unsupported_timezone)
        timezone.fill("Europe/London")
        expect(timezone).not_to_have_attribute("aria-invalid", "true")
        expect(section.get_by_role("alert")).to_have_count(0)
        save.tap()
        expect(timezone).to_be_focused()
        expect(section.get_by_role("alert")).to_contain_text("supported by this server")

        timezone.fill("Europe/Paris")
        save.tap()
        expect(section.get_by_role("status")).to_contain_text("Timezone saved")
        assert patches == [{"timezone": "Europe/London"}, {"timezone": "Europe/Paris"}]
    finally:
        context.close()


@pytest.mark.parametrize("resize_layout", [True, False], ids=["layout", "visual-only"])
def test_timezone_stays_reachable_when_keyboard_reduces_viewport(
    server, browser, resize_layout
):
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    try:
        page = context.new_page()
        login(page, server.url + "/?view=settings&section=regional")
        page.set_default_timeout(3500)
        timezone = page.get_by_label("Timezone", exact=True)
        save = page.get_by_role("button", name="Save Performance Settings")
        timezone.tap()
        timezone.fill("Europe/Paris")
        expect(timezone).to_be_focused()

        if resize_layout:
            page.set_viewport_size({"width": 390, "height": 390})
        else:
            # Model a keyboard that covers part of the layout without resizing it.
            page.evaluate(
                """() => {
                    const viewport = window.visualViewport;
                    Object.defineProperty(viewport, "height", { value: 390 });
                    viewport.dispatchEvent(new Event("resize"));
                }"""
            )

        visible_in_workspace = """selector => {
            const element = document.querySelector(selector);
            const box = element.getBoundingClientRect();
            const workspace = element.closest(".workspace").getBoundingClientRect();
            const viewport = window.visualViewport;
            const top = Math.max(workspace.top, viewport.offsetTop);
            const visibleBottom = viewport.offsetTop + viewport.height;
            const bottom = Math.min(workspace.bottom, visibleBottom);
            return box.top >= top && box.bottom <= bottom;
        }"""
        page.wait_for_function(visible_in_workspace, arg="#performance-timezone")
        expect(timezone).to_be_focused()

        page.locator(".workspace").evaluate("el => { el.scrollTop = el.scrollHeight; }")
        page.wait_for_function(
            visible_in_workspace, arg='[aria-label="Save Performance Settings"]'
        )
        save.tap(trial=True)
    finally:
        context.close()


def test_premium_phone_validation_clears_on_correction(server, browser):
    context = browser.new_context(
        viewport={"width": 320, "height": 568}, has_touch=True, is_mobile=True
    )
    try:
        page = context.new_page()
        login(page, server.url + "/?view=connections")
        page.get_by_role("button", name="Connect Premium Account", exact=True).tap()
        form = page.get_by_role("form", name="Connect a Premium Account", exact=True)
        phone = form.get_by_label("Phone Number", exact=True)
        phone.fill("1234567")
        form.get_by_role("button", name="Send Login Code", exact=True).tap()
        expect(form.get_by_role("alert")).to_contain_text("with its country code")
        expect(phone).to_be_focused()
        expect(phone).to_have_attribute("aria-invalid", "true")
        phone.fill("+441234567890")
        expect(form.get_by_role("alert")).to_have_count(0)
        expect(phone).not_to_have_attribute("aria-invalid", "true")
    finally:
        context.close()


def test_connection_validation_clears_after_credential_correction(server, browser):
    context = browser.new_context(
        viewport={"width": 320, "height": 568}, has_touch=True, is_mobile=True
    )
    try:
        page = context.new_page()
        login(page, server.url + "/?view=connections")
        section = page.locator(".connection-management")
        section.get_by_role("button", name="Add Storage Bot", exact=True).tap()
        form = section.locator("form.connection-editor")
        token = form.get_by_label("Bot Token", exact=True)
        expect(token).to_be_focused()
        expect(form.locator("input")).to_have_count(1)
        token.fill(" ")
        form.get_by_role("button", name="Verify and Save", exact=True).tap()
        expect(form.get_by_role("alert")).to_contain_text("Enter the bot token")

        token.fill("987654:" + "z" * 30)
        expect(form.get_by_role("alert")).to_have_count(0)
        form.get_by_role("button", name="Verify and Save", exact=True).tap()
        expect(form).to_have_count(0)
        expect(section.get_by_role("status")).to_contain_text(
            "Connection verified and saved."
        )
    finally:
        context.close()
