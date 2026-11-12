"""Disk retention and the separate temporary stream ceiling."""

# ruff: noqa: F811 - shared browser fixtures use imported names.
from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.e2e
if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

from tests.browser.test_browser_e2e import (  # noqa: E402
    ARTIFACTS,
    browser,  # noqa: F401 - shared fixture
    desktop,  # noqa: F401 - shared fixture
    expect,
    login,
    server,  # noqa: F401 - shared fixture
)


@pytest.mark.parametrize(
    "width,theme", [(1920, "light"), (1440, "light"), (390, "dark")]
)
def test_cache_retention_and_stream_ceiling_review_save_reload_and_defaults(
    server, desktop, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 960 if width > 600 else 844})
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance','{theme}')")
    login(page, server.url)
    saved = page.request.get(server.url + "/api/v1/settings/performance").json()
    saved.update(cache_ttl_seconds=86400, stream_memory_limit_bytes=None)
    saved["defaults"] = {
        **saved,
        "cache_ttl_seconds": 86400,
        "stream_memory_limit_bytes": None,
    }
    saved["auto_resource_limits"] = {
        **saved.get("auto_resource_limits", {}),
        "stream_memory_limit_bytes": 256 * 1024**2,
    }
    writes = []

    def settings(route):
        nonlocal saved
        if route.request.method == "PATCH":
            assert route.request.headers["if-match"] == saved["revision"]
            payload = route.request.post_data_json
            expected = [
                {
                    "cache_ttl_seconds": 172800,
                    "stream_memory_limit_bytes": 16 * 1024**2,
                },
                {"cache_ttl_seconds": 86400, "stream_memory_limit_bytes": None},
            ]
            assert len(writes) < len(expected)
            assert payload == expected[len(writes)]
            writes.append(payload)
            saved = {**saved, **payload, "revision": '"saved-cache-controls"'}
        route.fulfill(json=saved)

    page.route("**/api/v1/settings/performance", settings)
    page.goto(server.url + "/?view=settings&section=cache")
    retention = page.get_by_role("textbox", name="Disk cache retention", exact=True)
    units = page.get_by_role("combobox", name="Disk cache retention unit", exact=True)
    stream = page.get_by_role("group", name="Stream working memory", exact=True)
    mode = stream.get_by_role("combobox", name="Stream working memory mode", exact=True)
    save = page.get_by_role("button", name="Save Performance Settings", exact=True)
    expect(retention).to_have_value("24")
    expect(stream).to_contain_text("Automatic ceiling: 256 MiB")
    expect(mode.locator("option")).to_have_count(2)
    workspace = page.locator(".workspace")
    page.evaluate("document.fonts.ready")
    workspace.evaluate("n=>n.scrollTo({top:0,behavior:'instant'})")
    page.screenshot(
        path=ARTIFACTS / f"cache-controls-initial-{width}-{theme}.png",
        animations="disabled",
    )
    units.select_option("days")
    expect(retention).to_have_value("1")
    expect(save).to_be_disabled()
    retention.fill("2")
    mode.select_option("custom")
    amount = stream.get_by_role(
        "textbox", name="Stream working memory (MiB)", exact=True
    )
    amount.fill("16")
    save.click()
    expect(save).to_be_disabled()
    assert writes == [
        {"cache_ttl_seconds": 172800, "stream_memory_limit_bytes": 16 * 1024**2}
    ]
    expect(page.locator(".performance-notice")).to_have_text("Cache settings saved.")

    page.reload()
    expect(retention).to_have_value("48")
    expect(amount).to_have_value("16 MiB")
    amount.fill("32")
    page.get_by_role("button", name="Discard changes", exact=True).click()
    expect(amount).to_have_value("16 MiB")
    page.get_by_role("button", name="Restore defaults", exact=True).click()
    expect(retention).to_have_value("24")
    expect(mode).to_have_value("automatic")
    expect(save).to_be_enabled()
    assert len(writes) == 1
    save.click()
    expect(save).to_be_disabled()
    assert writes[-1] == {"cache_ttl_seconds": 86400, "stream_memory_limit_bytes": None}
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(
        path=ARTIFACTS / f"cache-controls-{width}-{theme}.png", animations="disabled"
    )
    stream.scroll_into_view_if_needed()
    page.screenshot(
        path=ARTIFACTS / f"cache-stream-controls-{width}-{theme}.png",
        animations="disabled",
    )
    stream.get_by_role("button", name="About Stream working memory", exact=True).click()
    help_dialog = page.get_by_role(
        "dialog", name="About Stream working memory", exact=True
    )
    expect(help_dialog).to_contain_text("total process memory")
    expect(help_dialog).to_contain_text("Unknown memory does not cause growth")


def test_unchanged_retention_seconds_survive_an_unrelated_cache_save(server, desktop):
    _, page = desktop
    login(page, server.url)
    saved = page.request.get(server.url + "/api/v1/settings/performance").json()
    saved.update(cache_ttl_seconds=86401, stream_memory_limit_bytes=None)
    writes = []

    def settings(route):
        if route.request.method == "PATCH":
            assert route.request.headers["if-match"] == saved["revision"]
            payload = route.request.post_data_json
            assert not writes
            assert payload == {"auto_preload": False}
            writes.append(payload)
            saved.update(payload)
        route.fulfill(json=saved)

    page.route("**/api/v1/settings/performance", settings)
    page.goto(server.url + "/?view=settings&section=cache")
    page.get_by_role(
        "combobox", name="Disk cache retention unit", exact=True
    ).select_option("days")
    switch = page.get_by_role("switch", name=re.compile(r"^Preload during playback"))
    switch.locator("xpath=ancestor::label").click()
    page.get_by_role("button", name="Save Performance Settings", exact=True).click()
    expect(
        page.get_by_role("button", name="Save Performance Settings")
    ).to_be_disabled()
    assert writes == [{"auto_preload": False}]
    assert saved["cache_ttl_seconds"] == 86401
