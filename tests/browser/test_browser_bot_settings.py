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
    set_appearance,
)


@pytest.mark.parametrize(
    "width,height,theme", [(1536, 695, "dark"), (390, 844, "light")]
)
def test_owner_can_confirm_uncertain_bot_intake_review(
    server, browser, width, height, theme
):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    touch = width <= 720
    page = browser.new_page(
        viewport={"width": width, "height": height},
        color_scheme=theme,
        has_touch=touch,
        is_mobile=touch,
    )

    def activate(control):
        if touch:
            control.tap()
        else:
            control.press("Enter")

    try:
        job = server.control("/bot-intake-review", method="POST")
        route = server.url + f"/api/v1/jobs/{job['id']}/review-bot-intake"
        retry_route = server.url + f"/api/v1/jobs/{job['timeout_id']}/retry"
        assert page.request.post(route, data={}).status == 401
        login(page, server.url + "/?view=activity")
        set_appearance(page, theme)

        timeout = page.get_by_role("listitem").filter(has_text="timed-out.bin")
        expect(timeout).to_contain_text("Bot import failed")
        expect(timeout).to_contain_text("Telegram timed out. Try again.")
        retry = timeout.get_by_role("button", name="Retry timed-out.bin")
        expect(retry).to_have_text("Retry")
        expect(retry).to_be_visible()
        page.screenshot(
            path=ARTIFACTS / f"bot-intake-timeout-{width}.png", animations="disabled"
        )
        with page.expect_response(retry_route) as retried:
            activate(retry)
        assert retried.value.ok
        expect(timeout).to_have_attribute("data-status", "completed")
        assert (
            sum(item["name"] == "timed-out.bin" for item in server.control()["started"])
            == 1
        )

        review = page.get_by_role("button", name="Confirm intake channel reviewed")
        expect(review).to_be_visible()
        uncertain = page.get_by_role("listitem").filter(has=review)
        expect(uncertain.get_by_role("button", name="Retry relay.bin")).to_have_count(0)
        phase = page.get_by_text("Bot import needs review · bot queue paused")
        phase_bounds, review_bounds = phase.bounding_box(), review.bounding_box()
        assert phase_bounds is not None and review_bounds is not None
        assert review_bounds["y"] >= phase_bounds["y"] + phase_bounds["height"]
        activate(review)
        dialog = page.get_by_role("dialog", name="Confirm intake channel reviewed")
        expect(dialog.get_by_role("button", name="Keep queue paused")).to_be_focused()
        expect(dialog.get_by_text("relay.bin", exact=False)).to_be_visible()
        expect(
            dialog.get_by_text("This import stays failed.", exact=False)
        ).to_be_visible()
        dialog.press("Escape")
        expect(review).to_be_focused()

        activate(review)
        page.screenshot(
            path=ARTIFACTS / f"bot-intake-review-{width}.png", animations="disabled"
        )
        with page.expect_response(route) as saved:
            activate(dialog.get_by_role("button", name="Confirm review", exact=True))
        assert saved.value.ok
        assert saved.value.json()["phase"] == "bot_intake_reviewed"
        assert saved.value.json()["status"] == "error"
        expect(dialog).to_have_count(0)
        expect(review).to_have_count(0)
        repeated = page.request.post(route, data={})
        assert repeated.ok
        assert repeated.json()["updated_at"] == saved.value.json()["updated_at"]
        expect(
            page.get_by_text("File saved · temporary copy cleanup pending")
        ).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.context.close()


@pytest.mark.parametrize("width,height", [(1536, 695), (390, 844)])
def test_queued_bot_waits_name_the_blocked_resource(server, desktop, width, height):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    page.route(
        "**/api/v1/jobs",
        lambda route: route.fulfill(
            json={
                "jobs": [
                    {
                        "id": phase,
                        "name": name,
                        "kind": "telegram",
                        "status": "queued",
                        "phase": phase,
                        "current": 0,
                        "total": 16,
                    }
                    for phase, name in (
                        ("waiting_for_staging", "waiting-for-space.bin"),
                        ("waiting_for_telegram", "waiting-for-connection.bin"),
                    )
                ]
            }
        ),
    )
    login(page, server.url + "/?view=activity")
    set_appearance(page, "dark" if width > 720 else "light")
    for filename, message in (
        ("waiting-for-space.bin", "Waiting for temporary storage"),
        ("waiting-for-connection.bin", "Waiting for Telegram to reconnect"),
    ):
        row = page.get_by_role("listitem").filter(has_text=filename)
        expect(row.get_by_text(message, exact=True)).to_be_visible()
        expect(row.get_by_role("button", name=f"Cancel {filename}")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=ARTIFACTS / f"bot-intake-waits-{width}.png", animations="disabled"
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
    theme = "dark" if width == 1440 else "light"
    login(page, server.url + "/?view=settings&section=transfers#bot-mode")
    set_appearance(page, theme)
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
    track = section.locator(".mantine-Switch-track").bounding_box()
    help_bounds = help_button.bounding_box()
    assert track and help_bounds
    assert track["y"] + track["height"] / 2 == pytest.approx(
        help_bounds["y"] + help_bounds["height"] / 2, abs=1
    )
    help_button.focus()
    page.keyboard.press("Enter")
    help_content = page.get_by_role(
        "dialog", name="About Encrypt bot uploads", exact=True
    )
    expect(help_content).to_contain_text("channel access")
    palette = help_content.evaluate("""popup => {
        const probe = document.createElement('span');
        popup.append(probe);
        try {
            probe.style.backgroundColor = 'var(--surface-raised)';
            probe.style.color = 'var(--ink)';
            const style = getComputedStyle(probe);
            return {surface: style.backgroundColor, ink: style.color};
        } finally {
            probe.remove();
        }
    }""")
    expect(help_content).to_have_css("background-color", palette["surface"])
    expect(help_content).to_have_css("color", palette["ink"])
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


def test_bot_encryption_help_fits_short_touch_viewport(server, browser):
    context = browser.new_context(
        viewport={"width": 740, "height": 360},
        is_mobile=True,
        has_touch=True,
        reduced_motion="reduce",
    )
    try:
        page = context.new_page()
        login(page, server.url + "/?view=settings&section=transfers#bot-mode")
        help_button = page.get_by_role(
            "button", name="About Encrypt bot uploads", exact=True
        )
        help_button.tap()
        popup = page.get_by_role("dialog", name="About Encrypt bot uploads", exact=True)
        expect(popup).to_be_in_viewport(ratio=1)
        text = popup.locator("[data-autofocus]")
        assert text.evaluate("""node => {
            node.scrollTop = node.scrollHeight;
            return node.scrollHeight - node.clientHeight - node.scrollTop <= 1;
        }""")
        page.keyboard.press("Escape")
        expect(help_button).to_be_focused()
    finally:
        context.close()
