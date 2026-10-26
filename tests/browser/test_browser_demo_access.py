"""Demo visitor protection and full-access sessions in real Chromium."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, expect, login

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e
FULL_PASSWORD = "synthetic full demo password"
SAMPLE_CONTENT = b"Welcome to the public TGDrive demo.\n"


@pytest.fixture
def full_demo_server(monkeypatch, request):
    # Resolve the server only after its child process can inherit this policy.
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", FULL_PASSWORD)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD_HASH", raising=False)
    return request.getfixturevalue("server")


def navigate(page, label):
    fixtures.open_admin(page)
    page.get_by_role("link", name=label, exact=True).click()


def new_folder(page, name):
    page.locator("#new-button:visible, #compact-new-button:visible").click()
    page.get_by_role("button", name="New Folder", exact=True).click()
    dialog = page.get_by_role("dialog", name="New Folder", exact=True)
    dialog.get_by_label("Folder Name", exact=True).fill(name)
    dialog.get_by_role("button", name="Create Folder", exact=True).click()
    return dialog


def logout(page):
    fixtures.open_admin(page)
    page.get_by_role("button", name="Log Out", exact=True).click()
    expect(page.locator("#login-dialog")).to_be_visible()


@pytest.mark.parametrize("width", [1440, 390])
def test_demo_passwords_switch_permissions_and_preserve_readable_samples(
    full_demo_server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 960})
    url = full_demo_server.url
    login(page, url + "/?view=settings&section=regional")
    expect(page.get_by_label("Timezone", exact=True)).to_be_disabled()
    expect(page.locator(".demo-mode-notice")).to_be_visible()
    config = page.request.get(url + "/api/config").json()
    assert config["demo_mode"] and config["demo_read_only"]
    assert not config["demo_admin"]

    navigate(page, "My Files")
    demo_row = page.locator(".file-row").filter(
        has=page.get_by_role("button", name="Demo Files", exact=True)
    )
    expect(demo_row.locator(".demo-files-read-only-label")).to_have_text("Read-only")
    expect(new_folder(page, "Visitor workspace")).not_to_be_visible()
    expect(
        page.get_by_role("button", name="Visitor workspace", exact=True)
    ).to_be_visible()

    page.get_by_role("button", name="Demo Files", exact=True).press("Enter")
    expect(page.locator(".demo-files-read-only-note")).to_be_visible()
    new_folder(page, "Blocked visitor change")
    protection = page.get_by_role("dialog", name="Demo Files is read-only", exact=True)
    expect(protection).to_be_visible()
    expect(protection).to_contain_text("Open My Files")
    expect(protection.get_by_role("button", name="Close", exact=True)).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / f"demo-files-protection-{width}.png")
    protection.get_by_role("button", name="Open My Files", exact=True).click()
    expect(protection).not_to_be_visible()
    expect(page.get_by_role("button", name="Demo Files", exact=True)).to_be_visible()

    # Use this same page and login form for the full-access password.
    logout(page)
    page.get_by_label("Password", exact=True).fill(FULL_PASSWORD)
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.locator("#login-dialog")).not_to_be_visible()
    config = page.request.get(url + "/api/config").json()
    assert config["demo_mode"] and config["demo_admin"]
    assert not config["demo_read_only"]
    expect(demo_row.locator(".demo-files-read-only-label")).to_have_count(0)

    navigate(page, "Timezone")
    timezone = page.get_by_label("Timezone", exact=True)
    expect(timezone).to_be_enabled()
    expect(page.locator(".demo-mode-notice")).to_have_count(0)
    timezone.fill("UTC")
    with page.expect_response(
        lambda response: (
            response.url.endswith("/api/v1/settings/performance")
            and response.request.method == "PATCH"
        )
    ) as saved:
        page.get_by_role("button", name="Save Performance Settings", exact=True).click()
    assert saved.value.ok
    expect(timezone).to_have_value("UTC")

    navigate(page, "My Files")
    page.get_by_role("button", name="Demo Files", exact=True).press("Enter")
    expect(new_folder(page, "Owner sample folder")).not_to_be_visible()
    expect(
        page.get_by_role("button", name="Owner sample folder", exact=True)
    ).to_be_visible()
    page.locator("#new-button:visible, #compact-new-button:visible").click()
    with page.expect_file_chooser() as chooser:
        page.locator("#file-upload-action").click()
    chooser.value.set_files(
        [
            {
                "name": "Demo welcome.txt",
                "mimeType": "text/plain",
                "buffer": SAMPLE_CONTENT,
            }
        ]
    )
    started = fixtures.finish_fixture_uploads(page, full_demo_server, 1)
    assert started[0]["destination"] == "/tgdrive-demo-files"
    expect(
        page.get_by_role("button", name="Demo welcome.txt", exact=True)
    ).to_be_visible()

    # The ordinary password must lose owner capability without a page reload.
    logout(page)
    login(page)
    navigate(page, "Timezone")
    expect(page.get_by_label("Timezone", exact=True)).to_be_disabled()
    expect(page.get_by_label("Timezone", exact=True)).to_have_value("UTC")
    expect(page.locator(".demo-mode-notice")).to_be_visible()
    config = page.request.get(url + "/api/config").json()
    assert config["demo_read_only"] and not config["demo_admin"]

    navigate(page, "My Files")
    page.get_by_role("button", name="Demo Files", exact=True).press("Enter")
    expect(page.locator(".demo-files-read-only-note")).to_be_visible()
    expect(
        page.get_by_role("button", name="Owner sample folder", exact=True)
    ).to_be_visible()
    expect(
        page.get_by_role("button", name="Blocked visitor change", exact=True)
    ).to_have_count(0)
    sample = page.get_by_role("button", name="Demo welcome.txt", exact=True)
    sample.press("Enter")
    expect(page.locator(".text-preview")).to_contain_text(
        SAMPLE_CONTENT.decode().strip()
    )
    page.get_by_role("button", name="Close Preview", exact=True).click()
    page.get_by_label("Actions for Demo welcome.txt", exact=True).click()
    with page.expect_download() as download:
        page.get_by_role("link", name="Download", exact=True).click()
    assert Path(download.value.path()).read_bytes() == SAMPLE_CONTENT
    page.screenshot(path=ARTIFACTS / f"demo-files-visitor-samples-{width}.png")
