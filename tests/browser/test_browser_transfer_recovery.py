"""Local recovery remains usable and resume feedback stays with its upload."""

from __future__ import annotations

import json
import re

import pytest

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, expect, login
from tests.browser.test_browser_upload_progress import _choose_upload

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


def recovery_row(name, **changes):
    return {
        "key": name,
        "name": name,
        "size": 4,
        "lastModified": 0,
        "destination": "/",
        "state": "awaiting_file",
        "phase": "Choose the original file to resume",
        "current": 0,
        "total": 4,
        "initiatedHere": True,
        "createdAt": 1000,
        **changes,
    }


def seed_recovery(page, rows):
    page.add_init_script(
        "localStorage.setItem('tgdrive.uploads.v2', "
        + json.dumps(json.dumps(rows))
        + ");"
    )


@pytest.mark.parametrize(
    "width,height,theme", [(1536, 695, "dark"), (390, 844, "light")]
)
def test_interrupted_uploads_show_the_right_recovery_action(
    server, browser, width, height, theme
):
    fixtures.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    touch = width <= 720
    page = browser.new_page(
        viewport={"width": width, "height": height},
        color_scheme=theme,
        has_touch=touch,
        is_mobile=touch,
    )
    try:
        jobs = [
            {
                "id": name,
                "name": name + ".bin",
                "kind": "upload",
                "status": "error",
                "phase": "interrupted",
                "current": 4,
                "total": 4,
                "error": "TGDrive stopped while this transfer was running.",
                "interrupted_upload_recovery": guidance,
            }
            for name, guidance in (
                ("staged", "staged"),
                ("missing", "original_required"),
            )
        ]
        held = []

        def list_jobs(route):
            if hold_jobs:
                held.append(route)
            else:
                route.fulfill(json={"jobs": jobs})

        hold_jobs = True
        page.route("**/api/v1/jobs", list_jobs)
        page.route(
            "**/api/v1/transfers/history/tasks",
            lambda route: route.fulfill(
                json={
                    "jobs": jobs,
                    "page": 1,
                    "page_size": 50,
                    "total_tasks": len(jobs),
                    "total_pages": 1,
                    "has_more": False,
                }
            ),
        )

        def retry(route):
            jobs[0].update(status="completed", phase="completed", error=None)
            jobs[0].pop("interrupted_upload_recovery")
            route.fulfill(json=jobs[0])

        page.route("**/api/v1/jobs/staged/retry", retry)
        login(page, server.url + "/?view=activity")
        staged = page.get_by_role("listitem").filter(has_text="staged.bin")
        missing = page.get_by_role("listitem").filter(has_text="missing.bin")
        expect(staged).to_contain_text("Interrupted by restart")
        expect(staged.get_by_role("button", name="Retry staged.bin")).to_have_count(0)
        assert held
        hold_jobs = False
        for route in held:
            route.fulfill(json={"jobs": jobs})
        expect(staged).to_contain_text(
            "Retry uses the file already staged on the server."
        )
        expect(missing).to_contain_text(
            "Cancel this transfer, then upload the original again."
        )
        expect(missing.get_by_role("button", name="Retry missing.bin")).to_have_count(0)
        expect(missing.get_by_role("button", name="Cancel missing.bin")).to_be_visible()
        expect(staged).to_contain_text("TGDrive stopped")
        expect(staged).not_to_contain_text("99%")
        page.screenshot(
            path=ARTIFACTS / f"bug-16-interrupted-{width}.png", animations="disabled"
        )
        retry_button = staged.get_by_role("button", name="Retry staged.bin")
        with page.expect_response("**/api/v1/jobs/staged/retry") as response:
            if touch:
                retry_button.tap()
            else:
                retry_button.press("Enter")
        assert response.value.ok
        expect(staged).to_have_attribute("data-status", "completed")
        expect(staged).not_to_contain_text("Retry uses the file")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.context.close()


def test_saved_recovery_works_when_server_history_fails(server, desktop, tmp_path):
    _, page = desktop
    seed_recovery(page, [recovery_row("recover.bin", state="error")])
    page.route(
        "**/api/v1/transfers/history/tasks",
        lambda route: route.fulfill(
            status=503, json={"detail": "Saved history is temporarily unavailable"}
        ),
    )
    login(page, server.url)
    page.get_by_role("link", name=re.compile(r"^Transfers(?: \d+)?$")).click()
    row = page.locator(".transfer-item").filter(has_text="recover.bin")
    expect(page.get_by_text("Saved history is temporarily unavailable")).to_be_visible()
    expect(row.get_by_role("button", name="Choose File", exact=True)).to_be_visible()
    expect(
        row.get_by_role("button", name="Cancel recover.bin", exact=True)
    ).to_be_visible()

    original = tmp_path / "recover.bin"
    original.write_bytes(b"data")
    with page.expect_file_chooser() as chooser:
        row.get_by_role("button", name="Choose File", exact=True).click()
    chooser.value.set_files(str(original))

    server.wait_started(1)
    expect(row.get_by_role("button", name="Choose File", exact=True)).to_have_count(0)


