# TGDrive v2 release verification

[Docs home](README.md) · [Setup](setup.md) · [Development](development.md)

TGDrive **2.0.0** targets a fresh MongoDB database and dedicated private Telegram
channel. MongoDB is required on laptops and servers; local recovery journals do
not replace it. Use Docker Compose for all deployments.

[Install a release](#choose-the-deployment) · [Check a download](#verify-a-downloaded-source-archive) · [Prepare a release](#prepare-the-source-release)

## Choose the deployment

| Deployment | Runtime | Guide |
| --- | --- | --- |
| On-demand laptop | `compose.yaml` with automatically loaded `compose.override.yaml` | [Windows/macOS/Linux Docker](setup.md#4-start-locally) |
| Coolify server | `compose.yaml` selected explicitly | [Coolify](setup.md#coolify) |
| Server with an HTTPS proxy you manage | `compose.yaml` plus `compose.server.yaml` | [Server Compose](setup.md#docker-compose-on-a-server) |

Follow [Setup](setup.md) for credentials, permissions and first initialization.
Restarts and reinstalls must reuse the database, deployment identity, secrets
and persistent storage. See [Operations](operations.md) for recovery.

## Prepare the source release

Build from a clean, reviewed commit, validate the extracted archive, then publish
the matching tag with its ZIP and checksum. Resolve local work without resetting
or discarding changes. Building an artifact does not publish it.

<details>
<summary>Maintainer checklist: build, validate and publish</summary>

Run these from the release checkout, with the development prerequisites from
[Development](development.md).

- [ ] Confirm `2.0.0` in `pyproject.toml`, the frontend
   package metadata and Docker image label. Review the user guides against the
   implemented settings and defaults.
- [ ] Run CI against the exact release commit. Review quality/security,
   Python, frontend, Chromium, container and source-release results. Use the
   isolated test database described in Development; never run destructive tests
   against an operator's drive.
- [ ] Build the public source archive:

```bash
python3 scripts/build_release.py
(cd dist && sha256sum --check tgdrive-2.0.0.zip.sha256)
python3 -m zipfile -t dist/tgdrive-2.0.0.zip
```

The builder includes reviewed source, public documentation and a file-hash
manifest. Runtime data, credentials, caches and installed dependencies are
excluded. Review the generated manifest and archive before distribution.

- [ ] Extract into a clean directory. Repeat dependency installation,
   frontend build, relevant tests and Docker build from the extraction using
   [the documented commands](development.md). Confirm all three Compose modes
   resolve and that no private `.env` or runtime data enters the image.
- [ ] Perform the fresh-install smoke checks below using the extracted source.
   Record the commit, archive checksum, commands, environment and results.
- [ ] After those checks, publish the matching tag/revision and attach the ZIP
   and `.sha256` file together. Coolify users must select that same v2 revision.
   Record changes and limitations in the release's description and Git commits.

</details>

## Verify a downloaded source archive

Download the release's **tgdrive-2.0.0.zip** and **tgdrive-2.0.0.zip.sha256**
assets into the same folder. Use the custom release assets, rather than GitHub's
automatic source-code ZIP. Open a terminal in that download folder.

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

- [ ] **Local Docker:** on Windows, macOS and Linux, initialize once, sign in on
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
