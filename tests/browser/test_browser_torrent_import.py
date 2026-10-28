"""Torrent import controls and recovery against synthetic HTTP job responses."""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    UPLOAD_RECOVERY_RECORDS,
    Server,
    expect,
    login,
)
from tests.browser.test_browser_e2e import browser as browser_fixture
from tests.browser.test_browser_e2e import desktop as desktop_fixture
from tests.browser.test_browser_e2e import server as server_fixture

pytestmark = pytest.mark.e2e
browser, desktop, server = browser_fixture, desktop_fixture, server_fixture


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_torrent_import_metadata_and_reload_keep_server_identity(
    server: Server, desktop: tuple[Any, Any], width: int
) -> None:
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 960})
    job = {
        "id": "torrent-server-job",
        "name": "Sample folder",
        "kind": "torrent",
        "source_kind": "torrent",
        "status": "running",
        "phase": "torrent_metadata",
        "current": 0,
        "total": 0,
        "destination": "/",
    }
    starts: list[dict[str, Any]] = []

    def start(route: Any) -> None:
        starts.append(route.request.post_data_json)
        route.fulfill(json={"status": "ok", "id": job["id"]})

    page.route("**/api/startTorrentDownload", start)
    page.route(
        "**/api/getTransferProgress",
        lambda route: route.fulfill(json={"status": "ok", "data": job}),
    )
    page.route(
        "**/api/v1/jobs",
        lambda route: route.fulfill(json={"jobs": [job] if starts else []}),
    )
    login(page, server.url)
    page.locator("#new-button:visible, #compact-new-button:visible").click()
    page.get_by_role("button", name="Import torrent", exact=True).click()
    dialog = page.get_by_role("dialog", name="Import torrent", exact=True)
    expect(dialog).to_be_visible()
    source = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Sample%20folder"
    dialog.get_by_label("Magnet link or torrent URL", exact=True).fill(source)
    expect(dialog.get_by_label("Compatibility Mode", exact=False)).to_have_count(0)
    assert dialog.evaluate(
        "el => el.getBoundingClientRect().right <= window.innerWidth"
    )
    page.screenshot(path=ARTIFACTS / f"torrent-dialog-{width}.png")
    dialog.get_by_role("button", name="Start Import", exact=True).click()
    expect(dialog).not_to_be_visible()
    if width <= 720:
        page.get_by_role("button", name="Open transfer progress", exact=True).click()
    else:
        expect(page.locator(".transfer-center")).to_be_visible()
    toggle = page.get_by_role("button", name="Show Transfer Details", exact=True)
    if toggle.count():
        toggle.click()
    expect(page.get_by_text("Reading torrent metadata…", exact=True)).to_be_visible()
    meter = page.locator(".transfer-precheck-progress")
    expect(meter).to_be_visible()
    expect(meter).not_to_have_attribute("aria-valuenow", "0")
    expect(page.locator(".transfer-item progress")).to_have_count(0)
    assert len(starts) == 1
    assert starts[0]["url"] == source
    assert starts[0]["path"] == "/"
    assert starts[0]["client_request_id"]
    page.reload()
    expect(page.get_by_text("Reading torrent metadata…", exact=True)).to_be_visible()
    expect(
        page.get_by_text("Choose the original file to resume", exact=True)
    ).to_have_count(0)
    saved = page.evaluate(UPLOAD_RECOVERY_RECORDS)
    receipt = next(item for item in saved if item.get("jobId") == job["id"])
    assert receipt["kind"] == "torrent"
    assert receipt["state"] == "uploading"
    assert len(starts) == 1
    page.screenshot(path=ARTIFACTS / f"torrent-reloaded-{width}.png")
