# TGDrive viewer packages

The frontend installs these patched npm archives from pre-release GitHub
releases on this repository, pinned by URL and lockfile integrity. Normal
installs, development, tests and builds do not patch `node_modules`. The
archives are not committed; `*.tgz` here is gitignored.

| Package | Release tag | Assets |
| --- | --- | --- |
| `movi-player` | `viewer-movi-player-0.4.1-tgdrive.3` | `movi-player-0.4.1-tgdrive.3.tgz`, `movi-player-0.4.1-sources.tar.gz` |
| `pdfjs-dist` | `viewer-pdfjs-dist-6.4.299-tgdrive.1` | `pdfjs-dist-6.4.299-tgdrive.1.tgz` |

Each archive contains the complete exact upstream package, including all exports,
type declarations and licenses. TGDrive changes one reviewed JavaScript runtime
file, adds `TGDRIVE-NOTICE.txt` and gives the package a local version suffix. The
decoder WASM is unchanged. `manifest.json` records upstream integrity, local
archive integrity, individual file hashes and the patch recipe identity.

## Update deliberately

1. Review the pinned upstream URLs/integrities in `scripts/vendor_viewers.py` and
   the recipes in `website/scripts/patch-movi-player.js`, its hardening/decoding
   support files, and `patch-pdf-worker.js`.
2. From the repository root, use supported Node.js 24.22:

   ```bash
   python scripts/vendor_viewers.py --node node
   ```

   The generator downloads only those exact upstream archives and verifies their
   SHA-512 integrity before reading members. `--upstream-dir PATH` uses already
   downloaded archives with the same integrity requirement. `--output-dir PATH`
   produces candidates outside this directory for comparison.
3. Review the archive/manifest diff. A changed archive needs a new local
   version suffix; never replace a published asset.
4. Create a GitHub pre-release tagged `viewer-<package>-<version>` that is not
   marked latest, and upload the archive (plus
   `website/public/third-party/movi-player-<version>-sources.tar.gz` for Movi):

   ```bash
   gh release create viewer-pdfjs-dist-6.4.299-tgdrive.1 --prerelease --latest=false \
     --title "Viewer: pdfjs-dist 6.4.299-tgdrive.1" --notes "Patched viewer package" \
     website/vendor/pdfjs-dist-6.4.299-tgdrive.1.tgz
   ```

5. Point `website/package.json` and both `website/package-lock.json` entries at
   `https://github.com/TechShreyash/TGDrive/releases/download/<tag>/<archive>`
   with the manifest's `vendored.integrity`, update the source-archive link in
   `website/public/third-party/README.txt`, and run the focused viewer checks.

Archive order, ownership, permissions and gzip/tar timestamps are normalized.
The same inputs and Python/zlib runtime reproduce the same bytes; the manifest
contains no generation timestamps or machine-specific paths. No upstream install
or package lifecycle scripts run during generation.

Movi revision `0.4.1-tgdrive.3` includes metadata escaping, complete element
disconnect cleanup, audio-only buffering and software end-of-file corrections,
decoded-picture memory targets, and demand-driven previews. Preview bytes can
be reused by playback through a bounded cache, with a 16 MiB requested-range
allowance per newly generated image; speculative read-ahead does not
learn a bitrate from pause/seek cursor jumps, and pause-time packet targets keep
only a small resume cushion. The manifest hashes
the main recipe and each support file. A new local revision changes the archive
filename and lockfile integrity; the original published WASM remains unchanged.

Preserve `website/public/third-party/` in runtime releases. It contains the public
licenses and links to the Movi decoder source/relink archive release asset. Vendoring the JavaScript does not
replace those materials. PDF.js resource licenses remain in its complete package; preserve each relevant
license alongside the CMaps, fonts, color profiles and decoder assets emitted by
the frontend build.
