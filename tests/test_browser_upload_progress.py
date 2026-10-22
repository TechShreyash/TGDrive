"""Native Chromium upload progress, cancellation and safe retry coverage."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest
from test_browser_e2e import (
    ARTIFACTS,
    Server,
    expect,
    login,
)

pytestmark = pytest.mark.e2e
pytest_plugins = ("test_browser_e2e",)


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
    saved = page.evaluate("JSON.parse(localStorage.getItem('tgdrive.uploads.v2'))")
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
        "title", "Estimated time remaining for this stage"
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
        ("calculating", "Calculating speed"),
        ("stale", "No recent progress"),
        ("waiting", "Waiting for Telegram"),
    ]:
        mode[0] = state
        page.reload()
        expect(row.locator(".transfer-metrics")).to_contain_text(expected)
        expect(row.locator(".transfer-metrics")).not_to_contain_text("128 KB/s")
        expect(row.locator(".transfer-metrics")).not_to_contain_text("About 4s")
