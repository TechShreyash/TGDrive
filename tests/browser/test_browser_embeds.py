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
from tests.browser.test_browser_e2e import ARTIFACTS, ROOT, expect, fixture_nodes, login

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


def test_protected_folder_and_embed_require_unlock_and_honor_revocation(
    server, desktop, browser
):
    _, owner = desktop
    login(owner, server.url)
    seed = server.control()["seed"]
    password = "browser-fixture-password"
    owner.get_by_label("Actions for 資料", exact=True).click()
    owner.get_by_role("button", name="Share Link", exact=True).click()
    dialog = owner.get_by_role("dialog")
    dialog.get_by_label("Share password (optional)", exact=True).fill(password)
    dialog.get_by_role("button", name="Create Share Link", exact=True).click()
    folder_link = dialog.get_by_label("Share Link", exact=True).input_value()
    auth = parse_qs(urlsplit(folder_link).query)["auth"][0]
    direct = (
        server.url
        + "/file?"
        + urlencode({"path": seed["file"], "auth": auth, "download": 1})
    )
    file_embed = (
        server.url + "/embed?" + urlencode({"path": seed["file"], "auth": auth})
    )
    guest = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        color_scheme="dark",
    )
    embedded = browser.new_context()
    try:
        page = guest.new_page()
        page.goto(folder_link)
        expect(
            page.get_by_role("heading", name="Password-protected link")
        ).to_be_visible()
        assert guest.request.get(direct).status == 403
        password_field = page.get_by_label("Share password", exact=True)
        password_field.fill("wrong")
        page.get_by_role("button", name="Open shared link").click()
        expect(page.get_by_role("alert")).to_have_text("Incorrect share password.")
        expect(password_field).to_be_focused()
        expect(password_field).to_have_accessible_description(
            "Incorrect share password."
        )
        page.screenshot(path=ARTIFACTS / "share-unlock-password-error-phone.png")
        assert guest.request.get(direct).status == 403
        password_field.fill(password)
        expect(page.get_by_role("alert")).to_have_count(0)
        page.get_by_role("button", name="Open shared link").click()
        expect(
            page.get_by_role("button", name="résumé #1.txt", exact=True)
        ).to_be_visible()
        assert guest.request.get(direct).body() == b"public content"
        assert password not in page.evaluate("JSON.stringify(localStorage)")
        assert password not in page.evaluate("document.cookie")

        embed = embedded.new_page()
        embed.goto(file_embed)
        expect(
            embed.get_by_role("heading", name="Password-protected link")
        ).to_be_visible()
        embed.get_by_label("Share password").fill(password)
        embed.get_by_role("button", name="Open shared link").click()
        expect(embed.get_by_role("link", name="Download")).to_be_visible()

        links = owner.request.get(server.url + "/api/v1/shares").json()["shares"]
        identity = next(row["id"] for row in links if row["path"] == seed["folder"])
        assert owner.request.delete(
            server.url + "/api/v1/shares", data={"share_id": identity}
        ).ok
        assert guest.request.get(direct).status == 403
        assert embedded.request.get(direct).status == 403
        page.reload()
        expect(page.get_by_text("This link has expired", exact=False)).to_be_visible()
    finally:
        guest.close()
        embedded.close()


