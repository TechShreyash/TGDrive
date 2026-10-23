# Changelog

## 2.0.0 - public beta candidate

This release rebuilds TGDrive as a self-hosted, single-owner drive. Run one
service instance and one Uvicorn worker. No public deployment is implied by
this candidate.

### Permanent deletion

- Remove file/folder references and shares from the database while retaining
  original Telegram messages, for every file age. Bots cannot delete channel
  messages older than 48 hours even with administrator permissions.
- Complete retried legacy deletion intents without further Telegram deletion;
  retain durable jobs, lineage checks and independent-backup protection.
- Explain retained Telegram copies in the confirmation dialog and document the
  policy, backup implications and separate manual removal of originals.

### Deployment

- Use `/compose.yaml` directly in Coolify with six initial credential fields;
  local Docker commands automatically load the loopback-port override.
- Discover the internal Coolify proxy without hardcoded Docker subnets.
- Generate private deployment keys once for new drives and reuse existing
  MongoDB bootstrap keys, owner hashes and browser sessions during migration.
- Keep new containers accessible for explicit initialization and explain
  database ownership conflicts without resetting existing state.
- Retain `/compose.coolify.yaml` for existing applications.

### Reliability and Performance Controls

- Add operator-owned RAM/disk budgets, free-space reserves, frequency-aware
  retention, shared block fills and bounded demand-first adaptive preloading.
- Prepare complete files manually and inspect all retained byte ranges in
  Preloads; cancel work or clear selected data without deleting originals.
- Pipeline Telethon upload parts with shared per-account request budgets,
  adaptive FloodWait feedback and retries of individual unfinished parts.
- Queue origin work with demand priority and estimated waits, while cached
  viewers do not consume Telegram origin slots. Add process-local traffic,
  server/cache and per-account diagnostics in Performance.
- Add resumable manager links, accurate range/length/filename headers,
  stronger conditional requests and fresh revocation checks during delivery.
- Correct false player buffering, chapter labels, keyboard controls, modal
  copy notices, mobile layout and device-local floating upload notices.
- Preserve URL source identity across staging/recovery, support configurable
  future split sizes, default displayed times to IST and explain all controls.

### Playback Performance

- Clear alternative-service advertisements by default so normal installations
  avoid the reproduced slow HTTP/3 route without manual proxy rules. Preserve
  HTTPS, byte-range delivery and application authorization.
- Stripe ordered stream windows across healthy bots and Premium clients, with
  configured reads per account and bounded read-ahead for ordinary and multipart files.
- Share cached blocks across accounts and requests; retain a bounded private disk
  cache across restarts while checking authorization and message identity.
- Respect read FloodWaits instead of treating them as expired file references.
- Increase the player's requested source buffer to 32 MiB.
- Reclaim suspended responses' speculative frames so fully cached seeks and
  metadata reads cannot be starved by an older download's read-ahead. Keep
  shared origin fills and cancellation cleanup inside the global payload bound.
- Keep the custom player visible while opening/seeking; remove the separate
  Start Playback gate and native-browser Compatibility Playback fallback.
- Show measured receive progress, estimated buffered coverage/ahead time and
  loaded timeline sections, including while paused. Keep mobile collapsed
  track controls out of the touch/focus path.
- Exercise actual pooled RAM/disk delivery in browser fixtures and add stalled
  TCP/ASGI, concurrent-range, disconnect/resume and multipart regressions.

### UI Audit

- Fix mobile drawer focus containment, Escape/close behavior and background input.
- Correct shared-icon accessibility labels and light-theme selected-filter contrast.
- Enlarge small-screen hit areas, expose mixed selection and reset pagination scroll.
- Compact Move destinations and disable moves to the current parent.
- Add failed-load retry actions and inline share-revocation errors.
- Preserve expanded upload groups when their section changes.
- Prevent ambiguous file drops outside My Files and accidental directory flattening.
- Memoize file sorting/share lookup; keep reduced-motion loading feedback intact.

### UI Polish

