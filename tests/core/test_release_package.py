from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import (
    PUBLIC_DOCS,
    ROOT_FILES,
    SOURCE_TREES,
    build_release,
    check_version,
)


def _source_tree(root: Path) -> None:
    for name in (*ROOT_FILES, *PUBLIC_DOCS):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public source\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "tgdrive"\nversion = "2.0.0"\n', encoding="utf-8"
    )
    for directory in SOURCE_TREES:
        (root / directory).mkdir(exist_ok=True)
    (root / "tgdrive/__init__.py").write_text('__version__ = "2.0.0"\n')
    (root / "website/package.json").write_text('{"version": "2.0.0"}\n')
    (root / "website/package-lock.json").write_text(
        '{"version": "2.0.0", "packages": {"": {"version": "2.0.0"}}}\n'
    )
    (root / "Dockerfile").write_text('LABEL org.opencontainers.image.version="2.0.0"\n')
    (root / "uv.lock").write_text(
        '[[package]]\nname = "tgdrive"\nversion = "2.0.0"\nsource = {virtual = "."}\n'
    )
    application = root / "tgdrive" / "web" / "app.py"
    application.parent.mkdir(parents=True, exist_ok=True)
    application.write_text("# application\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("tgdrive/__init__.py", '__version__ = "2.0.1"\n'),
        ("tgdrive/__init__.py", "# Missing version\n"),
        ("website/package.json", '{"version": "2.0.1"}'),
        (
            "website/package-lock.json",
            '{"version": "2.0.1", "packages": {"": {"version": "2.0.0"}}}',
        ),
        (
            "website/package-lock.json",
            '{"version": "2.0.0", "packages": {"": {"version": "2.0.1"}}}',
        ),
        ("Dockerfile", 'LABEL org.opencontainers.image.version="2.0.1"\n'),
        ("Dockerfile", "FROM python\n"),
        (
            "uv.lock",
            '[[package]]\nname = "tgdrive"\nversion = "2.0.1"\n'
            'source = {virtual = "."}\n',
        ),
        ("uv.lock", "version = 1\n"),
    ],
)
def test_source_archive_rejects_version_drift(
    tmp_path: Path, name: str, content: str
) -> None:
    _source_tree(tmp_path)
    (tmp_path / name).write_text(content, encoding="utf-8")
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="Release version mismatch"):
        build_release(tmp_path, output)

    assert not output.exists()


@pytest.mark.parametrize("version", ["2.0", "v2.0.0", "02.0.0", "2.0.0-rc.1"])
def test_release_version_rejects_invalid_format(tmp_path: Path, version: str) -> None:
    _source_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "tgdrive"\nversion = "{version}"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match=r"major\.minor\.patch"):
        check_version(tmp_path)


def test_release_tag_must_match_version(tmp_path: Path) -> None:
    _source_tree(tmp_path)
    assert check_version(tmp_path, "v2.0.0") == "2.0.0"

    for tag in ("v2.0.1", "2.0.0"):
        with pytest.raises(ValueError, match=r"Release tag must be v2\.0\.0"):
            check_version(tmp_path, tag)