def test_protected_embed_fits_a_small_frame(server, desktop, browser):
    _, owner = desktop
    login(owner, server.url)
    source = server.control()["seed"]["file"]
    share = owner.request.post(
        server.url + "/api/v1/shares",
        data={"path": source, "password": "small-frame-demo"},
    ).json()
    url = server.url + "/embed?" + urlencode({"path": source, "auth": share["auth"]})
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        viewer = context.new_page()
        parent_origin = server.url.replace("127.0.0.1", "localhost")
        parent_url = parent_origin + "/protected-share-test"
        context.grant_permissions(["local-network-access"], origin=parent_origin)
        viewer.route(
            parent_url,
            lambda route: route.fulfill(
                content_type="text/html",
                body=f'<iframe src="{escape(url)}" width="320" height="260"></iframe>',
            ),
        )
        viewer.goto(parent_url)
        frame = viewer.frame_locator("iframe")
        expect(
            frame.get_by_role("heading", name="Password-protected link")
        ).to_be_visible()
        expect(frame.get_by_role("img", name="TGDrive", exact=True)).to_have_count(0)
        link = frame.get_by_role("link", name="Open in a new tab", exact=True)
        expect(link).to_have_attribute("href", url)
        button = frame.get_by_role("button", name="Open shared link", exact=True)
        button.scroll_into_view_if_needed()
        expect(button).to_be_in_viewport(ratio=1)
        frame.get_by_label("Share password", exact=True).fill("small-frame-demo")
        button.click()
        expect(frame.get_by_label("Share password", exact=True)).to_have_count(0)
        expect(
            frame.get_by_text("Your browser could not keep the unlock", exact=False)
        ).to_be_visible()
        with viewer.expect_popup() as opened:
            frame.get_by_role("link", name="Open in a new tab", exact=True).click()
        tab = opened.value
        field = tab.get_by_label("Share password", exact=True)
        field.fill("small-frame-demo")
        field.press("Enter")
        expect(tab.get_by_role("link", name="Download")).to_be_visible()


@pytest.mark.parametrize(
    "width,height,theme",
    [(1536, 695, "dark"), (820, 1180, "dark"), (390, 844, "light")],
)
def test_share_password_edit_field_and_guest_unlock_layout(
    server, browser, width, height, theme
):
    with browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=theme,
        has_touch=width < 1000,
    ) as context:
        owner = context.new_page()
        login(owner, server.url)
        seed = server.control()["seed"]
        creation = owner.request.post(
            server.url + "/api/v1/shares",
            data={
                "path": seed["folder"],
                "label": "Book club",
                "expires_at": "2031-12-21T08:42:24Z",
            },
        )
        assert creation.ok
        share = creation.json()

        owner.goto(server.url + "/?view=shares")
        owner.get_by_role("button", name="Edit link Book club", exact=True).click()
        panel = owner.get_by_role("dialog", name="Edit share link", exact=True)
        password = panel.get_by_label("Share password (optional)", exact=True)
        password.fill("fixture-book-club")
        owner.screenshot(
            path=ARTIFACTS / f"share-edit-password-{width}.png", animations="disabled"
        )
        pwd = password.evaluate("""node => ({
            innerBorder: getComputedStyle(node).borderTopWidth,
            control: node.closest(".mantine-PasswordInput-input")
                .getBoundingClientRect().height
        })""")
        reference = panel.get_by_label("Link label", exact=True).bounding_box()[
            "height"
        ]
        reveal = panel.get_by_role("button", name="Toggle password visibility")
        reveal.press("Enter")
        expect(password).to_have_attribute("type", "text")
        reveal.press("Enter")
        expect(password).to_have_attribute("type", "password")
        panel.get_by_role("button", name="Save changes", exact=True).click()
        expect(panel).not_to_be_visible()

    with browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=theme,
        has_touch=width < 1000,
    ) as context:
        guest = context.new_page()
        guest.goto(
            server.url
            + "/?"
            + urlencode(
                {
                    "path": "/share_" + seed["folder"].rsplit("/", 1)[-1],
                    "auth": share["auth"],
                }
            )
        )
        expect(
            guest.get_by_role("heading", name="Password-protected link")
        ).to_be_visible()
        field = guest.get_by_label("Share password", exact=True)
        button = guest.get_by_role("button", name="Open shared link", exact=True)
        guest.screenshot(
            path=ARTIFACTS / f"share-unlock-ready-{width}.png", animations="disabled"
        )
        field.fill("incorrect")
        field.press("Enter")
        expect(guest.get_by_role("alert")).to_have_text("Incorrect share password.")
        guest.screenshot(
            path=ARTIFACTS / f"share-unlock-error-{width}.png", animations="disabled"
        )
        expect(field).to_be_focused()
        expect(field).to_have_accessible_description("Incorrect share password.")
        assert pwd["innerBorder"] == "0px"
        assert abs(pwd["control"] - reference) <= 1
        expect(button).to_be_enabled()
        expect(button).to_be_in_viewport(ratio=1)
        expect(guest.get_by_role("img", name="TGDrive", exact=True)).to_be_visible()
        field.fill("fixture-book-club")
        field.press("Enter")
        expect(
            guest.get_by_role("button", name="résumé #1.txt", exact=True)
        ).to_be_visible()


