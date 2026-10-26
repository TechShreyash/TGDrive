"""Owner page interactions that must work on touch and across timezones."""

from __future__ import annotations

import pytest

from tests.browser.test_browser_e2e import ARTIFACTS, expect, login
from tests.browser.test_browser_e2e import browser as browser_fixture
from tests.browser.test_browser_e2e import desktop as desktop_fixture
from tests.browser.test_browser_e2e import server as server_fixture

pytestmark = pytest.mark.e2e
browser, desktop, server = browser_fixture, desktop_fixture, server_fixture


def test_shared_filename_opens_location_with_one_click(server, desktop):
    _, page = desktop
    login(page, server.url)
    contents = page.request.post(
        server.url + "/api/getDirectory", data={"path": "/"}
    ).json()["data"]["contents"]
    folder = next(item for item in contents.values() if item["name"] == "資料")
    assert page.request.post(
        server.url + "/api/v1/shares",
        data={"path": "/" + folder["id"], "expires_in_days": None},
    ).ok
    page.goto(server.url + "/?view=shares")
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="資料", exact=True).click()
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    expect(page.locator("tr.selected")).to_contain_text("資料")


def test_owner_documentation_and_backup_recovery_links_open_safe_new_tabs(
    server, desktop
):
    context, page = desktop
    login(page, server.url + "/?view=storage")
    context.route(
        "https://github.com/TechShreyash/TGDrive/blob/main/docs/**",
        lambda route: route.fulfill(
            body="Documentation destination", content_type="text/plain"
        ),
    )
    guide = page.get_by_role("link", name="Backup & recovery guide", exact=True)
    expect(guide).to_have_attribute(
        "href",
        "https://github.com/TechShreyash/TGDrive/blob/main/docs/backup-recovery.md",
    )
    expect(guide).to_have_attribute("target", "_blank")
    expect(guide).to_have_attribute("rel", "noopener noreferrer")
    with page.expect_popup() as opened:
        guide.click()
    popup = opened.value
    expect(popup.get_by_text("Documentation destination", exact=True)).to_be_visible()
    popup.close()
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Toggle Navigation", exact=True).click()
    docs = page.get_by_role("link", name="Documentation", exact=True)
    expect(docs).to_have_attribute(
        "href", "https://github.com/TechShreyash/TGDrive/blob/main/docs/README.md"
    )
    expect(docs).to_have_attribute("rel", "noopener noreferrer")
    with page.expect_popup() as opened:
        docs.click()
    popup = opened.value
    expect(popup.get_by_text("Documentation destination", exact=True)).to_be_visible()
    popup.close()
    assert page.url.endswith("?view=storage")


def test_shared_links_concise_expiry_keeps_exact_timezone(server, browser):
    context = browser.new_context(timezone_id="America/New_York", locale="en-US")
    try:
        page = context.new_page()
        expires = "2031-01-02T23:00:00Z"
        share = {
            "id": "compact-share",
            "node_id": "compact-file",
            "path": "/compact-file",
            "name": "Weekend trip.mp4",
            "folder_path": "/",
            "folder_label": "My Files",
            "type": "file",
            "size": 17800000,
            "created_at": "2026-10-17T00:00:00Z",
            "expires_at": expires,
            "copyable": True,
        }
        page.route(
            "**/api/v1/shares",
            lambda route: route.fulfill(
                json={
                    "shares": [
                        share,
                        dict(
                            share,
                            id="folder-share",
                            name="Family photos",
                            type="folder",
                            path="/photos",
                            expires_at=None,
                        ),
                        dict(
                            share,
                            id="old-share",
                            name="Project notes.pdf",
                            path="/notes",
                            expires_at="2020-01-02T23:00:00Z",
                        ),
                    ]
                }
            ),
        )
        login(page, server.url)
        page.goto(server.url + "/?view=shares")
        row = page.locator(
            ".share-row",
            has=page.get_by_role("button", name="Weekend trip.mp4", exact=True),
        )
        expect(row).to_have_count(1)
        expect(row).to_contain_text("Created")
        expect(page.get_by_text("Details", exact=True)).to_have_count(0)
        expect(
            page.get_by_text("Anyone with an active link", exact=False)
        ).to_have_count(0)
        exact = page.evaluate(
            "value => new Intl.DateTimeFormat(undefined, {dateStyle:'medium', "
            "timeStyle:'long', timeZone:'Asia/Kolkata'}).format(new Date(value))",
            expires,
        )
        expiry = row.locator(".share-expiry time")
        # Jan 2 in New York is Jan 3 in the configured instance timezone.
        expect(expiry).to_contain_text("Jan 3, 2031")
        expect(expiry).to_have_attribute("aria-label", "Expires " + exact)
        tooltip = expiry.get_by_role("tooltip")
        expect(tooltip).not_to_be_visible()
        expiry.focus()
        expect(tooltip).to_be_visible()
        expect(tooltip).to_have_text(exact)
        row.locator(".share-copy").focus()
        expect(tooltip).not_to_be_visible()
        expect(
            row.get_by_role("button", name="Show Weekend trip.mp4 in folder")
        ).to_be_visible()
        expect(
            row.get_by_role("button", name="Revoke Link for Weekend trip.mp4")
        ).to_be_visible()
        page.get_by_role("button", name="All 3", exact=True).click()
        expect(page.locator(".share-row")).to_have_count(3)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for width in (1440, 390, 320):
            page.set_viewport_size(
                {"width": width, "height": 960 if width > 600 else 844}
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert row.evaluate(
                "element => element.scrollWidth <= element.clientWidth + 1"
            )
            if width in (1440, 390):
                page.screenshot(path=str(ARTIFACTS / f"shares-concise-{width}.png"))
        # Tapping the date reveals the same exact expiry on touch-sized layouts.
        expiry.click()
        expect(tooltip).to_be_visible()
        expect(tooltip).to_have_text(exact)
    finally:
        context.close()


def test_backup_timestamp_uses_instance_timezone_in_another_browser_zone(
    server, browser
):
    context = browser.new_context(timezone_id="America/New_York", locale="en-US")
    try:
        page = context.new_page()
        login(page, server.url)
        page.goto(server.url + "/?view=storage")
        recovery = page.request.get(server.url + "/api/v1/status").json()["recovery"]
        expected = page.evaluate(
            "value => new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', "
            "timeStyle: 'long', timeZone: 'Asia/Kolkata'}).format(new Date(value))",
            recovery["last_success_at"],
        )
        expect(page.locator(".backup-confirmation-date strong")).to_have_text(expected)
        assert "GMT+5:30" in expected
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for width in (1440, 390, 320):
            page.set_viewport_size(
                {"width": width, "height": 960 if width > 600 else 844}
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator(".storage-page").evaluate(
                "element => element.scrollWidth <= element.clientWidth + 1"
            )
    finally:
        context.close()
