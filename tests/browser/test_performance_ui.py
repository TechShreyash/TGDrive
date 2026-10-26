"""Owner controls use real React pages and explicit HTTP boundary fixtures."""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.test_browser_e2e import ARTIFACTS, Server, expect, login
from tests.browser.test_browser_e2e import browser as browser_fixture
from tests.browser.test_browser_e2e import desktop as desktop_fixture
from tests.browser.test_browser_e2e import server as server_fixture

pytestmark = pytest.mark.e2e
browser, desktop, server = browser_fixture, desktop_fixture, server_fixture
MIB, GIB = 1024**2, 1024**3


def preferences():
    return {
        "revision": '"first-revision"',
        "timezone": "Asia/Kolkata",
        "cache_memory_bytes": 256 * MIB,
        "cache_disk_bytes": 10 * GIB,
        "cache_min_free_bytes": GIB,
        "cache_ttl_seconds": 86400,
        "auto_preload": True,
        "preload_max_jobs": 2,
        "telegram_upload_pipeline": 4,
        "telegram_request_limit": 8,
        "telegram_parts_per_second": 24,
        "telegram_media_per_bot": 3,
        "telegram_max_queue": 256,
        "upload_split_mib": 2000,
    }


def cache_snapshot():
    return {
        "memory": {
            "bytes": 20 * MIB,
            "max_bytes": 256 * MIB,
            "effective_max_bytes": 128 * MIB,
            "hits": 120,
            "misses": 25,
        },
        "disk": {
            "available": True,
            "bytes": 100 * MIB,
            "max_bytes": 10 * GIB,
            "free_bytes": 20 * GIB,
            "min_free_bytes": GIB,
        },
        "pending_writes": 0,
        "files": [
            {
                "id": "movie-cache",
                "name": "A long shared movie filename.mkv",
                "size": 100 * MIB,
                "ready_bytes": 20 * MIB,
                "complete": False,
                "ready_ranges": [[0, 10 * MIB], [90 * MIB, 100 * MIB]],
            },
            {
                "id": "complete-cache",
                "name": "Complete movie.mkv",
                "size": 80 * MIB,
                "ready_bytes": 80 * MIB,
                "complete": True,
                "ready_ranges": [[0, 80 * MIB]],
            },
        ],
    }


def test_settings_save_converts_units_guards_revision_and_recovers_conflict(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    saved = preferences()
    patches = []
    reject = False

    def settings(route):
        if route.request.method == "PATCH":
            patches.append(route.request.post_data_json)
            assert route.request.headers["if-match"] == saved["revision"]
            if reject:
                route.fulfill(
                    status=409,
                    json={"detail": "Changed in another tab. Reload before saving."},
                )
                return
            saved.update(route.request.post_data_json)
            saved["revision"] = '"second-revision"'
        route.fulfill(json=saved)

    page.route("**/api/v1/settings/performance", settings)
    login(page, server.url + "/?view=settings&section=cache")
    section = page.locator(".performance-settings")
    expect(section.get_by_label("Memory cache (MiB)", exact=True)).to_have_value("256")
    section.get_by_label("Memory cache (MiB)", exact=True).fill("512")
    section.get_by_label("Disk cache (GiB)", exact=True).fill("1.5")
    expect(
        section.locator("#perf-help-cache_memory_bytes")
        .locator("xpath=ancestor::*[contains(@class, 'performance-field')]")
        .locator(".mantine-Badge-root")
    ).to_have_count(0)
    expect(
        section.locator("#perf-help-cache_memory_bytes").locator(
            "xpath=ancestor::*[contains(@class, 'performance-field')]"
        )
    ).to_contain_text("reduces this budget when memory is tight")
    expect(section.locator("#perf-help-cache_min_free_bytes")).to_contain_text(
        "Keep 1 GiB or more for normal use"
    )
    section.get_by_label("Disk cache retention (hours)", exact=True).fill("2")
    page.get_by_role("link", name="Timezone", exact=True).click()
    section.get_by_label("Timezone", exact=True).fill("UTC")
    page.get_by_role("link", name="Cache settings", exact=True).click()
    section.get_by_role("button", name="Save Performance Settings", exact=True).click()
    expect(page.locator(".toast")).to_have_text("Performance settings saved.")
    assert patches == [
        {
            "cache_memory_bytes": 512 * MIB,
            "cache_disk_bytes": 1.5 * GIB,
            "cache_ttl_seconds": 7200,
        }
    ]
    page.get_by_role("link", name="Timezone", exact=True).click()
    expect(section.get_by_label("Timezone", exact=True)).to_have_value("UTC")
    section.get_by_role("button", name="Save Performance Settings", exact=True).click()
    expect(page.locator(".toast")).to_have_text("Performance settings saved.")
    assert patches[-1] == {"timezone": "UTC"}
    page.get_by_role("link", name="Cache settings", exact=True).click()
    section.scroll_into_view_if_needed()
    page.screenshot(path=ARTIFACTS / "performance-settings-desktop.png")
    page.set_viewport_size({"width": 390, "height": 844})
    section.get_by_label("Disk cache (GiB)", exact=True).scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=ARTIFACTS / "performance-settings-mobile.png")
    reject = True
    section.get_by_label("Disk cache (GiB)", exact=True).fill("2")
    section.get_by_role("button", name="Save Performance Settings", exact=True).click()
    expect(section.get_by_role("alert")).to_contain_text("another tab")
    expect(
        section.get_by_role("button", name="Save Performance Settings", exact=True)
    ).to_be_disabled()
    section.get_by_role("button", name="Reload Latest Settings", exact=True).click()
    expect(section.get_by_label("Disk cache (GiB)", exact=True)).to_have_value("1.5")
    expect(
        section.locator("#perf-help-cache_disk_bytes")
        .locator("xpath=ancestor::*[contains(@class, 'performance-field')]")
        .locator(".mantine-Badge-root")
    ).to_have_count(0)
    page.set_viewport_size({"width": 1440, "height": 960})
    page.get_by_role("link", name="Transfer limits", exact=True).click()
    section.locator("summary").filter(has_text="Advanced Telegram limits").click()
    expect(section.get_by_label("Requests per second", exact=True)).to_have_value("24")
    expect(
        section.locator("#perf-help-telegram_parts_per_second")
        .locator("xpath=ancestor::*[contains(@class, 'performance-field')]")
        .locator(".mantine-Badge-root")
    ).to_have_count(0)