def test_link_opens_count_successful_entries_only(server, desktop, browser):
    _, owner = desktop
    login(owner, server.url)
    seed = server.control()["seed"]
    response = owner.request.post(
        server.url + "/api/createNewFolder",
        data={"path": seed["folder"], "name": "Child folder"},
    )
    assert response.ok
    share = owner.request.post(
        server.url + "/api/v1/shares",
        data={"path": seed["folder"], "password": "first link password"},
    ).json()
    auth = share["auth"]
    folder_link = (
        server.url
        + "/?"
        + urlencode(
            {"path": "/share_" + seed["folder"].rsplit("/", 1)[-1], "auth": auth}
        )
    )
    embed_link = (
        server.url + "/embed?" + urlencode({"path": seed["file"], "auth": auth})
    )

    def count(expected):
        owner.wait_for_function(
            """async ({id, expected}) => {
                const response = await fetch('/api/v1/shares');
                if (!response.ok) return false;
                const {shares} = await response.json();
                return shares.find(link => link.id === id)?.open_count === expected;
            }""",
            arg={"id": share["id"], "expected": expected},
        )

    count(0)
    with browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    ) as guest:
        recorded = []
        page = guest.new_page()

        def lose_first_confirmation(route):
            recorded.append(route.request.post_data_json["event_id"])
            if len(recorded) == 1:
                assert route.fetch().status == 204
                route.abort("failed")
            else:
                route.continue_()

        page.route("**/api/v1/shared/opens", lose_first_confirmation)
        page.goto(folder_link)
        field = page.get_by_label("Share password", exact=True)
        field.fill("wrong")
        field.press("Enter")
        expect(page.get_by_role("alert")).to_have_text("Incorrect share password.")
        assert not recorded
        count(0)
        field.fill("first link password")
        with page.expect_response(
            lambda response: response.url.endswith("/shared/opens") and response.ok
        ):
            field.press("Enter")
        child = page.get_by_role("button", name="Child folder", exact=True)
        expect(child).to_be_visible()
        count(1)
        assert len(recorded) == 2 and recorded[0] == recorded[1]
        child.tap()
        expect(
            page.get_by_role("heading", name="Child folder", exact=True)
        ).to_be_visible()
        count(1)
        assert len(recorded) == 2
        page.reload()
        expect(
            page.get_by_role("heading", name="Child folder", exact=True)
        ).to_be_visible()
        count(2)
        assert (
            guest.request.get(
                server.url + "/file",
                params={"path": seed["file"], "auth": auth, "download": 1},
            ).body()
            == b"public content"
        )
        count(2)

    with browser.new_context() as guest:
        embed = guest.new_page()
        embed.goto(embed_link)
        expect(
            embed.get_by_role("heading", name="Password-protected link")
        ).to_be_visible()
        count(2)
        embed.get_by_label("Share password").fill("first link password")
        embed.get_by_role("button", name="Open shared link").click()
        expect(embed.get_by_role("link", name="Download")).to_be_visible()
        count(3)

    for width, height, theme in ((1536, 695, "dark"), (390, 844, "light")):
        owner.set_viewport_size({"width": width, "height": height})
        owner.add_init_script(f"localStorage.setItem('tgdrive.appearance', '{theme}')")
        owner.goto(server.url + "/?view=shares")
        opens = owner.locator(f'[data-share-id="{share["id"]}"] .share-opens')
        expect(opens).to_contain_text("3 opens")
        assert owner.evaluate("document.documentElement.scrollWidth <= innerWidth")
        owner.screenshot(
            path=ARTIFACTS / f"share-opens-{width}.png", animations="disabled"
        )


