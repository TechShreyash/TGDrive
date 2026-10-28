"""Owner backup UX uses the real routes with an offline replica service."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, CONTROL, expect, login, open_admin

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e
BOT = "222222222:" + "a" * 35
MAIN_BOT = "333333333:" + "b" * 35
BACKUP_CHANNEL = "-1003333333333"
NEW_MAIN_CHANNEL = "-1004444444444"
KEY = "c" * 64


@pytest.fixture(autouse=True)
def keep_console_errors(desktop, request):
    _, page = desktop
    errors = []

    def collect(message):
        if message.type == "error":
            text = message.text
            for secret in (BOT, MAIN_BOT, KEY):
                text = text.replace(secret, "<redacted>")
            errors.append(text)

    page.on("console", collect)
    yield
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / (request.node.name + "-console.json")).write_text(json.dumps(errors))
    assert errors == []


def open_storage(page):
    open_admin(page)
    page.get_by_role("link", name="Storage & Backups", exact=True).click()
    section = page.get_by_role("region", name="Backups & recovery", exact=True)
    expect(section).to_be_visible()
    return section


def scenario(page, server, name):
    response = page.request.post(
        server.url + "/__test__/replica/" + name, headers=CONTROL
    )
    assert response.ok
    page.get_by_role("button", name="Refresh channel backup status", exact=True).click()


def capture(page, name):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / name, animations="disabled")


@pytest.mark.parametrize(
    "width,theme", [(1440, "light"), (390, "dark"), (320, "light")]
)
def test_channel_backup_setup_progress_pause_errors_and_private_exports(
    server, desktop, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 900 if width > 720 else 844})
    page.add_init_script("localStorage.setItem('tgdrive.appearance','" + theme + "')")
    login(page, server.url)
    section = open_storage(page)
    database = page.get_by_role("region", name="Cloud Database", exact=True)
    expect(database).to_be_visible()
    expect(
        database.get_by_role("button", name="Back Up Now", exact=True)
    ).to_be_enabled()
    section.get_by_role("button", name="Set up backup channel", exact=True).click()
    section.get_by_label("Backup channel ID", exact=True).fill(BACKUP_CHANNEL)
    section.get_by_label("Backup bot token", exact=True).fill(BOT)
    submit = section.get_by_role("button", name="Verify and start backup", exact=True)
    expect(submit).to_be_disabled()
    section.get_by_role(
        "checkbox", name="I created the backup channel", exact=False
    ).check()
    capture(page, f"channel-backup-setup-{theme}-{width}.png")
    submit.click()
    expect(section.locator(".channel-backup-phase")).to_contain_text(
        "Copying to the backup channel"
    )
    expect(
        section.get_by_role("progressbar", name="Channel copy progress", exact=True)
    ).to_have_attribute("value", str(1024**2))
    expect(section.get_by_text("Project archive.zip", exact=True)).to_be_visible()
    expect(section.get_by_label("Backup bot token", exact=True)).to_have_count(0)
    expect(
        section.get_by_role("heading", name="Backups & recovery", exact=True)
    ).to_be_focused()
    section.scroll_into_view_if_needed()
    capture(page, f"channel-backup-progress-{theme}-{width}.png")
    section.get_by_role("button", name="Pause", exact=True).click()
    expect(section.locator(".channel-backup-phase")).to_contain_text("Paused")
    section.get_by_role("button", name="Resume", exact=True).click()
    expect(section.locator(".channel-backup-phase")).to_contain_text("Copying")
    scenario(page, server, "error")
    expect(section.get_by_role("alert")).to_contain_text("Backup bot cannot read")
    expect(
        section.get_by_text(
            "Restore the bot's administrator permissions, then retry.", exact=True
        )
    ).to_be_visible()
    expect(section.locator(".channel-backup-feedback")).not_to_contain_text("0")
    section.get_by_role("alert").scroll_into_view_if_needed()
    capture(page, f"channel-backup-error-{theme}-{width}.png")
    section.get_by_role("button", name="Retry", exact=True).click()
    expect(section.locator(".channel-backup-phase")).to_contain_text("Copying")
    scenario(page, server, "waiting")
    expect(section.locator(".channel-backup-phase")).to_contain_text("Waiting to retry")
    expect(section.get_by_text("Next retry:", exact=False)).to_contain_text("2026")
    section.locator(".channel-backup-phase").scroll_into_view_if_needed()
    capture(page, f"channel-backup-waiting-{theme}-{width}.png")
    scenario(page, server, "waiting-mongodb")
    expect(section.locator(".channel-backup-phase")).to_contain_text("Waiting to retry")
    expect(section.get_by_role("alert")).to_contain_text(
        "The backup database is temporarily unavailable."
    )
    expect(section.get_by_text("Next retry:", exact=False)).to_contain_text("2026")
    scenario(page, server, "protected")
    expect(section.locator(".channel-backup-phase")).to_contain_text("Backup confirmed")
    expect(section.locator(".channel-backup-confirmation")).to_contain_text(
        "catalog revision 9"
    )
    with page.expect_download() as backup_download:
        section.get_by_role("link", name="Download backup", exact=True).click()
    assert backup_download.value.suggested_filename == "tgdrive-backup.tgdb"
    assert (
        Path(backup_download.value.path()).read_bytes()
        == b"synthetic encrypted recovery file"
    )
    with page.expect_download() as key_download:
        section.get_by_role("link", name="Save recovery key", exact=True).click()
    exported = json.loads(Path(key_download.value.path()).read_text())
    assert exported["key"] == KEY
    scenario(page, server, "many-events")
    section.locator("summary").filter(has_text="Activity and report").click()
    expect(section.locator(".channel-backup-report li")).to_have_count(30)
    with page.expect_download() as report_download:
        section.get_by_role("link", name="Download report", exact=True).click()
    report = Path(report_download.value.path()).read_text()
    assert BOT not in report and KEY not in report and MAIN_BOT not in report
    assert "bot_token" not in report and "credentials" not in report
    section.locator("summary").filter(has_text="Backup settings").click()
    section.get_by_role("button", name="Disable automatic backup", exact=True).click()
    section.get_by_role("button", name="Confirm disable", exact=True).click()
    expect(section.locator(".channel-backup-phase")).to_contain_text("not enabled")
    section.get_by_role("button", name="Enable backup", exact=True).click()
    expect(section.locator(".channel-backup-phase")).to_contain_text("Copying")


@pytest.mark.parametrize(
    "catalog_only,width,theme", [(False, 320, "light"), (True, 390, "dark")]
)
def test_recovery_only_inspection_confirmation_resume_and_activation(
    server, desktop, catalog_only, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script("localStorage.setItem('tgdrive.appearance','" + theme + "')")
    response = page.request.post(
        server.url + "/__test__/replica/recovery-only", headers=CONTROL
    )
    assert response.ok
    if not catalog_only:
        response = page.request.post(
            server.url + "/__test__/replica/main-unavailable", headers=CONTROL
        )
        assert response.ok
    login(page, server.url)
    expect(
        page.get_by_role("heading", name="Storage recovery", exact=True)
    ).to_be_visible()
    expect(page.locator(".file-browser")).to_have_count(0)
    section = page.get_by_role("region", name="Recover your drive", exact=True)
    capture(page, f"channel-recovery-initial-{theme}-{width}.png")
    section.get_by_label("Recovery file (required)", exact=True).set_input_files(
        {
            "name": "tgdrive-backup.tgdb",
            "mimeType": "application/octet-stream",
            "buffer": b"synthetic encrypted recovery file",
        }
    )
    section.get_by_label("Recovery key (required)", exact=True).fill(
        json.dumps({"format": "tgdrive-recovery-key-v2", "key": KEY})
    )
    section.get_by_role("button", name="Inspect backup", exact=True).click()
    expect(
        section.get_by_role("heading", name="Recovery copy found", exact=True)
    ).to_be_visible()
    expect(
        section.get_by_role("heading", name="Recovery copy found", exact=True)
    ).to_be_focused()
    expect(section.get_by_label("Recovery key (required)", exact=True)).to_have_value(
        ""
    )
    database_only = section.get_by_role(
        "checkbox",
        name="Restore the database only on this surviving channel",
        exact=True,
    )
    if catalog_only:
        expect(database_only).to_be_checked()
    else:
        expect(database_only).not_to_be_checked()
    # The candidate and credentials are server-side; a page reload resumes the review.
    page.reload()
    expect(
        section.get_by_role("heading", name="Recovery copy found", exact=True)
    ).to_be_visible()
    target = "-1001234567890" if catalog_only else NEW_MAIN_CHANNEL
    if catalog_only:
        expect(database_only).to_be_checked()
        expect(
            section.get_by_label("Replacement main bot token (optional)", exact=True)
        ).to_be_empty()
    else:
        section.get_by_label("New main channel ID", exact=True).fill(target)
        section.get_by_label("New main bot token", exact=True).fill(MAIN_BOT)
    confirmation = (
        section.locator("label").filter(has_text="to confirm").locator("input")
    )
    copy = section.get_by_role(
        "button",
        name="Verify and prepare database restore"
        if catalog_only
        else "Copy to new main channel",
        exact=True,
    )
    confirmation.fill("-1001111111111")
    expect(copy).to_be_disabled()
    confirmation.fill(target)
    expect(copy).to_be_enabled()
    capture(page, f"channel-recovery-review-{theme}-{width}.png")
    copy.click()
    expect(section.locator(".channel-backup-phase")).to_contain_text(
        "Preparing the recovered drive"
    )
    section.get_by_role("button", name="Pause recovery", exact=True).click()
    expect(section.locator(".channel-backup-phase")).to_contain_text("Paused")
    section.get_by_role("button", name="Resume recovery", exact=True).click()
    # Advance the synthetic transport only after the UI requested a confirmed target.
    scenario(page, server, "recovery-ready")
    expect(
        section.get_by_role("heading", name="Recovered drive verified", exact=True)
    ).to_be_visible()
    activate = section.get_by_role(
        "button", name="Activate recovered drive", exact=True
    )
    expect(activate).to_be_disabled()
    section.get_by_label(
        "Type " + target + " to activate this recovered drive", exact=True
    ).fill(target)
    activate.click()
    expect(section.locator(".channel-backup-phase")).to_contain_text("Restart required")
    expect(
        section.get_by_text("The recovered configuration is saved.", exact=False)
    ).to_be_visible()
    state = page.request.get(server.url + "/__test__/replica", headers=CONTROL).json()
    recovery = next(item for item in state["events"] if item["action"] == "recover")
    assert (
        recovery["channel"] == int(target) and recovery["catalog_only"] is catalog_only
    )
    assert state["events"][-1]["action"] == "activate"
    assert any(item["action"] == "inspect_file" for item in state["events"])
    assert not page.evaluate(
        "Object.values(localStorage).some(value => value.includes('"
        + BOT
        + "') || value.includes('"
        + KEY
        + "'))"
    )
    capture(page, f"channel-recovery-activated-{theme}-{width}.png")


def test_file_import_validates_and_accepts_replacement_access(server, desktop):
    _, page = desktop
    page.set_viewport_size({"width": 390, "height": 844})
    login(page, server.url)
    section = open_storage(page)
    section.get_by_text("Restore backup", exact=True).click()
    file_input = section.get_by_label("Recovery file (required)", exact=True)
    key_input = section.get_by_label("Recovery key (required)", exact=True)
    inspect = section.get_by_role("button", name="Inspect backup", exact=True)
    expect(inspect).to_be_disabled()
    file_input.set_input_files(
        {"name": "wrong.txt", "mimeType": "text/plain", "buffer": b"x"}
    )
    key_input.fill(KEY)
    inspect.click()
    expect(section.get_by_role("alert")).to_contain_text("encrypted .tgdb")
    file_input.set_input_files(
        {
            "name": "tgdrive-backup.tgdb",
            "mimeType": "application/octet-stream",
            "buffer": b"synthetic encrypted recovery file",
        }
    )
    key_input.fill("not-a-key")
    inspect.click()
    expect(section.get_by_role("alert")).to_contain_text("saved recovery key")
    state = page.request.get(server.url + "/__test__/replica", headers=CONTROL).json()
    assert not state["events"]
    response = page.request.post(
        server.url + "/__test__/replica/access-revoked", headers=CONTROL
    )
    assert response.ok
    key_input.fill(KEY)
    section.get_by_text("Channel access changed?", exact=True).click()
    section.get_by_label("Surviving channel ID (optional)", exact=True).fill(
        BACKUP_CHANNEL
    )
    token_input = section.get_by_label("Replacement bot token (optional)", exact=True)
    token_input.fill(BOT)
    inspect.click()
    expect(
        section.get_by_role("heading", name="Recovery copy found", exact=True)
    ).to_be_focused()
    expect(token_input).to_have_value("")
    expect(key_input).to_have_value("")
    expect(
        section.get_by_role(
            "checkbox",
            name="Restore the database only on this surviving channel",
            exact=True,
        )
    ).not_to_be_checked()
    state = page.request.get(server.url + "/__test__/replica", headers=CONTROL).json()
    assert state["events"][-1] == {
        "action": "inspect_file",
        "channel": int(BACKUP_CHANNEL),
        "replacement_access": True,
    }
    assert not page.evaluate(
        "Object.values(localStorage).some(value => value.includes('"
        + BOT
        + "') || value.includes('"
        + KEY
        + "'))"
    )
    capture(page, "recovery-file-replacement-access-390.png")


def test_legacy_channel_inspection_stays_available_in_collapsed_controls(
    server, desktop
):
    _, page = desktop
    login(page, server.url)
    section = open_storage(page)
    section.get_by_text("Restore backup", exact=True).click()
    channel = section.get_by_label("Surviving channel ID", exact=True)
    expect(channel).to_be_hidden()
    section.get_by_text("Older backups: recover from a channel", exact=True).click()
    channel.fill(BACKUP_CHANNEL)
    section.get_by_label(
        "Bot token with access to the surviving channel", exact=True
    ).fill(BOT)
    section.get_by_label("Saved recovery key", exact=True).fill(KEY)
    section.get_by_role("button", name="Inspect recovery copy", exact=True).click()
    expect(
        section.get_by_role("heading", name="Recovery copy found", exact=True)
    ).to_be_visible()
    expect(
        section.get_by_text(
            "This older channel recovery copy restores the catalog and", exact=False
        )
    ).to_be_visible()
    expect(section.get_by_label("Saved recovery key", exact=True)).to_have_value("")


def test_demo_file_restore_is_disabled_without_disabling_private_backup_exports(
    server, desktop
):
    _, page = desktop
    page.set_viewport_size({"width": 390, "height": 844})
    for name in ("protected", "file-restore-disabled"):
        response = page.request.post(
            server.url + "/__test__/replica/" + name, headers=CONTROL
        )
        assert response.ok
    login(page, server.url)
    section = open_storage(page)
    section.get_by_text("Restore backup", exact=True).click()
    expect(
        section.get_by_text(
            "Recovery-file restore is unavailable in demo mode.", exact=False
        )
    ).to_be_visible()
    expect(
        section.get_by_label("Recovery file (required)", exact=True)
    ).to_be_disabled()
    expect(section.get_by_label("Recovery key (required)", exact=True)).to_be_disabled()
    expect(
        section.get_by_role("button", name="Inspect backup", exact=True)
    ).to_be_disabled()
    section.get_by_text("Channel access changed?", exact=True).click()
    expect(
        section.get_by_label("Surviving channel ID (optional)", exact=True)
    ).to_be_disabled()
    expect(
        section.get_by_label("Replacement bot token (optional)", exact=True)
    ).to_be_disabled()
    expect(
        page.get_by_role("region", name="Cloud Database", exact=True).get_by_role(
            "button", name="Back Up Now", exact=True
        )
    ).to_be_enabled()
    expect(
        section.get_by_role("link", name="Save recovery key", exact=True)
    ).to_be_visible()
    with page.expect_download() as saved_file:
        section.get_by_role("link", name="Download backup", exact=True).click()
    assert (
        Path(saved_file.value.path()).read_bytes()
        == b"synthetic encrypted recovery file"
    )
    state = page.request.get(server.url + "/__test__/replica", headers=CONTROL).json()
    assert not any(item["action"] == "inspect_file" for item in state["events"])
    capture(page, "recovery-file-demo-disabled-390.png")


def test_enabled_backup_refreshes_idle_lag_without_manual_reload(server, desktop):
    _, page = desktop
    page.clock.install()
    login(page, server.url)
    response = page.request.post(
        server.url + "/api/v1/replica/configure",
        data={
            "channel_id": int(BACKUP_CHANNEL),
            "bot_token": BOT,
            "independent_accounts": True,
        },
    )
    assert response.ok
    response = page.request.post(
        server.url + "/__test__/replica/protected", headers=CONTROL
    )
    assert response.ok
    section = open_storage(page)
    expect(section.locator(".channel-backup-phase")).to_contain_text("Backup confirmed")
    response = page.request.post(
        server.url + "/__test__/replica/lagging", headers=CONTROL
    )
    assert response.ok
    page.clock.fast_forward(15001)
    expect(section.locator(".channel-backup-confirmation")).to_contain_text(
        "Catalog revision 10 is waiting to be protected"
    )
    expect(section.locator(".channel-backup-phase")).to_contain_text("Ready to sync")


def test_slow_backup_poll_updates_progress_before_starting_the_next_request(
    server, desktop
):
    _, page = desktop
    page.clock.install()
    login(page, server.url)
    response = page.request.post(
        server.url + "/api/v1/replica/configure",
        data={
            "channel_id": int(BACKUP_CHANNEL),
            "bot_token": BOT,
            "independent_accounts": True,
        },
    )
    assert response.ok
    section = open_storage(page)
    progress = section.get_by_role(
        "progressbar", name="Channel copy progress", exact=True
    )
    expect(progress).to_have_attribute("value", str(1024**2))
    held = []
    requests = []
    payload = page.request.get(server.url + "/api/v1/replica").json()
    payload["done_bytes"] = 2 * 1024**2

    def delay_status(route):
        requests.append(route.request)
        if len(requests) == 1:
            held.append(route)
        else:
            route.fulfill(json=payload)

    page.route("**/api/v1/replica", delay_status)
    try:
        with page.expect_request("**/api/v1/replica"):
            page.clock.fast_forward(5001)
        # Deliver this response later than the five-second active polling interval.
        # An interval timer would start a second request and discard this progress.
        page.clock.fast_forward(6001)
        assert len(requests) == 1
        held.pop().fulfill(json=payload)
        expect(progress).to_have_attribute("value", str(2 * 1024**2))
        with page.expect_response("**/api/v1/replica"):
            page.clock.fast_forward(5001)
        assert len(requests) == 2
    finally:
        for route in held:
            route.fulfill(json=payload)
        page.unroute("**/api/v1/replica", delay_status)
