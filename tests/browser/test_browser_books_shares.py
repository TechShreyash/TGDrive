"""Reader and independent-link workflows on the real production bundle."""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, CONTROL, expect, login

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(
    "width,theme", [(1440, "light"), (390, "dark"), (320, "light")]
)
@pytest.mark.parametrize(
    "name", ["The little journey.epub", "Sample comic.cbz", "Sample manga.cbr"]
)
def test_selective_reader_controls_and_mobile_layout(
    server, desktop, width, theme, name
):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 900 if width > 700 else 844})
    page.add_init_script("localStorage.setItem('tgdrive.appearance', '" + theme + "')")
    originals = []
    external = []
    page.on(
        "request",
        lambda request: (
            originals.append(request.url)
            if urlparse(request.url).path == "/file"
            else external.append(request.url)
            if "external.invalid" in request.url
            else None
        ),
    )
    login(page, server.url)
    folder = page.request.post(server.url + "/__test__/books", headers=CONTROL).json()[
        "path"
    ]
    page.goto(server.url + "/?path=" + folder)
    page.get_by_role("button", name=name, exact=True).press("Enter")
    expect(
        page.get_by_role("group", name="Reader controls", exact=True)
    ).to_be_visible()
    if name.endswith("epub"):
        expect(page.locator(".book-chapter h1")).to_have_text("A quiet morning")
        page.locator('summary[aria-label="Reading options"]').click()
        page.get_by_role("button", name="Increase text size", exact=True).click()
        page.locator('summary[aria-label="Reading options"]').click()
        expect(page.locator(".book-reader")).to_have_class(
            __import__("re").compile("book-font-20")
        )
        page.get_by_role("button", name="Next chapter or page", exact=True).click()
        expect(page.locator(".book-chapter h1")).to_have_text("Across the city")
        image = page.locator(".book-chapter img")
        expect(image).to_be_visible()
        expect(image).to_have_js_property("naturalWidth", 480)
    else:
        image = page.locator(".book-content > .book-image > img")
        expect(image).to_be_visible()
        expect(image).to_have_js_property("naturalWidth", 480)
        page.locator('summary[aria-label="Reading options"]').click()
        page.get_by_label("Right to left", exact=True).check()
        page.get_by_label("Page fit", exact=True).select_option("page")
        page.locator('summary[aria-label="Reading options"]').click()
        page.locator(".book-content").focus()
        page.keyboard.press("ArrowLeft")
        expect(page.locator(".book-position")).to_have_text(
            "2 / " + ("2" if name.endswith("cbr") else "3")
        )
        expect(image).to_have_js_property("naturalWidth", 480)
    assert page.locator(".preview-heading h2").evaluate(
        "node => getComputedStyle(node).color === "
        'getComputedStyle(node.closest(".preview-header")).color'
    )
    assert not originals and not external
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".preview-header").evaluate(
        "node => node.scrollWidth <= node.clientWidth+1"
    )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=ARTIFACTS / f"reader-{name.rsplit('.', 1)[-1]}-{theme}-{width}.png"
    )
    page.get_by_role("button", name="Close Preview", exact=True).click()
    page.get_by_role("button", name=name, exact=True).press("Enter")
    expect(page.locator(".book-position")).to_have_text(
        "2 / " + ("2" if name.endswith(("epub", "cbr")) else "3")
    )


