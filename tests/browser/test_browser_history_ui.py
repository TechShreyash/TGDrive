"""Recorded ranges and real-sample inspection in the production charts."""

from __future__ import annotations

import contextlib
import os
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import expect, login

browser = fixtures.browser
desktop = fixtures.desktop
server = fixtures.server
pytestmark = pytest.mark.e2e


def history_result(start, end, *, empty=False):
    start, end = int(start // 60 * 60), int(end // 60 * 60)
    bucket = max(60, int((end - start + 359) // 360 // 60 + 1) * 60)
    samples = [
        {
            "at": at,
            "end_at": min(at + bucket, end),
            "cpu_percent": None if empty or index == 3 else 10 + index % 40,
            "cpu_scope": "cgroup",
            "memory_bytes": None if empty else 1024**3,
            "memory_limit_bytes": 8 * 1024**3,
            "received_bytes_per_second": None if empty or index == 3 else 1024**2,
            "sent_bytes_per_second": None if empty or index == 3 else 2 * 1024**2,
            "network_interface": "eth0",
            "coverage_seconds": {},
        }
        for index, at in enumerate(range(start, end, bucket))
    ]
    return {
        "start": start,
        "end": end,
        "bucket_seconds": bucket,
        "samples": samples,
        "retention_seconds": 31 * 86400,
        "earliest_at": None if empty else start,
        "tracked_since": start,
        "last_updated": end,
        "available": True,
        "totals": {
            "network_received_bytes": 5 * 1024**3,
            "network_sent_bytes": 2 * 1024**3,
            "http_download_bytes": 1024**3,
            "http_upload_bytes": 1024**2,
            "http_requests": 100,
            "http_errors": 0,
            "telegram_download_bytes": None,
            "telegram_upload_bytes": 3 * 1024**3,
        },
    }


def install_live_snapshot(page, server):
    login(page, server.url)
    snapshot = page.request.get(server.url + "/api/v1/performance").json()
    now = int(time.time())
    snapshot["server"]["cpu"] = {"percent": 70, "scope": "cgroup", "capacity_cores": 4}
    snapshot["server"]["history"] = [
        {
            **history_result(now - 300, now)["samples"][0],
            "at": now - (2 - index) * 3,
            "cpu_percent": 40 + index * 15,
        }
        for index in range(3)
    ]
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))


def query_bounds(route):
    values = parse_qs(urlparse(route.request.url).query)
    return float(values["start"][0]), float(values["end"][0])


def test_history_interval_hover_keyboard_poll_and_persisted_selection(server, desktop):
    _, page = desktop
    install_live_snapshot(page, server)
    requests = []

    def recorded(route):
        start, end = query_bounds(route)
        requests.append((start, end))
        route.fulfill(json=history_result(start, end))

    page.route("**/api/v1/performance/history?*", recorded)
    page.goto(server.url + "/?view=performance")
    interval = page.get_by_role("combobox", name="Time range", exact=True)
    expect(interval).to_have_value("live")
    page.get_by_role("tab", name="Resources", exact=True).click()
    cpu = page.get_by_role("region", name="Container CPU", exact=True)
    slider = cpu.get_by_role("slider", name="Inspect Container CPU samples")
    expect(slider).to_have_attribute("max", "2")
    interval.select_option("hour")
    expect(page.locator(".performance-history-availability")).to_contain_text(
        "Recorded interval averages"
    )
    expect(slider).to_have_attribute("max", "59")
    expect(cpu.locator("polyline.performance-plot-active")).to_have_count(2)
    for name, tab in [("Network traffic", "Overview"), ("Container RAM", "Resources")]:
        page.get_by_role("tab", name=tab, exact=True).click()
        expect(
            page.get_by_role("region", name=name, exact=True).get_by_role("slider")
        ).to_have_attribute("max", "59")
    expect(cpu.locator(".performance-chart-heading")).to_contain_text("70.0%Now")
    page.get_by_role("tab", name="Overview", exact=True).click()
    expect(page.locator(".performance-chart-legend")).to_contain_text(
        "Download to server"
    )
    expect(page.locator(".performance-chart-legend")).to_contain_text(
        "Upload from server"
    )
    page.get_by_role("tab", name="Overview", exact=True).click()
    expect(page.get_by_role("heading", name="All-time totals")).to_be_visible()
    expect(page.locator(".performance-all-time")).to_contain_text("5 GB")
    expect(page.locator(".performance-all-time")).to_contain_text("Not available")
    page.get_by_role("tab", name="Resources", exact=True).click()
    plot = cpu.get_by_role("img", name="Container CPU over time", exact=True)
    cpu.scroll_into_view_if_needed()
    bounds = plot.bounding_box()
    page.mouse.move(bounds["x"] + bounds["width"] * 0.251, bounds["y"] + 10)
    expect(cpu.locator(".performance-chart-tooltip")).to_be_visible()
    # Nearest real bucket is index 15, not an interpolated 24.809 value.
    expect(cpu.locator(".performance-chart-readout")).to_contain_text("25.0%")
    page.screenshot(path=fixtures.ARTIFACTS / "history-hover-desktop.png")
    page.mouse.move(0, 0)
    expect(cpu.locator(".performance-chart-tooltip")).to_have_count(0)
    expect(cpu.locator(".performance-chart-readout")).to_contain_text("29.0%")
    slider.focus()
    slider.press("Home")
    expect(cpu.locator(".performance-chart-readout")).to_contain_text("10.0%")
    page.mouse.move(bounds["x"] + bounds["width"] * 0.251, bounds["y"] + 10)
    page.mouse.move(0, 0)
    expect(cpu.locator(".performance-chart-readout")).to_contain_text("10.0%")
    before = len(requests)
    page.get_by_role("button", name="Refresh Performance", exact=True).click()
    expect(interval).to_have_value("hour")
    expect(slider).to_have_attribute("max", "59")
    page.wait_for_function(
        "() => !document.querySelector('.performance-history-status')"
    )
    assert len(requests) > before
    page.reload()
    page.get_by_role("tab", name="Resources", exact=True).click()
    expect(interval).to_have_value("hour")
    expect(slider).to_have_attribute("max", "59")
    assert requests[-1][1] - requests[-1][0] == 3600
    for tab, description in [
        ("Accounts", "Live account activity and current limits."),
    ]:
        page.get_by_role("tab", name=tab, exact=True).click()
        expect(interval).not_to_be_visible()
        expect(page.get_by_text(description, exact=True)).to_be_visible()
        expect(page.locator(".performance-history-availability")).not_to_be_visible()
    page.get_by_role("tab", name="Resources", exact=True).click()
    expect(interval).to_be_visible()
    expect(interval).to_have_value("hour")
    expect(cpu).to_be_visible()
    page.get_by_role("tab", name="Overview", exact=True).click()
    expect(interval).to_be_visible()
    expect(interval).to_have_value("hour")
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role(
        "heading", name="Performance", exact=True
    ).scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert interval.bounding_box()["x"] + interval.bounding_box()["width"] <= 382
    page.screenshot(path=fixtures.ARTIFACTS / "history-range-mobile.png")


def test_custom_history_is_absolute_utc_and_stale_fetch_cannot_replace_new_range(
    server, desktop
):
    _, page = desktop
    install_live_snapshot(page, server)
    held = []
    requests = []
    fail = False

    def recorded(route):
        start, end = query_bounds(route)
        requests.append((start, end))
        if end - start == 86400:
            held.append(route)
        elif fail:
            route.fulfill(
                status=503, json={"detail": "Recorded history is unavailable"}
            )
        else:
            route.fulfill(json=history_result(start, end, empty=end - start > 86400))

    page.route("**/api/v1/performance/history?*", recorded)
    page.goto(server.url + "/?view=performance")
    interval = page.get_by_role("combobox", name="Time range", exact=True)
    interval.select_option("day")
    expect(page.get_by_text("Loading selected history…", exact=False)).to_be_visible()
    interval.select_option("week")
    expect(page.locator(".performance-history-availability")).to_contain_text(
        "No readings were recorded"
    )
    assert held
    with contextlib.suppress(fixtures.playwright.Error):
        held[0].fulfill(json=history_result(*requests[-2]))
    expect(interval).to_have_value("week")
    expect(
        page.get_by_text("No recorded readings in this range.", exact=True)
    ).to_have_count(3)
    interval.select_option("custom")
    page.set_viewport_size({"width": 320, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert interval.bounding_box()["x"] + interval.bounding_box()["width"] <= 312
    start = datetime.fromtimestamp(time.time() - 7200, UTC).strftime("%Y-%m-%dT%H:%M")
    end = datetime.fromtimestamp(time.time() - 3600, UTC).strftime("%Y-%m-%dT%H:%M")
    page.get_by_label("Start (UTC)", exact=True).fill(start)
    page.get_by_label("End (UTC)", exact=True).fill(end)
    page.get_by_role("button", name="Apply Range", exact=True).click()
    expect(page.get_by_text("Applied:", exact=False)).to_be_visible()
    expect(page.locator(".performance-history-availability")).to_contain_text(
        "Recorded interval averages"
    )
    expected = tuple(
        datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()
        for value in [start, end]
    )
    assert requests[-1] == expected
    page.get_by_role("button", name="Refresh Performance", exact=True).click()
    page.wait_for_function(
        "() => !document.querySelector('.performance-history-status')"
    )
    assert requests[-1] == expected
    fail = True
    interval.select_option("hour")
    expect(page.locator(".performance-history-error")).to_contain_text(
        "Showing previously loaded history"
    )
    fail = False
    page.get_by_role("button", name="Retry History", exact=True).click()
    expect(page.locator(".performance-history-error")).to_have_count(0)
    expect(interval).to_have_value("hour")


def test_recorded_history_page_uses_real_private_history_endpoint(server, desktop):
    _, page = desktop
    login(page, server.url + "/?view=performance")
    page.get_by_role("combobox", name="Time range", exact=True).select_option("hour")
    expect(page.locator(".performance-history-availability")).to_contain_text(
        "Recorded interval averages"
    )
    expect(page.locator(".performance-history-error")).to_have_count(0)
    expect(
        page.get_by_role("heading", name="All-time totals", exact=True)
    ).to_be_visible()
    expect(page.locator(".performance-all-time")).to_contain_text(
        "independent of the time range"
    )
    end = int(time.time())
    response = page.request.get(
        server.url + f"/api/v1/performance/history?start={end - 3600}&end={end}"
    )
    assert response.status == 200
    value = response.json()
    assert value["available"] is True
    assert 0 < len(value["samples"]) <= 360
    assert value["bucket_seconds"] >= 60
    assert value["retention_seconds"] == 31 * 86400
    assert "network_received_bytes" in value["totals"]
