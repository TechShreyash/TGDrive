"""Real Chromium coverage of the production UI and HTTP application.

TGDRIVE_E2E=1 uv run --group e2e pytest -m e2e tests/browser/test_browser_e2e.py
Install Chromium first: uv run --group e2e playwright install --with-deps chromium
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest

pytestmark = pytest.mark.e2e
if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip(
        "Set TGDRIVE_E2E=1 to run real Chromium E2E tests", allow_module_level=True
    )

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "offline browser test password"
CONTROL = {"x-fixture-control": "tgdrive-offline-browser-fixture"}
ARTIFACTS = Path(os.environ.get("TGDRIVE_E2E_ARTIFACTS", ROOT / "cache/browser-e2e"))
UPLOAD_RECOVERY_RECORDS = """(() => {
    const prefix = 'tgdrive.uploads.v2:';
    const rows = new Map(JSON.parse(localStorage.getItem('tgdrive.uploads.v2') || '[]')
        .map(row => [row.key, row]));
    for (const key of Object.keys(localStorage)) {
        if (!key.startsWith(prefix)) continue;
        const row = JSON.parse(localStorage.getItem(key));
        if (row === null) rows.delete(key.slice(prefix.length));
        else rows.set(key.slice(prefix.length), row);
    }
    return [...rows.values()];
})()"""


class Server:
    def __init__(self, url: str) -> None:
        self.url = url

    def control(self, path: str = "/state", *, method: str = "GET") -> dict[str, Any]:
        request = Request(  # noqa: S310 - fixture URL is a fixed loopback server
            self.url + "/__test__" + path, headers=CONTROL, method=method
        )
        with urlopen(request, timeout=5) as response:  # noqa: S310 - loopback test server
            return json.load(response)

    def wait_started(self, count: int) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            started = self.control()["started"]
            if len(started) >= count:
                return started
            time.sleep(0.05)
        pytest.fail(
            f"Expected {count} uploads to start; fixture state: {self.control()}"
        )

    def finish(self, transfer_id: str) -> None:
        self.control(f"/finish/{transfer_id}", method="POST")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "fixture.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(  # noqa: S603 - fixed local test process
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.support.browser_transfer_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            ready = False
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urlopen(url + "/healthz", timeout=1) as response:  # noqa: S310
                        ready = response.status == 200
                except OSError:
                    pass
                if ready:
                    break
                time.sleep(0.05)
            assert ready, log_path.read_text(errors="replace")
            yield Server(url)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as engine:
        chromium = engine.chromium.launch(headless=True, channel="chromium")
        yield chromium
        chromium.close()


@pytest.fixture
def desktop(browser: Any, request: pytest.FixtureRequest):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    context = browser.new_context(viewport={"width": 1440, "height": 960})
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    page.set_default_timeout(10000)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield context, page
    if not page.is_closed():
        page.unroute_all(behavior="ignoreErrors")
    context.tracing.stop(path=ARTIFACTS / f"{request.node.name}.zip")
    context.close()
    assert errors == [], f"Uncaught browser errors: {errors}"


def login(page: Any, url: str | None = None) -> None:
    if url:
        page.goto(url)
    expect(page.locator("#login-dialog")).to_be_visible()
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.locator("#login-dialog")).not_to_be_visible()


def open_admin(page: Any) -> None:
    sidebar = page.locator(".sidebar")
    if page.viewport_size["width"] <= 720:
        if sidebar.get_attribute("inert") is not None:
            page.get_by_role("button", name="Toggle Navigation", exact=True).click()
    else:
        expect(sidebar).not_to_have_attribute("inert", "")
    tools = sidebar.locator(".sidebar-tools")
    if tools.get_attribute("open") is None:
        tools.locator("summary").click()


def upload(page: Any, names: list[str], *, add: bool = False) -> None:
    page.locator("#new-button:visible, #compact-new-button:visible").click()
    button = page.locator("#file-upload-action")
    with page.expect_file_chooser() as chooser:
        button.click()
    chooser.value.set_files(
        [
            {
                "name": name,
                "mimeType": "application/octet-stream",
                "buffer": b"x" * 65536,
            }
            for name in names
        ]
    )


def cancel_all(page: Any) -> None:
    page.get_by_role("button", name="Cancel All", exact=True).click()
    page.get_by_role("dialog", name="Cancel All Uploads?", exact=True).get_by_role(
        "button", name="Cancel Uploads", exact=True
    ).click()


def select_folder(page: Any, directory: Path):
    page.locator("#new-button:visible, #compact-new-button:visible").click()
    with page.expect_file_chooser() as chooser:
        page.locator("#folder-upload-action").click()
    chooser.value.set_files(str(directory))
    dialog = page.get_by_role("dialog", name="Upload Folder", exact=True)
    expect(dialog).to_be_visible()
    return dialog


def select_item(page: Any, name: str) -> None:
    checkbox = page.get_by_label("Select " + name, exact=True)
    if not checkbox.count():
        page.get_by_role("button", name="Select", exact=True).click()
    checkbox.check()


def fixture_nodes(page: Any, server: Server, parent: str) -> list[dict[str, Any]]:
    response = page.request.get(
        server.url + "/api/v1/nodes", params={"parent_id": parent, "limit": 500}
    )
    assert response.ok, response.text()
    return response.json()["items"]


def finish_fixture_uploads(page: Any, server: Server, count: int):
    started = server.wait_started(count)
    for item in started:
        server.finish(item["id"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        records = server.control()["records"]
        if all(records[item["id"]]["status"] == "completed" for item in started):
            return started
        page.wait_for_timeout(100)
    pytest.fail(f"Fixture uploads did not finish: {server.control()['records']}")


def row(page: Any, filename: str):
    # Ordinary files render directly; folder children mount when their task opens.
    # Open each batch once; avoid toggling an already expanded group.
    expect(page.locator("#transfer-list")).to_be_visible()
    for toggle in page.locator("button.transfer-batch-toggle").all():
        if toggle.get_attribute("aria-expanded") == "false":
            toggle.click()
    return page.locator(".transfer-item").filter(
        has=page.locator(".transfer-item-name", has_text=filename)
    )


def assert_layout(page: Any) -> None:
    # Expanded batches intentionally scroll. Confirm that each file action is
    # reachable, rather than treating a row below the scrollport as covered.
    for control in page.locator(
        "#transfer-dialog .transfer-item-actions button:not(:disabled)"
    ).all():
        control.scroll_into_view_if_needed()
        assert control.evaluate("""button => {
          const rect=button.getBoundingClientRect();
          const hit=document.elementFromPoint(
            rect.left+rect.width/2,rect.top+rect.height/2);
          return hit===button || button.contains(hit);
        }"""), control.get_attribute("aria-label") or control.inner_text()
    metrics = page.evaluate(
        """() => ({
          width: innerWidth,
          scrollWidth: document.documentElement.scrollWidth,
          unnamedButtons: [...document.querySelectorAll('button')].filter(button => {
            const rect = button.getBoundingClientRect();
            return rect.width && rect.height && !button.closest('[hidden]')
              && !button.textContent.trim() && !button.getAttribute('aria-label')
              && !button.getAttribute('aria-labelledby');
          }).length,
          coveredTransferControls: [...document.querySelectorAll(
            '#transfer-dialog button:not(:disabled)'
          )].filter(button => {
            const rect = button.getBoundingClientRect();
            if (!rect.width || !rect.height || button.hidden) return false;
            const x=rect.left+rect.width/2,y=rect.top+rect.height/2;
            if(x<0 || y<0 || x>=innerWidth || y>=innerHeight) return false;
            for(let ancestor=button.parentElement;ancestor;
                ancestor=ancestor.parentElement){
              const style=getComputedStyle(ancestor);
              const clip=ancestor.getBoundingClientRect();
              if(style.overflowX!=='visible' && (x<clip.left || x>=clip.right))
                return false;
              if(style.overflowY!=='visible' && (y<clip.top || y>=clip.bottom))
                return false;
            }
            const hit = document.elementFromPoint(
              rect.left + rect.width / 2, rect.top + rect.height / 2
            );
            return hit !== button && !button.contains(hit);
          }).map(button => {
            const rect = button.getBoundingClientRect();
            const hit = document.elementFromPoint(
              rect.left + rect.width / 2, rect.top + rect.height / 2
            );
            return {label: button.getAttribute('aria-label') || button.textContent,
              hit: hit?.className, top: rect.top, bottom: rect.bottom};
          }),
          panel: (() => {
            const panel = document.querySelector('#transfer-dialog');
            if (!panel) return {left: 0, right: 0, bottom: 0};
            const rect = panel.getBoundingClientRect();
            return {left: rect.left, right: rect.right, bottom: rect.bottom};
          })()
        })"""
    )
    assert metrics["scrollWidth"] <= metrics["width"]
    assert metrics["unnamedButtons"] == 0
    assert metrics["coveredTransferControls"] == []
    assert metrics["panel"]["left"] >= 0
    assert metrics["panel"]["right"] <= metrics["width"]
    assert metrics["panel"]["bottom"] <= page.viewport_size["height"]


def preview_dialog(page: Any):
    # Non-native files begin in a compact checking shell. Wait for supported
    # descriptors to expand before checking the full-window preview contract.
    dialog = page.locator("dialog.file-preview:not(.file-details)")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_attribute("aria-labelledby", "preview-title")
    assert dialog.evaluate("element => element.matches(':modal')")
    expect(
        dialog.get_by_role("button", name="Close Preview", exact=True)
    ).to_be_visible()
    download = dialog.get_by_role("link", name="Download Original", exact=True)
    expect(download).to_be_visible()
    metrics = dialog.evaluate("""element => {
      const rect=element.getBoundingClientRect();
      return {left:rect.left,top:rect.top,width:rect.width,height:rect.height,
        viewportWidth:innerWidth,viewportHeight:innerHeight};
    }""")
    assert abs(metrics["left"]) <= 1 and abs(metrics["top"]) <= 1
    assert abs(metrics["width"] - metrics["viewportWidth"]) <= 1
    assert abs(metrics["height"] - metrics["viewportHeight"]) <= 1
    assert download.bounding_box()["y"] < 100, (
        "Original download belongs in the top toolbar"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    return dialog


def file_details_dialog(page: Any, filename: str):
    dialog = page.locator("dialog.file-preview.file-details")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_attribute("aria-labelledby", "preview-title")
    assert dialog.evaluate("element=>element.matches(':modal')")
    expect(dialog.get_by_role("heading", name=filename, exact=True)).to_be_visible()
    expect(dialog.locator("#file-details-reason")).to_contain_text("Download")
    download = dialog.get_by_role("link", name="Download Original", exact=True)
    expect(download).to_be_visible()
    expect(download).to_have_count(1)
    expect(download).to_have_attribute("download", filename)
    expect(
        dialog.get_by_role("button", name="Back to files", exact=True)
    ).to_have_count(0)
    expect(
        dialog.get_by_role("button", name="Close Preview", exact=True)
    ).to_be_visible()
    metadata = dialog.locator("dl").evaluate("""element => Object.fromEntries(
      [...element.querySelectorAll('dt')].map(term=>[
        term.textContent.trim(),term.nextElementSibling?.textContent.trim()
      ]))""")
    assert set(metadata) == {"Type", "Size", "Added"}
    assert all(value and value != "—" for value in metadata.values())
    bounds = dialog.bounding_box()
    viewport = page.viewport_size
    assert bounds is not None
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= viewport["width"]
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= viewport["height"]
    assert bounds["width"] <= 520
    assert bounds["height"] < viewport["height"] - 20
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    assert dialog.locator("video,audio,canvas,iframe").count() == 0
    return dialog, metadata


def assert_rendered_pdf_page(page: Any, filename: str, number: int):
    canvas = page.get_by_role("img", name=f"{filename}, page {number}", exact=True)
    expect(canvas).to_be_visible(timeout=20000)
    expect(canvas).to_be_in_viewport()
    assert canvas.evaluate("""canvas => {
      const context=canvas.getContext('2d');
      const data=context.getImageData(0,0,canvas.width,canvas.height).data;
      let dark=0;
      for(let i=0;i<data.length;i+=4)
        if(data[i+3]>0 && data[i]<150 && data[i+1]<150 && data[i+2]<150) dark++;
      return dark>100;
    }"""), "PDF page must contain rendered glyphs, not an empty canvas"
    return canvas


def assert_pdf_header_layout(page: Any, dialog: Any) -> None:
    expect(dialog.locator(".preview-header")).to_have_count(1)
    toolbar = dialog.locator(".pdf-toolbar")
    expect(toolbar).to_have_count(1)
    assert toolbar.evaluate("element=>Boolean(element.closest('.preview-header'))")
    layout = dialog.evaluate("""element => {
      const header=element.querySelector('.preview-header').getBoundingClientRect();
      const scroll=element.querySelector('.pdf-scroll').getBoundingClientRect();
      return {headerHeight:header.height,headerBottom:header.bottom,
        scrollTop:scroll.top};
    }""")
    # Phone readers use a second row for navigation and accessible touch targets.
    max_height = 120 if page.viewport_size["width"] <= 720 else 72
    assert layout["headerHeight"] <= max_height, "Preview controls must stay compact"
    assert abs(layout["scrollTop"] - layout["headerBottom"]) <= 1, (
        "PDF viewport must start below the one header, without a second toolbar"
    )
    if page.viewport_size["width"] > 720:
        header = dialog.locator(".preview-header").bounding_box()
        for name in ("Zoom in", "Zoom Out", "Fit Width"):
            control = toolbar.get_by_role("button", name=name, exact=True)
            expect(control).to_be_visible()
            bounds = control.bounding_box()
            assert bounds["y"] >= header["y"] - 1
            assert bounds["y"] + bounds["height"] <= header["y"] + header["height"] + 1


def assert_pdf_canvas_budget(dialog: Any, *, max_canvases: int | None = 5) -> None:
    sizes = dialog.locator(".pdf-page canvas").evaluate_all(
        "canvases=>canvases.map(canvas=>canvas.width*canvas.height)"
    )
    assert sizes
    if max_canvases is not None:
        assert len(sizes) <= max_canvases
    assert all(size <= 2_000_000 for size in sizes)
    assert sum(sizes) <= 10_000_000


def test_resumable_multiple_files_navigation_and_mobile_layout(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    upload(page, ["first.txt", "second.txt"])
    started = {item["name"]: item["id"] for item in server.wait_started(2)}
    expect(row(page, "first.txt")).to_have_attribute("data-status", "uploading")
    expect(row(page, "second.txt")).to_have_attribute("data-status", "uploading")
    page.get_by_role("button", name="Hide Transfer Details", exact=True).click()
    expect(page.locator("#transfer-list")).not_to_be_visible()
    page.get_by_role("link", name="Trash", exact=True).click()
    expect(page.get_by_role("heading", name="Trash", exact=True)).to_be_visible()
    page.locator(".drive-nav").get_by_role("link", name="My Files", exact=True).click()
    page.get_by_role("button", name="資料", exact=True).dblclick()
    expect(page.get_by_role("button", name="résumé #1.txt", exact=True)).to_be_visible()
    page.get_by_role("button", name="Show Transfer Details", exact=True).click()
    expect(row(page, "first.txt").locator(".transfer-metrics")).to_contain_text("/s")
    expect(row(page, "first.txt").locator(".transfer-metrics")).to_contain_text(
        "remaining"
    )
    assert_layout(page)
    page.screenshot(path=ARTIFACTS / "react-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert_layout(page)
    page.screenshot(path=ARTIFACTS / "react-mobile.png", full_page=True)
    expect(
        page.get_by_role("button", name="Open transfer progress", exact=True)
    ).to_be_visible()
    expect(page.locator(".transfer-center")).to_have_count(0)
    page.set_viewport_size({"width": 1440, "height": 900})
    server.finish(started["first.txt"])
    expect(row(page, "first.txt")).to_have_attribute("data-status", "completed")
    clear = page.get_by_role("button", name="Clear Finished", exact=True)
    expect(clear).to_be_enabled()
    clear.click()
    expect(row(page, "first.txt")).to_have_count(0)
    expect(row(page, "second.txt")).to_be_visible()
    server.finish(started["second.txt"])
    expect(row(page, "second.txt")).to_have_attribute("data-status", "completed")
    expect(clear).to_be_enabled()
    clear.click()
    expect(page.locator(".transfer-batch")).to_have_count(0)
    page.wait_for_timeout(1800)
    expect(page.locator(".transfer-batch")).to_have_count(0)


def test_reload_reselect_checks_original_and_resumes_acknowledged_bytes(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    payload = {
        "name": "resumable.bin",
        "mimeType": "application/octet-stream",
        "buffer": b"x" * (10 * 1024 * 1024),
    }

    def interrupt_after_first_chunk(route):
        if (
            route.request.method == "PATCH"
            and route.request.headers.get("upload-offset") != "0"
        ):
            route.abort()
        else:
            route.continue_()

    page.route("**/api/v1/uploads/*", interrupt_after_first_chunk)
    page.locator("#file-input").set_input_files(payload)
    # Network interruptions retry after 2, 4 and 8 seconds. Let those attempts
    # exhaust before testing reload/reselection; do not block their hashing.
    expect(row(page, "resumable.bin")).to_have_attribute(
        "data-status", "error", timeout=25000
    )
    assert server.control()["started"] == []
    page.unroute("**/api/v1/uploads/*", interrupt_after_first_chunk)
    page.add_init_script("""
      const originalDigest = crypto.subtle.digest.bind(crypto.subtle);
      crypto.subtle.digest = async (...args) => {
        await new Promise(resolve => { window.__releaseOriginalCheck = resolve; });
        return originalDigest(...args);
      };
    """)
    page.reload()
    expect(row(page, "resumable.bin")).to_have_attribute("data-status", "error")
    expect(row(page, "resumable.bin").locator(".transfer-recovery")).to_be_visible()
    with page.expect_file_chooser() as chooser:
        row(page, "resumable.bin").get_by_role(
            "button", name="Choose File", exact=True
        ).click()
    resumed_offsets = []
    page.on(
        "request",
        lambda request: (
            resumed_offsets.append(request.headers.get("upload-offset"))
            if request.method == "PATCH" and "/api/v1/uploads/" in request.url
            else None
        ),
    )
    chooser.value.set_files(payload)
    checking = row(page, "resumable.bin")
    expect(checking).to_have_attribute("data-status", "verifying")
    expect(checking.get_by_role("progressbar")).to_have_attribute("value", "0.8")
    expect(checking.locator(".transfer-bytes")).to_have_text("8 / 10 MB")
    expect(checking.locator(".transfer-metrics")).not_to_contain_text("/s")
    expect(checking.locator(".transfer-verification .spinner")).to_be_visible()
    page.screenshot(path=ARTIFACTS / "resume-keeps-upload-progress.png", full_page=True)
    page.evaluate("window.__releaseOriginalCheck()")
    started = server.wait_started(1)
    assert resumed_offsets and resumed_offsets[0] == str(8 * 1024 * 1024)
    expect(row(page, "resumable.bin")).to_have_attribute("data-status", "uploading")
    server.finish(started[0]["id"])
    expect(row(page, "resumable.bin")).to_have_attribute("data-status", "completed")
    expect(page.get_by_role("button", name="resumable.bin", exact=True)).to_be_visible()


def test_cancellation_waits_for_server_confirmation(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    upload(page, ["cancel-me.txt"])
    started = server.wait_started(1)
    expect(row(page, "cancel-me.txt")).to_be_visible()
    page.get_by_role("button", name="Cancel cancel-me.txt", exact=True).click()
    expect(row(page, "cancel-me.txt")).to_have_attribute("data-status", "cancelled")
    assert server.control()["records"][started[0]["id"]]["status"] == "cancelled"
    expect(page.get_by_role("button", name="cancel-me.txt", exact=True)).to_have_count(
        0
    )


def test_public_share_stays_readonly_across_move_and_revocation(
    server: Server, desktop: tuple[Any, Any], browser: Any
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_label("Actions for 資料", exact=True).click()
    page.get_by_role("button", name="Share Link", exact=True).click()
    page.get_by_role("button", name="Create Share Link", exact=True).click()
    link = page.get_by_label("Share Link", exact=True).input_value()
    assert "share_" in link
    page.get_by_role("button", name="Done", exact=True).click()
    public_context = browser.new_context(viewport={"width": 390, "height": 844})
    try:
        public = public_context.new_page()
        public.goto(link)
        expect(
            public.get_by_role("button", name="résumé #1.txt", exact=True)
        ).to_be_visible()
        expect(
            public.get_by_role("button", name="Upload Files", exact=True)
        ).to_have_count(0)
        expect(public.locator(".item-actions")).to_have_count(0)
        public_file = public.get_by_role("button", name="résumé #1.txt", exact=True)
        public_file.click()
        expect(public.locator("dialog.file-preview")).to_have_count(0)
        public_file.dblclick()
        public_dialog = preview_dialog(public)
        expect(public_dialog.locator("pre")).to_contain_text("public content")
        public_dialog.get_by_role("button", name="Close Preview", exact=True).click()
        expect(public_file).to_be_focused()
        page.get_by_label("Actions for 資料", exact=True).click()
        page.get_by_role("button", name="Move", exact=True).click()
        page.get_by_role("dialog").get_by_role(
            "button", name="Private folder", exact=True
        ).click()
        page.get_by_role("button", name="Move Here", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        public.reload()
        expect(
            public.get_by_role("button", name="résumé #1.txt", exact=True)
        ).to_be_visible()
        page.get_by_role("link", name="Shared Links", exact=True).click()
        page.get_by_role("button", name="Revoke Link for 資料", exact=True).click()
        page.get_by_role("button", name="Revoke Link", exact=True).click()
        expect(
            page.get_by_role("heading", name="No Active Shared Links")
        ).to_be_visible()
        public.reload()
        expect(
            public.get_by_role("heading", name="Shared Folder Unavailable")
        ).to_be_visible()
    finally:
        public_context.close()


def test_unicode_names_are_text_and_bulk_trash_can_be_restored(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.locator("#new-button:visible, #compact-new-button:visible").click()
    page.get_by_role("button", name="New Folder", exact=True).click()
    name = "資料 <img src=x onerror=alert(1)>"
    page.get_by_label("Folder Name", exact=True).fill(name)
    page.get_by_role("button", name="Create Folder", exact=True).click()
    expect(page.get_by_role("button", name=name, exact=True)).to_be_visible()
    assert page.locator("img[src=x]").count() == 0
    select_item(page, name)
    page.get_by_role("button", name="Trash", exact=True).click()
    expect(page.get_by_role("button", name=name, exact=True)).to_have_count(0)
    page.get_by_role("link", name="Trash", exact=True).click()
    expect(page.get_by_role("button", name=name, exact=True)).to_be_visible()
    select_item(page, name)
    page.get_by_role("button", name="Restore", exact=True).click()
    expect(page.get_by_role("button", name=name, exact=True)).to_have_count(0)
    page.locator(".drive-nav").get_by_role("link", name="My Files", exact=True).click()
    expect(page.get_by_role("button", name=name, exact=True)).to_be_visible()


def test_queued_deletion_is_not_reported_complete_early(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    select_item(page, "Private folder")
    page.get_by_role("button", name="Trash", exact=True).click()
    page.get_by_role("link", name="Trash", exact=True).click()
    select_item(page, "Private folder")
    page.get_by_role("button", name="Delete Permanently", exact=True).click()
    expect(page.get_by_role("dialog")).to_contain_text(
        "The original files will remain in Telegram."
    )
    expect(page.get_by_role("dialog")).to_contain_text(
        "Items disappear from Trash once deletion starts and are removed "
        "in the background."
    )
    expect(page.get_by_role("dialog")).to_contain_text(
        "You cannot restore them after deletion."
    )
    page.get_by_role("dialog").get_by_role(
        "button", name="Delete Permanently", exact=True
    ).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(
        page.get_by_role("region", name="File Deletion").get_by_role("status")
    ).to_have_text("Deleting 1 item…")
    expect(page.get_by_role("button", name="Private folder", exact=True)).to_have_count(
        0
    )
    expect(page.get_by_text("Permanently deleted 1 item.", exact=True)).to_have_count(0)
    page.locator(".drive-nav").get_by_role("link", name="My Files", exact=True).click()
    expect(page.get_by_role("region", name="File Deletion")).to_have_count(0)
    page.get_by_role("link", name="Trash", exact=True).click()
    expect(page.get_by_role("region", name="File Deletion")).to_be_visible()
    expect(page.get_by_role("heading", name="Trash Is Empty")).to_be_visible()
    records = server.control()["records"]
    deletion_id = next(
        key for key, value in records.items() if value["phase"] == "deleting"
    )
    failed_refreshes = []

    def trash_refresh(route):
        if not failed_refreshes:
            failed_refreshes.append(True)
            route.fulfill(status=503, json={"detail": "Temporary listing failure"})
        else:
            route.continue_()

    page.route("**/api/v1/trash*", trash_refresh)
    with page.expect_response(
        lambda response: "/api/v1/trash" in response.url and response.status == 200
    ):
        server.finish(deletion_id)
    expect(page.get_by_role("region", name="File Deletion")).to_have_count(0)
    expect(page.get_by_role("heading", name="Trash Is Empty")).to_be_visible(
        timeout=10000
    )
    assert failed_refreshes


@pytest.mark.parametrize("automatic_dismissal", [False, True])
def test_fast_deletion_is_visible_when_an_older_job_poll_is_in_flight(
    server: Server, desktop: tuple[Any, Any], automatic_dismissal: bool
) -> None:
    _, page = desktop
    held = []
    hold = True

    def jobs(route):
        if hold:
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/jobs", jobs)
    login(page, server.url)
    select_item(page, "Private folder")
    page.get_by_role("button", name="Trash", exact=True).click()
    page.get_by_role("link", name="Trash", exact=True).click()
    select_item(page, "Private folder")
    page.get_by_role("button", name="Delete Permanently", exact=True).click()
    page.get_by_role("dialog").get_by_role(
        "button", name="Delete Permanently", exact=True
    ).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    notice = page.get_by_role("region", name="File Deletion", exact=True)
    expect(notice.get_by_role("status")).to_have_text("Deleting 1 item…")
    records = server.control()["records"]
    deletion_id = next(
        key for key, value in records.items() if value["phase"] == "deleting"
    )
    server.finish(deletion_id)
    assert held
    hold = False
    for route in held:
        route.fulfill(status=200, json={"jobs": []})
    expect(page.get_by_text("Permanently deleted 1 item.", exact=True)).to_be_visible()
    expect(notice).to_have_count(0)
    expect(page.get_by_role("heading", name="Trash Is Empty")).to_be_visible()
    if not automatic_dismissal:
        page.get_by_role("button", name="Dismiss Notification", exact=True).click()
    expect(page.get_by_text("Permanently deleted 1 item.", exact=True)).to_have_count(
        0, timeout=9000
    )
    page.locator(".drive-nav").get_by_role("link", name="My Files", exact=True).click()
    expect(notice).to_have_count(0)
    page.get_by_role("link", name="Trash", exact=True).click()
    with page.expect_response(lambda response: response.url.endswith("/api/v1/jobs")):
        page.reload()
    expect(notice).to_have_count(0)
    expect(page.get_by_text("Permanently deleted 1 item.", exact=True)).to_have_count(0)


def test_keyboard_dialog_focus_and_unicode_search(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    new = page.locator("#new-button")
    new.focus()
    new.press("Enter")
    menu = page.locator("#new-menu")
    expect(menu).to_be_visible()
    expect(menu.get_by_role("button", name="Upload Files", exact=True)).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(menu.get_by_role("button", name="Upload Folder", exact=True)).to_be_focused()
    page.keyboard.press("End")
    expect(
        menu.get_by_role("button", name="Import torrent", exact=True)
    ).to_be_focused()
    page.keyboard.press("Home")
    expect(menu.get_by_role("button", name="Upload Files", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    expect(new).to_be_focused()
    expect(new).to_have_attribute("aria-expanded", "false")
    summary = page.get_by_label("Actions for 資料", exact=True)
    summary.click()
    page.get_by_role("button", name="Rename", exact=True).click()
    expect(page.get_by_label("Name", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(summary).to_be_focused()
    search = page.get_by_role("searchbox", name="Search Files and Folders", exact=True)
    search.fill("résumé")
    search.press("Enter")
    expect(
        page.get_by_role("heading", name="Search Results", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("button", name="résumé #1.txt", exact=True)).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    compact_new = page.locator("#compact-new-button")
    expect(compact_new).to_have_count(0)
    search.press("Escape")
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    expect(compact_new).to_be_visible()
    compact_new.focus()
    compact_new.press("Enter")
    expect(menu.get_by_role("button", name="Upload Files", exact=True)).to_be_focused()
    page.keyboard.press("ArrowUp")
    expect(
        menu.get_by_role("button", name="Import torrent", exact=True)
    ).to_be_focused()
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    expect(compact_new).to_be_focused()


def test_move_and_restore_require_explicit_public_exposure_confirmation(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    seed = server.control()["seed"]
    response = page.request.post(
        server.url + "/api/v1/shares",
        data={"path": seed["folder"], "expires_in_days": 7},
    )
    assert response.ok
    page.get_by_label("Actions for Private folder", exact=True).click()
    page.get_by_role("button", name="Move", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="資料", exact=True).click()
    page.get_by_role("button", name="Move Here", exact=True).click()
    confirmation = page.get_by_label(
        "I understand that files moved here may become public.", exact=True
    )
    expect(confirmation).to_be_visible()
    expect(page.get_by_role("button", name="Move Here", exact=True)).to_be_disabled()
    confirmation.check()
    page.get_by_role("button", name="Move Here", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.get_by_role("button", name="資料", exact=True).dblclick()
    select_item(page, "Private folder")
    page.get_by_role("button", name="Trash", exact=True).click()
    page.get_by_role("link", name="Trash", exact=True).click()
    select_item(page, "Private folder")
    page.get_by_role("button", name="Restore", exact=True).click()
    expect(
        page.get_by_role("heading", name="Restore to a Shared Folder?", exact=True)
    ).to_be_visible()
    page.get_by_role(
        "button", name="Restore and Allow Public Access", exact=True
    ).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.get_by_role("heading", name="Trash Is Empty")).to_be_visible()


def test_move_folder_navigation_clears_failed_listing_and_recovers(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    folder = server.control()["seed"]["folder"]
    failed = []

    def directory(route):
        if route.request.post_data_json["path"] == folder and not failed:
            failed.append(True)
            route.fulfill(status=503, json={"detail": "Folder listing interrupted"})
        else:
            route.continue_()

    page.route("**/api/getDirectory", directory)
    page.get_by_label("Actions for Private folder", exact=True).click()
    page.get_by_role("button", name="Move", exact=True).click()
    dialog = page.get_by_role("dialog")
    picker = dialog.locator(".folder-picker")
    picker.get_by_role("button", name="資料", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("Folder listing interrupted")
    expect(picker.get_by_role("button")).to_have_count(0)
    dialog.get_by_role("navigation", name="Destination breadcrumb").get_by_role(
        "button", name="My Files", exact=True
    ).click()
    expect(picker.get_by_role("button", name="資料", exact=True)).to_be_visible()
    expect(dialog.get_by_role("alert")).to_have_count(0)
    picker.get_by_role("button", name="資料", exact=True).click()
    expect(picker).to_have_attribute("aria-busy", "false")
    expect(dialog.get_by_role("alert")).to_have_count(0)
    dialog.get_by_role("button", name="Cancel", exact=True).click()


def test_session_expiry_preserves_server_job_recovery(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    upload(page, ["session-recovery.txt"])
    started = server.wait_started(1)
    response = page.request.post(
        server.url + "/__test__/expire-session", headers=CONTROL
    )
    assert response.ok
    expect(page.locator("#login-dialog")).to_be_visible()
    saved = page.evaluate("JSON.stringify(Object.values(localStorage))")
    assert PASSWORD not in saved
    server.finish(started[0]["id"])
    login(page)
    expect(row(page, "session-recovery.txt")).to_have_attribute(
        "data-status", "completed"
    )
    expect(
        page.get_by_role("button", name="session-recovery.txt", exact=True)
    ).to_be_visible()


def test_svg_preview_renders_as_image_and_downloads_original(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    external_requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            external_requests.append(request.url)
            if "svg-external.invalid" in request.url
            else None
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="sample.svg", exact=True).dblclick()
    dialog = preview_dialog(page)
    image = dialog.get_by_role("img", name="sample.svg", exact=True)
    expect(image).to_be_visible()
    expect(
        dialog.locator(".preview-surface").locator("pre, iframe, object, embed, svg")
    ).to_have_count(0)
    page.wait_for_function(
        "() => { const img=document.querySelector('dialog img'); "
        "return img && img.complete && img.naturalWidth === 120; }"
    )
    pixel = image.evaluate("""img => {
        const canvas=document.createElement('canvas');
        canvas.width=120; canvas.height=80;
        const ctx=canvas.getContext('2d');
        ctx.drawImage(img,0,0);
        return Array.from(ctx.getImageData(40,40,1,1).data);
    }""")
    assert pixel == [24, 160, 88, 255], "SVG styles must render in image mode"
    assert page.evaluate("window.svgPreviewExecuted") is None
    assert external_requests == []
    with page.expect_download() as result:
        dialog.get_by_role("link", name="Download Original", exact=True).click()
    download = result.value
    assert download.suggested_filename == "sample.svg"
    original = Path(download.path()).read_bytes()
    assert original == (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
        b"<style>rect{fill:rgb(24,160,88)}</style>"
        b'<rect width="120" height="80"/>'
        b"<script>parent.svgPreviewExecuted=true</script>"
        b'<image href="https://svg-external.invalid/track.png" '
        b'width="1" height="1"/>'
        b"</svg>"
    )
    dialog.get_by_role("button", name="Close Preview", exact=True).click()


def test_safe_document_previews_download_and_pdf_canvas(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="sample.json", exact=True).dblclick()
    dialog = preview_dialog(page)
    expect(dialog.locator("pre")).to_contain_text(
        '"message": "<img src=x onerror=alert(1)>"'
    )
    assert dialog.locator("img").count() == 0
    page.get_by_label("Wrap Lines", exact=True).uncheck()
    expect(dialog.locator("pre")).not_to_have_class("wrap-lines")
    with page.expect_download() as result:
        dialog.get_by_role("link", name="Download Original", exact=True).click()
    download = result.value
    assert download.suggested_filename == "sample.json"
    assert (
        Path(download.path()).read_bytes()
        == b'{"message":"<img src=x onerror=alert(1)>","count":3}'
    )
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    page.get_by_role("button", name="source.html", exact=True).dblclick()
    dialog = preview_dialog(page)
    expect(dialog.locator("pre")).to_contain_text("<script>")
    assert page.evaluate("window.previewExecuted") is None
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    page.get_by_role("button", name="reports.csv", exact=True).dblclick()
    dialog = preview_dialog(page)
    expect(dialog.locator("pre")).to_contain_text("name,value")
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    page.get_by_role("button", name="sample.pdf", exact=True).dblclick()
    dialog = preview_dialog(page)
    scroll = dialog.locator(".pdf-scroll")
    expect(scroll).to_be_visible()
    assert_rendered_pdf_page(page, "sample.pdf", 1)
    assert_pdf_header_layout(page, dialog)
    expect(
        dialog.locator('.pdf-page[data-page-number="1"] .pdf-text-layer')
    ).to_contain_text("TGDrive PDF preview page 1")
    expect(
        dialog.get_by_role("button", name="Previous PDF page", exact=True)
    ).to_have_count(0)
    expect(
        dialog.get_by_role("button", name="Next PDF page", exact=True)
    ).to_have_count(0)
    expect(dialog.get_by_text("Page text", exact=True)).to_have_count(0)
    assert scroll.evaluate("element=>element.scrollHeight>element.clientHeight")
    for number in (2, 3):
        if number == 2:
            scroll.evaluate(
                "element=>element.scrollTop=(element.scrollHeight-element.clientHeight)/2"
            )
        else:
            scroll.evaluate("element=>element.scrollTop=element.scrollHeight")
        assert_rendered_pdf_page(page, "sample.pdf", number)
        expect(
            dialog.locator(f'.pdf-page[data-page-number="{number}"] .pdf-text-layer')
        ).to_contain_text(f"TGDrive PDF preview page {number}")
        assert_pdf_canvas_budget(dialog)
    zoom = dialog.get_by_label("PDF zoom", exact=True)
    initial_zoom = zoom.inner_text()
    dialog.get_by_role("button", name="Zoom in", exact=True).click()
    expect(zoom).not_to_have_text(initial_zoom)
    assert_pdf_canvas_budget(dialog)
    dialog.get_by_role("button", name="Zoom Out", exact=True).click()
    expect(zoom).to_have_text(initial_zoom)
    dialog.get_by_role("button", name="Fit Width", exact=True).click()
    expect(
        dialog.get_by_role("button", name="Fit Width", exact=True)
    ).to_have_attribute("aria-pressed", "true")
    assert_pdf_canvas_budget(dialog)
    page.screenshot(path=ARTIFACTS / "preview-desktop.png", full_page=False)
    page.set_viewport_size({"width": 390, "height": 844})
    preview_dialog(page)
    assert_pdf_header_layout(page, dialog)
    options = dialog.get_by_role("button", name="PDF View Options", exact=True)
    expect(options).to_be_visible()
    expect(dialog.get_by_role("button", name="Zoom in", exact=True)).not_to_be_visible()
    options.click()
    mobile_zoom = dialog.get_by_label("PDF zoom", exact=True)
    previous_zoom = mobile_zoom.inner_text()
    dialog.get_by_role("button", name="Zoom in", exact=True).click()
    expect(mobile_zoom).not_to_have_text(previous_zoom)
    dialog.get_by_role("button", name="Zoom Out", exact=True).click()
    expect(mobile_zoom).to_have_text(previous_zoom)
    dialog.get_by_role("button", name="Fit Width", exact=True).click()
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()
    expect(options).to_be_focused()
    expect(dialog.get_by_role("button", name="Zoom in", exact=True)).not_to_be_visible()
    page.screenshot(path=ARTIFACTS / "preview-mobile.png", full_page=False)
    page.set_viewport_size({"width": 320, "height": 740})
    preview_dialog(page)
    options.click()
    toolbar = dialog.locator(".pdf-toolbar")
    expect(toolbar).to_be_visible()
    bounds = toolbar.bounding_box()
    assert bounds is not None
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 320
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 740
    dialog.get_by_role("button", name="Fit Width", exact=True).click()
    expect(
        dialog.get_by_role("button", name="Fit Width", exact=True)
    ).to_have_attribute("aria-pressed", "true")
    page.screenshot(path=ARTIFACTS / "preview-mobile-320.png", full_page=False)
    page.keyboard.press("Escape")
    expect(options).to_be_focused()
    expect(toolbar).not_to_be_visible()
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    page.get_by_role("button", name="archive.bin", exact=True).dblclick()
    dialog, metadata = file_details_dialog(page, "archive.bin")
    assert metadata["Size"] == "256 B"


def test_preview_error_has_retry_and_keeps_download(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.route(
        "**/api/v1/preview?*",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"Preview temporarily unavailable. Try Again."}',
        ),
    )
    page.get_by_role("button", name="reports.csv", exact=True).dblclick()
    expect(page.get_by_role("alert")).to_contain_text("temporarily unavailable")
    expect(page.locator("dialog.file-preview.file-details")).to_have_count(0)
    expect(page.locator("#file-details-reason")).to_have_count(0)
    expect(
        page.get_by_role("link", name="Download Original", exact=True)
    ).to_be_visible()
    page.unroute("**/api/v1/preview?*")
    page.get_by_role("button", name="Try Preview Again", exact=True).click()
    expect(page.get_by_role("dialog").locator("pre")).to_contain_text("name,value")
    page.get_by_role("button", name="Close Preview", exact=True).click()
    page.route(
        "**/api/v1/preview?*",
        lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body='{"detail":"Authentication required."}',
        ),
    )
    page.get_by_role("button", name="reports.csv", exact=True).dblclick()
    expect(page.locator("#login-dialog")).to_be_visible()
    expect(page.locator("dialog.file-preview.file-details")).to_have_count(0)
    expect(page.locator("#file-details-reason")).to_have_count(0)


def test_enhanced_mkv_real_audio_subtitles_and_local_assets(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.add_init_script("""window.cspViolations=[];
      document.addEventListener('securitypolicyviolation',event=>{
        window.cspViolations.push(event.violatedDirective+':'+event.blockedURI+':'+event.sourceFile+':'+event.lineNumber);
      });""")
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="sample.json", exact=True).dblclick()
    expect(page.get_by_role("dialog").locator("pre")).to_be_visible()
    assert not any("element.slim-" in url or ".wasm" in url for url in requests)
    page.get_by_role("button", name="Close Preview", exact=True).click()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    preview_dialog(page)
    player = page.locator("movi-player")
    expect(player).to_be_visible(timeout=30000)
    expect(page.locator(".advanced-tracks")).to_have_count(0)
    player.hover()
    audio_button = player.get_by_role("button", name="Audio Track", exact=True)
    expect(audio_button).to_be_visible(timeout=30000)
    audio_button.click()
    audio_items = player.locator(".movi-audio-track-item[data-track-id]")
    expect(audio_items).to_have_count(2)
    tracks = player.evaluate(
        "element=>element.player.getAudioTracks().map(t=>({id:t.id,codec:t.codec}))"
    )
    assert all(track["codec"] == "eac3" for track in tracks)
    second_audio = player.locator(
        f'.movi-audio-track-item[data-track-id="{tracks[1]["id"]}"]'
    )
    second_audio.click()
    expect(second_audio).to_have_class("movi-audio-track-item movi-audio-track-active")
    assert (
        player.evaluate("element=>element.player.trackManager.getActiveAudioTrack().id")
        == tracks[1]["id"]
    )
    player.get_by_role("button", name="Subtitles/Captions", exact=True).click()
    subtitle_items = player.locator(".movi-subtitle-track-item[data-track-id]")
    expect(subtitle_items).to_have_count(2)
    subtitle = player.evaluate("element=>element.player.getSubtitleTracks()[0].id")
    subtitle_item = player.locator(
        f'.movi-subtitle-track-item[data-track-id="{subtitle}"]'
    )
    subtitle_item.click()
    expect(subtitle_item).to_have_class(
        "movi-subtitle-track-item movi-subtitle-track-active"
    )
    player.locator("button.movi-play-pause").click()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if player.evaluate("element=>element.currentTime") > 0.25:
            break
        page.wait_for_timeout(200)
    assert player.evaluate("element=>element.currentTime") > 0.25
    assert (
        player.evaluate(
            "element=>element.player.trackManager.getActiveSubtitleTrack().id"
        )
        == subtitle
    )
    expect(player.locator(".movi-subtitle-line").first).to_contain_text(
        "TGDrive subtitle", timeout=15000
    )
    audio_deadline = time.monotonic() + 2
    audio_active = False
    while time.monotonic() < audio_deadline:
        audio_active = player.evaluate(
            "element=>element.player.getAudioLevels(8)?.some(value=>value>0)"
        )
        if audio_active:
            break
        page.wait_for_timeout(100)
    assert audio_active is True
    with page.expect_download() as result:
        page.get_by_role("link", name="Download Original", exact=True).click()
    assert (
        Path(result.value.path()).read_bytes()
        == (ROOT / "tests/fixtures/multitrack.mkv").read_bytes()
    )
    page.screenshot(path=ARTIFACTS / "enhanced-desktop.png", full_page=False)
    page.set_viewport_size({"width": 390, "height": 844})
    player.hover()
    settings_button = player.get_by_role("button", name="Settings", exact=True)
    expect(settings_button).to_be_visible()
    expect(player.locator("button.movi-more-btn")).not_to_be_visible()
    settings_button.click()
    settings_menu = player.locator(".movi-settings-menu")
    expect(settings_menu).to_be_visible()
    settings_menu.locator('.movi-settings-row[data-page="audio"]').click()
    first_audio = settings_menu.locator(
        f'.movi-audio-track-item[data-track-id="{tracks[0]["id"]}"]'
    )
    first_audio.click()
    page.wait_for_function(
        "id=>document.querySelector('movi-player').player.trackManager"
        ".getActiveAudioTrack()?.id===id",
        arg=tracks[0]["id"],
    )
    expect(settings_menu).not_to_be_visible()
    settings_button.click()
    settings_menu.locator('.movi-settings-row[data-page="subtitles"]').click()
    subtitles_off = settings_menu.locator(
        '.movi-subtitle-track-item[data-track-id="null"]'
    )
    subtitles_off.click()
    page.wait_for_function(
        "()=>!document.querySelector('movi-player').player.trackManager"
        ".getActiveSubtitleTrack()"
    )
    expect(page.locator(".advanced-tracks")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    assert not page.evaluate("window.cspViolations"), page.evaluate(
        "({violations:window.cspViolations,styles:[...document.querySelectorAll('style')].map(s=>({id:s.id,text:s.textContent.slice(0,100)}))})"
    )
    assert not [
        url
        for url in requests
        if url.startswith(("http://", "https://")) and not url.startswith(server.url)
    ]
    page.screenshot(path=ARTIFACTS / "enhanced-mobile.png", full_page=False)
    page.get_by_role("button", name="Close Preview", exact=True).click()
    expect(player).to_have_count(0)


def test_mp4_movtext_real_cues_seek_track_switch_off_and_original(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    requests: list[str] = []
    csp: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.on(
        "console",
        lambda message: (
            csp.append(message.text)
            if "content security policy" in message.text.lower()
            else None
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="movtext-subtitles.mp4", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(player).to_be_visible(timeout=30000)
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    tracks = player.evaluate(
        "element=>element.player.getSubtitleTracks().map(track=>({id:track.id,codec:track.codec}))"
    )
    assert len(tracks) == 2 and all(track["codec"] == "mov_text" for track in tracks)

    def select_subtitle(identity: int | None) -> None:
        player.hover()
        value = "null" if identity is None else str(identity)
        option = player.locator(f'.movi-subtitle-track-item[data-track-id="{value}"]')
        if not option.is_visible():
            player.get_by_role("button", name="Subtitles/Captions", exact=True).click()
        expect(option).to_be_visible()
        option.click()

    select_subtitle(tracks[0]["id"])
    player.locator("button.movi-play-pause").click()
    cue = player.locator(".tgdrive-movtext-cue")
    expect(cue).to_have_text("TGDrive MP4 first cue", timeout=15000)
    expect(cue).to_be_visible()
    page.wait_for_function(
        "()=>document.querySelector('movi-player').player.getRenderHealth().framesPresented>=3",
        timeout=15000,
    )
    player.evaluate("element=>element.pause()")
    player.evaluate("async element=>{await element.player.seek(4.5);}")
    expect(cue).to_have_text("TGDrive MP4 second cue", timeout=15000)
    assert cue.is_visible()
    select_subtitle(None)
    expect(player.locator(".tgdrive-movtext-renderer")).to_have_count(0)

    select_subtitle(tracks[1]["id"])
    player.evaluate("async element=>{await element.player.seek(0.5);}")
    cue = player.locator(".tgdrive-movtext-cue")
    expect(cue).to_have_text("हिंदी <img src=x onerror=alert(1)>", timeout=15000)
    expect(cue.locator("img,script,a")).to_have_count(0)
    player.evaluate("async element=>{await element.player.seek(4.5);}")
    expect(cue).to_have_text("TGDrive दूसरी पंक्ति", timeout=15000)
    with page.expect_download() as original:
        dialog.get_by_role("link", name="Download Original", exact=True).click()
    assert (
        hashlib.sha256(Path(original.value.path()).read_bytes()).digest()
        == hashlib.sha256(
            (ROOT / "tests/fixtures/movtext-subtitles.mp4").read_bytes()
        ).digest()
    )
    assert csp == []
    assert all(
        request.startswith(server.url + "/") or request.startswith("blob:")
        for request in requests
    )
    page.keyboard.press("Escape")
    expect(page.locator("dialog.file-preview")).to_have_count(0)


def test_mouse_double_click_and_mobile_tap_preserve_focus(
    server: Server, desktop: tuple[Any, Any], browser: Any
) -> None:
    _, page = desktop
    login(page, server.url)
    directory_requests = []
    page.on(
        "request",
        lambda request: (
            directory_requests.append(request.post_data_json["path"])
            if request.url.endswith("/api/getDirectory")
            else None
        ),
    )
    folder = page.get_by_role("button", name="Preview samples", exact=True)
    folder.click()
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="sample.json", exact=True)).to_have_count(0)
    expect(page.get_by_role("checkbox")).to_have_count(0)
    assert directory_requests == []
    history_length = page.evaluate("history.length")
    folder.dblclick()
    primary = page.get_by_role("button", name="sample.json", exact=True)
    expect(primary).to_be_visible()
    assert len(set(directory_requests)) == 1
    assert page.evaluate("history.length") == history_length + 1
    page.evaluate("document.body.style.minHeight='220vh'")
    prior_overflow = page.evaluate("getComputedStyle(document.body).overflow")
    primary.click()
    expect(page.locator("dialog.file-preview")).to_have_count(0)
    expect(page.get_by_role("checkbox")).to_have_count(0)
    file_row = page.locator(".file-row").filter(has=primary)
    file_row.locator(".size-cell").click()
    expect(page.locator("dialog.file-preview")).to_have_count(0)
    expect(page.get_by_role("checkbox")).to_have_count(0)
    history_length = page.evaluate("history.length")
    primary.dblclick()
    dialog = preview_dialog(page)
    assert page.evaluate("history.length") == history_length + 1
    background_y = page.evaluate("scrollY")
    page.mouse.move(20, 20)
    page.mouse.wheel(0, 600)
    page.wait_for_timeout(100)
    assert page.evaluate("scrollY") == background_y, (
        "Preview must lock background scrolling"
    )
    assert page.evaluate("getComputedStyle(document.body).overflow") == "hidden"
    page.keyboard.press("Escape")
    expect(page.locator("dialog.file-preview")).to_have_count(0)
    expect(primary).to_be_focused()
    assert page.evaluate("getComputedStyle(document.body).overflow") == prior_overflow

    primary.press("Enter")
    dialog = preview_dialog(page)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    expect(primary).to_be_focused()
    actions = page.get_by_label("Actions for sample.json", exact=True)
    actions.click()
    page.get_by_role("button", name="Open", exact=True).click()
    dialog = preview_dialog(page)
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    expect(actions).to_be_focused()

    page.get_by_role("button", name="Select", exact=True).click()
    primary.click()
    expect(page.get_by_label("Select sample.json", exact=True)).to_be_checked()
    expect(page.locator("dialog.file-preview")).to_have_count(0)
    primary.click()
    expect(page.get_by_label("Select sample.json", exact=True)).not_to_be_checked()
    file_row.locator(".size-cell").click()
    expect(page.get_by_label("Select sample.json", exact=True)).to_be_checked()
    expect(page.locator("dialog.file-preview")).to_have_count(0)
    page.get_by_role("button", name="Done", exact=True).click()

    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    try:
        mobile = context.new_page()
        login(mobile, server.url)
        folder = mobile.get_by_role("button", name="Preview samples", exact=True)
        folder.tap()
        primary = mobile.get_by_role("button", name="sample.json", exact=True)
        primary.tap()
        mobile_dialog = preview_dialog(mobile)
        expect(mobile_dialog.locator("pre")).to_contain_text('"count": 3')
        mobile_dialog.get_by_role("button", name="Close Preview", exact=True).tap()
        expect(mobile.locator("dialog.file-preview")).to_have_count(0)
    finally:
        context.close()


def test_image_and_audio_previews_fill_window(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    for filename, selector in (
        ("sample.png", "img"),
        ("sample.wav", "audio"),
    ):
        page.get_by_role("button", name=filename, exact=True).dblclick()
        dialog = preview_dialog(page)
        content = dialog.locator(selector)
        expect(content).to_be_visible()
        if filename == "sample.png":
            assert content.evaluate("image=>image.complete && image.naturalWidth===16")
        else:
            expect(content).to_have_attribute("controls", "")
        page.keyboard.press("Escape")
        expect(page.locator("dialog.file-preview")).to_have_count(0)


def test_continuous_pdf_virtualizes_canvases_and_keeps_all_pages_reachable(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="long.pdf", exact=True).dblclick()
    dialog = preview_dialog(page)
    scroll = dialog.locator(".pdf-scroll")
    assert_rendered_pdf_page(page, "long.pdf", 1)
    assert_pdf_canvas_budget(dialog)
    scroll.evaluate(
        "element=>element.scrollTop=(element.scrollHeight-element.clientHeight)/2"
    )
    middle = page.locator(
        '.pdf-page[data-page-number="6"] canvas, .pdf-page[data-page-number="7"] canvas'
    ).first
    expect(middle).to_be_visible()
    expect(middle).to_be_in_viewport()
    assert_pdf_canvas_budget(dialog)
    scroll.evaluate("element=>element.scrollTop=element.scrollHeight")
    assert_rendered_pdf_page(page, "long.pdf", 12)
    expect(
        dialog.locator('.pdf-page[data-page-number="12"] .pdf-text-layer')
    ).to_contain_text("TGDrive PDF preview page 12")
    assert_pdf_canvas_budget(dialog)
    expect(page.get_by_role("img", name="long.pdf, page 1", exact=True)).to_have_count(
        0
    )
    scroll.evaluate("element=>element.scrollTop=0")
    assert_rendered_pdf_page(page, "long.pdf", 1)
    assert_pdf_canvas_budget(dialog)


def test_pdf_chunk_failure_keeps_preview_controls_and_can_reload(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    blocked: list[str] = []
    failures: list[tuple[str, str | None]] = []

    def block_pdf_chunk(route: Any) -> None:
        blocked.append(route.request.url)
        route.abort("failed")

    page.on(
        "requestfailed",
        lambda request: failures.append((request.url, request.failure)),
    )
    pattern = "**/assets/PdfPreview-*.js"
    page.route(pattern, block_pdf_chunk)
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    primary = page.get_by_role("button", name="sample.pdf", exact=True)
    primary.dblclick()
    dialog = preview_dialog(page)
    expect(
        dialog.get_by_role("button", name="Reload Viewer", exact=True)
    ).to_be_visible()
    assert blocked, "The actual lazy PDF JavaScript request must be blocked"
    assert any(url in blocked and failure for url, failure in failures)
    expect(dialog.locator(".pdf-scroll")).to_have_count(0)
    with page.expect_download() as result:
        dialog.get_by_role("link", name="Download Original", exact=True).click()
    assert result.value.suggested_filename == "sample.pdf"
    original = Path(result.value.path()).read_bytes()
    assert original.startswith(b"%PDF-1.4") and b"/Count 3" in original
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    expect(page.locator("dialog.file-preview")).to_have_count(0)
    expect(primary).to_be_focused()
    expect(
        page.get_by_role("heading", name="Preview samples", exact=True)
    ).to_be_visible()

    # React.lazy remembers a failed import. The offered reload must recover it
    # after transport is restored, while retaining the addressed preview.
    primary.dblclick()
    dialog = preview_dialog(page)
    expect(
        dialog.get_by_role("button", name="Reload Viewer", exact=True)
    ).to_be_visible()
    page.unroute(pattern, block_pdf_chunk)
    dialog.get_by_role("button", name="Reload Viewer", exact=True).click()
    preview_dialog(page)
    assert_rendered_pdf_page(page, "sample.pdf", 1)
    page.keyboard.press("Escape")
    expect(page.locator("dialog.file-preview")).to_have_count(0)


def test_low_zoom_tall_pdf_renders_every_intersecting_page(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    page.set_viewport_size({"width": 1440, "height": 2160})
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="long.pdf", exact=True).dblclick()
    dialog = preview_dialog(page)
    assert_rendered_pdf_page(page, "long.pdf", 1)
    zoom = dialog.get_by_label("PDF zoom", exact=True)
    for _ in range(20):
        if zoom.inner_text() == "25%":
            break
        dialog.get_by_role("button", name="Zoom Out", exact=True).click()
    expect(zoom).to_have_text("25%")
    expect(dialog.get_by_role("button", name="Zoom Out", exact=True)).to_be_disabled()
    scroll = dialog.locator(".pdf-scroll")
    scroll.evaluate("element=>element.scrollTop=0")
    layout = scroll.evaluate("""element => {
      const bounds=element.getBoundingClientRect();
      return {top:bounds.top,bottom:bounds.bottom,
        pages:[...element.querySelectorAll('.pdf-page')].map(page=>{
          const rect=page.getBoundingClientRect();
          return {number:Number(page.dataset.pageNumber),
            top:rect.top,height:rect.height};
        }).sort((a,b)=>a.number-b.number)};
    }""")
    frames = layout["pages"]
    assert len(frames) >= 2
    # The fixture's twelve pages have identical portrait dimensions. Deriving
    # pitch from any two mounted cards detects omitted visible pages even if a
    # broken virtualizer only mounts a five-page slice around the viewport center.
    first, second = frames[:2]
    pitch = (second["top"] - first["top"]) / (second["number"] - first["number"])
    top = first["top"] - (first["number"] - 1) * pitch
    visible = [
        number
        for number in range(1, 13)
        if top + (number - 1) * pitch < layout["bottom"]
        and top + (number - 1) * pitch + first["height"] > layout["top"]
    ]
    assert len(visible) > 5, "This viewport must exercise more than five visible pages"
    assert set(visible) <= {frame["number"] for frame in frames}
    for number in visible:
        canvas = page.get_by_role("img", name=f"long.pdf, page {number}", exact=True)
        expect(canvas).to_be_visible(timeout=20000)
        expect(canvas).to_be_in_viewport()
        assert canvas.evaluate("element=>element.width>0 && element.height>0")
    assert_pdf_canvas_budget(dialog, max_canvases=None)
    expect(dialog.locator(".pdf-page")).to_have_count(len(frames))


@pytest.mark.parametrize("play_during_open", [False, True])
def test_enhanced_video_delayed_startup_shows_status_and_recovers(
    server: Server, desktop: tuple[Any, Any], play_during_open: bool
) -> None:
    _, page = desktop
    pending: list[Any] = []
    holding = True

    def hold_media(route: Any) -> None:
        if holding and route.request.method == "GET":
            pending.append(route)
        else:
            route.continue_()

    page.route("**/file?*", hold_media)
    try:
        login(page, server.url)
        page.get_by_role("button", name="Preview samples", exact=True).dblclick()
        page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
        dialog = preview_dialog(page)
        status = dialog.locator(".advanced-state")
        expect(status).to_be_visible(timeout=30000)
        expect(status.locator('[role="status"]')).to_be_visible()
        deadline = time.monotonic() + 20
        while not pending and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert pending, "The test must hold an actual media GET, not simulate readiness"
        player = dialog.locator("movi-player")
        expect(player).to_be_visible(timeout=30000)
        player_host = dialog.locator(".advanced-player-host")
        expect(player_host).not_to_have_attribute("inert", "")
        expect(player_host).not_to_have_attribute("aria-hidden", "true")
        expect(player.locator("button.movi-play-pause")).to_be_visible()
        assert player.evaluate("element=>element.closest('[inert]')===null")
        expect(
            dialog.get_by_role("button", name="Start Playback", exact=True)
        ).to_have_count(0)
        expect(
            dialog.get_by_role("button", name="Use Compatibility Playback", exact=True)
        ).to_have_count(0)
        expect(dialog.locator(".preview-surface > video")).to_have_count(0)
        progress = status.get_by_role("progressbar", name="Loading video")
        expect(progress).to_be_visible()
        assert progress.evaluate("element=>!element.hasAttribute('value')"), (
            "Unknown duration must not manufacture a loading percentage"
        )
        expect(
            status.get_by_role("button", name="Try again", exact=True)
        ).to_be_visible(timeout=12000)
        expect(
            dialog.get_by_role("link", name="Download Original", exact=True)
        ).to_be_visible()
        page.screenshot(path=ARTIFACTS / "video-startup-delayed.png", full_page=False)
        page.set_viewport_size({"width": 390, "height": 844})
        preview_dialog(page)
        expect(status).to_be_visible()
        expect(
            status.get_by_role("button", name="Try again", exact=True)
        ).to_be_visible()
        page.screenshot(
            path=ARTIFACTS / "video-startup-delayed-mobile.png", full_page=False
        )
        if play_during_open:
            player.locator("button.movi-play-pause").click()
            expect(status).to_contain_text("Loading video…")
        holding = False
        for route in pending:
            route.continue_()
        pending.clear()
        expect(status).to_have_count(0, timeout=30000)
        expect(player_host).not_to_have_attribute("inert", "")
        expect(player_host).not_to_have_attribute("aria-hidden", "true")
        assert player.evaluate("element=>element.closest('[inert]')===null")
        if not play_during_open:
            assert player.evaluate("element=>element.paused"), (
                "Readiness must not force autoplay"
            )
            player.locator("button.movi-play-pause").click()
        deadline = time.monotonic() + 15
        while (
            player.evaluate("element=>element.currentTime") <= 0.25
            and time.monotonic() < deadline
        ):
            page.wait_for_timeout(100)
        assert player.evaluate("element=>element.currentTime") > 0.25
        page.screenshot(path=ARTIFACTS / "video-startup-ready.png", full_page=False)
        dialog.get_by_role("button", name="Close Preview", exact=True).click()
        expect(player).to_have_count(0)
    finally:
        holding = False
        for route in pending:
            route.continue_()
        page.unroute("**/file?*", hold_media)


def test_enhanced_video_quality_switch_restores_settled_paused_position(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    media_requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            media_requests.append(request.url)
            if "/file?" in request.url and request.method == "GET"
            else None
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="multitrack.mkv", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(player).to_be_visible(timeout=30000)
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    assert player.evaluate("element=>element.duration") > 4
    assert player.evaluate("element=>element.paused")
    # Seek the real FFmpeg/WebCodecs engine while paused; no state/event stub is
    # used. A later engine startup seek-to-zero used to overwrite this position.
    player.evaluate(
        "async element=>{await element.pause();await element.player.seek(3);}"
    )

    def wait_for_restored_position() -> None:
        deadline = time.monotonic() + 15
        state = None
        while time.monotonic() < deadline:
            state = player.evaluate(
                "element=>({time:element.currentTime,paused:element.paused})"
            )
            if abs(state["time"] - 3) < 0.2:
                return
            page.wait_for_timeout(100)
        pytest.fail(f"Real video engine did not restore the saved 3s position: {state}")

    wait_for_restored_position()
    expect(dialog.locator(".preview-quality")).to_have_count(0)

    def switch_source(value: str) -> None:
        player.hover()
        settings = player.locator("button.movi-settings-btn")
        if not settings.is_visible():
            player.locator("button.movi-more-btn").click()
        settings.click()
        player.locator('.movi-settings-row[data-page="quality"]').click()
        menu = player.locator(".movi-settings-menu")
        expect(menu.locator(".movi-settings-page-title")).to_have_text("Quality")
        labels = {
            "original": "Original",
            "fixture-same-bytes": "Fixture alternate source",
        }
        expect(menu.get_by_role("button", name="Original", exact=True)).to_be_visible()
        player.evaluate("element=>window.previousResumePlayer=element")
        menu.get_by_role("button", name=labels[value], exact=True).click()
        deadline = time.monotonic() + 15
        replaced = False
        while time.monotonic() < deadline:
            replaced = page.evaluate("""() => {
              const element=document.querySelector('movi-player');
              return Boolean(element &&
                element!==window.previousResumePlayer && element.player);
            }""")
            if replaced:
                break
            page.wait_for_timeout(100)
        assert replaced, "Changing quality must initialize a fresh real engine"
        expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)

    for value in ("fixture-same-bytes", "original"):
        switch_source(value)
        wait_for_restored_position()
        assert player.evaluate("element=>element.paused"), (
            "Switching source while paused must not start playback"
        )
        # Observe beyond metadata/ready so an asynchronous initializer seek-to-zero
        # cannot pass this regression by briefly reporting the restored position.
        deadline = time.monotonic() + 2
        samples = []
        while time.monotonic() < deadline:
            samples.append(
                player.evaluate(
                    "element=>({time:element.currentTime,paused:element.paused})"
                )
            )
            page.wait_for_timeout(100)
        assert samples and all(
            abs(sample["time"] - 3) < 0.2 and sample["paused"] for sample in samples
        ), samples
    # A genuine user play action must also survive engine replacement. Keep
    # enough of the five-second fixture remaining to observe resumed progress.
    player.evaluate(
        "async element=>{await element.pause();await element.player.seek(1);}"
    )
    player.locator("button.movi-play-pause").click()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = player.evaluate(
            "element=>({time:element.currentTime,paused:element.paused})"
        )
        if not state["paused"] and state["time"] > 1.05:
            break
        page.wait_for_timeout(50)
    assert not state["paused"] and state["time"] > 1.05
    saved_time = state["time"]
    switch_source("fixture-same-bytes")
    deadline = time.monotonic() + 15
    resumed_time = None
    while time.monotonic() < deadline:
        state = player.evaluate(
            "element=>({time:element.currentTime,paused:element.paused})"
        )
        if not state["paused"] and state["time"] >= saved_time - 0.25:
            resumed_time = state["time"]
            break
        page.wait_for_timeout(50)
    assert resumed_time is not None, state
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = player.evaluate(
            "element=>({time:element.currentTime,paused:element.paused})"
        )
        if not state["paused"] and state["time"] > resumed_time + 0.2:
            break
        page.wait_for_timeout(50)
    assert not state["paused"] and state["time"] > resumed_time + 0.2, state
    player.evaluate("element=>element.pause()")
    assert any("quality=fixture-same-bytes" in url for url in media_requests)
    with page.expect_download() as result:
        dialog.get_by_role("link", name="Download Original", exact=True).click()
    assert (
        Path(result.value.path()).read_bytes()
        == (ROOT / "tests/fixtures/multitrack.mkv").read_bytes()
    )
    page.evaluate("delete window.previousResumePlayer")
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    expect(player).to_have_count(0)


def test_unsupported_details_are_compact_explicit_and_shared_readonly(
    server: Server, desktop: tuple[Any, Any], browser: Any
) -> None:
    _, page = desktop
    downloads: list[str] = []
    page.on("download", lambda download: downloads.append(download.suggested_filename))
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    folder_url = page.url
    filename = "installer-" + "very-long-file-name-" * 5 + "setup.exe"
    primary = page.get_by_role("button", name=filename, exact=True)
    pending: list[Any] = []
    page.route("**/api/v1/preview?*", lambda route: pending.append(route))
    try:
        primary.dblclick()
        checking = page.locator("dialog.file-preview.file-details")
        expect(checking).to_be_visible()
        expect(checking.get_by_role("status")).to_have_text("Checking preview…")
        expect(
            checking.get_by_role("link", name="Download Original", exact=True)
        ).to_be_visible()
        assert downloads == []
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert pending, "The checking shell must be observed before metadata returns"
    finally:
        for route in pending:
            route.continue_()
        page.unroute("**/api/v1/preview?*")
    dialog, metadata = file_details_dialog(page, filename)
    assert "EXE" in metadata["Type"].upper()
    assert metadata["Size"] == "18 B"
    assert downloads == [], (
        "Opening unsupported details must never download automatically"
    )
    page.screenshot(path=ARTIFACTS / "unsupported-desktop.png", full_page=False)
    with page.expect_download() as result:
        dialog.get_by_role("link", name="Download Original", exact=True).click()
    assert result.value.suggested_filename == filename
    assert Path(result.value.path()).read_bytes() == b"Long-name fixture."
    dialog.get_by_role("button", name="Close Preview", exact=True).click()
    expect(page.locator("dialog.file-preview")).to_have_count(0)
    expect(page).to_have_url(folder_url)
    expect(primary).to_be_focused()

    empty = page.get_by_role("button", name="empty.exe", exact=True)
    empty.dblclick()
    _, metadata = file_details_dialog(page, "empty.exe")
    assert metadata["Size"] == "0 B"
    page.keyboard.press("Escape")
    expect(page.locator("dialog.file-preview")).to_have_count(0)
    expect(page).to_have_url(folder_url)
    expect(empty).to_be_focused()
    primary.dblclick()
    file_details_dialog(page, filename)
    page.go_back()
    expect(page.locator("dialog.file-preview")).to_have_count(0)
    expect(page).to_have_url(folder_url)
    expect(primary).to_be_focused()
    assert downloads == [filename]

    preview_path = server.control()["seed"]["preview_folder"]
    response = page.request.post(
        server.url + "/api/v1/shares",
        data={"path": preview_path, "expires_in_days": 7},
    )
    assert response.ok
    share = response.json()
    public_context = browser.new_context(viewport={"width": 1440, "height": 960})
    mobile_context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    extra_errors: list[str] = []
    try:
        public = public_context.new_page()
        public.on("pageerror", lambda error: extra_errors.append(str(error)))
        public_downloads: list[str] = []
        public.on(
            "download",
            lambda download: public_downloads.append(download.suggested_filename),
        )
        public.goto(
            server.url
            + "/?path=/share_"
            + preview_path.lstrip("/")
            + "&auth="
            + share["auth"]
        )
        public_primary = public.get_by_role("button", name="setup.exe", exact=True)
        public_primary.dblclick()
        public_dialog, _ = file_details_dialog(public, "setup.exe")
        assert public_downloads == []
        expect(public.locator(".item-actions")).to_have_count(0)
        expect(
            public_dialog.get_by_role("button", name="Upload Files", exact=True)
        ).to_have_count(0)
        with public.expect_download() as result:
            public_dialog.get_by_role(
                "link", name="Download Original", exact=True
            ).click()
        assert Path(result.value.path()).read_bytes() == (
            b"Synthetic fixture bytes; never an executable."
        )
        public_dialog.get_by_role("button", name="Close Preview", exact=True).click()
        expect(public_primary).to_be_focused()

        mobile = mobile_context.new_page()
        mobile.on("pageerror", lambda error: extra_errors.append(str(error)))
        mobile_downloads: list[str] = []
        mobile.on(
            "download",
            lambda download: mobile_downloads.append(download.suggested_filename),
        )
        login(mobile, server.url)
        folder = mobile.get_by_role("button", name="Preview samples", exact=True)
        folder.tap()
        mobile_primary = mobile.get_by_role("button", name=filename, exact=True)
        mobile_primary.tap()
        mobile_dialog, _ = file_details_dialog(mobile, filename)
        assert mobile_downloads == []
        mobile.screenshot(path=ARTIFACTS / "unsupported-mobile.png", full_page=False)
        mobile.set_viewport_size({"width": 320, "height": 740})
        file_details_dialog(mobile, filename)
        mobile_dialog.get_by_role("button", name="Close Preview", exact=True).tap()
        expect(mobile.locator("dialog.file-preview")).to_have_count(0)
        expect(mobile_primary).to_be_focused()
    finally:
        public_context.close()
        mobile_context.close()
    assert extra_errors == []


@pytest.mark.parametrize("width", [1440, 390])
def test_folder_upload_refresh_keeps_table_selection_page_and_scroll(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path, width: int
) -> None:
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 960})
    held: list[tuple[Any, Any, dict[str, Any]]] = []
    hold = False

    def listing(route: Any) -> None:
        response = route.fetch()
        payload = response.json()
        if route.request.post_data_json["path"] == "/":
            payload["data"]["contents"].update(
                {
                    f"stable-{i}": {
                        "id": f"stable-{i}",
                        "name": f"stable-{i:03}.txt",
                        "type": "file",
                        "path": "/",
                        "size": 10,
                    }
                    for i in range(130)
                }
            )
        if hold:
            held.append((route, response, payload))
        else:
            route.fulfill(response=response, json=payload)

    page.route("**/api/getDirectory", listing)
    login(page, server.url)
    directory = tmp_path / "Stable upload"
    directory.mkdir()
    for i in range(3):
        (directory / f"upload-{i}.txt").write_bytes(b"folder upload contents")
    dialog = select_folder(page, directory)
    dialog.get_by_role("button", name="Upload", exact=True).click()
    expect(dialog).not_to_be_visible()
    started = server.wait_started(3)
    expect(page.get_by_role("button", name="Stable upload", exact=True)).to_be_visible()
    hide = page.get_by_role("button", name="Hide Transfer Details", exact=True)
    if hide.is_visible():
        hide.click()
    page.get_by_role("button", name="Next Page", exact=True).click()
    select_item(page, "stable-110.txt")
    page.get_by_label("Select stable-110.txt", exact=True).focus()
    page.evaluate("""() => {
      window.stableTable = document.querySelector('.file-table');
      window.stableRow = document.querySelector('[data-item-id="stable-110"]');
      window.tableRemovals = 0;
      new MutationObserver(records => {
        for (const record of records) for (const removed of record.removedNodes)
          if (removed === window.stableTable || removed.contains?.(window.stableTable))
            window.tableRemovals++;
      }).observe(document.querySelector('.workspace'), {childList:true, subtree:true});
      window.stableScroll = document.querySelector(
        innerWidth < 721 ? '.workspace' : '.file-list-scroll');
      window.stableScroll.scrollTop = 250;
    }""")
    scroll = page.evaluate("window.stableScroll.scrollTop")
    assert scroll > 0
    hold = True
    try:
        for item in started:
            server.finish(item["id"])
            deadline = time.monotonic() + 10
            while not held and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert len(held) == 1
            expect(page.locator(".loading-state")).to_have_count(0)
            expect(
                page.get_by_label("Select stable-110.txt", exact=True)
            ).to_be_checked()
            route, response, payload = held.pop()
            route.fulfill(response=response, json=payload)
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame("
                "() => requestAnimationFrame(resolve)))"
            )
            expect(
                page.get_by_label("Select stable-110.txt", exact=True)
            ).to_be_checked()
            expect(
                page.get_by_label("Select stable-110.txt", exact=True)
            ).to_be_focused()
            expect(page.locator(".pagination")).to_contain_text("Showing 101\u2013")
            assert page.evaluate("window.stableScroll.scrollTop") == scroll
            assert page.evaluate(
                "window.stableTable === document.querySelector('.file-table')"
            )
            assert page.evaluate(
                "window.stableRow === "
                "document.querySelector('[data-item-id=stable-110]')"
            )
            assert page.evaluate("window.tableRemovals") == 0
        page.screenshot(path=ARTIFACTS / f"stable-folder-upload-{width}.png")
    finally:
        hold = False
        for route, response, payload in held:
            route.fulfill(response=response, json=payload)


def test_background_listing_failure_preserves_files_and_navigation_loads_normally(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    primary = page.get_by_role("button", name="資料", exact=True)
    expect(primary).to_be_visible()
    select_item(page, "資料")
    page.route(
        "**/api/getDirectory",
        lambda route: route.fulfill(
            status=503, json={"detail": "Temporary test failure"}
        ),
    )
    page.evaluate("window.dispatchEvent(new Event('tgdrive:files-changed'))")
    expect(page.get_by_role("alert")).to_contain_text("Could not update your files")
    expect(primary).to_be_visible()
    expect(page.get_by_label("Select 資料", exact=True)).to_be_checked()
    expect(page.get_by_role("heading", name="Could Not Load Your Files")).to_have_count(
        0
    )
    page.unroute("**/api/getDirectory")
    held: list[Any] = []
    page.route("**/api/getDirectory", lambda route: held.append(route))
    try:
        page.get_by_role("button", name="Done", exact=True).click()
        primary.press("Enter")
        expect(page.locator(".loading-state")).to_be_visible()
        expect(page.locator(".file-table")).to_have_count(0)
    finally:
        for route in held:
            route.continue_()
        page.unroute("**/api/getDirectory")
    expect(page.get_by_role("button", name="résumé #1.txt", exact=True)).to_be_visible()


def test_directory_picker_preserves_nested_paths_and_waits_for_atomic_tree(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path
) -> None:
    _, page = desktop
    directory = tmp_path / "Project release"
    payloads = {
        "readme.txt": b"Project root readme.",
        "alpha/notes.txt": b"Alpha notes; distinct bytes.",
        "beta/deep/notes.txt": b"Nested beta notes; same basename.",
    }
    for relative, content in payloads.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (directory / "empty.txt").write_bytes(b"")
    (directory / "empty-directory").mkdir()
    login(page, server.url)
    response = page.request.post(
        server.url + "/api/createNewFolder",
        data={"path": "/", "name": directory.name},
    )
    assert response.ok
    original = next(
        item
        for item in fixture_nodes(page, server, "/")
        if item["name"] == directory.name
    )
    held: list[tuple[Any, Any, dict[str, Any]]] = []
    upload_requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            upload_requests.append(request.url)
            if request.method == "POST" and request.url.endswith("/api/v1/uploads")
            else None
        ),
    )

    def hold_tree_reply(route: Any) -> None:
        response = route.fetch()
        assert response.ok, response.text()
        held.append((route, response, response.json()))

    page.route("**/api/v1/folders/tree", hold_tree_reply)
    dialog = select_folder(page, directory)
    expect(dialog.get_by_role("button", name="Upload", exact=True)).to_be_visible()
    assert server.control()["started"] == []
    expect(dialog.locator(".folder-upload-summary")).to_contain_text("3 files")
    expect(dialog.locator(".folder-upload-destination")).to_have_text("To My Files")
    expect(dialog.locator(".folder-upload-help")).to_have_count(0)
    assert "resumable upload queue" not in dialog.inner_text()
    assert "Keep this tab open" not in dialog.inner_text()
    page.screenshot(path=ARTIFACTS / "folder-upload-review.png", full_page=False)
    dialog.get_by_role("button", name="Upload", exact=True).click()
    try:
        expect(
            dialog.get_by_role("button", name="Cancel Preparation", exact=True)
        ).to_be_visible()
        deadline = time.monotonic() + 10
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert len(held) == 1
        assert upload_requests == [], (
            "No file receipt may precede folder acknowledgement"
        )
        assert server.control()["started"] == []
        request_body = held[0][0].request.post_data_json
        tree = held[0][2]
        assert request_body["root_name"] == directory.name
        assert request_body["destination"] == "/"
        assert request_body["client_request_id"]
        assert {tuple(parts) for parts in request_body["folders"]} == {
            ("alpha",),
            ("beta",),
            ("beta", "deep"),
        }
        assert tree["root"]["id"] != original["id"]
        assert tree["root"]["name"] != directory.name
        assert set(tree["folders"]) == {"", "alpha", "beta", "beta/deep"}
        held[0][0].fulfill(response=held[0][1])
        held.clear()
    finally:
        page.unroute("**/api/v1/folders/tree", hold_tree_reply)
        for route, response, _ in held:
            route.fulfill(response=response)
    expect(dialog).not_to_be_visible()
    started = finish_fixture_uploads(page, server, len(payloads))
    expected_destinations = {
        ("readme.txt", tree["folders"][""]),
        ("notes.txt", tree["folders"]["alpha"]),
        ("notes.txt", tree["folders"]["beta/deep"]),
    }
    assert {(item["name"], item["destination"]) for item in started} == (
        expected_destinations
    )
    assert len(upload_requests) == len(payloads)
    for relative, content in payloads.items():
        parts = relative.split("/")
        parent = tree["folders"]["/".join(parts[:-1])]
        items = fixture_nodes(page, server, parent)
        item = next(item for item in items if item["name"] == parts[-1])
        assert item["type"] == "file"
        response = page.request.get(
            server.url + "/file",
            params={"path": parent + "/" + item["id"], "download": "1"},
        )
        assert response.ok and response.body() == content
    root_items = fixture_nodes(page, server, tree["folders"][""])
    assert not any(
        item["name"] in {"empty.txt", "empty-directory"} for item in root_items
    )
    assert fixture_nodes(page, server, "/" + original["id"]) == []
    saved = page.evaluate(UPLOAD_RECOVERY_RECORDS)
    assert len(saved) == len(payloads)
    assert {item.get("relativePath") for item in saved} == {
        directory.name + "/" + relative for relative in payloads
    }
    page.screenshot(path=ARTIFACTS / "folder-upload-complete.png", full_page=False)


def test_folder_preparation_retries_response_loss_and_cancels_without_files(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path
) -> None:
    _, page = desktop
    directory = tmp_path / "Retry project"
    directory.mkdir()
    (directory / "retry.txt").write_bytes(b"Retry-safe directory upload.")
    login(page, server.url)
    attempts: list[dict[str, Any]] = []
    prepared: list[dict[str, Any]] = []

    def lose_first_reply(route: Any) -> None:
        attempts.append(route.request.post_data_json)
        response = route.fetch()
        assert response.ok, response.text()
        prepared.append(response.json())
        if len(attempts) == 1:
            route.abort("failed")
        else:
            route.fulfill(response=response)

    page.route("**/api/v1/folders/tree", lose_first_reply)
    dialog = select_folder(page, directory)
    dialog.get_by_role("button", name="Upload", exact=True).click()
    expect(
        dialog.get_by_role("button", name="Retry Preparation", exact=True)
    ).to_be_visible()
    assert len(prepared) == 1
    assert server.control()["started"] == []
    dialog.get_by_role("button", name="Retry Preparation", exact=True).click()
    expect(dialog).not_to_be_visible()
    page.unroute("**/api/v1/folders/tree", lose_first_reply)
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert prepared[0]["folders"] == prepared[1]["folders"]
    assert prepared[0]["root"]["id"] == prepared[1]["root"]["id"]
    finish_fixture_uploads(page, server, 1)
    matching = [
        item
        for item in fixture_nodes(page, server, "/")
        if item["name"] == directory.name
    ]
    assert len(matching) == 1

    cancelled_directory = tmp_path / "Cancelled project"
    cancelled_directory.mkdir()
    (cancelled_directory / "never-queued.txt").write_bytes(b"Do not enqueue.")
    held: list[tuple[Any, dict[str, Any]]] = []

    def hold_cancelled_reply(route: Any) -> None:
        response = route.fetch()
        assert response.ok, response.text()
        held.append((route, response.json()))

    page.route("**/api/v1/folders/tree", hold_cancelled_reply)
    dialog = select_folder(page, cancelled_directory)
    dialog.get_by_role("button", name="Upload", exact=True).click()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(held) == 1
    prepared_root = held[0][1]["folders"][""]
    try:
        dialog.get_by_role("button", name="Cancel Preparation", exact=True).click()
        for route, _ in held:
            route.abort("aborted")
        held.clear()
        expect(dialog.get_by_role("status")).to_contain_text(
            "Preparation cancelled. No files were queued."
        )
        expect(
            dialog.get_by_role("button", name="Retry Preparation", exact=True)
        ).to_be_visible()
        assert len(server.control()["started"]) == 1
        assert fixture_nodes(page, server, prepared_root) == []
        dialog.get_by_role("button", name="Close", exact=True).click()
        expect(dialog).not_to_be_visible()
        assert (
            page.locator(".transfer-item-name", has_text="never-queued.txt").count()
            == 0
        )
    finally:
        page.unroute("**/api/v1/folders/tree", hold_cancelled_reply)
        for route, _ in held:
            route.abort("aborted")


def test_large_folder_upload_stays_compact_bounded_and_filterable(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path
) -> None:
    _, page = desktop
    login(page, server.url)
    names = [f"batch-file-{number:03}.txt" for number in range(100)]
    directory = tmp_path / "Batch files"
    directory.mkdir()
    for name in names:
        (directory / name).write_bytes(b"x" * 65536)
    dialog = select_folder(page, directory)
    dialog.get_by_role("button", name="Upload", exact=True).click()
    expect(dialog).not_to_be_visible()
    batch = page.locator(".transfer-batch")
    expect(batch).to_have_count(1)
    expect(batch).to_be_visible()
    toggle = batch.locator("button.transfer-batch-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(batch.locator(".transfer-item")).to_have_count(0)
    batch_name = batch.locator(".transfer-batch-name").inner_text()
    progress = batch.locator(".transfer-batch-summary > progress")
    expect(progress).to_be_visible()
    assert progress.get_attribute("aria-label") in {
        "Device upload progress for " + batch_name,
        "Telegram upload progress for " + batch_name,
    }
    expect(batch.locator(".transfer-batch-status")).to_contain_text("100")
    page.screenshot(path=ARTIFACTS / "upload-batch-collapsed.png", full_page=False)
    server.wait_started(100)
    assert len(server.control()["started"]) == 100, (
        "Device receipts must continue independently of busy Telegram jobs"
    )
    expect(batch).to_have_attribute("data-stage", "telegram")
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(batch.locator(".transfer-item")).to_have_count(20)
    expect(
        batch.get_by_role("group", name="Files shown for " + batch_name, exact=True)
    ).to_have_count(0)

    expect(batch.locator(".transfer-item")).to_have_count(20)
    batch.locator("button.transfer-show-more").click()
    expect(batch.locator(".transfer-item")).to_have_count(40)
    page.screenshot(path=ARTIFACTS / "upload-batch-expanded.png", full_page=False)
    toggle.click()
    expect(batch.locator(".transfer-item")).to_have_count(0)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    heading = page.get_by_role("heading", name="My Files", exact=True).bounding_box()
    assert heading is not None and heading["height"] <= 64, (
        "Mobile toolbar actions must not squeeze the heading into vertical letters"
    )
    expect(batch).to_have_count(0)
    expect(page.locator(".mobile-transfer-status")).to_be_visible()
    page.screenshot(path=ARTIFACTS / "upload-batch-mobile.png", full_page=False)
    page.set_viewport_size({"width": 1440, "height": 960})
    cancel_all(page)
    deadline = time.monotonic() + 15
    saved = []
    while time.monotonic() < deadline:
        saved = page.evaluate(UPLOAD_RECOVERY_RECORDS)
        if len(saved) == 100 and all(item["state"] == "cancelled" for item in saved):
            break
        page.wait_for_timeout(100)
    assert len(saved) == 100 and all(item["state"] == "cancelled" for item in saved)
    assert len({item["batchId"] for item in saved}) == 1
    expect(batch.locator(".transfer-batch-name")).to_have_text(directory.name)
    toggle.click()
    expect(batch.locator(".transfer-files-toolbar")).to_have_count(0)
    expect(batch.locator(".transfer-item")).to_have_count(20)
    assert all(
        item.get_attribute("data-status") == "cancelled"
        for item in batch.locator(".transfer-item").all()
    )
    root = saved[0]["folderUpload"]["root_path"]
    assert not any(item["name"] in names for item in fixture_nodes(page, server, root))


def test_settings_backup_status_never_claims_unconfirmed_protection(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    open_admin(page)
    page.get_by_role("link", name="Storage & Backups", exact=True).click()
    section = page.get_by_role("region", name="Cloud Database", exact=True)
    status = section.get_by_role("status")
    expect(page.locator(".drive-protection")).to_have_count(0)
    expect(status).to_have_text("Saved to MongoDB", timeout=10000)

    def assert_not_protected() -> None:
        expect(status).not_to_have_text("Saved to MongoDB")
        assert "connection-ready" not in (status.get_attribute("class") or "")

    # A confirmed catalog checkpoint cannot protect bytes that are still on
    # the owner's device. Hold an actual receipt PATCH before acknowledgement.
    page.locator(".drive-nav").get_by_role("link", name="My Files", exact=True).click()
    held_receipts: list[Any] = []

    def hold_receipt(route: Any) -> None:
        if route.request.method == "PATCH":
            held_receipts.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/uploads/*", hold_receipt)
    try:
        upload(page, ["unfinished-device.txt"])
        deadline = time.monotonic() + 10
        while not held_receipts and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert len(held_receipts) == 1
        receipt = held_receipts[0].request
        assert receipt.headers["upload-offset"] == "0"
        assert len(receipt.post_data_buffer) == 65536
        offset = page.request.head(receipt.url)
        assert offset.ok and offset.headers["upload-offset"] == "0"
        assert server.control()["started"] == []
        open_admin(page)
        page.get_by_role("link", name="Storage & Backups", exact=True).click()
        expect(status).to_have_text("Uploads Still in Progress", timeout=10000)
        assert_not_protected()
        with page.expect_response("**/api/v1/backup") as catalog_backup:
            section.get_by_role("button", name="Back Up Now", exact=True).click()
        assert catalog_backup.value.status == 200
        protected_catalog = catalog_backup.value.json()["recovery"]
        assert (
            protected_catalog["protected_revision"]
            >= (protected_catalog["metadata_revision"])
        )
        expect(status).to_have_text("Uploads Still in Progress", timeout=8000)
        assert_not_protected()
        assert server.control()["started"] == []
        offset = page.request.head(receipt.url)
        assert offset.ok and offset.headers["upload-offset"] == "0"
    finally:
        for route in held_receipts:
            route.continue_()
        page.unroute("**/api/v1/uploads/*", hold_receipt)
    finish_fixture_uploads(page, server, 1)
    server.control("/backup/protected", method="POST")
    expect(status).to_have_text("Saved to MongoDB", timeout=8000)
    baseline_calls = server.control("/backup")["calls"]
    assert baseline_calls == 1

    server.control("/backup/pending", method="POST")
    expect(status).to_have_text("Backing up your latest changes", timeout=8000)
    assert_not_protected()
    server.control("/backup/error", method="POST")
    expect(status).to_have_text("Database Needs Attention", timeout=8000)
    assert_not_protected()
    expect(section.get_by_role("alert")).to_contain_text("Synthetic Telegram")
    # A response claiming 'protected' with an older checkpoint is inconsistent;
    # the owner must never see green confirmation for the newer metadata.
    stale = server.control("/backup/stale", method="POST")["recovery"]
    assert stale["protected_revision"] < stale["metadata_revision"]
    expect(status).to_have_text("Backup Status Unavailable", timeout=8000)
    assert_not_protected()
    server.control("/backup/protected", method="POST")
    expect(status).to_have_text("Saved to MongoDB", timeout=8000)

    def fail_status(route: Any) -> None:
        route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"Synthetic status service unavailable."}',
        )

    page.route("**/api/v1/status", fail_status)
    expect(status).to_have_text("Backup Status Unavailable", timeout=8000)
    assert_not_protected()
    expect(
        section.get_by_role("button", name="Back Up Now", exact=True)
    ).to_be_disabled()
    page.unroute("**/api/v1/status", fail_status)
    expect(status).to_have_text("Saved to MongoDB", timeout=8000)

    server.control("/backup/fail-next", method="POST")
    with page.expect_response("**/api/v1/backup") as failed_backup:
        section.get_by_role("button", name="Back Up Now", exact=True).click()
    assert failed_backup.value.status == 503
    expect(status).to_have_text("Database Needs Attention", timeout=8000)
    assert_not_protected()
    expect(section.get_by_role("alert")).to_contain_text("Backup is not confirmed")
    assert server.control("/backup")["calls"] == baseline_calls + 1

    with page.expect_response("**/api/v1/backup") as confirmed_backup:
        section.get_by_role("button", name="Back Up Now", exact=True).click()
    assert confirmed_backup.value.status == 200
    recovery = confirmed_backup.value.json()["recovery"]
    assert recovery["protected_revision"] >= recovery["metadata_revision"]
    expect(status).to_have_text("Saved to MongoDB", timeout=8000)
    assert server.control("/backup")["calls"] == baseline_calls + 2
    restore = page.get_by_role("region", name="Backups & recovery", exact=True)
    restore.get_by_text("Restore backup", exact=True).click()
    expect(restore.get_by_label("Recovery file (required)", exact=True)).to_be_visible()
    expect(restore.get_by_label("Recovery key (required)", exact=True)).to_be_visible()
    expect(
        restore.get_by_role("button", name="Inspect backup", exact=True)
    ).to_be_disabled()
    page.get_by_text(
        "Reopen an existing drive or restore an older archive", exact=True
    ).click()
    restore_form = page.locator(".drive-recovery-help .database-restore")
    restore_form.get_by_text(
        "Older backups: restore a database archive", exact=True
    ).click()
    expect(restore_form.get_by_label("Database archive (required)")).to_be_visible()
    expect(restore_form.get_by_label("Archive recovery key (required)")).to_be_visible()
    expect(
        restore_form.get_by_role("button", name="Restore database archive")
    ).to_be_disabled()
    page.screenshot(path=ARTIFACTS / "settings-backup-desktop.png", full_page=False)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(status).to_have_text("Saved to MongoDB")
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=ARTIFACTS / "settings-backup-mobile.png", full_page=False)


def test_decoder_worker_data_frames_do_not_allow_external_connections_or_scripts(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    external_attempts = []
    page.route(
        "https://not-tgdrive.invalid/**",
        lambda route: (external_attempts.append(True), route.abort()),
    )
    login(page, server.url)
    result = page.evaluate("""async () => {
      const canvas=document.createElement('canvas');
      canvas.width=4;canvas.height=3;
      const context=canvas.getContext('2d');
      context.fillStyle='#ef315f';context.fillRect(0,0,4,3);
      const frame=canvas.toDataURL('image/jpeg');
      const code=`self.onmessage=async event=>{
        try{
          const response=await fetch(event.data);
          const image=await createImageBitmap(await response.blob());
          const result={width:image.width,height:image.height};image.close();
          self.postMessage(result);
        }catch(error){self.postMessage({error:String(error)});}
      };`;
      const url=URL.createObjectURL(new Blob([code],{type:'text/javascript'}));
      const worker=new Worker(url);
      try{
        const decoded=await new Promise((resolve,reject)=>{
          worker.onmessage=event=>resolve(event.data);
          worker.onerror=event=>reject(Error(event.message));
          worker.postMessage(frame);
        });
        let externalBlocked=false;
        try{await fetch('https://not-tgdrive.invalid/forbidden');}
        catch{externalBlocked=true;}
        const script=document.createElement('script');
        script.src='data:text/javascript,window.__unexpectedDataScript=true';
        const scriptBlocked=await new Promise(resolve=>{
          script.onerror=()=>resolve(true);script.onload=()=>resolve(false);
          document.head.append(script);
        });
        script.remove();
        return {decoded,externalBlocked,scriptBlocked,
          scriptExecuted:window.__unexpectedDataScript===true};
      }finally{worker.terminate();URL.revokeObjectURL(url);}
    }""")
    assert result["decoded"] == {"width": 4, "height": 3}
    assert result["externalBlocked"] and result["scriptBlocked"]
    assert not result["scriptExecuted"]
    assert external_attempts == []


def test_supported_asf_wmv_software_decode_has_real_frames_pixels_seek_and_original(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    console_csp = []
    page.on(
        "console",
        lambda message: (
            console_csp.append(message.text)
            if "content security policy" in message.text.lower()
            else None
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="software-mpeg4.wmv", exact=True).dblclick()
    dialog = preview_dialog(page)
    player = dialog.locator("movi-player")
    expect(player).to_be_visible(timeout=30000)
    expect(dialog.locator(".advanced-state")).to_have_count(0, timeout=30000)
    player.hover()
    player.locator("button.movi-play-pause").click()
    deadline = time.monotonic() + 15
    state = None
    while time.monotonic() < deadline:
        state = player.evaluate("""element=>({
          time:element.currentTime,paused:element.paused,
          frames:element.player.getRenderHealth().framesPresented,
          codec:element.player.getStats()['Video Codec'],
          decoder:element.player.getStats()['Video Decoder']})""")
        if state["time"] > 0.3 and state["frames"] >= 3 and not state["paused"]:
            break
        page.wait_for_timeout(100)
    assert state and state["frames"] >= 3 and state["time"] > 0.3, state
    assert state["codec"] == "mpeg4" and "Software" in state["decoder"]
    player.evaluate("element=>element.pause()")
    canvases = [
        canvas for canvas in player.locator("canvas").all() if canvas.is_visible()
    ]
    canvas = max(canvases, key=lambda item: item.evaluate("e=>e.width*e.height"))
    pixels = page.evaluate(
        """async values=>{
      const blob=new Blob([new Uint8Array(values)],{type:'image/png'});
      const image=await createImageBitmap(blob);
      const canvas=document.createElement('canvas');canvas.width=64;canvas.height=64;
      const context=canvas.getContext('2d');context.drawImage(image,0,0,64,64);
      image.close();const rgba=context.getImageData(0,0,64,64).data;
      const colors=new Set();let brightest=0;
      for(let i=0;i<rgba.length;i+=4){
        colors.add(rgba[i]+','+rgba[i+1]+','+rgba[i+2]);
        brightest=Math.max(brightest,rgba[i],rgba[i+1],rgba[i+2]);
      }
      return {colors:colors.size,brightest};
    }""",
        list(canvas.screenshot()),
    )
    assert pixels["colors"] > 64 and pixels["brightest"] > 150, pixels
    player.evaluate(
        "async element=>{await element.pause();await element.player.seek(0.7);}"
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        position = player.evaluate(
            "element=>({time:element.currentTime,seeking:element.seeking})"
        )
        if abs(position["time"] - 0.7) < 0.25 and not position["seeking"]:
            break
        page.wait_for_timeout(100)
    assert abs(position["time"] - 0.7) < 0.25 and not position["seeking"]
    with page.expect_download() as result:
        dialog.get_by_role("link", name="Download Original", exact=True).click()
    assert (
        hashlib.sha256(Path(result.value.path()).read_bytes()).digest()
        == hashlib.sha256(
            (ROOT / "tests/fixtures/software-mpeg4.wmv").read_bytes()
        ).digest()
    )
    assert console_csp == []


def test_actual_unavailable_wmv_codec_stays_in_custom_player_with_original_download(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="unsupported-msmpeg4.wmv", exact=True).dblclick()
    dialog = preview_dialog(page)
    expect(dialog.locator("movi-player")).to_be_visible(timeout=30000)
    expect(dialog.get_by_role("alert")).to_contain_text("msmpeg4v3", timeout=30000)
    expect(dialog.get_by_role("alert")).to_contain_text(
        "is not supported by the player on this device"
    )
    expect(dialog.get_by_role("button", name="Try again", exact=True)).to_be_visible()
    assert dialog.evaluate("element => element.matches(':modal')")
    expect(dialog.locator(".preview-surface > video")).to_have_count(0)
    with page.expect_download() as result:
        dialog.get_by_role("link", name="Download Original", exact=True).click()
    assert (
        hashlib.sha256(Path(result.value.path()).read_bytes()).digest()
        == hashlib.sha256(
            (ROOT / "tests/fixtures/unsupported-msmpeg4.wmv").read_bytes()
        ).digest()
    )


def test_revoked_account_warning_links_to_connections_and_clears(server, desktop):
    _, page = desktop
    detail = (
        "This storage bot can no longer sign in. Open Connections and replace "
        "its revoked token, then restart TGDrive and retry."
    )
    issue = {"code": "TELEGRAM_CREDENTIALS_REVOKED", "detail": detail}
    state = {"telegram_ready": False, "telegram_issues": [issue]}
    page.route("**/api/v1/status", lambda route: route.fulfill(json=state))
    login(page, server.url)
    warning = page.get_by_role("status").filter(has_text=detail)
    expect(warning).to_be_visible()
    warning.get_by_role("button", name="Open Connections", exact=True).click()
    expect(
        page.get_by_role("heading", name="Storage Clients", exact=True)
    ).to_be_visible()
    state.update(telegram_ready=True, telegram_issues=[])
    expect(page.get_by_text(detail, exact=True)).not_to_be_visible(timeout=10000)


def assert_single_appearance(page: Any) -> None:
    assert page.locator("html").get_attribute("data-theme") in {"light", "dark"}
    if not page.locator('input[type="password"]').is_visible():
        expect(
            page.get_by_role("button", name="Change Appearance", exact=True)
        ).to_be_visible()


def assert_theme_contrast(page: Any) -> None:
    values = page.evaluate("""() => {
      const s=getComputedStyle(document.documentElement);
      return Object.fromEntries(['--surface','--ink','--muted','--action',
        '--on-action','--action-soft','--on-action-soft','--danger','--on-danger']
        .map(k=>[k,s.getPropertyValue(k).trim()]));
    }""")

    def luminance(value: str) -> float:
        value = value.lstrip("#")
        if len(value) == 3:
            value = "".join(channel * 2 for channel in value)
        assert len(value) == 6
        channels = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return sum(
            channel * weight
            for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
        )

    for foreground, background in [
        ("--ink", "--surface"),
        ("--muted", "--surface"),
        ("--on-action", "--action"),
        ("--on-action-soft", "--action-soft"),
        ("--on-danger", "--danger"),
    ]:
        pair = sorted([luminance(values[foreground]), luminance(values[background])])
        assert (pair[1] + 0.05) / (pair[0] + 0.05) >= 4.5, (
            foreground,
            background,
            values,
        )


def test_light_dark_theme_preference_persists_and_preserves_sort_keyboard(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    page.add_init_script(
        "if (!localStorage.getItem('tgdrive.appearance')) "
        "localStorage.setItem('tgdrive.appearance', 'light')"
    )
    page.emulate_media(color_scheme="light")
    page.goto(server.url)
    assert_single_appearance(page)
    assert_theme_contrast(page)
    baseline = page.evaluate("""() => ({
      font: getComputedStyle(document.body).fontFamily,
      background: getComputedStyle(document.body).backgroundColor,
      injected: document.querySelectorAll('style[data-mantine-styles]').length
    })""")
    assert "InterVariable" in baseline["font"]
    assert baseline["background"] == "rgb(245, 247, 249)"
    assert baseline["injected"] == 0
    login(page)
    page.reload()
    assert_single_appearance(page)
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.get_by_role("button", name="Change Appearance", exact=True).click()
    page.get_by_role("menuitemradio", name="Dark", exact=True).click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator("html")).to_have_attribute("data-mantine-color-scheme", "dark")
    assert_theme_contrast(page)
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    sort = page.get_by_role("button", name="Sort Files: Name", exact=True)
    sort.click()
    menu = page.get_by_role("menu", name="Sort Files", exact=True)
    expect(menu.get_by_role("menuitemradio", name="Name", exact=True)).to_be_focused()
    page.keyboard.press("End")
    # Direction choices follow fields; keyboard navigation traverses both groups.
    expect(menu.get_by_role("menuitemradio").last).to_be_focused()
    page.keyboard.press("Home")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    expect(
        menu.get_by_role("menuitemradio", name="File Size", exact=True)
    ).to_be_focused()
    page.keyboard.press("Enter")
    sort = page.get_by_role("button", name="Sort Files: File Size", exact=True)
    expect(sort).to_be_focused()
    sort.click()
    expect(
        menu.get_by_role("menuitemradio", name="File Size", exact=True)
    ).to_have_attribute("aria-checked", "true")
    page.keyboard.press("Escape")
    expect(sort).to_be_focused()
    page.screenshot(path=ARTIFACTS / "single-theme-desktop.png", full_page=False)
    for width in [390, 320]:
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        assert_single_appearance(page)
        assert_theme_contrast(page)
        page.screenshot(path=ARTIFACTS / f"single-theme-{width}.png", full_page=False)


def test_queued_uploads_have_no_elapsed_speed_or_eta(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    page.route(
        "**/api/v1/uploads",
        lambda route: route.fulfill(
            status=507, json={"detail": "Synthetic upload staging full"}
        ),
    )
    login(page, server.url)
    upload(page, ["waiting-one.bin", "waiting-two.bin"])
    panel = page.locator("#transfer-dialog")
    expect(panel.locator(".transfer-batch")).to_have_count(0)
    expect(panel.locator(".transfer-item")).to_have_count(2)
    expect(
        page.get_by_role("heading", name="2 uploads waiting", exact=True)
    ).to_be_visible()
    page.wait_for_timeout(1200)
    assert "Elapsed" not in panel.inner_text()
    assert "remaining" not in panel.inner_text()
    assert "/s" not in panel.inner_text()
    assert server.control()["started"] == []
    saved = page.evaluate(UPLOAD_RECOVERY_RECORDS)
    assert len(saved) == 2
    assert all(
        item["state"] == "queued" and not item.get("startedAt") for item in saved
    )
    expect(panel.get_by_role("button", name="Show Files", exact=True)).to_have_count(0)
    assert "Elapsed" not in panel.inner_text()
    expect(panel.locator("select")).to_have_count(0)
    expect(panel.locator('[data-filter="active"]')).to_have_count(0)
    expect(panel.locator('[data-filter="attention"]')).to_have_count(0)
    page.screenshot(path=ARTIFACTS / "queued-upload-no-elapsed.png", full_page=False)
    cancel_all(page)
    expect(panel.locator(".transfer-item[data-status=cancelled]")).to_have_count(2)


def test_settings_connection_changes_are_write_only_verified_and_restart_scoped(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    open_admin(page)
    page.get_by_role("link", name="Connections", exact=True).click()
    section = page.locator(".connection-management")
    initial = page.request.get(server.url + "/api/v1/settings")
    assert initial.ok and initial.json()["revision"] == 0
    assert "api_hash" not in initial.json()
    assert all("credential" not in client for client in initial.json()["clients"])
    expect(section.get_by_role("heading", name="Storage Clients")).to_be_visible()
    expect(section.locator(".managed-client-info strong").first).to_have_text(
        "TGDrive Test Bot"
    )
    expect(section.locator(".managed-client-info").first).to_contain_text(
        "@tgdrive_fixture_bot"
    )
    section.get_by_role("button", name="Refresh", exact=True).click()
    expect(section.locator(".managed-client-state").first).to_contain_text(
        "Token and channel verified"
    )
    expect(section.locator(".managed-client-info strong").first).to_have_text(
        "TGDrive Test Bot"
    )
    expect(section.get_by_role("heading", name="Telegram API")).to_be_visible()
    expect(section.locator("input")).to_have_count(0)
    expect(section.get_by_role("button", name="Verify and Save")).to_have_count(0)

    def open_form() -> Any:
        section.get_by_role("button", name="Edit API Credentials", exact=True).click()
        form = section.locator("form.connection-editor")
        expect(form.get_by_label("Replace API Hash", exact=True)).to_have_value("")
        return form

    def fill_candidate(form: Any) -> None:
        form.get_by_label("API ID", exact=True).fill("12346")
        form.get_by_label("Replace API Hash", exact=True).fill("b" * 32)

    form = open_form()
    fill_candidate(form)
    held: list[Any] = []
    attempts = 0

    def control_save(route: Any) -> None:
        nonlocal attempts
        if route.request.method != "PATCH":
            route.continue_()
            return
        attempts += 1
        if attempts == 1:
            held.append(route)
        elif attempts == 2:
            route.fulfill(
                status=409,
                json={
                    "detail": "Synthetic concurrent settings change",
                    "code": "SETTINGS_CONFLICT",
                },
            )
        else:
            route.continue_()

    page.route("**/api/v1/settings", control_save)
    form.get_by_role("button", name="Verify and Save", exact=True).click()
    expect(form.get_by_role("status")).to_contain_text(
        "Checking accounts and channel access."
    )
    expect(form.get_by_label("API ID", exact=True)).to_be_disabled()
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    assert len(held) == 1
    held[0].fulfill(
        status=503,
        json={
            "detail": "Synthetic channel access could not be verified",
            "code": "SETTINGS_PREFLIGHT_FAILED",
        },
    )
    expect(form.get_by_role("alert")).to_contain_text("could not be verified")
    expect(form.get_by_label("Replace API Hash", exact=True)).to_have_value("b" * 32)
    assert page.request.get(server.url + "/api/v1/settings").json()["revision"] == 0
    form.get_by_role("button", name="Verify and Save", exact=True).click()
    expect(section.locator("form.connection-editor")).to_have_count(0)
    expect(section.get_by_role("alert")).to_contain_text("changed elsewhere")
    form = open_form()
    fill_candidate(form)
    form.get_by_role("button", name="Verify and Save", exact=True).click()
    expect(section.locator("form.connection-editor")).to_have_count(0)
    expect(
        section.get_by_text("Saved changes need a restart", exact=True)
    ).to_be_visible()
    expect(page.locator(".restart-required-banner")).to_be_visible()

    # Per-client editing remains inline and retains omitted secrets.
    section.get_by_role("button", name="Manage TGDrive Test Bot", exact=True).click()
    form = section.locator("form.connection-editor")
    expect(form.get_by_label("Replace Bot Token", exact=True)).to_have_value("")
    form.get_by_role("button", name="Remove Client", exact=True).click()
    expect(form.locator(".client-removal")).to_be_visible()
    form.get_by_role("button", name="Undo Removal", exact=True).click()
    expect(form.locator(".client-removal")).to_have_count(0)
    form.get_by_label("Replace Bot Token", exact=True).fill("123456:" + "y" * 30)
    form.get_by_role("button", name="Verify and Save", exact=True).click()
    expect(section.locator("form.connection-editor")).to_have_count(0)
    section.get_by_role("button", name="Add Storage Bot", exact=True).click()
    form = section.locator("form.connection-editor")
    form.get_by_label("Local Label", exact=True).fill("Extra storage bot")
    form.get_by_label("Bot Token", exact=True).fill("987654:" + "z" * 30)
    form.get_by_role("button", name="Verify and Save", exact=True).click()
    expect(section.locator("form.connection-editor")).to_have_count(0)
    pending = page.request.get(server.url + "/api/v1/settings")
    assert pending.ok
    body = pending.json()
    assert body["revision"] == 3 and body["applied_revision"] == 0
    assert body["restart_required"] and body["admission_paused"]
    assert body["application_mode"] == "restart"
    assert body["active_clients"][0]["connected"] is True
    assert "api_hash" not in body
    serialized = json.dumps(body)
    for secret in ("b" * 32, "123456:" + "y" * 30, "987654:" + "z" * 30):
        assert secret not in serialized
        assert secret not in section.inner_text()
    extra = section.locator(".managed-client-list > li").filter(
        has_text="Extra storage bot"
    )
    expect(extra).to_contain_text("Not connected")
    expect(extra.locator(".is-connected")).to_have_count(0)
    section.get_by_role("button", name="Manage Extra storage bot", exact=True).click()
    form = section.locator("form.connection-editor")
    expect(form.get_by_label("Replace Bot Token", exact=True)).to_have_value("")
    form.get_by_role("button", name="Done", exact=True).click()
    page.locator(".drive-nav").get_by_role("link", name="My Files", exact=True).click()
    expect(page.locator("#new-button")).to_be_disabled()
    page.reload()
    expect(page.locator(".restart-required-banner")).to_be_visible()
    expect(page.locator("#new-button")).to_be_disabled()
    blocked = page.request.post(
        server.url + "/api/v1/uploads",
        data={"name": "must-not-start.bin", "parent_id": "/", "total_size": 3},
    )
    assert blocked.status == 409
    assert blocked.json()["code"] == "SETTINGS_RESTART_REQUIRED"
    open_admin(page)
    page.get_by_role("link", name="Connections", exact=True).click()
    page.set_viewport_size({"width": 320, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(
        path=ARTIFACTS / "settings-saved-pending-mobile.png", full_page=False
    )


def test_settings_guided_premium_login_cancel_code_and_two_step(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    open_admin(page)
    page.get_by_role("link", name="Connections", exact=True).click()
    section = page.locator(".connection-management")
    section.get_by_role("button", name="Connect Premium Account", exact=True).click()
    premium = section.get_by_role("form", name="Connect a Premium Account", exact=True)
    premium.get_by_label("Phone Number", exact=True).fill("+441234567801")
    premium.get_by_role("button", name="Send Login Code", exact=True).click()
    expect(premium.get_by_label("Telegram Login Code", exact=True)).to_be_focused()
    with page.expect_response(
        lambda response: (
            response.request.method == "DELETE" and "/premium-login/" in response.url
        )
    ) as cancelled:
        premium.get_by_role("button", name="Cancel Connection", exact=True).click()
    assert cancelled.value.status == 200
    expect(section.locator(".premium-connection")).to_have_count(0)
    section.get_by_role("button", name="Connect Premium Account", exact=True).click()
    premium = section.get_by_role("form", name="Connect a Premium Account", exact=True)
    premium.get_by_label("Account Name", exact=True).fill("My verified Premium")
    premium.get_by_label("Phone Number", exact=True).fill("+441234567802")
    premium.get_by_role("button", name="Send Login Code", exact=True).click()
    code = premium.get_by_label("Telegram Login Code", exact=True)
    expect(code).to_be_focused()
    code.fill("00000")
    premium.get_by_role("button", name="Verify Code", exact=True).click()
    expect(premium.get_by_role("alert")).to_have_text(
        "That code is incorrect. Try again."
    )
    expect(code).to_have_value("")
    code.fill("12345")
    premium.get_by_role("button", name="Verify Code", exact=True).click()
    password = premium.get_by_label("Telegram Two-Step Password", exact=True)
    expect(password).to_be_focused()
    expect(password).to_have_attribute("type", "password")
    password.fill("synthetic-two-step-password")
    premium.get_by_role("button", name="Connect Account", exact=True).click()
    expect(section.locator(".premium-connection")).to_have_count(0)
    expect(
        section.get_by_text("Saved changes need a restart", exact=True)
    ).to_be_visible()
    account = section.locator(".managed-client-list > li").filter(
        has_text="My verified Premium"
    )
    expect(account).to_contain_text("Not connected")
    expect(account).not_to_contain_text("Premium is not active")
    assert page.locator("input[type=password]").count() == 0
    snapshot = page.request.get(server.url + "/api/v1/settings").json()
    assert snapshot["restart_required"] is True
    assert "synthetic-premium-session-for-browser-tests" not in json.dumps(snapshot)
    assert not any("credential" in client for client in snapshot["clients"])


def test_bulk_deletion_is_one_notice_with_refresh_failure_and_retry(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    for name in ("Private folder", "資料", "Preview samples"):
        select_item(page, name)
    page.get_by_role("button", name="Trash", exact=True).click()
    page.get_by_role("link", name="Trash", exact=True).click()
    for name in ("Private folder", "資料", "Preview samples"):
        select_item(page, name)
    page.get_by_role("button", name="Delete Permanently", exact=True).click()
    page.get_by_role("dialog").get_by_role(
        "button", name="Delete Permanently", exact=True
    ).click()
    notice = page.get_by_role("region", name="File Deletion", exact=True)
    expect(notice.get_by_role("status")).to_contain_text("Deleting 3 items…")
    expect(notice).to_contain_text("You can close this tab.")
    for name in ("Private folder", "資料", "Preview samples"):
        expect(page.get_by_role("button", name=name, exact=True)).to_have_count(0)
    page.reload()
    expect(notice.get_by_role("status")).to_contain_text("Deleting 3 items…")
    expect(
        page.get_by_role("heading", name="Trash Is Empty", exact=True)
    ).to_be_visible()
    page.screenshot(path=ARTIFACTS / "deletion-pending-desktop.png")
    page.set_viewport_size({"width": 320, "height": 844})
    expect(notice).to_contain_text("You can close this tab.")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / "deletion-pending-mobile.png")
    page.set_viewport_size({"width": 1440, "height": 960})
    expect(page.locator(".transfer-batch")).to_have_count(0)
    records = server.control()["records"]
    ids = [key for key, record in records.items() if record.get("phase") == "deleting"]
    assert len(ids) == 3
    server.finish(ids[0])
    expect(notice.get_by_role("status")).to_contain_text("Deleting 2 items…")
    expect(notice).to_contain_text("1 completed.")
    server.control("/fail-deletion/" + ids[1], method="POST")
    server.finish(ids[2])
    expect(notice.get_by_role("status")).to_have_text("1 item could not be deleted.")
    jobs = page.request.get(server.url + "/api/v1/jobs").json()["jobs"]
    failed_name = next(job["name"] for job in jobs if job["id"] == ids[1])
    expect(page.get_by_role("button", name=failed_name, exact=True)).to_be_visible()
    expect(notice.locator(".deletion-errors")).not_to_have_attribute("open", "")
    expect(page.locator(".transfer-batch")).to_have_count(0)
    page.reload()
    expect(notice.get_by_role("status")).to_have_text("1 item could not be deleted.")
    page.locator(".drive-nav").get_by_role("link", name="My Files", exact=True).click()
    expect(notice).to_have_count(0)
    page.get_by_role("link", name="Trash", exact=True).click()
    expect(notice.get_by_role("status")).to_have_text("1 item could not be deleted.")
    expect(page.locator(".transfer-batch")).to_have_count(0)
    notice.get_by_text("View Failed Items (1)", exact=True).click()
    expect(notice).to_contain_text("Synthetic catalog deletion failed")
    page.set_viewport_size({"width": 320, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=ARTIFACTS / "deletion-failed-mobile.png")
    page.set_viewport_size({"width": 1440, "height": 960})
    notice.get_by_role("button", name="Retry Failed deletions", exact=True).click()
    expect(notice.get_by_role("status")).to_contain_text("Deleting 1 item…")
    expect(page.get_by_role("button", name=failed_name, exact=True)).to_have_count(0)
    server.finish(ids[1])
    expect(page.get_by_text("Permanently deleted 1 item.", exact=True)).to_be_visible()
    expect(notice).to_have_count(0)
    expect(
        page.get_by_role("heading", name="Trash Is Empty", exact=True)
    ).to_be_visible()
    expect(page.locator(".transfer-batch")).to_have_count(0)


def test_debounced_search_restores_origin_and_document_text_stays_selectable(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    page.get_by_role("button", name="資料", exact=True).dblclick()
    origin = page.url
    search = page.get_by_role("searchbox", name="Search Files and Folders", exact=True)
    before = page.evaluate("history.length")
    search.fill("private")
    expect(
        page.get_by_role("heading", name="Search Results", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("button", name="private.txt", exact=True)).to_be_visible()
    expect(
        page.locator(".search-result-location").filter(has_text="Private folder")
    ).to_be_visible()
    after = page.evaluate("history.length")
    assert after == before + 1
    search.fill("résumé")
    expect(page.get_by_role("button", name="résumé #1.txt", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="private.txt", exact=True)).to_have_count(0)
    assert page.evaluate("history.length") == after
    page.get_by_role("button", name="Clear Search", exact=True).click()
    expect(page).to_have_url(origin)
    expect(search).to_have_value("")
    search.fill("private")
    expect(
        page.get_by_role("heading", name="Search Results", exact=True)
    ).to_be_visible()
    search.press("Escape")
    expect(page).to_have_url(origin)
    expect(search).to_have_value("")
    page.get_by_role("heading").first.click()
    page.keyboard.press("Control+k")
    expect(search).to_be_focused()
    search.fill("input selection")
    search.press("Control+a")
    assert search.evaluate(
        "input=>input.selectionStart===0 && input.selectionEnd===input.value.length"
    )
    search.press("Escape")
    heading = page.get_by_role("heading").first
    assert heading.evaluate("node=>getComputedStyle(node).userSelect") == "none"
    page.evaluate("getSelection().removeAllRanges()")
    bounds = heading.bounding_box()
    assert bounds
    page.mouse.move(bounds["x"] + 2, bounds["y"] + bounds["height"] / 2)
    page.mouse.down()
    page.mouse.move(
        bounds["x"] + bounds["width"] - 2, bounds["y"] + bounds["height"] / 2, steps=10
    )
    page.mouse.up()
    assert page.evaluate("getSelection().toString()") == ""
    page.locator(".drive-nav").get_by_role("link", name="My Files", exact=True).click()
    page.get_by_role("button", name="Preview samples", exact=True).dblclick()
    page.get_by_role("button", name="sample.json", exact=True).dblclick()
    dialog = preview_dialog(page)
    pre = dialog.locator("pre")
    expect(pre).to_contain_text('"count": 3')
    assert pre.evaluate("node=>getComputedStyle(node).userSelect") == "text"
    bounds = pre.bounding_box()
    assert bounds
    page.mouse.move(bounds["x"] + 28, bounds["y"] + 32)
    page.mouse.down()
    page.mouse.move(bounds["x"] + 250, bounds["y"] + 77, steps=10)
    page.mouse.up()
    assert page.evaluate("getSelection().toString().length") > 0
    page.keyboard.press("Escape")
    expect(page.locator("dialog.file-preview")).to_have_count(0)


def test_ui_polish_batched_visual_states(
    server: Server, desktop: tuple[Any, Any], tmp_path: Path
) -> None:
    _, page = desktop
    target = ROOT / "cache/format-qa/ui-polish"
    target.mkdir(parents=True, exist_ok=True)
    directory = tmp_path / "Project release"
    (directory / "notes").mkdir(parents=True)
    (directory / "readme.txt").write_text("data")
    (directory / "notes" / "summary.txt").write_text("data")
    login(page, server.url)

    def capture(name: str) -> None:
        expect(page.locator("html")).to_have_attribute(
            "data-theme", name.split("-", 1)[0]
        )
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        page.screenshot(path=target / (name + ".png"), full_page=False)

    for mode in ("Dark",):
        page.set_viewport_size({"width": 1440, "height": 960})
        page.get_by_role("button", name="Change Appearance", exact=True).click()
        page.get_by_role("menuitemradio", name=mode, exact=True).click()
        assert_single_appearance(page)
        assert_theme_contrast(page)
        page.evaluate("document.fonts.ready")
        assert page.evaluate("document.fonts.check('14px InterVariable')")
        search_alignment = page.locator(".search-form").evaluate("""form=>{
          const field=form.querySelector('input').getBoundingClientRect();
          const box=form.getBoundingClientRect();
          return Math.abs((field.top+field.bottom)/2-(box.top+box.bottom)/2);
        }""")
        assert search_alignment <= 2
        capture(mode.lower() + "-files-desktop")
        page.set_viewport_size({"width": 390, "height": 844})
        capture(mode.lower() + "-files-mobile")
        page.set_viewport_size({"width": 1440, "height": 960})
        page.locator("#new-button").click()
        capture(mode.lower() + "-new-menu")
        page.locator("#new-button").click()
        dialog = select_folder(page, directory)
        expect(dialog.locator(".folder-upload-summary")).to_contain_text(
            "Project release"
        )
        expect(dialog.locator(".folder-upload-summary")).to_contain_text(
            "2 files · 8 B"
        )
        expect(dialog.locator(".folder-upload-destination")).to_have_text("To My Files")
        bounds = dialog.bounding_box()
        assert bounds and bounds["height"] < 400
        capture(mode.lower() + "-folder-desktop")
        page.set_viewport_size({"width": 390, "height": 844})
        bounds = dialog.bounding_box()
        assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
        capture(mode.lower() + "-folder-mobile")
        dialog.get_by_role("button", name="Close Dialog", exact=True).click()

    page.set_viewport_size({"width": 1440, "height": 960})
    page.route(
        "**/api/v1/uploads",
        lambda route: route.fulfill(
            status=507, json={"detail": "Synthetic upload staging full"}
        ),
    )
    page.reload()
    upload(page, ["waiting-one.bin", "waiting-two.bin", "waiting-three.bin"])
    panel = page.locator("#transfer-dialog")
    expect(panel.locator(".transfer-batch")).to_have_count(0)
    expect(panel.locator(".transfer-item")).to_have_count(3)
    expect(
        page.get_by_role("heading", name="3 uploads waiting", exact=True)
    ).to_be_visible()
    capture("dark-transfer-queued")
    expect(panel.get_by_role("button", name="Show Files", exact=True)).to_have_count(0)
    capture("dark-transfer-filters")
    page.set_viewport_size({"width": 390, "height": 844})
    capture("dark-transfer-mobile")
    expect(page.locator("#transfer-dialog")).to_have_count(0)
    page.set_viewport_size({"width": 1440, "height": 960})
    cancel_all(page)
    expect(panel.locator(".transfer-item[data-status=cancelled]")).to_have_count(3)
    page.unroute("**/api/v1/uploads")
    page.get_by_role("button", name="Clear Finished", exact=True).click()
    page.set_viewport_size({"width": 1440, "height": 960})
    open_admin(page)
    page.get_by_role("link", name="Connections", exact=True).click()
    section = page.locator(".connection-management")
    premium_button = section.get_by_role(
        "button", name="Connect Premium Account", exact=True
    )
    expect(premium_button).to_be_enabled()
    capture("dark-connections-desktop")
    premium_button.click()
    premium = section.get_by_role("form", name="Connect a Premium Account", exact=True)
    expect(premium.get_by_label("Phone Number", exact=True)).to_have_value("")
    premium.scroll_into_view_if_needed()
    capture("dark-premium-desktop")
    page.set_viewport_size({"width": 390, "height": 844})
    premium.scroll_into_view_if_needed()
    capture("dark-premium-mobile")
    premium.get_by_role("button", name="Cancel Connection", exact=True).click()
    page.locator(".workspace").evaluate("element=>element.scrollTop=0")
    page.get_by_role("button", name="Change Appearance", exact=True).click()
    page.get_by_role("menuitemradio", name="Light", exact=True).click()
    assert_single_appearance(page)
    capture("light-connections-mobile")

    for mode in ("Dark",):
        page.set_viewport_size({"width": 1440, "height": 960})
        page.get_by_role("button", name="Change Appearance", exact=True).click()
        page.get_by_role("menuitemradio", name=mode, exact=True).click()
        assert_single_appearance(page)
        for route, heading in (
            ("connections", "Connections"),
            ("storage", "Storage & Backups"),
        ):
            page.goto(server.url + "/?view=" + route)
            expect(
                page.get_by_role("heading", name=heading, exact=True)
            ).to_be_visible()
            if route == "connections":
                expect(
                    page.get_by_role("heading", name="Storage Clients")
                ).to_be_visible()
                expect(page.locator(".connection-management input")).to_have_count(0)
            elif route == "storage":
                expect(page.locator(".storage-catalog")).to_be_visible()
                expect(
                    page.get_by_role("progressbar", name="Reserved Upload Staging")
                ).to_be_visible()
            capture(mode.lower() + "-" + route + "-desktop")
            page.set_viewport_size({"width": 390, "height": 844})
            if route == "storage":
                backup_title = page.get_by_role(
                    "heading", name="Cloud Database", exact=True
                )
                expect(backup_title).to_be_visible()
                expect(
                    page.get_by_role(
                        "region", name="Cloud Database", exact=True
                    ).get_by_role("button", name="Back Up Now", exact=True)
                ).to_be_enabled()
                assert backup_title.bounding_box()["height"] <= 28
                assert (
                    page.locator(".drive-backup-state").bounding_box()["height"] <= 28
                )
            capture(mode.lower() + "-" + route + "-mobile")
            if route == "connections":
                page.locator(".workspace").evaluate(
                    "element=>element.scrollTop=element.scrollHeight"
                )
                footer = page.locator(".connection-footer-note")
                expect(footer).to_be_in_viewport()
                expect(page.locator("#compact-new-button")).to_have_count(0)
                footer_box = footer.bounding_box()
                assert footer_box and footer_box["y"] + footer_box["height"] <= 844
                storage_link = page.get_by_role(
                    "link", name="Storage & Backups", exact=True
                )
                if page.locator(".sidebar").get_attribute("inert") is not None:
                    page.get_by_role(
                        "button", name="Toggle Navigation", exact=True
                    ).click()
                storage_link.click()
                expect(
                    page.get_by_role("heading", name="Storage & Backups", exact=True)
                ).to_be_in_viewport()
                page.go_back()
                expect(
                    page.get_by_role("heading", name="Connections", exact=True)
                ).to_be_in_viewport()
            page.set_viewport_size({"width": 320, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.set_viewport_size({"width": 1440, "height": 960})


def test_transfer_progress_and_safe_panel_controls(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    page.emulate_media(color_scheme="dark")
    login(page, server.url)
    expect(page.locator(".drive-protection")).to_have_count(0)
    fraction = 0.0

    def measured_progress(route: Any) -> None:
        response = route.fetch()
        payload = response.json()
        for job in payload["jobs"]:
            if job["status"] in {"queued", "running"}:
                job["current"] = int(job["total"] * fraction)
        route.fulfill(response=response, json=payload)

    page.route("**/api/v1/jobs", measured_progress)
    upload(page, ["single-progress.bin"])
    server.wait_started(1)
    single = row(page, "single-progress.bin")
    expect(single.locator(".transfer-phase")).to_have_text("Uploading to Telegram")
    expect(single.get_by_role("progressbar")).to_have_attribute("value", "0")
    expect(single.locator(".transfer-bytes")).to_have_text("0 B / 64 KB")
    expect(page.get_by_role("button", name="Show Files", exact=True)).to_have_count(0)
    expect(
        page.get_by_role("button", name="Add files to upload queue", exact=True)
    ).to_have_count(0)
    expect(page.get_by_role("button", name="Clear Finished", exact=True)).to_have_count(
        0
    )

    fraction = 0.25
    expect(single.get_by_role("progressbar")).to_have_attribute("value", "0.25")
    expect(single.locator(".transfer-bytes")).to_have_text("16 / 64 KB")
    expect(single.locator(".transfer-batch-percent")).to_have_text("25%")
    page.get_by_role("link", name="Transfers", exact=False).click()
    expect(single.get_by_role("progressbar")).to_have_attribute("value", "0.25")
    expect(page.get_by_role("button", name="Show Files", exact=True)).to_have_count(0)
    page.screenshot(
        path=ARTIFACTS / "single-transfer-stage-progress.png", full_page=True
    )

    page.get_by_role("link", name="My Files", exact=True).click()
    page.get_by_role("button", name="Hide Transfer Details", exact=True).click()
    expect(page.locator("#transfer-list")).not_to_be_visible()
    page.get_by_role("button", name="Show Transfer Details", exact=True).click()
    expect(single).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    # The phone layout replaces the expanded desktop panel with its summary.
    # Wait for that React transition before examining controls from the new layout.
    expect(page.locator("#transfer-dialog")).to_have_count(0)
    assert_layout(page)
    page.screenshot(path=ARTIFACTS / "single-transfer-mobile.png", full_page=True)
    page.set_viewport_size({"width": 1440, "height": 960})

    page.get_by_role("button", name="Cancel All", exact=True).click()
    confirmation = page.get_by_role("dialog", name="Cancel All Uploads?", exact=True)
    expect(confirmation).to_be_visible()
    expect(confirmation).to_contain_text("1 unfinished upload")
    expect(
        confirmation.get_by_role("button", name="Keep Uploading", exact=True)
    ).to_be_focused()
    expect(single).to_have_attribute("data-status", "uploading")
    page.keyboard.press("Escape")
    expect(confirmation).to_have_count(0)
    expect(single).to_have_attribute("data-status", "uploading")
    cancel_all(page)
    expect(single).to_have_attribute("data-status", "cancelled")
    page.get_by_role("button", name="Clear Finished", exact=True).click()
    expect(single).to_have_count(0)

    page.set_viewport_size({"width": 1440, "height": 960})
    fraction = 0
    upload(page, ["batch-first.bin", "batch-second.bin"])
    server.wait_started(3)
    expect(page.locator(".transfer-batch")).to_have_count(0)
    for name in ("batch-first.bin", "batch-second.bin"):
        expect(row(page, name).locator(".transfer-bytes")).to_have_text("0 B / 64 KB")
    fraction = 0.25
    for name in ("batch-first.bin", "batch-second.bin"):
        item = row(page, name)
        expect(item.get_by_role("progressbar")).to_have_attribute("value", "0.25")
        expect(item.locator(".transfer-bytes")).to_have_text("16 / 64 KB")
    expect(page.get_by_role("button", name="Show Files", exact=True)).to_have_count(0)
    page.screenshot(
        path=ARTIFACTS / "multi-transfer-telegram-bytes.png", full_page=True
    )
    cancel_all(page)


def test_transfer_history_filters_cleared_records_and_empty_state(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    page.emulate_media(color_scheme="dark")
    login(page, server.url)
    page.get_by_role("link", name="Transfers", exact=False).click()
    history = page.get_by_role("region", name="Transfer History", exact=True)
    filters = history.get_by_role("group", name="Filter Finished Transfers", exact=True)
    expect(
        history.get_by_role("heading", name="No Finished Transfers Yet", exact=True)
    ).to_be_visible()
    expect(
        history.get_by_role("button", name="Upload Files", exact=True)
    ).to_have_count(0)
    for name in ("All", "Successful", "Failed", "Cancelled"):
        expect(filters.get_by_role("button", name=name, exact=True)).to_be_visible()
    # New first opens the destination context instead of a picker on Transfers.
    page.locator("#new-button").click()
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    expect(page.locator("#file-upload-action")).to_be_visible()
    page.keyboard.press("Escape")

    held = []

    def hold_first(route: Any) -> None:
        if route.request.method == "PATCH":
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/uploads/*", hold_first)
    upload(page, ["history-first.bin", "history-second.bin"])
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(30)
    # History behavior is independent of the browser's receive concurrency.
    assert held
    page.get_by_role("link", name="Transfers", exact=False).click()
    filters.get_by_role("button", name="Failed", exact=True).click()
    expect(
        history.get_by_role("heading", name="No Failed Transfers", exact=True)
    ).to_be_visible()
    filters.get_by_role("button", name="All", exact=True).click()
    expect(history.get_by_role("button", name="Show Files", exact=True)).to_have_count(
        0
    )
    expect(history.locator(".transfer-item-name")).to_have_count(2)
    expect(row(page, "history-first.bin")).to_be_visible()
    expect(row(page, "history-second.bin")).to_be_visible()
    for route in held:
        route.continue_()
    page.unroute("**/api/v1/uploads/*", hold_first)
    finish_fixture_uploads(page, server, 2)
    filters.get_by_role("button", name="Successful", exact=True).click()
    expect(history.locator(".transfer-batch")).to_have_count(0)
    expect(history.locator(".transfer-item-name")).to_have_count(2)
    page.get_by_role("link", name="My Files", exact=True).click()
    expect(page.locator("#transfer-dialog")).to_have_count(0, timeout=10000)

    page.get_by_role("link", name="Transfers", exact=False).click()
    filters.get_by_role("button", name="Successful", exact=True).click()
    expect(history.get_by_role("button", name="Show Files", exact=True)).to_have_count(
        0
    )
    expect(history.locator(".transfer-item-name")).to_have_count(2)
    expect(history.locator("time")).to_have_count(2)
    expect(
        history.get_by_role("button", name="Clear Finished", exact=True)
    ).to_have_count(0)
    page.screenshot(path=ARTIFACTS / "transfer-history-dark.png", full_page=True)
    # Prove server history remains after browser tracking is removed/reloaded.
    page.evaluate("localStorage.clear()")
    page.reload()
    filters.get_by_role("button", name="Successful", exact=True).click()
    expect(history.locator(".transfer-item-name")).to_have_count(2)
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme="light")
    assert_layout(page)
    for name in ("All", "Successful", "Failed", "Cancelled"):
        expect(filters.get_by_role("button", name=name, exact=True)).to_be_in_viewport()
    page.screenshot(path=ARTIFACTS / "transfer-history-mobile.png", full_page=True)
    filters.get_by_role("button", name="Cancelled", exact=True).click()
    expect(
        history.get_by_role("heading", name="No Cancelled Transfers", exact=True)
    ).to_be_visible()
    expect(
        history.get_by_role("button", name="Upload Files", exact=True)
    ).to_have_count(0)


def test_transfer_task_numbered_pages_keep_folder_intact(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    now = time.time()
    metadata = {
        "id": "explicit-folder-task-0001",
        "name": "Uploaded folder",
        "root_path": "/uploaded-folder",
        "created_at": now,
    }
    members = [
        {
            "id": "folder-member-" + str(index),
            "name": f"member-{index:02}.txt",
            "kind": "upload",
            "folder_upload": metadata,
            "destination": metadata["root_path"],
            "status": "completed",
            "phase": "completed",
            "current": 100,
            "total": 100,
            "created_at": now,
            "finished_at": now,
        }
        for index in range(72)
    ]
    ordinary = [
        {
            "id": "ordinary-task-" + str(index),
            "name": f"separate-{index:02}.txt",
            "kind": "upload",
            "destination": "/",
            "status": "completed",
            "phase": "completed",
            "current": 100,
            "total": 100,
            "created_at": now - index - 1,
            "finished_at": now,
        }
        for index in range(22)
    ]
    offsets = []

    def history(route: Any) -> None:
        payload = route.request.post_data_json
        offset = payload.get("offset", 0)
        offsets.append(offset)
        assert offset in (0, 20)
        route.fulfill(
            json={
                "jobs": members + ordinary[:19] if offset == 0 else ordinary[19:],
                "page": payload["page"],
                "page_size": 20,
                "total_tasks": 23,
                "total_pages": 2,
                "has_more": offset == 0,
            }
        )

    page.route("**/api/v1/transfers/history/tasks", history)
    login(page, server.url)
    page.get_by_role("link", name="Transfers", exact=False).click()
    center = page.get_by_role("region", name="Transfer History", exact=True)
    finished = center.locator('.transfer-category[data-category="finished"]')
    folder = center.locator('[data-batch-id="explicit-folder-task-0001"]')
    navigation = center.get_by_role("navigation", name="Finished transfer pages")
    first = navigation.get_by_role(
        "button", name="Page 1 of finished transfers", exact=True
    )
    second = navigation.get_by_role(
        "button", name="Page 2 of finished transfers", exact=True
    )
    expect(finished.locator(".transfer-category-title > span")).to_have_text("20")
    expect(folder.locator(".transfer-batch-count")).to_have_text("72 files")
    expect(center.locator(".transfer-item")).to_have_count(19)
    expect(first).to_have_attribute("aria-current", "page")
    second.click()
    expect(finished.locator(".transfer-category-title > span")).to_have_text("3")
    expect(folder).to_have_count(0)
    expect(center.locator(".transfer-item")).to_have_count(3)
    expect(second).to_have_attribute("aria-current", "page")
    assert 20 in offsets
    first.click()
    expect(first).to_have_attribute("aria-current", "page")
    expect(finished.locator(".transfer-category-title > span")).to_have_text("20")
    expect(folder.locator(".transfer-batch-count")).to_have_text("72 files")
    folder.get_by_role("button", name="Show Files", exact=True).click()
    expect(folder.locator(".transfer-item")).to_have_count(20)
    for count in (40, 60, 72):
        folder.locator("button.transfer-show-more").click()
        expect(folder.locator(".transfer-item")).to_have_count(count)
    expect(folder.locator("button.transfer-show-more")).to_have_count(0)
    assert offsets[-1] == 0


def test_upload_concurrency_setting_saves_and_survives_reload(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    page.emulate_media(color_scheme="dark")
    login(page, server.url)
    open_admin(page)
    page.get_by_role("link", name="Transfer limits", exact=True).click()
    setting = page.get_by_role("region", name="Parallel uploads", exact=True)
    field = setting.get_by_label("Simultaneous Uploads per Bot", exact=True)
    expect(field).to_have_value("1")
    setting.get_by_role("button", name="More Uploads per Bot", exact=True).click()
    expect(field).to_have_value("2")
    setting.get_by_role("button", name="Save Changes", exact=True).click()
    expect(setting.get_by_role("status")).to_have_text("Upload settings saved.")
    page.reload()
    expect(field).to_have_value("2")
    setting.screenshot(path=str(ARTIFACTS / "upload-settings-dark.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme="light")
    expect(field).to_be_visible()
    setting.screenshot(path=str(ARTIFACTS / "upload-settings-mobile.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    setting.get_by_role("button", name="Fewer Uploads per Bot", exact=True).click()
    setting.get_by_role("button", name="Save Changes", exact=True).click()
    expect(setting.get_by_role("status")).to_have_text("Upload settings saved.")


def test_transfer_sections_locate_highlight_and_normal_preview(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    login(page, server.url)
    upload(page, ["completed-location.txt"])
    first = server.wait_started(1)[0]
    server.control("/finish/" + first["id"], method="POST")
    expect(page.locator(".transfer-item[data-status=completed]")).to_be_visible()
    upload(page, ["still-running.bin"], add=True)
    server.wait_started(2)
    held = []

    def hold_device(route: Any):
        if route.request.method == "PATCH":
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/uploads/*", hold_device)
    upload(page, ["device-receiving.bin", "queued-device.bin"], add=True)
    page.get_by_role("link", name="Transfers", exact=False).click()
    history = page.get_by_role("region", name="Transfer History", exact=True)
    for label in ("Running", "Finished"):
        expect(history.get_by_role("region", name=label, exact=True)).to_be_visible()
    expect(history.get_by_role("region", name="Queued", exact=True)).to_have_count(0)
    completed = history.locator(".transfer-item[data-status=completed]")
    expect(completed.locator(".transfer-location")).to_have_text("My Files")
    expect(completed).not_to_contain_text("Elapsed")
    expect(completed.locator(".transfer-batch-percent")).to_have_count(0)
    expect(completed.locator(".transfer-bytes")).not_to_contain_text("/")
    page.screenshot(path=ARTIFACTS / "transfer-sections-light.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme="dark")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=ARTIFACTS / "transfer-sections-mobile.png", full_page=True)
    completed.locator(".transfer-item-name").dblclick()
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    selected = page.locator(".file-row.selected")
    expect(selected).to_have_count(1)
    expect(selected).to_contain_text("completed-location.txt")
    expect(page.get_by_role("dialog")).to_have_count(0)
    selected.get_by_role("button", name="completed-location.txt", exact=True).dblclick()
    expect(page.get_by_role("dialog")).to_be_visible()
    for route in held:
        route.abort()


def test_transfer_columns_handle_long_names_paths_and_narrow_screens(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    long_name = "Very-long-file-" + "filenamewithoutspaces" * 10 + "-résumé-写真.zip"
    long_folder = "My Files / " + "Projects-with-a-long-name / " * 16 + "Final"
    now = time.time()
    jobs = []
    for index, status in enumerate(
        ("completed", "cancelled", "queued", "running", "error")
    ):
        jobs.append(
            {
                "id": "column-fixture-" + str(index),
                "name": long_name if status != "cancelled" else "Cancelled-upload.zip",
                "status": status,
                "phase": "uploading" if status == "running" else status,
                "current": 100 if status == "completed" else 40,
                "total": 100,
                "kind": "upload",
                "destination": "/",
                "created_at": now - 100,
                "finished_at": now if status in ("completed", "cancelled") else None,
                "error": "Transfer cancelled"
                if status == "cancelled"
                else "Unbroken_error_" + "details" * 40
                if status == "error"
                else None,
                "location": {
                    "available": True,
                    "folder_path": "/",
                    "folder_label": long_folder,
                    "item_id": "exact",
                    "display_path": long_folder + " / " + long_name,
                },
            }
        )
    page.route(
        "**/api/v1/transfers/history/tasks",
        lambda route: route.fulfill(
            json={
                "jobs": jobs,
                "page": 1,
                "page_size": 20,
                "total_tasks": len(jobs),
                "total_pages": 1,
                "has_more": False,
            }
        ),
    )
    login(page, server.url)
    page.get_by_role("link", name="Transfers", exact=False).click()
    history = page.get_by_role("region", name="Transfer History", exact=True)
    expect(history.locator(".transfer-item")).to_have_count(5)
    completed = history.locator(".transfer-item[data-status=completed]")
    expect(completed.locator(".transfer-item-name")).to_have_attribute(
        "title", long_name
    )
    expect(completed.locator(".transfer-location")).to_have_attribute(
        "title", long_folder + " / " + long_name
    )
    for inactive_location in history.locator(
        ".transfer-item:not([data-status=completed])"
    ).all():
        expect(
            inactive_location.get_by_role("button", name="Show ", exact=False)
        ).to_have_count(0)
        inactive_location.locator(".transfer-item-name").dblclick()
        expect(
            page.get_by_role("heading", name="Transfers", exact=True)
        ).to_be_visible()
    cancelled = history.locator(".transfer-item[data-status=cancelled]")
    expect(cancelled.locator(".transfer-item-actions button")).to_have_count(0)
    expect(cancelled.locator(".transfer-error")).to_have_count(0)

    for width in (1920, 1440, 1100, 768, 390, 320):
        page.set_viewport_size({"width": width, "height": 1000})
        page.emulate_media(color_scheme="dark" if width < 720 else "light")
        page.wait_for_timeout(50)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        layout = completed.evaluate("""row => {
          const rect = el => {
            const r = el.getBoundingClientRect();
            return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,
              width:r.width,height:r.height};
          };
          return {row:rect(row), name:rect(row.querySelector('.transfer-item-name')),
            folder:rect(row.querySelector('.transfer-location')),
            size:rect(row.querySelector('.transfer-bytes')),
            date:rect(row.querySelector('time')),
            actions:rect(row.querySelector('.transfer-item-actions'))};
        }""")
        for field in ("name", "folder", "size", "date", "actions"):
            assert layout[field]["x"] >= layout["row"]["x"] - 1, (width, field, layout)
            assert layout[field]["right"] <= layout["row"]["right"] + 1, (
                width,
                field,
                layout,
            )
        if width >= 1440:
            assert abs(layout["name"]["y"] - layout["folder"]["y"]) < 3
            assert layout["name"]["right"] <= layout["folder"]["x"]
            assert layout["folder"]["right"] <= layout["size"]["x"]
            assert layout["size"]["right"] <= layout["date"]["x"]
            assert layout["row"]["height"] <= 60
        if width in (1920, 390):
            page.screenshot(
                path=ARTIFACTS / ("transfer-columns-" + str(width) + ".png"),
                full_page=True,
            )


def test_original_file_recovery_action_is_inline_and_not_stacked(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    name = "Long original video filename " + "archive-" * 15 + ".mkv"
    saved = [
        {
            "key": "recovery-layout",
            "initiatedHere": True,
            "name": name,
            "size": 4500000000,
            "lastModified": 0,
            "destination": "/",
            "state": "receiving",
            "phase": "Sending from this device",
            "current": 3900000000,
            "total": 4500000000,
            "createdAt": time.time() * 1000,
        }
    ]
    page.add_init_script(
        "localStorage.setItem('tgdrive.uploads.v2', "
        + json.dumps(json.dumps(saved))
        + ");"
    )
    login(page, server.url)
    page.get_by_role("link", name="Transfers", exact=False).click()
    item = page.locator(".transfer-item[data-status=awaiting_file]")
    expect(item).to_be_visible()
    action = item.get_by_role("button", name="Choose File", exact=True)
    expect(
        item.locator(".transfer-recovery").get_by_role("button", name="Choose File")
    ).to_be_visible()
    expect(item.locator(".transfer-item-actions button")).to_have_count(1)
    expect(
        item.locator(".transfer-item-actions").get_by_role(
            "button", name="Cancel " + name
        )
    ).to_be_visible()
    expect(
        item.get_by_role("button", name="Show " + name + " in folder")
    ).to_have_count(0)
    for width in (1440, 390):
        page.set_viewport_size({"width": width, "height": 960})
        page.emulate_media(color_scheme="dark" if width == 1440 else "light")
        expect(action).to_be_visible()
        bounds = item.evaluate("""row => {
          const r = el => {
            const b=el.getBoundingClientRect();
            return {x:b.x,y:b.y,right:b.right,bottom:b.bottom,height:b.height};
          };
          return {row:r(row), recovery:r(row.querySelector('.transfer-recovery')),
            message:r(row.querySelector('.transfer-recovery-message')),
            action:r(row.querySelector('.transfer-recovery-button')),
            cancel:r(row.querySelector('.transfer-item-actions button'))};
        }""")
        assert bounds["action"]["right"] <= bounds["recovery"]["right"] + 1
        assert bounds["cancel"]["y"] < bounds["action"]["bottom"]
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        if width == 1440:
            message_center = bounds["message"]["y"] + bounds["message"]["height"] / 2
            action_center = bounds["action"]["y"] + bounds["action"]["height"] / 2
            assert abs(message_center - action_center) <= 1
        page.screenshot(
            path=ARTIFACTS / ("transfer-recovery-" + str(width) + ".png"),
            full_page=True,
        )
    with page.expect_file_chooser():
        action.click()


def test_ordinary_files_stay_separate_and_folder_headers_stay_compact(
    server: Server, desktop: tuple[Any, Any]
) -> None:
    _, page = desktop
    now = time.time() * 1000
    saved = []
    jobs = []
    for batch_id, batch_name, count, folder, state in (
        ("six-files", "6 files", 6, False, "completed"),
        (
            "photos",
            "Photos from a long memorable holiday " + "album-" * 12,
            3,
            True,
            "completed",
        ),
        ("one-folder", "Single document folder", 1, True, "completed"),
        ("running-files", "2 files", 2, False, "uploading"),
        ("needs-files", "3 files", 3, False, "awaiting_file"),
    ):
        for index in range(count):
            identity = batch_id + "-" + str(index)
            name = (
                "Long filename " + "photo-" * 20
                if folder
                else batch_id + "-document-" + str(index)
            ) + ".txt"
            relative = (
                batch_name + ("/nested/" if folder and index else "/") + name
                if folder
                else None
            )
            destination = (
                "/photos/nested" if folder and index else "/photos" if folder else "/"
            )
            folder_upload = (
                {
                    "id": batch_id + "-folder-selection",
                    "name": batch_name,
                    "root_path": "/photos",
                    "created_at": now / 1000,
                }
                if folder
                else None
            )
            saved.append(
                {
                    "key": identity,
                    "initiatedHere": True,
                    **({"folderUpload": folder_upload} if folder_upload else {}),
                    "jobId": identity if state != "awaiting_file" else None,
                    "name": name,
                    "size": 64 * 1024 * 1024,
                    "lastModified": 0,
                    "destination": destination,
                    "relativePath": relative,
                    "batchId": folder_upload["id"] if folder_upload else batch_id,
                    "batchName": batch_name,
                    "batchCreatedAt": now,
                    "state": state,
                    "phase": "Upload completed"
                    if state == "completed"
                    else "Choose the original file to resume"
                    if state == "awaiting_file"
                    else "Uploading to Telegram",
                    "current": 64 * 1024 * 1024
                    if state == "completed"
                    else 30 * 1024 * 1024,
                    "total": 64 * 1024 * 1024,
                    "finishedAt": now if state == "completed" else None,
                }
            )
            if state == "completed":
                jobs.append(
                    {
                        "id": identity,
                        "name": name,
                        "destination": destination,
                        **({"folder_upload": folder_upload} if folder_upload else {}),
                        "status": "completed",
                        "phase": "completed",
                        "current": 64 * 1024 * 1024,
                        "total": 64 * 1024 * 1024,
                        "created_at": now / 1000 - 100,
                        "finished_at": now / 1000,
                        "location": {
                            "available": True,
                            "folder_path": destination,
                            "folder_label": (
                                "My Files / "
                                + batch_name
                                + (" / nested" if index else "")
                            )
                            if folder
                            else "My Files",
                            "item_id": identity,
                            "display_path": name,
                        },
                    }
                )
    page.add_init_script(
        "localStorage.setItem('tgdrive.uploads.v2', "
        + json.dumps(json.dumps(saved))
        + ");"
    )

    def task_history(route: Any) -> None:
        payload = route.request.post_data_json
        rows = jobs
        total = len(
            {
                ("folder", job["folder_upload"]["id"])
                if job.get("folder_upload")
                else ("file", job["id"])
                for job in rows
            }
        )
        route.fulfill(
            json={
                "jobs": rows,
                "page": payload["page"],
                "page_size": 20,
                "total_tasks": total,
                "total_pages": 1,
                "has_more": False,
            }
        )

    page.route("**/api/v1/transfers/history/tasks", task_history)
    login(page, server.url)
    page.get_by_role("link", name="Transfers", exact=False).click()
    history = page.get_by_role("region", name="Transfer History", exact=True)
    multi = history.locator(".transfer-item").filter(
        has=page.locator(".transfer-item-name", has_text="six-files-document-")
    )
    folder = history.locator(".transfer-batch").filter(
        has=page.locator(
            ".transfer-batch-name", has_text="Photos from a long memorable holiday"
        )
    )
    single_folder = history.locator(".transfer-batch").filter(
        has=page.locator(".transfer-batch-name", has_text="Single document folder")
    )
    expect(multi).to_have_count(6)
    expect(history.locator(".transfer-batch")).to_have_count(2)
    expect(folder.locator(".transfer-batch-count")).to_have_text("3 files")
    expect(single_folder.locator(".transfer-batch-count")).to_have_text("1 file")
    expect(multi.locator(".transfer-batch-status")).to_have_count(0)
    expect(multi.locator(".transfer-history-time")).to_have_count(6)
    expect(
        history.locator(".transfer-item")
        .filter(
            has=page.locator(".transfer-item-name", has_text="running-files-document-")
        )
        .locator(".transfer-item-actions .icon-button[title='Show in Folder']")
    ).to_have_count(0)
    expect(
        history.locator(".transfer-item")
        .filter(
            has=page.locator(".transfer-item-name", has_text="needs-files-document-")
        )
        .get_by_role("button", name="Choose File", exact=True)
    ).to_have_count(3)
    for width in (1920, 1440, 390, 320):
        page.set_viewport_size({"width": width, "height": 1000})
        page.emulate_media(color_scheme="dark" if width >= 1440 else "light")
        if width >= 1440:
            assert (
                folder.locator(".transfer-batch-summary").bounding_box()["height"] <= 64
            )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        assert history.locator(".transfer-batch-summary").evaluate_all("""
          rows => rows.every(row => {
          const b=row.getBoundingClientRect();
          const selectors = ['button', '.transfer-batch-size',
            '.transfer-batch-time', '.transfer-batch-location'].join(',');
          return [...row.querySelectorAll(selectors)].every(el=>{
            const r=el.getBoundingClientRect();return r.x>=b.x-1&&r.right<=b.right+1;
          });
        })""")
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.emulate_media(color_scheme="dark")
    location_requests = []

    def locate_route(route):
        location_requests.append(route.request.url)
        route.fulfill(json={"available": True, "folder_path": "/photos"})

    page.route("**/api/v1/transfers/location", locate_route)
    folder.locator(".transfer-batch-name").dblclick(delay=30)
    expect(
        folder.get_by_role("button", name="Show Files", exact=True)
    ).to_have_attribute("aria-expanded", "false")
    assert location_requests == []
    folder.get_by_role("button", name="Show Files", exact=True).click()
    folder.get_by_role("button", name="Hide Files", exact=True).click()
    assert location_requests == []
    expect(page.get_by_role("heading", name="Transfers", exact=True)).to_be_visible()
    expect(multi.get_by_role("button", name="Show Files", exact=True)).to_have_count(0)
    expect(multi).to_have_count(6)
    folder.get_by_role("button", name="Show Files", exact=True).click()
    expect(folder.locator(".transfer-item")).to_have_count(3)
    expect(
        folder.locator(".transfer-batch-files .transfer-history-time")
    ).to_have_count(0)
    assert all(
        not label.startswith("My Files")
        for label in folder.locator(
            ".transfer-child .transfer-location"
        ).all_text_contents()
    )
    single_folder.get_by_role("button", name="Show Files", exact=True).click()
    expect(single_folder.locator(".transfer-item")).to_have_count(1)
    assert folder.locator(".transfer-child").evaluate_all("""rows => rows.every(row => {
      const title = row.querySelector('.transfer-item-title').getBoundingClientRect();
      const bytes = row.querySelector('.transfer-bytes').getBoundingClientRect();
      return title.right <= bytes.left + 1;
    })""")
    folder.scroll_into_view_if_needed()
    page.screenshot(path=ARTIFACTS / "compact-batches-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 1000})
    page.emulate_media(color_scheme="light")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    folder.scroll_into_view_if_needed()
    page.screenshot(path=ARTIFACTS / "compact-batches-mobile.png", full_page=True)

    folder.locator(".transfer-batch-locate").click()
    assert len(location_requests) == 1
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()


def test_shared_links_compact_views_copy_location_and_indicators(server, desktop):
    context, page = desktop
    login(page, server.url)
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=server.url)
    contents = page.request.post(
        server.url + "/api/getDirectory", data={"path": "/"}
    ).json()["data"]["contents"]
    folder = next(item for item in contents.values() if item["name"] == "資料")
    folder_path = "/" + folder["id"]
    created = page.request.post(
        server.url + "/api/v1/shares",
        data={"path": folder_path, "expires_in_days": None},
    ).json()
    page.reload()
    expect(page.get_by_label("Shared by Link", exact=True)).to_have_count(1)
    page.get_by_role("button", name="資料", exact=True).dblclick()
    expect(
        page.get_by_label("Shared Through Parent Folder", exact=True).first
    ).to_be_visible()
    page.get_by_role("link", name="Shared Links", exact=True).click()
    current = page.request.get(server.url + "/api/v1/shares").json()["shares"][0]
    long_name = "Annual-report-" * 16 + ".pdf"
    expired = dict(
        current,
        id="expired-fixture",
        node_id="expired-node",
        name=long_name,
        type="file",
        size=985000,
        created_at="2020-01-01T10:00:00Z",
        expires_at="2020-01-02T10:00:00Z",
        folder_label="My Files / " + "A-long-directory-name/" * 16,
    )
    legacy = dict(
        current,
        id="legacy-fixture",
        node_id="legacy-node",
        path="/legacy-node",
        name="Previous link.txt",
        type="file",
        size=42000,
        copyable=False,
    )
    page.route(
        "**/api/v1/shares",
        lambda route: (
            route.fulfill(json={"shares": [current, expired, legacy]})
            if route.request.method == "GET"
            else route.continue_()
        ),
    )
    page.get_by_role("button", name="Refresh Shared Links", exact=True).click()
    expect(page.locator(".share-row")).to_have_count(2)
    active_row = page.locator(
        ".share-row", has=page.get_by_role("button", name="資料", exact=True)
    )
    copy_button = active_row.locator(".share-copy")
    expect(copy_button).to_have_text("")
    copy_button.click()
    expect(copy_button).to_have_attribute(
        "aria-label", __import__("re").compile("^Link copied")
    )
    copied = page.evaluate("navigator.clipboard.readText()")
    assert created["auth"] in copied and folder["id"] in copied
    expect(page.get_by_text("Unnamed link", exact=False)).to_have_count(0)
    unavailable_row = page.locator(".share-row").filter(has_text="Previous link.txt")
    expect(unavailable_row.locator(".share-copy")).to_have_attribute(
        "aria-disabled", "true"
    )
    expect(unavailable_row.locator(".share-copy")).to_be_disabled()
    copy_requests = []
    page.on(
        "request",
        lambda request: (
            copy_requests.append(request.url)
            if request.url.endswith("/api/v1/shares/link")
            else None
        ),
    )
    unavailable_control = unavailable_row.locator(".share-copy-control")
    unavailable_control.focus()
    expect(unavailable_control.get_by_role("tooltip")).to_have_text(
        "Original link is unavailable; existing copies still work."
    )
    unavailable_control.press("Escape")
    expect(unavailable_control).not_to_be_focused()
    unavailable_control.click()
    expect(unavailable_control).to_be_focused()
    unavailable_row.locator(".share-copy").evaluate("button => button.click()")
    expect(page.locator(".toast-error")).to_have_count(0)
    assert not copy_requests
    expect(page.get_by_role("button", name="New Link", exact=True)).to_have_count(0)
    assert (
        page.request.post(
            server.url + "/api/getDirectory",
            data={"path": "/share_" + folder["id"], "auth": created["auth"]},
        ).status
        == 200
    )
    page.get_by_role("button", name="All 3", exact=True).click()
    expect(page.locator(".share-row")).to_have_count(3)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        page.evaluate(
            "(theme) => { localStorage.setItem('tgdrive.appearance', theme);"
            "document.documentElement.dataset.theme = theme; }",
            theme,
        )
        for width in (1440, 390, 320):
            page.set_viewport_size(
                {"width": width, "height": 960 if width > 600 else 844}
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator(".share-row").evaluate_all(
                "rows => rows.every(row => row.scrollWidth <= row.clientWidth + 1)"
            )
            if width in (1440, 390):
                page.screenshot(
                    path=str(ARTIFACTS / f"shared-links-{theme}-{width}.png")
                )
    page.set_viewport_size({"width": 1440, "height": 960})
    page.get_by_role("button", name="Expired 1", exact=True).click()
    expect(page.locator(".share-row")).to_have_count(1)
    expect(page.locator(".share-row .share-copy")).to_have_attribute(
        "aria-disabled", "true"
    )
    expect(page.locator(".share-row .share-copy")).to_be_disabled()
    expect(page.get_by_role("button", name="New Link", exact=True)).to_have_count(0)
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.get_by_role("button", name="Active 2", exact=True).click()
    page.get_by_role("button", name="Show 資料 in folder", exact=True).click()
    expect(page.get_by_role("heading", name="My Files", exact=True)).to_be_visible()
    expect(
        page.locator("tr", has=page.get_by_role("button", name="資料", exact=True))
    ).to_have_class(__import__("re").compile("selected"))
    page.get_by_role("link", name="Shared Links", exact=True).click()
    page.get_by_role("button", name="Revoke Link for 資料", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Cancel", exact=True).click()
    assert (
        page.request.post(
            server.url + "/api/getDirectory",
            data={"path": "/share_" + folder["id"], "auth": created["auth"]},
        ).status
        == 200
    )
    page.get_by_role("button", name="Revoke Link for 資料", exact=True).click()
    page.get_by_role("button", name="Revoke Link", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert (
        page.request.post(
            server.url + "/api/getDirectory",
            data={"path": "/share_" + folder["id"], "auth": created["auth"]},
        ).status
        == 403
    )


def test_mobile_navigation_traps_focus_closes_and_restores_trigger(server, desktop):
    _, page = desktop
    page.set_viewport_size({"width": 390, "height": 844})
    login(page, server.url)
    trigger = page.get_by_role("button", name="Toggle Navigation", exact=True)
    trigger.click()
    navigation = page.get_by_role("dialog", name="Drive navigation", exact=True)
    expect(navigation).to_be_visible()
    expect(page.locator("#main-content")).to_have_attribute("inert", "")
    for _ in range(12):
        page.keyboard.press("Tab")
        assert navigation.evaluate("(node)=>node.contains(document.activeElement)")
    page.keyboard.press("Escape")
    expect(navigation).to_have_count(0)
    expect(trigger).to_be_focused()
    trigger.click()
    page.get_by_role("button", name="Close Navigation", exact=True).first.click()
    expect(trigger).to_be_focused()
    trigger.click()
    page.set_viewport_size({"width": 1440, "height": 960})
    page.set_viewport_size({"width": 390, "height": 844})
    expect(trigger).to_have_attribute("aria-expanded", "false")


def test_selection_mixed_state_and_next_page_scroll_position(server, desktop):
    _, page = desktop
    page.set_viewport_size({"width": 390, "height": 844})
    contents = {
        f"entry-{index}": {
            "id": f"entry-{index}",
            "path": "/",
            "type": "file",
            "name": f"{index:03d} " + "資料-😀-very-long-filename-" * 10 + ".pdf",
            "size": 12345678,
            "upload_date": "2026-10-17",
        }
        for index in range(105)
    }
    page.route(
        "**/api/getDirectory",
        lambda route: route.fulfill(
            json={"status": "ok", "data": {"name": "/", "contents": contents}}
        ),
    )
    login(page, server.url)
    page.get_by_role("button", name="Select", exact=True).click()
    checkbox = page.get_by_role(
        "checkbox", name="Select All Files on This Page", exact=True
    )
    page.locator("tbody input[type=checkbox]").first.check()
    assert checkbox.evaluate("(input)=>input.indeterminate")
    target = checkbox.locator("..").bounding_box()
    assert target["width"] >= 44 and target["height"] >= 44
    page.get_by_role("button", name="Next Page", exact=True).click()
    expect(page.locator("tbody .file-name").first).to_have_text(
        contents["entry-100"]["name"]
    )
    page.wait_for_function("document.querySelector('.workspace').scrollTop < 5")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_history_and_shares_failed_loads_have_retry_without_false_empty_state(
    server, desktop
):
    _, page = desktop
    failing = {"history": True, "shares": True}

    def history(route):
        if failing["history"]:
            route.fulfill(
                status=503, json={"detail": "History temporarily unavailable"}
            )
        else:
            route.fulfill(
                json={
                    "jobs": [],
                    "page": 1,
                    "page_size": 20,
                    "total_tasks": 0,
                    "total_pages": 1,
                    "has_more": False,
                }
            )

    def shares(route):
        if failing["shares"]:
            route.fulfill(
                status=503, json={"detail": "Shared links temporarily unavailable"}
            )
        else:
            route.fulfill(json={"shares": []})

    page.route("**/api/v1/transfers/history/tasks", history)
    page.route("**/api/v1/shares", shares)
    login(page, server.url)
    page.get_by_role("link", name="Transfers", exact=False).click()
    expect(page.get_by_role("button", name="Try Again", exact=True)).to_be_visible()
    expect(
        page.get_by_role("heading", name="No Finished Transfers Yet", exact=True)
    ).to_have_count(0)
    failing["history"] = False
    page.get_by_role("button", name="Try Again", exact=True).click()
    expect(
        page.get_by_role("heading", name="No Finished Transfers Yet", exact=True)
    ).to_be_visible()
    page.get_by_role("link", name="Shared Links", exact=True).click()
    expect(page.get_by_role("button", name="Try Again", exact=True)).to_be_visible()
    failing["shares"] = False
    page.get_by_role("button", name="Try Again", exact=True).click()
    expect(
        page.get_by_role("heading", name="No Active Shared Links", exact=True)
    ).to_be_visible()


def test_revoke_failure_stays_in_dialog_and_management_drops_do_not_upload(
    server, desktop
):
    _, page = desktop

    def share_route(route):
        if route.request.method == "DELETE":
            route.fulfill(
                status=503, json={"detail": "Could not revoke this link. Try again."}
            )
        else:
            route.fulfill(
                json={
                    "shares": [
                        {
                            "id": "audit-share",
                            "node_id": "audit-item",
                            "path": "/audit-item",
                            "name": "A shared item",
                            "folder_path": "/",
                            "folder_label": "My Files",
                            "type": "file",
                            "size": 10,
                            "created_at": "2026-10-17T00:00:00Z",
                            "expires_at": None,
                            "copyable": True,
                        }
                    ]
                }
            )

    page.route("**/api/v1/shares", share_route)
    login(page, server.url)
    page.get_by_role("link", name="Shared Links", exact=True).click()
    page.get_by_role("button", name="Revoke Link for A shared item", exact=True).click()
    dialog = page.get_by_role("dialog", name="Revoke This Link?", exact=True)
    dialog.get_by_role("button", name="Revoke Link", exact=True).click()
    expect(dialog.get_by_role("alert")).to_have_text(
        "Could not revoke this link. Try again."
    )
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(dialog).to_have_count(0)
    page.get_by_role("link", name="Transfers", exact=False).click()
    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.url)
            if request.method == "POST" and request.url.endswith("/api/v1/uploads")
            else None
        ),
    )
    page.locator("#main-content").evaluate("""element => {
      const data = new DataTransfer();
      data.items.add(new File(['data'], 'must-not-upload.txt'));
      element.dispatchEvent(new DragEvent('drop', {
        bubbles:true, cancelable:true, dataTransfer:data
      }));
    }""")
    expect(page.get_by_role("alert")).to_contain_text(
        "Open a folder in My Files before dropping files."
    )
    assert requests == []
