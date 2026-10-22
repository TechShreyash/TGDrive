# Develop and release TGDrive

[Docs home](README.md) · [Architecture](architecture.md) · [API](reference.md#http-api)

For contributors. To install the app for personal use, start with [Setup](setup.md).

Use the Linux checkout and tools. For this project's Windows workflow, run
builds, tests and local servers **inside WSL Ubuntu**, rather than Windows Python/npm.

## Install the development environment

~~~bash
uv sync --frozen --all-groups --python 3.14.8
npm ci --prefix website
npm run build --prefix website
~~~

Use the lockfiles. Ordinary tests inject fake resources and do not need your
live Telegram credentials.

## Run while editing

With private configuration and an existing drive:

~~~bash
COOKIE_SECURE=false uv run --frozen python -m uvicorn main:app \
  --host 127.0.0.1 --port 8000 --workers 1 \
  --timeout-graceful-shutdown 45 --no-access-log
~~~

In a second WSL terminal:

~~~bash
npm run dev --prefix website
~~~

- Vite normally opens port **5173** and proxies the API/media to **8000**.
- A production UI build is served from port 8000.
- Do not start a second owner of the same drive.
- Avoid automatic reload during real uploads unless testing interruption recovery.

## Preview with sample content

Build the website, then run the isolated fixture inside WSL:

~~~bash
npm run build --prefix website
TGDRIVE_UI_SHOWCASE=1 uv run --frozen --group e2e python -m uvicorn \
  --app-dir tests browser_transfer_app:app --host 0.0.0.0 --port 18767
~~~

Open [localhost:18767](http://localhost:18767/) and sign in with
`offline browser test password`. This disposable preview includes transfers,
files, shares, trash, complete/partial caches and illustrative performance data.
It also seeds **Reading samples** with generated EPUB, CBZ and CBR files and two
independent labelled links. CBR requires the native libarchive runtime, included
in the Docker image; see [reader setup and limits](reading.md).
It uses temporary stores and no real Telegram credentials. Restarting resets the
sample content. Ordinary browser tests use a 32 MiB cap; showcase mode uses the production
16 GiB default. Owners can adjust it in Transfer limits.
Never deploy this fixture or its `/__test__` endpoints publicly.

## Latest focused validation

The subsequent UI/upload-admission follow-up passed **1,007 offline Python tests**,
**213 frontend tests**, three player guards and **110 Chromium workflows**;
TypeScript, production build, Ruff and the AMD64 Docker build/runtime smoke check
also passed. Live MongoDB was not enabled for this follow-up.


The 2026-10-20 local candidate passed **1,018 Python tests** with an isolated
MongoDB replica set, warnings treated as errors, and **84.07% coverage**.
All **213 frontend unit tests**, three player patch guards, **100 Chromium
workflows**, TypeScript checking and the production UI build passed. Targeted browser checks cover real sparse PDF reads, synchronized seeking,
live uploads, contextual search, compact mobile layouts, backup links and
historical metrics. The AMD64 Docker image built and passed non-root runtime,
bundled PDF asset, writable-directory and private-state exclusion checks.
These synthetic fixtures do not certify every physical
device or live Telegram route.


Transfer locations: exact duplicate-name, move/rename, trash/deletion, owner API
and history checks passed, including an isolated native MongoDB check. The
Chromium transfer workflow covers grouped statuses, compact finished rows,
folder navigation, selection and normal preview access.

Upload scheduling: 36 backend checks, 33 browser-controller unit checks and one
Chromium settings workflow passed in WSL. Coverage includes three-bot dispatch,
live limit changes, queue backpressure, persistence and owner-only validation.
These checks do not certify a particular Telegram rate or replace release testing.

## Validation commands

**During a small change:** run the relevant checks.
**Before a release:** run the full validation and artifact checks below.
For the coverage run, first enable the isolated MongoDB tests described below;
the default offline tests do not exercise the production database sufficiently
to meet the 80% coverage floor.

<details>
<summary>Full lint, tests, dependency and build checks</summary>

~~~bash
uv lock --check
npm run typecheck --prefix website
npm test --prefix website
npm run build --prefix website
uv run --frozen --all-groups ruff check tgdrive tests scripts config.py main.py
uv run --frozen --all-groups ruff format --check tgdrive tests scripts config.py main.py
uv run --frozen --all-groups python -m pytest -W error \
  --cov=tgdrive --cov-report=term-missing
uv run --frozen --all-groups bandit -c pyproject.toml -r tgdrive
uv run --frozen --all-groups pip-audit --strict
npm audit --prefix website --audit-level=high
docker compose --env-file .env.example config --no-env-resolution --quiet
~~~

The coverage floor is 80%; do not lower it to hide untested work.
Compose validation checks the template, not the validity of placeholder credentials.

</details>

<details>
<summary>Chrome tests with the synthetic local backend</summary>

~~~bash
uv run --frozen --group e2e playwright install --with-deps chromium
TGDRIVE_E2E=1 uv run --frozen --group e2e python -m pytest \
  -m e2e tests/test_browser*.py
~~~

Install browser binaries/libraries inside WSL.
Evidence goes to ignored `cache/browser-e2e` and the feature-specific cache folders.
The image-heavy PDF fixture is generated only when the dedicated PDF browser
tests request it; it is not part of ordinary preview startup.

These tests render the real UI against a test backend. They do not prove a live
Telegram transfer. The fixture's `/__test__` routes are not production endpoints.

</details>

<details>
<summary>Opt-in live MongoDB tests</summary>

Use a disposable local replica set, or supply a dedicated test server URI:

~~~bash
docker run -d --name tgdrive-test-mongo -p 127.0.0.1:27017:27017 \
  mongo:8.0 --replSet tgdrive-test --bind_ip_all
until docker exec tgdrive-test-mongo mongosh --quiet --eval 'db.adminCommand({ping:1})'; do sleep 1; done
docker exec tgdrive-test-mongo mongosh --quiet --eval \
  'rs.initiate({_id:"tgdrive-test",members:[{_id:0,host:"localhost:27017"}]})'
until docker exec tgdrive-test-mongo mongosh --quiet --eval \
  'if (!db.hello().isWritablePrimary) { quit(1); }'; do sleep 1; done
export TGDRIVE_MONGO_TESTS=1
export TGDRIVE_TEST_MONGODB_URI='mongodb://127.0.0.1:27017/?replicaSet=tgdrive-test&directConnection=true'
uv run --frozen --all-groups python -Werror -m pytest tests/test_mongodb.py -q
~~~

Tests require an explicit `TGDRIVE_TEST_MONGODB_URI`; they never infer it from
`MONGODB_URI` or the deployment's `.env`. They create random `tgdrive_test_`
databases. Cleanup targets only those exact disposable databases. CI starts its
own local replica set.

Never point cleanup tests at the real application's database.
Remove the disposable local server after testing with
`docker rm -f tgdrive-test-mongo`.

</details>

### Live Telegram testing

Use a dedicated disposable channel and test credentials.
Create/delete messages only with the owner's authorization.

Record actual sizes, original-download checksums, rendered previews and confirmed
remote deletion. Keep secrets, traces and detailed evidence out of public docs.

## CI and release evidence

**Current candidate: MongoDB public beta, `dev` branch.**

Local beta validation on **2026-10-18**: 819 Python tests passed with an isolated
MongoDB 8.0 replica set and warnings treated as errors; coverage was **82.39%**.
The separate Chromium suite passed all 48 tests. All 156 frontend unit tests,
TypeScript checking, production build, Ruff lint/format, Bandit, locked-dependency
checks, Python/npm vulnerability audits and Docker Compose validation passed.
The AMD64 container built successfully and passed non-root runtime import,
bundled-asset, writable-directory and private-state exclusion smoke checks.
The source ZIP passed its SHA-256 and archive-integrity checks, exclusion checks,
and a clean extraction with locked dependency installation, production frontend
build, 819 Python tests and 156 frontend tests outside the working checkout.

Gitleaks scanned the public candidate and existing Git history. Five detections
inside bundled third-party FFmpeg sources were reviewed as source identifiers
and SHA-256 checksums. No application credentials were found. The real local
channel ID in a test was replaced with a synthetic ID. Downloaded manual samples
under `testfiles/` stay local because redistribution terms were not verified.

| Check | Evidence |
| --- | --- |
| MongoDB persistence and consistency | Focused tests passed for reload, transactions/rollback, names, folders, search, sharing and multipart records |
| Durable state and ownership | Jobs, receipts, sessions, settings, owner fencing and ambiguous commit behavior checked |
| Encrypted backups | A real snapshot was uploaded, read back and verified |
| Real emergency restore | That snapshot restored into an isolated empty DB; settings reopened without old local files; browser sessions were revoked |
| Live WSL Chrome workflows | Folder upload, grouped transfers, SVG rendering, exact downloads, sharing, Trash/restore and disposable deletion passed |
| Connection Refresh | Three real bots passed saved-token/channel checks; revision unchanged and no browser errors |
| Transfer display and controls | Focused WSL Chrome fixture passed stage-zero/live bytes, single/multi-file views, mobile layout, Hide/Show and confirmed bulk cancellation; targeted frontend tests/build passed |
| Transfer history | Focused registry/API and native MongoDB archive/reopen checks passed; Chrome verified status filters, panel dismissal, browser-tracking reset and responsive history views |
| Full regression after MongoDB switch | 819 Python tests, 82.39% coverage; 156 frontend tests and 48 Chromium tests passed on 2026-10-18 |
| Large live file after MongoDB switch | Subsequent server validation: 4,562,805,302-byte file reconstructed from three Telegram documents; full SHA-256 and part-boundary ranges matched. This is original-byte integrity evidence, not certification of every playback codec/device. |
| 2026-10-18 source ZIP/container baseline | Source ZIP checksum/extraction/clean build/tests and AMD64 container build/runtime smoke checks passed |

Earlier evidence, **before MongoDB**, includes:

- A **2,160,066,560-byte MP4**: upload, full original SHA-256, ranges across
  part boundaries, playback and visible subtitles.
- **68 format fixtures**: 47 actual previews and 21 expected download fallbacks;
  all explicit original downloads matched source sizes and SHA-256.
- Three small uploads using three bots.
- Broader Python, browser, container and recovery checks.

These results do not certify the current database revision or every codec/device.
Premium near-limit/speed checks and deletion of a message older than 48 hours
remain unverified. See [remaining checks](roadmap.md#before-public-release).

A configured GitHub workflow is not proof that it ran. Local results above do
not certify the GitHub Python-version matrix or multi-architecture container job;
check the workflow run for the pushed commit. Publishing the beta branch does
not publish a hosted service or a stable release.

## Version baseline and dependency policy

Use the pinned files as the exact dependency source:

- [Python dependencies](../pyproject.toml) and [uv.lock](../uv.lock).
- [Frontend dependencies](../website/package.json) and [package-lock.json](../website/package-lock.json).
- [Container/runtime pins](../Dockerfile).

<details>
<summary>Human-readable baseline — October 2026 candidate</summary>

| Component | Baseline |
| --- | --- |
| App Python | 3.14.8 default; declared support 3.11–3.14 |
| Setup host | Python 3.10+; Docker supplies the app runtime |
| uv | 0.13.1 |
| Node | 24.22.0 LTS; frontend only |
| Telegram | Telethon 1.45.0 + cryptg 0.6.0 |
| Backend | FastAPI 0.143.5 + Uvicorn 0.54.0 |
| Database | MongoDB replica set/Atlas + PyMongo 4.19.0 |
| Authentication | argon2-cffi 25.1.0 |
| Frontend | React 19.3.1, TypeScript 7.0.2, Vite 8.3.4, Vitest 5.0.5 |
| PDF/video | PDF.js 6.4.299; Movi Player 0.4.1 |
| Container targets | Linux AMD64 and ARM64 |

Earlier multi-Python results predate MongoDB. The current CI matrix is configured
for Python 3.11, 3.13 and 3.14; configuration is not executed evidence.

</details>

Choose supported stable versions that pass compatibility checks.
Do not replace locks with unconstrained installs. Future upstream support is
not guaranteed. Dependency updates must include the affected integration checks.

## Repository map and test map

| Area | Code/tests |
| --- | --- |
| Backend | `tgdrive/` |
| Web app | `website/src/` |
| MongoDB | `tests/test_mongodb.py`, `tests/test_mongodb_unit.py` |
| Jobs and receipts | `test_tasks.py`, `test_durable_jobs.py`, `test_resumable_uploads.py` |
| Large files | `test_multipart_transfers.py`, `test_multipart_streaming.py`, `test_multipart_previews.py` |
| Telegram pool/transport | `test_client_pool.py`, `test_telegram_clients.py`, `test_telethon_transport.py` |
| HTTP/auth/security | `test_app.py`, `test_http_guard.py`, `test_security.py`, `test_remote.py` |
| Connections/Premium | `test_management.py`, `test_management_admission.py`, `test_premium_login.py` |
| UI/browser | `website/src/*.test.ts`, `tests/test_browser_e2e.py` |
| Legacy persistence | `test_store.py`, `test_backup.py`, `test_recovery.py` |
| Packaging | `scripts/build_release.py`, `tests/test_release_package.py` |
| Private/generated output | Ignored `data/`, `cache/` and `dist/` |

Test names without a path are under `tests/`.
Follow the [architecture rules](architecture.md) when changing side effects:
include retry, cancel, interruption and ownership-loss behavior.

## Frontend contracts

| Area | Keep this behavior |
| --- | --- |
| Shell | Quiet file list, one New menu, separate owner pages; database status stays on Storage & Backups |
| Selection | One click selects; double-click/Enter opens; checkboxes appear in selection mode |
| Transfers | Single files have one row; multi-file batches collapse, retain completed siblings and show attention first; Cancel all requires confirmation |
| Metrics | Single-file bar/bytes use the same stage; batch bar/bytes count Telegram progress; no queued elapsed timer or invented ETA |
| Resume | Verify original bytes; name/size alone is insufficient |
| Folder preparation | Stable request/IDs, atomic tree, keep-both root; no invented empty folders |
| Previews | Full viewport, one header; unsupported files get compact details |
| PDF | Continuous scroll; no duplicate toolbar, page-flip buttons or Page text |
| Connections | Write-only secrets, revision guards, isolated verification, restart to apply |
| Themes/accessibility | Semantic tokens, keyboard/focus, mobile layout, text status and reduced motion |

Use local Inter, Lucide and existing tokens in [style.css](../website/src/style.css).
Do not duplicate the palette in another specification or add runtime font/script CDNs.

<details>
<summary>Media compatibility and third-party licensing details</summary>

- MP4 mov_text/tx3g subtitles use the public SubtitleRenderer extension in
  [MovTextRenderer.ts](../website/src/MovTextRenderer.ts).
- Decode the two-byte big-endian text length and strict UTF-8/BOM-marked UTF-16; ignore
  style/font/link boxes and render only textContent.
- The implementation caps packets at 128 KiB, each cue at 16 Ki characters, the queue at
  4096 cues/2 Mi characters and visible overlap at eight cues.
- It retains bounded recent cues for delay adjustment, clears on seek/track change, and
  removes owned DOM/styles on Off/source cleanup.
- Its scoped style keeps Movi's public mount visible while the built-in empty-cue path
  is inactive.
- This is not a private WASM decoder patch or a full rich-text subtitle implementation.
- Relevant primary references are [3GPP/ETSI timed text](https://www.etsi.org/deliver/etsi_ts/126200_126299/126245/16.00.00_60/ts_126245v160000p.pdf) and the [FFmpeg mov_text
  decoder](https://ffmpeg.org/doxygen/8.0/movtextdec_8c_source.html).
- The generated MP4 fixture verifies actual visible English/Hindi cues, seeking, Off,
  inert HTML-looking text and original bytes; MKV/SRT remains a separate real-decoder
  regression.

- The enhanced-player wrapper in [AdvancedVideo.tsx](../website/src/AdvancedVideo.tsx)
  adapts Movi 0.4.1's private addStyles method to keep fonts local and component styles
  compatible with the application CSP.
- This is version-specific compatibility code: an upgrade must pass the real WASM
  fixture, EAC3 audio output/track switching, rendered subtitle, seek/close and
  CSP/external-request checks.
- Observe worker console/CSP diagnostics as well as document securitypolicyviolation
  events; an empty document-event array cannot establish a clean worker decoder.
- API names and CSS assumptions must be reverified against the newly pinned version.
- Startup state follows real module/media events and unlocks controls on
  metadata/readiness without requiring autoplay.
- Keep the custom player's controls reachable during startup, buffering and seeking.
  Loading messages must not claim that source-byte coverage proves decoded audio/video
  readiness. Clean up event listeners and timers, and offer recovery for long waits
  without inventing a timeout failure. Chapters belong in the auto-hiding controls.
- Verify reduced motion and delayed media with the real decoder, not only a mocked state
  machine.

- Bundled viewer notices are retained in [website/public/third-party](../website/public/third-party/README.txt) and copied into the production
  frontend and Docker image.
- Preserve the full notices when redistributing these assets.
- Exact React, React DOM, Scheduler and Lucide license texts are included too; Lucide's
  full notice retains Feather-derived icon attribution.
- A self-contained source/relink archive includes FFmpeg/dav1d sources, application
  C/C++ and published static archives.
- The notice file records upstream's stale version label, binary/header evidence
  matching the published recipe and the reproduction limit.
- TGDrive has not independently rebuilt its published decoder WASM.

</details>

## Source releases

Run final validation when requested. Update docs/changelog, then prepare a
reviewable source artifact:

~~~bash
uv run --frozen python scripts/build_release.py
(cd dist && sha256sum --check tgdrive-2.0.0.zip.sha256)
unzip -l dist/tgdrive-2.0.0.zip
~~~

Use the actual project version after a bump.

<details>
<summary>Artifact and container verification checklist</summary>

- Build twice into different output directories; identical inputs should yield
  identical ZIP bytes.
- Review the allowlist and privately check for secrets. An allowlist is not a scanner.
- Include the eight docs, source/tests, lockfiles, notices and manifest hashes.
- Exclude credentials, DBs, sessions, staging, logs, caches, private evidence,
  generated frontend assets, node_modules and Git history.
- The downloaded 68-file operator corpus is not a release input.
- Extract into a new empty directory; validate the manifest, locked install,
  typecheck/tests/build and container build from that extraction.
- Shipped tests must not depend on omitted private fixtures.
- Retain full viewer/codec/font licenses and source/relink access.

~~~bash
docker buildx build --platform linux/amd64 --load -t tgdrive:release-candidate .
docker run --rm --network none --entrypoint python tgdrive:release-candidate \
  -c 'import tgdrive.app, telethon, cryptg, argon2; print("runtime imports passed")'
docker buildx build --platform linux/arm64 --load -t tgdrive:release-candidate-arm64 .
~~~

ARM execution requires hardware or a configured QEMU/binfmt builder.
Check construction, assets, nonroot permissions, disposable init, shutdown and
restart. Imports alone do not prove Telegram login.

Distinguish unit tests, synthetic Chrome, live source-service checks, container
checks and QEMU. A small transfer does not establish near-limit behavior;
a receipt-stage crash does not establish Telegram-send crash recovery.

</details>

## Documentation maintenance

Keep these eight pages; update the relevant one with each behavior change.

| Page | Owns |
| --- | --- |
| Setup | Accounts and installation |
| Usage | Everyday web workflows |
| Operations | Running, troubleshooting and recovery |
| Reference | Exact settings, API and security boundaries |
| Architecture | Components and correctness rules |
| Development | Tooling, tests, design constraints and releases |
| Roadmap | Remaining work and unverified scenarios |

Write the user action first. Use short steps/tables; define technical terms.
Keep deep implementation details out of the first-run path.
Record precise private evidence separately, and keep implemented/pending status honest.


### Shared-links interface

Shared Links uses compact responsive rows with the item, optional link label,
current parent directory, expiry and copy/edit/location/revoke controls. Active
and expired views share the same row structure. On narrow screens the filename
provides location navigation so the redundant location icon can disappear.
Each direct link is keyed by its share ID, including pending, copied and edit
state. Changing or revoking one link must not affect another link to the same
node. Legacy links that cannot be copied offer **New Link**, never silent rotation.

My Files uses a small link indicator for direct sharing or inherited folder access;
expired shares never produce an indicator. Public browsers do not fetch owner share
metadata. Creating a link refreshes indicators when the share dialog closes.


### UI Capitalization

Use title case for fixed navigation, headings, field labels, buttons and menu
actions: **My Files**, **Shared Links**, **Storage & Backups**, **Upload Files**.
Keep explanatory sentences in sentence case. Render user-provided file/folder
names verbatim; never apply CSS text-transform or a title-case function to them.
Location formatting may normalize only TGDrive's first root-label segment.

### Quiet Overlays and Touch Controls

The desktop floating transfer panel opens for unfinished work, starts minimized
on non-file browsing pages, and disappears five seconds after completion.
Loading server history alone never opens it. Finished history remains on Transfers;
hiding a popup never clears records. Settings, Connections and Storage & Backups
do not mount the floating popup; Transfers remains available from navigation.
Phones have their own composition: a top bar contains navigation, TGDrive, New
(in My Files) and appearance. The file toolbar holds search/selection/view controls.
The workspace owns scrolling, including its heading and column names. No floating
upload panel mounts on phones; a reserved bottom status row opens Transfers for
full progress and cancellation. The full-height drawer overlays the top bar and
uses one scroller for every navigation group and footer action. New is absent
from search/management pages and hidden during dialogs.

Keep sharing indicators next to their filename, preserve a visible indicator
when the name truncates, and keep row actions at least 44px on small screens.
Move name-conflict choices use explicit themed buttons rather than a native select.

Folder upload confirmation contains only the chosen folder, count, total size,
destination and actions. General picker limitations belong in usage documentation;
show a skipped-file notice only when the current selection actually contains one.

Upload completion refreshes the current listing in the background, keeping rows,
scroll position, pagination and surviving selections in place. Loading placeholders
are reserved for opening a different listing. A temporary background-refresh failure
keeps the last successful listing visible and reports the error.

Batch aggregation precedes section assignment and page-status filtering.
Never partition folder members by file status before constructing their batch.
Running takes priority, then queued work; terminal outcomes share Finished.
Finished has All, Successful, Failed and Cancelled filters, with cursor pagination.
The latest server page stays authoritative; unrelated browser history is not
prepended to it. Historical folder grouping requires retained browser batch metadata.
The expanded batch retains every child and its individual status.

Transfer group disclosure is a single stable button containing chevron, neutral
folder/files icon, name and count. It has no navigation handler, including on
double-click. Show Folder is a separate action available only after completion.
Expanded child rows omit the common group location and repeated dates; retain
relative nested locations or a different current location.


### UI Audit Contracts

- Mobile navigation owns focus while open. Escape and Close Navigation restore
  the trigger; the covered main content is inert. Resizing to desktop closes it.
- Shared-status icons have an accessible image role and name. Selected filters
  meet text contrast requirements in both themes.
- Small-screen actions use at least 44px hit areas; a checkbox's label supplies
  the target without enlarging its visual glyph.
- Select All exposes the mixed state for partially selected pages. Local file
  pagination returns to the top; reveal/highlight navigation still focuses its item.
- Expanded upload groups retain their disclosure state across category changes.
- Move destinations grow with their content within a bounded scrollport. Move Here
  is disabled when every selected item is already in that directory.
- Failed history/share loads offer Try Again and never masquerade as an empty
  result. A failed share revocation displays its error inside its confirmation.
- Dropping files requires a visible destination in My Files. Folder drops ask for
  Upload Folder rather than silently flattening or uploading a directory entry.
- Reduced motion stops spinner rotation while retaining loading text and status.
- Keep browser checks isolated from real file mutations; use the offline E2E
  fixture for destructive actions and synthetic network failures.

## Shared UI components

The frontend uses Mantine Core/Hooks 9.7.3 with React 19, pinned together in the
lockfile. TGDrive has one colorful dark theme. See [Design system](../website/DESIGN.md)
for semantic tokens, section structure, control choices and motion rules. Shared
library controls use the root provider; specialized file/player UI uses the same
semantic CSS tokens. Do not reintroduce per-browser light/system settings.
