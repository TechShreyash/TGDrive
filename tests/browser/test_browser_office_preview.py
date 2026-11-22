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

    held = []
    page.route("**/assets/OfficePreview-*.js", lambda route: held.append(route))
    with page.expect_request("**/assets/OfficePreview-*.js"):
        page.get_by_role("button", name="Meeting.docx", exact=True).dblclick()
    dialog = preview_dialog(page)
    expect(
        dialog.get_by_role("status", name="", exact=True).filter(
            has_text="Loading Office viewer"
        )
    ).to_be_visible()
    expect(dialog.get_by_role("button", name="Close Preview")).to_be_enabled()
    assert held
    for route in held:
        route.continue_()
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
    held = []
    forecast = "**/api/v1/books?*entry=1"
    page.route(forecast, lambda route: held.append(route))
    with page.expect_request(forecast):
        worksheet.select_option(label="Forecast")
    expect(dialog.get_by_role("status")).to_contain_text("Reading worksheet")
    worksheet.select_option(label="Wide")
    expect(dialog.get_by_role("table", name="Wide")).to_be_visible()
    held[0].fulfill(status=503, body="Old request")
    expect(dialog.get_by_role("table", name="Wide")).to_be_visible()
    expect(dialog.get_by_role("alert")).to_have_count(0)
    page.unroute(forecast)
    page.route(forecast, lambda route: route.fulfill(status=503, body="Try again"))
    worksheet.select_option(label="Forecast")
    expect(dialog.get_by_role("alert")).to_contain_text("Forecast")
    expect(
        dialog.get_by_role("link", name="Download original", exact=True)
    ).to_be_visible()
    page.unroute(forecast)
    dialog.get_by_role("button", name="Try again").click()
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


