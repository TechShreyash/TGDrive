"""Native Chromium upload progress, cancellation and safe retry coverage."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest

from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    UPLOAD_RECOVERY_RECORDS,
    Server,
    expect,
    login,
)

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def _choose_upload(page: Any, name: str, size: int) -> None:
    page.locator("#new-button:visible, #compact-new-button:visible").click()
    with page.expect_file_chooser() as chooser:
        page.locator("#file-upload-action").click()
    chooser.value.set_files(
        {"name": name, "mimeType": "application/octet-stream", "buffer": b"x" * size}
    )


def _observe_and_throttle(context: Any, page: Any) -> Any:
    page.add_init_script("""
      window.uploadObservations=[];
      const original=XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.send=function(body){
        this.upload.addEventListener('progress',event=>{
          window.uploadObservations.push({bytes:event.loaded,total:event.total,at:performance.now()});
        });
        return original.call(this,body);
      };
    """)
    cdp = context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send(
        "Network.emulateNetworkConditions",
        {
            "offline": False,
            "latency": 0,
            "downloadThroughput": -1,
            "uploadThroughput": 256 * 1024,
        },
    )
    return cdp


def test_device_upload_reports_bytes_before_first_chunk_receipt(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    context, page = desktop
    _observe_and_throttle(context, page)
    login(page, server.url)
    size = 2 * 1024 * 1024
    _choose_upload(page, "measured-device-upload.bin", size)
    row = page.locator(".transfer-item").filter(has_text="measured-device-upload.bin")
    page.wait_for_function(
        """() => {
          const row=document.querySelector('.transfer-item[data-status="receiving"]');
          const progress=row?.querySelector('progress');
          return progress && progress.value>0.05 && progress.value<0.8;
        }"""
    )
    expect(row.locator(".transfer-phase")).to_have_text("Uploading from your device")
    records = page.evaluate("window.uploadObservations")
    assert len(records) >= 2, records
    saved = page.evaluate(UPLOAD_RECOVERY_RECORDS)
    assert saved[0]["current"] == 0, "In-flight bytes must not become a resume offset"
    assert "sentCurrent" not in saved[0]
    value = float(row.get_by_role("progressbar").get_attribute("value"))
    assert 0 < value <= records[-1]["bytes"] / size
    assert row.locator(".transfer-bytes").inner_text() != "0 B / 2 MB"
    expect(row.locator(".transfer-speed")).to_have_text(
        re.compile(r"^\d[\d.,]* (?:B|KB|MB|GB)/s$")
    )
    expect(row.locator(".transfer-eta")).to_contain_text("remaining")
    expect(row.locator(".transfer-eta")).to_have_attribute(
        "title",
        "Estimated upload time for this stage. "
        "Verification and publishing may take longer.",
    )
    page.screenshot(
        path=ARTIFACTS / "device-upload-observed-progress.png", full_page=True
    )
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(
        path=ARTIFACTS / "device-upload-observed-progress-mobile.png", full_page=True
    )
    page.locator(".mobile-transfer-status").click()
    server.wait_started(1)
    expect(row).to_have_attribute("data-status", "uploading")
    records = page.evaluate("window.uploadObservations")
    assert len({record["bytes"] for record in records}) >= 5
    assert all(record["bytes"] <= size for record in records)
    (ARTIFACTS / "device-upload-progress.json").write_text(
        json.dumps(records, indent=2)
    )


def test_native_upload_cancel_stops_before_telegram_job(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    context, page = desktop
    _observe_and_throttle(context, page)
    page.emulate_media(reduced_motion="reduce")
    login(page, server.url)
    _choose_upload(page, "cancel-device-upload.bin", 2 * 1024 * 1024)
    row = page.locator(".transfer-item").filter(has_text="cancel-device-upload.bin")
    page.wait_for_function(
        """() => {
          const row=document.querySelector('.transfer-item[data-status="receiving"]');
          return row?.querySelector('progress')?.value>0.05;
        }"""
    )
    expect(row.get_by_role("progressbar")).to_be_visible()
    page.get_by_role(
        "button", name="Cancel cancel-device-upload.bin", exact=True
    ).click()
    expect(row).to_have_attribute("data-status", "cancelled")
    assert server.control()["started"] == []
    page.wait_for_timeout(500)
    assert server.control()["started"] == []


def test_native_upload_retries_rejected_chunk_from_confirmed_offset(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    offsets: list[str] = []

    def reject_first(route: Any) -> None:
        if route.request.method != "PATCH":
            route.continue_()
            return
        offsets.append(route.request.headers["upload-offset"])
        if len(offsets) == 1:
            route.fulfill(status=409, json={"detail": "Synthetic rejected receipt"})
        else:
            route.continue_()

    page.route("**/api/v1/uploads/*", reject_first)
    _choose_upload(page, "retry-device-upload.bin", 256 * 1024)
    row = page.locator(".transfer-item").filter(has_text="retry-device-upload.bin")
    expect(row).to_have_attribute("data-status", "error")
    page.get_by_role("button", name="Retry retry-device-upload.bin", exact=True).click()
    expect(row).to_have_attribute("data-status", "uploading")
    server.wait_started(1)
    assert offsets == ["0", "0"]


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_server_transfer_speed_and_eta_are_visible_and_expire_together(
    server: Server, desktop: tuple[Any, Any], width: int
) -> None:
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    mode = ["fresh"]

    def jobs(route: Any) -> None:
        at = time.time()
        state = mode[0]
        route.fulfill(
            json={
                "jobs": [
                    {
                        "id": "timed-server-transfer",
                        "name": "Timed transfer.mp4",
                        "kind": "upload",
                        "status": "running",
                        "phase": "retry_wait" if state == "waiting" else "uploading",
                        "current": 512 * 1024,
                        "total": 1024 * 1024,
                        "created_at": at - 30,
                        "started_at": at - 10,
                        "progress_updated_at": at - 30 if state == "stale" else at,
                        "speed_bytes_per_second": None
                        if state == "calculating"
                        else 128 * 1024,
                        "eta_seconds": None,
                    }
                ]
            }
        )

    page.route("**/api/v1/jobs", jobs)
    login(page, server.url)
    page.goto(server.url + "/?view=activity")
    row = page.locator(".transfer-page .transfer-item").filter(
        has_text="Timed transfer.mp4"
    )
    expect(row.locator(".transfer-phase")).to_have_text("Uploading to Telegram")
    expect(row.locator(".transfer-speed")).to_have_text("128 KB/s")
    expect(row.locator(".transfer-eta")).to_have_text("About 4s remaining")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    geometry = row.evaluate("""row => {
      const box = selector => {
        const value = row.querySelector(selector).getBoundingClientRect();
        return {top:value.top,bottom:value.bottom,left:value.left,right:value.right};
      };
      return {
        speed:box('.transfer-speed'), eta:box('.transfer-eta'),
        percent:box('.transfer-batch-percent'), bytes:box('.transfer-bytes'),
        progress:box('progress'), phase:box('.transfer-phase'),
        name:box('.transfer-item-title'), row:row.getBoundingClientRect().toJSON()
      };
    }""")
    metrics = [geometry[key] for key in ("speed", "eta", "percent", "bytes")]
    assert all(metric["bottom"] < geometry["progress"]["top"] for metric in metrics)
    assert all(metric["left"] >= geometry["row"]["left"] for metric in metrics)
    assert all(metric["right"] <= geometry["row"]["right"] for metric in metrics)
    if width == 1440:
        assert (
            max(metric["bottom"] for metric in metrics)
            - min(metric["bottom"] for metric in metrics)
            < 2
        )
        assert geometry["speed"]["left"] > geometry["name"]["right"]
    else:
        assert geometry["speed"]["top"] >= geometry["phase"]["bottom"]
    page.screenshot(path=ARTIFACTS / f"transfer-speed-eta-{width}.png", full_page=True)
    for state, expected in [
        ("calculating", None),
        ("stale", "No recent progress"),
        ("waiting", "Waiting for Telegram"),
    ]:
        mode[0] = state
        page.reload()
        if expected is None:
            expect(row.locator(".transfer-metrics")).to_have_text("")
        else:
            expect(row.locator(".transfer-metrics")).to_contain_text(expected)
        expect(row.locator(".transfer-metrics")).not_to_contain_text("128 KB/s")
        expect(row.locator(".transfer-metrics")).not_to_contain_text("About 4s")


def test_folder_eta_survives_small_file_handoffs_and_publishing(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    size = 128 * 1024
    mode = ["flow"]
    polls = [0]
    folder_upload = {
        "id": "eta-folder-selection",
        "name": "Small files",
        "root_path": "/small-files",
        "created_at": time.time(),
    }
    saved = [
        {
            "key": f"eta-file-{index}",
            "jobId": f"eta-job-{index}",
            "name": f"small-{index}.bin",
            "size": size,
            "lastModified": 0,
            "destination": folder_upload["root_path"],
            "relativePath": f"Small files/small-{index}.bin",
            "folderUpload": folder_upload,
            "batchId": folder_upload["id"],
            "batchName": "Small files",
            "batchCreatedAt": time.time() * 1000,
            "state": "uploading",
            "phase": "Queued for Telegram",
            "current": 0,
            "total": size,
            "initiatedHere": True,
        }
        for index in range(8)
    ]
    page.add_init_script(
        "localStorage.setItem('tgdrive.uploads.v2', "
        + json.dumps(json.dumps(saved))
        + ");"
    )

    def jobs(route: Any) -> None:
        at = time.time()
        step = min(polls[0], 3)
        polls[0] += 1
        records = []
        for index in range(8):
            completed = index < step
            publishing = index == step
            sending = index == step + 1
            status = (
                "completed"
                if completed
                else "running"
                if publishing or sending
                else "queued"
            )
            phase = (
                "completed"
                if completed
                else "finalizing"
                if publishing
                else "uploading"
                if sending
                else "queued"
            )
            if sending and mode[0] == "waiting":
                phase = "retry_wait"
            records.append(
                {
                    "id": f"eta-job-{index}",
                    "name": f"small-{index}.bin",
                    "folder_upload": folder_upload,
                    "kind": "upload",
                    "status": status,
                    "phase": phase,
                    "current": size
                    if completed or publishing
                    else size // 4
                    if sending
                    else 0,
                    "total": size,
                    "created_at": at - 30,
                    "started_at": at - 10 if status != "queued" else None,
                    "finished_at": at if completed else None,
                    "progress_updated_at": at,
                    # Small files can complete between polls without a per-file speed.
                    "speed_bytes_per_second": None,
                    "eta_seconds": None,
                }
            )
        route.fulfill(json={"jobs": records})

    page.route("**/api/v1/jobs", jobs)
    login(page, server.url)
    batch = page.locator('[data-batch-id="eta-folder-selection"]')
    expect(batch).to_have_attribute("data-stage", "telegram")
    metrics = batch.locator(".transfer-batch-throughput")
    expect(metrics).to_contain_text(re.compile(r"About .+ remaining"))
    assert polls[0] >= 2, "A restored snapshot alone must not invent throughput"
    expect(batch).not_to_contain_text("Time estimate unavailable")
    expect(batch).not_to_contain_text("Uploading from device")

    page.get_by_role("link", name="Transfers", exact=False).click()
    expect(metrics).to_contain_text(re.compile(r"About .+ remaining"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / "folder-rolling-eta-desktop.png", full_page=True)
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        expect(metrics).to_contain_text(re.compile(r"About .+ remaining"))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert batch.locator(".transfer-device-status").evaluate("""el =>
          el.getBoundingClientRect().height <=
            parseFloat(getComputedStyle(el).lineHeight) * 1.1
        """), "The short close-tab notice should stay together"
        page.screenshot(
            path=ARTIFACTS / f"folder-rolling-eta-mobile-{width}.png", full_page=True
        )

    mode[0] = "waiting"
    batch.get_by_role("button", name="Show Files", exact=True).click()
    expect(batch).to_contain_text("Waiting for Telegram; retry scheduled")
    expect(metrics).not_to_contain_text("remaining")
