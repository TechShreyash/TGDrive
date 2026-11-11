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


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_neutral_theme_surfaces_and_monitor_layout(server, desktop, theme):
    _, page = desktop
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance', '{theme}')")
    page.goto(server.url)
    expect(page.locator("#login-dialog")).to_be_visible()

    contrast_readings = {}

    def check_surface(selector, *, neutral=True):
        colors = page.locator(selector).first.evaluate("""node => {
          const style = getComputedStyle(node);
          let parent = node;
          while (parent.parentElement &&
            getComputedStyle(parent).backgroundColor === 'rgba(0, 0, 0, 0)')
            parent = parent.parentElement;
          return {ink: style.color,
            background: getComputedStyle(parent).backgroundColor};
        }""")
        ink, background = (
            [int(value) for value in re.findall(r"\d+", colors[key])[:3]]
            for key in ("ink", "background")
        )
        if neutral:
            assert max(background) - min(background) <= 12, colors

        def luminance(channels):
            linear = [
                value / 255 / 12.92
                if value / 255 <= 0.04045
                else ((value / 255 + 0.055) / 1.055) ** 2.4
                for value in channels
            ]
            return sum(
                a * b for a, b in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
            )

        bright, dark = sorted((luminance(ink), luminance(background)), reverse=True)
        ratio = (bright + 0.05) / (dark + 0.05)
        assert ratio >= 4.5, colors
        contrast_readings[f"{page.url}:{selector}"] = {**colors, "ratio": ratio}

    check_surface("body")
    check_surface("#login-dialog .primary-button", neutral=False)
    check_surface("#login-dialog > p")
    page.screenshot(
        path=ARTIFACTS / f"neutral-login-{theme}.png", animations="disabled"
    )
    login(page)
    for view, heading in (
        ("files", "My Files"),
        ("preloads", "Preloads"),
        ("settings", "Cache & preloading"),
        ("performance", "Performance"),
    ):
        page.goto(server.url + f"/?view={view}")
        expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
        check_surface(".workspace")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(
            path=ARTIFACTS / f"neutral-{view}-{theme}.png", animations="disabled"
        )
    activity = page.get_by_role("region", name="Current activity", exact=True)
    assert activity.bounding_box()["height"] < 360
    expect(
        page.get_by_role("region", name="Automatic tuning", exact=True)
    ).not_to_be_visible()
    expect(
        page.get_by_role("combobox", name="Time range", exact=True)
    ).not_to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert activity.bounding_box()["height"] < 440
    resources = page.get_by_role("region", name="Server resources", exact=True)
    expect(resources).to_be_visible()
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        actions = [
            activity.get_by_role("link", name="View Transfers", exact=True),
            activity.get_by_role("link", name="Upload storage limits", exact=True),
            resources.get_by_role("link", name="Cache limits", exact=True),
            resources.get_by_role("button", name="View resource history", exact=True),
        ]
        for link in actions:
            assert link.bounding_box()["height"] >= 44
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("tab", name="Resources", exact=True).click()
    expect(
        page.get_by_role("region", name="Server network", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("combobox", name="Time range", exact=True)).to_be_visible()
    page.screenshot(
        path=ARTIFACTS / f"neutral-performance-mobile-{theme}.png",
        full_page=True,
        animations="disabled",
    )
    (ARTIFACTS / f"neutral-contrast-{theme}.json").write_text(
        json.dumps(contrast_readings, indent=2)
    )


@pytest.mark.parametrize("width", [1440, 390, 320])
@pytest.mark.parametrize("known", [True, False])
def test_overview_exposes_real_resources_and_idle_rates(server, desktop, width, known):
    _, page = desktop
    page.emulate_media(reduced_motion="reduce")
    login(page, server.url)
    snapshot = page.request.get(server.url + "/api/v1/performance").json()
    mib = 1024**2
    snapshot["traffic"].update(download_bytes_per_second=0, upload_bytes_per_second=0)
    snapshot["telegram"].update(active=0, queued=0)
    snapshot["diagnosis"] = {
        "sampled_at": snapshot["traffic"]["at"],
        "window_seconds": 30,
        "observed_seconds": 12,
        "stale_after_seconds": 15,
        "state": "idle",
        "workload": dict.fromkeys(
            [
                "playback_requests",
                "download_requests",
                "unknown_file_requests",
                "upload_requests",
            ],
            0,
        ),
        "constraints": [],
    }
    snapshot["server"]["cpu"] = {"percent": 25 if known else None, "scope": "host"}
    snapshot["server"]["memory"].update(
        cgroup_current=512 * mib if known else 0,
        cgroup_limit=1024 * mib if known else None,
    )
    snapshot["cache"]["disk"].update(
        available=known, bytes=128 * mib if known else 0, max_bytes=1024 * mib
    )
    snapshot["cache"]["memory"].update(
        bytes=32 * mib if known else 0, effective_max_bytes=64 * mib if known else 0
    )
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(server.url + "/?view=performance")
    activity = page.get_by_role("region", name="Current activity", exact=True)
    resources = page.get_by_role("region", name="Server resources", exact=True)
    expect(activity).to_contain_text("Nothing is transferring right now")
    expect(activity.locator(".performance-file-rates dd")).to_have_text(
        ["0 B/s", "0 B/s"]
    )
    expect(resources.get_by_role("progressbar")).to_have_count(4 if known else 0)
    activity_box = activity.bounding_box()
    resources_box = resources.bounding_box()
    if width == 1440:
        assert resources_box["x"] > activity_box["x"] + activity_box["width"]
        assert abs(resources_box["y"] - activity_box["y"]) < 2
    else:
        assert resources_box["y"] >= activity_box["y"] + activity_box["height"]
    expect(
        activity.locator(".performance-activity-actions").get_by_role("link")
    ).to_have_count(2)
    if known:
        expect(
            resources.get_by_role("progressbar", name="CPU usage", exact=True)
        ).to_have_attribute("value", "25")
        expect(resources).to_contain_text("512 MiB / 1 GiB")
    else:
        expect(resources).to_contain_text("0 B in use")
        expect(resources).to_contain_text("No container limit reported")
        expect(resources).to_contain_text("Disabled")
        expect(resources).to_contain_text("Unavailable")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=ARTIFACTS / f"performance-overview-{width}-{known}.png", full_page=True
    )
    resources.get_by_role("button", name="View resource history", exact=True).click()
    expect(page.get_by_role("tab", name="Resources", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )


@pytest.mark.parametrize("state", ["unconfigured", "available", "zero", "unavailable"])
def test_storage_measurements_distinguish_guest_backing_and_headroom(
    server, desktop, state
):
    _, page = desktop
    login(page, server.url)
    snapshot = page.request.get(server.url + "/api/v1/performance").json()
    gib = 1024**3
    snapshot["database"] = {
        "used_bytes": 0,
        "index_bytes": 0,
        "objects": 0,
        "volume_total_bytes": 0,
    }
    available = state in ("available", "zero")
    snapshot["server"]["disk"].update(
        available=True,
        total_bytes=1000 * gib,
        used_bytes=200 * gib,
        free_bytes=800 * gib,
        capacity={
            "filesystem": {
                "available": True,
                "total_bytes": 1000 * gib,
                "used_bytes": 200 * gib,
                "free_bytes": 800 * gib,
            },
            "backing": {
                "configured": state != "unconfigured",
                "available": available,
                "total_bytes": 100 * gib if available else None,
                "used_bytes": 60 * gib if available else None,
                "free_bytes": (40 * gib if state == "available" else 0)
                if available
                else None,
            },
            "effective": {
                "available": available,
                "free_bytes": (40 * gib if state == "available" else 0)
                if available
                else None,
                "headroom_bytes": (20 * gib if state == "available" else 0)
                if available
                else None,
            },
            "scope": "filesystem_only"
            if state == "unconfigured"
            else "backing_filesystem"
            if available
            else "backing_unavailable",
        },
    )
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    page.get_by_role("tab", name="Resources", exact=True).click()
    expect(
        page.get_by_role("progressbar", name="Data filesystem", exact=True)
    ).to_have_attribute("max", str(1000 * gib))
    database = page.locator(".performance-usage").filter(
        has=page.get_by_text("MongoDB", exact=True)
    )
    expect(database.locator("strong")).to_have_text("0 B")
    expect(database.get_by_role("progressbar")).to_have_count(0)
    readings = page.get_by_role("group", name="Storage capacity measurements")
    if state == "unconfigured":
        expect(readings).not_to_be_attached()
        page.get_by_text("Resource measurement details", exact=True).click()
        expect(
            page.get_by_text("Backing storage is not separately measured", exact=False)
        ).to_be_visible()
    else:
        expect(readings.get_by_role("definition")).to_have_text(
            ["40 GiB", "20 GiB"]
            if state == "available"
            else ["0 B", "0 B"]
            if state == "zero"
            else ["Unavailable", "Unavailable"]
        )
        if state == "unavailable":
            expect(readings.get_by_role("status")).to_contain_text(
                "New writes are unavailable"
            )
        else:
            expect(readings.get_by_role("status")).not_to_be_attached()
    management = page.request.get(server.url + "/api/v1/settings").json()
    management["stats"].update(
        free_disk_bytes=800 * gib,
        disk_capacity=snapshot["server"]["disk"]["capacity"],
    )
    page.route("**/api/v1/settings", lambda route: route.fulfill(json=management))
    page.goto(server.url + "/?view=storage")
    staging = page.get_by_role("region", name="Temporary Storage", exact=True)
    expect(staging).to_contain_text("800 GiB reported staging-filesystem free")
    if state == "unconfigured":
        expect(staging).not_to_contain_text("Backing filesystem free:")
    else:
        backing, after_reserve = (
            ("40 GiB", "20 GiB")
            if state == "available"
            else ("0 B", "0 B")
            if state == "zero"
            else ("Unavailable", "Unavailable")
        )
        expect(staging).to_contain_text("Backing filesystem free: " + backing)
        expect(staging).to_contain_text("Free after reserve: " + after_reserve)
        if state == "unavailable":
            expect(staging.get_by_role("status")).to_contain_text(
                "New writes are unavailable"
            )


def test_performance_explanation_without_backend_support(server, desktop):
    _, page = desktop
    login(page, server.url)
    snapshot = page.request.get(server.url + "/api/v1/performance").json()
    snapshot.pop("diagnosis", None)
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    explanation = page.get_by_role("region", name="Current activity", exact=True)
    expect(explanation).to_contain_text("Current information is unavailable")
    expect(page.get_by_role("link", name="View Transfers", exact=True)).to_be_visible()
    summary = explanation.locator("summary")
    summary.focus()
    page.keyboard.press("Enter")
    expect(
        explanation.get_by_text("Idle resources can be appropriate", exact=False)
    ).to_be_visible()
    expect(explanation).to_contain_text("Low throughput alone cannot locate")
    page.screenshot(
        path=ARTIFACTS / "performance-explanation-desktop.png",
        full_page=True,
        animations="disabled",
    )
    page.set_viewport_size({"width": 390, "height": 844})
    assert explanation.evaluate("node => node.scrollWidth <= node.clientWidth")
    assert explanation.evaluate(
        "node => node.getBoundingClientRect().right <= innerWidth"
    )
    explanation.screenshot(
        path=ARTIFACTS / "performance-explanation-unavailable.png",
        animations="disabled",
    )


@pytest.mark.parametrize("width", [1440, 390])
def test_performance_diagnosis_evidence_states_and_expiry(server, desktop, width):
    _, page = desktop
    login(page, server.url)
    snapshot = page.request.get(server.url + "/api/v1/performance").json()
    snapshot["traffic"]["at"] = 1003
    diagnosis = {
        "sampled_at": 1000,
        "window_seconds": 30,
        "observed_seconds": 12,
        "stale_after_seconds": 15,
        "state": "idle",
        "workload": {
            "playback_requests": 0,
            "download_requests": 0,
            "unknown_file_requests": 0,
            "upload_requests": 0,
        },
        "telegram_inbound_bytes_per_second": None,
        "http_delivered_bytes_per_second": 1024,
        "cache_hits": None,
        "cache_misses": 0,
        "constraints": [],
    }
    snapshot["diagnosis"] = diagnosis
    failing = False

    def reading(route):
        if failing:
            route.fulfill(status=503, json={"detail": "Synthetic sampling unavailable"})
        else:
            route.fulfill(json=snapshot)

    page.route("**/api/v1/performance", reading)
    page.set_viewport_size({"width": width, "height": 900})
    page.clock.install()
    page.goto(server.url + "/?view=performance")
    explanation = page.get_by_role("region", name="Current activity", exact=True)
    expect(explanation).to_contain_text("Nothing is transferring right now")
    diagnosis["state"] = "observing"
    diagnosis["workload"].update(playback_requests=1, download_requests=2)
    page.get_by_role("button", name="Refresh Performance", exact=True).click()
    expect(explanation).to_contain_text("Checking recent activity")
    diagnosis["state"] = "active"
    diagnosis["constraints"] = [
        {
            "code": "telegram_queue",
            "certainty": "observed",
            "evidence": [
                {
                    "name": "configured_operations_per_account",
                    "value": 3,
                    "unit": "operations",
                },
            ],
        },
        {
            "code": "telegram_cooldown",
            "certainty": "observed",
            "evidence": [
                {"name": "longest_cooldown_seconds", "value": 4, "unit": "seconds"},
            ],
        },
        {
            "code": "scratch_wait",
            "certainty": "observed",
            "evidence": [
                {"name": "reserved_bytes", "value": 1024, "unit": "bytes"},
            ],
        },
    ]
    page.get_by_role("button", name="Refresh Performance", exact=True).click()
    expect(explanation.locator(".performance-wait > summary")).to_have_count(3)
    measurements = explanation.locator("summary").filter(
        has_text="Measurements and scope"
    )
    measurements.click()
    expect(explanation).to_contain_text("12 s observed")
    expect(explanation).to_contain_text("30 s window")
    page.screenshot(
        path=ARTIFACTS / f"performance-overview-waits-default-{width}.png",
        full_page=True,
        animations="disabled",
    )
    evidence = explanation.locator(".performance-wait > summary")
    evidence.first.focus()
    page.keyboard.press("Enter")
    expect(explanation.get_by_text("3 operations", exact=True)).to_be_visible()
    expect(explanation.get_by_text("1 KiB/s", exact=True)).to_be_visible()
    expect(explanation).to_contain_text(
        "Telegram inbound - reporting accountsUnavailable"
    )
    assert explanation.evaluate("node => node.scrollWidth <= node.clientWidth")
    page.screenshot(
        path=ARTIFACTS / f"performance-diagnosis-{width}.png",
        full_page=True,
        animations="disabled",
    )
    # A failed refresh must not keep the retained sample's evidence current.
    failing = True
    page.clock.fast_forward(16000)
    expect(explanation).to_contain_text("Current information is unavailable")
    expect(explanation.get_by_text("1 KiB/s", exact=True)).to_have_count(0)
    expect(explanation.locator(".performance-wait > summary")).to_have_count(0)
    failing = False
    snapshot["traffic"]["at"] = diagnosis["sampled_at"] = 2000
    page.get_by_role("button", name="Refresh Performance", exact=True).click()
    expect(explanation.locator(".performance-wait > summary")).to_have_count(3)
    diagnosis["state"] = "unavailable"
    page.get_by_role("button", name="Refresh Performance", exact=True).click()
    expect(explanation).to_contain_text("Current information is unavailable")


def test_missing_request_history_does_not_imply_zero_errors(server, desktop):
    _, page = desktop
    login(page, server.url)
    snapshot = page.request.get(server.url + "/api/v1/performance").json()
    snapshot.pop("traffic", None)
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    page.get_by_role("tab", name="Accounts", exact=True).click()
    status = page.get_by_role("region", name="Account and request status", exact=True)
    expect(status).to_contain_text("recent request error history unavailable")
    expect(status).not_to_contain_text("no recent failed requests")
    expect(status).not_to_contain_text("Healthy")
    page.get_by_role("tab", name="Resources", exact=True).click()
    expect(
        page.get_by_role("heading", name="Server network", exact=True)
    ).to_be_visible()


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
    expect(page.get_by_text("Cache details & recent preparations")).to_have_count(0)
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
    feedback = page.get_by_text(
        "Cached bytes evicted. The original is unchanged.", exact=True
    )
    expect(feedback).to_have_count(1)
    expect(feedback).to_be_visible()
    assert cleared == [{"file_id": "partial-file"}]
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
            expect(brand.locator(".brand-logo-dark")).not_to_be_visible()
            expect(brand.locator(".brand-logo-icon")).to_be_visible()
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
    expect(page.get_by_text("No cache entries yet", exact=True)).to_be_visible()
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
    snapshot["traffic"]["at"] = now
    snapshot["diagnosis"] = {
        "sampled_at": now,
        "window_seconds": 30,
        "observed_seconds": 12,
        "stale_after_seconds": 15,
        "state": "active",
        "workload": {
            "playback_requests": 1,
            "download_requests": 0,
            "unknown_file_requests": 0,
            "upload_requests": 1,
        },
        "telegram_inbound_bytes_per_second": None,
        "http_delivered_bytes_per_second": None,
        "cache_hits": None,
        "cache_misses": None,
        "constraints": [],
    }
    snapshot["tuning"] = {
        "running": True,
        "values": {"cache_memory_bytes": 256 * 1024**2},
        "reasons": {"cache_memory_bytes": "Sized from free memory"},
    }
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=snapshot))
    page.goto(server.url + "/?view=performance")
    overview = page.get_by_role("tab", name="Overview", exact=True)
    expect(overview).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("tabpanel", name="Overview")).to_be_visible()
    expect(
        page.get_by_role("region", name="Automatic tuning", exact=True)
    ).not_to_be_visible()
    expect(page.get_by_role("link", name="View Transfers", exact=True)).to_be_visible()
    expect(page.get_by_text("3 MiB/s", exact=True)).to_be_visible()
    expect(page.get_by_text("1 MiB/s", exact=True)).to_be_visible()
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
        page.get_by_role("progressbar", name="Data filesystem", exact=True)
    ).to_be_visible()
    assert (
        page.get_by_role("heading", name="Storage & cache")
        .locator("xpath=ancestor::details")
        .count()
        == 0
    )
    tuning = page.get_by_role("region", name="Automatic tuning", exact=True)
    expect(tuning.get_by_text("Memory cache", exact=True)).not_to_be_visible()
    tuning.locator("summary").focus()
    page.keyboard.press("Enter")
    expect(tuning.get_by_text("Memory cache", exact=True)).to_be_visible()
    expect(tuning.get_by_role("definition")).to_have_text("256 MiB")
    expect(tuning).to_contain_text("Sized from free memory")
    page.screenshot(path=ARTIFACTS / "owner-performance-resources.png", full_page=True)
    page.get_by_role("tab", name="Overview", exact=True).click()
    page.screenshot(
        path=ARTIFACTS / "owner-performance-desktop.png",
        full_page=True,
        animations="disabled",
    )
    page.set_viewport_size({"width": 390, "height": 844})
    set_appearance(page, "dark")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=ARTIFACTS / "owner-performance-mobile-dark.png",
        full_page=True,
        animations="disabled",
    )
    for tab in ["Accounts", "Resources"]:
        page.get_by_role("tab", name=tab, exact=True).click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(
            path=ARTIFACTS / f"owner-performance-{tab.lower()}-mobile.png",
            full_page=True,
        )
    page.goto(server.url + "/?view=settings&section=cache")
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
    preload = page.get_by_role("switch", name=re.compile(r"^Preload during playback"))
    expect(preload).to_be_checked()
    page.get_by_text("Preload during playback", exact=True).click()
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
    page.get_by_role("tab", name="Resources", exact=True).click()
    network = page.get_by_role("region", name="Server network", exact=True)
    expect(network).to_contain_text("12 MiB/s")
    expect(network).to_contain_text(
        "All traffic on the reported network interface; not Telegram alone"
    )
    expect(
        page.get_by_role("img", name="Server network over time", exact=True)
    ).to_be_visible()
    expect(network.locator("polyline.performance-plot-active")).to_have_count(2)
    plot = network.locator(".performance-chart-plot")
    network.get_by_role("img").scroll_into_view_if_needed()
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
    # Narrow charts flip the tooltip to the pointer's left to stay contained.
    assert max(positions) - min(positions) > 10
    expect(network.locator("svg title")).to_have_count(0)

    expect(page.get_by_role("region", name="Container RAM", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Overview", exact=True).click()
    expect(page.locator(".performance-file-rates dd")).to_have_text(["0 B/s", "0 B/s"])

    def record_overview_style(label):
        activity = page.get_by_role("region", name="Current activity", exact=True)
        expect(activity).to_have_css("opacity", "1")
        (ARTIFACTS / f"performance-overview-style-{label}.json").write_text(
            activity.evaluate("""node => JSON.stringify({
              opacity: getComputedStyle(node).opacity,
              panelOpacity: getComputedStyle(node.closest('[role=tabpanel]')).opacity,
              headingColor: getComputedStyle(node.querySelector('h3')).color,
              summaryColor: getComputedStyle(node.querySelector('[role=status]')).color,
              animationName: getComputedStyle(node).animationName,
              theme: document.documentElement.dataset.theme,
              drawerExpanded: document.querySelector('#navigation-toggle')
                ?.getAttribute('aria-expanded'),
              drawerBounds: document.querySelector('#drive-navigation')
                ?.getBoundingClientRect()
            }, null, 2)""")
        )

    page.set_viewport_size({"width": 1440, "height": 1100})
    page.get_by_role(
        "heading", name="Performance", exact=True
    ).scroll_into_view_if_needed()
    page.mouse.move(0, 0)
    record_overview_style("desktop")
    page.screenshot(
        path=ARTIFACTS / "performance-overview-default-desktop.png",
        full_page=True,
        animations="disabled",
    )
    page.set_viewport_size({"width": 390, "height": 844})
    set_appearance(page, "dark")
    toggle = page.get_by_role("button", name="Toggle Navigation", exact=True)
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    page.wait_for_function(
        "() => document.querySelector('#drive-navigation')"
        ".getBoundingClientRect().right <= 1"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    record_overview_style("mobile-dark")
    page.screenshot(
        path=ARTIFACTS / "performance-overview-default-mobile-dark.png",
        full_page=True,
        animations="disabled",
    )
    page.set_viewport_size({"width": 1440, "height": 1100})
    set_appearance(page, "light")
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
    expect(ram.locator(".performance-chart-heading")).to_contain_text("2 GiB / 8 GiB")
    expect(ram.locator(".performance-chart-axis")).to_contain_text("8 GiB")
    page.set_viewport_size({"width": 1440, "height": 1100})
    page.get_by_role(
        "heading", name="Performance", exact=True
    ).scroll_into_view_if_needed()
    page.screenshot(
        path=ARTIFACTS / "owner-performance-network-container.png", full_page=False
    )
    snapshot["server"]["memory"]["cgroup_limit"] = None
    page.get_by_role("button", name="Refresh Performance", exact=True).click()
    expect(ram.locator(".performance-chart-heading")).to_contain_text(
        "limit unavailable"
    )
    expect(ram).to_contain_text("No container memory limit reported.")
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
        page.get_by_role("img", name="Server network over time", exact=True)
    ).to_have_count(0)
    page.get_by_role("tab", name="Overview", exact=True).click()
    expect(network).not_to_be_visible()
    page.get_by_role("tab", name="Resources", exact=True).click()
    expect(network.locator(".performance-chart-heading")).to_contain_text("12 MiB/s")
    expect(network.locator(".performance-chart-collecting")).to_be_visible()