def test_preload_inventory_cancel_and_scoped_eviction(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    data = {
        "active_jobs": 1,
        "max_background_jobs": 2,
        "cache": cache_snapshot(),
        "jobs": [
            {
                "id": "preparing-job",
                "file_id": "movie-cache",
                "name": "Preparing a movie.mkv",
                "state": "preparing",
                "mode": "manual",
                "bytes_done": 20 * MIB,
                "target_bytes": 100 * MIB,
                "size": 100 * MIB,
                "start": 0,
                "end": 100 * MIB,
            }
        ],
    }
    actions = []

    def handler(route):
        path = route.request.url.split("/api/v1/preloads", 1)[1]
        if route.request.method == "POST":
            actions.append((path, route.request.post_data_json))
            if path.endswith("/cancel"):
                data["jobs"][0]["state"] = "cancelled"
                data["active_jobs"] = 0
            else:
                file_id = route.request.post_data_json.get("file_id")
                data["cache"]["files"] = [
                    item
                    for item in data["cache"]["files"]
                    if file_id and item["id"] != file_id
                ]
            route.fulfill(json={"status": "ok"})
        else:
            route.fulfill(json=data)

    page.route("**/api/v1/preloads**", handler)
    login(page, server.url + "/?view=preloads")
    page.get_by_role(
        "button",
        name="Cancel preload of A long shared movie filename.mkv",
        exact=True,
    ).click()
    expect(
        page.get_by_role("status").filter(has_text="Preload cancelled").first
    ).to_be_visible()
    page.screenshot(path=ARTIFACTS / "preloads-desktop.png")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=ARTIFACTS / "preloads-mobile.png")
    page.get_by_role(
        "button",
        name="Evict cached bytes for A long shared movie filename.mkv",
        exact=True,
    ).click()
    page.get_by_role("button", name="Clear Cached File", exact=True).click()
    expect(
        page.get_by_text("A long shared movie filename.mkv", exact=True)
    ).to_have_count(0)
    page.get_by_role("button", name="Clear Cache", exact=True).click()
    page.get_by_role("button", name="Clear All Cache", exact=True).click()
    expect(page.get_by_text("No files cached yet", exact=True)).to_be_visible()
    assert actions == [
        ("/preparing-job/cancel", {}),
        ("/clear", {"file_id": "movie-cache"}),
        ("/clear", {}),
    ]


