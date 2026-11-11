"""Page-only PDF resume keeps owner and anonymous reading separate."""

from __future__ import annotations

import time

import pytest

from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    assert_pdf_canvas_budget,
    assert_rendered_pdf_page,
    expect,
    login,
    preview_dialog,
)

pytestmark = pytest.mark.e2e
pytest_plugins = ("tests.browser.test_browser_e2e",)


def _open_pdf(page):
    file = page.get_by_role("button", name="long.pdf", exact=True)
    identity = file.locator("xpath=ancestor::*[@data-item-id][1]").get_attribute(
        "data-item-id"
    )
    assert identity
    file.press("Enter")
    dialog = preview_dialog(page)
    expect(dialog.get_by_label("Page", exact=True)).to_be_enabled()
    return dialog, identity


def _page(dialog, number):
    field = dialog.get_by_label("Page", exact=True)
    field.fill(str(number))
    field.press("Enter")
    expect(field).to_have_value(str(number))


def _saved(page, server, identity, number):
    deadline = time.monotonic() + 10
    value = None
    while time.monotonic() < deadline:
        response = page.request.get(
            server.url + f"/api/v1/files/{identity}/pdf-progress"
        )
        assert response.ok, response.text()
        value = response.json()["progress"]
        if value and value["page_number"] == number:
            assert value["page_count"] == 12
            return value
        page.wait_for_timeout(50)
    pytest.fail(f"Saved PDF page did not reach {number}: {value}")


def test_owner_pdf_page_crosses_contexts_without_device_import(
    server, desktop, browser
):
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    dialog, identity = _open_pdf(page)
    assert_rendered_pdf_page(page, "long.pdf", 1)
    _page(dialog, 7)
    assert_rendered_pdf_page(page, "long.pdf", 7)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    _saved(page, server, identity, 7)
    other_context = browser.new_context(viewport={"width": 390, "height": 844})
    other_context.tracing.start(screenshots=True, snapshots=True, sources=True)
    try:
        other = other_context.new_page()
        other.add_init_script(
            "localStorage.setItem('tgdrive.pdf.positions.v1:share',"
            "JSON.stringify({['"
            + identity
            + "']:{page_number:2,page_count:12,updated_at:1}}))"
        )
        login(other, server.url)
        session = other.request.get(server.url + "/api/v1/session")
        assert session.ok and session.json()["authenticated"] is True
        other.get_by_role("button", name="Preview samples", exact=True).press("Enter")
        with other.expect_response(
            lambda response: (
                response.request.method == "GET"
                and response.url.endswith("/pdf-progress")
            )
        ) as saved_response:
            other_dialog, _ = _open_pdf(other)
        assert saved_response.value.ok
        assert saved_response.value.json()["progress"]["page_number"] == 7
        expect(other_dialog.get_by_label("Page", exact=True)).to_have_value("7")
        assert_rendered_pdf_page(other, "long.pdf", 7)
        assert_pdf_canvas_budget(other_dialog)
        other_dialog.get_by_role("button", name="Next page", exact=True).click()
        expect(other_dialog.get_by_label("Page", exact=True)).to_have_value("8")
        other_dialog.get_by_role("button", name="Previous page", exact=True).click()
        expect(other_dialog.get_by_label("Page", exact=True)).to_have_value("7")
        _page(other_dialog, 9)
        other.screenshot(path=ARTIFACTS / "pdf-owner-resume-mobile.png", full_page=True)
        other_dialog.get_by_role("button", name="Close Preview", exact=True).click()
        _saved(other, server, identity, 9)
        dialog, _ = _open_pdf(page)
        expect(dialog.get_by_label("Page", exact=True)).to_have_value("9")
        assert_rendered_pdf_page(page, "long.pdf", 9)
    finally:
        other_context.tracing.stop(path=ARTIFACTS / "pdf-resume-second-context.zip")
        other_context.close()