def test_source_archive_excludes_local_state_and_private_documents(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _source_tree(source)
    private_names = (
        ".env",
        ".agents/skills/private/SKILL.md",
        ".codex/private.json",
        "skills-lock.json",
        "docs/request-tracker.md",
        "website/.agents/skills/private/config.json",
        "website/.codex/private.json",
        "website/skills-lock.json",
        ".git/config",
        "data/drive.json",
        "cache/uploads/private.upload",
        ".venv/credentials.py",
        "docs/private-local-notes.md",
        "tgdrive/__pycache__/private.py",
        "website/private.session",
        "website/node_modules/dependency/index.js",
        "website/dist/assets/index.js",
        "website/coverage/report.json",
        "website/.vite/cache.json",
        "website/.impeccable/design.json",
    )
    for name in private_names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE-CONTENT", encoding="utf-8")

    frontend_source = source / "website" / "src" / "app" / "App.tsx"
    frontend_source.parent.mkdir(parents=True, exist_ok=True)
    frontend_source.write_text("export default function App() { return null; }\n")
    patch_script = source / "website" / "scripts" / "patch-movi-player.js"
    patch_script.parent.mkdir(parents=True, exist_ok=True)
    patch_script.write_text("// reviewed portable seek fix\n", encoding="utf-8")
    brand_assets = (
        "website/public/static/brand/tgdrive-icon.svg",
        "website/public/static/brand/favicon-32.png",
        "website/public/static/brand/favicon.ico",
        "website/public/static/brand/apple-touch-icon.png",
        "website/public/static/brand/social-preview.png",
    )
    for name in brand_assets:
        asset = source / name
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"public brand asset")
    archive_path, checksum_path = build_release(source, tmp_path / "output")
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        assert "tgdrive-2.0.0/tgdrive/web/app.py" in names
        assert "tgdrive-2.0.0/.env.example" in names
        assert "tgdrive-2.0.0/website/src/app/App.tsx" in names
        assert "tgdrive-2.0.0/website/package-lock.json" in names
        assert "tgdrive-2.0.0/website/scripts/patch-movi-player.js" in names
        for name in brand_assets:
            assert f"tgdrive-2.0.0/{name}" in names
        for public_asset in (
            "docs/backup-recovery.md",
            "docs/configuration.md",
            "docs/performance-tuning.md",
            "docs/assets/README.md",
            "docs/assets/drive-overview.jpg",
            "docs/assets/torrent-import.jpg",
            "docs/assets/video-continue.jpg",
            "docs/assets/mobile-drive.jpg",
            "docs/assets/current-features.mp4",
            "website/public/third-party/ui-components-LICENSES.txt",
            "website/public/third-party/README.txt",
            "website/public/third-party/movi-player-LICENSE.txt",
            "website/public/third-party/movi-player-TGDrive-patch-NOTICE.txt",
            "website/public/third-party/movi-player-0.4.1-sources.tar.gz",
            "website/public/third-party/FFmpeg-LGPL-2.1.txt",
            "website/public/third-party/pdfjs-LICENSE.txt",
            "website/public/third-party/pdfjs-TGDrive-patch-NOTICE.txt",
            "website/vendor/README.md",
            "website/vendor/manifest.json",
            "website/vendor/movi-player-0.4.1-tgdrive.3.tgz",
            "website/vendor/pdfjs-dist-6.4.299-tgdrive.1.tgz",
            "website/public/third-party/react-LICENSE.txt",
            "website/public/third-party/react-dom-LICENSE.txt",
            "website/public/third-party/scheduler-LICENSE.txt",
            "website/public/third-party/lucide-react-LICENSE.txt",
            "website/public/third-party/octicons-MIT.txt",
            "tests/fixtures/multitrack.mkv",
        ):
            assert f"tgdrive-2.0.0/{public_asset}" in names
        for name in private_names:
            assert f"tgdrive-2.0.0/{name}" not in names
        assert all(b"PRIVATE-CONTENT" not in archive.read(name) for name in names)
        manifest = json.loads(archive.read("tgdrive-2.0.0/release-manifest.json"))
        for name, digest in manifest["files"].items():
            assert (
                hashlib.sha256(archive.read(f"tgdrive-2.0.0/{name}")).hexdigest()
                == digest
            )
    assert (
        checksum_path.read_text().split()[0]
        == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    )


def test_source_archive_is_reproducible(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _source_tree(source)
    first, _ = build_release(source, tmp_path / "first")
    second, _ = build_release(source, tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()


def test_source_archive_rejects_symlinked_sources(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _source_tree(source)
    outside = tmp_path / "private.py"
    outside.write_text("private credentials", encoding="utf-8")
    (source / "tgdrive" / "linked.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        build_release(source, tmp_path / "output")
