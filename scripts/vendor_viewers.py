"""Regenerate TGDrive's reviewed viewer packages; never used by normal builds.

Run explicitly after reviewing the upstream identities and JavaScript recipes.
The complete upstream packages, licenses, declarations and exports are retained.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import io
import json
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 128 * 1024**2
MAX_PACKAGE_BYTES = 512 * 1024**2
MAX_MEMBERS = 10_000


@dataclass(frozen=True)
class Viewer:
    name: str
    version: str
    integrity: str
    target: str
    recipe: str
    patch: str
    revision: int = 1
    recipe_support: tuple[str, ...] = ()

    @property
    def local_version(self) -> str:
        return f"{self.version}-tgdrive.{self.revision}"

    @property
    def upstream_filename(self) -> str:
        return f"{self.name}-{self.version}.tgz"

    @property
    def upstream_url(self) -> str:
        return f"https://registry.npmjs.org/{self.name}/-/{self.upstream_filename}"

    @property
    def filename(self) -> str:
        return f"{self.name}-{self.local_version}.tgz"


VIEWERS = (
    Viewer(
        name="movi-player",
        version="0.4.1",
        integrity=(
            "sha512-oA91s8nxyt7tnd7SaZNKVkjkz8UeHVrcZNxWQSBo5Jj/"
            "MOog7pnGtwv0edimjwocJkZVBBtBMy8Ef4HOGzBJGQ=="
        ),
        target="dist/element.slim.js",
        recipe="website/scripts/patch-movi-player.js",
        patch="patchMoviSource",
        revision=3,
        recipe_support=(
            "website/scripts/movi-player-hardening.js",
            "website/scripts/movi-player-decoding.js",
            "website/scripts/movi-player-preview-cache.js",
        ),
    ),
    Viewer(
        name="pdfjs-dist",
        version="6.4.299",
        integrity=(
            "sha512-AVl138zALtfaAPvADulE0PZThbYzCBS79nL4pOSL/"
            "6Sm/4AH5A21BD9VHt97OlCuzJuCpmeZtAtkinisF4Vb1g=="
        ),
        target="build/pdf.worker.mjs",
        recipe="website/scripts/patch-pdf-worker.js",
        patch="patchPdfSource",
    ),
)


@dataclass(frozen=True)
class Entry:
    content: bytes | None
    mode: int


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def integrity(content: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode(
        "ascii"
    )


def upstream_bytes(viewer: Viewer, source_dir: Path | None) -> bytes:
    if source_dir is not None:
        content = (source_dir / viewer.upstream_filename).read_bytes()
    else:
        # Fixed HTTPS npm identities above; no user-supplied URL or npm hooks.
        with urllib.request.urlopen(viewer.upstream_url, timeout=60) as response:  # noqa: S310
            content = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"Upstream archive is too large: {viewer.name}")
    if not hmac.compare_digest(integrity(content), viewer.integrity):
        raise ValueError(f"Upstream integrity mismatch: {viewer.name}")
    return content


def package_entries(content: bytes) -> dict[str, Entry]:
    """Read regular npm files in memory; never extract paths or follow links."""
    entries: dict[str, Entry] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(content), mode="r|gz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            parts = name.split("/")
            if (
                len(entries) >= MAX_MEMBERS
                or not parts
                or parts[0] != "package"
                or any(part in {"", ".", ".."} for part in parts)
                or "\\" in name
                or ":" in name
                or PurePosixPath(name).is_absolute()
                or name in entries
            ):
                raise ValueError(f"Unsafe or duplicate npm archive path: {member.name}")
            if member.isdir():
                entries[name] = Entry(None, 0o755)
                continue
            if not member.isfile() or member.size < 0:
                raise ValueError(f"Unsupported npm archive member: {member.name}")
            total += member.size
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("Unpacked viewer package exceeds the size limit")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"Unreadable npm archive member: {member.name}")
            with handle:
                data = handle.read(member.size + 1)
            if len(data) != member.size:
                raise ValueError(f"Truncated npm archive member: {member.name}")
            entries[name] = Entry(data, 0o755 if member.mode & 0o111 else 0o644)
    if "package/package.json" not in entries:
        raise ValueError("Npm package metadata is missing")
    return entries


NODE_PATCH = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const input = JSON.parse(readFileSync(0, "utf8"));
const recipe = await import(pathToFileURL(input.recipe).href);
process.stdout.write(recipe[input.patch](input.source, input.version));
"""


