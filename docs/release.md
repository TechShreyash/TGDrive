# TGDrive v2 release verification

[Docs home](README.md) · [Setup](setup.md) · [Development](development.md)

TGDrive **2.0.0** targets a fresh MongoDB database and dedicated private Telegram
channel. MongoDB is required on laptops and servers; local recovery journals do
not replace it. Use Docker Compose for all deployments.

The public source includes the [visual walkthroughs](showcase.md) and their
[media assets](assets/README.md). Keep those files in the release allowlist when
replacing captures.

For cloning or optional source downloads, use [Get the source](setup.md#get-the-source).
The procedures below describe preparing a versioned release and optionally
building a downloadable archive. Cloning is sufficient to install TGDrive.

**Contents**

- [Choose the deployment](#choose-the-deployment)
- [Version every published update](#version-every-published-update)
- [Prepare the source release](#prepare-the-source-release)
- [Verify a downloaded source archive](#verify-a-downloaded-source-archive)
- [Fresh-install smoke checks](#fresh-install-smoke-checks)

## Choose the deployment

| Deployment | Runtime | Guide |
| --- | --- | --- |
| On-demand laptop | `compose.yaml` with automatically loaded `compose.override.yaml` | [Windows/macOS/Linux Docker](setup.md#4-start-locally) |
| Coolify server | `compose.yaml` selected explicitly | [Coolify](setup.md#coolify) |
| Server with an HTTPS proxy you manage | `compose.yaml` plus `compose.server.yaml` | [Server Compose](setup.md#docker-compose-on-a-server) |

Follow [Setup](setup.md) for credentials, permissions and first initialization.
Restarts and reinstalls must reuse the database, deployment identity, secrets
and persistent storage. See [Operations](operations.md) for recovery.

## Version every published update

Every update published to `main` gets a new semantic version and a matching
GitHub release, starting with **2.0.0** and tag **`v2.0.0`**. Development commits
can accumulate on `dev` for one upcoming release; they do not each need a tag.
`main` receives stable updates after release review and required checks. `dev`
contains ongoing development and may be unstable; do not use it for a stable
deployment. Prefer a published tag to reproduce an exact release.
See the [release CI checks](development.md#when-github-checks-run) for `main`.

| Change | Next version after `2.0.0` |
| --- | --- |
| Compatible fixes, documentation or maintenance | `2.0.1` |
| Compatible features | `2.1.0` |
| Breaking changes | `3.0.0` |

`pyproject.toml` is the version authority. Keep `tgdrive/__init__.py`, the
frontend `package.json` and both root version entries in `package-lock.json`,
the TGDrive project entry in `uv.lock`, and the Docker image version label in
sync. The release builder rejects mismatched versions before creating an
archive; CI checks the same metadata. To inspect it without building:

```bash
python3 scripts/build_release.py --check-version
```

Name the target version in the release PR, describe the changes and compatibility
requirements, and link the actual validation results. After merging and checking
the final `main` commit, create an **annotated** `vX.Y.Z` tag on that commit.
Never tag an unmerged `dev` revision as the final release. A published tag is
immutable: fix a release with a new version instead of moving its tag.
User-facing GitHub documentation links point to `main`; release download links
pin the corresponding tag. Reserve `dev` links for contributor instructions.

The matching GitHub release must link its PR and exact commit (and the comparison
with the previous tag for later releases) and explain upgrade requirements and
known limits. Git users can clone the tag; no ZIP is required. If custom source
assets are offered, attach the ZIP and SHA-256 file from that revision together.
Follow the checks below before publishing. Creating a PR or tag does not publish
custom assets, and repository approval rules still apply to checks and pushes.

## Prepare the source release

Prepare a clean, reviewed commit and its matching tag. If distributing a custom
source ZIP, build and validate it from the same revision. Resolve local work
without resetting or discarding changes. Building an artifact does not publish it.

<details>
<summary>Maintainer checklist: build, validate and publish</summary>

Run these from the release checkout, with the development prerequisites from
[Development](development.md).

- [ ] Choose the release version using the policy above and synchronize all
   version metadata. For this initial release, use `2.0.0`. Review the user
   guides against the implemented settings and defaults.
- [ ] In **Actions → CI → Run workflow**, select the release branch and suite
   **`all`** against the exact release commit. Review quality/security,
   Python, frontend, Chromium, container and source-release results. Use the
   isolated test database described in Development; never run destructive tests
   against an operator's drive.
- [ ] If offering custom source assets, build the public source archive:

```bash
python3 scripts/build_release.py
(cd dist && sha256sum --check tgdrive-2.0.0.zip.sha256)
python3 -m zipfile -t dist/tgdrive-2.0.0.zip
```

The builder includes reviewed source, public documentation and a file-hash
manifest. Runtime data, credentials, caches and installed dependencies are
excluded. Review the generated manifest and archive before distribution.

- [ ] For custom source assets, extract into a clean directory. Repeat dependency installation,
   frontend build, relevant tests and Docker build from the extraction using
   [the documented commands](development.md). Confirm all three Compose modes
   resolve and that no private `.env` or runtime data enters the image.
- [ ] Perform the fresh-install smoke checks below from a clean clone of the
   release revision, or the extracted archive if distributing one. Record the
   commit, commands, environment and results (and checksum for an archive).
- [ ] After those checks, create the annotated tag on the final merged `main`
   commit. For the initial release (substitute the next version for later ones):

```bash
git switch main
git pull --ff-only origin main
python3 scripts/build_release.py --check-version --tag v2.0.0
git tag -a v2.0.0 -m "Release TGDrive 2.0.0"
```

- [ ] With approval for the specific tag push, push `v2.0.0` to `origin` and
   create its GitHub release. Attach matching ZIP and `.sha256` files together
   only if offering custom source assets.
   Tag-push CI also checks that the tag matches the metadata. Coolify users must
   select that same revision. Record changes and limitations in the release's
   description and Git commits.

</details>

## Verify a downloaded source archive

For a published **2.0.0** release, download its **tgdrive-2.0.0.zip** and
**tgdrive-2.0.0.zip.sha256** assets into the same folder. These commands verify
the custom release assets; the development ZIP from GitHub does not have this
companion checksum. Open a terminal in that download folder.

**Windows PowerShell:**

```powershell
$expectedHash = (Get-Content .\tgdrive-2.0.0.zip.sha256 -Raw).Split(' ')[0]
if ((Get-FileHash .\tgdrive-2.0.0.zip -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'Checksum mismatch. Download the release files again.'
}
Expand-Archive .\tgdrive-2.0.0.zip -DestinationPath .
Set-Location .\tgdrive-2.0.0
```

**macOS Terminal:**

```bash
shasum -a 256 -c tgdrive-2.0.0.zip.sha256
```

**Linux/WSL terminal:**

```bash
sha256sum --check tgdrive-2.0.0.zip.sha256
```

On macOS/Linux/WSL, continue **only after the checksum reports OK**, then extract:

```bash
unzip tgdrive-2.0.0.zip
cd tgdrive-2.0.0
```

Your file manager can extract the ZIP if `unzip` is unavailable.
Continue with [Setup](setup.md). A checksum verifies the download against its
companion file; obtain both from the trusted project release.

## Fresh-install smoke checks

Use disposable databases, channels and files. Keep credentials and private
evidence out of public docs and reports.

<details>
<summary>Checks with disposable databases, channels and files</summary>

- [ ] **Local Docker:** on Windows, macOS and Linux, confirm automatic first setup, sign in on
  localhost, upload/download a small file, stop/start, and reopen it. Check that
  configuration and volumes persist. These are separate platform checks.
- [ ] **Hosted:** verify HTTPS login, private-file denial without authentication,
  a scoped share and revocation. Confirm the direct app port is not public.
- [ ] **Transfers:** check a split file's original checksum, an interrupted upload,
  retry/reselection guidance, and server processing after browser acceptance.
- [ ] **Channel protection:** verify required bot permissions and that public or
  auto-delete settings produce an actionable error. Restore the correct state.
- [ ] **Recovery:** confirm an encrypted database backup, save its matching key,
  and restore into a separate empty database. If independent channel protection
  is enabled, verify its completed restore point and recovery flow separately.

Automated tests and synthetic browser checks do not certify real Telegram,
Coolify, DNS/TLS, every codec/device or sustained throughput. See the
[dated evidence](development.md#ci-and-release-evidence) and
[validation boundaries](roadmap.md#before-public-release). A documented release
procedure is not a claim that publication or all live checks have occurred.

</details>
