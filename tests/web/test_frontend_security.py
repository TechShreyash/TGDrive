"""Security checks target authored React sources and the production entry."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEBSITE = ROOT / "website"


def test_authored_frontend_has_no_html_injection_or_dynamic_code_sinks() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (WEBSITE / "src").rglob("*")
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
    manifest = json.loads((WEBSITE / "vendor/manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert set(manifest["packages"]) == {"movi-player", "pdfjs-dist"}
    dependencies = package["dependencies"] | package["devDependencies"]
    for name, version in dependencies.items():
        if name not in manifest["packages"]:
            assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
            continue
        viewer = manifest["packages"][name]
        vendored = viewer["vendored"]
        assert version == "file:vendor/" + vendored["archive"]
        archive = WEBSITE / version.removeprefix("file:")
        assert archive.parent == WEBSITE / "vendor"
        assert archive.suffix == ".tgz"
        content = archive.read_bytes()
        actual_integrity = "sha512-" + base64.b64encode(
            hashlib.sha512(content).digest()
        ).decode("ascii")
        entry = lock["packages"]["node_modules/" + name]
        assert entry["resolved"] == version
        assert entry["version"] == vendored["version"]
        assert entry["integrity"] == vendored["integrity"] == actual_integrity
        assert hashlib.sha256(content).hexdigest() == vendored["sha256"]
        patch = viewer["patch"]
        recipes = {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in patch["recipe_files_sha256"]
        }
        assert patch["recipe"] in recipes
        assert recipes == patch["recipe_files_sha256"]
        recipe_hash = (
            hashlib.sha256(json.dumps(recipes, sort_keys=True).encode()).hexdigest()
            if len(recipes) > 1
            else recipes[patch["recipe"]]
        )
        assert recipe_hash == patch["recipe_sha256"]
    for hook in ("predev", "prebuild", "pretest", "dev", "build", "test"):
        command = package["scripts"].get(hook, "")
        assert "patch-movi-player.js" not in command
        assert "patch-pdf-worker.js" not in command


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
