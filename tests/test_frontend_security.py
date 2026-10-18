"""Security checks target authored React sources and the production entry."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBSITE = ROOT / "website"


def test_authored_frontend_has_no_html_injection_or_dynamic_code_sinks() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (WEBSITE / "src").glob("*")
        if path.suffix in {".ts", ".tsx"} and ".test." not in path.name
    )
    for forbidden in (
        "dangerouslySetInnerHTML",
        ".innerHTML",
        "insertAdjacentHTML",
        "eval(",
        "document.write",
    ):
        assert forbidden not in sources


def test_production_entry_and_player_have_no_inline_or_remote_active_content() -> None:
    entries = [WEBSITE / "index.html", WEBSITE / "public" / "VideoPlayer.html"]
    if (WEBSITE / "dist" / "index.html").exists():
        entries.append(WEBSITE / "dist" / "index.html")
    for path in entries:
        html = path.read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)
        assert "<style" not in html.lower()
        assert not re.search(r"<script(?![^>]+src=)[^>]*>", html, re.IGNORECASE)
        assert not re.search(r"(?:src|href)=[\"']https?://", html, re.IGNORECASE)
        ids = re.findall(r'\bid="([^"]+)"', html)
        assert len(ids) == len(set(ids)), path


def test_production_assets_are_local_and_content_addressed() -> None:
    entry = WEBSITE / "dist" / "index.html"
    if not entry.exists():
        return  # Source packages build this artifact in the Docker frontend stage.
    for asset in re.findall(r'(?:src|href)="(/assets/[^"]+)"', entry.read_text()):
        assert re.search(r"-[A-Za-z0-9_-]+\.(?:js|css)$", asset)
        assert (WEBSITE / "dist" / asset.lstrip("/")).is_file()


def test_frontend_dependencies_are_locked() -> None:
    package = json.loads((WEBSITE / "package.json").read_text())
    lock = json.loads((WEBSITE / "package-lock.json").read_text())
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    for version in (package["dependencies"] | package["devDependencies"]).values():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), version


def test_frontend_text_is_clean_utf8() -> None:
    suspicious = ("â€", "ðŸ", "�")
    for base in (WEBSITE / "src", WEBSITE / "public"):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {
                ".html",
                ".css",
                ".js",
                ".ts",
                ".tsx",
                ".svg",
            }:
                text = path.read_text(encoding="utf-8")
                assert not any(marker in text for marker in suspicious), path