@pytest.mark.parametrize(
    "width,height,touch,theme",
    [(1536, 695, False, "dark"), (820, 1180, True, "light"), (390, 844, True, "dark")],
)
def test_presentation_and_markdown_previews(
    server, browser, width, height, touch, theme
):
    server.control("/office", method="POST")
    with browser.new_context(
        viewport={"width": width, "height": height}, has_touch=touch
    ) as context:
        page = context.new_page()
        page.add_init_script(f"localStorage.setItem('tgdrive.appearance','{theme}')")
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        login(page, server.url)

        def open_item(name):
            row = page.get_by_role("button", name=name, exact=True)
            if touch:
                row.tap()
            else:
                row.press("Enter")

        open_item("Preview samples")
        open_item("Briefing.pptx")
        dialog = preview_dialog(page)
        previous = dialog.get_by_role("button", name="Previous slide")
        next_slide = dialog.get_by_role("button", name="Next slide")
        expect(dialog.get_by_text("Slide 1 of 2", exact=True)).to_be_visible()
        expect(dialog.get_by_text("Team briefing", exact=True)).to_be_visible()
        expect(
            dialog.get_by_role("link", name="Download original", exact=True)
        ).to_be_visible()
        expect(dialog.get_by_role("img", name="Image 1 on slide 1")).to_be_visible()
        expect(previous).to_be_disabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=ARTIFACTS / f"presentation-{width}-{theme}.png")

        if touch:
            next_slide.tap()
        else:
            next_slide.press("Enter")
        expect(dialog.get_by_text("Rollout checklist", exact=True)).to_be_visible()
        expect(next_slide).to_be_disabled()
        previous.press("Enter")
        expect(dialog.get_by_text("Team briefing", exact=True)).to_be_visible()
        page.keyboard.press("Escape")

        open_item("Open budget.ods")
        expect(dialog.get_by_role("cell", name="12", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        open_item("Open notes.odt")
        expect(dialog.get_by_role("heading", name="Open document")).to_be_visible()
        page.keyboard.press("Escape")
        open_item("Open slides.odp")
        expect(dialog.get_by_text("Open presentation", exact=True)).to_be_visible()
        expect(dialog.get_by_text("Presenter cue", exact=True)).to_have_count(0)
        page.keyboard.press("Escape")

        open_item("Read me.md")
        expect(dialog.get_by_role("heading", name="Local setup")).to_be_visible()
        expect(
            dialog.get_by_role("listitem").filter(has_text="Start the server")
        ).to_be_visible()
        expect(dialog.locator(".markdown-preview strong")).to_have_text("Docker")
        expect(
            dialog.locator(".markdown-preview img, .markdown-preview script")
        ).to_have_count(0)
        assert not any("example.invalid" in url for url in requests)
        page.screenshot(path=ARTIFACTS / f"markdown-{width}-{theme}.png")
        source = dialog.get_by_role("button", name="Show source")
        source.click()
        expect(source).to_have_attribute("aria-pressed", "true")
        expect(
            dialog.get_by_role("region", name="Source of Read me.md")
        ).to_contain_text("# Local setup")


def test_protected_guest_can_preview_office_and_markdown_until_revoked(
    server, desktop, browser
):
    server.control("/office", method="POST")
    _, owner = desktop
    login(owner, server.url)
    owner.get_by_label("Actions for Preview samples", exact=True).click()
    owner.get_by_role("button", name="Share Link", exact=True).click()
    form = owner.get_by_role("dialog")
    password = "office-browser-fixture-password"
    form.get_by_label("Share password (optional)", exact=True).fill(password)
    form.get_by_role("button", name="Create Share Link", exact=True).click()
    share_url = form.get_by_label("Share Link", exact=True).input_value()
    with browser.new_context(viewport={"width": 390, "height": 844}) as guest:
        page = guest.new_page()
        page.goto(share_url)
        page.get_by_label("Share password", exact=True).fill(password)
        page.get_by_role("button", name="Open shared link", exact=True).click()
        samples = [
            ("Meeting.docx", "Meeting notes"),
            ("Budget.xlsx", "Gross income"),
            ("Briefing.pptx", "Team briefing"),
            ("Open notes.odt", "Open document"),
            ("Open budget.ods", "12"),
            ("Open slides.odp", "Open presentation"),
            ("Read me.md", "Local setup"),
        ]
        for name, text in samples:
            page.get_by_role("button", name=name, exact=True).press("Enter")
            preview = preview_dialog(page)
            expect(preview.get_by_text(text, exact=True)).to_be_visible()
            page.keyboard.press("Escape")
            expect(preview).to_have_count(0)

        links = owner.request.get(server.url + "/api/v1/shares").json()["shares"]
        assert len(links) == 1
        assert owner.request.delete(
            server.url + "/api/v1/shares", data={"share_id": links[0]["id"]}
        ).ok
        page.reload()
        expect(page.get_by_text("This link has expired", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="Briefing.pptx")).to_have_count(0)


def test_slide_navigation_ignores_late_results_and_closes_while_loading(
    server, desktop
):
    server.control("/office", method="POST")
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    presentation = page.get_by_role("button", name="Open slides.odp", exact=True)
    presentation.press("Enter")
    dialog = preview_dialog(page)
    expect(dialog.get_by_text("Open presentation", exact=True)).to_be_visible()

    next_slide = dialog.get_by_role("button", name="Next slide")
    second_request = "**/api/v1/books?*entry=1"
    held = []
    page.route(second_request, lambda route: held.append(route))
    with page.expect_request(second_request):
        next_slide.press("Enter")
    expect(dialog.get_by_text("Slide 1 of 4", exact=True)).to_be_visible()
    expect(
        dialog.get_by_role("status").filter(has_text="Reading slide 2")
    ).to_be_visible()
    next_slide.press("Enter")
    expect(dialog.get_by_text("Slide 3 of 4", exact=True)).to_be_visible()
    picture = dialog.get_by_role("img", name="Image 1 on slide 3")
    expect(picture).to_be_visible()
    assert picture.evaluate("node => node.complete && node.naturalWidth > 0")
    held.pop().fulfill(status=503)
    expect(dialog.get_by_role("alert")).to_have_count(0)
    expect(picture).to_be_visible()

    next_slide.press("Enter")
    expect(
        dialog.get_by_text("This slide has no previewable text or images.")
    ).to_be_visible()
    previous_slide = dialog.get_by_role("button", name="Previous slide")
    previous_slide.press("Enter")
    expect(picture).to_be_visible()
    with page.expect_request(second_request):
        previous_slide.press("Enter")
    expect(
        dialog.get_by_role("status").filter(has_text="Reading slide 2")
    ).to_be_visible()
    dialog.get_by_role("button", name="Close Preview").press("Enter")
    expect(dialog).to_have_count(0)
    expect(presentation).to_be_focused()
    held.pop().fulfill(status=503)
    page.unroute(second_request)
    presentation.press("Enter")
    expect(dialog.get_by_text("Open presentation", exact=True)).to_be_visible()
    expect(dialog.get_by_role("alert")).to_have_count(0)
