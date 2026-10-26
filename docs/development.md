# Develop and release TGDrive

[Docs home](README.md) · [Architecture](architecture.md) · [Release verification](release.md)

Deployment uses [Docker Compose](setup.md). This page covers contributor builds,
tests and development servers **inside WSL Ubuntu**, using the Linux checkout.

[Run locally](#install-the-development-environment) · [Sample UI](#preview-with-sample-content) · [Validation](#validation-commands) · [Release checks](#source-releases)

The [showcase](showcase.md) documents live illustrative captures. Follow the
[media guide](assets/README.md) when replacing them; captures do not replace the
automated and live validation described below.

## Install the development environment

```bash
uv sync --frozen --all-groups --python 3.14.8
npm ci --prefix website
npm run build --prefix website
```

Keep lockfiles. Ordinary tests inject fake resources and need no live Telegram credentials.

## Run while editing

With private configuration and an existing drive:

```bash
COOKIE_SECURE=false uv run --frozen python -m uvicorn main:app \
  --host 127.0.0.1 --port 8000 --workers 1 \
  --timeout-graceful-shutdown 45 --no-access-log
```

In another WSL terminal:

```bash
npm run dev --prefix website
```

Vite normally serves 5173 and proxies API/media to 8000; production builds use 8000.

> [!IMPORTANT]
> Run one owner per drive. Avoid reload during real uploads unless testing
> interruption recovery.

## Preview with sample content

```bash
npm run build --prefix website
TGDRIVE_UI_SHOWCASE=1 uv run --frozen --group e2e python -m uvicorn \
  --app-dir tests browser_transfer_app:app --host 0.0.0.0 --port 18767
```

Open [localhost:18767](http://localhost:18767/) with `offline browser test password`.
The fixture seeds files, transfers, shares, caches, performance and reading samples
without Telegram credentials. Restart resets it. CBR needs
[libarchive](reading.md), included in Docker. Showcase uses the production
16 GiB file cap; ordinary browser tests use 32 MiB. Never publicly deploy the
fixture or its `/__test__` endpoints.

## Latest focused validation

Recorded evidence describes a tested snapshot, not the state of a later checkout.

<details>
<summary>URL intake, slow queue and immediate start feedback — 2026-10-23</summary>

Offline fixtures passed **156 backend tests plus 24 subtests** and **88 frontend
tests**. After correcting intake-mode leakage into the Telegram-stage ETA, all
**15 progress-metrics tests** passed again. TypeScript checking, Ruff lint/format
checks on 17 changed Python files, and Prettier checks on 10 frontend files passed.

Coverage includes exact range assembly, representation mismatch and single-stream
fallback, cancellation while writing, promotion with an existing prefix, full-speed
slot/connection bounds, slow pacing, storage windows, saved concurrency settings,
immediate client feedback and lost-start-response reconciliation. These are
synthetic offline checks; no live downloads, throughput benchmark, production build,
browser visual check or deployment was performed for this change.

</details>

<details>
<summary>Shared Links and simultaneous owner sessions — 2026-10-23</summary>

The reviewed working tree passed **201 focused backend tests**, **335 frontend
tests**, typechecking and the production build. Ten focused Chromium cases cover
independent file/folder links, icon-only copy controls, unavailable/expired links,
edit/revoke scope, exact timestamps, and desktop/phone layouts in light and dark
themes. These use the real frontend with synthetic Telegram/storage fixtures.

Backend session checks use separate client cookie jars, SQLite and mocked MongoDB
session storage: later logins leave existing devices signed in, and logout only
revokes that session. Client tests cover bounded retries for simultaneous login
verification. Recovery tests enforce the destination confirmation at activation.
These checks do not certify live provider deployments or every browser/device.

</details>

<details>
<summary>Authentication, shares and outbound-request checks — 2026-10-23</summary>

The reviewed working tree passed **170 focused tests and 24 subtests**, covering
authentication/configuration, request guards, frontend security, share edits,
remote imports, deployment, recovery APIs, logging and access revalidation.
Regression cases cover invalid logout cookies causing no database writes,
omitted share fields preserving expiry/labels under concurrent edits, and shared/
site-local IP ranges blocked through literals, DNS and redirects.

Focused Ruff checks passed. Bandit reported no findings; the locked runtime
Python dependency audit and frontend dependency audit found no known
vulnerabilities. These are automated checks, including SQLite and mocked MongoDB
behavior. They are not a live MongoDB, HTTPS/proxy/provider test or an independent
penetration test. See the [remaining authentication limits](reference.md#security-and-privacy).

</details>

<details>
<summary>Adaptive read-ahead and Preloads results</summary>

The recorded adaptive read-ahead/Preloads snapshot passed **1,479 offline Python
tests** (55 skipped, 15 subtests), **179 strict focused backend checks**, **321
frontend tests** and **eight Chromium workflows**, plus TypeScript, production
build and focused Ruff checks. Coverage includes exact range bytes, bounded
read-ahead, distinct-viewer promotion, cache-index races and responsive Preloads
with a capacity panel and full-width segmented file coverage.
These fake-Telegram/synthetic checks do not measure live speed. They also do not
certify documentation edits or a later release artifact.

</details>

## Validation commands

Run relevant checks for a small change. Before release, run full validation and
[artifact verification](release.md). Coverage requires opt-in isolated MongoDB;
offline tests alone do not sufficiently exercise the production database.

| Change | Checks to include |
| --- | --- |
| Backend behavior | Focused Python tests; MongoDB tests for persistence changes |
| Interface | Typecheck, frontend tests/build and relevant Chromium workflows |
| Transfer/recovery | Retry, cancellation, interruption and ownership-loss cases |
| Release | Full checks below, then a clean extracted archive |

For URL intake changes, the focused offline checks are:

```bash
uv run --frozen --all-groups python -m pytest -W error \
  tests/test_remote.py tests/test_remote_ranges.py tests/test_remote_retries.py \
  tests/test_remote_scheduler.py tests/test_transfers.py \
  tests/test_transfer_metrics.py tests/test_staging_admission.py -q
```

These cover fake HTTP responses, staging admission, advisory progress versus
durable state, cancellation and handoff to fake Telegram. They do not measure
live download speed or confirm a deployment.

For thumbnail regeneration and selective readers, focus on
`tests/test_thumbnail_rebuild.py`, `tests/test_generated_thumbnails.py`,
`tests/test_thumbnails.py`, `tests/test_books.py`, `tests/test_books_api.py` and
`tests/test_multipart_previews.py`. The rebuild tests use generated images/books,
a loopback range bridge and fake Telegram reads, including padded archives that
must not be fetched in full. `tests/test_browser_thumbnail_rebuild.py` covers
confirmation, progress/reload/cancellation and image-based EPUB manga fit/direction.
These synthetic checks do not establish live Telegram throughput or universal
codec/device support.

<details>
<summary>Full lint, tests, security and build checks</summary>

```bash
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
```

Keep the 80% coverage floor. Compose validation checks syntax, not placeholder
credential validity. The latest focused checks pass with warnings as errors;
a full offline strict run still exposes existing SQLite fixture-cleanup warnings.

</details>

<details>
<summary>Chromium with the synthetic backend</summary>

```bash
uv run --frozen --group e2e playwright install --with-deps chromium
TGDRIVE_E2E=1 uv run --frozen --group e2e python -m pytest \
  -m e2e tests/test_browser*.py

# Focused explicit-refresh pending, failure and confirmation checks
TGDRIVE_E2E=1 uv run --frozen --group e2e python -m pytest \
  tests/test_browser_refresh_feedback.py
```

Install Chromium/libraries inside WSL. Evidence stays in ignored `cache/browser-e2e`
and feature folders. These render real UI against a test backend, without proving
live Telegram transfers. Only dedicated tests generate image-heavy PDF fixtures.

For player interaction changes, include `test_browser_player_gestures.py`,
`test_browser_player_mobile.py` and `test_browser_player_settings.py` under
`tests/`. They use decoded media, a generated 65-second H.264/AAC gesture fixture,
320/390 px portrait and 720 px landscape layouts, keyboard and fullscreen checks.
Emulated touch is separate from physical-device/other-browser validation.

`test_browser_player_controls.py` covers measured loading feedback, held delivery
and decoded audio, frame stalls, retries, source-byte retention and buffered
forward seeks during simulated network loss. The pinned-player checks in
`website/scripts/check-movi-player.js` also exercise bitrate scaling, refill
hysteresis, allocation failure, shared/non-shared buffer growth and hardware EOF
drain. `website/src/videoBufferMemory.test.ts` covers device-hint ceilings and the
missing-hint fallback. Validate actual HEVC profiles on the
target browser/OS; browser-native support alone does not validate the custom
player. Thumbnail repair tests use cached sources and forbid origin payload reads;
thumbnail rendering tests require FFmpeg and Poppler.

</details>

<details>
<summary>Opt-in MongoDB replica-set tests</summary>

Use a disposable local replica set or a dedicated test URI:

```bash
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
```

Tests require `TGDRIVE_TEST_MONGODB_URI`, never infer the deployment URI, and
create/clean only exact random `tgdrive_test_` databases. CI uses its own replica
set. Never use the application's real database. Remove the disposable server:

```bash
docker rm -f tgdrive-test-mongo
```

</details>

### Live Telegram testing

Use disposable channels/test credentials and owner-authorized message mutations.
Record sizes, original-download checksums, rendered previews and confirmed remote
outcomes privately. Never commit credentials or traces.

## CI and release evidence

Use these records to understand coverage. Validate the exact release commit and
clean extraction; keep its checksum with the test record.

<details>
<summary>Self-hosting snapshot and live original-byte validation</summary>

These guides target **2.0.0** and fresh MongoDB deployments. On **2026-10-23**, the
self-hosting snapshot passed 1,394 Python tests with isolated MongoDB and 84.07%
coverage, 255 frontend tests, 53 Chromium checks, container/runtime/certificate
checks, encrypted-archive restore into an empty database, lint/security audits.
It did not deploy to live Coolify or use real Telegram credentials.

Separately recorded live MongoDB evidence reconstructed a 4,562,805,302-byte file
from three Telegram documents with matching SHA-256 and boundary-range checks.
That establishes the tested original-byte path, not universal playback speed.

Results apply to their tested revisions/environments. Workflow definitions are
not proof of execution. Physical devices, multi-architecture execution, outages
and Premium speedups need [separate evidence](roadmap.md#before-public-release).

</details>

## Version baseline and dependency policy

| Tool/runtime | Baseline |
| --- | --- |
| App Python | 3.14.8 default; supported 3.11–3.14 |
| Setup host | Python 3.10+; Docker supplies app runtime |
| uv | 0.13.4 |
| Frontend Node | 24.22.0 LTS |
| Container targets | Linux AMD64/ARM64 |

Exact libraries live in [pyproject.toml](../pyproject.toml), [uv.lock](../uv.lock),
[package.json](../website/package.json), [package-lock.json](../website/package-lock.json)
and [Dockerfile](../Dockerfile). Do not replace locks with unconstrained installs.
Updates need affected integration checks. CI configures Python 3.11/3.13/3.14;
configuration alone is not executed evidence.

## Repository map and test map

<details>
<summary>Find the relevant tests</summary>

Names below are under `tests/` unless stated otherwise.

| Area | Code/tests |
| --- | --- |
| Backend / web app | `tgdrive/` / `website/src/` |
| MongoDB | `test_mongodb.py`, `test_mongodb_unit.py` |
| Jobs/receipts | `test_tasks.py`, `test_durable_jobs.py`, `test_resumable_uploads.py` |
| Split files | `test_multipart_transfers.py`, `test_multipart_streaming.py`, `test_multipart_previews.py` |
| Telegram | `test_client_pool.py`, `test_telegram_clients.py`, `test_telethon_transport.py` |
| HTTP/security | `test_app.py`, `test_http_guard.py`, `test_security.py`, `test_remote.py` |
| Public demo access and protected samples | `test_demo_mode.py`, `test_demo_admin_auth.py`, `test_demo_files.py`, `test_demo_task_policy.py`, `test_browser_demo_mode.py`, `test_browser_demo_access.py` |
| Connections | `test_management.py`, `test_management_admission.py`, `test_premium_login.py` |
| Cache/preloads | `test_adaptive_read_ahead.py`, `test_preloading.py`, `test_preload_api.py` |
| Thumbnails | `test_thumbnails.py`, `test_generated_thumbnails.py`, `test_cached_thumbnail_repair.py`, `test_thumbnail_repair_access.py` |
| UI | `website/src/*.test.ts`, `test_browser*.py` |
| Store/checkpoints | `test_store.py`, `test_backup.py`, `test_recovery.py` |
| Packaging | `scripts/build_release.py`, `test_release_package.py` |

Generated/private output belongs in ignored `data/`, `cache/` and `dist/`.
For side-effect changes, test retry, cancellation, interruption and ownership loss
against the [architecture contracts](architecture.md).

</details>

## Frontend contracts

Use [the design system](../website/DESIGN.md), semantic tokens, local Inter and
Lucide. Preserve keyboard/focus, saved appearance and reduced motion. Keep upload
byte verification, stable operation IDs and atomic folder preparation intact.

<details>
<summary>Media compatibility and redistribution</summary>

[MovTextRenderer.ts](../website/src/MovTextRenderer.ts) renders MP4 mov_text/tx3g
through the public subtitle extension as inert text: strict UTF-8/BOM UTF-16,
no style/link boxes. Bounds are 128 KiB packets, 16 Ki-character cues, 4096 cues /
2 Mi characters queued, eight visible overlaps. Seeking/track changes clear cues;
Off/source cleanup removes owned DOM/styles. Test visible English/Hindi,
HTML-looking text, seeking/Off and original bytes; MKV/SRT is a separate regression.

[AdvancedVideo.tsx](../website/src/AdvancedVideo.tsx) adapts Movi 0.4.1 private
`addStyles` for local fonts and CSP. Upgrades must recheck APIs/styles using the
real WASM fixture, EAC3 output/track changes, subtitles, seek/close and external-
request/CSP checks. Inspect worker diagnostics as well as document CSP events.
Keep controls reachable during startup/buffering; source coverage does not prove
decoder readiness. Clean listeners/timers and test delayed media/reduced motion.

Preserve full [third-party notices](../website/public/third-party/README.txt),
including React/Scheduler/Lucide/Feather attribution and viewer/codec/font licenses.
The source/relink archive includes FFmpeg/dav1d, application C/C++ and published
static archives. Notices record upstream's stale version label, binary/header
recipe evidence and reproduction limits; TGDrive has not independently rebuilt
the published decoder WASM.

</details>

### Shared-links interface

<details>
<summary>Link identity, exposure indicators and URL recovery</summary>

Key per-link state by share ID, even when links target one item. Expired links
never produce sharing indicators; public browsers never fetch owner share data.
Keep indicators visible beside truncated names. Unreconstructable URLs offer
**New Link**, never silent rotation. Refresh indicators after the share dialog closes.

</details>

### UI Capitalization

Title-case fixed labels/actions; sentence-case explanatory text. Display user
filenames verbatim, without text transforms. Only TGDrive's initial root label
may be normalized in location displays.

### Quiet Overlays and Touch Controls

<details>
<summary>Shell, listing and transfer behavior</summary>

- Desktop transfer popups show unfinished browser-initiated work, disappear five
  seconds after completion and never open from history alone. Management pages
  omit them. Phones use a reserved bottom status row opening Transfers.
- Mobile has a top navigation/brand/New/appearance bar and independently scrolling
  workspace. The full-height drawer has one scroller. Hide New outside My Files
  and during dialogs; selection must not shift the list vertically.
- Mouse opens by double-click; touch/Enter opens once. Select explicitly enables
  selection and retains file icons. Small-screen actions have 44 px hit areas.
- Background listing refresh preserves rows, scroll, pagination and selections;
  failures retain the last listing and show errors. Placeholders belong to new
  listings, not background refreshes.
- Build batches before assigning sections/filters; retain every child and its
  status. Running precedes queued; terminal outcomes share Finished with All,
  Successful, Failed and Cancelled filters and server-authoritative pagination.
- Batch disclosure is one stable button without navigation; Show Folder is a
  separate completed action. Expanded children omit repeated locations/dates.
  Retain disclosure state across category changes. Single-file progress uses one
  stage; batch metrics use Telegram progress. Never invent queued timers/ETAs.
- Folder confirmation shows selection/count/size/destination/actions. Show skipped
  files only when present. Move conflicts use explicit themed choices.

</details>

### UI Audit Contracts

Use this checklist when changing dialogs, navigation, selection or transfer controls.

- [ ] Navigation traps focus, makes covered content inert, restores focus on close
  and closes on desktop resize. Selection exposes mixed state and visible icons.
- [ ] Icons have accessible names; filters meet contrast. Reduced motion retains
  loading text without spinner rotation.
- [ ] Pagination returns to the top; reveal navigation still focuses the item.
  Move dialogs have bounded scrolling; disable Move Here for unchanged destinations.
- [ ] Failed loads offer Try Again instead of empty states; revocation errors stay
  inside confirmation. Cancel All requires confirmation.
- [ ] Drops require a visible My Files destination. Folder drops request Upload Folder,
  never silently flatten. Use isolated browser fixtures for destructive checks.

## Source releases

Follow [release verification](release.md) for the canonical publication procedure.
These commands build a reviewable artifact; use the actual version after a bump:

```bash
uv run --frozen python scripts/build_release.py
(cd dist && sha256sum --check tgdrive-2.0.0.zip.sha256)
unzip -l dist/tgdrive-2.0.0.zip
```

<details>
<summary>Artifact and container checks</summary>

Build twice and compare ZIP bytes. Review the public allowlist/manifest and check
privately for secrets; an allowlist is not a scanner. Exclude credentials, state,
staging, logs, private fixtures/evidence, installed/generated dependencies and Git
history. Retain public docs, lockfiles, tests and full licenses/source access.
Validate the manifest, locked install, tests/build and container from a clean
extraction. Shipped tests must not require omitted private fixtures.

```bash
docker buildx build --platform linux/amd64 --load -t tgdrive:release-candidate .
docker run --rm --network none --entrypoint python tgdrive:release-candidate \
  -c 'import tgdrive.app, telethon, cryptg, argon2; print("runtime imports passed")'
docker buildx build --platform linux/arm64 --load -t tgdrive:release-candidate-arm64 .
```

ARM execution needs hardware or QEMU/binfmt. Check assets, nonroot/writable
permissions, disposable initialization, shutdown and restart. Imports do not prove
login. Distinguish unit, synthetic browser, live Telegram, container and emulated
execution; small files and receipt crashes do not establish near-limit/send recovery.

</details>

## Documentation maintenance

Follow [AGENTS.md](../AGENTS.md): update important behavior with its implementation,
keep one canonical explanation, link focused public guides from Docs home and add
them to `PUBLIC_DOCS` in `scripts/build_release.py`. Keep guides brief, put technical
detail in collapsible sections, and record change history in meaningful Git commits.

## Shared UI components

Mantine Core/Hooks 9.7.3 and React 19 are pinned together. Library controls use the
root provider; specialized file/player UI shares semantic CSS tokens. See
[Design system](../website/DESIGN.md) for component, theme and motion rules.

## Independent channel backup contracts

<details>
<summary>Journal guarantees and focused validation</summary>

`replica_state.py` owns the private journal/authenticated capsule;
`replica_transport.py` owns the independent bot and copy confirmation;
`replica.py` coordinates recoverable catalog publication; `replica_api.py` and
`ChannelBackup` expose owner/recovery controls.

Keep copied media, mappings and protected revision consistent. Confirm both
recovery metadata copies before advancing protection. Persist send identities
before calls and reuse them after ambiguity. Restored catalogs must never replay
old-channel jobs/deletions against new IDs. Preserve sources/prior state during activation.

[Telegram account-wide random_id deduplication](https://core.telegram.org/api/updates#updatemessageid-updates)
survives sessions. `RANDOM_ID_DUPLICATE` may mean the first send is in flight;
retain the typed error and retry the same identity after a bounded wait. Verify
typed update receipts and the destination document before journal confirmation.

The offline transport/coordinator tests cover request pacing, fresh channel and
document checks, bounded batch verification, ambiguous sends and durable waits:

```bash
uv run --frozen python -m pytest tests/test_replica_transport.py tests/test_replica.py -q
```

These tests use synthetic Telegram clients; they do not establish live Telegram
rate limits or demonstrate production backup completion. Pacing and retry policy
are documented in the [backup guide](backup-recovery.md#enable-and-confirm-the-first-copy).

For the broader offline backup group, including journal rollback, pause races,
session authorization waits, archive handoff and activation/bootstrap recovery:

```bash
TGDRIVE_MONGO_TESTS=0 uv run --frozen python -m pytest \
  tests/test_replica.py tests/test_replica_transport.py \
  tests/test_replica_state.py tests/test_replica_settings.py \
  tests/test_replica_mongo.py tests/test_replica_activation.py \
  tests/test_replica_activation_mongodb.py tests/test_replica_bootstrap.py \
  tests/test_replica_api.py tests/test_replica_deletion_barrier.py -q
```

The opt-in live MongoDB tests are skipped by this command. It makes no live
Telegram requests and does not replace a recovery rehearsal.

```bash
TGDRIVE_E2E=1 PYTHONPATH=.:tests uv run --frozen --group e2e python -m pytest tests/test_browser_channel_backup.py -q
```

This uses real auth/API/UI with a synthetic replica service at 1440/390/320 px.
It covers configuration, progress, pause/retry, exports and explicit recovery
activation, not live copying or independent human-account ownership. Live tests
need disposable private source/target channels and unrelated bots; rehearse both
directions and database-only recovery without risking the only copy.

</details>

### Selective book pages and shared PDF resources

The pinned PDF.js worker patch (`website/scripts/patch-pdf-worker.js`) keeps image substreams sparse during parsing, defers image
stream bodies in page resource discovery and loads a painted image's dependencies
before decoding. Version/source identity guards reject unknown upstream bundles;
`predev`, `prebuild`, and `pretest` apply it. The browser bundles this worker locally.
Review the patch when updating PDF.js. Integrity tests live in
`website/scripts/check-pdf-worker.js`; the `shared` case in
`tests/test_browser_pdf_ranges.py` exercises a generated PDF with every page image
in one shared resource dictionary. These are synthetic fixtures, not a guarantee
for all PDFs or devices.

`tests/test_books.py` covers unchanged large original page bytes and bounded result
reuse. `tests/test_books_api.py` covers access checks on cached/conditional responses.
`website/src/bookImage.test.ts` covers download progress, errors, retry and cancellation;
the comic error/recovery, large original page and EPUB offscreen-image cases in
`tests/test_browser_books_shares.py` exercise the shipped UI and HTTP path.