def test_independent_share_ui_and_public_reader_revocation(server, desktop, browser):
    context, page = desktop
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=server.url)
    login(page, server.url)
    folder = page.request.post(server.url + "/__test__/books", headers=CONTROL).json()[
        "path"
    ]
    first = page.request.post(
        server.url + "/api/v1/shares",
        data={"path": folder, "label": "Alex", "expires_in_days": 1},
    ).json()
    second = page.request.post(
        server.url + "/api/v1/shares",
        data={"path": folder, "label": "Book club", "expires_in_days": 30},
    ).json()
    page.get_by_role("link", name="Shared Links", exact=True).click()
    expect(page.locator(".share-row")).to_have_count(2)
    expect(page.get_by_text("Unnamed link", exact=False)).to_have_count(0)
    expect(page.get_by_role("button", name="New Link", exact=True)).to_have_count(0)
    for share, label in [(first, "Alex"), (second, "Book club")]:
        row = page.locator(".share-row").filter(has_text=label)
        copy = row.locator(".share-copy")
        expect(copy).to_have_text("")
        copy.click()
        expect(copy).to_have_attribute(
            "aria-label", __import__("re").compile("^Link copied")
        )
        copied_url = page.evaluate("navigator.clipboard.readText()")
        assert share["auth"] in copied_url and folder.strip("/") in copied_url
    assert len(page.request.get(server.url + "/api/v1/shares").json()["shares"]) == 2
    page.get_by_role("button", name="Edit link Alex", exact=True).click()
    page.get_by_label("Link label", exact=True).fill("Alex review")
    page.get_by_label("No expiry", exact=True).check()
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(
        page.get_by_role("button", name="Edit link Alex review", exact=True)
    ).to_be_visible()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for width, theme in [(1440, "light"), (390, "dark"), (320, "light")]:
        page.set_viewport_size({"width": width, "height": 900 if width > 700 else 844})
        page.evaluate(
            """theme => {
          localStorage.setItem('tgdrive.appearance', theme);
          window.dispatchEvent(new CustomEvent(
            'tgdrive:appearance-changed', {detail: theme}
          ));
        }""",
            theme,
        )
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=ARTIFACTS / f"independent-links-{theme}-{width}.png")
    page.set_viewport_size({"width": 1440, "height": 900})
    copied = page.request.post(
        server.url + "/api/v1/shares/link", data={"share_id": first["id"]}
    ).json()
    assert copied["auth"] == first["auth"]
    public = browser.new_context(viewport={"width": 390, "height": 844})
    try:
        reader = public.new_page()
        reader.goto(
            server.url + "/?path=/share_" + folder.strip("/") + "&auth=" + first["auth"]
        )
        reader.get_by_role("button", name="The little journey.epub", exact=True).press(
            "Enter"
        )
        expect(reader.locator(".book-chapter h1")).to_have_text("A quiet morning")
        row = page.locator(".share-row").filter(has_text="Alex review")
        row.get_by_role(
            "button", name="Revoke Link for Reading samples", exact=True
        ).click()
        page.get_by_role("button", name="Revoke Link", exact=True).click()
        expect(page.locator(".share-row")).to_have_count(1)
        reader.get_by_role("button", name="Next chapter or page", exact=True).click()
        expect(reader.get_by_text("Invalid share link", exact=True)).to_be_visible()
        reader.goto(
            server.url
            + "/?path=/share_"
            + folder.strip("/")
            + "&auth="
            + second["auth"]
        )
        reader.get_by_role("button", name="The little journey.epub", exact=True).press(
            "Enter"
        )
        expect(reader.locator(".book-chapter")).to_be_visible()
        assert (
            page.request.post(
                server.url + "/api/getDirectory",
                data={"path": "/share_" + folder.strip("/"), "auth": first["auth"]},
            ).status
            == 403
        )
        assert (
            page.request.post(
                server.url + "/api/getDirectory",
                data={"path": "/share_" + folder.strip("/"), "auth": second["auth"]},
            ).status
            == 200
        )
    finally:
        public.close()


