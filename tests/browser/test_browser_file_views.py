"""Sorting, grid browsing and lazy provider thumbnails on the production bundle."""

from __future__ import annotations

import json
import os
import re
from urllib.parse import parse_qs, urlparse

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.node_pages import NODES, fetch_with_rows
from tests.browser.test_browser_e2e import ARTIFACTS, expect, login

browser = fixtures.browser
desktop = fixtures.desktop
server = fixtures.server
pytestmark = pytest.mark.e2e


def choose_view(page, name):
    compact = page.get_by_role("button", name="View and sort", exact=True)
    if compact.count():
        compact.scroll_into_view_if_needed()
        page.evaluate(
            "() => new Promise(resolve => "
            "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        compact.click()
        page.get_by_role("menuitemradio", name=name, exact=True).click()
    else:
        page.get_by_role("button", name=name, exact=True).click()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_forced_colors_distinguishes_active_navigation_and_selection(
    server, desktop, scheme
):
    _, page = desktop
    page.emulate_media(
        forced_colors="active", color_scheme=scheme, reduced_motion="reduce"
    )
    login(page, server.url)
    active = page.get_by_role("link", name="My Files", exact=True)
    inactive = page.get_by_role("link", name="Starred", exact=True)
    expect(active).to_have_attribute("aria-current", "page")
    expect(active).to_have_css("text-decoration-line", "underline")
    expect(inactive).to_have_css("text-decoration-line", "none")

    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    file = page.get_by_role("button", name="sample.json", exact=True)
    row = page.locator(".file-row").filter(has=file)
    file.click()
    expect(row.locator(".name-cell")).to_have_css("outline-style", "solid")
    choose_view(page, "Grid view")
    if not row.evaluate("el => el.classList.contains('selected')"):
        file.click()
    expect(row).to_have_css("outline-style", "solid")
    expect(page.get_by_role("button", name="Grid view", exact=True)).to_have_css(
        "text-decoration-line", "underline"
    )
    file.press("Tab")
    page.keyboard.press("Shift+Tab")
    expect(file).to_be_focused()
    expect(row).to_have_css("outline-style", "solid")
    expect(file).to_have_css("outline-style", "none")


@pytest.mark.parametrize(
    "width,height,theme,touch",
    [
        (1920, 1080, "dark", False),
        (1440, 960, "light", False),
        (390, 844, "light", True),
        (320, 740, "dark", True),
        (900, 400, "dark", True),
    ],
)
def test_file_options_menu_density_touch_targets_and_focus(
    server, browser, width, height, theme, touch
):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
    )
    context.add_init_script(
        f"localStorage.setItem('tgdrive.appearance', '{theme}');"
        "localStorage.setItem('tgdrive.file-view', 'grid');"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    artifact = f"file-options-menu-{width}x{height}-{theme}"
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    try:
        login(page, server.url)
        folder = page.get_by_role("button", name="Preview samples", exact=True)
        folder.tap() if touch else folder.press("Enter")
        row = page.locator(".file-row").filter(
            has=page.get_by_role("button", name="sample.json", exact=True)
        )
        trigger = row.get_by_label("Actions for sample.json", exact=True)
        trigger.tap() if touch else trigger.click()
        menu = row.locator(".action-menu")
        expect(menu).to_be_visible()
        actions = menu.locator(":scope > button, :scope > a")
        expect(actions).to_have_text(
            [
                "Open",
                "Download",
                "Copy Download Link",
                "Preload file",
                "Add to Starred",
                "Create ZIP",
                "Rename",
                "Move",
                "Share Link",
                "Move to Trash",
            ]
        )
        expect(menu.get_by_role("separator")).to_have_count(2)
        for name in ("Copy Download Link", "Preload file"):
            expect(menu.get_by_role("button", name=name, exact=True)).to_be_visible()
        page.evaluate("document.fonts.ready")
        bounds = menu.bounding_box()
        assert bounds is not None
        assert bounds["width"] == 208
        assert bounds["x"] >= 11 and bounds["y"] >= 11
        assert bounds["x"] + bounds["width"] <= width - 11
        assert bounds["y"] + bounds["height"] <= height - 11
        targets = actions.evaluate_all(r"""nodes => nodes.map(node => ({
          label: node.textContent.trim().replace(/\s+/g, ' '),
          height: node.getBoundingClientRect().height,
          width: node.getBoundingClientRect().width,
          fits: node.scrollWidth <= node.clientWidth
        }))""")
        assert all(target["fits"] for target in targets), targets
        if touch:
            assert all(target["height"] >= 44 for target in targets), targets
        else:
            assert bounds["height"] <= 340
            assert all(30 <= target["height"] <= 32 for target in targets), targets
        assert menu.get_by_role("button", name="Move to Trash").evaluate(
            "node => getComputedStyle(node).color"
        ) != menu.get_by_role("button", name="Rename", exact=True).evaluate(
            "node => getComputedStyle(node).color"
        )
        (ARTIFACTS / f"{artifact}.json").write_text(
            json.dumps({"bounds": bounds, "targets": targets}, indent=2)
        )
        page.screenshot(path=ARTIFACTS / f"{artifact}.png", animations="disabled")
        menu.screenshot(
            path=ARTIFACTS / f"{artifact}-detail.png", animations="disabled"
        )
        last = menu.get_by_role("button", name="Move to Trash", exact=True)
        last.scroll_into_view_if_needed()
        expect(last).to_be_in_viewport()
        if height == 400:
            assert menu.evaluate("node => node.scrollHeight > node.clientHeight")
            menu.screenshot(
                path=ARTIFACTS / f"{artifact}-scrolled.png", animations="disabled"
            )
        if width in (320, 1440):
            name = row.get_by_role("button", name="sample.json", exact=True)
            next_name = row.locator("xpath=following-sibling::li[1]").locator(
                ".item-name-button"
            )
            trigger.focus()
            page.keyboard.press("Tab")
            expect(menu.get_by_role("button", name="Open", exact=True)).to_be_focused()
            page.keyboard.press("Shift+Tab")
            expect(trigger).to_be_focused()
            expect(menu).to_be_visible()
            page.keyboard.press("Shift+Tab")
            expect(name).to_be_focused()
            expect(menu).not_to_be_visible()
            trigger.press("Enter")
            expect(menu).to_be_visible()
            last.focus()
            page.keyboard.press("Tab")
            expect(next_name).to_be_focused()
            expect(menu).not_to_be_visible()
            trigger.press("Enter")
            expect(menu).to_be_visible()
        menu.get_by_role("button", name="Open", exact=True).focus()
        page.keyboard.press("Tab")
        expect(menu.get_by_role("link", name="Download", exact=True)).to_be_focused()
        page.keyboard.press("Escape")
        expect(menu).not_to_be_visible()
        expect(trigger).to_be_focused()
        trigger.press("Enter")
        rename = menu.get_by_role("button", name="Rename", exact=True)
        rename.tap() if touch else rename.press("Enter")
        expect(page.get_by_role("dialog", name="Rename", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        expect(trigger).to_be_focused()
        trigger.press("Enter")
        menu.get_by_role("button", name="Add to Starred", exact=True).click()
        expect(page.get_by_text("Added to Starred.", exact=True)).to_be_visible()
        trigger.press("Enter")
        remove = menu.get_by_role("button", name="Remove from Starred", exact=True)
        expect(remove).to_be_visible()
        assert remove.evaluate("node => node.scrollWidth <= node.clientWidth")
    finally:
        context.close()
    assert errors == []


def test_row_options_close_when_scrolled_behind_the_file_toolbar(server, desktop):
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    files = page.get_by_role("region", name="File list", exact=True)
    row = files.locator('.file-row[data-kind="file"]').first
    trigger = row.locator("summary")
    trigger.click()
    menu = row.locator(".action-menu")
    expect(menu).to_be_visible()
    files.evaluate("list => { list.scrollTop = list.scrollHeight; }")
    assert trigger.evaluate(
        "trigger => trigger.getBoundingClientRect().bottom <= "
        "trigger.closest('.file-list-scroll').getBoundingClientRect().top"
    )
    expect(menu).to_be_hidden()


@pytest.mark.parametrize("width,height", [(390, 844), (320, 740), (900, 400)])
def test_last_mobile_card_actions_clear_header_new_button(
    server, browser, width, height
):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=True,
        is_mobile=True,
        reduced_motion="reduce",
    )
    context.add_init_script("localStorage.setItem('tgdrive.file-view', 'grid')")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def listing(route):
        response, data = fetch_with_rows(
            route,
            [
                {
                    "id": "mobile-long-pdf",
                    "name": "CN_Complete_Theory_Study_Book.pdf",
                    "path": "/",
                    "type": "file",
                    "size": 500000,
                },
                {
                    "id": "mobile-short-video",
                    "name": "vid_720p.mp4",
                    "path": "/",
                    "type": "file",
                    "size": 8000000,
                },
            ],
        )
        folders = [item for item in data["items"] if item["type"] == "folder"]
        assert len(folders) == 3
        folders[0]["name"] = (
            "Cyberpunk.Edgerunners.S02.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264"
        )
        route.fulfill(response=response, json=data)

    page.route(NODES, listing)
    try:
        login(page, server.url)
        long_card = page.locator('[data-item-id="mobile-long-pdf"]')
        short_card = page.locator('[data-item-id="mobile-short-video"]')
        expect(short_card).to_be_visible()
        if width <= 720:
            assert (
                long_card.locator(".file-name").bounding_box()["height"]
                > short_card.locator(".file-name").bounding_box()["height"]
            )
            assert (
                abs(
                    long_card.bounding_box()["height"]
                    - short_card.bounding_box()["height"]
                )
                <= 1
            )
        page.locator(".workspace.file-workspace").evaluate(
            "node => node.scrollTop = node.scrollHeight"
        )
        page.evaluate(
            "() => new Promise(resolve => "
            "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        geometry = short_card.locator("summary").evaluate("""node => {
          const rect = element => {
            const r = element.getBoundingClientRect();
            return {top:r.top,bottom:r.bottom,left:r.left,right:r.right};
          };
          const workspace = document.querySelector('.workspace.file-workspace');
          const target = node.getBoundingClientRect();
          const hit = document.elementFromPoint(
            target.x + target.width / 2, target.y + target.height / 2);
          const create = document.querySelector('#compact-new-button');
          return {target:rect(node), create:rect(create),
            workspace:rect(workspace), scrollTop:workspace.scrollTop,
            maxScroll:workspace.scrollHeight-workspace.clientHeight,
            centerHitsAction:hit?.closest('summary')===node};
        }""")
        assert geometry["create"]["bottom"] <= geometry["workspace"]["top"], geometry
        assert geometry["workspace"]["top"] <= geometry["target"]["top"], geometry
        assert geometry["target"]["bottom"] <= geometry["workspace"]["bottom"], geometry
        assert geometry["centerHitsAction"], geometry
        assert abs(geometry["scrollTop"] - geometry["maxScroll"]) <= 1, geometry
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / f"last-card-clearance-{width}x{height}.json").write_text(
            json.dumps(geometry, indent=2)
        )
        page.screenshot(
            path=ARTIFACTS / f"last-card-clearance-{width}x{height}.png",
            animations="disabled",
        )
        trigger = short_card.get_by_label("Actions for vid_720p.mp4", exact=True)
        trigger.tap()
        expect(short_card.locator(".action-menu")).to_be_visible()
        page.keyboard.press("Escape")
        expect(trigger).to_be_focused()
    finally:
        context.close()
    assert errors == []


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_grid_views_preserve_selection_actions_and_bounded_thumbnails(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    thumbnails = []
    originals = []
    page.on(
        "request",
        lambda request: (
            thumbnails.append(request.url)
            if "/api/v1/thumbnail?" in request.url
            else originals.append(request.url)
            if urlparse(request.url).path == "/file"
            else None
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    expect(page.get_by_role("button", name="sample.png", exact=True)).to_be_visible()
    expect(page.locator(".file-table thead")).to_be_visible()
    list_sample = page.locator(".file-row").filter(has_text="sample.png")
    list_sample.scroll_into_view_if_needed()
    expect(list_sample.locator(".file-list-thumbnail img")).to_be_visible()
    assert thumbnails and not originals
    if width > 720:
        view = page.get_by_role("group", name="File view", exact=True)
        list_button = view.get_by_role("button", name="List view", exact=True)
        grid_button = view.get_by_role("button", name="Grid view", exact=True)
        expect(list_button).to_have_text("List")
        expect(grid_button).to_have_text("Grid")
        for button in (list_button, grid_button):
            box = button.bounding_box()
            assert 24 <= box["height"] <= 36 and box["width"] >= 24
        grid_button.click()
        expect(grid_button).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_role("list", name="Files and folders")).to_be_visible()
        list_button.click()
        expect(list_button).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".file-table thead")).to_be_visible()
        list_button.focus()
        page.keyboard.press("ArrowRight")
        expect(grid_button).to_be_focused()
        expect(list_button).to_have_attribute("aria-pressed", "true")
        page.keyboard.press("Space")
        expect(grid_button).to_have_attribute("aria-pressed", "true")
        page.keyboard.press("Space")
        expect(grid_button).to_have_attribute("aria-pressed", "true")
        expect(list_button).to_have_attribute("aria-pressed", "false")
    else:
        trigger = page.get_by_role("button", name="View and sort", exact=True)
        assert trigger.bounding_box()["height"] >= 44
        expect(trigger).to_have_attribute("aria-description", re.compile("^List view"))
        choose_view(page, "Grid view")
        expect(trigger).to_have_attribute("aria-description", re.compile("^Grid view"))
    grid = page.get_by_role("list", name="Files and folders", exact=True)
    expect(grid).to_be_visible()
    # Ensure the fixture can scroll fully outside the thumbnail observer margin.
    columns = grid.evaluate(
        """element => {
            const previous = element.style.gridTemplateColumns;
            element.style.gridTemplateColumns = 'repeat(2, minmax(0, 1fr))';
            return previous;
        }"""
    )
    sample = grid.locator(".file-row").filter(has_text="sample.png")
    sample.scroll_into_view_if_needed()
    preview = sample.locator(".file-tile-preview")
    image = preview.locator("img")
    expect(image).to_be_visible()
    assert thumbnails and not originals
    source = image.get_attribute("src")
    item_id = sample.get_attribute("data-item-id")
    assert source and item_id
    request = next(
        url
        for url in thumbnails
        if parse_qs(urlparse(url).query)["path"][0].endswith("/" + item_id)
    )
    request_count = thumbnails.count(request)
    observer = preview.evaluate_handle(
        """element => {
            const observer = new IntersectionObserver(([entry]) => {
                element.dataset.inView = String(entry.isIntersecting);
            }, {rootMargin: '120px'});
            observer.observe(element);
            return observer;
        }"""
    )
    # The first row can already be visible while the thumbnail is still in view.
    page.locator(".workspace, .file-grid-scroll").evaluate_all(
        "(areas) => areas.forEach(area => area.scrollTo({top: 0, behavior: 'instant'}))"
    )
    expect(preview).to_have_attribute("data-in-view", "false")
    expect(image).to_have_attribute("src", source)
    sample.scroll_into_view_if_needed()
    expect(preview).to_have_attribute("data-in-view", "true")
    preview.evaluate(
        """() => new Promise(resolve =>
            requestAnimationFrame(() => requestAnimationFrame(resolve))
        )"""
    )
    expect(sample.locator(".thumbnail-loading")).to_have_count(0)
    expect(image).to_have_attribute("src", source)
    assert thumbnails.count(request) == request_count
    observer.evaluate("observer => observer.disconnect()")
    grid.evaluate(
        "(element, value) => element.style.gridTemplateColumns = value", columns
    )
    page.get_by_role("button", name="Select", exact=True).click()
    page.get_by_role("checkbox", name="Select sample.png", exact=True).check()
    expect(page.get_by_text("1 selected", exact=True)).to_be_visible()
    page.get_by_role(
        "checkbox", name="Select All Files on This Page", exact=True
    ).check()
    expect(page.get_by_text("16 selected", exact=True)).to_be_visible()
    page.get_by_role("button", name="Done", exact=True).click()
    page.locator('summary[aria-label="Actions for sample.png"]').click()
    expect(page.get_by_role("button", name="Rename", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    page.reload()
    expect(page.get_by_role("list", name="Files and folders")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=ARTIFACTS / f"grid-files-{width}.png")
    choose_view(page, "List view")
    expect(page.locator(".file-table thead")).to_be_visible()
    page.reload()
    expect(page.locator(".file-table thead")).to_be_visible()
    assert page.evaluate("localStorage.getItem('tgdrive.file-view')") == "list"


@pytest.mark.parametrize(
    "width,theme",
    [
        (1440, "light"),
        (1440, "dark"),
        (1536, "light"),
        (1536, "dark"),
        (1920, "light"),
        (1920, "dark"),
        (390, "light"),
        (320, "dark"),
    ],
)
def test_long_file_cards_and_compact_list_thumbnails(server, desktop, width, theme):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance', '{theme}')")
    login(page, server.url)
    names = [
        "Cyberpunk.Edgerunners.S02E01.Last.Chance.for.a.Slow.Dance.1080p.WEB-DL.mkv",
        "Pilot.mkv",
    ]
    identities = []
    originals = []
    page.on(
        "request",
        lambda request: (
            originals.append(request.url)
            if urlparse(request.url).path == "/file"
            else None
        ),
    )

    def listing(route):
        response = route.fetch()
        data = response.json()
        if not any(item["name"] == "sample.png" for item in data["items"]):
            route.fulfill(response=response)
            return
        items = [
            next(item for item in data["items"] if item["name"] == name)
            for name in ("sample.png", "multitrack.mkv")
        ]
        identities[:] = [item["id"] for item in items]
        data.update(
            items=[
                {
                    **item,
                    "name": name,
                    "starred": True,
                    "upload_date": added,
                    "active_at": "2040-01-10T12:00:00Z",
                }
                for item, name, added in zip(
                    items, names, ("2020-04-09T12:00:00Z", None), strict=True
                )
            ],
            total=2,
            next_cursor=None,
        )
        route.fulfill(response=response, json=data)

    def thumbnail(route):
        path = parse_qs(urlparse(route.request.url).query)["path"][0]
        if identities and path.endswith("/" + identities[1]):
            route.fulfill(status=404, json={"detail": "Synthetic missing thumbnail"})
        else:
            route.continue_()

    page.route(NODES, listing)
    page.route("**/api/v1/thumbnail?**", thumbnail)
    with page.expect_response(
        lambda response: "/api/v1/thumbnail?" in response.url and response.status == 404
    ):
        page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    rows = page.locator(".file-row")
    expect(rows).to_have_count(2)
    first, second = rows.nth(0), rows.nth(1)
    expect(first.locator(".file-list-thumbnail img")).to_be_visible()
    expect(second.locator(".thumbnail-loading")).to_have_count(0)
    expect(second.locator(".file-list-thumbnail .file-icon")).to_be_visible()
    expect(second.locator(".file-list-thumbnail img")).to_have_count(0)
    assert first.locator(".file-list-thumbnail").bounding_box()["width"] == 36
    fallback = second.locator(".file-list-thumbnail .file-icon").bounding_box()
    assert fallback["width"] <= 36 and fallback["height"] <= 36
    if width > 720:
        expect(first.locator(".date-cell")).to_contain_text("2020")
        expect(second.locator(".date-cell")).to_have_text("—")
        header = page.get_by_role("button", name="Sort by Name", exact=True)
        assert (
            abs(
                header.bounding_box()["x"]
                - first.locator(".file-name").bounding_box()["x"]
            )
            <= 1
        )
        for cell in (".date-cell", ".size-cell", ".actions-cell"):
            assert (
                abs(
                    first.locator(cell).bounding_box()["x"]
                    - second.locator(cell).bounding_box()["x"]
                )
                <= 1
            )
    else:
        mobile_meta = first.locator(".file-mobile-meta")
        expect(mobile_meta).to_be_visible()
        expect(mobile_meta.locator(".file-mobile-date")).to_contain_text("2020")
        expect(second.locator(".file-mobile-date")).to_have_count(0)
        name = first.locator(".file-name").bounding_box()
        metadata = mobile_meta.bounding_box()
        assert abs(name["x"] - metadata["x"]) <= 1
        assert metadata["y"] >= name["y"] + name["height"] - 1
    assert not originals
    page.screenshot(
        path=ARTIFACTS / f"file-list-thumbnails-{width}-{theme}.png",
        animations="disabled",
    )
    choose_view(page, "Grid view")
    expect(rows.locator(".size-cell, .date-cell, .file-mobile-meta")).to_have_count(0)
    expect(first.locator(".file-tile-preview img")).to_be_visible()
    badge = first.locator(".file-status-starred")
    expect(badge).to_be_visible()
    assert badge.evaluate("node => getComputedStyle(node).borderTopWidth") == "1px"
    title = first.get_by_role("button", name=names[0], exact=True)
    expect(title).to_have_attribute("title", names[0])
    assert title.evaluate("node => getComputedStyle(node).webkitLineClamp") == "2"
    expect(first).to_have_css("transform", "none")
    expect(second).to_have_css("transform", "none")
    tile = first.locator(".file-tile-preview")
    expect(tile).to_have_css("transform", "none")
    card = first.bounding_box()
    preview = tile.bounding_box()
    assert abs(preview["width"] / preview["height"] - 16 / 9) < 0.03
    badge_box = badge.bounding_box()
    assert preview["y"] <= badge_box["y"]
    assert badge_box["y"] + badge_box["height"] <= preview["y"] + preview["height"]
    if width > 720:
        assert 195 <= card["width"] <= 340
        assert card["height"] <= card["width"] + 24
        assert abs(card["width"] - preview["width"] - 14) <= 1
    else:
        assert 130 <= card["width"] <= 190
    title_box = title.bounding_box()
    first_menu = first.locator("summary").bounding_box()
    other_menu = second.locator("summary").bounding_box()
    thumbnail_bottom = preview["y"] + preview["height"]
    assert title_box["y"] >= thumbnail_bottom - 1
    assert first_menu["y"] >= thumbnail_bottom - 1
    assert title_box["x"] + title_box["width"] <= first_menu["x"] + 1
    assert first_menu["y"] < title_box["y"] + title_box["height"]
    assert title_box["y"] < first_menu["y"] + first_menu["height"]
    assert first_menu["y"] + first_menu["height"] <= card["y"] + card["height"] + 1
    if width <= 720:
        assert first_menu["width"] >= 44 and first_menu["height"] >= 44
    assert abs(card["height"] - second.bounding_box()["height"]) <= 1
    assert abs(first_menu["y"] - other_menu["y"]) <= 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=ARTIFACTS / f"file-cards-long-names-{width}-{theme}.png",
        animations="disabled",
    )
    page.keyboard.press("Tab")
    title.focus()
    expect(title).to_be_focused()
    expect(first).to_have_css("outline-style", "solid")
    expect(title).to_have_css("outline-style", "none")
    assert first.bounding_box()["height"] == card["height"]
    first.locator("summary").click()
    expect(first.locator(".action-menu")).to_be_visible()
    full_name = first.locator(".file-action-name")
    if width <= 720:
        expect(full_name).to_have_text(names[0])
        expect(full_name).to_be_visible()
        assert full_name.evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )
    else:
        expect(full_name).to_have_count(0)
    page.keyboard.press("Escape")
    expect(first.locator(".action-menu")).not_to_be_visible()
    page.get_by_role("button", name="Select", exact=True).click()
    page.get_by_role("checkbox", name="Select " + names[0], exact=True).check()
    expect(first).to_have_class("file-row selected")
    assert not originals


@pytest.mark.parametrize(
    "width,theme",
    [
        (1440, "light"),
        (1440, "dark"),
        (1536, "light"),
        (1536, "dark"),
        (1920, "light"),
        (1920, "dark"),
        (390, "light"),
        (320, "dark"),
    ],
)
def test_root_mixed_cards_keep_compact_desktop_bounds(server, desktop, width, theme):
    _, page = desktop
    folder_name = "Cyberpunk.Edgerunners.S02.1080p.NF.WEB-DL.DUAL.DDP5.1"
    page.set_viewport_size({"width": width, "height": 1080})
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance', '{theme}')")

    def listing(route):
        response, data = fetch_with_rows(
            route,
            [
                {
                    "id": "mixed-media-fixture",
                    "name": "A root video with a longer filename.mkv",
                    "path": "/",
                    "type": "file",
                    "size": 1024,
                }
            ],
        )
        first_folder = next(item for item in data["items"] if item["type"] == "folder")
        first_folder["name"] = folder_name
        first_folder["starred"] = True
        first_folder["read_only"] = width == 320
        short_folder = next(
            item
            for item in data["items"]
            if item["type"] == "folder" and item is not first_folder
        )
        short_folder["name"] = "Short folder"
        short_folder["starred"] = True
        route.fulfill(response=response, json=data)

    page.route(NODES, listing)
    login(page, server.url)
    choose_view(page, "Grid view")
    rows = page.locator(".file-grid .file-row")
    expect(rows).to_have_count(4)
    expect(rows.locator(".size-cell, .date-cell, .file-mobile-meta")).to_have_count(0)
    folder_row = rows.filter(has=page.get_by_text(folder_name, exact=True))
    folder_id = folder_row.get_attribute("data-item-id")
    assert folder_id
    if width != 1440:
        assert page.request.post(
            server.url + "/api/v1/shares",
            data={"path": "/" + folder_id, "expires_in_days": 7},
        ).ok
        page.reload()
        expect(folder_row.locator(".file-status-shared")).to_be_visible()
    for row in rows.all():
        box = row.bounding_box()
        if width > 720:
            assert 195 <= box["width"] <= 340
        if row.get_attribute("data-kind") == "folder":
            expect(row.locator(".file-tile-preview")).to_have_count(0)
            assert any(
                "Folder" in line.split()
                for line in row.aria_snapshot().splitlines()
                if line.lstrip().startswith("- text:")
            )
            minimum = 60 if width > 720 else 70
            assert minimum <= box["height"] <= 90
            expect(row.locator(".file-kind-icon")).to_be_visible()
            status = row.locator(".file-item-status")
            if status.count():
                status_box = status.bounding_box()
                menu_icon = row.locator("summary svg").bounding_box()
                assert status_box["y"] + status_box["height"] <= menu_icon["y"]
            if width <= 720:
                assert box["width"] > 260
                name = row.locator(".file-name").bounding_box()
                menu = row.locator("summary").bounding_box()
                assert name["x"] + name["width"] <= menu["x"] - 4
        elif width > 720:
            assert box["height"] <= box["width"] + 24
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.mouse.move(0, 0)
    page.screenshot(
        path=ARTIFACTS / f"root-mixed-cards-compact-{width}-{theme}.png",
        animations="disabled",
    )
    status = folder_row.locator(".file-item-status")
    expected_status = (
        "Read-onlyStarredShared"
        if width == 320
        else "Starred"
        if width == 1440
        else "StarredShared"
    )
    expect(status).to_have_text(expected_status)
    name_box = folder_row.locator(".file-name").bounding_box()
    status_box = status.bounding_box()
    menu_icon = folder_row.locator("summary svg").bounding_box()
    assert status_box["x"] >= name_box["x"] + name_box["width"]
    assert status_box["y"] + status_box["height"] <= menu_icon["y"]
    for badge in status.locator(".file-status-label").all():
        style = badge.evaluate("""node => {
          const style = getComputedStyle(node);
          return {background:style.backgroundColor,shadow:style.boxShadow,
            border:style.borderTopWidth};
        }""")
        assert style == {
            "background": "rgba(0, 0, 0, 0)",
            "shadow": "none",
            "border": "0px",
        }
    wrapped_height = folder_row.bounding_box()["height"]
    assert folder_row.locator(".file-name").evaluate("""name => {
        const text = document.createRange();
        text.selectNodeContents(name);
        const {bottom} = name.getBoundingClientRect();
        return new Set([...text.getClientRects()]
            .filter(rect => rect.bottom <= bottom + 1)
            .map(rect => Math.round(rect.top))).size === 2;
    }""")
    folder_row.locator("summary").click()
    expect(folder_row.locator(".action-menu")).to_be_visible()
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Select", exact=True).click()
    page.get_by_role("checkbox", name="Select " + folder_name, exact=True).check()
    expect(folder_row).to_have_class("file-row selected")
    assert folder_row.bounding_box()["height"] == pytest.approx(wrapped_height, abs=1)


@pytest.mark.parametrize("width", [1536, 1920])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_grid_fills_available_width_without_stretching_sparse_folders(
    server, desktop, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 1080})
    page.emulate_media(reduced_motion="reduce")
    page.add_init_script(
        f"localStorage.setItem('tgdrive.appearance', '{theme}');"
        "localStorage.setItem('tgdrive.file-view', 'grid');"
    )
    items = [
        {
            "id": f"responsive-recent-{index}",
            "name": f"A recent video with a long descriptive filename {index}.mkv",
            "type": "file",
            "path": "/",
            "size": 1024,
            "parent_names": ["Preview samples"],
            "active_at": "2026-11-03T12:00:00Z",
        }
        for index in range(6)
    ]
    page.route(
        "**/api/v1/recent?**",
        lambda route: route.fulfill(json={"items": items, "next_cursor": None}),
    )
    login(page, server.url)
    page.goto(server.url + "/?view=recent")
    choose_view(page, "Grid view")
    grid = page.get_by_role("list", name="Files and folders", exact=True)
    cards = grid.locator(":scope > .file-row")
    expect(cards).to_have_count(6)
    tracks = grid.evaluate("""element => {
      const style = getComputedStyle(element);
      const columns = style.gridTemplateColumns.split(' ').map(parseFloat);
      return {columns,gap:parseFloat(style.columnGap),
        width:element.getBoundingClientRect().width};
    }""")
    page.screenshot(
        path=ARTIFACTS / f"responsive-grid-{width}-{theme}.png",
        animations="disabled",
    )
    assert len(tracks["columns"]) == 5
    assert sum(tracks["columns"]) + tracks["gap"] * (
        len(tracks["columns"]) - 1
    ) == pytest.approx(tracks["width"], abs=1)
    first = cards.first.bounding_box()
    for index, card in enumerate(cards.all()):
        bounds = card.bounding_box()
        assert 230 <= bounds["width"] <= 340
        if index < len(tracks["columns"]):
            assert bounds["y"] == pytest.approx(first["y"], abs=1)
        else:
            assert bounds["y"] >= first["y"] + first["height"]
        thumbnail = card.locator(".file-tile-preview").bounding_box()
        assert thumbnail["width"] / thumbnail["height"] == pytest.approx(
            16 / 9, abs=0.03
        )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    def sparse(route):
        response = route.fetch()
        data = response.json()
        data.update(items=items[:2], total=2, next_cursor=None)
        route.fulfill(response=response, json=data)

    page.route(NODES, sparse)
    page.goto(server.url)
    expect(cards).to_have_count(2)
    assert cards.first.bounding_box()["width"] == pytest.approx(first["width"], abs=1)
    page.screenshot(
        path=ARTIFACTS / f"responsive-sparse-two-{width}-{theme}.png",
        animations="disabled",
    )
    held = []
    page.route(NODES, lambda route: held.append(route))
    with page.expect_request(NODES):
        page.reload(wait_until="domcontentloaded")
    skeleton = page.locator('.file-skeleton[data-layout="grid"]')
    expect(skeleton).to_be_visible()
    skeleton_columns = skeleton.evaluate(
        "node => getComputedStyle(node).gridTemplateColumns.split(' ').map(parseFloat)"
    )
    assert skeleton_columns == pytest.approx(tracks["columns"], abs=1)
    for route in held:
        route.continue_()


def test_sort_direction_and_trash_original_location_work_in_both_views(server, desktop):
    _, page = desktop
    login(page, server.url)
    contents = page.request.post(
        server.url + "/api/getDirectory", data={"path": "/"}
    ).json()["data"]["contents"]
    source = next(x for x in contents.values() if x["name"] == "Preview samples")
    response = page.request.post(
        server.url + "/api/createNewFolder",
        data={"path": "/" + source["id"], "name": "Nested trash"},
    )
    assert response.ok
    nested = page.request.post(
        server.url + "/api/getDirectory", data={"path": "/" + source["id"]}
    ).json()["data"]["contents"]
    target = next(x for x in nested.values() if x["name"] == "Nested trash")
    assert page.request.post(
        server.url + "/api/trashFileFolder",
        data={"path": "/" + source["id"] + "/" + target["id"], "trash": True},
    ).ok
    page.get_by_role("link", name="Trash", exact=True).click()
    expect(
        page.get_by_text("Deleted from: My Files / Preview samples", exact=True)
    ).to_be_visible()
    choose_view(page, "Grid view")
    expect(
        page.get_by_text("Deleted from: My Files / Preview samples", exact=True)
    ).to_be_visible()
    page.get_by_role("link", name="My Files", exact=True).click()
    choose_view(page, "List view")
    page.get_by_role("button", name="Sort Files: Name", exact=True).click()
    page.get_by_role("menuitemradio", name="Descending", exact=True).click()
    names = page.locator(".file-table .item-name-button").all_text_contents()
    assert names == sorted(names, key=str.casefold, reverse=True)
    page.get_by_role("button", name="Sort by Name", exact=True).click()
    expect(page.locator(".file-table th").first).to_have_attribute(
        "aria-sort", "ascending"
    )
    search = page.get_by_role("searchbox", name="Search Files and Folders", exact=True)
    search.fill("sample")
    expect(
        page.get_by_role("heading", name="Search Results", exact=True)
    ).to_be_visible()
    page.get_by_role("button", name="Sort Files: Name", exact=True).click()
    with page.expect_response(
        lambda response: (
            "/api/v1/search?" in response.url
            and parse_qs(urlparse(response.url).query).get("order") == ["desc"]
        )
    ):
        page.get_by_role("menuitemradio", name="Descending", exact=True).click()
    choose_view(page, "Grid view")
    expect(page.get_by_role("list", name="Files and folders")).to_be_visible()


@pytest.mark.parametrize("view", ["Overview", "Server resources", "Tuning", "Accounts"])
def test_performance_content_separates_from_toolbar(server, desktop, view):
    _, page = desktop
    login(page, server.url + "/?view=performance")
    page.get_by_role("tab", name=view, exact=True).click()
    panel = page.get_by_role("tabpanel", name=view, exact=True)
    expect(panel).to_be_visible()
    first_content = panel.locator(":scope > *:visible").first
    expect(first_content).to_be_visible()
    gap = first_content.evaluate("""next => {
      const toolbar=document.querySelector('.performance-toolbar')
        .getBoundingClientRect();
      return next.getBoundingClientRect().top-toolbar.bottom;
    }""")
    # Each tab retains a clear gap below the toolbar.
    assert gap >= 12


@pytest.mark.parametrize("width", [390, 320])
def test_mobile_header_stays_compact_and_search_and_view_controls_work(
    server, desktop, width
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)
    expect(page.get_by_role("button", name="View and sort", exact=True)).to_be_visible()
    assert page.locator(".file-toolbar").bounding_box()["height"] <= 70
    search = page.get_by_role("searchbox", name="Search Files and Folders", exact=True)
    expect(search).not_to_be_visible()
    page.get_by_role("button", name="Search Files and Folders", exact=True).click()
    expect(search).to_be_focused()
    search.press("Escape")
    expect(search).not_to_be_visible()
    expect(
        page.get_by_role("button", name="Search Files and Folders", exact=True)
    ).to_be_focused()
    page.keyboard.press("Control+k")
    expect(search).to_be_focused()
    search.press("Escape")
    choose_view(page, "Grid view")
    expect(page.get_by_role("list", name="Files and folders")).to_be_visible()
    page.get_by_role("button", name="View and sort", exact=True).click()
    menu = page.get_by_role("menu", name="View and sort", exact=True)
    expect(
        menu.get_by_role("menuitemradio", name="Grid view", exact=True)
    ).to_have_attribute("aria-checked", "true")
    bounds = menu.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 844
    page.keyboard.press("End")
    expect(
        menu.get_by_role("menuitemradio", name="Descending", exact=True)
    ).to_be_focused()
    page.keyboard.press("Escape")
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    heading = page.get_by_role("heading", name="Preview samples", exact=True)
    expect(heading).to_be_visible()
    page.evaluate("document.fonts.ready")
    assert heading.evaluate("h => h.scrollHeight === h.clientHeight")


def test_grid_respects_data_saver(server, desktop):
    _, page = desktop
    page.add_init_script(
        "Object.defineProperty(navigator, 'connection', "
        "{value:{saveData:true},configurable:true})"
    )
    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.url)
            if "/api/v1/thumbnail?" in request.url
            else None
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    sample = page.locator(".file-row").filter(has_text="sample.png")
    sample.scroll_into_view_if_needed()
    fallback = sample.locator(".file-list-thumbnail .file-icon")
    expect(fallback).to_be_visible()
    bounds = fallback.bounding_box()
    assert bounds["width"] <= 36 and bounds["height"] <= 36
    assert not requests
    choose_view(page, "Grid view")
    expect(page.get_by_role("list", name="Files and folders")).to_be_visible()
    expect(page.locator(".file-tile-preview img")).to_have_count(0)
    page.get_by_role("button", name="sample.png", exact=True).focus()
    assert not requests


@pytest.mark.parametrize(
    "width,theme", [(1440, "light"), (390, "dark"), (320, "light")]
)
def test_item_status_is_quiet_metadata_without_interrupting_names(
    server, desktop, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(color_scheme=theme)
    login(page, server.url)
    contents = page.request.post(
        server.url + "/api/getDirectory", data={"path": "/"}
    ).json()["data"]["contents"]
    target = next(
        item for item in contents.values() if item["name"] == "Preview samples"
    )
    path = "/" + target["id"]
    assert page.request.patch(
        server.url + "/api/v1/starred", data={"path": path, "starred": True}
    ).ok
    assert page.request.post(
        server.url + "/api/v1/shares", data={"path": path, "expires_in_days": 7}
    ).ok
    page.reload()
    row = page.locator(".file-row").filter(
        has=page.get_by_role("button", name="Preview samples", exact=True)
    )
    status = row.locator(".file-item-status")
    expect(status).to_have_text("StarredShared")
    expect(status.locator("svg")).to_have_count(0)
    expect(status.get_by_title("Shared by Link", exact=True)).to_have_text("Shared")
    name = row.locator(".file-name").bounding_box()
    state = status.bounding_box()
    assert name and state
    if width > 720:
        assert state["x"] >= name["x"] + name["width"]
    else:
        assert state["y"] >= name["y"] + name["height"] - 1
    height = row.bounding_box()["height"]
    sibling = page.locator(".file-row").filter(
        has=page.get_by_role("button", name="Private folder", exact=True)
    )
    assert abs(height - sibling.bounding_box()["height"]) <= 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=ARTIFACTS / f"item-status-list-{theme}-{width}.png",
        animations="disabled",
    )
    choose_view(page, "Grid view")
    expect(status).to_be_visible()
    assert status.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=ARTIFACTS / f"item-status-grid-{theme}-{width}.png",
        animations="disabled",
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_grid_tab_has_one_focus_ring_and_stable_thumbnail(server, desktop, theme):
    _, page = desktop
    page.set_viewport_size({"width": 1536, "height": 695})
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance', '{theme}')")
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    choose_view(page, "Grid view")
    file = page.get_by_role("button", name="sample.json", exact=True)
    row = page.locator(".file-row").filter(has=file)
    tile = row.locator(".file-tile-preview")
    file.click()
    file.press("Tab")
    page.keyboard.press("Shift+Tab")
    expect(file).to_be_focused()
    expect(row).to_have_class("file-row selected")
    expect(row).to_have_css("outline-style", "solid")
    expect(file).to_have_css("outline-style", "none")
    expect(row).to_have_css("border-top-color", "rgba(0, 0, 0, 0)")
    expect(row).to_have_css("outline-offset", "-2px")
    expect(tile).to_have_css("transform", "none")
    assert page.evaluate("getSelection().isCollapsed")
    page.screenshot(path=ARTIFACTS / f"grid-file-focus-{theme}.png")


@pytest.mark.parametrize("view", ["List view", "Grid view"])
def test_mouse_modifiers_select_visible_file_ranges_and_individual_items(
    server, desktop, view
):
    _, page = desktop
    page.set_viewport_size({"width": 1536, "height": 695})
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    choose_view(page, view)
    files = page.locator(".file-row .item-name-button")
    chosen = page.locator(".file-row.selected .item-name-button")
    names = files.all_text_contents()

    files.nth(1).click()
    range_end = (
        page.locator(".file-row").nth(4).locator(".file-tile-preview")
        if view == "Grid view"
        else files.nth(4)
    )
    range_end.click(modifiers=["Shift"])
    expect(chosen).to_have_text(names[1:5])
    expect(page.get_by_text("4 selected", exact=True)).to_be_visible()
    files.nth(2).click(modifiers=["Shift"])
    expect(chosen).to_have_text(names[1:3])
    files.nth(4).click(modifiers=["Control"])
    expect(chosen).to_have_text([*names[1:3], names[4]])
    files.nth(4).click(modifiers=["Meta"])
    expect(chosen).to_have_text(names[1:3])
    files.nth(3).click(modifiers=["Control"])
    files.nth(4).click(modifiers=["Control", "Shift"])
    expect(chosen).to_have_text(names[1:5])

    files.nth(0).click()
    expect(chosen).to_have_text(names[:1])
    expect(page.get_by_role("button", name="Select", exact=True)).to_be_visible()
    files.nth(2).click(modifiers=["Control"])
    page.keyboard.press("Escape")
    expect(chosen).to_have_count(0)
    expect(files.nth(2)).to_be_focused()
    assert page.evaluate("getSelection().isCollapsed")


@pytest.mark.parametrize("width,view", [(1440, "Grid view"), (390, "List view")])
def test_escape_selection_returns_focus_to_file_control_not_workspace(
    server, desktop, width, view
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)
    choose_view(page, view)
    page.get_by_role("button", name="Select", exact=True).click()
    checkbox = page.get_by_role("checkbox", name="Select Preview samples", exact=True)
    checkbox.check()
    expect(page.get_by_text("1 selected", exact=True)).to_be_visible()
    checkbox.focus()
    page.keyboard.press("Escape")
    item = page.get_by_role("button", name="Preview samples", exact=True)
    expect(item).to_be_focused()
    expect(page.locator(".file-row.selected")).to_have_count(0)
    expect(page.locator(".file-list-scroll")).not_to_be_focused()
    assert not page.locator(".file-list-scroll").evaluate(
        "e => e.matches(':focus-visible')"
    )
    assert item.evaluate("e => e.matches(':focus-visible')")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=ARTIFACTS / f"selection-escape-focus-{width}.png", animations="disabled"
    )
    item.press("Enter")
    expect(
        page.get_by_role("heading", name="Preview samples", exact=True)
    ).to_be_visible()


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_new_browsers_open_files_in_list_with_type_filters(server, desktop, width):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)
    expect(page.get_by_role("columnheader", name="Sort by Name")).to_be_visible()
    assert page.evaluate("localStorage.getItem('tgdrive.file-view')") is None
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    expect(page.get_by_role("button", name="sample.png", exact=True)).to_be_visible()
    filters = page.get_by_role("group", name="Filter by type", exact=True)
    select = page.get_by_role("combobox", name="Type", exact=True)
    if width <= 480:
        expect(filters).not_to_be_visible()
        expect(select).to_have_text("All types")
        count = page.locator(".file-mobile-item-count")
        expect(count).to_be_visible()
        control_box = select.bounding_box()
        assert control_box["height"] >= 44
        assert control_box["x"] + control_box["width"] < count.bounding_box()["x"]
        select.press("Enter")
        options = page.get_by_role("listbox", name="File type", exact=True)
        expect(options).to_be_visible()
        expect(
            options.get_by_role("option", name="All types", exact=True)
        ).to_have_attribute("aria-selected", "true")
        expect(options.get_by_role("option", name="Other", exact=True)).to_be_visible()
        bounds = options.bounding_box()
        assert 0 <= bounds["x"] < bounds["x"] + bounds["width"] <= width
        assert 0 <= bounds["y"] < bounds["y"] + bounds["height"] <= 844
        page.keyboard.press("Escape")
        expect(select).to_be_focused()
        select.click()
        options.get_by_role("option", name="Audio", exact=True).click()
    else:
        expect(select).not_to_be_visible()
        filters.get_by_role("button", name="Audio", exact=True).click()
    expect(page.get_by_role("button", name="sample.wav", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="sample.png", exact=True)).to_have_count(0)
    if width <= 480:
        expect(select).to_have_text("Audio")
        expect(select).to_be_focused()
        select.click()
        page.get_by_role("option", name="All types", exact=True).click()
    else:
        filters.get_by_role("button", name="All", exact=True).click()
    expect(page.get_by_role("button", name="sample.png", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("view", ["List view", "Grid view"])
def test_touch_hold_selects_once_and_cancels_during_scroll(server, browser, view):
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
        is_mobile=True,
        reduced_motion="reduce",
    )
    try:
        page = context.new_page()
        login(page, server.url)
        page.get_by_role("button", name="Preview samples", exact=True).tap()
        choose_view(page, view)
        target = page.get_by_role("button", name="sample.json", exact=True)
        touch = context.new_cdp_session(page)

        def target_point():
            target.scroll_into_view_if_needed()
            box = target.bounding_box()
            assert box
            return {
                "x": box["x"] + min(24, box["width"] / 2),
                "y": box["y"] + box["height"] / 2,
            }

        def send(phase, *points):
            touch.send(
                "Input.dispatchTouchEvent", {"type": phase, "touchPoints": list(points)}
            )

        page.clock.install()
        done = page.get_by_role("button", name="Done", exact=True)
        point = target_point()
        send("touchStart", point)
        send("touchMove", {"x": point["x"] + 24, "y": point["y"]})
        page.clock.run_for(600)
        expect(done).to_have_count(0)
        send("touchCancel")
        expect(page.locator("dialog.file-preview")).to_have_count(0)

        send("touchStart", target_point())
        assert page.locator(".file-workspace").evaluate("""scroller => {
            const before = scroller.scrollTop;
            scroller.scrollTop += before > 20 ? -20 : 20;
            return scroller.scrollTop !== before;
        }""")
        page.clock.run_for(600)
        expect(done).to_have_count(0)
        send("touchCancel")

        send("touchStart", target_point())
        page.clock.run_for(600)
        checkbox = page.get_by_role("checkbox", name="Select sample.json", exact=True)
        expect(checkbox).to_be_checked()
        send("touchEnd")
        expect(checkbox).to_be_checked()
        expect(page.get_by_text("1 selected", exact=True)).to_be_visible()
        expect(page.locator("dialog.file-preview")).to_have_count(0)
        done.tap()
        target.tap()
        expect(page.locator("dialog.file-preview")).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("view", ["List view", "Grid view"])
def test_right_click_and_keyboard_use_the_same_item_actions(server, desktop, view):
    _, page = desktop
    page.set_viewport_size({"width": 1536, "height": 695})
    login(page, server.url)
    choose_view(page, view)
    active = page.get_by_role("button", name=view, exact=True)
    expect(active).to_have_attribute("aria-pressed", "true")
    expect(active).to_have_css("box-shadow", "none")
    folder = page.get_by_role("button", name="Preview samples", exact=True)
    folder.click(button="right")
    row = page.locator(".file-row").filter(has=folder)
    menu = row.locator(".action-menu")
    expect(menu.get_by_role("button", name="Rename", exact=True)).to_be_visible(
        timeout=2000
    )
    expect(row).to_have_class("file-row selected")
    assert page.get_by_role("heading", name="My Files", exact=True).is_visible()
    page.screenshot(path=ARTIFACTS / f"folder-context-{view.lower().split()[0]}.png")
    page.keyboard.press("Escape")
    expect(menu).not_to_be_visible()
    expect(folder).to_be_focused()
    folder.press("Enter")

    file = page.get_by_role("button", name="sample.json", exact=True)
    file.press("Shift+F10")
    row = page.locator(".file-row").filter(has=file)
    menu = row.locator(".action-menu")
    expect(menu.get_by_role("button", name="Open", exact=True)).to_be_focused()
    expect(menu.get_by_role("button", name="Move", exact=True)).to_be_visible()
    geometry = menu.bounding_box()
    assert geometry is not None
    assert geometry["x"] >= 0 and geometry["x"] + geometry["width"] <= 1536
    assert geometry["y"] >= 0 and geometry["y"] + geometry["height"] <= 695
    page.keyboard.press("Escape")
    expect(file).to_be_focused()
    row.get_by_label("Actions for sample.json", exact=True).click()
    expect(menu.get_by_role("button", name="Rename", exact=True)).to_be_visible()
    page.keyboard.press("Escape")

    page.get_by_role("button", name="Select", exact=True).click()
    for name in ("sample.json", "source.html"):
        page.get_by_role("checkbox", name="Select " + name, exact=True).check()
    third = page.get_by_role("button", name="reports.csv", exact=True)
    third.click(button="right")
    third_row = page.locator(".file-row").filter(has=third)
    expect(third_row.locator(".action-menu")).to_be_visible()
    assert page.get_by_role("checkbox", checked=True).count() == 2
    page.keyboard.press("Escape")
    expect(third).to_be_focused()
    assert page.get_by_role("checkbox", checked=True).count() == 2


def test_guest_context_menu_exposes_only_read_only_actions(server, desktop, browser):
    from urllib.parse import urlencode

    _, owner = desktop
    login(owner, server.url)
    folder = server.control()["seed"]["folder"]
    share = owner.request.post(
        server.url + "/api/v1/shares", data={"path": folder}
    ).json()
    url = (
        server.url
        + "/?"
        + urlencode(
            {"path": "/share_" + folder.rsplit("/", 1)[-1], "auth": share["auth"]}
        )
    )
    with browser.new_context() as context:
        page = context.new_page()
        page.goto(url)
        file = page.get_by_role("button", name="résumé #1.txt", exact=True)
        file.click(button="right")
        menu = page.locator(".file-row").filter(has=file).locator(".action-menu")
        expect(menu.get_by_role("button", name="Open", exact=True)).to_be_visible(
            timeout=2000
        )
        expect(menu.get_by_role("link", name="Download", exact=True)).to_be_visible()
        expect(menu.get_by_role("button")).to_have_count(1)
        expect(menu.get_by_role("link")).to_have_count(1)
        page.keyboard.press("Escape")
        expect(file).to_be_focused()
        expect(
            page.get_by_role("link", name="Download résumé #1.txt", exact=True)
        ).to_be_visible()


@pytest.mark.parametrize("width", [768, 820, 1024])
def test_tablet_type_filters_stay_reachable(server, desktop, width):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 940})
    theme = "dark" if width == 820 else "light"
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance', '{theme}');")
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")

    nav = page.get_by_role("button", name="Toggle Navigation", exact=True)
    if width <= 900:
        nav.press("Enter")
        expect(page.get_by_role("dialog", name="Drive navigation")).to_be_visible()
        page.keyboard.press("Escape")
        expect(nav).to_be_focused()
    else:
        expect(nav).not_to_be_visible()

    select = page.get_by_role("combobox", name="Type", exact=True)
    expect(select).to_have_text("All types")
    listbox = page.get_by_role("listbox", name="File type", exact=True)
    page.evaluate("document.fonts.ready")
    page.screenshot(path=ARTIFACTS / f"tablet-files-{width}.png", animations="disabled")
    select.press("Enter")
    expect(listbox).to_be_visible()
    page.screenshot(path=ARTIFACTS / f"tablet-filters-{width}.png", animations="disabled")
    names = listbox.get_by_role("option").all_text_contents()
    assert "Other" in names
    page.keyboard.press("End")
    page.keyboard.press("Enter")
    expect(select).to_have_text(names[-1])
    expect(select).to_be_focused()
    for name in names:
        select.click()
        option = listbox.get_by_role("option", name=name, exact=True)
        assert option.evaluate("""element => {
            const rect = element.getBoundingClientRect();
            return element.contains(document.elementFromPoint(
                rect.x + rect.width / 2, rect.y + rect.height / 2,
            ));
        }""")
        option.click()
        expect(select).to_have_text(name)
        expect(select).to_be_focused()


@pytest.mark.parametrize(
    "width, height, theme",
    [
        (1536, 695, "dark"),
        (1024, 695, "light"),
        (820, 1180, "dark"),
        (768, 1024, "light"),
        (390, 844, "light"),
    ],
)
def test_sort_labels_keep_file_toolbar_in_place(server, desktop, width, height, theme):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    page.add_init_script(
        f"localStorage.setItem('tgdrive.appearance', '{theme}');"
        "localStorage.setItem('tgdrive.file-view', 'grid');"
    )
    login(page, server.url)
    page.evaluate("document.fonts.ready")
    toolbar = page.locator(".file-toolbar")
    controls = toolbar.locator(".file-toolbar-controls")
    switch = toolbar.locator(".file-view-toggle")
    initial = [element.bounding_box() for element in (toolbar, controls, switch)]
    assert initial[0] and initial[1]
    label = "View and sort" if width < 721 else "Sort Files: Name"
    for sort in ("Date Added", "File Size", "Name"):
        toolbar.get_by_role("button", name=label, exact=True).click()
        page.get_by_role("menuitemradio", name=sort, exact=True).click()
        if width >= 721:
            label = "Sort Files: " + sort
        expect(toolbar.get_by_role("button", name=label, exact=True)).to_be_focused()
        assert [
            element.bounding_box() for element in (toolbar, controls, switch)
        ] == initial
        assert toolbar.evaluate("node => node.scrollWidth <= node.clientWidth")