def test_root_folder_share_can_display_and_count_an_open(server, desktop, browser):
    _, owner = desktop
    login(owner, server.url)
    response = owner.request.post(server.url + "/api/v1/shares", data={"path": "/"})
    assert response.ok
    share = response.json()
    with browser.new_context() as guest:
        page = guest.new_page()
        with page.expect_response(
            lambda response: response.url.endswith("/shared/opens") and response.ok
        ):
            page.goto(
                server.url
                + "/?"
                + urlencode({"path": "/share_root", "auth": share["auth"]})
            )
        expect(
            page.get_by_role("button", name="Private folder", exact=True)
        ).to_be_visible()
    (row,) = owner.request.get(server.url + "/api/v1/shares").json()["shares"]
    assert row["open_count"] == 1


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
    link = page.get_by_label(
        "Embed Link" if kind == "folder" else "Share Link", exact=True
    )
    expect(link).to_be_visible()
    embed_url = link.input_value()
    share_url = page.get_by_label("Share Link", exact=True).input_value()
    assert urlsplit(embed_url).path == "/embed"
    assert parse_qs(urlsplit(embed_url).query) == parse_qs(urlsplit(share_url).query)
    page.get_by_role(
        "button",
        name="Copy Embed Link" if kind == "folder" else "Copy Link",
        exact=True,
    ).click()
    if clipboard:
        expect(
            page.get_by_text(
                "Embed Link copied." if kind == "folder" else "Share Link copied.",
                exact=True,
            )
        ).to_be_visible()
        assert page.evaluate("navigator.clipboard.readText()") == embed_url
    else:
        expect(page.get_by_role("alert")).to_contain_text(
            "Select and copy the embed link above."
            if kind == "folder"
            else "Select and copy the link above."
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
    copy = row.locator(".share-embed-copy" if kind == "folder" else ".share-copy")
    copy.click()
    if clipboard:
        expect(copy).to_have_attribute(
            "aria-label",
            re.compile("^Embed link copied" if kind == "folder" else "^Link copied"),
        )
        shared_url = page.evaluate("navigator.clipboard.readText()")
    else:
        dialog = page.get_by_role(
            "dialog",
            name="Copy Embed Link" if kind == "folder" else "Copy Share Link",
            exact=True,
        )
        shared_input = dialog.get_by_label(
            "Embed Link" if kind == "folder" else "Share Link", exact=True
        )
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


def test_ordinary_video_share_uses_rich_tracks_and_preserves_original_download(
    server, desktop
):
    _, page = desktop
    login(page, server.url)
    folder = server.control()["seed"]["preview_folder"]
    page.goto(server.url + "/?" + urlencode({"path": folder}))
    page.get_by_label("Actions for multitrack.mkv", exact=True).click()
    page.get_by_role("button", name="Share Link", exact=True).click()
    page.get_by_role("button", name="Create Share Link", exact=True).click()
    url = page.get_by_label("Share Link", exact=True).input_value()
    assert urlsplit(url).path == "/embed"
    params = parse_qs(urlsplit(url).query)
    original = page.request.get(
        server.url + "/file",
        params={
            "path": params["path"][0],
            "auth": params["auth"][0],
            "download": "1",
        },
    )
    assert original.ok
    assert original.body() == (ROOT / "tests/fixtures/multitrack.mkv").read_bytes()
    page.goto(url)
    player = page.locator("movi-player")
    page.wait_for_function("()=>document.querySelector('movi-player')?.duration>4")
    assert (
        player.evaluate(
            "el=>el.player.getMediaInfo().tracks.filter(t=>t.type==='audio').length"
        )
        >= 2
    )
    expect(page.get_by_role("button", name="Close Preview", exact=True)).to_have_count(
        0
    )
    expect(
        page.get_by_role("link", name="Download Original", exact=True)
    ).to_be_visible()


@pytest.mark.parametrize("change", ["navigate", "refresh"])
def test_late_embed_continuation_cannot_replace_current_folder_or_cursor(
    server, desktop, change
):
    _, page = desktop
    held = []
    child = {"id": "child", "name": "Child folder", "type": "folder", "path": "/root"}

    def shared(route):
        params = parse_qs(urlsplit(route.request.url).query)
        if "cursor" in params:
            held.append(route)
            return
        nested = params["parent_id"] == ["/child"]
        route.fulfill(
            json={
                "items": [
                    {
                        "id": "current",
                        "name": "Current file.txt",
                        "type": "file",
                        "path": "/child",
                    }
                ]
                if nested
                else [child],
                "folder": {"name": "Child folder" if nested else "Root folder"},
                "total": 1 if nested else 201,
                "next_cursor": None if nested else "root-cursor",
            }
        )

    page.route("**/api/v1/shared/nodes?*", shared)
    page.goto(server.url + "/embed?path=%2Fshare_root&auth=token")
    page.get_by_role("button", name="Show more", exact=True).click()
    expect(page.get_by_role("button", name="Show more", exact=True)).to_be_disabled()
    if change == "navigate":
        page.get_by_role("button", name="Child folder Folder", exact=True).click()
        expect(
            page.get_by_role("button", name="Current file.txt", exact=False)
        ).to_be_visible()
    else:
        page.get_by_role("button", name="Refresh shared folder", exact=True).click()
        expect(page.get_by_role("button", name="Show more", exact=True)).to_be_enabled()
    assert len(held) == 1
    held[0].fulfill(
        json={
            "items": [
                {
                    "id": "obsolete",
                    "name": "Wrong folder.txt",
                    "type": "file",
                    "path": "/root",
                }
            ],
            "total": 201,
            "next_cursor": "wrong-cursor",
        }
    )
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame("
        "() => requestAnimationFrame(resolve)))"
    )
    expect(page.get_by_text("Wrong folder.txt", exact=True)).to_have_count(0)
    if change == "navigate":
        expect(page.get_by_role("button", name="Show more", exact=True)).to_have_count(
            0
        )
    else:
        with page.expect_request(lambda request: "cursor=root-cursor" in request.url):
            page.get_by_role("button", name="Show more", exact=True).click()


@pytest.mark.parametrize(
    "outcome", ["continue", "final", "empty", "retry", "moved", "pointer"]
)
def test_embed_show_more_keeps_keyboard_position(server, desktop, outcome):
    _, page = desktop
    page.set_viewport_size({"width": 320, "height": 360})
    held = []
    items = [
        {
            "id": f"photo-{index}",
            "name": f"Photo {index}.jpg",
            "type": "file",
            "path": "/root",
        }
        for index in range(1, 10)
    ]
    next_photo = dict(items[-1], id="photo-10", name="Photo 10.jpg")

    def shared(route):
        if "cursor" in parse_qs(urlsplit(route.request.url).query):
            held.append(route)
        else:
            route.fulfill(
                json={
                    "items": items,
                    "folder": {"name": "Photo archive"},
                    "total": 20,
                    "next_cursor": "second",
                }
            )

    page.route("**/api/v1/shared/nodes?*", shared)
    page.goto(server.url + "/embed?path=%2Fshare_root&auth=token")
    more = page.get_by_role("button", name="Show more", exact=True)
    with page.expect_request(lambda request: "cursor=second" in request.url):
        if outcome == "pointer":
            more.click()
        else:
            more.press("Enter")
    expect(more).to_be_disabled()
    if outcome != "pointer":
        expect(more).to_be_focused()
        expect(more).to_have_attribute("aria-busy", "true")
        page.keyboard.press("Enter")
        assert len(held) == 1

    if outcome == "retry":
        held.pop().fulfill(status=503, json={"detail": "Synthetic page failure"})
        retry = page.get_by_role("button", name="Could not load more. Try Again")
        expect(retry).to_be_focused()
        with page.expect_request(lambda request: "cursor=second" in request.url):
            retry.press("Enter")
        expect(more).to_be_focused()
    previous = page.get_by_role("link", name="Download Photo 9.jpg", exact=True)
    if outcome == "moved":
        page.keyboard.press("Shift+Tab")
        expect(previous).to_be_focused()

    viewport = page.locator(".embed-view")
    scroll_top = viewport.evaluate("view => view.scrollTop")
    held[0].fulfill(
        json={
            "items": [] if outcome == "empty" else [next_photo],
            "total": {"empty": 9, "final": 10}.get(outcome, 20),
            "next_cursor": None if outcome in {"final", "empty"} else "third",
        }
    )
    if outcome == "empty":
        expect(more).to_have_count(0)
        last = page.get_by_role("button", name=re.compile(r"^Photo 9\.jpg"))
        expect(last).to_be_focused()
        return
    added = page.get_by_role("button", name=re.compile(r"^Photo 10\.jpg"))
    expect(added).to_be_visible()
    if outcome == "moved":
        expect(previous).to_be_focused()
    elif outcome == "pointer":
        expect(added).not_to_be_focused()
        assert viewport.evaluate("view => view.scrollTop") == scroll_top
    else:
        expect(added).to_be_focused()
        if outcome == "continue":
            page.screenshot(path=ARTIFACTS / "embed-more-keyboard-focus.png")
        page.keyboard.press("Tab")
        expect(
            page.get_by_role("link", name="Download Photo 10.jpg", exact=True)
        ).to_be_focused()


def test_small_embed_keeps_folder_path_and_keyboard_focus_reachable(server, desktop):
    _, page = desktop
    page.set_viewport_size({"width": 320, "height": 210})
    names = [f"Long nested photographs folder {level}" for level in range(6)]
    hold_parent = False
    held_parent = []

    def shared(route):
        params = parse_qs(urlsplit(route.request.url).query)
        level = int(params["parent_id"][0].removeprefix("/level-"))
        if level == 3 and hold_parent:
            held_parent.append(route)
            return
        route.fulfill(
            json={
                "items": [
                    {
                        "id": f"level-{level + 1}",
                        "name": names[level + 1],
                        "type": "folder",
                        "path": f"/level-{level}",
                    }
                ],
                "folder": {"name": names[level]},
                "total": 1,
                "next_cursor": None,
            }
        )

    page.route("**/api/v1/shared/nodes?*", shared)
    page.goto(server.url + "/embed?path=%2Fshare_level-0&auth=token")
    path = page.get_by_role("navigation", name="Shared folder path")
    contents = page.get_by_role("list", name="Shared folder contents")
    for level in range(1, 5):
        contents.get_by_role("button", name=f"{names[level]} Folder").press("Enter")
        current = path.get_by_role("button", name=names[level], exact=True)
        expect(current).to_have_attribute("aria-current", "page")
        expect(current).to_be_focused()
        child = contents.get_by_role("button", name=f"{names[level + 1]} Folder")
        expect(child).to_be_in_viewport(ratio=1)
        row = child.bounding_box()
        header = page.locator(".embed-header").bounding_box()
        assert row and header and row["y"] >= header["y"] + header["height"]
    expect(path.get_by_role("button")).to_have_count(3)

    page.set_viewport_size({"width": 1440, "height": 900})
    expect(path.get_by_role("button")).to_have_count(5)
    page.set_viewport_size({"width": 320, "height": 210})
    parent = path.get_by_role("button", name=names[3], exact=True)
    hold_parent = True
    parent.press("Enter")
    expect(parent).to_have_attribute("aria-current", "page")
    expect(page.get_by_role("status")).to_have_text("Loading shared folder…")
    root = path.get_by_role("button", name=names[0], exact=True)
    expect(root).to_be_enabled()
    root.press("Enter")
    expect(root).to_have_attribute("aria-current", "page")
    expect(root).to_be_focused()
    expect(contents.get_by_role("button", name=f"{names[1]} Folder")).to_be_in_viewport(
        ratio=1
    )
    assert len(held_parent) == 1
    held_parent[0].fulfill(json={"items": [], "folder": {"name": names[3]}, "total": 0})
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame("
        "() => requestAnimationFrame(resolve)))"
    )
    expect(contents.get_by_role("button", name=f"{names[1]} Folder")).to_be_visible()


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
            frame.get_by_role("heading", name="Shared Item Unavailable", exact=True)
        ).to_be_visible()
        expect(frame.get_by_role("alert")).to_contain_text("Invalid share link")
        expect(
            frame.locator(".sidebar, #login-dialog, .preview-surface")
        ).to_have_count(0)
