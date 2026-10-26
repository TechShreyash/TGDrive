"""Device receipts flow independently of Telegram and update listings in place."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    UPLOAD_RECOVERY_RECORDS,
    Server,
    expect,
    fixture_nodes,
    login,
    select_folder,
    select_item,
    upload,
)
from tests.browser.test_browser_e2e import browser as browser_fixture
from tests.browser.test_browser_e2e import desktop as desktop_fixture
from tests.browser.test_browser_e2e import server as server_fixture

pytestmark = pytest.mark.e2e
browser, desktop, server = browser_fixture, desktop_fixture, server_fixture


def _wait_started(page: Any, server: Server, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        started = server.control()["started"]
        if len(started) >= count:
            return started
        # Keep servicing browser routing callbacks while receipt requests run.
        page.wait_for_timeout(50)
    pytest.fail(f"Expected {count} accepted uploads: {server.control()}")


def _wait_completed(server: Server, started: list[dict[str, Any]]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        records = server.control()["records"]
        if all(records[item["id"]]["status"] == "completed" for item in started):
            return
        time.sleep(0.05)
    pytest.fail(f"Server uploads did not finish: {server.control()}")


def _device_close_blocked(page: Any) -> bool:
    return page.evaluate("""() => {
      const event=new Event('beforeunload', {cancelable:true});
      window.dispatchEvent(event);
      return event.defaultPrevented;
    }""")


@pytest.mark.parametrize("selection", ["files", "folder"])
def test_device_receipts_finish_while_telegram_waits_and_survive_tab_close(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path, selection: str
) -> None:
    context, page = desktop
    login(page, server.url)
    names = [f"pipeline-{index:02}.txt" for index in range(8)]
    held: list[Any] = []
    first_patch = True

    def hold_later_receipts(route: Any) -> None:
        nonlocal first_patch
        if route.request.method != "PATCH":
            route.continue_()
        elif first_patch:
            first_patch = False
            route.continue_()
        else:
            held.append(route)

    page.route("**/api/v1/uploads/*", hold_later_receipts)
    try:
        if selection == "folder":
            directory = tmp_path / "Flow folder"
            directory.mkdir()
            for name in names:
                (directory / name).write_bytes(b"x" * 65536)
            dialog = select_folder(page, directory)
            dialog.get_by_role("button", name="Upload", exact=True).click()
            expect(dialog).not_to_be_visible()
        else:
            upload(page, names)
        first = _wait_started(page, server, 1)
        assert len(first) == 1, "Telegram must start before the remaining receipts"
        assert held, "Other device receipts must still be in flight"
        assert all(
            record["status"] != "completed"
            for record in server.control()["records"].values()
        )
        assert _device_close_blocked(page), "Unsent device data must guard tab close"
        if selection == "folder":
            batch = page.locator('.transfer-batch[data-stage="device"]')
            expect(batch).to_have_count(1)
            expect(batch.locator(".transfer-batch-status")).to_contain_text(
                "Uploading from device"
            )
            expect(batch).not_to_contain_text("Telegram")
            batch.get_by_role("button", name="Show Files", exact=True).click()
            received = batch.locator('.transfer-child[data-status="uploading"]')
        else:
            expect(page.locator(".transfer-batch")).to_have_count(0)
            rows = page.locator(".transfer-item:not(.transfer-child)")
            expect(rows).to_have_count(len(names))
            expect(
                page.get_by_role("button", name="Show Files", exact=True)
            ).to_have_count(0)
            received = page.locator(
                '.transfer-item:not(.transfer-child)[data-status="uploading"]'
            )
        expect(received).to_have_count(1)
        expect(received.locator(".transfer-phase")).to_have_text("Received")
        expect(received.get_by_role("progressbar")).to_have_count(0)
        expect(received.locator(".transfer-metrics")).to_have_count(0)
        if selection == "folder":
            batch.get_by_role("button", name="Hide Files", exact=True).click()
    finally:
        # Playwright releases any pending routes when this handler is removed.
        page.unroute("**/api/v1/uploads/*", hold_later_receipts)

    started = _wait_started(page, server, len(names))
    assert {item["name"] for item in started} == set(names)
    assert all(
        record["status"] != "completed"
        for record in server.control()["records"].values()
    ), "Receiving later files must not require earlier Telegram jobs to finish"
    safe = page.locator(".transfer-device-status, .transfer-notice").filter(
        has_text="You can close this tab"
    )
    expect(safe.first).to_be_visible()
    if selection == "folder":
        batch = page.locator('.transfer-batch[data-stage="telegram"]')
        expect(batch).to_have_count(1)
        expect(batch.locator(".transfer-batch-status")).to_contain_text(
            "Uploading to Telegram"
        )
        expect(batch).not_to_contain_text("Uploading from device")
    else:
        expect(page.locator(".transfer-batch")).to_have_count(0)
        rows = page.locator(".transfer-item:not(.transfer-child)")
        expect(rows).to_have_count(len(names))
        expect(rows.locator(".transfer-phase")).to_have_text(
            ["Uploading to Telegram"] * len(names)
        )
        for name in names:
            expect(
                rows.locator(".transfer-item-name").filter(has_text=name)
            ).to_have_count(1)
    assert not _device_close_blocked(page)
    saved = page.evaluate(UPLOAD_RECOVERY_RECORDS)
    assert len(saved) == len(names)
    assert all(item.get("jobId") for item in saved)
    if selection == "folder":
        assert len({item["folderUpload"]["id"] for item in saved}) == 1
    else:
        assert all(not item.get("folderUpload") for item in saved)
        assert len({item["batchId"] for item in saved}) == len(names)
    destination = started[0]["destination"]
    assert {item["destination"] for item in started} == {destination}
    assert set(names).isdisjoint(
        item["name"] for item in fixture_nodes(page, server, destination)
    )
    page.screenshot(path=ARTIFACTS / f"pipeline-{selection}-safe-to-close.png")

    dialogs: list[str] = []

    def accept_unexpected_dialog(dialog: Any) -> None:
        dialogs.append(dialog.type)
        dialog.accept()

    page.on("dialog", accept_unexpected_dialog)
    page.close(run_before_unload=True)
    reopened = context.new_page()
    assert page.is_closed()
    assert dialogs == [], "Confirmed server receipts must remove the close warning"
    for item in started:
        server.finish(item["id"])
    _wait_completed(server, started)

    errors: list[str] = []
    reopened.on("pageerror", lambda error: errors.append(str(error)))
    reopened.goto(server.url + "/?" + urlencode({"path": destination}))
    for name in names:
        expect(
            reopened.locator(".item-name-button").filter(has_text=name)
        ).to_have_count(1)
    assert set(names).issubset(
        item["name"] for item in fixture_nodes(reopened, server, destination)
    )
    assert errors == []
    reopened.screenshot(path=ARTIFACTS / f"pipeline-{selection}-server-finished.png")


def test_slow_listing_refresh_coalesces_upload_bursts_without_unmounting_rows(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    held: list[tuple[Any, Any, dict[str, Any]]] = []
    refresh_requests: list[Any] = []
    failed_requests: list[str] = []
    recording = False
    hold = False

    def listing(route: Any) -> None:
        response = route.fetch()
        payload = response.json()
        root = route.request.post_data_json["path"] == "/"
        if root:
            payload["data"]["contents"].update(
                {
                    f"stable-{index}": {
                        "id": f"stable-{index}",
                        "name": f"stable-{index:03}.txt",
                        "type": "file",
                        "path": "/",
                        "size": 10,
                    }
                    for index in range(130)
                }
            )
            if recording:
                refresh_requests.append(route.request)
        if root and hold:
            held.append((route, response, payload))
        else:
            route.fulfill(response=response, json=payload)

    def failed(request: Any) -> None:
        if recording and request.url.endswith("/api/getDirectory"):
            failed_requests.append(request.failure or "Listing failed")

    page.route("**/api/getDirectory", listing)
    page.on("requestfailed", failed)
    login(page, server.url)
    names = [f"zz-upload-{index}.txt" for index in range(6)]
    upload(page, names)
    started = _wait_started(page, server, len(names))
    expect(
        page.locator(".transfer-device-status, .transfer-notice")
        .filter(has_text="You can close this tab")
        .first
    ).to_be_visible()
    hide = page.get_by_role("button", name="Hide Transfer Details", exact=True)
    if hide.is_visible():
        hide.click()
    next_page = page.get_by_role("button", name="Next Page", exact=True)
    next_page.focus()
    next_page.press("Enter")
    select_item(page, "stable-110.txt")
    checkbox = page.get_by_label("Select stable-110.txt", exact=True)
    checkbox.focus()
    page.evaluate("""() => {
      window.stableTable=document.querySelector('.file-table');
      window.stableRow=document.querySelector('[data-item-id="stable-110"]');
      window.tableRemovals=0;
      new MutationObserver(records => {
        for(const record of records) for(const removed of record.removedNodes)
          if(removed===window.stableTable || removed.contains?.(window.stableTable))
            window.tableRemovals++;
      }).observe(document.querySelector('.workspace'), {childList:true,subtree:true});
      window.stableScroll=document.querySelector('.file-list-scroll');
      window.stableScroll.scrollTop=250;
    }""")
    scroll = page.evaluate("window.stableScroll.scrollTop")
    assert scroll > 0
    recording = True
    hold = True
    try:
        page.evaluate("window.dispatchEvent(new Event('tgdrive:files-changed'))")
        deadline = time.monotonic() + 10
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert len(held) == 1
        for item in started:
            server.finish(item["id"])
        page.wait_for_function(
            "() => { const items="
            + UPLOAD_RECOVERY_RECORDS
            + "; return items.length===6 && "
            "items.every(item=>item.state==='completed'); }"
        )
        page.evaluate("""async () => {
          for(let index=0;index<30;index++)
            window.dispatchEvent(new Event('tgdrive:files-changed'));
          await new Promise(resolve=>requestAnimationFrame(
            ()=>requestAnimationFrame(resolve)));
        }""")
        assert len(refresh_requests) == 1, "A slow fetch must finish before its retry"
        assert failed_requests == [], "Upload updates must not abort listing requests"
        expect(page.locator(".loading-state")).to_have_count(0)
        expect(checkbox).to_be_checked()
        expect(checkbox).to_be_focused()
        hold = False
        route, response, payload = held.pop()
        route.fulfill(response=response, json=payload)
        for name in names:
            expect(
                page.locator(".item-name-button").filter(has_text=name)
            ).to_have_count(1)
        assert len(refresh_requests) == 2, "Completion bursts need one trailing fetch"
        assert failed_requests == []
        expect(checkbox).to_be_checked()
        expect(checkbox).to_be_focused()
        expect(page.locator(".pagination")).to_contain_text("Showing 101\u2013")
        assert page.evaluate("window.stableScroll.scrollTop") == scroll
        assert page.evaluate(
            "window.stableTable===document.querySelector('.file-table')"
        )
        assert page.evaluate(
            "window.stableRow===document.querySelector('[data-item-id=stable-110]')"
        )
        assert page.evaluate("window.tableRemovals") == 0
        page.screenshot(path=ARTIFACTS / "pipeline-listing-burst-preserves-state.png")
    finally:
        hold = False
        for route, response, payload in held:
            route.fulfill(response=response, json=payload)


def test_new_folder_gets_device_slots_while_older_folder_and_telegram_are_pending(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path
) -> None:
    _, page = desktop
    login(page, server.url)
    receipt_names: dict[str, str] = {}
    held: list[Any] = []

    def remember_receipt(route: Any) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        response = route.fetch()
        assert response.ok, response.text()
        receipt_names[response.json()["id"]] = route.request.post_data_json["name"]
        route.fulfill(response=response)

    def hold_old_payloads(route: Any) -> None:
        receipt_id = route.request.url.rsplit("/", 1)[-1]
        if route.request.method == "PATCH" and receipt_names[receipt_id].startswith(
            "older-"
        ):
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/uploads", remember_receipt)
    page.route("**/api/v1/uploads/*", hold_old_payloads)
    try:
        older = tmp_path / "Older folder"
        newer = tmp_path / "New folder"
        older.mkdir()
        newer.mkdir()
        for index in range(8):
            (older / f"older-{index}.txt").write_bytes(b"x" * 65536)
        for index in range(2):
            (newer / f"newer-{index}.txt").write_bytes(b"y" * 65536)
        dialog = select_folder(page, older)
        dialog.get_by_role("button", name="Upload", exact=True).click()
        expect(dialog).not_to_be_visible()
        deadline = time.monotonic() + 10
        while len(held) < 2 and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert len(held) == 2

        dialog = select_folder(page, newer)
        dialog.get_by_role("button", name="Upload", exact=True).click()
        expect(dialog).not_to_be_visible()
        # Existing requests continue; released slots must be shared with the new batch.
        initial = held[:]
        held.clear()
        for route in initial:
            route.continue_()
        deadline = time.monotonic() + 10
        started = []
        while time.monotonic() < deadline:
            started = server.control()["started"]
            if sum(item["name"].startswith("newer-") for item in started) == 2:
                break
            page.wait_for_timeout(50)
        assert sum(item["name"].startswith("newer-") for item in started) == 2
        assert sum(item["name"].startswith("older-") for item in started) < 8
        assert all(
            record["status"] != "completed"
            for record in server.control()["records"].values()
        ), "The new folder must not wait for older Telegram jobs"
        new_batch = page.locator(".transfer-batch").filter(
            has=page.locator(".transfer-batch-name", has_text="New folder")
        )
        expect(new_batch).to_have_attribute("data-stage", "device")
        expect(new_batch).to_contain_text("Received")
        expect(new_batch).not_to_contain_text("Uploading to Telegram")
        expect(new_batch.locator(".transfer-batch-throughput")).to_have_text("")
        expect(new_batch).to_contain_text("Keep this tab open for other uploads")
        assert _device_close_blocked(page)
        page.screenshot(path=ARTIFACTS / "pipeline-new-folder-fair-slots.png")
    finally:
        page.unroute("**/api/v1/uploads/*", hold_old_payloads)
        page.unroute("**/api/v1/uploads", remember_receipt)
    _wait_started(page, server, 10)
    expect(new_batch).to_have_attribute("data-stage", "telegram")
    expect(new_batch).to_contain_text("Uploading to Telegram")
    assert not _device_close_blocked(page)
