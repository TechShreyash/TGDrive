# Develop and release TGDrive

[Docs home](README.md) · [Architecture](architecture.md) · [Release verification](release.md)

Deployment uses [Docker Compose](setup.md). This page covers contributor builds,
tests and development servers **inside WSL Ubuntu**, using the Linux checkout.

Develop on `dev`; it contains work in progress and may be unstable. `main`
receives reviewed, tested stable updates through versioned release PRs. Normal
installations use a published tag or `main`; see [source selection](setup.md#get-the-source)
and [release verification](release.md) for the checks required before promotion.

The [showcase](showcase.md) documents live illustrative captures. Follow the
[media guide](assets/README.md) when replacing them; captures do not replace the
automated and live validation described below.

**Contents**

- [Install the development environment](#install-the-development-environment)
- [Run while editing](#run-while-editing)
- [Preview with sample content](#preview-with-sample-content)
- [Latest validation](#latest-validation)
- [Validation commands](#validation-commands)
  - [Live Telegram testing](#live-telegram-testing)
- [CI and release evidence](#ci-and-release-evidence)
  - [When GitHub checks run](#when-github-checks-run)
  - [Recorded results](#recorded-results)
- [Version baseline and dependency policy](#version-baseline-and-dependency-policy)
- [Code conventions](#code-conventions)
- [Repository map and test map](#repository-map-and-test-map)
- [Frontend contracts](#frontend-contracts)
  - [Shared-links interface](#shared-links-interface)
  - [UI Audit Contracts](#ui-audit-contracts)
- [Source releases](#source-releases)
- [Documentation maintenance](#documentation-maintenance)
- [Independent channel backup contracts](#independent-channel-backup-contracts)
  - [Selective book pages and shared PDF resources](#selective-book-pages-and-shared-pdf-resources)

## Install the development environment

```bash
uv sync --frozen --all-groups --python 3.14.8
npm ci --prefix website
npm run build --prefix website
```

Keep lockfiles. Ordinary tests inject fake resources and need no live Telegram credentials.

Use the [supported runtimes](#version-baseline-and-dependency-policy) and run the
commands in this guide only when the current task authorizes the relevant checks,
as required by [contributor guidance](../AGENTS.md#ask-before-running-tests-or-validation).

## Run while editing

With private configuration and an existing drive:

```bash
COOKIE_SECURE=false uv run --frozen python -m uvicorn tgdrive.web.app:create_app --factory \
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
  tests.support.browser_transfer_app:app --host 0.0.0.0 --port 18767
```

Open [localhost:18767](http://localhost:18767/) with `offline browser test password`.
The fixture seeds files, transfers, shares, caches, performance and reading samples
without Telegram credentials. Restart resets it. CBR needs
[libarchive](reading.md), included in Docker. Showcase uses the production
16 GiB file cap; ordinary browser tests use 32 MiB. Never publicly deploy the
fixture or its `/__test__` endpoints.

## Latest validation

Recorded evidence describes a tested snapshot, not the state of a later checkout.

<details>
<summary>Upload responsiveness and repeated-work reductions — 2026-10-27</summary>

The focused run passed **168 Python cases** across transfer journals, multipart
hashes, queue estimates, backup observations, catalog statistics and HTTP routes.
The selected six Vitest files passed **123 cases**, including upload cadence,
acknowledged-offset persistence, cross-tab recovery and byte formatting.
All **five new Chromium cases** passed for navigation request reuse, obsolete
breadcrumb cancellation and share-badge invalidation, including delayed startup
configuration and partial metadata failures.

The production frontend build and TypeScript checks passed on Node **24.22.0**.
Existing viewer CommonJS and bundle-size warnings remain. These checks used
offline or synthetic fixtures; no full suite, lint run, live MongoDB/Telegram
validation, speed benchmark or deployment was performed. See
[the performance policy](performance.md#navigation-during-uploads).

</details>

<details>
<summary>Reader HTTP compression — 2026-10-26</summary>

The focused `bookImage.test.ts` Vitest file passed **12 tests** on Node **24.22.0**.
Coverage includes decoded gzip/Brotli responses, unknown progress totals,
uncompressed truncation checks and cancellation above the 64 MiB image limit.
These are mocked Fetch responses; no browser/proxy validation, full suite,
typecheck or build was run for this correction. See [reader behavior](reading.md#bandwidth-and-compatibility).

</details>

<details>
<summary>Audit fixes: durability, concurrency and browser state — 2026-10-26</summary>

The offline run passed **1,930 cases and 36 subtests** in **83.18 seconds**, with
78 opt-in skips. Two failures came from one missed `await` in a test fixture;
after correction, its complete 12-case module passed. The complete Vitest suite
ran through the Python bridge. Ten additional journal-boundary cases passed,
including blocked commits, crash recovery, cancellation and staging limits.

The MongoDB parity group passed **98 cases**, using a disposable local replica
set for live database cases. Four Chromium cases verified preserved drafts after
configuration failure and initial permission gating. The production build and
TypeScript checks passed on Node **24.22.0**, with existing viewer bundle warnings.
Ruff lint/format passed on **264 Python files**; edited frontend formatting passed.

These checks use isolated or synthetic data. They do not validate a deployment,
live Telegram sends, hard-kill recovery or every device/network condition.

</details>

<details>
<summary>Repository cleanup, viewers and settings — 2026-10-26</summary>

Checks used Node **24.22.0** and Python **3.14.8**. The complete offline run passed
**1,908 Python cases and 36 subtests**, including the complete Vitest behavior
suite, in **87.20 seconds**. Its two fixture setup errors passed after repair;
an additional directory-authorization regression also passed. The 78 skips
covered opt-in browser and live MongoDB cases, checked separately below.

All **287 Chromium cases** passed across the full run and focused failed-case
reruns. Coverage includes native player chapters/subtitles, PDF ranges and
passwords, book readers, transfers, sharing and responsive settings. Two policy
gate cases also passed after fixing cross-category draft loss. Desktop/phone
and light/dark screenshots were inspected. Native CBR checks passed after
installing the required libarchive runtime.

The disposable MongoDB replica-set run passed **103 cases** across its initial
run and two repaired fixture reruns, including **43 live database cases**.
No deployment database or private environment file was used.

The production build, **28 viewer patch cases**, Ruff lint/format on **267 Python
files**, authored frontend formatting, Bandit, lockfile consistency and Python/npm
dependency audits passed. Local documentation checks found no broken references
among 557 links, images and anchors. The AMD64 Docker build and offline factory,
imports, bundled-resource and container-policy smoke checks passed.
The public source archive's 522 file hashes matched its manifest; a fresh
extraction installed its frontend dependencies and built successfully.

Passing cases were reused; the entire suite was not repeated after every final
fix. These checks do not validate live Telegram transfers, production startup,
deployment, ARM64 or physical-device/GPU behavior. Viewer archives were generated
from integrity-checked upstream packages; normal commands use the local archives.

</details>

<details>
<summary>MongoDB-only startup and website archive recovery — 2026-10-26</summary>

The final configuration, first-initialization, catalog-decoding, portable-part
and channel-recovery checks passed **257 backend cases** across the initial run
and failed-case reruns. Outdated local-storage fixtures and client doubles were
corrected. Earlier focused runtime, persistence, archive API and startup-error
checks also passed. Coverage includes atomic first setup, immediately active
initial connections, no local database fallback, refused startup, archive
validation, preserved destinations and recovery failure reporting.

The new archive-form/transport tests passed **11 frontend tests**, and TypeScript
`--noEmit` passed.

These were synthetic checks. No live MongoDB, Docker/Coolify deployment, browser
session, full suite or release build was run. Frontend checks used the available
Linux Node **24.21.0**, below the declared **24.22.0** minimum; repeat them on the
supported runtime before release. Database recovery still needs live validation.

</details>

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

Upload-recovery regressions include concurrent inspection after a folder move,
explicit error clearing, cross-tab sending/cancellation, and retained originals
after clearing history. The browser cases in
[`test_browser_transfer_recovery.py`](../tests/browser/test_browser_transfer_recovery.py)
exercise failed-history recovery, scoped resume feedback and compact upload
status on desktop and mobile;
[`test_browser_books_shares.py`](../tests/browser/test_browser_books_shares.py)
includes EPUB anchor navigation. These use synthetic data and do not establish
live Telegram integrity. Run them only with the approval required above.

Upload-load regressions include
[`test_browser_navigation_requests.py`](../tests/browser/test_browser_navigation_requests.py)
for breadcrumb request reuse, obsolete-request cancellation and share-badge
invalidation, and
[`test_catalog_statistics.py`](../tests/storage/test_catalog_statistics.py)
for committed-snapshot reuse and invalidation. Transfer tests also protect
immediate acknowledged-offset persistence, bounded progress notifications,
coalesced jobs refreshes and cross-tab recovery. These checks use synthetic
state; they do not measure a deployment's network, MongoDB or disk latency.

For URL intake changes, the focused offline checks are:

```bash
uv run --frozen --all-groups python -m pytest -W error \
  tests/transfers/test_remote.py tests/transfers/test_remote_ranges.py tests/transfers/test_remote_retries.py \
  tests/transfers/test_remote_scheduler.py tests/transfers/test_transfers.py \
  tests/observability/test_transfer_metrics.py tests/transfers/test_staging_admission.py -q
```

These cover fake HTTP responses, staging admission, advisory progress versus
durable state, cancellation and handoff to fake Telegram. They do not measure
live download speed or confirm a deployment.

For Telegram download admission and request-status changes, the focused offline
regressions are:

```bash
uv run --frozen --all-groups python -m pytest -W error \
  tests/media/test_media_request_fairness.py tests/media/test_book_range_admission.py \
  tests/media/test_file_status.py tests/media/test_media_scheduler.py \
  tests/media/test_media_admission_priority.py tests/media/test_stream_delivery.py \
  tests/media/test_multipart_streaming.py tests/web/test_app.py -q
cd website
npx vitest run src/features/previews/Preview.test.ts
npm run typecheck
```

These exercise one bot sharing distinct cold files, priority and cancellation,
one-slot thumbnail source reads, exact stream delivery, authorized status and
bounded preview retry policy. The image retry/close and video status workflows
are in `tests/browser/test_browser_preview_player.py`. These are synthetic
regressions, not measurements of Telegram throughput. Run checks only with the
approval required by `AGENTS.md`; listing commands here does not record a pass.

For thumbnail regeneration and selective readers, focus on
`tests/media/test_thumbnail_rebuild.py`, `tests/media/test_generated_thumbnails.py`,
`tests/media/test_thumbnails.py`, `tests/media/test_books.py`, `tests/media/test_books_api.py` and
`tests/media/test_multipart_previews.py`. The rebuild tests use generated images/books,
a loopback range bridge and fake Telegram reads, including padded archives that
must not be fetched in full. `tests/browser/test_browser_thumbnail_rebuild.py` covers
confirmation, progress/reload/cancellation and image-based EPUB manga fit/direction.
These synthetic checks do not establish live Telegram throughput or universal
codec/device support.

<details>
<summary>Full lint, tests, security and build checks</summary>

First start a [disposable MongoDB replica set](#opt-in-mongodb-replica-set-tests)
and export its two test variables. The coverage command below needs that opt-in;
never point it at your deployment database.

```bash
uv lock --check
npm run typecheck --prefix website
npm test --prefix website
npm run build --prefix website
uv run --frozen --all-groups ruff check tgdrive tests scripts
uv run --frozen --all-groups ruff format --check tgdrive tests scripts
uv run --frozen --all-groups python -m pytest -W error \
  --cov=tgdrive --cov-report=term-missing
uv run --frozen --all-groups bandit -c pyproject.toml -r tgdrive
uv run --frozen --all-groups pip-audit --strict
npm audit --prefix website --audit-level=high
docker compose --env-file .env.example config --no-env-resolution --quiet
```

Keep the 80% coverage floor. Compose validation checks syntax, not placeholder
credential validity. See the [recorded results](#latest-validation) for the
environments, coverage and known limitations of earlier runs.

</details>

<details>
<summary>Chromium with the synthetic backend</summary>

```bash
uv run --frozen --group e2e playwright install --with-deps chromium
TGDRIVE_E2E=1 uv run --frozen --group e2e python -m pytest \
  -m e2e tests/browser/

# Focused explicit-refresh pending, failure and confirmation checks
TGDRIVE_E2E=1 uv run --frozen --group e2e python -m pytest \
  tests/browser/test_browser_refresh_feedback.py
```

Install Chromium/libraries inside WSL. Evidence stays in ignored `cache/browser-e2e`
and feature folders. These render real UI against a test backend, without proving
live Telegram transfers. Only dedicated tests generate image-heavy PDF fixtures.

For player interaction changes, include `test_browser_player_gestures.py`,
`test_browser_player_mobile.py` and `test_browser_player_settings.py` under
`tests/browser/`. They use decoded media, a generated 65-second H.264/AAC gesture fixture,
320/390 px portrait and 720 px landscape layouts, keyboard and fullscreen checks.
Emulated touch is separate from physical-device/other-browser validation.

`test_browser_player_controls.py` covers measured loading feedback, held delivery
and decoded audio, frame stalls, retries, source-byte retention and buffered
forward seeks during simulated network loss. The pinned-player checks in
`website/scripts/check-movi-player.js` also exercise bitrate scaling, refill
hysteresis, allocation failure, shared/non-shared buffer growth and hardware EOF
drain. `website/src/features/previews/video/videoBufferMemory.test.ts` covers device-hint ceilings and the
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
uv run --frozen --all-groups python -Werror -m pytest tests/storage/test_mongodb.py -q
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

### When GitHub checks run

The [CI workflow](../.github/workflows/ci.yml) keeps routine pushes focused:

| Trigger | Checks |
| --- | --- |
| Push to `main` or `dev` | Quality/security, frontend and backend tests with isolated MongoDB and coverage |
| Pull request | Core checks, plus production-frontend Chromium tests |
| Manual run | Core checks, plus the selected additional suite |

All Python jobs use **3.14.8**, the sole supported runtime pinned in
`.python-version` and the Docker image. There is no older-version test matrix.

Pushes confined to Markdown, `docs/` or `LICENSE` skip automatic CI. Every pull
request runs CI, including documentation-only changes, so required merge checks
can finish. Documentation changes can still affect release contents; verify
custom archives when preparing them for distribution.

Release pull requests should pass `Quality and security`, `Frontend behavior`,
`Backend tests` and `Chromium end-to-end` before merging into `main`.
Repository branch protection is currently disabled; maintainers follow this
release process without GitHub enforcing a merge gate.
Manual container and source-release checks are separate from the merge gate.

In **Actions → CI → Run workflow**, select the branch and additional suite:

| Suite | Additional work |
| --- | --- |
| `core` (default) | None |
| `browser` | Chromium workflows against the synthetic backend |
| `container` | AMD64/ARM64 image builds and local container smoke checks |
| `release` | Container checks, then build/extract/rebuild/test the source ZIP |
| `all` | Browser, container and source-release checks |

Container checks wait for core checks; source verification waits for the
container. Browser failures do not prevent those independent packaging checks
in an `all` run, but every selected check must pass before release. These checks
do not deploy, publish an image or create a GitHub release. Browser evidence and
source candidates are retained as Actions artifacts for 14 days.

This avoids rebuilding Docker images and extracted releases for every push
while keeping installation and packaging verification available before shipping.

### Recorded results

Use these records to understand coverage. Validate the exact release commit and
clean extraction; keep its checksum with the test record.

<details>
<summary>GitHub CI failure review — 2026-10-26</summary>

The push runs for `7a4ecc7`, `2321915` and `acd9ca5` passed frontend behavior
and Python 3.14 tests but failed Bandit: B105 treated the recovery setting
`MAIN_BOT_TOKEN=None` as a hardcoded password. That value clears the optional
credential. A line-specific suppression preserves the value and keeps B105
enabled elsewhere. The exact CI Bandit command passed locally on Python 3.14.8
with no issues after this correction; it emitted unused-suppression warnings
for other keys in the same settings mapping. No GitHub rerun or push was made.

The subsequent `a5a8b1c` push passed frontend checks but
[failed Ruff lint](https://github.com/TechShreyash/TGDrive/actions/runs/40373319271/job/121749557834)
on four long lines and a nested condition from the download-scheduling change.
After wrapping those lines, merging the equivalent condition and formatting one
scheduler lambda, repository-wide Ruff lint passed and its format check reported
**270 files already formatted**. No tests, builds or security audits were rerun
for this correction; GitHub CI has not verified these local edits.

Browser, container and source-release jobs were intentionally skipped on these
pushes. The failed Bandit step also prevented the later Python dependency audit
and frontend security smoke steps from running.

</details>

<details>
<summary>Focused CI repair checks — 2026-10-26</summary>

The local WSL checkout passed **253 affected Python tests** and the **three
previously failing Chromium cases**, plus repository-wide Ruff lint/format,
Bandit, Python/npm dependency audits and the production frontend build.
Two MongoDB receipt tests skipped because the isolated database was not enabled.
The Python audit passed after updating the locked `filelock` dependency to 4.2.4.

These focused results do not establish a passing full GitHub run, full Python
matrix, Docker build or extracted-release run. `actionlint` was unavailable.
The frontend build still reports upstream media-bundle size/CommonJS warnings;
Bandit reports stale suppression warnings without findings.

</details>

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
| App Python | 3.14.8 only |
| uv | 0.13.4 |
| Frontend Node | 24.22.0 LTS |
| Container targets | Linux AMD64/ARM64 |

Exact libraries live in [pyproject.toml](../pyproject.toml), [uv.lock](../uv.lock),
[package.json](../website/package.json), [package-lock.json](../website/package-lock.json)
and [Dockerfile](../Dockerfile). Do not replace locks with unconstrained installs.
Updates need affected integration checks. CI uses Python 3.14.8 only;
configuration alone is not executed evidence.

## Code conventions

Use the existing tool policies: Python follows the Ruff rules and 88-column limit
in `pyproject.toml`; frontend TypeScript uses strict checking and the 80-column
Prettier configuration in `website/.prettierrc.json`. Authored code uses four-space
indentation. `.editorconfig` keeps UTF-8, LF and final newlines consistent across
editors. After approval,
`npm run format:check --prefix website` checks authored frontend formatting;
`npm run format --prefix website` applies it.

- Give each module a focused responsibility and name it after the behavior it owns.
  Import its concrete owner instead of adding forwarding modules or barrel exports.
- Use descriptive names and explicit units for time, byte counts and limits. Type
  service boundaries and structured results; avoid spreading `Any` or type casts.
- Keep components with their feature. Shared code serves multiple features and
  does not import the application shell. Extract independently useful helpers
  when they make policy or control flow clearer.
- Prefer guard clauses and named decisions to deeply nested branches. Comments
  explain intent, invariants and recovery limits; docstrings describe contracts.
- Catch the errors a boundary can handle. Preserve cancellation, request identity
  and durable-state ordering in transfer and recovery code; see the
  [architecture contracts](architecture.md).

## Repository map and test map

| Location | What belongs here |
| --- | --- |
| `tgdrive/` | Backend domain packages; see the [backend code map](architecture.md#components) |
| `website/src/app/` | Application shell, login, navigation and page composition |
| `website/src/features/` | Files, shares, transfers, previews, settings/storage, performance and backup UI |
| `website/src/shared/` | Common API client, types, controls, branding and theme |
| `tests/` | Python tests grouped into domain packages, plus `browser/` |
| `tests/support/` | Shared app fixtures, synthetic server and generated samples |
| `tests/fixtures/` | Committed media fixtures shared by tests |
| `scripts/` | Release packaging, viewer vendoring and brand asset tooling |
| `docs/` | Public guides and their showcase assets |
| `website/public/` | Browser assets, fonts, player and third-party notices |

Keep new code in its owning domain. Frontend styles and `*.test.ts` / `*.test.tsx`
files stay beside the module they exercise. Backend imports use concrete modules,
for example `tgdrive.storage.store`; Python test helpers use qualified imports
such as `tests.support.app_fixtures`. Retain lazy imports where they break
dependency cycles; reusable fixtures belong in support modules, outside test cases.
Use feature folders for cohesive groups of code. Single-screen code belongs with
its existing owner: login in `app/`, and the storage/settings screen in `settings/`.

Use `tgdrive.web.app:create_app` as the ASGI factory and import settings from
`tgdrive.core.config`. `tgdrive/server.py` launches the container; startup handles
initialization and the website provides administration and recovery.
Keep Compose files, the Dockerfile, manifests and lockfiles at the root for tooling.
`website/src/main.tsx` remains the frontend entrypoint.

Runtime state belongs in `data/` and `cache/`; dependencies, bytecode and builds
belong in their ignored generated directories. VS Code hides these generated
folders through `.vscode/settings.json` without removing their contents. Existing
`output/` captures and design drafts are ignored local development evidence;
public showcase media belongs in `docs/assets/`.

<details>
<summary>Choose a focused test area</summary>

| Area | Python test directory |
| --- | --- |
| Lifecycle, configuration and packaging | `tests/core/` |
| HTTP, security, sharing, listing, connections and demo access | `tests/web/` |
| Catalog and MongoDB | `tests/storage/` |
| Telegram clients, bot mode and Premium login | `tests/telegram/` |
| Jobs, uploads, split files and URL imports | `tests/transfers/` |
| Streaming, cache, previews, books and thumbnails | `tests/media/` |
| Checkpoints and database recovery | `tests/recovery/` |
| Independent channel backups and activation | `tests/replication/` |
| Performance counters, logging, request IDs and history | `tests/observability/` |
| Real Chromium with synthetic backend | `tests/browser/` |

The default pytest invocation still discovers `tests/` recursively. Browser tests
remain opt-in through `TGDRIVE_E2E=1`; MongoDB integration tests retain their own
opt-in configuration. For side-effect changes, cover retry, cancellation,
interruption and ownership loss against the [architecture contracts](architecture.md).

</details>

## Frontend contracts

Keep the file manager focused on browsing, transfers and maintenance. Use a
compact toolbar, independently scrolling workspace and restrained bordered
sections. The [checked-in captures](showcase.md) illustrate the interface.

| Concern | Canonical source |
| --- | --- |
| Palette, typography and workspace layout | [Application CSS](../website/src/app/style.css) |
| Shared control styles | [Design-system CSS](../website/src/shared/design-system.css) |
| Mantine provider and component defaults | [DesignSystem.tsx](../website/src/shared/DesignSystem.tsx) |
| Light, dark and system appearance | [theme.ts](../website/src/shared/theme.ts) |
| Brand artwork | [SVG masters](../website/public/static/brand/) |

- Reuse semantic tokens, self-hosted Inter and Lucide icons. Keep logo cobalt
  separate from interface action colors. Use tabular numerals for measurements.
- Use `BrandLogo`; preserve the SVG masters' proportions, openings
  and clear space. Generate raster assets with `scripts/brand-assets.py`.
- Retain native dialog and custom player behavior. Keep shared theme variables
  in bundled external CSS and preserve the content security policy.
- Preserve keyboard focus and saved appearance. Target 4.5:1 text contrast, 3:1
  meaningful control/chart contrast and 44px touch targets. Respect browser zoom.
- Keep menus and controls brief and responsive; avoid decorative entrance effects.
  Honor reduced motion, and keep correctness independent of animation completion.
- Show fresh-request progress and actionable errors. Announce successful saves or
  refreshes only after completion; background polling stays quiet. Keep performance
  measurements honest about missing data and uncertainty, and show preload coverage
  across the available workspace width.

Keep upload byte verification, stable operation IDs and atomic folder preparation
intact. The interaction and accessibility contracts below cover individual flows.

<details>
<summary>Media compatibility and redistribution</summary>

Movi and PDF.js install from committed modified npm archives in
[`website/vendor/`](../website/vendor/README.md). The lockfile records archive
integrity; normal installs, development and builds do not rewrite dependencies.
New upstream releases cannot silently replace these viewer packages. Upgrades
are deliberate: review the upstream source, update the recipes, regenerate with
`scripts/vendor_viewers.py` and run the focused checks. Other dependencies still
come from the locked npm installation; this is not a fully offline build.

[MovTextRenderer.ts](../website/src/features/previews/video/MovTextRenderer.ts) renders MP4 mov_text/tx3g
through the public subtitle extension as inert text: strict UTF-8/BOM UTF-16,
no style/link boxes. Bounds are 128 KiB packets, 16 Ki-character cues, 4096 cues /
2 Mi characters queued, eight visible overlaps. Seeking/track changes clear cues;
Off/source cleanup removes owned DOM/styles. Test visible English/Hindi,
HTML-looking text, seeking/Off and original bytes; MKV/SRT is a separate regression.

[AdvancedVideo.tsx](../website/src/features/previews/video/AdvancedVideo.tsx) adapts Movi 0.4.1 private
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
filenames verbatim, without text transforms. Use **My Files** for TGDrive's root
navigation label.

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
  Retain disclosure state across category changes. Show device progress while any
  local upload still needs the browser, then switch to Telegram progress after
  every remaining file has an accepted server job. Follow the
  [upload display rules](usage.md#what-upload-progress-means); never invent queued timers/ETAs.
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
  -c 'import tgdrive.web.app, telethon, cryptg, argon2; print("runtime imports passed")'
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
Update each guide's **Contents** links when adding, renaming or moving sections.

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
uv run --frozen python -m pytest tests/replication/test_replica_transport.py tests/replication/test_replica.py -q
```

These tests use synthetic Telegram clients; they do not establish live Telegram
rate limits or demonstrate production backup completion. Pacing and retry policy
are documented in the [backup guide](backup-recovery.md#enable-and-confirm-the-first-copy).

For the broader offline backup group, including journal rollback, pause races,
session authorization waits, archive handoff and activation/bootstrap recovery:

```bash
TGDRIVE_MONGO_TESTS=0 uv run --frozen python -m pytest \
  tests/replication/test_replica.py tests/replication/test_replica_transport.py \
  tests/replication/test_replica_state.py tests/replication/test_replica_settings.py \
  tests/replication/test_replica_mongo.py tests/replication/test_replica_activation.py \
  tests/replication/test_replica_activation_mongodb.py tests/replication/test_replica_bootstrap.py \
  tests/replication/test_replica_api.py tests/replication/test_replica_deletion_barrier.py -q
```

The opt-in live MongoDB tests are skipped by this command. It makes no live
Telegram requests and does not replace a recovery rehearsal.

```bash
TGDRIVE_E2E=1 uv run --frozen --group e2e python -m pytest tests/browser/test_browser_channel_backup.py -q
```

This uses real auth/API/UI with a synthetic replica service at 1440/390/320 px.
It covers configuration, progress, pause/retry, exports and explicit recovery
activation, not live copying or independent human-account ownership. Live tests
need disposable private source/target channels and unrelated bots; rehearse both
directions and database-only recovery without risking the only copy.

</details>

### Selective book pages and shared PDF resources

The vendored PDF.js worker keeps image substreams sparse during parsing, defers image
stream bodies in page resource discovery and loads a painted image's dependencies
after dimension/size rejection and before decoding. Its recipe is
`website/scripts/patch-pdf-worker.js`; version/source guards reject unknown inputs
during deliberate vendor regeneration. Normal frontend commands use the committed
modified package. Review the recipe when updating PDF.js. Focused tests live in
`website/scripts/check-pdf-worker.js`; the `shared` case in
`tests/browser/test_browser_pdf_ranges.py` exercises a generated PDF with every page image
in one shared resource dictionary. These are synthetic fixtures, not a guarantee
for all PDFs or devices.

`website/scripts/pdfjs-assets.ts` serves and emits matching local CMaps, standard
fonts, color profiles and decoder WASM with their resource notices. These resources
support additional character sets and image formats without an asset CDN. Keep
sparse transport, page/image/pixel bounds and XFA/script restrictions intact.
Password prompts pass only an in-memory value to PDF.js; passwords are never
stored in browser preferences or sent to a TGDrive password endpoint.

`tests/media/test_books.py` covers unchanged large original page bytes and bounded result
reuse. `tests/media/test_books_api.py` covers access checks on cached/conditional responses.
`website/src/features/previews/books/bookImage.test.ts` covers download progress, errors, retry and cancellation;
the comic error/recovery, large original page and EPUB offscreen-image cases in
`tests/browser/test_browser_books_shares.py` exercise the shipped UI and HTTP path.
