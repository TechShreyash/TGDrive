# Develop and release TGDrive

[Docs home](README.md) · [Architecture](architecture.md) · [Release verification](release.md)

Contributor commands run **inside WSL Ubuntu**, using the Linux checkout and
[supported runtimes](#version-baseline-and-dependency-policy). Develop on `dev`;
normal installations use stable `main` or a published tag through [Setup](setup.md).

Run tests, builds and other validation only when authorized for the current work,
as required by [contributor guidance](../AGENTS.md#ask-before-running-tests-or-validation).

| Task | Start here |
| --- | --- |
| Edit the app | [Install](#install-the-development-environment) · [Run](#run-while-editing) · [Sample content](#preview-with-sample-content) |
| Choose checks | [Validation commands](#validation-commands) · [Live Telegram](#live-telegram-testing) |
| Find the code | [Repository/test map](#repository-map-and-test-map) · [Code conventions](#code-conventions) |
| Preserve behavior | [Frontend](#frontend-contracts) · [Channel backups](#independent-channel-backup-contracts) · [Readers](#selective-book-pages-and-shared-pdf-resources) |
| Prepare a release | [CI](#when-github-checks-run) · [Source artifacts](#source-releases) · [Release procedure](release.md) |
| Understand earlier checks | [Feature snapshots](#latest-validation) · [CI/release evidence](#recorded-results) |
| Update guides or captures | [Documentation policy](#documentation-maintenance) · [Media guide](assets/README.md) |

## Install the development environment

```bash
uv sync --frozen --all-groups --python 3.14.8
npm ci --prefix website
npm run build --prefix website
```

Keep lockfiles. Ordinary tests inject fake resources and need no live Telegram credentials.

Native development needs `aria2c` on `PATH` to perform real torrent imports
(`sudo apt-get install aria2` on Ubuntu). Docker includes it. Torrent unit tests
use synthetic metadata and engine responses; passing them does not establish
live swarm reachability or real Telegram upload behavior.

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

These are historical results for the tested snapshots, **not a passing status for
this checkout**. Focused runs reused applicable passing cases; counts from separate
runs do not describe one final full-suite run. Synthetic Telegram/browser fixtures
exercise behavior without proving provider throughput or physical-device support.
For release evidence, use [CI and release evidence](#ci-and-release-evidence).

<details>
<summary>28 October snapshots: recovery, imports, playback and UI</summary>

| Snapshot | Recorded checks and coverage | Limits |
| --- | --- | --- |
| Durable video posters · 2026-10-28 · working tree with Movi `.2` | 222 focused Python cases, 71 frontend cases, 53 viewer checks and 18 applicable Chromium cases passed across initial and targeted runs; typecheck/build passed. Real FFmpeg checks cover dimensions, no upscaling and paired outputs. Coverage includes upload reply loss/restart/cancellation, poster authorization/cache, SQLite migration, purge, replica/restore mapping, sharp stored images, URL cleanup and saved positions. Browser checks exposed a late representative-frame reset; the ownership fix passed held-seek early Play/Continue/seek and desktop/touch quality restoration. | Telegram and MongoDB transports use fixtures; browser checks use a local synthetic origin. No live poster publication, deployment, full-suite rerun, lint, physical-device or cold-network benchmark. A concurrent `.3` dependency upgrade began after these checks and is not covered. Earlier applicable passing cases were reused; counts do not describe one final full-suite run. |
| Opening-frame previews · 2026-10-28 · `c9ebf7c` plus local repairs | Typecheck/build, 53 viewer checks, 653 frontend tests and 70 applicable synthetic Chromium cases passed across the main run and targeted reruns. Live Chrome showed a sharp paused MP4 opening frame; two MKVs, including the originally reported file, rendered one black opening frame while paused. MP4 Play, Continue and completion worked. The initial Python run passed 2,243 cases plus 36 subtests; two stale test contracts were repaired and their 57-case modules passed. | Python had 87 skips; the full suite was not repeated after fixture repairs. Repository-wide Ruff/Prettier checks still fail; checks for the repaired Python fixtures and new opening-frame test passed. A transient missing-build browser failure passed on rerun. The separate share-copy focus fix passed locally and was not deployed. No physical-phone, Safari/Firefox or universal network/codec guarantee. |
| Background permanent deletion · 2026-10-28 · `9861924` | 112 metadata/job/API cases, six backup-barrier cases and ten isolated MongoDB cases passed across overlapping runs. Eleven frontend and two synthetic Chromium cases, typecheck/build and focused formatting checks passed. Live Chrome: three disposable folders, including a 61-byte file, stayed hidden through reload and tab closure, then completed. Synthetic desktop/320 px captures cover pending, failure and retry states. | Final cleanup was observed about 4½ minutes after confirmation while backup verification ran; this is one observation, not a speed guarantee. No live outage/server-restart injection, physical-phone test, full suite or benchmark. Whole-file Ruff still reports unrelated backup/torrent/recovery issues. |
| Unified recovery files · 2026-10-28 | Initial focused run: 407 passing Python cases; 21 affected/new cases and two final malformed-content/free-space cases passed after repairs. Frontend typecheck and 28 backup UI cases passed. Covers database/channel consistency, legacy retries, replacement credentials, rebuilding, activation and matching file/key exports. | Synthetic fixtures; five opt-in MongoDB activation cases skipped. No live recovery, browser check, production build, deployment or full suite. |
| Unified recovery verification · 2026-10-28 · deployed `c9ebf7c` plus local repairs | Final focused backend group passed 460 cases across its run and one fixture rerun; 46 isolated real MongoDB cases passed, including full v2 restoration and transaction rollback. After the startup-key fix, 30 focused runtime/key/publication cases passed. Production frontend build/typecheck, 39 backup UI units and ten synthetic Chromium cases passed across focused runs. Live checks passed readiness, anonymous/visitor export denial, secret-free status, manual checkpoint creation, file/key download and decryption. The downloaded production snapshot restored matching catalog/settings/history into an isolated MongoDB without replaying unsafe work. All 159 parts in each channel verified for the latest point; three byte/checksum samples matched at an earlier point. | The final synthetic run skipped seven real Mongo cases already exercised separately. Demo mode correctly rejects restore imports; production failover or activation was not induced. Local fixes cover restore-mode reload, startup key durability, confirmed-file retention, stale safety-copy dates and explicit demo restore limits and require deployment. Telegram's verification cooldown was respected before completing the latest check. No full repository run or physical-device guarantee. |
| Backup database retry safety · 2026-10-28 · `a452dae` | 350 offline cases, five isolated MongoDB activation cases and seven synthetic Chromium cases passed, plus typecheck/build and focused Ruff. Live deployment readiness and anonymous-access rejection passed; **Back Up Now** confirmed an encrypted Telegram archive, and **Sync now** reverified the existing independent restore point with no pending revisions. | Existing data/settings retained. No induced production outage or full live failover/restore; isolated faults do not establish recovery from every provider failure. |
| Video Continue · 2026-10-28 | 57 video unit and 55 synthetic Chromium cases passed. A live 17.01 MiB video retained positions across reopen/reload in Chrome and the in-app browser; Continue, ordinary Play, completion cleanup, fullscreen Enter and separate profiles worked. Desktop and 320/390 px portrait/740 px landscape layouts were inspected. Later hour-formatting checks passed 23 units, typecheck and build. | Initial checks reused the prior build and used a short clip. Quality switching/storage failures were offline; no physical phones, Safari/Firefox or native-player resume. Long-video verification is recorded next. |
| Video hour-format deployment · 2026-10-28 | 77 video units, typecheck/build and 43 synthetic Chromium cases passed across focused runs. A live 93-minute video showed **1:00:00**, **1:00:57** and **1:17:15**; the last position survived reopen/reload. Fullscreen Space resumed playback through **1:18:28**. The long label fit 320 px portrait/740 px landscape with a 44 px target. Browser-profile positions remained independent; no console errors were captured. | Two initial browser starts lost assets during a concurrent rebuild; both failed cases passed from an isolated build snapshot. No full repository suite, physical phones, Safari/Firefox, whole-film playback or live hour-long PiP check. |
| Imports/history · 2026-10-28 · `46ffce7` | Null-name history fix passed 23 Python cases. Live magnet, torrent-folder, device-upload, URL and ZIP roundtrips preserved checked bytes; details below. Access/origin rejection, folder idempotency and 320/390 px layouts worked. aria2 1.37.0 startup/RPC/shutdown and a local 362,017-byte magnet retrieval passed. | One small swarm observation; no speed guarantee, restart/outage/disk-pressure test or physical phone. Video integrity sampled head/tail ranges, not the full file. |
| Multi-format extraction · 2026-10-28 | Applicable focused results: 94 service/format/API, 45 frontend and 16 synthetic Chromium cases; typecheck/build on Node 24.22.0. Real local fixtures cover supported suffixes, RAR4/5, ZIP codecs, unsafe entries, corruption, memory/staging limits and repeated cancellation. Desktop/320 px screenshots inspected. | Service tests use fake Telegram; browsers use stubbed jobs and do not verify native codecs. No live multi-format deployment, container/lint/full-suite check or benchmark. Earlier ZIP evidence does not establish deployment of new formats. |
| Multi-format extraction live · 2026-10-28 | All 17 advertised suffixes plus RAR5 and ZIP BZIP2/LZMA passed 20 format/codec cases through real Telegram, with output names and downloaded bytes checked. ZIP roundtrips, Unicode/empty folders, extract-here conflicts, retry identity, cancellation and rejection before publication passed. Desktop UI extraction continued after tab closure; its output preview and bulk-created ZIP were verified. Corruption feedback and browser console checks passed. | Tiny fixtures; API payload traffic including cleanup was about 349 KiB, excluding headers and browser traffic. Only disposable data moved to recoverable Trash; existing root items stayed unchanged. Terminal test history remains because selective clearing is unsupported. No large-archive/load, live restart/outage/disk-pressure or physical-phone check; no full-suite rerun. |
| ZIP creation/extraction · 2026-10-28 · `0c76e03` | Final focused run: 57 backend and 72 frontend cases, typecheck/build and Ruff on 11 Python files. Eight Chromium cases passed across focused runs. Live tiny-fixture creation/extraction preserved names/bytes, empty folders and conflict handling; unsafe paths, empty files and damaged CRCs failed before output. Retry identity, cancellation, anonymous denial and Show in Folder worked. An accepted ZIP finished after its tab closed. | Synthetic browser/service checks and tiny live fixtures; no large-archive stress, server restart, disk pressure, outage, throughput or full-suite test. |
| Torrent/magnet imports · 2026-10-28 | 76 Python, 80 frontend and four synthetic Chromium cases passed, plus typecheck/build. A later 24-case backend run covered hash-check disk accounting and device uploads while torrent slots were busy. | Initial runs used mocked torrent/Telegram transports; later live results are recorded separately above. No full suite, lint or container run for this feature. |
| Idle polling/thumbnail reuse · 2026-10-28 | 168 frontend cases, production build/typecheck and three Chromium cases passed. Coverage includes hidden/idle polling, auth transitions, cross-tab recovery and thumbnail reuse at 1440/390/320 px. | Mocked requests/synthetic backend; no full suite, lint, live providers, benchmark or deployment. |

Builds above retained existing viewer CommonJS and bundle-size warnings.

</details>

<details>
<summary>Live import integrity recorded on 46ffce7</summary>

| Path | Verified result |
| --- | --- |
| Magnet import | 362,017-byte EPUB completed; full original-download SHA-256 matched. |
| Torrent URL/folder | All 11 Sintel names/sizes matched the 129,302,391-byte manifest; ten non-video files matched full hashes. Video HEAD and 65,536-byte head/tail ranges matched source bytes and headers. |
| Device upload | 8,389,120-byte file matched full SHA-256; 8 MiB receipt offset persisted, stale offset returned 409, completion replay kept the same job. Uploading continued while two torrent metadata jobs occupied download slots. |
| Direct URL | 1,101-byte license downloaded from Telegram matched its public source. |
| ZIP | 363,456-byte ZIP had expected entries, valid CRCs and matching member hashes; both extracted files matched. |

The EPUB rendered/changed chapters and the video played. Finished history showed
one folder-torrent task and **Show in Folder** opened its root. Cancellation
settled both test jobs, removed staging and left no aria2 processes. Existing
files/settings were preserved. These observations do not establish universal
throughput or full-video integrity.

</details>

<details>
<summary>Earlier snapshots · 23–27 October 2026</summary>

| Snapshot | Recorded results | Scope and limits |
| --- | --- | --- |
| Upload responsiveness · 2026-10-27 | 168 Python, 123 frontend and five Chromium cases; typecheck/build. | Offline journals, progress, catalog statistics, navigation reuse and share invalidation; no full suite, lint, live providers, benchmark or deployment. |
| Reader HTTP compression · 2026-10-26 | 12 `bookImage.test.ts` cases. | Mocked Fetch: decoded compression, unknown totals, truncation and 64 MiB cancellation; no browser/proxy check, full suite, typecheck or build. |
| Durability/concurrency audit · 2026-10-26 | 1,930 offline cases + 36 subtests, 78 opt-in skips; repaired fixture's 12-case module and ten extra journal cases passed. MongoDB parity: 98 cases. Four Chromium cases, build/typecheck, Ruff on 264 Python files and edited frontend formatting passed. | Isolated/synthetic data, including disposable MongoDB; no deployment, live Telegram or hard-kill recovery. Complete offline run included Vitest through the Python bridge. |
| Repository/viewer/settings cleanup · 2026-10-26 | 1,908 offline cases + 36 subtests; fixture repairs and an extra authorization regression passed. 287 Chromium cases across full/focused reruns; 103 MongoDB cases including 43 live database cases. Build, 28 viewer cases, Ruff on 267 files, formatting, Bandit, lock/dependency audits and local link checks passed. AMD64 Docker/offline smoke checks and 522 source-manifest hashes passed; clean extraction installed/built. | Node 24.22.0/Python 3.14.8; 78 opt-in skips checked separately. Applicable passes reused after repairs. No live Telegram, production startup/deployment, ARM64 or physical devices. Viewer packages came from integrity-checked upstream archives. |
| MongoDB-only startup/archive recovery · 2026-10-26 | 257 backend cases across initial/failure reruns; 11 archive-form/transport frontend cases and TypeScript passed. | Synthetic startup, preserved-state and archive validation; no live MongoDB, browser, Docker/Coolify, full suite or release build. Frontend used Node 24.21.0, below minimum; repeat on supported runtime before release. |
| URL intake/progress · 2026-10-23 | 156 backend + 24 subtests, 88 frontend and a corrected 15-case metrics module; typecheck, focused Ruff and Prettier passed. | Synthetic ranges, fallback, cancellation, admission, pacing, storage windows and lost responses; no live downloads, benchmark, production build, browser check or deployment. |
| Shared Links/owner sessions · 2026-10-23 | 201 backend, 335 frontend and ten Chromium cases; typecheck/build. | Synthetic UI and legacy SQLite/mocked-MongoDB session fixtures: independent logins/logout, link scope/expiry/edit/revoke and recovery confirmation. No live deployment or universal browser/device coverage. |
| Authentication/shares/outbound requests · 2026-10-23 | 170 focused cases + 24 subtests, focused Ruff, Bandit and locked Python/npm audits passed. | Automated SQLite/mocked-MongoDB checks, including IP/redirect validation; no live database, HTTPS/proxy/provider check or independent penetration test. |
| Adaptive read-ahead/Preloads | 1,479 offline Python (55 skipped, 15 subtests), 179 strict backend, 321 frontend and eight Chromium workflows; typecheck/build and focused Ruff passed. | Fake Telegram/synthetic ranges, cache races and responsive UI; no live-speed measurement or evidence for later artifacts. |

</details>

## Validation commands

Run relevant checks for a small change. Before release, run full validation and
[artifact verification](release.md). Coverage requires opt-in isolated MongoDB;
offline tests alone do not sufficiently exercise the production database.
Get approval for the current checks under [contributor guidance](../AGENTS.md#ask-before-running-tests-or-validation).
Commands below record no pass. Mocked requests and synthetic backends do not
measure live Telegram/MongoDB throughput or certify deployment.

| Change | Checks to include |
| --- | --- |
| Backend behavior | Focused Python tests; MongoDB tests for persistence changes |
| Interface | Typecheck, frontend tests/build and relevant Chromium workflows |
| Transfer/recovery | Retry, cancellation, interruption and ownership-loss cases |
| Release | Full checks below, then a clean extracted archive |

| Focused area | Where to look |
| --- | --- |
| Upload recovery, folder moves, errors and cross-tab cancellation | [`test_browser_transfer_recovery.py`](../tests/browser/test_browser_transfer_recovery.py) and transfer journal/receipt tests; preserve acknowledged offsets and source files after history cleanup. |
| Navigation reuse, stale requests and share indicators | [`test_browser_navigation_requests.py`](../tests/browser/test_browser_navigation_requests.py) and [`test_catalog_statistics.py`](../tests/storage/test_catalog_statistics.py). |
| Idle/hidden polling and thumbnail scroll reuse | `website/src/app/statusPolling.test.ts`, `website/src/features/transfers/jobPolling.test.ts`, `tests/browser/test_browser_file_views.py`; sending/retry correctness stays independent of polling. |
| Thumbnail regeneration and selective readers | `tests/media/test_thumbnail_rebuild.py`, `test_generated_thumbnails.py`, `test_thumbnails.py`, `test_books.py`, `test_books_api.py`, `test_multipart_previews.py`; synthetic browser flows in `test_browser_thumbnail_rebuild.py` and `test_browser_books_shares.py`. |

<details>
<summary>Focused archive, URL, torrent and download commands</summary>

For archive work, use the service/format/API and UI checks after approval:

```bash
uv run --frozen --all-groups python -m pytest -q \
  tests/transfers/test_archives.py tests/transfers/test_archive_formats.py tests/web/test_archives.py
cd website
npx vitest run src/features/files/archiveApi.test.ts src/features/transfers/archiveJobs.test.ts
npm run build
cd ..
TGDRIVE_E2E=1 uv run --frozen --group e2e python -m pytest -q \
  tests/browser/test_browser_archives.py
```

For URL intake changes, the focused offline checks are:

```bash
uv run --frozen --all-groups python -m pytest -W error \
  tests/transfers/test_remote.py tests/transfers/test_remote_ranges.py tests/transfers/test_remote_retries.py \
  tests/transfers/test_remote_scheduler.py tests/transfers/test_transfers.py \
  tests/observability/test_transfer_metrics.py tests/transfers/test_staging_admission.py -q
```

These cover fake HTTP responses, staging admission, progress versus durable state,
cancellation and handoff to fake Telegram.

For torrent imports, include engine, destination, staging and history recovery:

```bash
.venv/bin/python -m pytest -q tests/transfers/test_torrent.py \
  tests/transfers/test_torrent_imports.py tests/web/test_torrent_imports.py \
  tests/transfers/test_staging_admission.py tests/transfers/test_remote_job_recovery.py \
  tests/transfers/test_upload_destinations.py tests/transfers/test_transfer_history.py
npm run build --prefix website
TGDRIVE_E2E=1 .venv/bin/python -m pytest -q tests/browser/test_browser_torrent_import.py \
  tests/browser/test_browser_e2e.py::test_keyboard_dialog_focus_and_unicode_search
```

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

These exercise cold-file sharing, priority/cancellation, thumbnail source reads,
exact delivery, authorized status and bounded retries. UI retry/close and video
status cases are in `tests/browser/test_browser_preview_player.py`.

</details>

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

# Local watch-position persistence, Continue, completion and browser isolation
TGDRIVE_E2E=1 uv run --frozen --group e2e python -m pytest \
  tests/browser/test_browser_player_resume.py
```

Install Chromium/libraries inside WSL. Evidence stays in ignored `cache/browser-e2e`
and feature folders. These render real UI against a test backend, without proving
live Telegram transfers. Only dedicated tests generate image-heavy PDF fixtures.

For player interaction changes, include `test_browser_player_gestures.py`,
`test_browser_player_mobile.py` and `test_browser_player_settings.py` under
`tests/browser/`. They use decoded media, a generated 65-second H.264/AAC gesture fixture,
320/390 px portrait and 720 px landscape layouts, keyboard and fullscreen checks.
Emulated touch is separate from physical-device/other-browser validation.

`test_browser_player_resume.py` covers local watch positions across preview close
and reload, explicit Continue versus normal Play, completion cleanup and separate
browser profiles. `website/src/features/previews/video/videoWatchProgress.test.ts`
covers invalid positions and unavailable/full browser storage.

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

<a id="opt-in-mongodb-replica-set-tests"></a>

<details>
<summary>Opt-in MongoDB replica-set tests</summary>

Use a disposable local replica set or a dedicated test URI:

```bash
docker run -d --name tgdrive-test-mongo -p 127.0.0.1:27017:27017 \
  mongo:8.0 --replSet tgdrive-test --bind_ip_all
until docker exec -e HOME=/tmp tgdrive-test-mongo mongosh --quiet --eval 'db.adminCommand({ping:1})'; do sleep 1; done
docker exec -e HOME=/tmp tgdrive-test-mongo mongosh --quiet --eval \
  'rs.initiate({_id:"tgdrive-test",members:[{_id:0,host:"localhost:27017"}]})'
until docker exec -e HOME=/tmp tgdrive-test-mongo mongosh --quiet --eval \
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
| Pull request | Core checks and production-frontend Chromium tests; Docker/runtime dependency changes also build and smoke-check the AMD64 container |
| Manual run | Core checks, plus the selected additional suite |

All Python jobs use **3.14.8**, the sole supported runtime pinned in
`.python-version` and the Docker image. There is no older-version test matrix.
The backend job temporarily uses Ubuntu 22.04 for its older kernel. The pinned
MongoDB 8.0.34 image rejects some newer Ubuntu runner kernels; its
[Ubuntu kernel-detection fix](https://jira.mongodb.org/browse/SERVER-131779)
is assigned to 8.0.35. Return this job to 24.04 after verifying a compatible image,
before [22.04 retires on 17 April 2027](https://github.com/actions/runner-images/issues/14254).
A startup failure records the container state and logs before stopping;
application tests require a writable isolated replica set.
Its shell probes keep their temporary home in `/tmp`, outside the entrypoint's
data-directory ownership pass, so short-lived shell lock files cannot abort startup.

Pushes confined to Markdown, `docs/` or `LICENSE` skip automatic CI. Every pull
request runs CI, including documentation-only changes, so required merge checks
can finish. Documentation changes can still affect release contents; verify
custom archives when preparing them for distribution.

Release pull requests should pass `Quality and security`, `Frontend behavior`,
`Backend tests` and `Chromium end-to-end` before merging into `main`.
Repository branch protection is currently disabled; maintainers follow this
release process without GitHub enforcing a merge gate.
PRs that change `Dockerfile`, `.dockerignore`, `.python-version`, Python or
frontend manifests/locks, `website/vendor/` or the CI workflow also need a passing
`Container build`. This checks the locked install and bundled assets in the
AMD64 image; source-only changes do not trigger it. Full AMD64/ARM64 container
and source-release checks remain manual release checks.

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

These records describe earlier revisions. Validate the exact release commit and,
when distributing an archive, its clean extraction; retain its checksum with the
record. Workflow definitions do not prove execution.

<details>
<summary>CI repairs and release evidence · 23–26 October 2026</summary>

| Record | Result and boundary |
| --- | --- |
| Failed CI · 2026-10-26 | Pushes `7a4ecc7`, `2321915`, `acd9ca5` passed frontend/Python checks but failed Bandit B105 on optional `MAIN_BOT_TOKEN=None`. A narrow suppression fixed the false positive locally; unused-suppression warnings remained. `a5a8b1c` later [failed Ruff](https://github.com/TechShreyash/TGDrive/actions/runs/40373319271/job/121749557834); local lint/format then passed on 270 files. No GitHub rerun verified those repairs. Browser/container/source jobs were skipped; Bandit failure prevented later audit/security steps. |
| Focused repair checks · 2026-10-26 | 253 affected Python and three previously failing Chromium cases passed, plus Ruff, Bandit, Python/npm audits and production build. Two MongoDB receipt tests skipped; Python audit passed with locked `filelock` 4.2.4. No full GitHub/Python, Docker or extracted-release run; `actionlint` unavailable. Existing media build and stale-suppression warnings remained. |
| Self-hosting snapshot · 2026-10-23 | 1,394 Python cases with isolated MongoDB/84.07% coverage, 255 frontend and 53 Chromium cases passed, plus container/runtime/certificate checks, encrypted-archive restore into an empty database and lint/security audits. No live Coolify deployment or real Telegram credentials. |
| Separate live original-byte evidence | A 4,562,805,302-byte file reconstructed from three Telegram documents with matching SHA-256 and boundary ranges. This establishes the tested original-byte path, not universal playback speed. |

Physical devices, multi-architecture execution, outages and Premium speedups need
[separate evidence](roadmap.md#before-public-release).

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

### Dependabot updates

[Dependabot](../.github/dependabot.yml) schedules routine update checks against
`dev` on Mondays between 04:00 and 04:45 Asia/Kolkata. Minor and patch updates are
grouped per ecosystem; other major updates stay separate for review. Each of the
four ecosystems allows at most two open routine PRs targeting `dev`, eight in
total. Grouping reduces review work; it does not establish compatibility or
merge updates automatically.

Docker Python stays at the baseline above so its image matches the project and
CI pins; same-version digest updates remain eligible. Docker Node and
`@types/node` stay below 25. Change these constraints together with the runtime
pins, locks and CI when deliberately upgrading the supported versions. Dependabot
does not track the uv image in `COPY --from`; update it manually alongside CI's
`UV_VERSION`.

GitHub reads the configuration from default `main`, so edits on `dev` take effect
after release to `main`. Routine updates follow the versioned release process.
[Security updates](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#target-branch)
still target `main` and have separate limits; these `dev` rules do not configure
or enable security updates. Keep repository security updates enabled, review
their fixes promptly and carry merged fixes back to `dev`.

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

Movi revision `0.4.1-tgdrive.3` composes the source-window recipe with
`movi-player-hardening.js`, `movi-player-decoding.js` and
`movi-player-preview-cache.js`; the vendor manifest
records every recipe file's hash. Its focused Node checks exercise actual
patched methods, including hostile track metadata, listener/observer ownership,
cancelled thumbnail waits, audio-only readiness, software EOF/backward seeks and
decoded/paused-packet memory admission. EOF draining uses the existing WASM ABI;
it does not establish an independent decoder rebuild.

`check-movi-preview-cache.js` exercises the actual source readers: a real preview
range can satisfy later playback, sequential requests skip retained intervals,
holes and partial responses never become invented media, and identity checks,
byte/entry limits, close cleanup and the per-image requested-range budget hold.
Reused bytes must not increase network delivery counters or throughput samples.

`test_browser_player_hardening.py` covers real AAC-only MP4 playback/seeking,
malicious MKV metadata and repeated playing-close cleanup. The preview tests in
`test_browser_player_previews.py` check decoded image pixels, visible chapter
work, cached-byte reuse, Object URL cleanup and stopping work when closed or
playback is busy. Keep the existing keyboard/mobile/fullscreen chapter regression.
These are synthetic local-origin checks; remote uncached preview latency and
physical-device GPU/memory behaviour need separate measurements.

Revision `.2` validation on 2026-10-28 passed 49 player recipe checks, 77 distinct
focused frontend unit tests, 20 selected Chromium browser cases and 17 source
release-packaging checks; the frontend typecheck/build also passed. Real images
were inspected for hover and chapter previews. The cached fixture needed zero
additional file payload reads for previews. An isolated software-decoder check
presented all 120 B-frame fixture pictures and retained forward/backward playback
with the new queue admission. These checks used local synthetic origins, without
live Telegram or real GPU/mobile calibration; no independent WASM rebuild ran.

Revision `.3` validation on 2026-10-28 passed 65 player/cache recipe checks,
95 video unit tests, 12 focused synthetic Chromium cases and 17 release-package
checks; the frontend typecheck/build passed. Browser cases include hover and
chapter pixels, keyboard opening with the pointer over the seek bar, native
dragging, pause/resume, track changes, real buffered ranges and close cleanup.
The build used an isolated output directory to avoid other development builds.
No `.3` live deployment, remote bandwidth benchmark or physical-device check ran.

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
(cd dist && sha256sum --check tgdrive-2.2.0.zip.sha256)
unzip -l dist/tgdrive-2.2.0.zip
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

## Selective book pages and shared PDF resources

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
