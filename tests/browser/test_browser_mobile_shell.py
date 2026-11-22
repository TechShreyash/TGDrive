"""Phone-specific navigation and upload state cannot cover file content."""

from __future__ import annotations

import os

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.node_pages import NODES, fetch_with_rows, is_listing
from tests.browser.test_browser_e2e import ARTIFACTS, expect, login, upload

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_new_dialog_returns_focus_without_moving_controls(server, desktop, motion):
    _, page = desktop
    page.set_viewport_size({"width": 1536, "height": 695})
    page.emulate_media(reduced_motion=motion)
    login(page, server.url)
    new = page.get_by_role("button", name="New", exact=True)
    menu = page.get_by_role("group", name="New Item Actions")
    dialog = page.get_by_role("dialog", name="New Folder", exact=True)
    expected = "none" if motion == "reduce" else "refresh-fade"
    for _ in range(2):
        new.press("Enter")
        expect(menu).to_have_css("animation-name", expected)
        menu.get_by_role("button", name="New Folder", exact=True).press("Enter")
        expect(dialog).to_have_css("animation-name", expected)
        expect(dialog).to_have_css("transform", "none")
        expect(dialog.get_by_label("Folder Name")).to_be_focused()
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        expect(new).to_be_focused()


@pytest.mark.parametrize(
    "width,height,theme", [(320, 640, "light"), (390, 844, "dark"), (600, 800, "light")]
)
def test_phone_browsing_drawer_and_upload_use_separate_surfaces(
    server, desktop, width, height, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    page.add_init_script("localStorage.setItem('tgdrive.appearance','" + theme + "')")

    def listing(route):
        if not is_listing(route.request, "/"):
            route.fallback()
            return
        response, payload = fetch_with_rows(
            route,
            [
                {
                    "id": f"phone-{i}",
                    "name": f"Project notes {i:03}.txt",
                    "type": "file",
                    "path": "/",
                    "size": 1024,
                }
                for i in range(60)
            ],
        )
        route.fulfill(response=response, json=payload)

    page.route(NODES, listing)
    login(page, server.url)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    header = page.locator(".topbar")
    new = header.get_by_role("button", name="New", exact=True)
    expect(new).to_be_visible()
    expect(
        header.locator(":is(.brand-logo-light, .brand-logo-dark):visible")
    ).to_have_count(1)
    idle_new = new.bounding_box()
    theme_menu = header.locator(".theme-trigger").bounding_box()
    assert idle_new["width"] >= 44 and idle_new["height"] >= 44
    assert idle_new["x"] + idle_new["width"] <= theme_menu["x"]
    page.screenshot(path=ARTIFACTS / f"phone-files-idle-{width}.png")
    upload(page, ["Project walkthrough.mp4"])
    server.wait_started(1)
    status = page.get_by_role("button", name="Open transfer progress", exact=True)
    expect(status).to_be_visible()
    assert new.bounding_box() == idle_new
    assert page.locator(".file-row summary").evaluate_all("""actions => {
        const workspace = document.querySelector('.workspace').getBoundingClientRect();
        const visible = actions.filter(action => {
            const r = action.getBoundingClientRect();
            return r.top >= workspace.top && r.bottom <= workspace.bottom;
        });
        return visible.length > 0 && visible.every(action => {
            const r = action.getBoundingClientRect();
            return document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
                ?.closest('summary') === action;
        });
    }""")
    expect(page.locator("#transfer-dialog")).to_have_count(0)
    assert (
        page.locator(".workspace").bounding_box()["y"]
        + page.locator(".workspace").bounding_box()["height"]
        <= status.bounding_box()["y"] + 1
    )
    assert page.locator(".file-list-scroll").evaluate(
        "e => getComputedStyle(e).overflowY === 'visible'"
    )
    assert page.locator(".file-toolbar").evaluate(
        "e => e.scrollWidth <= e.clientWidth+1"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / f"phone-files-upload-{width}.png")
    page.locator(".workspace").evaluate("e => e.scrollTop=500")
    assert page.locator(".workspace").evaluate("e=>e.scrollTop") == 500
    expect(
        page.get_by_role("button", name="Toggle Navigation", exact=True)
    ).to_be_in_viewport()
    page.get_by_role("button", name="Toggle Navigation", exact=True).click()
    drawer = page.get_by_role("dialog", name="Drive navigation", exact=True)
    expect(drawer).to_be_visible()
    rect = drawer.bounding_box()
    assert rect["y"] == 0 and rect["height"] == height
    assert rect["width"] <= width - 39
    assert drawer.evaluate("e=>e.scrollTop") == 0
    assert page.locator("main").get_attribute("inert") is not None
    page.screenshot(path=ARTIFACTS / f"phone-drawer-{width}.png")
    page.keyboard.press("Escape")
    expect(drawer).to_have_count(0)
    expect(
        page.get_by_role("button", name="Toggle Navigation", exact=True)
    ).to_be_focused()
    more = page.get_by_role("button", name="More", exact=True)
    more.click()
    expect(drawer).to_be_visible()
    page.keyboard.press("Escape")
    expect(more).to_be_focused()
    more.click()
    drawer.get_by_role("link", name="Starred", exact=True).click()
    expect(page.locator("#main-content")).to_be_focused()
    status.click()
    expect(page.get_by_role("heading", name="Transfers", exact=True)).to_be_visible()
    expect(page.locator(".transfer-page")).to_be_visible()
    expect(status).to_have_count(0)
    page.screenshot(path=ARTIFACTS / f"phone-transfers-{width}.png")
    page.go_back()
    expect(status).to_be_visible()
    dismiss = page.get_by_role("button", name="Dismiss transfer notice", exact=True)
    assert dismiss.bounding_box()["width"] >= 44
    dismiss.click()
    expect(status).to_have_count(0)
    transfer_tab = page.get_by_role("navigation", name="Sections").get_by_role(
        "button", name="Transfers", exact=False
    )
    expect(transfer_tab).to_be_focused()
    expect(transfer_tab.locator(".mobile-tab-count")).to_have_text("1")
    page.get_by_role("navigation", name="Sections").get_by_role(
        "button", name="Files", exact=True
    ).click()
    expect(status).to_have_count(0)
    upload(page, ["Another upload.txt"])
    expect(status).to_have_text("2 uploads in progress")


@pytest.mark.parametrize(
    "width,theme", [(1440, "light"), (390, "dark"), (320, "light")]
)
def test_sidebar_groups_tools_and_exposes_project_link(server, desktop, width, theme):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(color_scheme=theme)
    login(page, server.url)

    def open_drawer():
        if page.locator(".sidebar").get_attribute("inert") is not None:
            page.get_by_role("button", name="Toggle Navigation", exact=True).click()
            # The drawer slides in; measure geometry once it has settled.
            page.locator(".sidebar").evaluate(
                "e => Promise.all(e.getAnimations().map(a => a.finished))"
            )

    open_drawer()
    navigation = page.get_by_role("navigation", name="Drive pages", exact=True)
    expect(navigation.get_by_role("group")).to_have_count(3)
    tools = navigation.get_by_role("group", name="Tools and settings", exact=True)
    expect(tools.get_by_role("link")).to_have_count(0)
    for label in ["My Files", "Starred", "Transfers", "Shared Links", "Trash"]:
        expect(page.get_by_role("link", name=label, exact=True)).to_be_in_viewport()
    recent = navigation.get_by_role("link", name="Recent", exact=True)
    starred = navigation.get_by_role("link", name="Starred", exact=True)
    recent_bounds, starred_bounds = recent.bounding_box(), starred.bounding_box()
    assert recent_bounds and starred_bounds
    assert recent_bounds["height"] >= (44 if width <= 720 else 40)
    assert starred_bounds["y"] - recent_bounds["y"] - recent_bounds["height"] >= 4
    starred.hover()
    github = page.get_by_role("link", name="TGDrive on GitHub", exact=True)
    expect(github).to_have_attribute("href", "https://github.com/TechShreyash/TGDrive")
    expect(github).to_have_attribute("target", "_blank")
    expect(github).to_have_attribute("rel", "noopener noreferrer")
    expect(github).to_be_in_viewport()
    assert github.inner_text() == ""
    support = page.locator(".sidebar-support")
    assert support.locator("a").count() == 2
    assert support.evaluate("e => getComputedStyle(e).position === 'static'")
    assert github.bounding_box()["width"] >= 44
    assert github.bounding_box()["height"] >= 44
    docs = support.get_by_role("link", name="Documentation", exact=True)
    assert abs(docs.bounding_box()["y"] - github.bounding_box()["y"]) <= 1
    logout = page.get_by_role("button", name="Log Out", exact=True)
    expect(logout).to_be_in_viewport()
    assert docs.evaluate("""element => {
      const style = getComputedStyle(element);
      return style.paddingLeft === style.paddingRight && style.flexGrow === '0';
    }""")
    nav_icon = page.get_by_role("link", name="My Files", exact=True).locator("svg")
    for icon in (docs.locator("svg"), logout.locator("svg")):
        assert abs(icon.bounding_box()["x"] - nav_icon.bounding_box()["x"]) <= 1
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    docs.hover()
    page.screenshot(
        path=ARTIFACTS / f"sidebar-documentation-hover-{theme}-{width}.png",
        animations="disabled",
    )
    page.screenshot(
        path=ARTIFACTS / f"sidebar-simple-{theme}-{width}.png", animations="disabled"
    )
    tools.get_by_role("button", name="Tools and settings", exact=True).press("Enter")
    expect(tools.get_by_role("link")).to_have_text(
        ["Performance", "Preloads", "Settings", "Connections", "Backups"]
    )
    tools.get_by_role("link", name="Performance", exact=True).press("Enter")
    expect(page.get_by_role("heading", name="Performance", exact=True)).to_be_visible()
    open_drawer()
    expect(page.locator('.drive-nav a[aria-current="page"]')).to_have_text(
        "Performance"
    )
    page.screenshot(
        path=ARTIFACTS / f"sidebar-tools-{theme}-{width}.png", animations="disabled"
    )
    page.get_by_role("link", name="My Files", exact=True).click()
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    open_drawer()
    page.goto(server.url + "/?view=settings&section=regional")
    expect(
        page.get_by_role("heading", name="Regional settings", exact=True)
    ).to_be_visible()
    open_drawer()
    expect(page.get_by_role("link", name="Settings", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_upload_count_keeps_files_reachable_in_a_very_short_viewport(server, desktop):
    _, page = desktop
    page.set_viewport_size({"width": 392, "height": 640})
    login(page, server.url)
    upload(page, ["zoom-layout.bin"])
    server.wait_started(1)
    status = page.get_by_role("button", name="Open transfer progress", exact=True)
    expect(status).to_be_visible()

    page.set_viewport_size({"width": 392, "height": 197})
    expect(status).to_have_count(0)
    folder = page.get_by_role("button", name="Preview samples", exact=True)
    folder.scroll_into_view_if_needed()
    expect(folder).to_be_in_viewport(ratio=1)
    transfers = page.get_by_role("navigation", name="Sections").get_by_role(
        "button", name="Transfers", exact=False
    )
    expect(transfers).to_contain_text("1")
    transfers.press("Enter")
    expect(page.get_by_role("heading", name="Transfers", exact=True)).to_be_visible()


@pytest.mark.parametrize(
    "width,height,theme",
    [(1536, 695, "dark"), (820, 1180, "light"), (390, 844, "light")],
)
def test_tools_and_settings_collapses_during_file_browsing(
    server, browser, width, height, theme
):
    touch = width <= 390
    with browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
    ) as context:
        page = context.new_page()
        page.add_init_script(f"localStorage.setItem('tgdrive.appearance', '{theme}')")
        login(page, server.url)

        def drawer():
            if page.locator(".sidebar").get_attribute("inert") is not None:
                page.get_by_role("button", name="Toggle Navigation", exact=True).click()

        drawer()
        navigation = page.get_by_role("navigation", name="Drive pages")
        tools = navigation.get_by_role("group", name="Tools and settings")
        page.screenshot(
            path=ARTIFACTS / f"sidebar-tools-{width}.png", animations="disabled"
        )
        toggle = tools.get_by_role("button", name="Tools and settings", exact=True)
        settings = tools.get_by_role("link", name="Settings", exact=True)
        expect(toggle).to_have_attribute("aria-expanded", "false", timeout=2000)
        expect(settings).to_have_count(0)
        if touch:
            toggle.tap()
        else:
            toggle.press("Space")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(settings).to_be_visible()
        toggle.press("Enter")
        expect(toggle).to_be_focused()
        expect(settings).to_have_count(0)

        page.goto(server.url + "/?view=settings&section=transfers")
        expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()
        drawer()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(settings).to_have_attribute("aria-current", "page")
        toggle.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_be_focused()
        expect(toggle).to_have_accessible_description("Current page is Settings")
        navigation.get_by_role("link", name="My Files", exact=True).click()
        drawer()
        expect(toggle).to_have_attribute("aria-expanded", "false")


@pytest.mark.parametrize("width,height", [(1280, 420), (1536, 695)])
def test_desktop_sidebar_scrolls_links_and_footer_with_brand_pinned(
    server, desktop, width, height
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 960})
    login(page, server.url + "/?view=performance")
    sidebar = page.locator(".sidebar")
    scroll = sidebar.locator(".sidebar-scroll")
    nav = sidebar.get_by_role("navigation", name="Drive pages")
    brand = sidebar.locator(".brand:visible")
    performance = nav.get_by_role("link", name="Performance", exact=True)
    expect(performance).to_have_attribute("aria-current", "page")
    page.set_viewport_size({"width": width, "height": height})
    expect(performance).to_be_in_viewport(ratio=1)
    expect(performance).not_to_be_focused()
    before = brand.bounding_box()
    assert before is not None
    storage = nav.get_by_role("link", name="Backups", exact=True)
    if height == 695:
        expect(storage).to_be_in_viewport(ratio=1)
    storage.focus()
    expect(storage).to_be_in_viewport()
    expect(brand).to_be_in_viewport()
    logout = sidebar.get_by_role("button", name="Log Out", exact=True)
    logout.focus()
    expect(logout).to_be_in_viewport(ratio=1)
    documentation = sidebar.get_by_role("link", name="Documentation", exact=True)
    expect(documentation).to_be_in_viewport(ratio=1)
    before_wheel = scroll.evaluate("e => e.scrollTop")
    documentation.hover()
    page.mouse.wheel(0, -120)
    page.wait_for_function(
        "(before) => document.querySelector('.sidebar-scroll').scrollTop < before",
        arg=before_wheel,
    )
    assert abs(brand.bounding_box()["y"] - before["y"]) <= 1
    assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")


@pytest.mark.parametrize("width,height", [(320, 640), (740, 360)])
def test_short_phone_drawer_keeps_close_visible_while_reaching_footer(
    server, browser, width, height
):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=True,
        is_mobile=True,
        reduced_motion="reduce",
    )
    try:
        page = context.new_page()
        login(page, server.url + "/?view=performance")
        toggle = page.get_by_role("button", name="Toggle Navigation", exact=True)
        toggle.tap()
        drawer = page.get_by_role("dialog", name="Drive navigation", exact=True)
        active = drawer.get_by_role("link", name="Performance", exact=True)
        close = drawer.get_by_role("button", name="Close Navigation", exact=True)
        expect(active).to_be_focused()
        expect(active).to_be_in_viewport(ratio=1)
        pinned = close.bounding_box()
        close.focus()
        page.keyboard.press("Shift+Tab")
        logout = drawer.get_by_role("button", name="Log Out", exact=True)
        expect(logout).to_be_focused()
        expect(logout).to_be_in_viewport(ratio=1)
        assert close.bounding_box() == pinned
        expect(close).to_be_in_viewport(ratio=1)
        close.tap()
        expect(drawer).to_have_count(0)
        expect(toggle).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize(
    "width,theme", [(1440, "light"), (1440, "dark"), (390, "dark"), (320, "light")]
)
def test_brand_asset_and_sidebar_footer_stay_clear_in_both_themes(
    server, desktop, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(color_scheme=theme)
    page.goto(server.url)
    expect(page.locator("#login-dialog")).to_be_visible()
    logo = page.locator(f".login-page .brand-logo-{theme}")
    expect(logo).to_be_visible()
    expected_logo = "tgdrive-logo-dark.svg" if theme == "dark" else "tgdrive-logo.svg"
    expect(logo).to_have_attribute("src", "/static/brand/" + expected_logo)
    logo.evaluate("img => img.decode()")
    assert logo.evaluate("img => img.complete && img.naturalWidth > 0")
    expect(page.locator('link[rel="icon"][type="image/svg+xml"]')).to_have_attribute(
        "href", "/static/brand/favicon.svg"
    )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=ARTIFACTS / f"brand-login-{theme}-{width}.png", animations="disabled"
    )
    login(page)
    for image in page.locator(".brand-logo img").all():
        image.evaluate("img => img.decode()")
        assert image.evaluate("img => img.complete && img.naturalWidth > 0")
    if width <= 720:
        page.screenshot(
            path=ARTIFACTS / f"brand-mobile-header-{theme}-{width}.png",
            animations="disabled",
        )
        page.get_by_role("button", name="Toggle Navigation", exact=True).click()
    sidebar = page.locator(".sidebar")
    assert sidebar.evaluate("e => e.scrollWidth <= e.clientWidth")
    support = sidebar.locator(".sidebar-support")
    assert support.evaluate("e => e.scrollWidth <= e.clientWidth")
    github = support.get_by_role("link", name="TGDrive on GitHub", exact=True)
    support.get_by_role("link", name="Documentation", exact=True).focus()
    page.keyboard.press("Tab")
    expect(github).to_be_focused()
    assert github.evaluate("e => getComputedStyle(e).outlineStyle !== 'none'")
    page.screenshot(
        path=ARTIFACTS / f"brand-sidebar-{theme}-{width}.png", animations="disabled"
    )
