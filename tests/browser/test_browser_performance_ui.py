"""Owner cache and performance controls in real Chromium."""
# ruff: noqa: F811 - pytest fixture parameters shadow imported fixture functions.

from __future__ import annotations

import json
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
    set_appearance,
)


def test_cache_entry_names_purposes_search_and_scoped_eviction(server, desktop):
    _, page = desktop
    login(page, server.url)
    snapshot = page.request.get(server.url + "/api/v1/preloads").json()
    snapshot["jobs"] = []
    snapshot["active_jobs"] = 0
    filename = (
        "Cyberpunk.Edgerunners.S02E01.Last.Chance.for.a.Slow.Dance.1080p."
        "NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv"
    )
    files = [
        {
            "id": identity,
            "name": name,
            "message_id": 17 + index,
            "size": 100,
            "ready_bytes": ready,
            "complete": ready == 100,
            "ready_ranges": [[0, ready]],
            **({"purpose": purpose} if purpose else {}),
        }
        for index, (identity, name, purpose, ready) in enumerate(
            (
                ("original-key", filename, "original", 20),
                ("poster-key", filename, "poster", 100),
                ("unknown-key", "", "unknown", 100),
                ("legacy-key", "Old download.mp4", None, 100),
            )
        )
    ]
    snapshot["cache"]["files"] = files.copy()
    cleared = []

    def inventory(route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            cleared.append(body)
            snapshot["cache"]["files"] = [
                file
                for file in snapshot["cache"]["files"]
                if file["id"] != body["file_id"]
            ]
            route.fulfill(json={"status": "ok"})
        else:
            route.fulfill(json=snapshot)

    page.route("**/api/v1/preloads**", inventory)
    page.goto(server.url + "/?view=preloads")
    expect(page.get_by_role("heading", name="Cached entries")).to_be_visible()
    rows = page.locator(".cached-file")
    expect(rows).to_have_count(4)
    original = rows.filter(has=page.get_by_text(filename, exact=True))
    expect(original.locator(".cache-ready-state")).to_have_text("Partial")
    expect(original).to_contain_text("20 / 100 B cached")
    poster = rows.filter(has_text="Poster for " + filename)
    expect(poster.locator(".cache-ready-state")).to_have_text("Poster cached")
    expect(rows.filter(has_text="Cached data · message 19")).to_contain_text(
        "Fully cached"
    )
    expect(rows.filter(has_text="Old download.mp4")).to_contain_text("Fully cached")
    page.get_by_role("button", name="Fully cached 3", exact=True).click()
    expect(rows).to_have_count(3)
    search = page.get_by_role("searchbox", name="Search cached files", exact=True)
    search.fill("poster for cyberpunk")
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text("Poster cached")
    search.fill("cached data")
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text("message 19")
    search.fill("")
    page.get_by_role("button", name="All 4", exact=True).click()
    page.screenshot(
        path=ARTIFACTS / "cache-entry-purposes-desktop.png", animations="disabled"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Change Appearance", exact=True).click()
    page.get_by_role("menuitemradio", name="Dark", exact=True).click()
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        poster.scroll_into_view_if_needed()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        title = poster.locator("strong")
        expect(title).to_have_attribute("title", "Poster for " + filename)
        assert title.evaluate("node => node.clientHeight <= 63")
        assert title.evaluate("node => getComputedStyle(node).webkitLineClamp") == "3"
        assert title.bounding_box()["width"] >= 180
        assert poster.bounding_box()["height"] <= 160
        state = poster.locator(".cache-ready-state").bounding_box()
        heading = poster.locator(".cached-file-heading").bounding_box()
        assert state["y"] >= heading["y"] + heading["height"]
        page.screenshot(
            path=ARTIFACTS / f"cache-entry-long-names-{width}-dark.png",
            animations="disabled",
        )
    poster.get_by_role(
        "button", name="Evict cached bytes for Poster for " + filename, exact=True
    ).click()
    dialog = page.get_by_role("dialog", name="Clear cached entry?", exact=True)
    expect(dialog).to_contain_text("Poster for " + filename)
    expect(dialog.get_by_role("button", name="Keep Cache")).to_be_focused()
    dialog.get_by_role("button", name="Clear Cached Entry", exact=True).click()
    expect(rows).to_have_count(3)
    assert cleared == [{"file_id": "poster-key"}]
    assert snapshot["cache"]["files"] == [files[0], files[2], files[3]]
    expect(original).to_contain_text("20 / 100 B cached")


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
        if len(cleared) == 1:
            route.fulfill(status=503, json={"detail": "Cache is busy. Try again."})
        else:
            route.fulfill(json={"status": "ok"})

    page.route("**/api/v1/preloads/clear", clear)
    page.goto(server.url + "/?view=preloads")
    expect(page.locator(".cached-file")).to_have_count(2)
    expect(
        page.locator(".cached-file").filter(has_text="Interview.mkv")
    ).to_contain_text("Preloading")
    ready = page.locator(".cached-file").filter(has_text="Prepared movie")
    partial = page.locator(".cached-file").filter(has_text="Interview.mkv")
    # Ready rows hide the coverage bar; the painted range stays in the markup.
    expect(ready.locator("svg[role=img]")).to_be_hidden()
    expect(ready.locator(".cache-coverage-ready")).to_have_attribute("x", "0")
    expect(ready.locator(".cache-coverage-ready")).to_have_attribute("width", "1000")
    expect(ready.locator(".cache-ready-state")).to_have_text("Fully cached")
    segments = partial.locator(".cache-coverage-ready")
    expect(segments).to_have_count(2)
    assert [segments.nth(i).get_attribute("x") for i in range(2)] == ["0", "900"]
    assert [segments.nth(i).get_attribute("width") for i in range(2)] == ["100", "100"]
    assert ready.locator(".cache-coverage-ready").evaluate(
        "node => getComputedStyle(node).fill"
    ) != segments.first.evaluate("node => getComputedStyle(node).fill")
    expect(partial.locator(".cache-ready-state")).to_contain_text("Partial")
    expect(page.get_by_text("Cached ranges", exact=False)).to_have_count(0)
    expect(page.get_by_text("Cache details and recent preparations")).to_have_count(0)
    expect(page.get_by_role("heading", name="Preload queue")).to_be_visible()
    expect(
        page.get_by_text("Telegram is temporarily unavailable", exact=True)
    ).to_be_visible()
    page.get_by_role("button", name="Fully cached 1", exact=True).click()
    expect(page.locator(".cached-file")).to_have_count(1)
    expect(page.locator(".cached-file")).to_contain_text("Prepared movie")
    page.get_by_role("button", name="Partial 1", exact=True).click()
    expect(page.locator(".cache-coverage")).to_contain_text("20 / 100 B cached")
    expect(page.locator(".cache-coverage")).to_contain_text("20%")
    page.get_by_role("searchbox", name="Search cached files", exact=True).fill(
        "missing"
    )
    expect(
        page.get_by_text("No cached entries match this filter.", exact=False)
    ).to_be_visible()
    page.get_by_role("searchbox", name="Search cached files", exact=True).fill("")
    page.get_by_role("button", name="All 2", exact=True).click()

    # Retain tiny-range edge checks above; judge the composition with realistic
    # synthetic file sizes and actual tier counters, independent of coverage.
    gib = 1024**3
    snapshot["cache"]["memory"]["bytes"] = 80 * 1024**2
    snapshot["cache"]["memory"]["effective_max_bytes"] = 256 * 1024**2
    snapshot["cache"]["disk"]["max_bytes"] = 10 * gib
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
    dialog = page.get_by_role("dialog", name="Clear cached entry?", exact=True)
    expect(dialog.get_by_role("button", name="Keep Cache")).to_be_focused()
    assert cleared == []
    dialog.get_by_role("button", name="Keep Cache").click()
    expect(dialog).not_to_be_visible()
    page.get_by_role("button", name="Evict cached bytes for Interview.mkv").click()
    dialog.get_by_role("button", name="Clear Cached Entry", exact=True).click()
    expect(dialog.get_by_role("alert")).to_have_text("Cache is busy. Try again.")
    expect(page.locator(".toast")).to_have_count(0)
    dialog.get_by_role("button", name="Clear Cached Entry", exact=True).click()
    feedback = page.get_by_text(
        "Cached bytes evicted. The original is unchanged.", exact=True
    )
    expect(feedback).to_have_count(1)
    expect(feedback).to_be_visible()
    assert cleared == [{"file_id": "partial-file"}] * 2
    page.get_by_role("button", name="Dismiss Notification").click()
    page.set_viewport_size({"width": 390, "height": 844})
    set_appearance(page, "dark")
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
        set_appearance(page, theme)
        if width == 320 and theme == "dark":
            brand = page.locator(".mobile-brand .brand-logo")
            expect(brand.locator("img:visible")).to_have_count(1)
            expect(brand.locator(".brand-logo-dark")).to_be_visible()
            expect(brand.locator(".brand-logo-icon")).not_to_be_visible()
        if width <= 640:
            selector = page.get_by_role("combobox", name="Filter cached entries")
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
    all_dialog = page.get_by_role(
        "dialog", name="Clear all cached entries?", exact=True
    )
    expect(all_dialog.get_by_role("button", name="Keep Cache")).to_be_focused()
    assert cleared == [{"file_id": "partial-file"}] * 2
    all_dialog.get_by_role("button", name="Keep Cache").click()
    expect(all_dialog).not_to_be_visible()
    page.get_by_role("button", name="Clear Cache", exact=True).click()
    all_dialog.get_by_role("button", name="Clear All Cache", exact=True).click()
    feedback = page.get_by_text(
        "Cached bytes cleared. Originals remain in Telegram.", exact=True
    )
    expect(feedback).to_have_count(1)
    expect(feedback).to_be_visible()
    assert cleared == [{"file_id": "partial-file"}, {"file_id": "partial-file"}, {}]

    snapshot["cache"]["files"] = []
    snapshot["cache"]["memory"].update(max_bytes=256 * 1024**2, effective_max_bytes=0)
    snapshot["cache"]["disk"]["available"] = False
    page.get_by_role("button", name="Refresh Cache Inventory").click()
    expect(page.get_by_text("No cache entries yet", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Clear Cache", exact=True)).to_be_disabled()
    expect(page.get_by_role("group", name="Server cache usage")).to_contain_text(
        "Unavailable"
    )
    expect(page.get_by_role("group", name="Server cache usage")).not_to_contain_text(
        "Disabled"
    )
    page.screenshot(path=ARTIFACTS / "preloads-empty-unavailable-320-dark.png")


def test_cache_settings_groups_and_scoped_save(server, desktop):
    _, page = desktop
    login(page, server.url)
    page.goto(server.url + "/?view=settings&section=cache")
    expect(
        page.get_by_role("heading", name="Cache and preloading", exact=True)
    ).to_be_visible()
    limits = page.get_by_role("group", name="Media cache limits", exact=True)
    automatic = page.get_by_role("group", name="Automatic preloading", exact=True)
    for settings_width, theme in ((1440, "dark"), (390, "light")):
        page.set_viewport_size({"width": settings_width, "height": 900})
        set_appearance(page, theme)
        page.evaluate("document.fonts.ready")
        mode = limits.get_by_label("Media disk cache mode", exact=True)
        expect(mode).to_have_css(
            "background-color",
            page.locator(".workspace").evaluate(
                "node => getComputedStyle(node).backgroundColor"
            ),
        )
        mode.focus()
        page.keyboard.press("Tab")
        page.keyboard.press("Shift+Tab")
        expect(mode).to_be_focused()
        expect(mode).to_have_css("outline-width", "2px")
        mode.blur()
        legend = limits.locator(":scope > legend").bounding_box()
        help_line = limits.locator(":scope > .settings-help").first.bounding_box()
        assert help_line["x"] == pytest.approx(legend["x"], abs=1)
        icon = automatic.locator("legend svg").bounding_box()
        help_line = automatic.locator(":scope > .settings-help").first.bounding_box()
        assert help_line["x"] == pytest.approx(icon["x"] + icon["width"] + 12, abs=1)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(
            path=ARTIFACTS / f"cache-heading-alignment-{settings_width}.png",
            full_page=True,
            animations="disabled",
        )
    page.set_viewport_size({"width": 390, "height": 844})
    # Owner ceilings are distinct from the governor effective budgets.
    expect(page.get_by_role("group", name="Cache storage", exact=True)).to_have_count(0)
    expect(page.get_by_label("Media disk cache mode", exact=True)).to_be_visible()
    expect(page.get_by_label("Media memory cache mode", exact=True)).to_be_visible()
    expect(
        page.get_by_role("group", name="Automatic preloading", exact=True)
    ).to_be_visible()
    expect(
        page.get_by_role("button", name="Save Performance Settings")
    ).to_be_disabled()
    preload = page.get_by_role("switch", name="Preload during playback", exact=True)
    expect(preload).to_be_checked()
    expect(preload).to_have_accessible_description(
        re.compile(r"^Adapts extra server caching to media playback\.")
    )
    preload.locator("xpath=ancestor::label").click()
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
    assert saved.value.request.post_data_json == {"auto_preload": False}
    assert saved.value.json()["auto_preload"] is False
    expect(page.locator(".toast")).to_have_text("Performance settings saved.")
    expect(preload).not_to_be_checked()
    expect(
        page.get_by_role("button", name="Save Performance Settings")
    ).to_be_disabled()
    page.screenshot(
        path=ARTIFACTS / "owner-performance-settings-mobile.png", full_page=True
    )


def measured_snapshot(page, server):
    snapshot = page.request.get(server.url + "/api/v1/performance").json()
    now = time.time()
    snapshot["server"].update(
        at=now,
        sampled_at=now,
        stale_after_seconds=15,
        cpu={"percent": 12.5, "scope": "host"},
        network={
            "available": True,
            "interface": "eth0",
            "sent_bytes_per_second": 2_000_000,
            "received_bytes_per_second": 1_000_000,
        },
        history=[
            {
                "at": now - 15 + index * 3,
                "sent_bytes_per_second": None
                if index == 3
                else (index + 1) * 2_000_000 / 6,
                "received_bytes_per_second": None
                if index == 3
                else (index + 1) * 1_000_000 / 6,
                "network_interface": "eth0",
            }
            for index in range(6)
        ],
    )
    capacity = {
        "filesystem": {
            "available": True,
            "total_bytes": 100 * 1024**3,
            "free_bytes": 80 * 1024**3,
            "used_bytes": 20 * 1024**3,
        },
        "backing": {
            "configured": True,
            "available": True,
            "total_bytes": 50 * 1024**3,
            "free_bytes": 30 * 1024**3,
            "used_bytes": 20 * 1024**3,
        },
        "effective": {
            "available": True,
            "total_bytes": 50 * 1024**3,
            "free_bytes": 30 * 1024**3,
            "headroom_bytes": 25 * 1024**3,
        },
        "scope": "configured_backing_probe",
    }
    snapshot["server"]["disk"]["capacity"] = capacity
    snapshot["tuning"] = {
        "running": True,
        "interval_seconds": 5,
        "values": {"cache_memory_bytes": 256 * 1024**2},
        "reasons": {"cache_memory_bytes": "Resource-based ceiling"},
        "measurements": {},
    }
    snapshot["resource_summary"] = {
        "sampled_at": now,
        "cpu": {"scope": "cgroup", "source": "cgroup_v2", "capacity_cores": 1.5},
        "memory": {
            "scope": "cgroup",
            "source": "cgroup_v2",
            "used_bytes": 256 * 1024**2,
            "limit_bytes": 1024**3,
            "cgroup_limit_bytes": 1024**3,
            "environment_total_bytes": 8 * 1024**3,
            "limit_source": "cgroup_v2",
            "available_bytes": 512 * 1024**2,
        },
        "storage": {"data": capacity, "cache": capacity},
    }
    snapshot["traffic"].update(
        at=now, download_bytes_per_second=0, upload_bytes_per_second=0
    )
    snapshot["telegram"]["accounts"] = [
        {
            "name": "storage-bot-with-a-long-name-8066775295",
            "kind": "bot",
            "uploads": 1,
            "downloads": 0,
            "combined_limit": 3,
            "cooldown_seconds": 0,
            "requests": {
                "active": 4,
                "queued": 0,
                "upload_bytes": 347 * 1024**2,
                "download_bytes": 0,
                "effective_limit": 8,
                "configured_limit": 8,
                "effective_parts_per_second": 48,
                "configured_parts_per_second": 48,
                "floods": 0,
                "failed": 0,
                "completed": 1,
                "bytes": 347 * 1024**2,
            },
        }
    ]
    snapshot["diagnosis"] = {
        "sampled_at": now,
        "state": "active",
        "stale_after_seconds": 15,
        "window_seconds": 30,
        "observed_seconds": 12,
        "workload": {
            "playback_requests": 0,
            "download_requests": 0,
            "unknown_file_requests": 0,
            "upload_requests": 0,
        },
        "telegram_inbound_bytes_per_second": None,
        "cache_hits": None,
        "cache_misses": None,
        "constraints": [
            {
                "code": "telegram_cooldown",
                "certainty": "observed",
                "evidence": [{"name": "cooldown", "value": 3, "unit": "seconds"}],
            }
        ],
    }
    return snapshot


@pytest.mark.parametrize(
    "width,theme",
    [(1920, "light"), (1440, "dark"), (820, "light"), (390, "light"), (320, "dark")],
)
def test_performance_views_keep_layout_and_measured_activity(
    server, desktop, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 695 if width > 1160 else 844})
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance','{theme}')")
    page.emulate_media(reduced_motion="reduce")
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    snapshot["telegram"]["queued"] = 2
    snapshot["telegram"]["accounts"][0]["cooldown_seconds"] = 3
    snapshot["telegram"]["accounts"][0]["requests"]["queued"] = 2
    for index, sample in enumerate(snapshot["server"]["history"]):
        sample.update(
            cpu_percent=30 + index,
            cpu_scope="cgroup",
            memory_bytes=(128 + index) * 1024**2,
        )
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    overview = page.get_by_role("tabpanel", name="Overview", exact=True)
    expect(page.get_by_role("tab", name="Overview", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    readings = overview.locator(".network-readings dd")
    expect(readings).to_have_text(["16 Mbps", "8 Mbps"])
    expect(readings.first).to_have_css("font-size", "18px")
    for label in ("Overview", "Server resources", "Tuning", "Accounts"):
        expect(page.get_by_role("tab", name=label, exact=True)).to_be_visible()
    expect(overview.locator(".performance-network-chart")).to_have_count(1)
    expect(overview.locator(".network-trend")).to_have_count(0)
    activity = overview.get_by_role("region", name="Transfer activity")
    waiting = overview.get_by_role("region", name="Current waits")
    expect(activity).to_be_visible()
    expect(waiting).to_be_visible()
    expect(
        waiting.get_by_text("2 operations queued · 1 account on hold", exact=True)
    ).to_have_count(1)
    expect(activity).not_to_contain_text("Telegram")
    waiting_box = waiting.bounding_box()
    activity_box = activity.bounding_box()
    assert waiting_box and activity_box
    if width >= 1080:
        assert waiting_box["x"] >= activity_box["x"] + activity_box["width"]
    else:
        assert waiting_box["y"] >= activity_box["y"] + activity_box["height"]
    upload_color = overview.locator("polyline.performance-plot-active").first.evaluate(
        "n=>getComputedStyle(n).stroke"
    )
    download_color = overview.locator(
        "polyline.performance-plot-queued"
    ).first.evaluate("n=>getComputedStyle(n).stroke")
    workspace = page.locator(".workspace")
    page.evaluate("document.fonts.ready")
    workspace.evaluate("n=>n.scrollTo({top:0,behavior:'instant'})")
    expect(workspace).to_have_js_property("scrollTop", 0)
    page.screenshot(
        path=ARTIFACTS / f"performance-overview-{width}-{theme}.png",
        animations="disabled",
    )
    if width <= 820:
        tabs = page.get_by_role("tablist", name="Performance views").bounding_box()
        expect(page.get_by_role("button", name="Refresh Performance")).to_have_count(0)
        assert tabs and tabs["x"] + tabs["width"] <= width
        tabs_center = tabs["y"] + tabs["height"] / 2
        for tab in page.get_by_role("tab").all():
            bounds = tab.bounding_box()
            assert bounds and bounds["height"] >= 44
            assert abs(bounds["y"] + bounds["height"] / 2 - tabs_center) <= 1

    transfers_link = overview.get_by_role("link", name="Open Transfers", exact=True)
    transfers_link.focus()
    expect(transfers_link).to_be_focused()
    if width <= 720:
        footer = page.get_by_role("navigation", name="Sections", exact=True)
        assert (
            transfers_link.bounding_box()["y"] + transfers_link.bounding_box()["height"]
            <= footer.bounding_box()["y"]
        )

    waiting.get_by_role("button", name="View Accounts").press("Enter")
    expect(page.get_by_role("tab", name="Accounts", exact=True)).to_be_focused()
    accounts = page.get_by_role("tabpanel", name="Accounts", exact=True)
    expect(
        accounts.get_by_role("cell", name="347 MiB uploaded 0 B downloaded", exact=True)
    ).to_be_visible()
    expect(
        accounts.get_by_role("columnheader", name="Media operations", exact=True)
    ).to_be_visible()
    expect(
        accounts.get_by_role("heading", name="Waiting for Telegram", exact=True)
    ).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    assert page.evaluate("document.scrollingElement.scrollHeight <= innerHeight + 1")
    workspace.evaluate("n=>n.scrollTo({top:0,behavior:'instant'})")
    page.screenshot(
        path=ARTIFACTS / f"final-accounts-{width}-{theme}.png", animations="disabled"
    )
    page.get_by_role("tab", name="Overview", exact=True).click()
    resources = page.get_by_role("tabpanel", name="Overview", exact=True)
    history = resources.get_by_role("heading", name="History", exact=True)
    expect(history).to_be_visible()
    history.evaluate(
        "node => node.scrollIntoView({block: 'start', behavior: 'instant'})"
    )
    assert page.evaluate("scrollY") == 0
    assert page.evaluate("document.scrollingElement.scrollHeight <= innerHeight + 1")
    network_card = resources.locator(".performance-network-chart")
    cpu_card = resources.locator(".performance-resource-charts > section").first
    ram_card = resources.locator(".performance-resource-charts > section").last
    network_box = network_card.bounding_box()
    cpu_box = cpu_card.bounding_box()
    ram_box = ram_card.bounding_box()
    assert network_box and cpu_box and ram_box
    assert abs(cpu_box["x"] - ram_box["x"]) <= 1
    assert ram_box["y"] - (cpu_box["y"] + cpu_box["height"]) >= 12
    for card in (network_card, cpu_card, ram_card):
        plot = card.locator(".performance-chart-plot > svg").bounding_box()
        times = card.locator(".performance-chart-times").bounding_box()
        assert plot and times and plot["y"] + plot["height"] <= times["y"] + 1
    if width >= 1160:
        assert abs(network_box["y"] - cpu_box["y"]) <= 1
        assert cpu_box["x"] - (network_box["x"] + network_box["width"]) >= 12
    else:
        assert abs(network_box["x"] - cpu_box["x"]) <= 1
        assert cpu_box["y"] - (network_box["y"] + network_box["height"]) >= 12
    expect(network_card.locator(".performance-chart-plot circle")).to_have_count(0)
    expect(network_card).to_contain_text("Upload from server")
    expect(network_card).to_contain_text("Download to server")
    expect(network_card.locator("polyline.performance-plot-active").first).to_have_css(
        "stroke", upload_color
    )
    expect(network_card.locator("polyline.performance-plot-queued").first).to_have_css(
        "stroke", download_color
    )
    dash = network_card.locator("polyline.performance-plot-queued").first.evaluate(
        "n=>getComputedStyle(n).strokeDasharray.split(/[ ,]+/).map(parseFloat)"
    )
    assert dash == [5, 4]
    legend = network_card.locator(".network-readings .chart-series-secondary")
    assert (
        legend.evaluate("n=>getComputedStyle(n,'::before').borderTopStyle") == "dashed"
    )

    readout = network_card.locator(".performance-chart-tooltip")
    expect(readout).to_have_count(0)
    inspect = network_card.get_by_role("button", name="Inspect Server network samples")
    inspect.press("Home")
    expect(readout).to_be_visible()
    assert (
        readout.locator(".chart-series-primary").evaluate(
            "n=>getComputedStyle(n,'::before').backgroundColor"
        )
        == upload_color
    )
    secondary_marker = readout.locator(".chart-series-secondary")
    assert (
        secondary_marker.evaluate("n=>getComputedStyle(n,'::before').borderTopColor")
        == download_color
    )
    assert (
        secondary_marker.evaluate("n=>getComputedStyle(n,'::before').borderTopStyle")
        == "dashed"
    )
    inspect.press("Escape")
    expect(readout).to_have_count(0)
    expect(inspect).to_be_focused()

    expect(network_card.locator(".performance-chart-axis")).not_to_contain_text("MiB/s")

    page.get_by_role("tab", name="Server resources", exact=True).click()
    storage = page.get_by_role("tabpanel", name="Server resources", exact=True)
    expect(storage.get_by_text("RAM cache", exact=True)).to_be_visible()
    assert storage.locator(".performance-resource-summary").evaluate(
        """(el, wide) => {
        const containerRight = el.getBoundingClientRect().right;
        const rowEnds = new Map();
        for (const meter of el.querySelectorAll('.performance-cache-meter-grid > *')) {
            const {top, right} = meter.getBoundingClientRect();
            rowEnds.set(top, Math.max(rowEnds.get(top) ?? 0, right));
        }
        return rowEnds.size > 0 && (!wide || rowEnds.size === 1) &&
        [...rowEnds.values()].every(right => {
            return containerRight - right <= 2;
        });
    }""",
        width >= 1160,
    )
    memory_detail = storage.locator(".performance-memory-details dd + dd").first
    expect(memory_detail).to_have_css("font-size", "13px")
    expect(storage.locator(".performance-facts > div > :not(dt, dd)")).to_have_count(0)
    expect(storage.get_by_text("TGDrive process", exact=True)).to_be_visible()
    expect(storage.get_by_text("1.5 CPU equivalents", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Tuning", exact=True).click()
    diagnostics = page.get_by_role("tabpanel", name="Tuning", exact=True)
    tuning = diagnostics.get_by_role("region", name="Automatic tuning", exact=True)
    expect(tuning).to_be_visible()
    page.get_by_role("tab", name="Overview", exact=True).click()
    totals = overview.get_by_role("region", name="All-time totals", exact=True)
    table = totals.get_by_role("table")
    expect(table).to_be_visible()
    expect(totals).to_have_css("border-inline-start-width", "1px")
    totals.scroll_into_view_if_needed()
    panel_box = totals.bounding_box()
    table_box = table.bounding_box()
    assert panel_box and table_box
    assert table_box["x"] - panel_box["x"] >= 14
    assert (
        panel_box["x"] + panel_box["width"] - table_box["x"] - table_box["width"] >= 14
    )
    assert totals.bounding_box()["y"] > network_card.bounding_box()["y"]
    page.get_by_role("tab", name="Tuning", exact=True).click()
    expect(diagnostics.locator("details")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    workspace.evaluate("n=>n.scrollTo({top:0,behavior:'instant'})")
    page.screenshot(
        path=ARTIFACTS / f"performance-tuning-{width}-{theme}.png",
        animations="disabled",
    )


@pytest.mark.parametrize(
    "width,height,theme", [(1536, 695, "dark"), (390, 844, "light")]
)
def test_showcase_tuning_reports_effective_values_and_retains_stale_reading(
    monkeypatch, request, desktop, width, height, theme
):
    monkeypatch.setenv("TGDRIVE_UI_SHOWCASE", "1")
    server = request.getfixturevalue("server")
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance','{theme}')")
    login(page, server.url)
    response = page.request.get(server.url + "/api/v1/performance")
    assert response.ok
    snapshot = response.json()
    assert snapshot["tuning"]["values"]["cache_disk_bytes"] == 6 * 1024**3
    assert snapshot["tuning"]["values"]["staging_budget_bytes"] == 12 * 1024**3
    assert snapshot["tuning"]["measurements"]["disk_effective_free"] == 14 * 1024**3

    page.clock.install()
    page.goto(server.url + "/?view=performance")
    page.get_by_role("tab", name="Tuning", exact=True).click()
    tuning = page.get_by_role("region", name="Automatic tuning", exact=True)
    expect(tuning).to_contain_text("Example")
    expect(tuning).to_contain_text("6 GiB")
    expect(tuning).to_contain_text("50% of free media-cache disk after reserve")
    expect(tuning).to_contain_text("12 GiB")
    expect(tuning).not_to_contain_text("Other Parameters")
    expect(tuning).not_to_contain_text("startup ceiling")
    fits_line = tuning.get_by_text("12 GiB", exact=True).first.evaluate("""value => {
        const range = document.createRange();
        range.selectNodeContents(value);
        return range.getClientRects().length === 1;
    }""")
    assert fits_line
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=ARTIFACTS / f"tuning-example-{width}.png", animations="disabled"
    )

    state = "stale"

    def report(route):
        payload = {**snapshot, "tuning": dict(snapshot["tuning"])}
        if state == "stale":
            payload["tuning"]["measurements"] = {
                "sampled_at": snapshot["server"]["at"] - 45
            }
        elif state == "missing":
            payload["tuning"] = None
        route.fulfill(json=payload)

    page.route("**/api/v1/performance", report)
    page.clock.fast_forward(6000)
    expect(tuning).to_contain_text("Last applied values")
    expect(tuning).to_contain_text("6 GiB")
    state = "missing"
    page.clock.fast_forward(6000)
    expect(tuning).to_contain_text("Waiting for a tuning report")
    expect(tuning).not_to_contain_text("6 GiB")
    state = "fresh"
    page.clock.fast_forward(6000)
    expect(tuning).to_contain_text("6 GiB")
    expect(tuning).not_to_contain_text("Last applied values")


@pytest.mark.parametrize("state", ["missing", "stale", "zero"])
def test_performance_missing_and_stale_samples_never_become_zero(
    server, desktop, state
):
    _, page = desktop
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    if state == "missing":
        snapshot.pop("resource_summary")
        snapshot.pop("diagnosis")
        snapshot["server"].pop("sampled_at")
    elif state == "stale":
        snapshot["server"]["sampled_at"] -= 60
        snapshot["diagnosis"]["sampled_at"] -= 60
    else:
        snapshot["server"]["network"].update(
            sent_bytes_per_second=0, received_bytes_per_second=0
        )
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    expected = (
        ["0 Mbps", "0 Mbps"] if state == "zero" else ["Unavailable", "Unavailable"]
    )
    expect(page.locator(".network-readings dd")).to_have_text(expected)
    if state != "zero":
        expect(page.get_by_role("region", name="Current waits")).to_contain_text(
            "Current wait readings unavailable."
        )
    if state == "missing":
        page.get_by_role("tab", name="Overview", exact=True).click()
        expect(
            page.get_by_role("tabpanel", name="Overview", exact=True)
        ).to_contain_text("Unavailable")


def test_performance_failed_poll_preserves_context_and_recovers(server, desktop):
    _, page = desktop
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    fail = False

    def respond(route):
        if fail:
            route.fulfill(
                status=503, json={"detail": "Snapshot temporarily unavailable"}
            )
        else:
            route.fulfill(json=snapshot)

    page.clock.install()
    page.route("**/api/v1/performance", respond)
    page.goto(server.url + "/?view=performance")
    expect(page.locator(".network-readings dd")).to_have_text(["16 Mbps", "8 Mbps"])
    fail = True
    page.clock.fast_forward(5100)
    expect(page.locator("#performance").get_by_role("alert")).to_contain_text(
        "Showing the last loaded snapshot"
    )
    expect(page.locator(".toast-error")).to_have_count(0)
    expect(page.locator(".network-readings dd")).to_have_text(
        ["Unavailable", "Unavailable"]
    )
    page.get_by_role("tab", name="Accounts", exact=True).click()
    expect(page.get_by_role("tabpanel", name="Accounts", exact=True)).to_contain_text(
        "347 MiB"
    )
    page.get_by_role("tab", name="Overview", exact=True).click()
    fail = False
    page.clock.fast_forward(5100)
    expect(page.locator(".network-readings dd")).to_have_text(["16 Mbps", "8 Mbps"])
    expect(page.locator("#performance").get_by_role("alert")).not_to_be_visible()


def test_performance_poll_waits_for_the_pending_snapshot(server, desktop):
    _, page = desktop
    page.set_viewport_size({"width": 390, "height": 844})
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    old = json.loads(json.dumps(snapshot))
    old["server"]["network"]["sent_bytes_per_second"] = 99_000_000
    held = []
    calls = 0

    def respond(route):
        nonlocal calls
        calls += 1
        if calls == 2:
            held.append(route)
        else:
            route.fulfill(json=snapshot)

    page.clock.install()
    page.route("**/api/v1/performance", respond)
    page.goto(server.url + "/?view=performance")
    expect(page.locator(".network-readings dd").first).to_have_text("16 Mbps")
    network = page.locator(".performance-network-chart")
    inspect = network.get_by_role("button", name="Inspect Server network samples")
    inspect.press("Home")
    readout = network.locator(".performance-chart-tooltip")
    expect(readout).to_be_visible()
    inspected_text = readout.text_content()
    workspace = page.locator(".workspace")
    scroll_top = workspace.evaluate("node => node.scrollTop")
    selected_range = page.get_by_label("Time range", exact=True).input_value()
    with page.expect_request(
        lambda request: request.url.endswith("/api/v1/performance")
    ):
        page.clock.fast_forward(5100)
    page.clock.fast_forward(10_000)
    assert calls == 2 and len(held) == 1
    held[0].fulfill(json=old)
    expect(page.locator(".network-readings dd").first).to_have_text("792 Mbps")
    page.clock.fast_forward(5100)
    expect(page.locator(".network-readings dd").first).to_have_text("16 Mbps")

    expect(page.get_by_role("tab", name="Overview", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.get_by_label("Time range", exact=True)).to_have_value(selected_range)
    expect(inspect).to_be_focused()
    expect(readout).to_have_text(inspected_text)
    expect(workspace).to_have_js_property("scrollTop", scroll_top)


def test_performance_help_supports_keyboard_escape_and_focus_return(server, desktop):
    _, page = desktop
    login(page, server.url + "/?view=performance")
    help_button = page.get_by_role("button", name="About Server network", exact=True)
    expect(help_button).to_be_visible()
    help_button.focus()
    page.keyboard.press("Enter")
    content = page.get_by_role(
        "dialog", name="About Server network", exact=True
    ).filter(has_text="all traffic on the reported interface")
    expect(content).to_be_visible()
    expect(content.locator("[data-autofocus]")).to_be_focused()
    page.keyboard.press("Escape")
    expect(content).not_to_be_visible()
    expect(help_button).to_be_focused()
    help_button.press("Enter")
    expect(content).to_be_visible()
    expect(content.locator("[data-autofocus]")).to_be_focused()
    help_button.press("Escape")
    expect(content).not_to_be_visible()
    expect(help_button).to_be_focused()
    page.set_viewport_size({"width": 390, "height": 844})
    help_button.click()
    expect(content).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_transfer_speed_controls_preserve_conflicts_and_category_save(server, desktop):
    _, page = desktop
    login(page, server.url)
    saved = page.request.get(server.url + "/api/v1/settings/performance").json()
    saved.update(
        upload_limit_bytes_per_second=None, download_limit_bytes_per_second=1_234_567
    )
    requests = []
    conflict = True

    def settings(route):
        nonlocal saved
        if route.request.method == "PATCH":
            requests.append(route.request.post_data_json)
            assert route.request.headers["if-match"] == saved["revision"]
            if conflict:
                route.fulfill(
                    status=409,
                    json={"detail": "Settings changed elsewhere", "code": "CONFLICT"},
                )
            else:
                saved = {
                    **saved,
                    **route.request.post_data_json,
                    "revision": '"saved-speed"',
                }
                route.fulfill(json=saved)
        else:
            route.fulfill(json=saved)

    page.route("**/api/v1/settings/performance", settings)
    page.goto(server.url + "/?view=settings&section=transfers")
    upload = page.get_by_role(
        "combobox", name="Upload from server limit mode", exact=True
    )
    upload.select_option("custom")
    page.get_by_role(
        "textbox", name="Upload from server limit (Mbps)", exact=True
    ).fill("80")
    save = page.get_by_role("button", name="Save Performance Settings", exact=True)
    save.click()
    expect(page.get_by_role("alert")).to_contain_text("Settings changed elsewhere")
    expect(save).to_be_disabled()
    conflict = False
    page.get_by_role("button", name="Reload Latest Settings", exact=True).click()
    expect(upload).to_have_value("unlimited")
    upload.select_option("custom")
    page.get_by_role(
        "textbox", name="Upload from server limit (Mbps)", exact=True
    ).fill("80")
    save.click()
    expect(save).to_be_disabled()
    assert requests == [{"upload_limit_bytes_per_second": 10_000_000}] * 2
    expect(
        page.get_by_role("combobox", name="Download to server limit mode")
    ).to_have_value("custom")
    page.reload()
    expect(upload).to_have_value("custom")
    expect(
        page.get_by_role("textbox", name="Upload from server limit (Mbps)")
    ).to_have_value(re.compile(r"^80(?:\.0+)? Mbps$"))


@pytest.mark.parametrize(
    "width,height,theme",
    [(1536, 695, "dark"), (820, 1180, "light"), (390, 844, "dark")],
)
def test_transfer_speed_controls_save_both_limits_to_the_backend(
    server, desktop, width, height, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance','{theme}')")
    login(page, server.url)
    page.goto(server.url + "/?view=performance")
    page.get_by_role("tab", name="Tuning", exact=True).click()
    link = page.get_by_role("link", name="Transfer limits", exact=True)
    expect(link).to_be_visible()
    expect(
        page.get_by_role("region", name="Transfer activity", exact=True)
    ).to_be_hidden()
    expect(link).to_be_in_viewport()

    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / f"tuning-speed-limits-{width}.png")
    if width == 820:
        link.focus()
        expect(link).to_be_focused()
        link.press("Enter")
    else:
        link.click()
    expect(page).to_have_url(re.compile(r"\?view=settings&section=transfers$"))
    for direction, rate in (("Upload from server", "80"), ("Download to server", "20")):
        page.get_by_role(
            "combobox", name=f"{direction} limit mode", exact=True
        ).select_option("custom")
        page.get_by_role("textbox", name=f"{direction} limit (Mbps)", exact=True).fill(
            rate
        )
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith("/api/v1/settings/performance")
        )
    ) as saved:
        page.get_by_role("button", name="Save Performance Settings", exact=True).click()
    assert saved.value.ok, saved.value.text()
    persisted = page.request.get(server.url + "/api/v1/settings/performance").json()
    assert persisted["upload_limit_bytes_per_second"] == 10_000_000
    assert persisted["download_limit_bytes_per_second"] == 2_500_000

    page.reload()
    for direction, rate in (("Upload from server", "80"), ("Download to server", "20")):
        expect(
            page.get_by_role("textbox", name=f"{direction} limit (Mbps)", exact=True)
        ).to_have_value(re.compile(rf"^{rate}(?:\.0+)? Mbps$"))


def test_intentional_speed_wait_is_separate_from_network_failure(server, desktop):
    _, page = desktop
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    direction = {
        "configured_bytes_per_second": 1024,
        "effective_bytes_per_second": 512,
        "waiting": 2,
        "admitted_bytes": 123,
        "wait_seconds": 30,
    }
    snapshot["bandwidth"] = {
        "upload": direction,
        "download": {**direction, "waiting": 0},
        "native_engines": 1,
        "scope": "managed_file_payload",
    }
    snapshot["diagnosis"]["constraints"] = []
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    waiting = page.get_by_role("region", name="Current waits", exact=True)
    expect(waiting).to_contain_text("Server uploads waiting at <0.01 Mbps")
    expect(page.locator(".network-readings dd").first).to_have_text("16 Mbps")
    waiting.get_by_role("link", name="Transfer limits", exact=True).click()
    expect(page).to_have_url(re.compile(r"\?view=settings&section=transfers$"))
    expect(
        page.get_by_role("heading", name="Uploads and Telegram", exact=True)
    ).to_be_visible()


def test_overview_shows_stream_waits_but_omits_idle_cooldowns(server, desktop):
    _, page = desktop
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    snapshot["telegram"]["queued"] = 0
    snapshot["telegram"]["accounts"][0]["cooldown_seconds"] = 3
    snapshot["diagnosis"]["constraints"] = []
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    waiting = page.get_by_role("region", name="Current waits", exact=True)
    expect(waiting).to_contain_text("No service waits observed.")
    expect(waiting.get_by_role("button", name="View Accounts")).to_have_count(0)

    snapshot["diagnosis"]["constraints"] = [
        {
            "code": "scratch_wait",
            "certainty": "observed",
            "evidence": [{"name": "waiting_heads", "value": 2, "unit": "streams"}],
        }
    ]
    expect(waiting).to_contain_text(
        "2 streams waiting for a read buffer", timeout=9_000
    )
    waiting.get_by_role("button", name="Server resources").press("Enter")
    resources = page.get_by_role("tab", name="Server resources", exact=True)
    expect(resources).to_have_attribute("aria-selected", "true")
    expect(resources).to_be_focused()


def test_resource_long_unknown_value_is_contained_at_320(server, desktop):
    _, page = desktop
    page.set_viewport_size({"width": 320, "height": 844})
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    snapshot["resource_summary"]["storage"]["data"]["backing"].update(
        configured=False, available=False
    )
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    page.get_by_role("tab", name="Server resources", exact=True).click()
    value = page.get_by_text("Not configured", exact=True)
    expect(value).to_be_visible()
    boxes = value.evaluate("""n=>{
        const box=x=>{
            const r=x.getBoundingClientRect();
            return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};
        };
        return {label:box(n.previousElementSibling),
                value:box(n),row:box(n.parentElement)};
    }""")
    assert (
        boxes["label"]["right"] <= boxes["value"]["left"] + 1
        or boxes["label"]["bottom"] <= boxes["value"]["top"] + 1
    )
    assert boxes["value"]["left"] >= boxes["row"]["left"] - 1
    assert boxes["value"]["right"] <= boxes["row"]["right"] + 1
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_isolated_network_samples_are_visible_without_bridging_gaps(
    server, desktop, theme
):
    _, page = desktop
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance','{theme}')")
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    now = snapshot["server"]["at"]
    snapshot["server"]["history"] = [
        {
            "at": now - 6,
            "sent_bytes_per_second": 1_000_000,
            "received_bytes_per_second": 500_000,
            "network_interface": "eth0",
        },
        {
            "at": now - 3,
            "sent_bytes_per_second": None,
            "received_bytes_per_second": None,
            "network_interface": "eth0",
        },
        {
            "at": now,
            "sent_bytes_per_second": 2_000_000,
            "received_bytes_per_second": 1_000_000,
            "network_interface": "eth0",
        },
    ]
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    chart = page.locator(".performance-network-chart .performance-chart-plot > svg")
    points = chart.locator(
        "circle.performance-chart-point, circle.performance-chart-point-secondary"
    )
    expect(points).to_have_count(4)
    expect(chart.locator("polyline")).to_have_count(0)
    for point in points.all():
        reading = point.evaluate(
            "n=>({r:n.r.baseVal.value,fill:getComputedStyle(n).fill,opacity:getComputedStyle(n).opacity,box:n.getBoundingClientRect().width})"
        )
        assert reading["r"] >= 2 and reading["box"] > 0 and reading["opacity"] == "1"
        assert reading["fill"] != "none"
    page.screenshot(
        path=ARTIFACTS / f"final-isolated-points-{theme}.png", animations="disabled"
    )


@pytest.mark.parametrize("backing_known", [True, False], ids=["verified", "unknown"])
def test_primary_storage_uses_verified_envelope_not_virtual_filesystem(
    server, desktop, backing_known
):
    _, page = desktop
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    snapshot["server"]["disk"].update(
        available=True,
        total_bytes=1024 * 1024**3,
        used_bytes=178 * 1024**3,
        free_bytes=846 * 1024**3,
    )
    capacity = snapshot["resource_summary"]["storage"]["data"]
    capacity["backing"].update(
        configured=backing_known,
        available=backing_known,
        total_bytes=200 * 1024**3,
        free_bytes=100 * 1024**3,
    )
    capacity["effective"].update(
        available=True,
        free_bytes=100 * 1024**3,
        headroom_bytes=45 * 1024**3,
    )
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    page.get_by_role("tab", name="Server resources", exact=True).click()
    primary = page.locator('[data-storage="effective"]')
    expect(primary).to_contain_text("Verified free disk space")
    expect(primary.locator("strong")).to_have_text(
        "100 GiB" if backing_known else "Unavailable"
    )
    expect(primary).not_to_contain_text("846")
    expect(primary.locator("progress")).to_have_count(0)
    if backing_known:
        expect(primary).to_contain_text("45 GiB available after reserve")
        expect(page.locator('[data-storage="filesystem"]')).to_have_count(0)
    else:
        visible = page.locator('[data-storage="filesystem"]')
        expect(visible).to_contain_text("Filesystem visible to TGDrive")
        expect(visible).to_contain_text("846 GiB free of 1 TiB")
    details = page.get_by_role("region", name="Filesystem measurement details")
    expect(details).to_be_visible()
    expect(details).to_contain_text("846 GiB")


def test_live_speed_headlines_are_compact_without_rounding_to_false_zero(
    server, desktop
):
    _, page = desktop
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    snapshot["server"]["network"].update(
        sent_bytes_per_second=11_702.375,
        received_bytes_per_second=1_024,
    )
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    expect(page.locator(".network-readings dd")).to_have_text(
        ["0.09 Mbps", "<0.01 Mbps"]
    )


@pytest.mark.parametrize("scope", ["host", "unavailable"])
def test_memory_scope_does_not_invent_a_container_limit(server, desktop, scope):
    _, page = desktop
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    snapshot["resource_summary"]["memory"].update(
        scope=scope,
        limit_bytes=8 * 1024**3 if scope == "host" else None,
        used_bytes=2 * 1024**3 if scope == "host" else None,
        available_bytes=3 * 1024**3 if scope == "host" else None,
    )
    snapshot["server"]["memory"].update(cgroup_current=None, cgroup_limit=None)
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    memory = page.locator('[data-metric="memory_bytes"]')
    title = "Visible system RAM" if scope == "host" else "Server memory"
    expect(memory.get_by_role("heading")).to_have_text(title)
    memory.get_by_role("button", name=f"About {title}").click()
    help_popup = page.get_by_role("dialog", name=f"About {title}")
    if scope == "host":
        expect(help_popup).to_contain_text("not a container hard limit")
    else:
        expect(help_popup).to_contain_text(
            "No finite visible container limit established"
        )
    expect(help_popup).not_to_contain_text("Current container limit")


def test_effective_memory_budget_is_not_a_container_hard_limit(server, desktop):
    _, page = desktop
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    snapshot["resource_summary"]["memory"].update(
        scope="cgroup",
        used_bytes=256 * 1024**2,
        limit_bytes=8 * 1024**3,
        cgroup_limit_bytes=None,
        environment_total_bytes=8 * 1024**3,
        limit_source="proc_meminfo",
        available_bytes=3 * 1024**3,
    )
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    memory = page.locator('[data-metric="memory_bytes"]')
    expect(memory.locator('[title="Current reading"]')).not_to_contain_text("8 GiB")
    memory.get_by_role("button", name="About Container RAM").click()
    expect(page.get_by_role("dialog", name="About Container RAM")).to_contain_text(
        "No finite visible container limit established"
    )
    expect(page.get_by_role("link", name="Open Transfers", exact=True)).to_have_count(1)
    page.get_by_role("tab", name="Server resources", exact=True).click()
    budget = page.locator(".performance-facts > div").filter(
        has=page.get_by_text("Memory reference for automatic limits", exact=True)
    )
    expect(budget).to_contain_text("8 GiB")
    expect(budget).to_contain_text("Uses the memory reported by Linux")
    expect(budget).not_to_contain_text("proc_meminfo")
    expect(page.get_by_role("heading", name="Drive contents")).to_have_count(0)
    expect(page.get_by_role("heading", name="Temporary upload storage")).to_be_visible()


@pytest.mark.parametrize(
    "width,theme", [(1536, "dark"), (820, "light"), (390, "light")]
)
def test_overview_library_and_server_resources_are_separate(
    server, desktop, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844 if width < 721 else 695})
    page.emulate_media(color_scheme=theme)
    login(page, server.url)
    management = page.request.get(server.url + "/api/v1/settings").json()
    management["stats"].update(
        files=17,
        folders=3,
        trashed_files=2,
        total_bytes=5 * 1024**2,
        trashed_bytes=2 * 1024,
        active_jobs=1,
        queued_jobs=2,
        failed_jobs=3,
        staging_reserved_bytes=1024**2,
        staging_budget_bytes=8 * 1024**2,
    )
    page.route("**/api/v1/settings", lambda route: route.fulfill(json=management))
    page.goto(server.url + "/?view=storage")
    expect(
        page.get_by_role("heading", name="MongoDB catalog", exact=True)
    ).to_be_visible()
    for label in [
        "Temporary upload storage",
        "Current Transfers",
        "Start a new drive",
    ]:
        expect(page.get_by_role("heading", name=label, exact=True)).to_have_count(0)
    expect(page.locator(".start-new-drive")).to_have_count(0)
    files = page.get_by_role("region", name="TGDrive library", exact=True)
    expect(files).to_have_count(0)

    snapshot = measured_snapshot(page, server)
    snapshot["drive"] = {
        key: management["stats"][key]
        for key in ("files", "folders", "total_bytes", "trashed_files", "trashed_bytes")
    }
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    expect(files.locator("dt")).to_have_text(
        ["Stored in Telegram", "Files", "Folders", "Files in Trash"]
    )
    expect(files.locator("dd")).to_have_text(
        ["5 MiB", "17", "3", "2", "2 KiB", "Open Trash"]
    )
    chart = page.get_by_role("heading", name="History", exact=True)
    assert files.bounding_box()["y"] < chart.bounding_box()["y"]
    assert files.evaluate("el => el.scrollWidth <= el.clientWidth")
    network = page.locator(".performance-network-chart")
    resources = page.locator(".performance-resource-charts")
    network_bounds, resource_bounds = network.bounding_box(), resources.bounding_box()
    assert network_bounds and resource_bounds
    if width == 1536:
        assert abs(network_bounds["y"] - resource_bounds["y"]) <= 1
        assert abs(network_bounds["height"] - resource_bounds["height"]) <= 1
    else:
        assert resource_bounds["y"] >= network_bounds["y"] + network_bounds["height"]
    trash = files.get_by_role("link", name="Open Trash", exact=True)
    trash.click()
    expect(page.get_by_role("heading", name="Trash", exact=True)).to_be_visible()
    page.go_back()
    expect(files).to_be_visible()
    expect(page.get_by_role("link", name="Open Transfers", exact=True)).to_have_count(1)
    page.get_by_role("tab", name="Server resources", exact=True).click()
    expect(files).not_to_be_visible()
    staging = page.get_by_role("region", name="Temporary upload storage", exact=True)
    expect(
        staging.get_by_role("progressbar", name="Upload staging reservations")
    ).to_be_visible()
    expect(staging).to_contain_text("1 MiB / 8 MiB")


def test_management_statistics_poll_only_while_resources_are_open(server, desktop):
    _, page = desktop
    login(page, server.url)
    snapshot = measured_snapshot(page, server)
    snapshot["drive"] = {"files": 17, "folders": 3, "total_bytes": 1024}
    management = page.request.get(server.url + "/api/v1/settings").json()
    settings_calls = []

    def settings(route):
        settings_calls.append(route.request.url)
        route.fulfill(json=management)

    page.clock.install()
    page.route("**/api/v1/settings", settings)
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    expect(page.get_by_role("tab", name="Overview", exact=True)).to_be_visible()
    files = (
        page.get_by_role("region", name="TGDrive library")
        .locator("div")
        .filter(has=page.get_by_role("term").filter(has_text=re.compile(r"^Files$")))
        .locator("dd")
    )
    expect(files).to_have_text("17")
    assert not settings_calls
    page.get_by_role("tab", name="Server resources", exact=True).click()
    staging = page.get_by_role("region", name="Temporary upload storage")
    expect(staging).to_contain_text("Upload space checked")
    assert len(settings_calls) == 1
    management["stats"].update(
        staging_reserved_bytes=2 * 1024**2, staging_budget_bytes=8 * 1024**2
    )
    page.clock.fast_forward(5100)
    expect(staging).to_contain_text("2 MiB / 8 MiB")
    assert len(settings_calls) == 2
    snapshot["drive"]["files"] = 23
    page.get_by_role("tab", name="Overview", exact=True).click()
    with page.expect_response(
        lambda response: response.url.endswith("/api/v1/performance")
    ):
        page.clock.fast_forward(5100)
    expect(files).to_have_text("23")
    assert len(settings_calls) == 2

    page.goto(server.url + "/?view=storage")
    expect(page.get_by_role("heading", name="Backups", exact=True)).to_be_visible()
    for _ in range(2):
        with page.expect_request(
            lambda request: request.url.endswith("/api/v1/status")
        ):
            page.clock.fast_forward(5100)
    assert len(settings_calls) == 2
