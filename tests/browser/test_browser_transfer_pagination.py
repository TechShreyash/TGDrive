"""Bounded terminal task pages alongside current live uploads."""
# ruff: noqa: F811 - pytest fixture parameters shadow imported fixture functions.

from __future__ import annotations

import os

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


def test_finished_history_cursor_pages_keep_live_sections_and_reset_filters(
    server,
    desktop,
):
    _, page = desktop

    def job(identity, status, at):
        return {
            "id": identity,
            "name": identity + ".bin",
            "kind": "upload",
            "status": status,
            "phase": "uploading" if status == "running" else status,
            "current": 40 if status == "running" else 100,
            "total": 100,
            "created_at": at,
            "finished_at": at + 1
            if status in {"completed", "error", "cancelled"}
            else None,
            "error": "Provider rejected the upload" if status == "error" else None,
        }

    active = [job("live-running", "running", 1000), job("live-queued", "queued", 999)]
    records = [
        job("recent-success", "completed", 100),
        job("recent-failure", "error", 99),
        *[job(f"recent-error-{at}", "error", at) for at in range(98, 80, -1)],
        job("older-cancelled", "cancelled", 80),
        job("older-success", "completed", 79),
        *[job(f"older-error-{at}", "error", at) for at in range(78, 60, -1)],
        job("oldest-failure", "error", 60),
    ]
    page.route(
        "**/api/v1/jobs",
        lambda route: route.fulfill(json={"jobs": active + records[:2]}),
    )
    seen = []
    fail_once = [True]

    def history(route):
        query = route.request.post_data_json
        seen.append(query)
        status = query["status"]
        if status == "error" and fail_once:
            fail_once.pop()
            route.fulfill(
                status=503, json={"detail": "History temporarily unavailable"}
            )
            return
        selected = (
            records
            if status == "finished"
            else []
            if status in {"error", "cancelled"}
            else [record for record in records if record["status"] == status]
        )
        offset = query["offset"]
        batch = selected[offset : offset + 20]
        route.fulfill(
            json={
                "jobs": batch,
                "page": query["page"],
                "page_size": 20,
                "total_tasks": len(selected),
                "total_pages": max(1, (len(selected) + 19) // 20),
                "has_more": offset + 20 < len(selected),
            }
        )

    page.route("**/api/v1/transfers/history/tasks", history)
    login(page, server.url)
    page.goto(server.url + "/?view=activity")
    history_region = page.get_by_role("region", name="Transfer History", exact=True)
    finished = history_region.locator('[data-category="finished"]')
    pager = history_region.get_by_role("navigation", name="Finished transfer pages")
    expect(finished).to_contain_text("recent-success.bin")
    expect(finished).to_contain_text("recent-failure.bin")
    expect(finished.locator(".transfer-item-name")).to_have_text(
        [record["name"] for record in records[:20]]
    )
    assert history_region.locator(".transfer-category").evaluate_all(
        "rows => rows.map(row => row.dataset.category)"
    ) == ["running", "queued", "finished"]
    previous = pager.get_by_role(
        "button", name="Previous page of finished transfers", exact=True
    )
    next_page = pager.get_by_role(
        "button", name="Next page of finished transfers", exact=True
    )
    expect(previous).to_be_disabled()
    next_page.click()
    expect(pager.get_by_role("status")).to_contain_text("Page 2 of 3")
    expect(finished).to_contain_text("older-cancelled.bin")
    expect(finished).not_to_contain_text("recent-success.bin")
    expect(finished).not_to_contain_text("recent-failure.bin")
    expect(history_region.locator('[data-category="running"]')).to_contain_text(
        "live-running.bin"
    )
    expect(history_region.locator('[data-category="queued"]')).to_contain_text(
        "live-queued.bin"
    )
    next_page.click()
    expect(pager.get_by_role("status")).to_contain_text("Page 3 of 3")
    expect(finished).to_contain_text("oldest-failure.bin")
    expect(next_page).to_be_disabled()
    previous.click()
    expect(pager.get_by_role("status")).to_contain_text("Page 2 of 3")
    expect(finished).to_contain_text("older-success.bin")
    page.screenshot(path=ARTIFACTS / "transfer-pagination-desktop.png", full_page=True)
    filters = history_region.get_by_role("group", name="Filter Finished Transfers")
    filters.get_by_role("button", name="Successful", exact=True).click()
    expect(finished).to_contain_text("recent-success.bin")
    expect(finished).not_to_contain_text("recent-failure.bin")
    expect(pager.get_by_role("status")).to_have_text("Page 1 of 1 · 2 tasks")
    filters.get_by_role("button", name="Failed", exact=True).click()
    expect(history_region.get_by_role("alert")).to_contain_text(
        "History temporarily unavailable"
    )
    history_region.get_by_role("button", name="Try Again", exact=True).click()
    expect(history_region.get_by_role("alert")).to_have_count(0)
    expect(finished.get_by_role("heading", name="No Failed Transfers")).to_be_visible()
    filters.get_by_role("button", name="Cancelled", exact=True).click()
    expect(
        finished.get_by_role("heading", name="No Cancelled Transfers")
    ).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(pager).to_have_count(0)
    page.screenshot(
        path=ARTIFACTS / "transfer-pagination-empty-mobile.png", full_page=True
    )
    filters.get_by_role("button", name="All", exact=True).click()
    next_page.click()
    expect(pager.get_by_role("status")).to_contain_text("Page 2 of 3")
    expect(finished).to_contain_text("older-cancelled.bin")
    pager.scroll_into_view_if_needed()
    page.screenshot(path=ARTIFACTS / "transfer-pagination-mobile.png", full_page=True)
    assert all(query["limit"] == 20 for query in seen)
    assert any(query["offset"] == 20 for query in seen)
    assert any(query["offset"] == 40 for query in seen)
