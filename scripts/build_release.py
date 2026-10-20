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
    ".env.example",
    ".gitattributes",
    ".gitignore",
    ".python-version",
    "CHANGELOG.md",
    "Dockerfile",
    "LICENSE",
    "README.md",
    "compose.yaml",
    "compose.override.yaml",
    "compose.coolify.yaml",
    "config.py",
    "docker_start.sh",
    "main.py",
    "pyproject.toml",
    "render.yaml",
    "uv.lock",
    "website/package.json",
    "website/package-lock.json",
    "website/public/third-party/README.txt",
    "website/public/third-party/movi-player-LICENSE.txt",
    "website/public/third-party/movi-player-TGDrive-patch-NOTICE.txt",
    "website/public/third-party/movi-player-0.4.1-sources.tar.gz",
    "website/public/third-party/FFmpeg-LGPL-2.1.txt",
    "website/public/third-party/pdfjs-LICENSE.txt",
    "website/public/third-party/react-LICENSE.txt",
    "website/public/third-party/react-dom-LICENSE.txt",
    "website/public/third-party/scheduler-LICENSE.txt",
    "website/public/third-party/lucide-react-LICENSE.txt",
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
        ".woff2",
    },
}
PUBLIC_DOCS = (
    "docs/README.md",
    "docs/setup.md",
    "docs/usage.md",
    "docs/architecture.md",
    "docs/operations.md",
    "docs/backup-recovery.md",
    "docs/configuration.md",
    "docs/development.md",
    "docs/roadmap.md",
    "docs/reference.md",
    "docs/performance.md",
    "docs/streaming-validation-2026-10-18.md",
    "website/PRODUCT.md",
    "website/DESIGN.md",
)

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
EXCLUDED_PARTS = {
    "__pycache__",
    "node_modules",
    "dist",
    "coverage",
    ".vite",
    ".impeccable",
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
    permissions = 0o100755 if name.endswith("/docker_start.sh") else 0o100644
    entry.external_attr = permissions << 16
    archive.writestr(
        entry, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
    )


def build_release(root: Path, output_dir: Path) -> tuple[Path, Path]:
    root = root.resolve()
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Release version must use major.minor.patch format")
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
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    archive, checksum = build_release(root, args.output_dir.resolve())
    print(f"Source archive: {archive}")
    print(f"SHA-256 file: {checksum}")


if __name__ == "__main__":
    main()
