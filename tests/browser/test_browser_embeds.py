"""Public file/folder embeds and both owner copy entry points on Chromium."""

from __future__ import annotations

import os
import re
from html import escape
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, expect, fixture_nodes, login

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("kind", ["file", "folder"])
@pytest.mark.parametrize("clipboard", [True, False])
def test_embed_copy_reuses_popup_share_in_shared_links(
    server, desktop, kind, clipboard
):
    context, page = desktop
    if clipboard:
        context.grant_permissions(
            ["clipboard-read", "clipboard-write"], origin=server.url
        )
    else:
        page.add_init_script(
            "Object.defineProperty(navigator, 'clipboard', {get: () => undefined})"
        )
    login(page, server.url)
    path = server.control()["seed"][kind]
    name = "資料" if kind == "folder" else "résumé #1.txt"
    page.goto(server.url + "/?" + urlencode({"path": path.rsplit("/", 1)[0] or "/"}))
    page.locator(f'summary[aria-label="Actions for {name}"]').click()
    page.get_by_role("button", name="Share Link", exact=True).click()
    page.get_by_role("button", name="Create Share Link", exact=True).click()
    link = page.get_by_label("Embed Link", exact=True)
    expect(link).to_be_visible()
    embed_url = link.input_value()
    share_url = page.get_by_label("Share Link", exact=True).input_value()
    assert urlsplit(embed_url).path == "/embed"
    assert parse_qs(urlsplit(embed_url).query) == parse_qs(urlsplit(share_url).query)
    page.get_by_role("button", name="Copy Embed Link", exact=True).click()
    if clipboard:
        expect(page.get_by_text("Embed Link copied.", exact=True)).to_be_visible()
        assert page.evaluate("navigator.clipboard.readText()") == embed_url
    else:
        expect(page.get_by_role("alert")).to_contain_text(
            "Select and copy the embed link above."
        )
        link.focus()
        assert link.evaluate(
            "input => input.selectionStart === 0 && "
            "input.selectionEnd === input.value.length"
        )
    before = page.request.get(server.url + "/api/v1/shares").json()["shares"]
    assert len(before) == 1
    assert parse_qs(urlsplit(embed_url).query)["auth"] == [
        page.request.post(
            server.url + "/api/v1/shares/link", data={"share_id": before[0]["id"]}
        ).json()["auth"]
    ]
    page.get_by_role("button", name="Done", exact=True).click()
    page.get_by_role("link", name="Shared Links", exact=True).click()
    row = page.locator(f'[data-share-id="{before[0]["id"]}"]')
    copy = row.locator(".share-embed-copy")
    copy.click()
    if clipboard:
        expect(copy).to_have_attribute("aria-label", re.compile("^Embed link copied"))
        shared_url = page.evaluate("navigator.clipboard.readText()")
    else:
        dialog = page.get_by_role("dialog", name="Copy Embed Link", exact=True)
        shared_input = dialog.get_by_label("Embed Link", exact=True)
        expect(shared_input).to_be_visible()
        shared_url = shared_input.input_value()
        dialog.get_by_role("button", name="Done", exact=True).click()
    shared_params = parse_qs(urlsplit(shared_url).query)
    assert urlsplit(shared_url).path == "/embed"
    assert shared_params["auth"] == parse_qs(urlsplit(embed_url).query)["auth"]
    assert (
        shared_params["path"][0]
        == ("/share_" if kind == "folder" else "/") + path.rsplit("/", 1)[-1]
    )
    assert page.request.get(server.url + "/api/v1/shares").json()["shares"] == before


@pytest.mark.parametrize(
    "kind,width", [("text", 1440), ("image", 390), ("folder", 320)]
)
def test_cross_origin_embed_renders_and_rejects_invalid_or_revoked_links(
    server, desktop, kind, width
):
    context, page = desktop
    login(page, server.url)
    seed = server.control()["seed"]
    if kind == "folder":
        moved = page.request.post(
            server.url + "/api/v1/nodes/move",
            data={"paths": [seed["folder"]], "destination": seed["preview_folder"]},
        )
        assert moved.ok, moved.text()
        path = seed["preview_folder"]
    elif kind == "image":
        node = next(
            item
            for item in fixture_nodes(page, server, seed["preview_folder"])
            if item["name"] == "sample.png"
        )
        path = seed["preview_folder"] + "/" + node["id"]
    else:
        path = seed["file"]
    share = page.request.post(server.url + "/api/v1/shares", data={"path": path}).json()
    params = {
        "path": "/share_" + path.rsplit("/", 1)[-1] if kind == "folder" else path,
        "auth": share["auth"],
    }
    embed_url = server.url + "/embed?" + urlencode(params)
    parent_origin = server.url.replace("127.0.0.1", "localhost")
    parent_url = parent_origin + "/embed-test-parent"
    # Chromium gates loopback requests from this synthetic parent.
    context.grant_permissions(["local-network-access"], origin=parent_origin)
    page.route(
        parent_url,
        lambda route: route.fulfill(
            content_type="text/html",
            body='<iframe id="tgdrive-embed" title="Shared item" '
            'style="width:100%;height:700px;border:0" allowfullscreen src="'
            + escape(embed_url, quote=True)
            + '"></iframe>',
        ),
    )
    private_requests = []
    page.on(
        "request",
        lambda request: (
            private_requests.append(request.url)
            if urlsplit(request.url).path
            in {"/api/v1/session", "/api/v1/status", "/api/v1/shares", "/api/v1/jobs"}
            else None
        ),
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(parent_url)
    frame = page.frame_locator("#tgdrive-embed")
    expect(frame.locator(".embed-view")).to_be_visible()
    if kind == "folder":
        expect(
            frame.get_by_role("navigation", name="Shared folder path")
        ).to_be_visible()
        frame.get_by_role("button", name="資料 Folder", exact=True).click()
        frame.get_by_role("button", name=re.compile("résumé #1.txt")).click()
    if kind == "image":
        image = frame.locator(".preview-surface img")
        expect(image).to_be_visible()
        expect(image).to_have_js_property("naturalWidth", 16)
    else:
        expect(frame.locator(".text-preview pre")).to_contain_text("public content")
    expect(frame.locator(".sidebar, #login-dialog, #new-button")).to_have_count(0)
    assert not private_requests
    assert frame.locator("html").evaluate(
        "node => node.scrollWidth <= window.innerWidth"
    )
    page.screenshot(path=ARTIFACTS / f"embed-{kind}-{width}.png")
    if kind == "folder":
        frame.get_by_role("button", name="Close Preview", exact=True).click()
        frame.get_by_role("button", name="Preview samples", exact=True).click()
        expect(
            frame.get_by_role("button", name="資料 Folder", exact=True)
        ).to_be_visible()

    revoked = page.request.delete(
        server.url + "/api/v1/shares", data={"share_id": share["id"]}
    )
    assert revoked.ok, revoked.text()
    for auth in ("invalid-token", share["auth"]):
        failed_url = server.url + "/embed?" + urlencode({**params, "auth": auth})
        with page.expect_event(
            "framenavigated",
            predicate=lambda frame, failed_url=failed_url: frame.url == failed_url,
        ):
            page.locator("#tgdrive-embed").evaluate(
                "(node, url) => { node.src = url; }", failed_url
            )
        expect(
            frame.get_by_role("heading", name="Embed unavailable", exact=True)
        ).to_be_visible()
        expect(frame.get_by_role("alert")).to_contain_text("Invalid share link")
        expect(
            frame.locator(".sidebar, #login-dialog, .preview-surface")
        ).to_have_count(0)
