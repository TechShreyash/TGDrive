"""ZIP listing uses real authenticated routes and the isolated parser."""

from urllib.parse import urlencode

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


@pytest.mark.parametrize("width,theme", [(1440, "light"), (390, "dark")])
def test_zip_folder_navigation_without_extracting(
    server: Server, desktop, width, theme
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script(f"localStorage.setItem('tgdrive.appearance', '{theme}')")
    original_requests = []
    page.on(
        "request",
        lambda request: (
            original_requests.append(request.url) if "/file?" in request.url else None
        ),
    )
    login(page, server.url)
    server.control("/zip", method="POST")
    page.reload()
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="folders.zip", exact=True).dblclick()
    dialog = preview_dialog(page)
    expect(dialog.get_by_role("region", name="ZIP contents")).to_be_visible()
    expect(dialog.get_by_text("Password protected", exact=True)).to_be_visible()
    expect(
        dialog.get_by_text("<img src=x onerror=alert(1)>.txt", exact=True)
    ).to_be_visible()
    assert dialog.locator(".archive-preview img").count() == 0
    dialog.get_by_role("button", name="Documents", exact=True).click()
    dialog.get_by_role("button", name="nested", exact=True).click()
    expect(dialog.get_by_text("notes.txt", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="ZIP contents", exact=True).click()
    dialog.get_by_role("button", name="empty", exact=True).click()
    expect(dialog.get_by_text("This folder is empty.", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="ZIP contents", exact=True).click()
    dialog.get_by_role("button", name="many", exact=True).click()
    expect(dialog.get_by_text("Page 1 of 2", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Next", exact=True).click()
    expect(dialog.get_by_text("file-104.txt", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not original_requests
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=ARTIFACTS / f"zip-{width}-{theme}.png")


def test_zip_preview_share_is_scoped_and_revocable(server: Server, desktop, browser):
    _, owner = desktop
    login(owner, server.url)
    path = server.control("/zip", method="POST")["path"]
    share = owner.request.post(
        server.url + "/api/v1/shares",
        data={"path": path, "label": "ZIP review", "expires_in_days": 1},
    ).json()
    public = browser.new_context()
    try:
        url = server.url + "/api/v1/books?" + urlencode({"path": path})
        assert public.request.get(url).status in (401, 403)
        allowed = url + "&" + urlencode({"auth": share["auth"]})
        response = public.request.get(allowed)
        assert response.status == 200
        assert response.json()["kind"] == "archive"
        conditional = {"If-None-Match": response.headers["etag"]}
        assert public.request.get(allowed, headers=conditional).status == 304
        owner.request.delete(
            server.url + "/api/v1/shares", data={"share_id": share["id"]}
        )
        assert public.request.get(allowed, headers=conditional).status in (
            401,
            403,
            404,
        )
    finally:
        public.close()
