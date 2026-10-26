"""Phone activation, selection geometry and reachable controls in real Chromium.

Uses the isolated offline browser fixture. To check a running Vite build, set
TGDRIVE_MOBILE_UI_URL=http://127.0.0.1:5173; API traffic still uses that fixture.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, expect, login

browser, server = fixtures.browser, fixtures.server
pytestmark = pytest.mark.e2e


def open_fixture(page, server, suffix=""):
    url = os.environ.get("TGDRIVE_MOBILE_UI_URL", server.url)
    if url != server.url:

        def forward(route):
            requested = urlsplit(route.request.url)
            headers = dict(route.request.headers)
            if "origin" in headers:
                headers["origin"] = server.url
            if "referer" in headers:
                headers["referer"] = server.url + "/"
            response = route.fetch(
                url=server.url
                + requested.path
                + ("?" + requested.query if requested.query else ""),
                headers=headers,
            )
            route.fulfill(response=response)

        page.route("**/api/**", forward)
        page.route("**/file?*", forward)
    login(page, url + suffix)
    return url


def assert_bounded(page, selector):
    for element in page.locator(selector).all():
        if not element.is_visible():
            continue
        bounds = element.bounding_box()
        assert bounds["x"] >= -1, (selector, bounds)
        assert bounds["x"] + bounds["width"] <= page.viewport_size["width"] + 1, (
            selector,
            bounds,
        )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def geometry(page, name):
    return page.get_by_role("button", name=name, exact=True).evaluate("""element => {
      const rect = element.getBoundingClientRect();
      let y = rect.top;
      for (let parent = element.parentElement; parent; parent = parent.parentElement)
        y += parent.scrollTop;
      return { x: rect.left, y, height: rect.height };
    }""")


def assert_stable(before, after, dimensions=("x", "y", "height")):
    for dimension in dimensions:
        assert abs(before[dimension] - after[dimension]) <= 1, (before, after)


def choose_view(page, label):
    compact = page.get_by_role("button", name="View and sort", exact=True)
    if compact.is_visible():
        compact.tap()
        page.get_by_role("menuitemradio", name=label, exact=True).tap()
    else:
        page.get_by_role("button", name=label, exact=True).tap()


@pytest.mark.parametrize(
    "width,height",
    [
        (320, 640),
        (360, 740),
        (390, 844),
        (430, 932),
        (740, 360),
        (1440, 900),
    ],
)
def test_single_tap_and_selection_keep_file_positions_stable(
    server, browser, width, height
):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=True,
        is_mobile=width < 800,
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        open_fixture(page, server)
        folder = page.get_by_role("button", name="Preview samples", exact=True)
        folder_row = page.locator(".file-row").filter(has=folder)
        expect(folder_row.locator(".folder-icon")).to_be_visible()
        expect(folder_row.locator(".file-selection-slot")).to_have_count(0)
        page.get_by_role("button", name="Select", exact=True).tap()
        expect(folder_row.locator(".folder-icon")).to_be_visible()
        folder.tap()
        expect(page.get_by_label("Select Preview samples", exact=True)).to_be_checked()
        page.get_by_role("button", name="Done", exact=True).tap()
        page.get_by_role("button", name="Preview samples", exact=True).tap()
        target = page.get_by_role("button", name="sample.json", exact=True)
        expect(target).to_be_visible()
        for layout in ("List view", "Grid view"):
            choose_view(page, layout)
            target.scroll_into_view_if_needed()
            target.tap()
            preview = page.locator("dialog.file-preview")
            expect(preview).to_be_visible()
            expect(preview.locator("pre")).to_contain_text('"count": 3')
            assert_bounded(page, ".file-preview button, .file-preview a")
            preview.get_by_role("button", name="Close Preview", exact=True).tap()
            expect(preview).to_have_count(0)
            if width == 1440:
                target.click(modifiers=["Control"])
                expect(preview).to_have_count(0)
                expect(
                    page.get_by_label("Select sample.json", exact=True)
                ).to_have_count(0)
                target.dblclick()
                expect(preview).to_be_visible()
                preview.get_by_role("button", name="Close Preview", exact=True).click()
            # Browsing has no empty checkbox slot. Explicit selection adds its
            # checkbox alongside the icon while preserving all row heights.
            page.locator(".workspace").evaluate("e => e.scrollTop = 0")
            before = geometry(page, "sample.json")
            row = page.locator(".file-row").filter(has=target)
            expect(row.locator(".file-icon")).to_be_visible()
            expect(row.locator(".file-selection-slot")).to_have_count(0)
            if layout == "List view":
                icon = row.locator(".file-icon").bounding_box()
                cell = row.locator(".name-cell").bounding_box()
                assert 0 <= icon["x"] - cell["x"] <= 16
            toolbar = page.locator(".file-toolbar").bounding_box()
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            if layout == "List view":
                page.screenshot(
                    path=ARTIFACTS / f"phone-list-idle-{width}x{height}.png"
                )
            page.get_by_role("button", name="Select", exact=True).tap()
            expect(row.locator(".file-icon")).to_be_visible()
            assert_stable(before, geometry(page, "sample.json"), ("y", "height"))
            assert_stable(toolbar, page.locator(".file-toolbar").bounding_box())
            selecting = geometry(page, "sample.json")
            checkbox = page.get_by_role(
                "checkbox", name="Select sample.json", exact=True
            )
            # Click the label padding as a phone user does, rather than only
            # programmatically changing the input: this catches double toggles.
            label = checkbox.locator("..")
            label.tap(position={"x": 4, "y": 4})
            expect(checkbox).to_be_checked()
            expect(page.get_by_text("1 selected", exact=True)).to_be_visible()
            assert_stable(selecting, geometry(page, "sample.json"))
            if layout == "List view":
                page.locator(".workspace").evaluate("e => e.scrollTop = 0")
                page.screenshot(
                    path=ARTIFACTS / f"phone-list-selected-{width}x{height}.png"
                )
            target.tap()
            expect(checkbox).not_to_be_checked()
            expect(page.get_by_text("0 selected", exact=True)).to_be_visible()
            assert_stable(selecting, geometry(page, "sample.json"))
            page.get_by_role("button", name="Done", exact=True).tap()
            assert_stable(before, geometry(page, "sample.json"))
            expect(row.locator(".file-selection-slot")).to_have_count(0)
            assert_bounded(page, ".file-toolbar button")
            page.get_by_label("Actions for sample.json", exact=True).tap()
            menu = page.locator(".action-menu:popover-open")
            expect(menu).to_be_visible()
            assert_bounded(page, ".action-menu:popover-open")
            menu.get_by_role("button", name="Rename", exact=True).tap()
            dialog = page.get_by_role("dialog", name="Rename", exact=True)
            expect(dialog).to_be_visible()
            assert_bounded(
                page, "dialog[open], dialog[open] input, dialog[open] button"
            )
            page.keyboard.press("Escape")
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=ARTIFACTS / f"phone-stable-selection-{width}x{height}.png")
        assert errors == []
    finally:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=ARTIFACTS / f"phone-last-{width}x{height}.png")
        context.close()


@pytest.mark.parametrize("width,height", [(320, 640), (390, 844), (740, 360)])
def test_phone_navigation_search_new_and_owner_controls_are_reachable(
    server, browser, width, height
):
    context = browser.new_context(
        viewport={"width": width, "height": height}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        url = open_fixture(page, server)
        if page.get_by_role(
            "button", name="Toggle Navigation", exact=True
        ).is_visible():
            page.get_by_role(
                "button", name="Search Files and Folders", exact=True
            ).tap()
            expect(
                page.get_by_role(
                    "searchbox", name="Search Files and Folders", exact=True
                )
            ).to_be_focused()
            assert_bounded(page, ".file-toolbar input, .file-toolbar button")
            page.keyboard.press("Escape")
            page.get_by_role("button", name="Toggle Navigation", exact=True).tap()
            drawer = page.get_by_role("dialog", name="Drive navigation", exact=True)
            expect(drawer).to_be_visible()
            drawer.get_by_role("link", name="Trash", exact=True).tap()
            expect(drawer).to_have_count(0)
            page.goto(url)
        page.locator("#new-button:visible, #compact-new-button:visible").tap()
        assert_bounded(page, "#new-menu")
        page.get_by_role("button", name="New Folder", exact=True).tap()
        expect(page.get_by_role("dialog")).to_be_visible()
        assert_bounded(page, "dialog[open], dialog[open] input, dialog[open] button")
        page.keyboard.press("Escape")
        shares = [
            {
                "id": "phone-share",
                "node_id": "phone-node",
                "path": "/phone-node",
                "name": "A long shared document name for a phone.pdf",
                "type": "file",
                "created_at": "2026-10-20T00:00:00Z",
                "expires_at": None,
                "copyable": True,
            }
        ]
        page.route(
            "**/api/v1/shares", lambda route: route.fulfill(json={"shares": shares})
        )
        for name, suffix in [
            ("shares", "?view=shares"),
            ("storage", "?view=storage"),
            ("connections", "?view=connections"),
            ("preloads", "?view=preloads"),
            ("cache", "?view=settings&section=cache"),
            ("uploads", "?view=settings&section=transfers"),
            ("regional", "?view=settings&section=regional"),
            ("transfers", "?view=activity"),
            ("performance", "?view=performance"),
        ]:
            page.goto(url + "/" + suffix)
            expect(page.locator(".workspace h1").first).to_be_visible()
            page.wait_for_timeout(150)
            assert_bounded(
                page,
                ".workspace input, .workspace select, .workspace button, "
                ".share-row-actions a",
            )
            if name == "shares":
                for label in (
                    "Edit link for A long shared document name for a phone.pdf",
                    "Show A long shared document name for a phone.pdf in folder",
                    "Revoke Link for A long shared document name for a phone.pdf",
                ):
                    # Keep all link management actions available on phones.
                    expect(
                        page.get_by_role("button", name=label, exact=True)
                    ).to_be_visible()
            for control in page.locator(
                ".workspace input:not([type=checkbox]):not([type=radio])"
                ":not([type=range]):not([type=file]), "
                ".workspace select, .workspace textarea"
            ).all():
                if control.is_visible():
                    assert control.evaluate(
                        "e => parseFloat(getComputedStyle(e).fontSize) >= 16"
                    ), (
                        name,
                        control.evaluate(
                            "e => ({html:e.outerHTML,"
                            "font:getComputedStyle(e).fontSize})"
                        ),
                    )
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=ARTIFACTS / f"phone-{name}-{width}x{height}.png")
        page.route(
            "**/api/v1/trash?*",
            lambda route: route.fulfill(
                json={
                    "data": {
                        "contents": {
                            "phone-trash": {
                                "id": "phone-trash",
                                "name": "Trashed phone notes.txt",
                                "path": "/trash",
                                "type": "file",
                                "size": 124,
                                "parent_names": ["My Files"],
                            }
                        }
                    }
                }
            ),
        )
        page.goto(url + "/?path=/trash")
        trashed = page.get_by_role("button", name="Trashed phone notes.txt", exact=True)
        trashed.tap()
        expect(page.get_by_role("button", name="Select", exact=True)).to_be_visible()
        expect(page.get_by_role("checkbox")).to_have_count(0)
        expect(page.locator("dialog.file-preview")).to_have_count(0)
        page.get_by_role("button", name="Select", exact=True).tap()
        trashed.tap()
        expect(
            page.get_by_label("Select Trashed phone notes.txt", exact=True)
        ).to_be_checked()
        expect(page.get_by_role("button", name="Restore", exact=True)).to_be_visible()
        assert_bounded(page, ".file-toolbar button")
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("width,height", [(320, 640), (740, 360)])
def test_phone_preview_pdf_options_unsupported_and_retry_fit_viewport(
    server, browser, width, height
):
    context = browser.new_context(
        viewport={"width": width, "height": height}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    try:
        open_fixture(page, server)
        page.get_by_role("button", name="Preview samples", exact=True).tap()
        page.get_by_role("button", name="sample.pdf", exact=True).tap()
        expect(page.locator(".pdf-page canvas").first).to_be_visible()
        options = page.get_by_role("button", name="PDF View Options", exact=True)
        expect(options).to_be_visible()
        options.tap()
        assert_bounded(page, ".file-preview button, .file-preview a")
        fit = page.get_by_role("button", name="Fit Page", exact=True)
        fit.tap()
        expect(fit).to_have_attribute("aria-pressed", "true")
        expect(
            page.locator(".pdf-page").first.locator(".pdf-page-loading")
        ).to_have_count(0)
        page.screenshot(path=ARTIFACTS / f"phone-pdf-options-{width}x{height}.png")
        page.get_by_role("button", name="Close Preview", exact=True).tap()
        long_name = "installer-" + "very-long-file-name-" * 5 + "setup.exe"
        page.get_by_role("button", name=long_name, exact=True).tap()
        download = page.locator(".file-details-download")
        expect(download).to_be_visible()
        download.scroll_into_view_if_needed()
        expect(download).to_be_in_viewport()
        assert_bounded(page, ".file-preview button, .file-preview a")
        page.screenshot(path=ARTIFACTS / f"phone-unsupported-{width}x{height}.png")
        page.get_by_role("button", name="Close Preview", exact=True).tap()
        page.route(
            "**/api/v1/preview?*",
            lambda route: route.fulfill(
                status=503, json={"detail": "Temporary preview fixture failure. " * 10}
            ),
        )
        page.get_by_role("button", name="sample.json", exact=True).tap()
        retry = page.get_by_role("button", name="Try Preview Again", exact=True)
        expect(retry).to_be_visible()
        retry.scroll_into_view_if_needed()
        expect(retry).to_be_in_viewport()
        assert_bounded(page, ".file-preview button, .file-preview a")
        page.screenshot(path=ARTIFACTS / f"phone-preview-error-{width}x{height}.png")
        page.get_by_role("button", name="Close Preview", exact=True).tap()
        expect(page.locator("dialog.file-preview")).to_have_count(0)
    finally:
        context.close()
