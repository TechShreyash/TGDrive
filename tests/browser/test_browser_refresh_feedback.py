"""Explicit refresh feedback waits for fresh data; automatic polling stays quiet."""

from __future__ import annotations

import os
import re
import time
from urllib.parse import urlsplit

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium tests", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import expect, login

browser = fixtures.browser
desktop = fixtures.desktop
server = fixtures.server
pytestmark = pytest.mark.e2e


def response_path(response, path, method="GET"):
    return urlsplit(response.url).path == path and response.request.method == method


def open_page(page, server, view, initial_path):
    login(page, server.url)
    with page.expect_response(lambda response: response_path(response, initial_path)):
        page.goto(server.url + "/?view=" + view)
    expect(page.locator(".toast")).to_have_count(0)


def wait_for_request(page, held):
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    assert held, "The explicit refresh did not request fresh data"


def assert_pending(page, button, held):
    wait_for_request(page, held)
    expect(button).to_be_disabled()
    expect(button.locator(".spinner")).to_have_count(1)
    expect(page.locator(".toast:not(.toast-error)")).to_have_count(0)
    # Give both click events and a new render time to settle, with the actual
    # response still held. This detects duplicate origin requests without
    # relying on how the component stores its pending state.
    page.wait_for_timeout(100)
    assert len(held) == 1


@pytest.mark.parametrize(
    "view,initial_path,endpoint,method,button_name,button_title",
    [
        (
            "preloads",
            "/api/v1/preloads",
            "/api/v1/preloads",
            "GET",
            "Refresh Cache Inventory",
            None,
        ),
        (
            "performance",
            "/api/v1/performance",
            "/api/v1/performance",
            "GET",
            "Refresh Performance",
            None,
        ),
        (
            "shares",
            "/api/v1/shares",
            "/api/v1/shares",
            "GET",
            "Refresh Shared Links",
            None,
        ),
        ("storage", "/api/v1/settings", "/api/v1/settings", "GET", "Check Again", None),
        (
            "connections",
            "/api/v1/settings",
            "/api/v1/settings/refresh",
            "POST",
            None,
            "Check saved credentials and storage channel access",
        ),
        (
            "storage",
            "/api/v1/replica",
            "/api/v1/replica",
            "GET",
            "Refresh channel backup status",
            None,
        ),
    ],
    ids=[
        "preloads",
        "performance",
        "shares",
        "storage",
        "connections",
        "channel-backup",
    ],
)
def test_manual_refresh_disables_and_confirms_only_after_success(
    server, desktop, view, initial_path, endpoint, method, button_name, button_title
):
    _, page = desktop
    open_page(page, server, view, initial_path)
    button = (
        page.get_by_title(button_title, exact=True)
        if button_title
        else page.get_by_role("button", name=button_name, exact=True)
    )
    expect(button).to_be_enabled()
    payload_path = "/api/v1/settings" if method == "POST" else endpoint
    payload = page.request.get(server.url + payload_path).json()
    held = []

    def hold(route):
        if route.request.method != method:
            route.continue_()
        else:
            held.append(route)

    page.route("**" + endpoint, hold)
    button.dblclick()
    assert_pending(page, button, held)
    held[0].fulfill(json=payload)
    expect(button).to_be_enabled()
    expect(page.locator(".toast:not(.toast-error)")).to_contain_text(
        re.compile(r"refreshed|checks requested", re.I)
    )


def test_performance_refresh_waits_for_history_as_well_as_snapshot(server, desktop):
    _, page = desktop
    open_page(page, server, "performance", "/api/v1/performance/history")
    button = page.get_by_role("button", name="Refresh Performance", exact=True)
    expect(button).to_be_enabled()
    held = []

    def hold_history(route):
        held.append((route, route.fetch().json()))

    page.route("**/api/v1/performance/history?*", hold_history)
    with page.expect_response(
        lambda response: response_path(response, "/api/v1/performance")
    ):
        button.dblclick()
    wait_for_request(page, held)
    expect(button).to_be_disabled()
    expect(page.locator(".toast:not(.toast-error)")).to_have_count(0)
    assert len(held) == 1
    route, payload = held[0]
    route.fulfill(json=payload)
    expect(button).to_be_enabled()
    expect(page.locator(".toast:not(.toast-error)")).to_have_text(
        re.compile(r"Performance refreshed\.")
    )


def test_failed_refresh_has_no_success_and_retry_then_polling_stays_quiet(
    server, desktop
):
    _, page = desktop
    open_page(page, server, "preloads", "/api/v1/preloads")
    button = page.get_by_role("button", name="Refresh Cache Inventory", exact=True)
    expect(button).to_be_enabled()
    payload = page.request.get(server.url + "/api/v1/preloads").json()
    held = []
    page.route("**/api/v1/preloads", lambda route: held.append(route))
    button.dblclick()
    assert_pending(page, button, held)
    held[0].fulfill(status=503, json={"detail": "Refresh is temporarily unavailable."})
    expect(button).to_be_enabled()
    expect(page.locator(".toast.toast-error")).to_contain_text(
        "Refresh is temporarily unavailable."
    )
    expect(page.locator(".toast:not(.toast-error)")).to_have_count(0)
    page.get_by_role("button", name="Dismiss Notification").click()
    held.clear()
    button.dblclick()
    assert_pending(page, button, held)
    held[0].fulfill(json=payload)
    expect(button).to_be_enabled()
    expect(page.locator(".toast:not(.toast-error)")).to_contain_text(
        "Cache inventory refreshed."
    )
    expect(page.locator(".toast.toast-error")).to_have_count(0)
    page.get_by_role("button", name="Dismiss Notification").click()
    page.unroute("**/api/v1/preloads")
    with page.expect_response(
        lambda response: response_path(response, "/api/v1/preloads"), timeout=8000
    ):
        pass
    expect(page.locator(".toast")).to_have_count(0)