def test_pdf_manual_navigation_wins_over_late_saved_page(server, desktop):
    _, page = desktop
    page.add_init_script("""{
      const original=window.fetch;
      window.fetch=(url,options)=>String(url).endsWith('/pdf-progress')
        && (!options?.method || options.method==='GET')
        ? original(url,{...options,signal:undefined}):original(url,options);
    }""")
    held = []

    def delay_read(route):
        if route.request.method == "GET":
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/files/*/pdf-progress", delay_read)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    dialog, identity = _open_pdf(page)
    assert_rendered_pdf_page(page, "long.pdf", 1)
    _page(dialog, 5)
    _saved(page, server, identity, 5)
    assert held
    held.pop().fulfill(
        json={"progress": {"page_number": 8, "page_count": 12, "updated_at": 1}}
    )
    expect(dialog.get_by_label("Page", exact=True)).to_have_value("5")
    assert_rendered_pdf_page(page, "long.pdf", 5)


def test_pdf_count_mismatch_does_not_restore_or_overwrite_before_navigation(
    server, desktop
):
    _, page = desktop
    writes = []
    page.on(
        "request",
        lambda request: (
            writes.append(request)
            if "/pdf-progress" in request.url and request.method in {"POST", "PUT"}
            else None
        ),
    )

    def mismatch(route):
        if route.request.method == "GET":
            route.fulfill(
                json={"progress": {"page_number": 8, "page_count": 99, "updated_at": 1}}
            )
        else:
            route.continue_()

    page.route("**/api/v1/files/*/pdf-progress", mismatch)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    dialog, identity = _open_pdf(page)
    assert_rendered_pdf_page(page, "long.pdf", 1)
    expect(
        dialog.get_by_role("status").filter(has_text="different PDF page count")
    ).to_be_visible()
    assert writes == []
    _page(dialog, 3)
    _saved(page, server, identity, 3)


@pytest.mark.parametrize("blocked", [False, True])
def test_anonymous_pdf_page_stays_local_and_storage_failure_keeps_reader_usable(
    server, desktop, browser, blocked
):
    _, owner = desktop
    login(owner, server.url)
    owner.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    dialog, identity = _open_pdf(owner)
    _page(dialog, 6)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    before = _saved(owner, server, identity, 6)
    response = owner.request.post(
        server.url + "/api/v1/shares", data={"path": "/" + identity}
    )
    assert response.ok
    token = response.json()["auth"]
    context = browser.new_context(viewport={"width": 390, "height": 844})
    try:
        public = context.new_page()
        if blocked:
            public.add_init_script("""{
              const read=Storage.prototype.getItem,write=Storage.prototype.setItem;
              Storage.prototype.getItem=function(key){
                if(key.startsWith('tgdrive.pdf.')) throw Error('Storage blocked');
                return read.call(this,key);
              };
              Storage.prototype.setItem=function(key,value){
                if(key.startsWith('tgdrive.pdf.')) throw Error('Storage blocked');
                return write.call(this,key,value);
              };
            }""")
        requests = []
        public.on(
            "request",
            lambda request: (
                requests.append(request.url) if "/pdf-progress" in request.url else None
            ),
        )
        public.goto(server.url + "/embed?path=/" + identity + "&auth=" + token)
        shared = public.locator("dialog.file-preview:not(.file-details)")
        expect(shared).to_be_visible()
        expect(
            shared.get_by_role("button", name="Close Preview", exact=True)
        ).to_have_count(0)
        expect(
            shared.get_by_role("link", name="Download Original", exact=True)
        ).to_be_visible()
        assert_rendered_pdf_page(public, "long.pdf", 1)
        _page(shared, 4)
        assert_rendered_pdf_page(public, "long.pdf", 4)
        if blocked:
            expect(
                shared.get_by_role("status").filter(
                    has_text="Browser storage is unavailable"
                )
            ).to_be_visible()
            assert public.evaluate("document.documentElement.scrollWidth <= innerWidth")
            public.screenshot(
                path=ARTIFACTS / "pdf-local-storage-unavailable-mobile.png",
                full_page=True,
            )
        public.reload()
        shared = public.locator("dialog.file-preview:not(.file-details)")
        expect(shared).to_be_visible()
        expect(
            shared.get_by_role("button", name="Close Preview", exact=True)
        ).to_have_count(0)
        expect(shared.get_by_label("Page", exact=True)).to_have_value(
            "1" if blocked else "4"
        )
        assert requests == []
        assert _saved(owner, server, identity, 6) == before
    finally:
        context.close()
