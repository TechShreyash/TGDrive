"""Owner cache and performance controls in real Chromium."""
# ruff: noqa: F811 - pytest fixture parameters shadow imported fixture functions.

from __future__ import annotations

import os
import re
import time

import pytest

pytestmark = pytest.mark.e2e
if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium tests", allow_module_level=True)

from tests.browser.test_browser_e2e import (  # noqa: E402
    ARTIFACTS,
    browser,  # noqa: F401 - shared pytest fixture
    desktop,  # noqa: F401 - shared pytest fixture
    expect,
    login,
    server,  # noqa: F401 - shared pytest fixture
)


def test_cache_inventory_filters_verified_coverage_and_clear_actions(server, desktop):
    _, page = desktop
    login(page, server.url)
    snapshot = page.request.get(server.url + "/api/v1/preloads").json()
    snapshot["cache"]["files"] = [
        {
            "id": "ready-file",
            "name": "Prepared movie.mp4",
            "size": 100,
            "ready_bytes": 100,
            "complete": True,
            "ready_ranges": [[0, 100]],
        },
        {
            "id": "partial-file",
            "name": "Interview.mkv",
            "size": 100,
            "ready_bytes": 100,
            "complete": True,
            "ready_ranges": [[0, 10], [90, 100]],
        },
    ]
    snapshot["jobs"] = [
        {
            "id": "active",
            "file_id": "partial-file",
            "name": "Interview.mkv",
            "state": "preparing",
            "mode": "auto",
            "bytes_done": 20,
            "target_bytes": 100,
            "size": 100,
            "start": 0,
            "end": 100,
        },
        {
            "id": "failed",
            "file_id": "other-file",
            "name": "Uncached video.mp4",
            "state": "failed",
            "mode": "manual",
            "bytes_done": 0,
            "target_bytes": 100,
            "size": 100,
            "start": 0,
            "end": 100,
            "error": "Telegram is temporarily unavailable",
        },
    ]
    snapshot["active_jobs"] = 1
    page.route("**/api/v1/preloads", lambda route: route.fulfill(json=snapshot))
    cleared = []

    def clear(route):
        cleared.append(route.request.post_data_json)
        route.fulfill(json={"status": "ok"})

    page.route("**/api/v1/preloads/clear", clear)
    page.goto(server.url + "/?view=preloads")
    expect(page.locator(".cached-file")).to_have_count(2)
    expect(
        page.locator(".cached-file").filter(has_text="Interview.mkv")
    ).to_contain_text("Preloading")
    ready = page.locator(".cached-file").filter(has_text="Prepared movie")
    partial = page.locator(".cached-file").filter(has_text="Interview.mkv")
    expect(ready.locator("svg[role=img]")).to_be_visible()
    expect(ready.locator(".cache-coverage-ready")).to_have_attribute("x", "0")
    expect(ready.locator(".cache-coverage-ready")).to_have_attribute("width", "1000")
    expect(ready.locator(".cache-ready-state")).to_have_text("Ready")
    segments = partial.locator(".cache-coverage-ready")
    expect(segments).to_have_count(2)
    assert [segments.nth(i).get_attribute("x") for i in range(2)] == ["0", "900"]
    assert [segments.nth(i).get_attribute("width") for i in range(2)] == ["100", "100"]
    assert ready.locator(".cache-coverage-ready").evaluate(
        "node => getComputedStyle(node).fill"
    ) != segments.first.evaluate("node => getComputedStyle(node).fill")
    expect(partial.locator(".cache-ready-state")).to_contain_text("Partial")
    expect(page.get_by_text("Cached ranges", exact=False)).to_have_count(0)
    expect(page.get_by_text("Cache details & recent preparations")).to_have_count(0)
    expect(page.get_by_role("heading", name="Preload queue")).to_be_visible()
    expect(
        page.get_by_text("Telegram is temporarily unavailable", exact=True)
    ).to_be_visible()
    page.get_by_role("button", name="Ready 1", exact=True).click()
    expect(page.locator(".cached-file")).to_have_count(1)
    expect(page.locator(".cached-file")).to_contain_text("Prepared movie")
    page.get_by_role("button", name="Partial 1", exact=True).click()
    expect(page.locator(".cache-coverage")).to_contain_text("20 / 100 B cached")
    expect(page.locator(".cache-coverage")).to_contain_text("20%")
    page.get_by_role("searchbox", name="Search cached files", exact=True).fill(
        "missing"
    )
    expect(
        page.get_by_text("No cached files match this filter.", exact=False)
    ).to_be_visible()
    page.get_by_role("searchbox", name="Search cached files", exact=True).fill("")
    page.get_by_role("button", name="All 2", exact=True).click()

    # Retain tiny-range edge checks above; judge the composition with realistic
    # synthetic file sizes and actual tier counters, independent of coverage.
    gib = 1024**3
    snapshot["cache"]["memory"]["bytes"] = 80 * 1024**2
    snapshot["cache"]["files"][0].update(
        name="Prepared movie - director's cut remastered full edition 2026.mp4",
        size=2 * gib,
        ready_bytes=2 * gib,
        ready_ranges=[[0, 2 * gib]],
    )
    snapshot["cache"]["files"][1].update(
        size=gib, ready_ranges=[[0, gib // 10], [gib * 9 // 10, gib]]
    )
    snapshot["cache"]["disk"]["bytes"] = 2 * gib + gib // 10 + (gib - gib * 9 // 10)
    page.get_by_role("button", name="Refresh Cache Inventory").click()
    expect(page.get_by_role("group", name="Memory cache", exact=True)).to_contain_text(
        "31%"
    )
    expect(page.get_by_role("group", name="Disk cache", exact=True)).to_contain_text(
        "22%"
    )
    page.screenshot(path=ARTIFACTS / "owner-preloads-desktop.png", full_page=True)
    page.get_by_role("button", name="Evict cached bytes for Interview.mkv").click()
    dialog = page.get_by_role("dialog", name="Clear cached file?", exact=True)
    expect(dialog.get_by_role("button", name="Keep Cache")).to_be_focused()
    assert cleared == []
    dialog.get_by_role("button", name="Keep Cache").click()
    expect(dialog).not_to_be_visible()
    page.get_by_role("button", name="Evict cached bytes for Interview.mkv").click()
    dialog.get_by_role("button", name="Clear Cached File", exact=True).click()
    feedback = page.get_by_text(
        "Cached bytes evicted. The original is unchanged.", exact=True
    )
    expect(feedback).to_have_count(1)
    expect(feedback).to_be_visible()
    assert cleared == [{"file_id": "partial-file"}]
    page.get_by_role("button", name="Dismiss Notification").click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(
        page.get_by_role("searchbox", name="Search cached files", exact=True)
    ).to_be_visible()
    page.screenshot(path=ARTIFACTS / "owner-preloads-mobile-dark.png", full_page=True)

    # The same coverage and controls stay usable in compact layouts and themes.
    for width, theme in [
        (1440, "light"),
        (1440, "dark"),
        (390, "light"),
        (390, "dark"),
        (320, "dark"),
    ]:
        page.set_viewport_size({"width": width, "height": 900})
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        if width == 320 and theme == "dark":
            brand = page.locator(".mobile-brand .brand-logo")
            expect(brand.locator("img:visible")).to_have_count(1)
            expect(brand.locator(".brand-logo-dark")).not_to_be_visible()
            expect(brand.locator(".brand-logo-icon")).to_be_visible()
        if width <= 640:
            selector = page.get_by_role("combobox", name="Filter cached files")
            selector.select_option("ready")
            expect(page.locator(".cached-file")).to_have_count(1)
            expect(page.locator(".cached-file")).to_contain_text("Prepared movie")
            selector.select_option("partial")
            expect(page.locator(".cached-file")).to_contain_text("Interview.mkv")
            selector.select_option("all")
        expect(page.locator(".cached-file")).to_have_count(2)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(
            path=ARTIFACTS / f"preloads-compact-{width}-{theme}.png", full_page=True
        )

    help_panel = page.locator(".preload-help")
    summary = help_panel.get_by_text("About preloading", exact=True)
    summary.focus()
    page.keyboard.press("Enter")
    expect(help_panel).to_have_attribute("open", "")
    expect(
        help_panel.get_by_role("link", name="Read the preloading guide")
    ).to_have_attribute(
        "href",
        "https://github.com/TechShreyash/TGDrive/blob/main/docs/usage.md"
        "#preload-a-file-for-smoother-playback-and-downloads",
    )
    expect(help_panel).to_contain_text("downloads fetch their requested ranges")
    page.keyboard.press("Enter")
    expect(help_panel).not_to_have_attribute("open", "")

    cancellations = []

    def cancel(route):
        cancellations.append(route.request.url)
        snapshot["jobs"][0]["state"] = "cancelled"
        snapshot["active_jobs"] = 0
        route.fulfill(json={"status": "ok"})

    page.route("**/api/v1/preloads/active/cancel", cancel)
    page.get_by_role("button", name="Cancel preload of Interview.mkv").click()
    expect(
        page.get_by_role("button", name="Cancel preload of Interview.mkv")
    ).to_have_count(0)
    assert len(cancellations) == 1
    expect(partial.locator(".cache-ready-state")).to_have_text("Partial")

    page.get_by_role("button", name="Clear Cache", exact=True).click()
    all_dialog = page.get_by_role("dialog", name="Clear all cached files?", exact=True)
    expect(all_dialog.get_by_role("button", name="Keep Cache")).to_be_focused()
    assert cleared == [{"file_id": "partial-file"}]
    all_dialog.get_by_role("button", name="Keep Cache").click()
    expect(all_dialog).not_to_be_visible()
    page.get_by_role("button", name="Clear Cache", exact=True).click()
    all_dialog.get_by_role("button", name="Clear All Cache", exact=True).click()
    feedback = page.get_by_text(
        "Cached bytes cleared. Originals remain in Telegram.", exact=True
    )
    expect(feedback).to_have_count(1)
    expect(feedback).to_be_visible()
    assert cleared == [{"file_id": "partial-file"}, {}]

    snapshot["cache"]["files"] = []
    snapshot["cache"]["memory"].update(max_bytes=256 * 1024**2, effective_max_bytes=0)
    snapshot["cache"]["disk"]["available"] = False
    page.get_by_role("button", name="Refresh Cache Inventory").click()
    expect(page.get_by_text("No files cached yet", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Clear Cache", exact=True)).to_be_disabled()
    expect(page.get_by_role("group", name="Server cache usage")).to_contain_text(
        "Unavailable"
    )
    expect(page.get_by_role("group", name="Server cache usage")).not_to_contain_text(
        "Disabled"
    )
    page.screenshot(path=ARTIFACTS / "preloads-empty-unavailable-320-dark.png")


def test_performance_views_keyboard_navigation_and_settings_groups(server, desktop):
    _, page = desktop
    login(page, server.url)
    snapshot = page.request.get(server.url + "/api/v1/performance").json()
    page.goto(server.url + "/?view=performance")
    expect(page.get_by_role("tab", name="Overview", exact=True)).to_be_visible()
    expect(
        page.get_by_role("img", name="Measured HTTP send and receive throughput")
    ).not_to_be_visible()
    page.screenshot(path=ARTIFACTS / "owner-performance-idle.png", full_page=True)
    now = time.time()
    snapshot["traffic"].update(
        download_bytes_per_second=3 * 1024**2,
        upload_bytes_per_second=1024**2,
        history=[
            {
                "at": now - 6,
                "download_bytes": 0,
                "upload_bytes": 0,
                "requests": 1,
                "active": 2,
                "errors": 0,
            },
            {
                "at": now - 3,
                "download_bytes": 6 * 1024**2,
                "upload_bytes": 3 * 1024**2,
                "requests": 2,
                "active": 2,
                "errors": 0,
            },
            {
                "at": now,
                "download_bytes": 15 * 1024**2,
                "upload_bytes": 6 * 1024**2,
                "requests": 3,
                "active": 2,
                "errors": 0,
            },
        ],
    )
    snapshot["telegram"].update(
        active=2,
        queued=1,
        history=[
            {"at": now - 3, "active": 1, "queued": 0},
            {"at": now, "active": 2, "queued": 1},
        ],
        accounts=[
            {
                "name": "Storage bot 1",
                "kind": "bot",
                "uploads": 1,
                "downloads": 1,
                "combined_limit": 3,
                "cooldown_seconds": 0,
                "requests": {
                    "active": 2,
                    "queued": 1,
                    "effective_limit": 8,
                    "effective_parts_per_second": 24,
                    "configured_parts_per_second": 24,
                    "floods": 0,
                },
            }
        ],
        queue=[
            {
                "position": 1,
                "operation": "download",
                "priority": "demand",
                "waiting_seconds": 3,
                "estimated_wait_seconds": 5,
            }
        ],
    )
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    overview = page.get_by_role("tab", name="Overview", exact=True)
    expect(overview).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("tabpanel", name="Overview")).to_be_visible()
    expect(page.get_by_role("heading", name="Live file transfers")).to_be_visible()
    expect(page.get_by_text("3 MB/s", exact=True)).to_be_visible()
    expect(page.get_by_text("1 MB/s", exact=True)).to_be_visible()
    expect(
        page.get_by_role("region", name="Container RAM", exact=True)
    ).not_to_be_visible()
    expect(
        page.get_by_role("heading", name="Storage & cache", exact=True)
    ).not_to_be_visible()
    overview.focus()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("tab", name="Accounts", exact=True)).to_be_focused()
    expect(page.get_by_role("heading", name="Account capacity")).to_be_visible()
    expect(page.get_by_role("heading", name="Waiting for Telegram")).to_be_visible()
    expect(page.get_by_role("cell", name="About 5s", exact=True)).to_be_visible()
    page.screenshot(path=ARTIFACTS / "owner-performance-accounts.png", full_page=True)
    page.get_by_role("tab", name="Accounts", exact=True).focus()
    page.keyboard.press("End")
    page.keyboard.press("Enter")
    expect(
        page.get_by_role("heading", name="Storage & cache", exact=True)
    ).to_be_visible()
    expect(
        page.get_by_role("progressbar", name="Cache filesystem", exact=True)
    ).to_be_visible()
    assert (
        page.get_by_role("heading", name="Storage & cache")
        .locator("xpath=ancestor::details")
        .count()
        == 0
    )
    page.screenshot(path=ARTIFACTS / "owner-performance-resources.png", full_page=True)
    page.get_by_role("tab", name="Overview", exact=True).click()
    page.screenshot(path=ARTIFACTS / "owner-performance-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=ARTIFACTS / "owner-performance-mobile-dark.png", full_page=True
    )
    for tab in ["Accounts", "Resources"]:
        page.get_by_role("tab", name=tab, exact=True).click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(
            path=ARTIFACTS / f"owner-performance-{tab.lower()}-mobile.png",
            full_page=True,
        )
    page.goto(server.url + "/?view=settings&section=cache")
    expect(page.get_by_role("group", name="Cache storage", exact=True)).to_be_visible()
    expect(
        page.get_by_role("group", name="Automatic preloading", exact=True)
    ).to_be_visible()
    expect(
        page.get_by_role("button", name="Save Performance Settings")
    ).to_be_disabled()
    page.get_by_label("Memory cache (MiB)", exact=True).fill("512")
    expect(page.get_by_text("Unsaved changes", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Save Performance Settings")).to_be_enabled()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/api/v1/settings/performance")
        )
    ) as saved:
        page.get_by_role("button", name="Save Performance Settings").click()
    assert saved.value.ok, saved.value.text()
    assert saved.value.json()["cache_memory_bytes"] == 512 * 1024**2
    expect(page.locator(".toast")).to_have_text("Performance settings saved.")
    expect(page.get_by_label("Memory cache (MiB)", exact=True)).to_have_value("512")
    expect(
        page.get_by_role("button", name="Save Performance Settings")
    ).to_be_disabled()
    page.screenshot(
        path=ARTIFACTS / "owner-performance-settings-mobile.png", full_page=True
    )


def test_network_includes_url_downloads_and_resources_use_container_limits(
    server, desktop
):
    _, page = desktop
    login(page, server.url)
    snapshot = page.request.get(server.url + "/api/v1/performance").json()
    snapshot["server"]["network"] = {
        "available": True,
        "interface": "eth0",
        "started_at": time.time() - 120,
        "received_bytes": 1024**3,
        "sent_bytes": 2 * 1024**2,
        "received_bytes_per_second": 12 * 1024**2,
        "sent_bytes_per_second": 0,
        "scope": "Default-route interface in the application's network namespace",
    }
    snapshot["server"]["cpu"] = {"percent": 25, "scope": "cgroup", "capacity_cores": 4}
    snapshot["server"]["memory"] = {
        "MemTotal": 16 * 1024**3,
        "MemAvailable": 10 * 1024**3,
        "cgroup_current": 2 * 1024**3,
        "cgroup_limit": 8 * 1024**3,
    }
    snapshot["traffic"].update(
        download_bytes=0,
        upload_bytes=0,
        download_bytes_per_second=0,
        upload_bytes_per_second=0,
        history=[],
    )
    now = time.time()
    snapshot["server"]["history"] = [
        {
            "at": now - (6 - index) * 3,
            "cpu_percent": percent,
            "cpu_scope": "cgroup",
            "memory_bytes": int(memory * 1024**3) if memory is not None else None,
            "memory_limit_bytes": 8 * 1024**3,
            "received_bytes_per_second": incoming * 1024**2
            if incoming is not None
            else None,
            "sent_bytes_per_second": outgoing * 1024**2
            if outgoing is not None
            else None,
            "network_interface": "eth0",
        }
        for index, (percent, memory, incoming, outgoing) in enumerate(
            [
                (10, 1, 2, 1),
                (15, 1.3, 4, 2),
                (20, 1.7, 5, 3),
                (None, None, None, None),
                (30, 1.8, 8, 1),
                (40, 2.3, 10, 2),
                (25, 2, 12, 0),
            ]
        )
    ]
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    network = page.get_by_role("region", name="Network traffic", exact=True)
    expect(network).to_contain_text("12 MB/s")
    expect(network).to_contain_text(
        "Includes Telegram, URL downloads, device uploads and viewer downloads"
    )
    expect(
        page.get_by_role("img", name="Network traffic over time", exact=True)
    ).to_be_visible()
    expect(network.locator("polyline.performance-plot-active")).to_have_count(2)
    plot = network.locator(".performance-chart-plot")
    bounds = network.get_by_role("img").bounding_box()
    positions = []
    for fraction in (0.2, 0.65, 0.98):
        pointer_x = bounds["x"] + bounds["width"] * fraction
        page.mouse.move(pointer_x, bounds["y"] + bounds["height"] * 0.45)
        tooltip = network.locator(".performance-chart-tooltip")
        expect(tooltip).to_be_visible()
        tip = tooltip.bounding_box()
        frame = plot.bounding_box()
        assert tip["x"] >= frame["x"]
        assert tip["x"] + tip["width"] <= frame["x"] + frame["width"] + 1
        positions.append(tip["x"])
    assert positions[1] > positions[0] + 100
    expect(network.locator("svg title")).to_have_count(0)

    expect(
        page.get_by_role("region", name="Container RAM", exact=True)
    ).not_to_be_visible()
    file_traffic = page.locator(
        "section",
        has=page.get_by_role("heading", name="Live file transfers", exact=True),
    ).last
    expect(file_traffic).to_contain_text("0 B/s")
    page.get_by_role("tab", name="Resources", exact=True).click()
    cpu = page.get_by_role("region", name="Container CPU", exact=True)
    expect(cpu.locator(".performance-chart-axis")).to_contain_text("100.0%")
    slider = cpu.get_by_role("slider", name="Inspect Container CPU samples", exact=True)
    slider.focus()
    slider.press("Home")
    slider.press("ArrowRight")
    slider.press("ArrowRight")
    slider.press("ArrowRight")
    expect(slider).to_have_attribute("aria-valuetext", re.compile("no reading"))
    expect(cpu.locator(".performance-chart-readout")).to_contain_text("No reading")
    cpu.get_by_role("button", name="Live", exact=True).click()
    expect(cpu.locator(".performance-chart-readout")).to_contain_text("25.0%")
    ram = page.get_by_role("region", name="Container RAM", exact=True)
    expect(ram.locator(".performance-chart-heading")).to_contain_text("2 GB / 8 GB")
    expect(ram.locator(".performance-chart-axis")).to_contain_text("8 GB")
    page.set_viewport_size({"width": 1440, "height": 1100})
    page.get_by_role(
        "heading", name="Performance", exact=True
    ).scroll_into_view_if_needed()
    page.screenshot(
        path=ARTIFACTS / "owner-performance-network-container.png", full_page=False
    )
    snapshot["server"]["memory"]["cgroup_limit"] = None
    page.get_by_role("button", name="Refresh Performance", exact=True).click()
    expect(ram).to_contain_text("No container memory limit.")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role(
        "heading", name="Performance", exact=True
    ).scroll_into_view_if_needed()
    page.screenshot(
        path=ARTIFACTS / "owner-performance-network-mobile.png", full_page=False
    )
    cpu.get_by_role(
        "heading", name="Container CPU", exact=True
    ).scroll_into_view_if_needed()
    page.screenshot(
        path=ARTIFACTS / "owner-performance-resource-graphs-mobile.png", full_page=False
    )
    snapshot["server"]["history"] = []
    page.get_by_role("button", name="Refresh Performance", exact=True).click()
    expect(
        page.get_by_role("img", name="Network traffic over time", exact=True)
    ).to_have_count(0)
    page.get_by_role("tab", name="Overview", exact=True).click()
    expect(network.locator(".performance-chart-heading")).to_contain_text("12 MB/s")
    expect(network.locator(".performance-chart-collecting")).to_be_visible()