def test_create_multiple_links_from_share_dialog(server, desktop):
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="資料", exact=True).focus()
    # Use the same per-item action as a person browsing their drive.
    for label, expiry in [("Alex", "1 day"), ("Project team", "Custom")]:
        page.locator('summary[aria-label="Actions for 資料"]').click()
        page.get_by_role("button", name="Share Link", exact=True).click()
        page.get_by_label("Link label (optional)", exact=True).fill(label)
        page.get_by_role("button", name=expiry, exact=True).click()
        if expiry == "Custom":
            page.get_by_label("Expiry date and time", exact=True).fill(
                "2030-01-01T12:00"
            )
        page.get_by_role("button", name="Create Share Link", exact=True).click()
        expect(page.get_by_label("Share Link", exact=True)).to_be_visible()
        page.get_by_role("button", name="Done", exact=True).click()
    rows = page.request.get(server.url + "/api/v1/shares").json()["shares"]
    assert {row["label"] for row in rows} == {"Alex", "Project team"}
    assert len({row["id"] for row in rows}) == 2


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_same_item_links_keep_their_copy_edit_and_revoke_scope(server, desktop, kind):
    context, page = desktop
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=server.url)
    login(page, server.url)
    path = server.control()["seed"][kind]
    links = [
        page.request.post(
            server.url + "/api/v1/shares",
            data={"path": path, "label": label, "expires_in_days": days},
        ).json()
        for label, days in [("Review", 1), ("Archive", 30)]
    ]
    assert links[0]["auth"] != links[1]["auth"]
    page.goto(server.url + "/?view=shares")
    expect(page.locator(".share-row")).to_have_count(2)

    for link in links:
        row = page.locator(f'[data-share-id="{link["id"]}"]')
        expect(row).to_contain_text(link["label"])
        assert row.locator(".share-expiry time").evaluate(
            "(node, expiry) => Date.parse(node.dateTime) === Date.parse(expiry)",
            link["expires_at"],
        )
        copy = row.locator(".share-copy")
        expect(copy).to_have_text("")
        copy.click()
        expect(copy).to_have_attribute(
            "aria-label", __import__("re").compile("^Link copied")
        )
        assert link["auth"] in page.evaluate("navigator.clipboard.readText()")

    first_row = page.locator(f'[data-share-id="{links[0]["id"]}"]')
    first_row.get_by_role("button", name="Edit link Review", exact=True).click()
    page.get_by_label("Link label", exact=True).fill("Review updated")
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(first_row).to_contain_text("Review updated")
    listed = page.request.get(server.url + "/api/v1/shares").json()["shares"]
    assert len(listed) == 2
    by_id = {link["id"]: link for link in listed}
    assert by_id[links[0]["id"]]["expires_at"] == links[0]["expires_at"]
    assert by_id[links[1]["id"]]["expires_at"] == links[1]["expires_at"]
    assert by_id[links[1]["id"]]["label"] == "Archive"

    first_row.get_by_role(
        "button", name=__import__("re").compile("^Revoke Link for ")
    ).click()
    dialog = page.get_by_role("dialog", name="Revoke This Link?", exact=True)
    expect(dialog).to_contain_text("Review updated")
    dialog.get_by_role("button", name="Revoke Link", exact=True).click()
    expect(page.locator(".share-row")).to_have_count(1)

    for link, expected_status in [(links[0], 403), (links[1], 200)]:
        if kind == "folder":
            response = page.request.post(
                server.url + "/api/getDirectory",
                data={"path": "/share_" + path.strip("/"), "auth": link["auth"]},
            )
        else:
            response = page.request.head(
                server.url + "/file", params={"path": path, "auth": link["auth"]}
            )
        assert response.status == expected_status


