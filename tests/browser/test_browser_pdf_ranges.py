"""Large real PDFs use authorized sparse HTTP bytes, never a full-file warmup."""

from __future__ import annotations

import json
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

pytestmark = pytest.mark.e2e
browser, desktop, server = browser_fixture, desktop_fixture, server_fixture


@pytest.mark.parametrize("kind", ["single", "split", "shared", "shared-filtered"])
def test_large_pdf_first_page_and_distant_page_need_only_sparse_ranges(
    server: Server, desktop, kind
):
    data = server.control(
        "/pdf/large"
        + (
            "?shared=1" + ("&filtered=1" if kind == "shared-filtered" else "")
            if kind.startswith("shared")
            else ""
        ),
        method="POST",
    )
    assert data["size"] > 30 * 1024**2
    context, page = desktop
    cdp = context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
    requests, finished = [], []

    def is_pdf(request):
        return urlsplit(request.url).path == "/file" and "preview=1" in request.url

    def completed(request):
        if is_pdf(request):
            response = request.response()
            finished.append(
                {
                    "range": request.headers.get("range"),
                    "status": response.status,
                    "bytes": int(response.headers.get("content-length", "0")),
                }
            )

    page.on(
        "request", lambda request: requests.append(request) if is_pdf(request) else None
    )
    page.on("requestfinished", completed)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    name = "split-images.pdf" if kind == "split" else "large-images.pdf"
    page.get_by_role("button", name=name, exact=True).dblclick()
    dialog = preview_dialog(page)
    assert_rendered_pdf_page(page, name, 1)
    if kind.startswith("shared"):
        canvas = dialog.locator('.pdf-page[data-page-number="1"] canvas')
        assert canvas.evaluate("""canvas => {
          const ctx = canvas.getContext('2d');
          const pixels = ctx.getImageData(
            0, 0, canvas.width, canvas.height).data;
          let color = 0;
          for(let i = 0; i < pixels.length; i += 16) {
            const channels = [pixels[i], pixels[i+1], pixels[i+2]];
            if (Math.max(...channels) - Math.min(...channels) > 40) color++;
          }
          return color > 1000;
        }"""), "Rendered text alone must not hide a missing image or soft mask"
    page.wait_for_function(
        "() => [...document.querySelectorAll('.pdf-page')].every("
        "e => e.getAttribute('aria-busy') === 'false')"
    )
    initial = sum(row["bytes"] for row in finished)
    assert 0 < initial < data["size"] // 3
    before = len(requests)
    page.wait_for_timeout(350)
    assert len(requests) == before, "Idle viewing must not auto-fetch the remaining PDF"
    dialog.locator(".pdf-scroll").evaluate("e => { e.scrollTop = e.scrollHeight; }")
    assert_rendered_pdf_page(page, name, 48)
    expect(
        dialog.locator('.pdf-page[data-page-number="48"] .pdf-text-layer')
    ).to_contain_text("TGDrive large PDF page 48")
    page.wait_for_function(
        "() => [...document.querySelectorAll('.pdf-page')].every("
        "e => e.getAttribute('aria-busy') === 'false')"
    )
    assert all(
        request.headers.get("range", "").startswith("bytes=") for request in requests
    )
    assert all(row["status"] == 206 for row in finished)
    assert sum(row["bytes"] for row in finished) < data["size"] * 2 // 3
    assert any(
        int(request.headers["range"].split("=")[1].split("-")[0]) > data["size"] // 2
        for request in requests
    )
    assert_pdf_canvas_budget(dialog)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"pdf-ranges-{kind}.json").write_text(
        json.dumps(
            {
                "source_bytes": data["size"],
                "initial_bytes": initial,
                "ranges": finished,
            },
            indent=2,
        )
    )
    page.screenshot(path=str(ARTIFACTS / f"pdf-ranges-{kind}.png"))
    dialog.get_by_role("button", name="Close Preview", exact=True).click()


def test_pdf_ignored_range_fails_without_full_download(server: Server, desktop):
    _, page = desktop
    calls = []

    def ignore_range(route):
        calls.append(route.request.headers.get("range"))
        route.fulfill(
            status=200,
            body=b"%PDF-1.4\nwrong range response",
            content_type="application/pdf",
        )

    page.route("**/file?**", ignore_range)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="sample.pdf", exact=True).dblclick()
    dialog = preview_dialog(page)
    expect(dialog.get_by_role("alert")).to_contain_text("needs byte-range responses")
    expect(
        dialog.get_by_role("link", name="Download Original", exact=True)
    ).to_be_visible()
    assert calls and all(calls)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()


def test_pdf_close_aborts_active_range_reads(server: Server, desktop):
    _, page = desktop
    held = []
    page.route("**/file?**", lambda route: held.append(route))
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    with page.expect_request(lambda request: urlsplit(request.url).path == "/file"):
        page.get_by_role("button", name="sample.pdf", exact=True).dblclick()
    dialog = preview_dialog(page)
    expect(dialog.get_by_role("status")).to_contain_text("Loading PDF")
    assert held, "A real explicit range must be pending before cancellation"
    with page.expect_event(
        "requestfailed", predicate=lambda request: urlsplit(request.url).path == "/file"
    ) as cancelled:
        dialog.get_by_role("button", name="Close Preview", exact=True).click()
    assert "ABORTED" in cancelled.value.failure
    expect(page.locator("dialog.file-preview")).to_have_count(0)
    for route in held:
        route.abort()


def test_shared_pdf_rechecks_access_for_uncached_pages_after_revocation(
    server: Server, desktop, browser
):
    data = server.control("/pdf/large", method="POST")
    _, owner = desktop
    login(owner, server.url)
    folder = data["split"].rsplit("/", 1)[0]
    response = owner.request.post(
        server.url + "/api/v1/shares", data={"path": folder, "expires_in_days": 7}
    )
    assert response.ok
    token = response.json()["auth"]
    public_context = browser.new_context(viewport={"width": 390, "height": 844})
    try:
        public = public_context.new_page()
        public.goto(
            server.url + "/?path=/share_" + folder.lstrip("/") + "&auth=" + token
        )
        public.get_by_role("button", name="split-images.pdf", exact=True).dblclick()
        dialog = preview_dialog(public)
        assert_rendered_pdf_page(public, "split-images.pdf", 1)
        assert owner.request.delete(
            server.url + "/api/v1/shares", data={"path": folder}
        ).ok
        dialog.locator(".pdf-scroll").evaluate("e => { e.scrollTop = e.scrollHeight; }")
        expect(dialog.get_by_role("alert")).to_contain_text(
            "access has expired", timeout=15000
        )
        expect(
            dialog.get_by_role("link", name="Download Original", exact=True)
        ).to_be_visible()
    finally:
        public_context.close()
