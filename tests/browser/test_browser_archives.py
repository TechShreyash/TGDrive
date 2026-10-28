"""Archive dialogs and server-job feedback on the production browser bundle.

Archive payloads and job progress are synthetic; Python archive tests exercise
the real archive readers, ZIP writer, upload pipeline and recovery behavior.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, expect, login

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e
ZIP_NAME = "Project files.zip"
ZIP_ID = "fixture-archive-source"


class ArchiveRequests:
    def __init__(self, page: Any, source_name: str = ZIP_NAME):
        self.source_name = source_name
        self.requests: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.reject: tuple[int, dict[str, str]] | None = None
        page.route("**/api/getDirectory", self.directory)
        page.route(
            "**/api/v1/jobs", lambda route: route.fulfill(json={"jobs": self.jobs})
        )
        page.route("**/api/v1/transfers/history/tasks", self.history)
        page.route("**/api/v1/jobs/fixture-archive-*/cancel", self.cancel)
        page.route("**/api/v1/archives", self.start)
        page.route("**/api/v1/archives/extract", self.start)

    def directory(self, route: Any) -> None:
        response = route.fetch()
        body = response.json()
        parent = route.request.post_data_json.get("path")
        if response.ok and parent:
            body["data"]["contents"][ZIP_ID] = {
                "id": ZIP_ID,
                "path": parent,
                "name": self.source_name,
                "type": "file",
                "size": 4096,
            }
        route.fulfill(response=response, json=body)

    def start(self, route: Any) -> None:
        payload = route.request.post_data_json
        extracting = route.request.url.endswith("/extract")
        self.requests.append({"extracting": extracting, **payload})
        if self.reject:
            status, body = self.reject
            route.fulfill(status=status, json=body)
            return
        at = time.time()
        job = {
            "id": "fixture-archive-" + str(len(self.jobs)),
            "name": self.source_name if extracting else payload["name"],
            "kind": "extract" if extracting else "archive",
            "status": "running",
            "phase": "extracting" if extracting else "archiving",
            "current": 1024,
            "total": 4096,
            "created_at": at - 10,
            "started_at": at - 5,
            "destination": payload["destination"],
            "location": {
                "available": True,
                "folder_path": "/",
                "folder_label": "My Files",
            },
        }
        self.jobs.append(job)
        route.fulfill(json={"id": job["id"], "job": job})

    def cancel(self, route: Any) -> None:
        identity = route.request.url.split("/")[-2]
        job = next(job for job in self.jobs if job["id"] == identity)
        job.update(status="cancelled", phase="cancelled", finished_at=time.time())
        route.fulfill(json=job)

    def history(self, route: Any) -> None:
        status = route.request.post_data_json["status"]
        jobs = [
            job
            for job in self.jobs
            if job["status"] in {"completed", "cancelled", "error"}
            and (status in {"all", "finished"} or status == job["status"])
        ]
        route.fulfill(
            json={
                "jobs": jobs,
                "page": 1,
                "page_size": 20,
                "total_tasks": len(jobs),
                "total_pages": 1,
                "has_more": False,
            }
        )


def item_action(page: Any, name: str, action: str) -> None:
    page.locator(f'summary[aria-label="Actions for {name}"]').click()
    page.get_by_role("button", name=action, exact=True).click()


def assert_dialog_layout(page: Any, dialog: Any) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert dialog.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
    for control in dialog.locator("input, button").all():
        if not control.is_visible():
            continue
        control.scroll_into_view_if_needed()
        assert control.evaluate("""element => {
            const rect = element.getBoundingClientRect();
            return rect.left >= 0 && rect.right <= innerWidth
                && rect.top >= 0 && rect.bottom <= innerHeight;
        }""")


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_create_zip_single_and_bulk_actions_start_server_work(server, desktop, width):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    archive = ArchiveRequests(page)
    login(page, server.url)
    item_action(page, "Private folder", "Create ZIP")
    dialog = page.get_by_role("dialog", name="Create ZIP", exact=True)
    expect(dialog.get_by_label("ZIP name", exact=True)).to_have_value(
        "Private folder.zip"
    )
    expect(dialog.get_by_label("ZIP name", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()

    page.get_by_role("button", name="Select", exact=True).click()
    page.get_by_role("checkbox", name="Select Private folder", exact=True).check()
    page.get_by_role("checkbox", name="Select 資料", exact=True).check()
    page.get_by_role("button", name="Create ZIP", exact=True).click()
    expect(
        dialog.get_by_text("2 selected files and folders", exact=True)
    ).to_be_visible()
    expect(dialog.get_by_label("ZIP name", exact=True)).to_have_value("Archive.zip")
    dialog.get_by_label("ZIP name", exact=True).fill("Together.zip")
    assert_dialog_layout(page, dialog)
    page.screenshot(path=ARTIFACTS / f"archive-create-{width}.png")
    dialog.get_by_role("button", name="Create ZIP", exact=True).click()
    expect(dialog).not_to_be_visible()
    assert len(archive.requests) == 1
    assert archive.requests[0]["name"] == "Together.zip"
    assert archive.requests[0]["destination"] == "/"
    assert len(archive.requests[0]["paths"]) == 2
    assert archive.requests[0]["allow_public_exposure"] is False

    page.goto(server.url + "/?view=activity")
    row = page.locator(".transfer-page .transfer-item").filter(has_text="Together.zip")
    expect(row.locator(".transfer-phase")).to_contain_text("ZIP")
    expect(row.get_by_role("progressbar")).to_be_visible()
    expect(row.get_by_role("button", name="Choose File", exact=True)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / f"archive-progress-{width}.png")
    with page.expect_response(lambda response: response.url.endswith("/cancel")):
        row.get_by_role("button", name="Cancel Together.zip", exact=True).click()
    page.reload()
    expect(row).to_have_attribute("data-status", "cancelled")


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_extract_zip_current_and_new_folder_keep_clear_destinations(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    if width == 1440:
        page.add_init_script("localStorage.setItem('tgdrive.appearance', 'dark')")
    archive = ArchiveRequests(page)
    login(page, server.url)
    for current_folder in (False, True):
        item_action(page, ZIP_NAME, "Extract archive")
        dialog = page.get_by_role("dialog", name="Extract archive", exact=True)
        expect(
            dialog.get_by_role("radio", name="New folder", exact=True)
        ).to_be_checked()
        expect(dialog.get_by_label("New folder name", exact=True)).to_have_value(
            "Project files"
        )
        if current_folder:
            dialog.get_by_role("radio", name="This folder", exact=True).check()
            expect(dialog.get_by_label("New folder name", exact=True)).to_have_count(0)
        else:
            dialog.get_by_label("New folder name", exact=True).fill("Unpacked project")
        assert_dialog_layout(page, dialog)
        if not current_folder:
            page.screenshot(path=ARTIFACTS / f"archive-extract-{width}.png")
        dialog.get_by_role("button", name="Extract archive", exact=True).click()
        expect(dialog).not_to_be_visible()
        payload = archive.requests[-1]
        assert payload["path"] == "/" + ZIP_ID
        assert payload["destination"] == "/"
        assert payload["folder_name"] == (
            None if current_folder else "Unpacked project"
        )
        assert payload["allow_public_exposure"] is False
    assert len(archive.requests) == 2
    if width == 1440:
        page.get_by_role("button", name="Private folder", exact=True).press("Enter")
        item_action(page, ZIP_NAME, "Extract archive")
        dialog = page.get_by_role("dialog", name="Extract archive", exact=True)
        expect(dialog.get_by_text("Current folder", exact=True)).to_be_visible()
        page.keyboard.press("Escape")


@pytest.mark.parametrize("width", [1440, 320])
@pytest.mark.parametrize(
    "source_name,folder_name",
    [
        ("Project files.7z", "Project files"),
        ("Photos.RAR", "Photos"),
        ("Source bundle.tar.gz", "Source bundle"),
        ("notes.txt.gz", "notes.txt"),
    ],
)
def test_common_archive_formats_offer_extraction_with_clear_folder_names(
    server, desktop, width, source_name, folder_name
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    archive = ArchiveRequests(page, source_name)
    login(page, server.url)
    item_action(page, source_name, "Extract archive")
    dialog = page.get_by_role("dialog", name="Extract archive", exact=True)
    expect(dialog.get_by_label("New folder name", exact=True)).to_have_value(
        folder_name
    )
    expect(dialog.get_by_label("New folder name", exact=True)).to_be_focused()
    assert_dialog_layout(page, dialog)
    if source_name.endswith(".tar.gz"):
        page.screenshot(path=ARTIFACTS / f"archive-tar-gz-{width}.png")
    dialog.get_by_role("button", name="Extract archive", exact=True).click()
    expect(dialog).not_to_be_visible()
    assert archive.requests == [
        {
            "extracting": True,
            "path": "/" + ZIP_ID,
            "destination": "/",
            "folder_name": folder_name,
            "allow_public_exposure": False,
        }
    ]


def test_archive_failures_stay_inline_and_public_destination_requires_consent(
    server, desktop
):
    _, page = desktop
    archive = ArchiveRequests(page)
    login(page, server.url)
    item_action(page, "Private folder", "Create ZIP")
    dialog = page.get_by_role("dialog", name="Create ZIP", exact=True)
    submit = dialog.get_by_role("button", name="Create ZIP", exact=True)
    name = dialog.get_by_label("ZIP name", exact=True)
    name.fill("")
    expect(submit).to_be_disabled()
    name.fill("Shared bundle.zip")
    archive.reject = (
        507,
        {"detail": "Not enough temporary storage. Free space and retry."},
    )
    submit.click()
    expect(dialog.get_by_role("alert")).to_contain_text("Not enough temporary storage")
    expect(name).to_have_value("Shared bundle.zip")
    expect(submit).to_be_enabled()

    archive.reject = (
        409,
        {"detail": "Destination is public", "code": "PUBLIC_EXPOSURE"},
    )
    submit.click()
    consent = dialog.get_by_role(
        "checkbox", name="I understand that files created here may become public."
    )
    expect(consent).to_be_visible()
    expect(submit).to_be_disabled()
    consent.check()
    expect(submit).to_be_enabled()
    archive.reject = None
    submit.click()
    expect(dialog).not_to_be_visible()
    assert [row["allow_public_exposure"] for row in archive.requests] == [
        False,
        False,
        True,
    ]


def test_completed_extraction_opens_its_new_output_folder(server, desktop):
    _, page = desktop
    archive = ArchiveRequests(page)
    location_requests = []
    login(page, server.url)
    assert page.request.post(
        server.url + "/api/createNewFolder",
        data={"path": "/", "name": "Unpacked project"},
    ).ok
    contents = page.request.post(
        server.url + "/api/getDirectory", data={"path": "/"}
    ).json()["data"]["contents"]
    folder = next(
        item for item in contents.values() if item["name"] == "Unpacked project"
    )
    destination = "/" + folder["id"]
    location = {
        "available": True,
        "folder_path": destination,
        "folder_label": "My Files / Unpacked project",
    }

    def locate(route):
        payload = route.request.post_data_json
        location_requests.append(payload)
        route.fulfill(
            json=location
            if payload["destination"] == destination
            else {"available": True, "folder_path": "/", "folder_label": "My Files"}
        )

    def output_directory(route):
        if route.request.post_data_json["path"] != destination:
            route.fallback()
            return
        route.fulfill(
            json={
                "data": {
                    "name": "Unpacked project",
                    "contents": {
                        "extracted-note": {
                            "id": "extracted-note",
                            "path": destination,
                            "name": "Extracted note.txt",
                            "type": "file",
                            "size": 8192,
                        }
                    },
                }
            }
        )

    page.route("**/api/v1/transfers/location", locate)
    page.route("**/api/getDirectory", output_directory)
    item_action(page, ZIP_NAME, "Extract archive")
    dialog = page.get_by_role("dialog", name="Extract archive", exact=True)
    dialog.get_by_label("New folder name", exact=True).fill("Unpacked project")
    dialog.get_by_role("button", name="Extract archive", exact=True).click()
    expect(dialog).not_to_be_visible()
    page.goto(server.url + "/?view=activity")
    row = page.locator(".transfer-page .transfer-item").filter(has_text=ZIP_NAME)
    expect(row).to_have_attribute("data-status", "uploading")
    archive.jobs[0].update(
        status="completed",
        phase="completed",
        current=8192,
        total=8192,
        destination=destination,
        location=location,
        finished_at=time.time(),
    )
    expect(row).to_have_attribute("data-status", "completed", timeout=15000)
    expect(row.locator(".transfer-location")).to_have_text(
        "My Files / Unpacked project"
    )
    expect(row.locator(".transfer-bytes")).to_contain_text("8 KB")
    row.get_by_role(
        "button", name="Show " + ZIP_NAME + " in folder", exact=True
    ).click()
    expect(
        page.get_by_role("button", name="Extracted note.txt", exact=True)
    ).to_be_visible()
    assert location_requests == [
        {
            "transfer_id": archive.jobs[0]["id"],
            "destination": destination,
            "completed": False,
        }
    ]
    assert parse_qs(urlparse(page.url).query)["path"] == [destination]
    page.screenshot(path=ARTIFACTS / "archive-output-navigation.png")