- Stop old completed transfers reopening the floating panel; minimize active
  uploads on mobile, suppress floating popups on management pages, and quietly
  dismiss completed popups.
- Restrict the mobile New button to My Files and hide it during dialogs.
- Attach sharing icons to filenames, align the Name column heading, enlarge
  mobile row actions and replace the Move conflict select with explicit choices.

### UI Copy

- Use consistent title capitalization for fixed navigation, headings, field labels,
  menu actions and buttons; keep all user file/folder names unchanged.
- Normalize only the generated My Files location prefix, including older API responses.

### Shared Links

- Add compact, responsive Active/Expired/All views with file types, sizes, current
  parent directories, creation/expiry dates and copy/location actions.
- Re-copy new links without changing their token; preserve older random-token
  links and offer explicit replacement when their original URL cannot be recovered.
- Confirm revocation, renew expired entries, and show sharing indicators in My files,
  including inherited folder access. Explicit links follow moves and renames.

### Upload scheduling

- Remove the fixed two-part limit for a single large file. Scale workers to
  eligible client slots and live per-bot limits; admit Premium clients for
  bot-compatible parts while preserving retry sender identity and cooldown gates.

- Default to one simultaneous Telegram media upload per bot; add bots to add slots.
- Add Settings → Uploads with a persistent 1–4 uploads-per-bot control, applied
  without restarting or interrupting running transfers.
- Receive device files independently of Telegram backlog; dispatch each ready file
  immediately and automatically wait/retry when temporary storage is full.
- Keep account/channel FloodWait deadlines, fair leasing and publication pacing.

### Transfer history

- Separate group disclosure from Show Folder; rapid expand/collapse no longer
  navigates. Use a neutral folder header with adjacent file count and compact
  children, omitting repeated parent paths and completion dates.


- Keep each folder/multi-file upload in one status section and preserve its children
  when filtering. Running takes priority; Completed requires every file to finish.


- Align multi-file and folder summaries as compact expandable rows, retain
  one-file folder identity, indent child files, and omit uniform-status filters.
- Use responsive filename/location/size/date columns on wide transfer pages,
  with truncated long text and compact stacked layouts in smaller spaces.
- Group Running, Queued, Needs attention, Completed and Cancelled work in compact,
  unboxed sections; omit finished percentages, repeated byte counters and elapsed times.
- Show current TGDrive destinations. Double-click or Show in folder follows the
  exact uploaded item through moves and renames, selects its file-list page,
  and keeps previews in the normal file browser.
- Handle missing/trashed items without guessing a same-named file.

- Add All/Queued/Running/Completed/Failed/Cancelled views, dates and cursor-paged
  server history that survives live-job cleanup and browser-panel dismissal.
- Archive compact resolved summaries in MongoDB without private recovery context.
- Remove the Transfers upload action; New opens My files before choosing uploads.

### Transfer progress

- Show sizes with up to two decimals, compact shared-unit progress counters and a
  remaining-byte hint near completion, including when displayed totals round equal.


- Distinguish bytes sent across unfinished multipart uploads from confirmed
  channel messages; show the confirmed part count in the active phase.


- Keep received-byte progress while checking a reselected original; exclude local
  hashing from upload speed and resume from the acknowledged offset.
- Put the original-file recovery button beside its message. Restrict transfer
  location navigation to completed uploads and completed folder groups.

- Confirm Cancel all, remove the duplicate panel upload button, and label
  Hide/Show and Clear finished controls.
- Keep database/backup status on its page instead of repeating it in the header.
- Show single uploads directly, without a duplicate expandable file list.
- Start Telegram-stage progress at zero and keep its byte counter in sync.
- Include in-progress Telegram bytes in multi-file totals instead of counting
  only fully completed files or adding browser receipt bytes to the percentage.

### Documentation

- Simplify first-time setup, everyday tasks and recovery instructions; keep technical
  contracts in expandable reference sections with a plain-language glossary.

### Connections

- Connections Refresh now verifies saved credentials and storage-channel permissions
  for enabled clients, updates names, and shows individual background check results
  with bounded concurrency and Telegram-aware retry delays.

