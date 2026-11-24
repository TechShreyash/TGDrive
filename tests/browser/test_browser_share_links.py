"""Independent-link workflows on the real production bundle."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import ARTIFACTS, CONTROL, expect, login
from tests.support.book_samples import epub

browser, desktop, server = fixtures.browser, fixtures.desktop, fixtures.server
pytestmark = pytest.mark.e2e


def test_grid_folder_headers_keep_native_list_semantics(server, desktop):
    _, page = desktop
    login(page, server.url)
    page.request.post(server.url + "/__test__/books", headers=CONTROL)
    page.goto(server.url)
    page.get_by_role("button", name="Grid view", exact=True).click()
    file_list = page.get_by_role("region", name="File list", exact=True)
    expect(file_list).to_be_visible()
    header = file_list.get_by_role("listitem").filter(
        has=page.get_by_role("heading", name="Folders", exact=True)
    )
    expect(header).to_have_count(1)


def test_independent_share_ui_and_public_download_revocation(server, desktop, browser):
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
        download = reader.get_by_role("link", name="Download Original", exact=True)
        with reader.expect_download() as result:
            download.click()
        assert result.value.path().read_bytes() == epub(unsafe=True)
        row = page.locator(".share-row").filter(has_text="Alex review")
        row.get_by_role(
            "button", name="Revoke Link for Reading samples", exact=True
        ).click()
        page.get_by_role("button", name="Revoke Link", exact=True).click()
        expect(page.locator(".share-row")).to_have_count(1)
        reader.reload()
        expect(reader.get_by_text("This link has expired", exact=False)).to_be_visible()
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
        expect(download).to_be_visible()
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


@pytest.mark.parametrize(
    "zone,browser_zone,expected,edited",
    [
        ("UTC", "Asia/Kolkata", "2030-01-15T18:00:00.000Z", "2030-01-16T19:15:00.000Z"),
        ("Asia/Kolkata", "UTC", "2030-01-15T12:30:00.000Z", "2030-01-16T13:45:00.000Z"),
        (
            "America/New_York",
            "UTC",
            "2030-01-15T23:00:00.000Z",
            "2030-01-17T00:15:00.000Z",
        ),
    ],
)
def test_custom_share_expiry_uses_instance_timezone(
    server, browser, zone, browser_zone, expected, edited
):
    context = browser.new_context(timezone_id=browser_zone)
    try:
        page = context.new_page()

        def config(route):
            response = route.fetch()
            route.fulfill(response=response, json={**response.json(), "timezone": zone})

        page.route("**/api/config", config)
        login(page, server.url)
        page.locator('summary[aria-label="Actions for 資料"]').click()
        page.get_by_role("button", name="Share Link", exact=True).click()
        page.get_by_label("Link label (optional)", exact=True).fill("Zone test")
        page.get_by_role("button", name="Custom", exact=True).click()
        if zone == "America/New_York":
            creates = []
            page.on(
                "request",
                lambda request: (
                    creates.append(request)
                    if request.method == "POST"
                    and urlparse(request.url).path == "/api/v1/shares"
                    else None
                ),
            )
            for value, message in (
                ("2030-03-10T02:30", "does not exist"),
                ("2030-11-03T01:30", "occurs twice"),
            ):
                page.locator("#share-custom-expiry").fill(value)
                page.get_by_role("button", name="Create Share Link", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text(message)
                assert not creates
        page.locator("#share-custom-expiry").fill("2030-01-15T18:00")
        with page.expect_request(
            lambda request: (
                urlparse(request.url).path == "/api/v1/shares"
                and request.method == "POST"
            )
        ) as created:
            page.get_by_role("button", name="Create Share Link", exact=True).click()
        assert created.value.post_data_json["expires_at"] == expected
        expect(page.get_by_label("Share Link", exact=True)).to_be_visible()
        page.get_by_role("button", name="Done", exact=True).click()
        share = page.request.get(server.url + "/api/v1/shares").json()["shares"][0]
        # A label-only edit must preserve the original seconds, not its minute input.
        assert page.request.patch(
            server.url + "/api/v1/shares",
            data={
                "share_id": share["id"],
                "expires_at": expected.replace(":00.000Z", ":37.000Z"),
            },
        ).ok
        page.goto(server.url + "/?view=shares")
        before = page.request.get(server.url + "/api/v1/shares").json()["shares"][0]
        page.get_by_role("button", name="Edit link Zone test", exact=True).click()
        expiry = page.get_by_label(f"Expiry date and time ({zone})", exact=True)
        expect(expiry).to_have_value("2030-01-15T18:00")
        page.get_by_label("Link label", exact=True).fill("Zone label-only")
        with page.expect_request(
            lambda request: (
                urlparse(request.url).path == "/api/v1/shares"
                and request.method == "PATCH"
            )
        ) as patched:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert "expires_at" not in patched.value.post_data_json
        expect(
            page.get_by_role("button", name="Edit link Zone label-only", exact=True)
        ).to_be_visible()
        after = page.request.get(server.url + "/api/v1/shares").json()["shares"][0]
        assert after["expires_at"] == before["expires_at"]
        page.get_by_role("button", name="Edit link Zone label-only", exact=True).click()
        expiry.fill("2030-01-16T19:15")
        with page.expect_request(
            lambda request: (
                urlparse(request.url).path == "/api/v1/shares"
                and request.method == "PATCH"
            )
        ) as patched:
            page.get_by_role("button", name="Save changes", exact=True).click()
        assert patched.value.post_data_json["expires_at"] == edited
        expect(page.get_by_role("dialog", name="Edit share link")).not_to_be_visible()
        if zone == "America/New_York":
            page.get_by_role(
                "button", name="Edit link Zone label-only", exact=True
            ).click()
            requests = []
            page.on(
                "request",
                lambda request: (
                    requests.append(request) if request.method == "PATCH" else None
                ),
            )
            for value, message in (
                ("2030-03-10T02:30", "does not exist"),
                ("2030-11-03T01:30", "occurs twice"),
            ):
                expiry.fill(value)
                page.get_by_role("button", name="Save changes", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text(message)
                assert not requests
            page.get_by_role("dialog", name="Edit share link").get_by_role(
                "button", name="Cancel"
            ).click()
            assert page.request.patch(
                server.url + "/api/v1/shares",
                data={"share_id": share["id"], "expires_at": "2030-11-03T06:30:37Z"},
            ).ok
            page.reload()
            before = page.request.get(server.url + "/api/v1/shares").json()["shares"][0]
            page.get_by_role(
                "button", name="Edit link Zone label-only", exact=True
            ).click()
            expect(expiry).to_have_value("2030-11-03T01:30")
            page.get_by_label("Link label", exact=True).fill("Fold kept")
            page.get_by_role("button", name="Save changes", exact=True).click()
            expect(
                page.get_by_role("button", name="Edit link Fold kept", exact=True)
            ).to_be_visible()
            after = page.request.get(server.url + "/api/v1/shares").json()["shares"][0]
            assert after["expires_at"] == before["expires_at"]
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=ARTIFACTS / f"expiry-{zone.replace('/', '-')}.png")
    finally:
        context.close()


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
            page.get_by_label(re.compile(r"^Expiry date and time")).fill(
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
    expect(
        page.get_by_role("group", name="Filter Shared Links").locator(
            'button[aria-pressed="true"]'
        )
    ).to_be_focused()

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


@pytest.mark.parametrize("action", ["edit", "revoke"])
@pytest.mark.parametrize("late_result", ["snapshot", "error"])
def test_share_mutation_supersedes_pending_manual_refresh(
    server, desktop, action, late_result
):
    _, page = desktop
    login(page, server.url)
    path = server.control()["seed"]["file"]
    created = page.request.post(
        server.url + "/api/v1/shares",
        data={"path": path, "label": "Review", "expires_in_days": 7},
    )
    assert created.ok, created.text()
    share = created.json()
    page.goto(server.url + "/?view=shares")
    row = page.locator(f'[data-share-id="{share["id"]}"]')
    expect(row).to_contain_text("Review")
    snapshot = page.request.get(server.url + "/api/v1/shares").json()
    held = []
    reads = []

    def hold_first_read(route):
        if route.request.method != "GET":
            route.continue_()
            return
        reads.append(route.request)
        if len(reads) == 1:
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/shares{,?*}", hold_first_read)
    refresh = page.get_by_role("button", name="Refresh Shared Links", exact=True)
    with page.expect_request(
        lambda request: (
            request.url.split("?", 1)[0].endswith("/api/v1/shares")
            and request.method == "GET"
        )
    ):
        refresh.click()
    expect(refresh).to_be_disabled()
    assert len(held) == 1
    try:
        if action == "edit":
            row.get_by_role("button", name="Edit link Review", exact=True).click()
            page.get_by_label("Link label", exact=True).fill("Review updated")
            dialog = page.get_by_role("dialog", name="Edit share link", exact=True)
            submit = dialog.get_by_role("button", name="Save changes", exact=True)
            method = "PATCH"
        else:
            row.get_by_role(
                "button", name=__import__("re").compile("^Revoke Link for ")
            ).click()
            dialog = page.get_by_role("dialog", name="Revoke This Link?", exact=True)
            submit = dialog.get_by_role("button", name="Revoke Link", exact=True)
            method = "DELETE"
        with page.expect_response(
            lambda response: (
                response.url.split("?", 1)[0].endswith("/api/v1/shares")
                and response.request.method == method
            )
        ) as mutation:
            submit.click()
        assert mutation.value.ok, mutation.value.text()
        expect(dialog).to_have_count(0)
        # The mutation must load fresh state while the older snapshot is held.
        if action == "edit":
            expect(row).to_contain_text("Review updated")
        else:
            expect(row).to_have_count(0)
        assert len(reads) == 2
        expect(refresh).to_be_disabled()

        if late_result == "snapshot":
            held.pop().fulfill(json=snapshot)
        else:
            held.pop().fulfill(
                status=503, json={"detail": "Superseded share refresh failed"}
            )
        expect(refresh).to_be_enabled()
        if action == "edit":
            expect(row).to_contain_text("Review updated")
        else:
            expect(row).to_have_count(0)
        expect(page.locator(".page-load-error")).to_have_count(0)
        expect(page.locator(".toast")).to_have_count(0)

        if late_result == "error":
            page.unroute("**/api/v1/shares{,?*}", hold_first_read)
            page.route(
                "**/api/v1/shares{,?*}",
                lambda route: route.fulfill(
                    status=503, json={"detail": "Current share refresh failed"}
                ),
            )
            refresh.click()
            expect(page.locator(".page-load-error")).to_contain_text(
                "Current share refresh failed"
            )
            expect(page.locator(".toast-error")).to_contain_text(
                "Current share refresh failed"
            )
            retry = page.get_by_role("button", name="Try Again", exact=True)
            expect(retry).to_be_enabled()
            page.unroute("**/api/v1/shares{,?*}")
            retry.click()
            expect(page.locator(".page-load-error")).to_have_count(0)
            expect(page.locator(".toast:not(.toast-error)")).to_contain_text(
                "Shared links refreshed."
            )
    finally:
        for route in held:
            route.fulfill(json=snapshot)
        page.unroute_all(behavior="ignoreErrors")