def patched_source(viewer: Viewer, content: bytes, root: Path, node: str) -> bytes:
    request = {
        "recipe": str(root / viewer.recipe),
        "patch": viewer.patch,
        "source": content.decode("utf-8"),
        "version": viewer.version,
    }
    result = subprocess.run(  # noqa: S603 - explicit local maintenance recipe
        [node, "--input-type=module", "--eval", NODE_PATCH],
        input=json.dumps(request),
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return result.stdout.encode("utf-8")


def modification_notice(viewer: Viewer) -> bytes:
    return (
        f"TGDrive modifications to {viewer.name} {viewer.version}\n"
        f"Local package version: {viewer.local_version}\n\n"
        f"TGDrive changed {viewer.target} using the reviewed recipe\n"
        f"{viewer.recipe}. The recipe records the original source identity\n"
        "and the viewer corrections applied to that file. The modified file\n"
        "also carries a TGDrive change notice.\n\n"
        "The upstream package.json version was given the local suffix above.\n"
        "All other upstream package files, licenses, attribution, declarations\n"
        "and exported entrypoints are retained unchanged.\n\n"
        f"Upstream package: {viewer.upstream_url}\n"
        f"Upstream integrity: {viewer.integrity}\n\n"
        "See website/vendor/manifest.json for upstream and local content hashes.\n"
        "Movi decoder source/relink materials and full LGPL notice accompany\n"
        "TGDrive under website/public/third-party/.\n"
    ).encode()


def deterministic_archive(entries: dict[str, Entry]) -> bytes:
    """Normalize ordering, permissions, owner identity and archive timestamps."""
    contents = dict(entries)
    for name in tuple(contents):
        for parent in PurePosixPath(name).parents:
            if parent.as_posix() != ".":
                contents.setdefault(parent.as_posix(), Entry(None, 0o755))
    output = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(
            fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
        ) as archive,
    ):
        for name, entry in sorted(contents.items()):
            header = tarfile.TarInfo(name)
            header.mode = entry.mode
            header.mtime = 0
            header.uid = header.gid = 0
            header.uname = header.gname = ""
            header.type = tarfile.DIRTYPE if entry.content is None else tarfile.REGTYPE
            header.size = len(entry.content) if entry.content is not None else 0
            archive.addfile(
                header,
                io.BytesIO(entry.content) if entry.content is not None else None,
            )
    return output.getvalue()


def generate(root: Path, output_dir: Path, node: str, source_dir: Path | None) -> dict:
    manifest: dict = {"schema_version": 1, "packages": {}}
    generated: dict[str, bytes] = {}
    for viewer in VIEWERS:
        upstream = upstream_bytes(viewer, source_dir)
        entries = package_entries(upstream)
        metadata_entry = entries["package/package.json"]
        metadata = json.loads(metadata_entry.content)
        if metadata["name"] != viewer.name or metadata["version"] != viewer.version:
            raise ValueError(f"Unexpected upstream package metadata: {viewer.name}")
        target = "package/" + viewer.target
        original = entries[target].content
        if original is None:
            raise ValueError(f"Expected regular viewer runtime file: {target}")
        patched = patched_source(viewer, original, root, node)
        entries[target] = Entry(patched, entries[target].mode)
        metadata["version"] = viewer.local_version
        package_json = (
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"
        ).encode()
        entries["package/package.json"] = Entry(package_json, metadata_entry.mode)
        entries["package/TGDRIVE-NOTICE.txt"] = Entry(
            modification_notice(viewer), 0o644
        )
        files = {
            name.removeprefix("package/"): sha256(entry.content)
            for name, entry in sorted(entries.items())
            if entry.content is not None
        }
        archive = deterministic_archive(entries)
        recipe_files = {
            path: sha256((root / path).read_bytes())
            for path in (viewer.recipe, *viewer.recipe_support)
        }
        generated[viewer.filename] = archive
        manifest["packages"][viewer.name] = {
            "upstream": {
                "version": viewer.version,
                "url": viewer.upstream_url,
                "integrity": viewer.integrity,
                "sha256": sha256(upstream),
                "bytes": len(upstream),
            },
            "vendored": {
                "version": viewer.local_version,
                "archive": viewer.filename,
                "integrity": integrity(archive),
                "sha256": sha256(archive),
                "bytes": len(archive),
                "package_json_sha256": sha256(package_json),
                "package_files_sha256": sha256(
                    json.dumps(files, sort_keys=True).encode()
                ),
                "file_count": len(files),
            },
            "patch": {
                "file": viewer.target,
                "upstream_sha256": sha256(original),
                "vendored_sha256": sha256(patched),
                "recipe": viewer.recipe,
                "recipe_sha256": (
                    sha256(json.dumps(recipe_files, sort_keys=True).encode())
                    if viewer.recipe_support
                    else recipe_files[viewer.recipe]
                ),
                "recipe_files_sha256": recipe_files,
            },
            "files_sha256": files,
        }
    generated["manifest.json"] = (json.dumps(manifest, indent=4) + "\n").encode()
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in generated.items():
        (output_dir / name).write_bytes(content)
    return manifest


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node", help="Supported Node.js executable")
    parser.add_argument("--output-dir", type=Path, default=root / "website/vendor")
    parser.add_argument(
        "--upstream-dir",
        type=Path,
        help="Read exact upstream .tgz files here instead of downloading them",
    )
    args = parser.parse_args()
    manifest = generate(root, args.output_dir, args.node, args.upstream_dir)
    for name, package in manifest["packages"].items():
        local = package["vendored"]
        print(f"{name}: {local['archive']} ({local['bytes']:,} bytes)")


if __name__ == "__main__":
    main()
