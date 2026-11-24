"""Synthetic password and rendering-resource cases use the shipped PDF reader."""

from __future__ import annotations

import time
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


@pytest.mark.parametrize("width,height", [(1536, 695), (820, 1180), (390, 844)])
def test_pdf_file_menu_can_queue_each_password_operation(
    server: Server, desktop, width, height
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    if width > 390:
        page.add_init_script("localStorage.setItem('tgdrive.appearance', 'dark')")
    requests = []

    def save(route):
        requests.append(route.request.post_data_json)
        route.fulfill(
            status=202, json={"id": "fixture-pdf-password", "job": {"name": "copy.pdf"}}
        )

    page.route("**/api/v1/pdfs/password", save)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    for operation in ("Add", "Change", "Remove"):
        page.get_by_label("Actions for sample.pdf", exact=True).click()
        page.get_by_role("button", name="PDF password", exact=True).click()
        dialog = page.get_by_role("dialog", name="PDF password", exact=True)
        action = dialog.get_by_label("Action", exact=True)
        expect(action).to_be_focused()
        action.select_option(label=operation + " password")
        assert action.evaluate("node => getComputedStyle(node).paddingTop") == "0px"
        expect(dialog.get_by_text("sample.pdf", exact=True)).to_be_visible()
        expect(
            dialog.get_by_text(
                "Unlocked copy" if operation == "Remove" else "Protected copy",
                exact=True,
            )
        ).to_be_visible()
        if operation != "Add":
            current = dialog.get_by_label("Current PDF password")
            assert (
                current.evaluate("node => getComputedStyle(node).borderTopWidth")
                == "0px"
            )
            current.fill("before")
        if operation != "Remove":
            dialog.get_by_label("New PDF password").fill("after")
        save_button = dialog.get_by_role("button", name="Save PDF copy")
        if operation == "Change":
            expect(save_button).to_be_in_viewport(ratio=1)
            page.screenshot(
                path=ARTIFACTS / f"pdf-password-copy-{width}.png",
                animations="disabled",
            )
        save_button.click()
        expect(dialog).not_to_be_visible()
        assert requests[-1]["operation"] == operation.lower()
        assert requests[-1].get("password") == (
            None if operation == "Add" else "before"
        )
        assert requests[-1].get("new_password") == (
            None if operation == "Remove" else "after"
        )
    assert "new_password" not in page.evaluate("JSON.stringify(localStorage)")


def test_pdf_change_retry_asks_for_both_passwords_without_reusing_them(server, desktop):
    _, page = desktop
    login(page, server.url)
    requested = []
    job = {
        "id": "fixture-pdf-change",
        "name": "sample (protected).pdf",
        "kind": "pdf_password",
        "status": "error",
        "phase": "rewriting_pdf",
        "archive_password_required": True,
        "pdf_password_operation": "change",
        "created_at": time.time() - 2,
        "finished_at": time.time(),
        "error": "The current PDF password is incorrect.",
    }
    page.route("**/api/v1/jobs", lambda route: route.fulfill(json={"jobs": [job]}))
    page.route(
        "**/api/v1/transfers/history/tasks",
        lambda route: route.fulfill(
            json={
                "jobs": [job],
                "page": 1,
                "page_size": 20,
                "total_tasks": 1,
                "total_pages": 1,
                "has_more": False,
            }
        ),
    )

    def retry(route):
        requested.append(route.request.post_data_json)
        job.update(status="running", error=None)
        route.fulfill(json=job)

    page.route("**/api/v1/jobs/fixture-pdf-change/retry", retry)
    page.goto(server.url + "/?view=activity")
    row = page.locator(".transfer-page .transfer-item").filter(has_text=job["name"])
    expect(row).to_contain_text("The current PDF password is incorrect.")
    row.get_by_role("button", name=f"Retry {job['name']}", exact=True).click()
    dialog = page.get_by_role("dialog", name="Retry changing PDF password", exact=True)
    current = dialog.get_by_label("Current PDF password")
    new = dialog.get_by_label("New PDF password")
    expect(current).to_have_value("")
    expect(new).to_have_value("")
    expect(current).to_be_focused()
    submit = dialog.get_by_role("button", name="Retry", exact=True)
    current.fill("correct-current")
    expect(submit).to_be_disabled()
    new.fill("replacement-password")
    submit.click()
    expect(dialog).not_to_be_visible()
    assert requested == [
        {"password": "correct-current", "new_password": "replacement-password"}
    ]
    stored = page.evaluate("JSON.stringify(localStorage)")
    assert "correct-current" not in stored and "replacement-password" not in stored


@pytest.mark.parametrize(
    "width,height,theme",
    [(1536, 695, "dark"), (820, 1180, "dark"), (390, 844, "light")],
)
def test_preview_options_do_not_select_labels_and_file_text_remains_selectable(
    server, desktop, width, height, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": height})
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance', '{theme}')")
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    dialog = open_sample(page, "sample.pdf")
    page_text = dialog.locator(
        '.pdf-page[data-page-number="1"] .pdf-text-layer'
    ).get_by_text("TGDrive PDF preview page 1", exact=True)
    expect(page_text).to_be_visible()
    trigger = dialog.get_by_role("button", name="PDF View Options", exact=True)
    trigger.click()
    fit = dialog.get_by_role("button", name="Fit Width", exact=True)
    expect(fit).to_be_visible()
    fit_select = fit.evaluate("node => getComputedStyle(node).userSelect")
    trigger_select = trigger.evaluate("node => getComputedStyle(node).userSelect")
    selection_bounds = fit.evaluate("""node => {
        const range = document.createRange();
        range.selectNodeContents(node.lastChild);
        return range.getBoundingClientRect().toJSON();
    }""")
    page.mouse.move(
        selection_bounds["x"] + 1,
        selection_bounds["y"] + selection_bounds["height"] / 2,
    )
    page.mouse.down()
    page.mouse.move(
        selection_bounds["right"] - 1,
        selection_bounds["y"] + selection_bounds["height"] / 2,
        steps=10,
    )
    page.mouse.up()
    page.screenshot(
        path=ARTIFACTS / f"control-text-selection-{width}.png", animations="disabled"
    )
    assert page.evaluate("getSelection().toString()") == ""
    assert fit_select == trigger_select == "none"
    if fit.is_visible():
        page.keyboard.press("Escape")
    expect(trigger).to_be_visible()
    page_text.scroll_into_view_if_needed()
    page_text.dblclick()
    assert page.evaluate("getSelection().toString().trim().length") > 0
    page.mouse.click(1, 1)
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()


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
    expect(dialog.get_by_label("Page", exact=True)).to_be_disabled()
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