### MongoDB persistence

- Use native MongoDB catalog transactions and a committed immutable read cache,
  with majority writes, unique active sibling names and distributed ownership fencing.
- Persist jobs, upload receipts, owner sessions, managed credentials, bootstrap
  settings, bot names and bot-mode preferences in MongoDB.
- Reopen the same cloud database after reinstall; no local catalog recovery or
  per-upload Telegram checkpoint is required.
- Upload consistent, complete encrypted MongoDB snapshots to Telegram in the
  background, with separate recovery-key download and empty-database restore.
- Start a fresh MongoDB test catalog without importing the old SQLite data.
  Retain legacy SQLite code only for compatibility paths/tests.
- Update guided setup, web status and canonical documentation for MongoDB.

### Storage and transfers

The earlier SQLite/checkpoint bullets below describe the initial rebuild and
are superseded by the MongoDB changes above; transport features remain current.

- Replace mutable serialized object metadata with normalized SQLite tables,
  WAL transactions, stable parent IDs, hierarchy constraints, and unique names.
- Persist job state, operation identifiers, browser receipt offsets, and staged
  files across restarts. Bound upload staging, free-space use, transfer work,
  and simultaneous streams.
- Add chunked browser upload receipt with explicit resume/reselection behavior,
  an upload center, additional queued files, and per-file cancellation.
- Use Telethon 1.x and cryptg behind a Telegram adapter. Support storage bots,
  optional Premium accounts, media copying, download streaming, and durable
  retry reconciliation.
- Record permanent-deletion intents so failed database removal can be retried
  safely. Preflight whole delete selections; commit v1 move/trash/
  restore batches atomically, including names, revisions and share revocations.
- Bind pending receipts/jobs to stable destination folder IDs across ancestor
  moves and restarts; reject unavailable destinations without recreating folders.
- Retry a URL job's unpublished local staging safely after a handoff crash.
  Preserve staged bytes and confirmed part identities on transient catalog faults;
  distinguish retryable errors from an unavailable destination's cancellation.
- Restore account/channel and backup publication cooldowns before guided setup
  starts clients, including waits retained by failed jobs.
- Prepare folder-upload hierarchy atomically with durable request fingerprints,
  a keep-both root, bounded validated paths, and per-file resumable transfer
  destinations. Retry a lost preparation response without creating a second root.
- Group multi-file/folder selections into persistent, collapsed transfer batches
  with attention-first detail, filtering, completed counts and truthful aggregate
  progress. Retain unfinished-batch siblings and support bulk original reselection.
- Limit browser receipt work to one active file and stop new receipts while four
  nonterminal server media jobs are outstanding; retain the default two normal
  Telegram media slots and separate serialized checkpoint publication.
- Publish immutable, pinned JSON checkpoints whose marker binds stable drive
  identity, channel, revision and SHA-256. Verify pin/readback before reporting
  protection; checkpoint retries preserve sender/request identity.
- Make durable uploads complete only after a checkpoint covers their catalog
  commit. Checkpoint failure/restart retries protection without resending media;
  committed files cannot be cancelled as unsent uploads.
- Add recover to discover the latest pinned checkpoint with fresh credentials
  and no local checkpoint ID/entity cache. Explicit restore still supports known
  message IDs/offline JSON, refuses existing destinations, and revokes old shares.
- Migrate supported SQLite catalogs to v4, preserving old message IDs and adding
  ordered logical-file parts, folder preparations and stable drive/channel
  identity. Portable JSON uses v4 and accepts supported older checkpoints.
- Store files above a bot document's limit as durable ordered parts with per-part
  and whole-file checksums. Preserve one visible item/name/share URL and serve
  transparent full or cross-part range downloads; cancellation/deletion and
  recovery retain every part's identity.
- Detect stale/different catalog lineage before recovering jobs, keep metadata
  readable, and block mutations/new media access until explicit recovery. Check
  fresh lineage before destructive Telegram effects and retain the catalog's
  channel binding across portable recovery.

### Guided installation

