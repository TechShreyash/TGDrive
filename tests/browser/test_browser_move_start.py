import pytest

from tests.browser.test_browser_e2e import ARTIFACTS, expect, login

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def test_move_starts_in_source_folder_and_can_navigate_to_root(server, desktop):
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="資料", exact=True).dblclick()
    page.get_by_label("Actions for résumé #1.txt", exact=True).click()
    page.get_by_role("button", name="Move", exact=True).click()
    dialog = page.get_by_role("dialog")
    breadcrumb = dialog.get_by_role("navigation", name="Destination breadcrumb")
    expect(breadcrumb.get_by_role("button", name="資料")).to_be_visible()
    expect(dialog.get_by_role("button", name="Move Here")).to_be_disabled()
    dialog.get_by_role("button", name="Up to My Files").click()
    expect(dialog.get_by_role("button", name="Private folder")).to_be_visible()
    expect(dialog.get_by_role("button", name="Move Here")).to_be_enabled()


def test_move_shows_canonical_path_from_all_views(server, desktop):
    _, page = desktop
    login(page, server.url)
    server.control("/move-paths", method="POST")
    page.reload()
    for folder in ("資料", "Projects", "Planning"):
        page.get_by_role("button", name=folder, exact=True).dblclick()

    dialog = page.get_by_role("dialog")
    breadcrumb = dialog.get_by_role("navigation", name="Destination breadcrumb")
    for view in (None, "recent", "starred"):
        if view:
            page.goto(f"{server.url}/?view={view}")
        page.get_by_label("Actions for Agenda.txt", exact=True).click()
        page.get_by_role("button", name="Move", exact=True).click()
        expect(breadcrumb.get_by_role("button")).to_have_text(
            ["My Files", "資料", "Projects", "Planning"]
        )
        expect(dialog.get_by_role("button", name="Move Here")).to_be_disabled()
        expect(
            dialog.get_by_text("My Files / 資料 / Projects / Planning", exact=True)
        ).to_be_visible()
        breadcrumb.get_by_role("button", name="Projects", exact=True).click()
        expect(
            dialog.get_by_role("button", name="Planning", exact=True)
        ).to_be_visible()
        expect(dialog.get_by_role("button", name="Move Here")).to_be_enabled()
        dialog.get_by_role("button", name="Cancel", exact=True).click()

    page.get_by_role("button", name="Select", exact=True).click()
    for name in ("Agenda.txt", "private.txt"):
        page.get_by_role("checkbox", name=f"Select {name}", exact=True).check()
    page.get_by_role("button", name="Move", exact=True).click()
    expect(breadcrumb.get_by_role("button")).to_have_text(["My Files"])
    expect(dialog.get_by_text("Different folders", exact=True)).to_be_visible()


@pytest.mark.parametrize(
    "width,height,theme",
    [(1536, 695, "dark"), (820, 1180, "dark"), (390, 844, "light")],
)
def test_move_keeps_selection_when_destination_disappears(
    server, browser, width, height, theme
):
    fixture = server.control("/move-paths", method="POST")
    with browser.new_context(
        viewport={"width": width, "height": height}, has_touch=width < 1000
    ) as context:
        page = context.new_page()
        page.add_init_script(f"localStorage.setItem('tgdrive.appearance','{theme}')")
        login(page, server.url)
        page.goto(f"{server.url}/?view=starred")
        page.get_by_role("button", name="Select", exact=True).click()
        for name in ("Agenda.txt", "private.txt"):
            page.get_by_role("checkbox", name=f"Select {name}", exact=True).check()
        page.get_by_role("button", name="Move", exact=True).click()
        dialog = page.get_by_role("dialog")
        picker = dialog.locator(".folder-picker")
        crumbs = dialog.get_by_role("navigation", name="Destination breadcrumb")
        picker.get_by_role("button", name="Private folder", exact=True).click()
        picker.get_by_role("button", name="Planning", exact=True).click()
        expect(crumbs.get_by_role("button")).to_have_text(
            ["My Files", "Private folder", "Planning"]
        )
        move = dialog.get_by_role("button", name="Move Here", exact=True)
        expect(move).to_be_enabled()
        result = page.request.post(
            server.url + "/api/v1/nodes/bulk",
            data={"paths": [fixture["duplicate"]], "action": "trash"},
        )
        assert result.ok, result.text()
        move.click()
        expect(dialog.get_by_role("alert")).to_be_visible()
        page.screenshot(
            path=ARTIFACTS / f"move-recovery-{width}.png", animations="disabled"
        )
        expect(move).to_have_count(0)
        expect(dialog.get_by_role("heading", name="Move 2 items")).to_be_visible()
        recover = dialog.get_by_role("button", name="Choose another folder", exact=True)
        expect(recover).to_be_in_viewport(ratio=1)
        expect(
            dialog.get_by_role("button", name="Cancel", exact=True)
        ).to_be_in_viewport(ratio=1)
        recover.press("Enter")
        expect(
            crumbs.get_by_role("button", name="My Files", exact=True)
        ).to_be_focused()
        for name in ("資料", "Projects", "Planning"):
            picker.get_by_role("button", name=name, exact=True).click()
        expect(crumbs.get_by_role("button")).to_have_text(
            ["My Files", "資料", "Projects", "Planning"]
        )
        expect(move).to_be_enabled()
        move.click()
        expect(dialog).to_have_count(0)
        rows = page.request.get(
            server.url + "/api/v1/nodes", params={"parent_id": fixture["path"]}
        )
        assert rows.ok, rows.text()
        assert {item["name"] for item in rows.json()["items"]} == {
            "Agenda.txt",
            "private.txt",
        }


def test_move_retries_root_listing_without_losing_the_item(server, desktop):
    _, page = desktop
    login(page, server.url)
    fail_once = True

    def listing(route):
        nonlocal fail_once
        if fail_once and "type=folder" in route.request.url:
            fail_once = False
            route.fulfill(status=503, json={"detail": "Folder listing interrupted"})
        else:
            route.continue_()

    page.route("**/api/v1/nodes?**", listing)
    page.get_by_label("Actions for Private folder", exact=True).click()
    page.get_by_role("button", name="Move", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("alert")).to_have_text("Folder listing interrupted")
    dialog.get_by_role("button", name="Try again", exact=True).click()
    expect(dialog.get_by_role("heading", name="Move “Private folder”")).to_be_visible()
    expect(dialog.get_by_role("button", name="資料", exact=True)).to_be_visible()
    expect(dialog.get_by_role("alert")).to_have_count(0)
