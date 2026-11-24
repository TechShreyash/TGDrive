import pytest

from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    expect,
    file_details_dialog,
    login,
    preview_dialog,
)

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


@pytest.mark.parametrize(
    "width,height,touch,theme",
    [(1536, 695, False, "dark"), (820, 1180, True, "light"), (390, 844, True, "dark")],
)
def test_markdown_preview(server, browser, width, height, touch, theme):
    server.control("/document-samples", method="POST")
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
        open_item("Read me.md")
        dialog = preview_dialog(page)
        expect(dialog.get_by_role("heading", name="Local setup")).to_be_visible()
        expect(
            dialog.get_by_role("listitem").filter(has_text="Start the server")
        ).to_be_visible()
        expect(dialog.locator(".markdown-preview strong")).to_have_text("Docker")
        expect(
            dialog.locator(".markdown-preview img, .markdown-preview script")
        ).to_have_count(0)
        assert not any("example.invalid" in url for url in requests)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=ARTIFACTS / f"markdown-{width}-{theme}.png")
        source = dialog.get_by_role("button", name="Show source")
        source.click()
        expect(source).to_have_attribute("aria-pressed", "true")
        expect(
            dialog.get_by_role("region", name="Source of Read me.md")
        ).to_contain_text("# Local setup")


def test_protected_guest_can_preview_markdown_and_download_documents_until_revoked(
    server, desktop, browser
):
    server.control("/document-samples", method="POST")
    _, owner = desktop
    login(owner, server.url)
    owner.get_by_label("Actions for Preview samples", exact=True).click()
    owner.get_by_role("button", name="Share Link", exact=True).click()
    form = owner.get_by_role("dialog")
    password = "document-browser-fixture-password"
    form.get_by_label("Share password (optional)", exact=True).fill(password)
    form.get_by_role("button", name="Create Share Link", exact=True).click()
    share_url = form.get_by_label("Share Link", exact=True).input_value()
    with browser.new_context(viewport={"width": 390, "height": 844}) as guest:
        page = guest.new_page()
        page.goto(share_url)
        page.get_by_label("Share password", exact=True).fill(password)
        page.get_by_role("button", name="Open shared link", exact=True).click()
        page.get_by_role("button", name="Read me.md", exact=True).press("Enter")
        preview = preview_dialog(page)
        expect(preview.get_by_text("Local setup", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        expect(preview).to_have_count(0)

        page.get_by_role("button", name="Sample.ppt", exact=True).press("Enter")
        details, _ = file_details_dialog(page, "Sample.ppt")
        expect(
            details.get_by_text(
                "This format does not have a browser preview.", exact=False
            )
        ).to_be_visible()
        with page.expect_download() as result:
            details.get_by_role("link", name="Download Original", exact=True).click()
        assert result.value.suggested_filename == "Sample.ppt"
        assert result.value.path().read_bytes() == b"Original ppt bytes"
        page.keyboard.press("Escape")

        links = owner.request.get(server.url + "/api/v1/shares").json()["shares"]
        assert len(links) == 1
        assert owner.request.delete(
            server.url + "/api/v1/shares", data={"share_id": links[0]["id"]}
        ).ok
        page.reload()
        expect(page.get_by_text("This link has expired", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="Sample.ppt")).to_have_count(0)


def test_unsupported_documents_offer_original_download(server, desktop):
    _, page = desktop
    server.control("/document-samples", method="POST")
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    for suffix in (
        "doc",
        "docx",
        "odt",
        "ppt",
        "pptx",
        "odp",
        "xls",
        "xlsx",
        "ods",
        "epub",
        "cbz",
        "cbr",
    ):
        name = "Sample." + suffix
        page.get_by_role("button", name=name, exact=True).press("Enter")
        dialog, _ = file_details_dialog(page, name)
        expect(
            dialog.get_by_text(
                "This format does not have a browser preview.", exact=False
            )
        ).to_be_visible()
        download = dialog.get_by_role("link", name="Download Original", exact=True)
        with page.expect_download() as result:
            download.click()
        assert result.value.suggested_filename == name
        assert result.value.path().read_bytes() == f"Original {suffix} bytes".encode()
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