- Add python3 scripts/setup.py for Linux/WSL Docker hosts with Python 3.10+,
  Compose and Buildx. Diagnose prerequisites without installing host packages.
- Privately prompt for Telegram credentials/channel ID or private message link
  and a chosen web password; store Argon2id, generate SESSION_SECRET and preserve
  existing .env rather than overwriting it.
- Require explicit New, Recover or Reuse, verify Telegram checkpoint protection,
  preserve existing data, and keep manual Docker/source installation available.

### Authentication and interface

- Establish one original TGDrive visual system with local Inter Variable under
  OFL, an azure accent, neutral light/charcoal semantic themes and consistent
  Lucide icons. Anchor the app shell and independently scroll its workspace.
- Add Light/Dark/System appearance with designed radio choices, browser preference
  persistence and a consistently dark media stage/white PDF paper.
- Separate Connections, Storage & backup and Settings, with inline secret
  replacement only while editing, compact health/usage summaries and focused
  Appearance/Sign-in controls.
- Debounce literal global filename search, abort stale requests, show result
  locations and server-sorted result pages, and preserve origin/history on clear
  or Back. Select files and folders with one click, open with double-click/two
  taps/Enter, and reveal checkboxes only in selection mode.
- Keep creation/upload actions in New instead of a repeated heading toolbar;
  use a compact keyboard sort menu and sortable desktop columns. New also supports
  keyboard action focus/navigation, Escape and focus return on desktop/mobile.
- Shorten folder confirmation, hide clocks for wholly queued batches, and show
  permanent deletion as one operation notice with failure details/retry.
- Show verified Telegram account names/usernames from a private local SQLite
  cache, seed it when adding accounts, populate missing names in the background,
  and provide web refresh with bounded retry and persisted FloodWait deadlines.
  Preserve local labels, stable sender identities and connection revisions.
- Add owner connection management for API ID/write-only hash and up to 16
  storage clients, individual running health, catalog/staging/job statistics,
  and complete-candidate revision/preflight checks. Save private mode-0600
  connection overrides and fence new mutations until restart; retain read access.
- Add owner-bound Premium phone/code/optional two-step authentication without
  returning session credentials; keep StringSession import as an advanced path.
  Expiry/cancel/failure discard unsaved login authorizations. Actual Premium
  authentication/capacity still needs live validation with an eligible account.
- Open supported file previews in a full-window viewer with a fixed header, desktop
  double-click, keyboard/touch access, Back/Forward and restored focus.
- Use continuous PDF scrolling with selectable text and zoom/fit controls;
  remove page-flip buttons, Page text and the small preview popup. Keep PDF
  controls in the filename header instead of a second permanent toolbar.
- Distinguish TypeScript .ts/.mts from MPEG-TS video using bounded content evidence,
  with MIME corroboration and inert .cts source support. Reject binary/invalid
  text encodings rather than displaying garbled source; preserve original bytes.
- Detect the bundled decoder's unavailable-codec state rather than showing a
  black preview with an advancing clock, and offer a compact original-download
  fallback. Permit the trusted worker's data-JPEG conversion under CSP.
- Verify all 68 external sample originals across 64 extensions with actual UI
  download hashes. Record 47 rendered/played previews and 21 honest format/codec
  fallbacks; keep Chrome-only and representative-workflow scope explicit.
- Tighten file-browser spacing, clarify selected rows and label dates as Added.
- Show unsupported formats in compact details/download dialogs, and expose
  current catalog-backup protection plus Back up now in Storage & backup.
- Use the enhanced player by default for video previews, with automatic native
  fallback. Keep audio/subtitle selection inside the player's menus and remove
  the duplicated external controls and player-mode switch.
- Render MP4 mov_text/tx3g through the player's public subtitle extension with
  bounded strict Unicode decoding and inert text. Verify visible cues, seek,
  track switching/Off and original integrity alongside existing MKV/SRT playback.
- Add animated, accessible startup/buffering feedback, true readiness signals,
  reduced-motion support and manual recovery for long waits without a false
  timeout failure.
