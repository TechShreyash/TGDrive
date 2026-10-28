"""Build a reproducible public source ZIP without local credentials or state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
import zipfile
from pathlib import Path

ROOT_FILES = (
    ".dockerignore",
    ".editorconfig",
    ".env.example",
    ".gitattributes",
    ".gitignore",
    ".python-version",
    ".vscode/settings.json",
    "AGENTS.md",
    "Dockerfile",
    "LICENSE",
    "README.md",
    "compose.yaml",
    "compose.override.yaml",
    "compose.server.yaml",
    "pyproject.toml",
    "uv.lock",
    "website/package.json",
    "website/package-lock.json",
    "website/vendor/README.md",
    "website/vendor/manifest.json",
    "website/vendor/movi-player-0.4.1-tgdrive.3.tgz",
    "website/vendor/pdfjs-dist-6.4.299-tgdrive.1.tgz",
    "website/public/third-party/README.txt",
    "website/public/third-party/movi-player-LICENSE.txt",
    "website/public/third-party/movi-player-TGDrive-patch-NOTICE.txt",
    "website/public/third-party/movi-player-0.4.1-sources.tar.gz",
    "website/public/third-party/FFmpeg-LGPL-2.1.txt",
    "website/public/third-party/pdfjs-LICENSE.txt",
    "website/public/third-party/pdfjs-TGDrive-patch-NOTICE.txt",
    "website/public/third-party/react-LICENSE.txt",
    "website/public/third-party/react-dom-LICENSE.txt",
    "website/public/third-party/scheduler-LICENSE.txt",
    "website/public/third-party/lucide-react-LICENSE.txt",
    "website/public/third-party/octicons-MIT.txt",
    "website/public/third-party/Inter-OFL-1.1.txt",
    "website/public/third-party/ui-components-LICENSES.txt",
    "website/public/static/fonts/InterVariable.woff2",
    "tests/fixtures/multitrack.mkv",
    "tests/fixtures/movtext-subtitles.mp4",
    "tests/fixtures/software-mpeg4.wmv",
    "tests/fixtures/unsupported-msmpeg4.wmv",
)
SOURCE_TREES = {
    ".github": {".yaml", ".yml"},
    "scripts": {".py"},
    "tests": {".py", ".cjs"},
    "tgdrive": {".py"},
    "website": {
        ".html",
        ".css",
        ".js",
        ".cjs",
        ".ts",
        ".tsx",
        ".json",
        ".svg",
        ".png",
        ".ico",
        ".woff2",
    },
}
PUBLIC_DOCS = (
    "docs/README.md",
    "docs/new-user-guide.md",
    "docs/showcase.md",
    "docs/setup.md",
    "docs/hosting.md",
    "docs/release.md",
    "docs/usage.md",
    "docs/reading.md",
    "docs/thumbnails.md",
    "docs/architecture.md",
    "docs/operations.md",
    "docs/backup-recovery.md",
    "docs/configuration.md",
    "docs/development.md",
    "docs/roadmap.md",
    "docs/reference.md",
    "docs/performance.md",
    "docs/performance-tuning.md",
    "docs/streaming-validation-2026-10-18.md",
    "docs/assets/README.md",
    "docs/assets/archive-result.jpg",
    "docs/assets/audio-preview.jpg",
    "docs/assets/cache-settings.jpg",
    "docs/assets/comic-reader.jpg",
    "docs/assets/create-zip.jpg",
    "docs/assets/drive-list.jpg",
    "docs/assets/drive-overview.jpg",
    "docs/assets/epub-reader.jpg",
    "docs/assets/extract-archive.jpg",
    "docs/assets/file-actions.jpg",
    "docs/assets/image-preview.jpg",
    "docs/assets/media-preview.jpg",
    "docs/assets/mobile-drive.jpg",
    "docs/assets/new-menu.jpg",
    "docs/assets/pdf-reader.jpg",
    "docs/assets/performance-overview.jpg",
    "docs/assets/performance-resources.jpg",
    "docs/assets/preloads.jpg",
    "docs/assets/recovery-file.jpg",
    "docs/assets/thumbnail-rebuild.jpg",
    "docs/assets/torrent-import.jpg",
    "docs/assets/transfer-settings.jpg",
    "docs/assets/url-import.jpg",
    "docs/assets/video-continue.jpg",
    "docs/assets/video-settings.jpg",
    "docs/assets/current-features.mp4",
)

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
EXCLUDED_PARTS = {
    "__pycache__",
    "node_modules",
    "dist",
    "coverage",
    ".vite",
    ".impeccable",
    ".agents",
    ".codex",
}


def source_files(root: Path) -> list[Path]:
    """Select only reviewed source locations; never traverse runtime folders."""
    selected: list[Path] = []
    for name in (*ROOT_FILES, *PUBLIC_DOCS):
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"Missing required release source: {name}")
        selected.append(path)
    for directory, suffixes in SOURCE_TREES.items():
        base = root / directory
        if not base.is_dir():
            raise FileNotFoundError(f"Missing required source directory: {directory}")
        for path in base.rglob("*"):
            if (
                EXCLUDED_PARTS.intersection(path.relative_to(base).parts)
                or path.name in {"skills-lock.json", "request-tracker.md"}
                or path.suffix not in suffixes
            ):
                continue
            if path.is_file():
                selected.append(path)
    for path in selected:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Release source cannot be a symlink: {path}")
        if any(parent.is_symlink() for parent in path.parents if parent != root):
            raise ValueError(f"Release source parent cannot be a symlink: {path}")
    return sorted(selected, key=lambda path: path.relative_to(root).as_posix())


def _write_entry(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    entry = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    archive.writestr(
        entry, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
    )


def check_version(root: Path, tag: str | None = None) -> str:
    """Reject release metadata drift before CI or archive creation."""
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}", version
    ):
        raise ValueError("Release version must use major.minor.patch format")
    package = json.loads((root / "website/package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "website/package-lock.json").read_text(encoding="utf-8"))
    lock_root = lock.get("packages", {}).get("", {})
    uv_lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    versions = {
        "website/package.json": package.get("version"),
        "website/package-lock.json version": lock.get("version"),
        "website/package-lock.json root package": lock_root.get("version"),
        "uv.lock local project": next(
            (
                package.get("version")
                for package in uv_lock.get("package", [])
                if package.get("name") == metadata["project"]["name"]
                and package.get("source") == {"virtual": "."}
            ),
            None,
        ),
    }
    for name, pattern in (
        ("tgdrive/__init__.py", r'^__version__\s*=\s*"([^"]+)"\s*$'),
        ("Dockerfile", r'org\.opencontainers\.image\.version="([^"]+)"'),
    ):
        match = re.search(pattern, (root / name).read_text(encoding="utf-8"), re.M)
        versions[name] = match.group(1) if match else None
    for name, actual in versions.items():
        if actual != version:
            raise ValueError(
                f"Release version mismatch: {name} is {actual!r}, expected {version!r}"
            )
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Release tag must be v{version}, got {tag!r}")
    return version


def build_release(
    root: Path, output_dir: Path, tag: str | None = None
) -> tuple[Path, Path]:
    root = root.resolve()
    version = check_version(root, tag)
    prefix = f"tgdrive-{version}"
    contents = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in source_files(root)
    }
    manifest = {
        "project": "TGDrive",
        "version": version,
        "files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in contents.items()
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{prefix}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, content in contents.items():
            _write_entry(archive, f"{prefix}/{name}", content)
        _write_entry(
            archive,
            f"{prefix}/release-manifest.json",
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        )
    checksum_path = output_dir / f"{prefix}.zip.sha256"
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    return archive_path, checksum_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--check-version", action="store_true")
    parser.add_argument("--tag", help="Require the release tag to match v<version>")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.check_version:
        print(f"Release version: {check_version(root, args.tag)}")
        return
    archive, checksum = build_release(root, args.output_dir.resolve(), args.tag)
    print(f"Source archive: {archive}")
    print(f"SHA-256 file: {checksum}")


if __name__ == "__main__":
    main()
