"""File renames preserve the extension used by previews and downloads."""

from __future__ import annotations

import os

import pytest

if os.environ.get("TGDRIVE_E2E") != "1":
    pytest.skip("Set TGDRIVE_E2E=1 for Chromium checks", allow_module_level=True)

import tests.browser.test_browser_e2e as fixtures
from tests.browser.test_browser_e2e import (
    ARTIFACTS,
    expect,
    finish_fixture_uploads,
    fixture_nodes,
    login,
    preview_dialog,
    upload,
)

browser = fixtures.browser
desktop = fixtures.desktop
server = fixtures.server
pytestmark = pytest.mark.e2e


def rename_dialog(page, name):
    page.get_by_label("Actions for " + name, exact=True).click()
    page.get_by_role("button", name="Rename", exact=True).click()
    dialog = page.get_by_role("dialog", name="Rename", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_label("Name", exact=True)).to_be_focused()
    return dialog


def save_name(page, dialog, name):
    dialog.get_by_label("Name", exact=True).fill(name)
    with page.expect_response("**/api/renameFileFolder") as saved:
        dialog.get_by_role("button", name="Save Name", exact=True).click()
    assert saved.value.ok, saved.value.text()
    expect(dialog).not_to_be_visible()
    return saved.value.request.post_data_json["name"]


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_file_rename_keeps_extension_and_video_preview(server, desktop, width):
    _, page = desktop
    page.set_viewport_size({"width": width, "height": 844})
    login(page, server.url)
    page.get_by_role("button", name="Preview samples", exact=True).press("Enter")
    dialog = rename_dialog(page, "movtext-subtitles.mp4")
    name = dialog.get_by_label("Name", exact=True)
    expect(name).to_have_value("movtext-subtitles")
    expect(name).to_have_attribute("aria-describedby", "item-name-hint")
    suffix = dialog.locator(".rename-extension")
    expect(suffix).to_be_visible()
    expect(suffix).to_have_text(".mp4")
    assert suffix.evaluate(
        "element => !element.isContentEditable && "
        "!element.matches('input,textarea,select')"
    )
    expect(dialog.locator("#item-name-hint")).to_contain_text("extension")
    name.fill("earth.v2")
    expect(suffix).to_have_text(".mp4")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    bounds = dialog.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    page.screenshot(path=ARTIFACTS / f"rename-extension-{width}.png")

    assert save_name(page, dialog, "earth.v2") == "earth.v2.mp4"
    renamed = page.get_by_role("button", name="earth.v2.mp4", exact=True)
    expect(renamed).to_be_visible()
    renamed.press("Enter")
    preview = preview_dialog(page)
    player = preview.locator("movi-player")
    expect(player).to_be_visible(timeout=30000)
    expect(preview.locator(".advanced-state")).to_have_count(0, timeout=30000)
    player.locator("button.movi-play-pause").click()
    page.wait_for_function(
        "() => document.querySelector('movi-player').player"
        ".getRenderHealth().framesPresented >= 1",
        timeout=15000,
    )
    player.evaluate("element => element.pause()")
    with page.expect_download() as download:
        preview.get_by_role("link", name="Download Original", exact=True).click()
    assert download.value.suggested_filename == "earth.v2.mp4"


def test_dotted_folder_rename_edits_the_complete_name(server, desktop):
    _, page = desktop
    login(page, server.url)
    created = page.request.post(
        server.url + "/api/createNewFolder",
        data={"path": "/", "name": "Project.v1"},
    )
    assert created.ok, created.text()
    page.reload()
    dialog = rename_dialog(page, "Project.v1")
    expect(dialog.get_by_label("Name", exact=True)).to_have_value("Project.v1")
    expect(dialog.locator(".rename-extension")).to_have_count(0)
    assert save_name(page, dialog, "Project.v2") == "Project.v2"
    expect(page.get_by_role("button", name="Project.v2", exact=True)).to_be_visible()
    names = [node["name"] for node in fixture_nodes(page, server, "/")]
    assert "Project.v2" in names and "Project.v2.v1" not in names


def test_extensionless_file_can_be_renamed_and_have_its_extension_restored(
    server, desktop
):
    _, page = desktop
    login(page, server.url)
    upload(page, ["README"])
    finish_fixture_uploads(page, server, 1)
    page.reload()
    dialog = rename_dialog(page, "README")
    expect(dialog.get_by_label("Name", exact=True)).to_have_value("README")
    expect(dialog.locator(".rename-extension")).to_have_count(0)
    assert save_name(page, dialog, "restored.txt") == "restored.txt"
    expect(page.get_by_role("button", name="restored.txt", exact=True)).to_be_visible()

    dialog = rename_dialog(page, "restored.txt")
    expect(dialog.get_by_label("Name", exact=True)).to_have_value("restored")
    expect(dialog.locator(".rename-extension")).to_have_text(".txt")
    dialog.get_by_role("button", name="Cancel", exact=True).click()