def test_resume_result_is_scoped_dismissible_and_clears_when_resolved(
    server, desktop, tmp_path
):
    _, page = desktop
    folder = {
        "id": "browser-recovery-folder-0001",
        "name": "jee",
        "root_path": "/fixture-jee",
        "created_at": 1,
    }
    seed_recovery(
        page,
        [
            recovery_row(
                "received.bin",
                relativePath="jee/received.bin",
                folderUpload=folder,
                state="completed",
                current=4,
                jobId="previously-completed-job",
            ),
            recovery_row(
                "missing.bin", relativePath="jee/missing.bin", folderUpload=folder
            ),
        ],
    )
    originals = tmp_path / "jee"
    originals.mkdir()
    (originals / "received.bin").write_bytes(b"data")
    login(page, server.url)
    batch = page.locator(".transfer-batch").filter(
        has=page.get_by_text("jee", exact=True)
    )

    def reselect():
        with page.expect_file_chooser() as chooser:
            batch.get_by_role("button", name="Choose Folder", exact=True).click()
        chooser.value.set_files(str(originals))
        expect(batch.locator(".transfer-notice")).to_contain_text(
            "0 files added to the resume queue."
        )
        expect(batch.locator(".transfer-notice")).to_contain_text(
            "1 still need their original files."
        )

    reselect()
    _choose_upload(page, "unrelated.bin", 4)
    expect(page.locator("#transfer-dialog > .transfer-notice")).to_have_count(0)
    expect(batch.locator(".transfer-notice")).to_be_visible()
    page.get_by_role("button", name="Dismiss resume result for jee", exact=True).click()
    expect(page.locator(".transfer-notice")).to_have_count(0)

    reselect()
    batch.get_by_role("button", name="Show Files", exact=True).click()
    batch.get_by_role("button", name="Cancel missing.bin", exact=True).click()
    expect(page.locator(".transfer-notice")).to_have_count(0)


@pytest.mark.parametrize("width", [1440, 390])
def test_collapsed_panel_shows_running_finished_and_blocked_work(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)
    _choose_upload(page, "compact-status.bin", 4)
    started = server.wait_started(1)
    toggle = page.locator("#transfer-toggle")

    def collapse():
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()

    mobile = width <= 720
    if mobile:
        status = page.get_by_role("button", name="Open transfer progress", exact=True)
        expect(status).to_have_text("1 upload in progress")
    else:
        panel = page.locator("main > #transfer-dialog")
        expect(panel).to_be_in_viewport(ratio=1)
        assert panel.evaluate("""element => {
            const workspace = element.previousElementSibling;
            return workspace.matches(".workspace") &&
                workspace.getBoundingClientRect().bottom <=
                    element.getBoundingClientRect().top;
        }""")
        collapse()
        status = page.locator("#transfer-summary .transfer-collapsed-status")
        expect(status).to_contain_text("1 uploading")
        expect(status).to_contain_text("You can close this tab")
        expect(page.locator("#transfer-list")).to_be_hidden()
    page.screenshot(path=ARTIFACTS / f"collapsed-upload-running-{width}.png")

    server.finish(started[0]["id"])
    if mobile:
        expect(status).to_have_count(0)
    else:
        expect(status).to_have_text("Uploads complete")
    page.screenshot(path=ARTIFACTS / f"collapsed-upload-complete-{width}.png")

    seed_recovery(page, [recovery_row("original-needed.bin")])
    page.reload()
    if mobile:
        expect(status).to_have_text("Uploads need attention")
    else:
        expect(toggle).to_be_visible()
        collapse()
        expect(status).to_contain_text("1 needs attention")
        expect(status).to_contain_text("Open details to continue")
    expect(status).not_to_contain_text("uploading")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert status.evaluate("node => node.scrollWidth <= node.clientWidth + 1")
    page.screenshot(path=ARTIFACTS / f"collapsed-upload-attention-{width}.png")
    if mobile:
        status.click()
        expect(page.locator(".transfer-page")).to_be_visible()
        expect(
            page.get_by_role("button", name="Choose File", exact=True)
        ).to_be_visible()