- Add Argon2id owner-password hashes, expiring HTTP-only sessions, and browser
  mutation-origin checks.
- Reject empty/whitespace-only plaintext credentials unless a valid owner hash
  is configured.
- Add expiring/revocable bearer shares stored as hashes.
- Rebuild the web interface with React, TypeScript, and Vite, with typed API
  calls, navigation, search, trash, sharing, and transfer status.
- Restrict bot mode to configured administrator IDs.
- Validate remote HTTP(S) destinations and redirects, enforce byte/time limits,
  redact secret values and URLs, and expose controlled health/readiness.

### Packaging and verification

- Use Python 3.14.8 and Node 24.22.0 LTS with locked dependencies and pinned
  container image digests. Node is used only for frontend builds.
- Add CI for Python/frontend checks, security audits, browser tests, container
  builds for AMD64/ARM64, and extracted-source verification.
- Run Docker as an unprivileged user with persistent metadata and staging
  volumes, a health check, and graceful shutdown.
- Build a reproducible source ZIP with a per-file SHA-256 manifest, excluding
  credentials, sessions, databases, uploads, build output, and private documents.

### Upgrade notes

- Back up the entire persistent data and staging volumes before upgrading.
  Restore and verify a checkpoint in a separate directory.
- Existing drive.data metadata requires the explicit restricted offline
  converter. Existing JSON checkpoints use the owner restore command.
  Startup never automatically unpickles or replaces local metadata. Missing
  metadata now always requires explicit recover/restore/init, even with the
  legacy INITIALIZE_EMPTY_DRIVE flag enabled. Channel checkpoint publication
  also needs administrator edit_messages rights for pinning.
- Old Pyrogram/Kurigram session strings cannot be reused with Telethon.
  Authorize new user sessions locally. Legacy share secrets are discarded.
- Browsers cannot restore selected local file bytes after reload; reselect
  the same original file to continue an incomplete browser receipt.
- A Telegram retry may retransmit the staged file. Browser chunk resumability
  does not guarantee persistence of Telegram's temporary upload parts.
- Recovery checkpoints contain catalog metadata, not credentials, staged bytes
  or media payload copies. Regain access to the same Telegram channel and retained
  file messages; keep full deployment backups for unfinished work/configuration.
- Include exact bundled React, ReactDOM, Scheduler and Lucide/Feather notices
  in local assets and the public source archive.
- Verify 765 Python tests/15 subtests (one opt-in E2E module skip), 84.43%
  coverage under -W error, isolated 764-test suites on 3.11/3.12/3.13,
  typecheck/build and 139 Vitest tests. All 35 Chromium workflows passed
  together in 166.75 seconds on the final bundle; exact scope is recorded in
  docs/README.md.
- Verify a real 2,160,066,560-byte two-part bot upload (a valid short MP4 with
  sparse free-box padding and media across the real boundary), full Chrome
  original SHA, cross-part ranges, actual playback/seek/audio/visible mov_text,
  three small-upload senders and stable Unicode metadata/share flows.
- Verify a real partially received upload through an ancestor-folder move,
  exact original download, atomic bulk-failure rollback and confirmed disposable
  cleanup after the final backend restart.
- Verify fresh empty-data recovery of multipart manifests plus an exact
  cross-boundary range, then independently confirm disposal of all five owned
  test documents and the final protected catalog. Keep that partial recovery
  read distinct from the earlier full Chrome original hash.
- Verify live nested folder-upload bytes and completion-after-pin, then recover
  the catalog into a separate fresh DATA_DIR without old sessions/index,
  revoke restored shares and verify media hashes while preserving the original
  deployment. Record exact scope in docs/README.md.
- Verify one actual lost SendMedia reply/client-restart replay with the same
  request identity/message/hash and confirmed disposable cleanup; keep process
  crashes during media send as a separate unverified scenario.
- Keep Premium capacity/acceleration, old-message deletion and physical ARM
  evidence separate; catalog recovery is not a full
  staging/configuration restore or a guarantee of future provider retention.