def test_performance_real_samples_nullable_eta_and_mobile_tables(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    value = {
        "telegram": {
            "active": 2,
            "queued": 1,
            "max_queue": 256,
            "mean_activity_seconds": None,
            "estimates": True,
            "accounts": [
                {
                    "name": "Storage account with a deliberately long label",
                    "kind": "bot",
                    "uploads": 1,
                    "downloads": 1,
                    "combined_limit": 3,
                    "cooldown_seconds": 0,
                    "requests": None,
                }
            ],
            "queue": [
                {
                    "position": 1,
                    "priority": "demand",
                    "operation": "download",
                    "waiting_seconds": 2,
                    "estimated_wait_seconds": None,
                }
            ],
            "history": [
                {"at": 100, "active": 1, "queued": 0},
                {"at": 103, "active": 2, "queued": 1},
            ],
        },
        "cache": cache_snapshot(),
        "drive": {"files": 25, "folders": 3, "total_bytes": GIB},
        "server": {
            "at": 103,
            "memory": {"MemAvailable": 2 * GIB, "MemTotal": 4 * GIB},
            "process_peak_rss_bytes": 150 * MIB,
            "load_average": [0.1, 0.2, 0.3],
            "disk": {
                "free_bytes": 20 * GIB,
                "total_bytes": 40 * GIB,
                "used_bytes": 20 * GIB,
            },
            "staging_budget_bytes": 16 * GIB,
        },
    }
    value["traffic"] = {
        "at": 106,
        "requests": 5,
        "active": 1,
        "errors": 0,
        "started_at": 90,
        "download_bytes": 30 * MIB,
        "upload_bytes": 6 * MIB,
        "history": [
            {
                "at": at,
                "requests": 5,
                "active": 1,
                "errors": 0,
                "download_bytes": download * MIB,
                "upload_bytes": upload * MIB,
            }
            for at, download, upload in [(100, 0, 0), (103, 15, 3), (106, 30, 6)]
        ],
    }
    value["telegram"]["accounts"][0]["requests"] = {
        "active": 2,
        "queued": 1,
        "configured_limit": 8,
        "effective_limit": 4,
        "configured_parts_per_second": 24,
        "effective_parts_per_second": 12,
        "floods": 1,
        "failed": 1,
        "completed": 30,
        "bytes": 3 * MIB,
        "upload_bytes": MIB,
        "download_bytes": 2 * MIB,
    }
    page.route("**/api/v1/performance", lambda route: route.fulfill(json=value))
    login(page, server.url + "/?view=performance")
    expect(
        page.get_by_role("heading", name="Live file transfers", exact=True)
    ).to_be_visible()
    page.screenshot(path=ARTIFACTS / "performance-load-desktop.png")
    page.get_by_role("tab", name="Accounts", exact=True).click()
    expect(
        page.get_by_role("img", name="Recent Telegram load", exact=False)
    ).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.get_by_role("tab", name="Accounts", exact=True).click()
    expect(page.get_by_text("Estimating…", exact=True)).to_be_visible()
    page.get_by_role(
        "heading", name="Account capacity", exact=True
    ).scroll_into_view_if_needed()
    page.screenshot(path=ARTIFACTS / "performance-load-mobile.png")
    assert page.locator(".performance-table-wrap").first.evaluate(
        "el=>el.scrollWidth>el.clientWidth"
    )


def test_copy_download_link_is_explicit_public_capability_and_prepare_is_scoped(
    server: Server, desktop: tuple[Any, Any], browser: Any
) -> None:
    context, page = desktop
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_label("Actions for sample.json", exact=True).click()
    page.get_by_role("menuitem", name="Copy Download Link", exact=True).click()
    dialog = page.get_by_role("dialog", name="Copy Download Link", exact=True)
    expect(dialog).to_contain_text("Anyone with this link")
    with page.expect_response("**/api/v1/download-links") as response:
        dialog.get_by_role("button", name="Create or Copy Link", exact=True).click()
    link = response.value.json()
    expect(dialog.get_by_label("Download link", exact=True)).to_have_value(link["url"])
    assert "auth=" in link["url"]
    assert page.evaluate("navigator.clipboard.readText()") == link["url"]
    zone = (
        page.request.get(server.url + "/api/config")
        .json()
        .get("timezone", "Asia/Kolkata")
    )
    expiry = page.evaluate(
        """({value, zone})=>new Intl.DateTimeFormat(undefined,{
      dateStyle:'medium',timeStyle:'long',timeZone:zone
    }).format(new Date(value))""",
        {"value": link["expires_at"], "zone": zone},
    )
    expect(dialog).to_contain_text("Expires " + expiry)
    public = browser.new_context()
    try:
        download = public.request.get(link["url"])
        assert download.status == 200
        assert b'"count":3' in download.body()
    finally:
        public.close()
    dialog.get_by_role("button", name="Close", exact=True).click()
    prepared = []

    def prepare(route):
        prepared.append(route.request.post_data_json)
        route.fulfill(status=202, json={"id": "prepared", "state": "queued"})

    page.route("**/api/v1/preloads", prepare)
    page.get_by_label("Actions for sample.json", exact=True).click()
    page.get_by_role("menuitem", name="Preload file", exact=True).click()
    dialog = page.get_by_role("dialog", name="Preload file", exact=True)
    expect(dialog).to_contain_text("Cached bytes may be removed when space is needed.")
    dialog.get_by_role("button", name="Preload file", exact=True).click()
    expect(dialog).not_to_be_visible()
    assert prepared == [{"path": link["path"]}]
