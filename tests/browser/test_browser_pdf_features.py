"""Synthetic password and rendering-resource cases use the shipped PDF reader."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    Server,
    assert_pdf_canvas_budget,
    assert_rendered_pdf_page,
    expect,
    login,
    preview_dialog,
)
from tests.browser.test_browser_e2e import browser as browser_fixture
from tests.browser.test_browser_e2e import desktop as desktop_fixture
from tests.browser.test_browser_e2e import server as server_fixture
from tests.support.book_samples import PDF_SAMPLE_PASSWORD

pytestmark = pytest.mark.e2e
browser, desktop, server = browser_fixture, desktop_fixture, server_fixture


def open_sample(page, name):
    page.get_by_role("button", name=name, exact=True).dblclick()
    return preview_dialog(page)


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_pdf_password_retry_cancel_and_reopen_keep_the_password_in_memory(
    server: Server, desktop, width, theme
):
    server.control("/pdf/features", method="POST")
    context, page = desktop
    context.add_init_script(f"localStorage.setItem('tgdrive.appearance', '{theme}')")
    page.set_viewport_size({"width": width, "height": 900})
    requests = []
    messages = []
    page.on(
        "request", lambda request: requests.append((request.url, request.post_data))
    )
    page.on("console", lambda message: messages.append(message.text))
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()

    name = "password-protected.pdf"
    dialog = open_sample(page, name)
    password = dialog.get_by_label("PDF Password", exact=True)
    expect(password).to_be_visible()
    expect(password).to_be_focused()
    expect(password).to_have_attribute("type", "password")
    expect(password).to_have_value("")
    expect(dialog.locator(".pdf-controls select")).to_contain_text("Password Required")
    expect(dialog.locator(".pdf-zoom")).to_have_text("—")
    assert dialog.locator(".pdf-password").evaluate(
        "form => form.getBoundingClientRect().width <= innerWidth - 16"
    )
    page.screenshot(path=str(ARTIFACTS / f"pdf-password-{theme}-{width}-required.png"))
    for attempt in range(2):
        password.fill("incorrect-fixture-password-" + str(attempt))
        dialog.get_by_role("button", name="Open PDF", exact=True).click()
        expect(dialog.get_by_role("alert")).to_contain_text("Incorrect password")
        expect(password).to_have_value("")
        expect(password).to_be_focused()
        expect(password).to_have_attribute("aria-invalid", "true")
    page.screenshot(path=str(ARTIFACTS / f"pdf-password-{theme}-{width}-incorrect.png"))
    password.fill(PDF_SAMPLE_PASSWORD)
    dialog.get_by_role("button", name="Open PDF", exact=True).click()
    assert_rendered_pdf_page(page, name, 1)
    expect(dialog.locator(".pdf-text-layer")).to_contain_text(
        "Encrypted PDF fixture opened"
    )
    assert_pdf_canvas_budget(dialog)
    page.screenshot(path=str(ARTIFACTS / f"pdf-password-{theme}-{width}-opened.png"))
    dialog.get_by_role("button", name="Close Preview", exact=True).click()

    dialog = open_sample(page, name)
    password = dialog.get_by_label("PDF Password", exact=True)
    expect(password).to_have_value("")
    expect(password).to_be_focused()
    password.fill(PDF_SAMPLE_PASSWORD)
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("PDF opening cancelled")
    expect(dialog.locator(".preview-pdf-error")).to_be_focused()
    expect(dialog.locator(".pdf-password")).to_have_count(0)
    expect(
        dialog.get_by_role("link", name="Download Original", exact=True)
    ).to_be_visible()
    page.screenshot(path=str(ARTIFACTS / f"pdf-password-{theme}-{width}-cancelled.png"))
    dialog.get_by_role("button", name="Close Preview", exact=True).click()

    dialog = open_sample(page, name)
    expect(dialog.get_by_label("PDF Password", exact=True)).to_have_value("")
    expect(dialog.get_by_label("PDF Password", exact=True)).to_be_focused()
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    stored = page.evaluate(
        "() => JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})"
    )
    assert PDF_SAMPLE_PASSWORD not in stored
    assert all(PDF_SAMPLE_PASSWORD not in url + (body or "") for url, body in requests)
    assert all(PDF_SAMPLE_PASSWORD not in message for message in messages)
    file_requests = [url for url, _ in requests if urlsplit(url).path == "/file"]
    assert file_requests


def test_pdf_fonts_cmaps_and_cmyk_resources_render_from_local_assets(
    server: Server, desktop
):
    server.control("/pdf/features", method="POST")
    _, page = desktop
    resources = []
    request_urls = []

    def response_seen(response):
        path = urlsplit(response.url).path
        if path.startswith("/static/pdfjs/"):
            resources.append((path, response.status))

    page.on("request", lambda request: request_urls.append(request.url))
    page.on("response", response_seen)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    name = "local-resources.pdf"
    dialog = open_sample(page, name)
    assert_rendered_pdf_page(page, name, 1)
    expect(dialog.locator(".pdf-text-layer")).to_contain_text(
        "Local PDF rendering resources"
    )
    expect(
        dialog.locator(".pdf-text-layer").get_by_text("A", exact=True)
    ).to_have_count(1)
    canvas = dialog.locator('.pdf-page[data-page-number="1"] canvas')
    assert canvas.evaluate("""canvas => {
        const scale = canvas.width / 420;
        const pixels = canvas.getContext('2d').getImageData(
            Math.floor(22 * scale), Math.floor(126 * scale),
            Math.ceil(35 * scale), Math.ceil(34 * scale)).data;
        let ink = 0;
        for (let index = 0; index < pixels.length; index += 4) {
            if (pixels[index] < 80 && pixels[index + 1] < 80 &&
                pixels[index + 2] < 80) ink++;
        }
        return ink > 50;
    }"""), "The packed CMap glyph must paint, not merely appear in selectable text"
    assert canvas.evaluate("""canvas => {
        const pixels = canvas.getContext('2d')
            .getImageData(0, 0, canvas.width, canvas.height).data;
        let red = 0;
        for (let index = 0; index < pixels.length; index += 4) {
            if (pixels[index] > 100 && pixels[index + 1] < 120 &&
                pixels[index + 2] < 120) red++;
        }
        return red > 1000;
    }"""), "The CMYK patch must render; selectable text alone is insufficient"
    assert resources
    assert all(status == 200 for _, status in resources)
    assert {path.split("/")[4] for path, _ in resources} == {
        "standard_fonts",
        "cmaps",
        "wasm",
        "iccs",
    }
    assert any(path.endswith("qcms_bg.wasm") for path, _ in resources)
    assert any(path.endswith("CGATS001Compat-v2-micro.icc") for path, _ in resources)
    origin = urlsplit(server.url)
    assert all(
        (urlsplit(url).scheme, urlsplit(url).netloc) == (origin.scheme, origin.netloc)
        for url in request_urls
        if urlsplit(url).scheme in {"http", "https"}
    )
    assert_pdf_canvas_budget(dialog)
    page.screenshot(path=str(ARTIFACTS / "pdf-local-resources.png"))
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
