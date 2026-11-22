import pytest

from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    Server,
    expect,
    login,
    preview_dialog,
)

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def test_office_previews_read_documents_and_switch_sheets(server: Server, desktop):
    _, page = desktop
    login(page, server.url)
    server.control("/office", method="POST")
    page.reload()
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()

    page.get_by_role("button", name="Meeting.docx", exact=True).dblclick()
    dialog = preview_dialog(page)
    expect(dialog.get_by_role("heading", name="Meeting notes")).to_be_visible()
    expect(dialog.get_by_text("Review <draft>", exact=True)).to_be_visible()
    page.keyboard.press("Escape")

    page.get_by_role("button", name="Budget.xlsx", exact=True).dblclick()
    expect(dialog.get_by_role("cell", name="Gross income")).to_be_visible()
    worksheet = dialog.get_by_label("Worksheet", exact=True)
    surface = dialog.locator(".preview-surface")
    worksheet_bounds = worksheet.bounding_box()
    surface_bounds = surface.bounding_box()
    assert worksheet_bounds is not None and surface_bounds is not None
    assert worksheet_bounds["y"] - surface_bounds["y"] < 64
    worksheet.select_option(label="Forecast")
    expect(dialog.get_by_role("cell", name="Next quarter")).to_be_visible()
    expect(dialog.get_by_role("cell", name="42", exact=True)).to_be_visible()
    page.keyboard.press("Escape")

    page.get_by_role("button", name="Damaged.docx", exact=True).dblclick()
    expect(dialog.get_by_role("alert")).to_be_visible()
    with page.expect_download() as result:
        dialog.get_by_role("link", name="Download original", exact=True).click()
    assert result.value.suggested_filename == "Damaged.docx"


@pytest.mark.parametrize(
    "width,height,touch,theme",
    [(1536, 695, False, "dark"), (820, 1180, True, "dark"), (390, 844, True, "light")],
)
def test_office_wide_and_empty_sheets_keep_controls_and_reader_usable(
    server, browser, width, height, touch, theme
):
    server.control("/office", method="POST")
    with browser.new_context(
        viewport={"width": width, "height": height}, has_touch=touch
    ) as context:
        page = context.new_page()
        page.add_init_script(f"localStorage.setItem('tgdrive.appearance','{theme}')")
        login(page, server.url)
        folder = page.get_by_role("button", name="Preview samples", exact=True)
        if touch:
            folder.tap()
            page.get_by_role("button", name="Budget.xlsx", exact=True).tap()
        else:
            folder.press("Enter")
            page.get_by_role("button", name="Budget.xlsx", exact=True).press("Enter")
        dialog = preview_dialog(page)
        worksheet = dialog.get_by_label("Worksheet", exact=True)
        worksheet.select_option(label="Wide")
        cells = dialog.get_by_role("region", name="Wide worksheet cells", exact=True)
        cell = cells.get_by_role("cell", name="long_value_" * 35, exact=True)
        expect(cell).to_be_visible()
        assert cell.bounding_box()["width"] <= 240
        assert cell.evaluate("node => node.scrollWidth <= node.clientWidth")
        expect(cells).to_have_attribute("tabindex", "0")
        assert cells.evaluate("node => node.scrollWidth > node.clientWidth")
        cells.focus()
        cells.press("ArrowRight")
        page.wait_for_function("() => document.activeElement.scrollLeft > 0")
        expect(worksheet).to_have_value("2")
        expect(
            dialog.get_by_role("link", name="Download Original", exact=True)
        ).to_be_visible()
        page.screenshot(
            path=ARTIFACTS / f"office-wide-{width}.png", animations="disabled"
        )
        last = cells.get_by_role("cell", name="Last column", exact=True)
        last.scroll_into_view_if_needed()
        expect(last).to_be_in_viewport()
        worksheet.focus()
        worksheet.press("End")
        expect(worksheet).to_have_value("3")
        expect(
            dialog.get_by_text("This worksheet is empty.", exact=True)
        ).to_be_visible()
        expect(dialog.get_by_role("alert")).to_have_count(0)
        page.keyboard.press("Escape")

        report = page.get_by_role("button", name="Long notes.docx", exact=True)
        if touch:
            report.tap()
        else:
            report.press("Enter")
        heading = dialog.get_by_role("heading", name="LongHeading" * 20, exact=True)
        expect(heading).to_be_visible()
        assert heading.evaluate("node => node.scrollWidth <= node.clientWidth")
        dialog.get_by_text("End of notes", exact=True).scroll_into_view_if_needed()
        expect(dialog.get_by_text("End of notes", exact=True)).to_be_in_viewport()