def test_comic_image_error_details_and_automatic_recovery(server, desktop):
    _, page = desktop
    login(page, server.url)
    folder = page.request.post(server.url + "/__test__/books", headers=CONTROL).json()[
        "path"
    ]
    page.goto(server.url + "/?path=" + folder)
    calls = []

    def page_resource(route):
        if "entry=" not in route.request.url:
            route.continue_()
        else:
            calls.append(route.request.url)
            if len(calls) == 1:
                route.fulfill(
                    status=503,
                    content_type="application/json",
                    body='{"detail":"Reader busy"}',
                    headers={"Retry-After": "1"},
                )
            else:
                route.continue_()

    page.route("**/api/v1/books?**", page_resource)
    page.get_by_role("button", name="Sample comic.cbz", exact=True).press("Enter")
    expect(
        page.get_by_text("Connection interrupted. Retrying page…", exact=True)
    ).to_be_visible()
    expect(page.locator(".book-content > .book-image > img")).to_be_visible(
        timeout=15000
    )
    assert len(calls) == 2
    page.unroute("**/api/v1/books?**", page_resource)

    def refused(route):
        route.fulfill(
            status=422,
            content_type="application/json",
            body='{"detail":"This resource exceeds the 64 MiB preview limit."}',
        )

    page.route("**/api/v1/books?**entry=**", refused)
    page.get_by_role("button", name="Next chapter or page", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("64 MiB preview limit")
    expect(page.get_by_role("button", name="Retry image", exact=True)).to_be_visible()


def test_large_original_comic_page_renders_and_revalidates_without_resending(
    server, desktop
):
    import hashlib
    from urllib.parse import parse_qs

    _, page = desktop
    login(page, server.url)
    sample = page.request.post(
        server.url + "/__test__/books/large-page", headers=CONTROL
    ).json()
    responses = []

    def record(response):
        if urlparse(response.url).path == "/api/v1/books" and "entry=" in response.url:
            responses.append(response)

    page.on("response", record)
    page.goto(server.url + "/?path=" + sample["path"])
    page.get_by_role("button", name="Large original page.cbz", exact=True).press(
        "Enter"
    )
    image = page.locator(".book-content > .book-image > img")
    expect(image).to_be_visible(timeout=45000)
    expect(image).to_have_js_property("naturalWidth", 480)
    assert len(responses) == 1
    result = responses[0]
    assert result.status == 200
    assert int(result.headers["content-length"]) == sample["image_size"]
    # Native decode must succeed under the shipped CSP, using the unchanged bytes.
    assert image.get_attribute("src").startswith("blob:")
    assert parse_qs(urlparse(result.url).query)["entry"] == ["page.png"]
    # Inspector caches can evict a 39 MiB response. Verify its declared wire size
    # above and the authorized API bytes against the fixture's independent hash.
    again = page.request.get(result.url)
    assert len(again.body()) == sample["image_size"]
    assert hashlib.sha256(again.body()).hexdigest() == sample["image_sha256"]
    conditional = page.request.get(
        result.url, headers={"If-None-Match": result.headers["etag"]}
    )
    assert conditional.status == 304 and not conditional.body()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_epub_links_reach_anchor_and_image_targets(server, desktop):
    _, page = desktop
    login(page, server.url)
    folder = page.request.post(server.url + "/__test__/books", headers=CONTROL).json()[
        "path"
    ]
    page.route(
        "**/api/v1/books?**entry=OPS%2Fone.xhtml",
        lambda route: route.fulfill(
            json={
                "kind": "chapter",
                "nodes": [
                    {
                        "tag": "a",
                        "href": "OPS/two.xhtml#note",
                        "children": ["Read footnote"],
                    }
                ],
            }
        ),
    )
    paragraphs = [
        {"tag": "p", "children": ["An authored chapter paragraph. " * 8]}
        for _ in range(40)
    ]
    page.route(
        "**/api/v1/books?**entry=OPS%2Ftwo.xhtml",
        lambda route: route.fulfill(
            json={
                "kind": "chapter",
                "nodes": [
                    {
                        "tag": "a",
                        "id": "jump",
                        "href": "OPS/two.xhtml#illustration",
                        "children": ["Jump to illustration"],
                    },
                    *paragraphs,
                    {"tag": "a", "id": "note", "children": ["The footnote"]},
                    *paragraphs,
                    {
                        "tag": "img",
                        "id": "illustration",
                        "src": "OPS/images/page.png",
                        "alt": "Target illustration",
                        "children": [],
                    },
                ],
            }
        ),
    )
    page.goto(server.url + "/?path=" + folder)
    page.get_by_role("button", name="The little journey.epub", exact=True).press(
        "Enter"
    )
    page.get_by_role("button", name="Read footnote", exact=True).click()
    expect(page.locator("span#book-anchor-note")).to_be_in_viewport()
    expect(page.locator("button#book-anchor-jump")).to_have_text("Jump to illustration")
    page.get_by_role("button", name="Jump to illustration", exact=True).click()
    expect(page.locator(".book-image#book-anchor-illustration")).to_be_in_viewport()


def test_epub_offscreen_images_release_blobs_and_load_only_near_viewport(
    server, desktop
):
    _, page = desktop
    login(page, server.url)
    folder = page.request.post(server.url + "/__test__/books", headers=CONTROL).json()[
        "path"
    ]
    nodes = [
        {
            "tag": "img",
            "src": "OPS/images/page.png",
            "alt": "First image",
            "children": [],
        }
    ]
    nodes.extend(
        {"tag": "p", "children": ["A chapter with illustrations. " * 8]}
        for _ in range(80)
    )
    nodes.append(
        {
            "tag": "img",
            "src": "OPS/images/page.png",
            "alt": "Last image",
            "children": [],
        }
    )
    page.route(
        "**/api/v1/books?**entry=OPS%2Fone.xhtml",
        lambda route: route.fulfill(json={"kind": "chapter", "nodes": nodes}),
    )
    page.goto(server.url + "/?path=" + folder)
    page.get_by_role("button", name="The little journey.epub", exact=True).press(
        "Enter"
    )
    first = page.get_by_role("img", name="First image", exact=True)
    last = page.get_by_role("img", name="Last image", exact=True)
    expect(first).to_be_visible()
    expect(last).to_have_count(0)
    page.locator(".book-content").evaluate("node => node.scrollTop = node.scrollHeight")
    expect(last).to_be_visible()
    expect(first).to_have_count(0)
    page.locator(".book-content").evaluate("node => node.scrollTop = 0")
    expect(first).to_be_visible()
    expect(last).to_have_count(0)
