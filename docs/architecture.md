# How TGDrive works

[Docs home](README.md) · For contributors · [API reference](reference.md#http-api)

## The short version

~~~text
Browser → TGDrive server → Telegram: file contents
                         → MongoDB: file list and app state

MongoDB → encrypted database copy → Telegram, in the background
~~~

TGDrive runs one FastAPI process with its own supervised background work.
Use **one installation and one worker per drive**.
There is no separate task broker or Redis requirement.

## Components

| Part | Responsibility | Main files |
| --- | --- | --- |
| Web interface | File manager, previews and transfer display | `website/src/` |
| HTTP layer | Routes, authentication, request limits and file responses | `app.py`, `api_v1.py`, `http_guard.py` |
| Database | Transactions, ownership and committed read cache | `mongo.py`, `mongo_store.py` |
| Browser uploads | Received offsets, checksums and staging reservations | `uploads.py` |
| Jobs | Upload, retry, cancellation and deletion supervision | `tasks.py`, `transfers.py` |
| Large files | Ordered Telegram parts and logical byte ranges | `multipart_transfer.py`, `multipart_streaming.py`, `logical_media.py` |
| Telegram connections | Telethon adapter, account selection and bounded requests | `telegram_clients.py`, `client_pool.py` |
| Previews/downloads | Authorized ranges, format inspection and rendering descriptors | `streaming.py`, `ranges.py`, `previews.py` |
| Media cache and preloads | Shared verified block fills, resource budgets, inventory, eviction and background preparation | `media_reader.py`, `media_cache.py`, `media_policy.py`, `preloading.py` |
| URL import | Bounded HTTP fetching and destination checks | `remote.py` |
| Database backups | Encrypted snapshots and empty-database restore | `mongo_backup.py` |
| Web connection management | Private settings, preflight and Premium login | `management.py`, `managed_settings.py`, `premium_login.py` |
| Lifecycle and setup | Startup, shutdown, CLI and Docker wizard | `runtime.py`, `__main__.py`, `setup.py`, `scripts/setup.py` |

Backend filenames are under `tgdrive/` unless shown otherwise.

## Authoritative data and identity

**MongoDB is the source of truth.** The in-memory catalog is a read cache,
not a second database.

| Record | Why it exists |
| --- | --- |
| Catalog and nodes | Drive/channel identity, schema, folders, files and ordered file parts |
| Shares | Scoped link access and expiry |
| Operations and preparations | Retry the same upload/folder request without duplicating it |
| Jobs and receipts | Resume known work and track acknowledged bytes; archived jobs retain compact transfer history |
| Deletions | Track retryable catalog removal; completion drops references and retains Telegram originals |
| Settings, sessions and identities | Owner access, Telegram credentials, names and preferences |
| Backups | Snapshot publication and retry history |
| Runtime lease | Prevent competing installations from writing the same drive |

Key rules:

- Item paths contain stable **IDs**, not names. Moves retain item identity.
- Names use Unicode normalization and case-insensitive sibling checks.
- A file stores one message reference or an ordered list of part references,
  offsets, sizes and checksums. Public listings hide those Telegram locations.
- Schema version 4 tracks catalog revision and commit generation.
- Current catalog bounds are 128 levels and 250,000 items.
- MongoDB mode never opens a local SQLite catalog. Legacy code remains for
  compatibility tests and earlier maintenance paths.

### How a write becomes visible

1. A mutation changes an isolated working copy.
2. A MongoDB transaction checks ownership and catalog generation.
3. Changed records commit together with majority acknowledgement.
4. Only then does the new immutable read cache become visible.

A failed transaction keeps earlier visible state. An uncertain acknowledgement
reloads MongoDB authority before more work. Unique indexes enforce active
sibling names.

## Lifecycle

| Event | Behavior |
| --- | --- |
| Startup | Validate configuration, take local/cloud ownership, load the existing catalog, start clients and eligible jobs |
| Missing catalog | Stop; never silently create an empty replacement |
| Lost database ownership | Stop admitting writes; require a safe restart after repair |
| Shutdown | Persist interrupted work, stop owned tasks and release resources/ownership |
| Reinstall | Reconnect to the same MongoDB database |

The expiring cloud lease renews in the background and is checked inside writes.
Local unfinished file bytes are still needed after a restart.

## Account identity and selection

- Bots are balanced by active work and recent selection.
- Each bot has one media slot by default; the owner can choose 1–4 in
  **Configuration → Transfer limits**. Premium clients retain one slot each. Multipart parts
  use these same slots, with FIFO admission and least-active client selection.
- Slots scale with connected bots, independently of the local I/O semaphore.
  Lower limits drain existing leases; increasing a limit wakes eligible waiters.
- Shared publishing pacing and persisted account/channel FloodWait deadlines
  still bound requests. More bots do not promise unlimited bandwidth.
- Upload preferences live in MongoDB's settings collection, are included in full
  database backups, and load before recovered transfers start.
- Large files use bot-compatible parts with the default 2000 MiB split cap.
  All accounts, including Premium, use at most 2000 MiB per new document.
  Smaller explicit caps reduce part size; 0 uses the same 2000 MiB maximum.
  Ready Premium slots are preferred for uploads and download payload windows,
  with eligible bot fallback when those slots are busy or unavailable.
- Initial media lookup can prefer healthy Premium users, then bots. Independent
  payload windows bind the same immutable media through each selected account;
  the pooled reader may use different healthy accounts for different windows.
- Upload/download origin activities share a combined per-account budget, while
  uploads retain their additional per-bot/user lane limits. Demand waiters precede preload waiters;
  running work is not preempted. The media wait queue has a global upper bound.
- Bot sessions use stable account IDs. Reordering tokens does not change sender identity.
- Each dispatched part keeps its recorded sender and request ID during retry.
- Runtime credential/channel failures quarantine only the affected account and
  wake lease waiters. Reads retry eligible accounts; undispatched upload parts
  can change sender, while dispatched parts retain their durable identities and
  report actionable repair errors. Verified channel repair can re-enable a live
  client; credential replacement applies after restart.

### Names and Refresh

Startup uses the existing authenticated account response to populate missing
names in the background. Cached names are stored in MongoDB.

**Connections → Refresh** separately verifies saved credentials and channel
permissions using temporary sessions:

- Maximum two checks at once; repeated clicks share in-progress work.
- No test messages are sent or deleted.
- Transient errors retry up to three attempts. Account waits apply to checks
  and transfers; provider deadlines persist across restart.
- Last known names survive failures. Probe results remain distinct from live
  socket health.
- Changes cancel outdated checks; shutdown cancels all owned check tasks.

## Connection management

1. The owner submits a full candidate with its expected revision.
2. Temporary clients verify enabled accounts and channel permissions.
3. The private settings document commits atomically.
4. New mutations pause until a restart applies that revision.

Secrets are write-only. Omitted replacements retain their saved value.
Editing requires settled jobs/receipts and valid database ownership.
A Telegram database backup is not a save prerequisite.

Premium login uses owner-bound, in-memory five-minute challenges.
Codes and two-step passwords are not stored. Successful sessions go directly
to private settings; the browser never receives a StringSession.
Cancelled/expired/unsaved authorizations are revoked.

## Transfer pipeline

Browser receiving and Telegram dispatch are independent. Browser receives start
with two slots and adapt between one and four. Each completed receipt immediately
creates a durable server job: Telegram work can start for that file while the
browser sends later files. Neither stage waits for the whole folder or batch.
Staging reservations, free-disk headroom and the 1,000 unfinished receipt bound
still control admission.

### From a browser to one drive file

1. **Receive:** accept fixed-size HTTP chunks into local staging.
2. **Acknowledge:** flush bytes, then save the offset and checksum in MongoDB.
3. **Queue:** verify received chunks and create or reuse one durable job.
4. **Send:** upload one Telegram document or ordered parts.
5. **Commit:** publish the logical file once, using its stable operation ID.
6. **Finish:** confirm MongoDB state, report completion and release staging.

After a lost response, resume from the acknowledged offset.
Names and sizes alone never prove that a reselected file has matching bytes.

Full device progress means the browser has sent the bytes; the server must still
confirm durable ownership. **You can close this tab** requires accepted server
jobs for all remaining device uploads. Fully received files finish verification
and durable enqueue even if the completion request disconnects; polling can
recover the accepted job identity after a lost response. Telegram work continues
while TGDrive stays running. Use **Completed** as the point for deleting local
originals. Restart recovery still needs unfinished staged bytes on the server;
database backups do not include them.

Receipt durability is implemented in [uploads.py](../tgdrive/uploads.py);
browser handoff and recovery are in [transfers.ts](../website/src/transfers.ts)
and [deviceUploadStatus.ts](../website/src/deviceUploadStatus.ts).

### Adaptive device upload concurrency

The browser adapts simultaneous file receives to measured aggregate throughput,
independently of Telegram slots. Increasing concurrency is useful only when more
pending files are ready and existing payload transfers are busy. A freed receive
slot immediately admits another eligible file. Lowering the limit lets active
files finish; it does not abort or restart them.

| Policy | Current behavior |
| --- | --- |
| Starting and allowed limits | Start at 2 concurrent files; adapt from 1 to 4; reset to 2 after device work and its queue drain |
| Measurement | Aggregate HTTP PATCH payload progress in 4-second windows, with confirmed bytes as a fallback for transports without progress events |
| Eligible windows | A ready backlog and payload requests occupying at least 70% of the available receive slots over the window |
| Probe for more capacity | After 2 stable windows, try one extra file; rates within 25% of baseline count as stable |
| Keep or roll back a probe | Compare the next 2 eligible windows with the baseline; keep the increase only for at least 10% aggregate throughput gain |
| Sustained degradation | Reduce by one after 2 eligible windows with nonzero throughput below 65% of baseline |
| Payload stall | Reduce after 15 seconds without progress once actual browser payload progress has been observed; waiting for a first event or a completed body's acknowledgement is not a stall |
| Transient pressure | Reduce for device network failures and HTTP 408, 429, 500, 502, 503 or 504 before server handoff; cancellation, authentication, checksum and storage failures do not lower the limit |
| Settling time | Wait 30 seconds before probing again after pressure or an unsuccessful probe; wait 12 seconds after a successful probe |
| Unreliable measurements | Discard trends while hidden/paused or after a sample gap or window longer than 12 seconds; exclude hashing, acknowledged resume offsets and Telegram work |

The policy and its thresholds live in
[deviceUploadConcurrency.ts](../website/src/deviceUploadConcurrency.ts).
[transfers.ts](../website/src/transfers.ts) supplies payload observations,
visibility state and transient errors, and applies the resulting admission limit.

### Bounds and retries

- HTTP 429 pauses new device admission globally and reduces adaptive concurrency;
  HTTP 507 defers only the affected file so smaller files can use remaining space.
  Both retry automatically after at least 5 seconds or the longer server
  `Retry-After` deadline, retaining the source file and acknowledged offset.
- Listing invalidations from completed uploads are coalesced and applied without
  remounting the current directory. Existing rows stay visible during refresh;
  selection, focus, pagination and scroll stay in place. See
  [listingUpdates.ts](../website/src/listingUpdates.ts) for refresh coalescing and
  row reconciliation.
- The controller retains up to 2,000 rows in unfinished batches.
- Media leases default to one upload per bot, configurable to 1–4; each Premium
  client has one slot. Multipart workers scale to eligible client capacity, with
  a one-second check for live limit increases. Database backups are separately serialized.
- Bot-compatible parts can use both bot and Premium clients. Older persisted
  plans requiring a Premium-sized document stay on Premium clients. Retry and cleanup retain the
  original sender; fair leases and shared channel pacing apply to every part.
- Folder upload prepares an atomic, repeatable tree before queuing file bytes.
  Later file uploads can succeed or fail independently.
- Confirmed remote identities survive retry. The catalog operation lookup closes
  the gap between a file commit and its job-journal update.
- The Telethon transport pipelines a bounded number of 512 KiB upload RPCs.
  A shared per-account scheduler caps concurrency and request rate for upload,
  download and metadata work. Provider waits reduce pressure rather than being
  bypassed with replacement identities.
- A confirmed Telegram document survives process restart. A partially uploaded
  unfinished document can retain protocol acknowledgements during an in-process
  retry, but its payload is uploaded again after process restart.
- MongoDB finalization never waits for an encrypted Telegram snapshot.
- Completed catalog entries cannot be cancelled as if they were unsent media.

### Cancellation and destination changes

| Situation | Result |
| --- | --- |
| Cancel before dispatch | Stop and reclaim local work |
| Cancel after dispatch | Reconcile the remote outcome and clean up before confirming cancellation |
| Move the destination | Accepted work follows its stable folder ID |
| Trash a receiving destination | Keep acknowledged bytes; restore or cancel |
| Destination disappears before catalog commit | Cancel/reconcile unpublished remote work |
| Database persistence fails | Retain staged bytes and remote identities for retry |
| URL retrieval is interrupted | Retry may fetch from byte zero |
| Bot-mode ingestion | Separate path; lacks the browser queue's full durability contract |

Permanent deletion records a durable intent, then atomically removes that catalog
subtree and its shares from the database. It also drops message references from
the completed intent. This is a catalog-only deletion policy; it never deletes
the original Telegram messages. The same behavior applies to
all file ages and to retries of legacy intents with partial remote confirmations.
Job retries, lineage checks, upload reconciliation and the independent-backup
barrier remain in place. Different selected items complete independently.

### Finished history

Live terminal jobs may expire or be evicted to keep the worker registry bounded.
Resolved upload/import records become compact summaries instead of disappearing.

- MongoDB stores archived summaries in the jobs collection, outside the live loader.
- Archived summaries omit private source/recovery context and cannot be retried as live jobs.
- The owner history API merges fresh live progress with indexed, cursor-paged archives.
- Reads are bounded per request; old records are not all loaded into startup memory.
- Deletion notices remain outside upload history.
- Floating-panel dismissal does not affect server history.
- A browser-only `initiatedHere` receipt marker scopes floating panels to the
  initiating browser. Global server history and owner transfer snapshots retain
  jobs started elsewhere; polling does not turn those into local receipts.
- URL imports retain their server-job identity and remote origin through
  upload-stage transitions and browser reloads. They never require a local
  browser file to resume a server-owned job.
- Owner history responses resolve the current destination through the upload
  operation's stable node identity. Resolution uses the native catalog read
  cache in MongoDB mode, without extra database queries per file.
- Show-in-folder resolves again at activation, follows moves/renames, and rejects
  trashed/deleted identities. Browser navigation selects and reveals the correct
  file-list page without automatically opening a preview.
- Complete database backups include the archived records.

## Telegram transport and file responses

Telethon with cryptg provides MTProto transport. Hosted HTTP Bot API limits
do not directly describe this adapter.

- Authorization is checked before Telegram access or cached bytes.
- One HTTP range maps to the required logical file parts, including boundaries.
- Full part reads verify checksums; partial reads validate stored identity/size.
- Expired file references refresh from the stored message with bounded retries.
- Streams close iterators and leases on failure, cancellation or disconnect.
- Missing Telegram messages or failures after headers can interrupt a download.

## Media and performance

| Area | Implementation |
| --- | --- |
| Original bytes | Browser uploads remain documents; preview choices do not change originals |
| Text | Bounded 256 KiB read-only preview |
| PDF | Local PDF.js with lazy byte ranges, up to 256 MiB, continuous rendering with memory bounds |
| SVG | Passive image element; never inject uploaded markup into the page |
| Video | Lazy local Movi/WASM decoder using the same authorized range endpoint |
| Unsupported format | Compact details and original download |
| Quality choices | Only representations actually present in Telegram |

File responses read ahead through ordered 1 MiB blocks, scaled to client
capacity and their share of the instance-wide transient payload budget. A
single consumer can use the default 32 MiB budget; concurrent consumers share
it. The 32-block policy is an additional ceiling. Waiting response heads take priority
over further prefetch and can revoke another suspended response's speculative
frames. Its awaited/yielded head remains protected. A revoked in-flight frame
retains its reservation until cancellation cleanup finishes; a completed frame
can release immediately. The original reader reloads any reclaimed gaps in
order, and a shared fill stays alive while another subscriber needs it. Both ordinary and multipart files spread reads across healthy bots and
Premium clients. Download activities scale within the configured shared media
budget instead of a fixed two-read cap. Part RPCs and metadata also use the shared request/rate
scheduler. Account FloodWait deadlines gate eligible work.

The application configures a shared 256 MiB RAM cache and 10 GiB disk cache by
default. Effective RAM is clamped to a quarter of available host/container
memory. Disk caching respects a separate free-space floor, sliding retention,
and an entry bound that scales with the byte budget. A probation/protected
policy favors reuse over one-time scans. Atomic files and block digests reject
partial or corrupt entries. The pooled path bypasses the old per-client payload
cache so it does not silently duplicate its own retained-byte budgets.

Disk writes use a bounded asynchronous queue inside the RAM budget. Failure
falls back to origin where possible; explicit whole-file preparation fails if
verified retained coverage cannot be achieved. The shared inventory tracks
observed ordinary downloads as well as preloads, with half-open ready ranges
and current complete/ready-byte state. Disk inventory survives clean restarts.

Transient source blocks have a separate shared scratch budget, default 32 MiB,
availability-clamped with a 2 MiB streaming floor even if retained RAM is zero.
Reservations cover queued, completed and yielded blocks until consumer resume,
closure or failure. This bounds payload retention across different files and
viewers without a fixed HTTP response count. Transport/server/kernel buffers
remain outside these retained-cache and scratch budgets.

Every request authenticates before cached bytes. Workers bind account-specific
references and verify immutable document/rendition identity and size before
sharing content. Supplied capabilities must validate even alongside an owner
cookie. Shared-stream access is checked again before future blocks so revocation
can stop an active response without pretending to recall bytes already sent.

Concurrent foreground and preload requests share in-flight fills. The final
subscriber's cancellation drains remote work; cancelling a preload does not
abort another authorized subscriber. Closing or seeking drains unused read-ahead.

Automatic preloading requires 4 MiB of sequential data actually delivered to a
viewer. First use warms only an adaptive 4–32 MiB ahead region, further limited
by effective capacity. Two qualified runs overlapping by 4 MiB within ten
minutes establish repeated use; only then can a whole file fitting half an
effective tier be prepared. Consumption and background fill rates adjust the
ahead window. The last viewer leaving cancels ahead work; justified whole-file
work may finish. HEAD, metadata and tiny probes never establish that evidence.
Manual preparation reserves capacity,
verifies range coverage and checks full multipart checksums where applicable.
Demand is prioritized over queued speculative work, with bounded background
jobs and block concurrency. Preloads are disposable acceleration, not catalog
commits or persistent copies of source files.

Owner diagnostics expose account activity, queues, cooldowns, request metrics,
effective budgets and cache coverage. Queue ETAs derive from bounded recent
duration samples and are explicitly estimates; no samples produce no fabricated
wait estimate. See [performance](performance.md) for operational interpretation.

Movi's requested 32 MiB source buffer applies after initial loading.
Decoder/demux caches can use more memory. Neither this setting nor extra bots
guarantee a particular speed or codec's real-time playback.

The React preview reconciles buffering feedback against advancing playback.
Chapter navigation uses container titles/times directly instead of blocking on
remote thumbnails; its controls are portaled into the player's shadow root in
fullscreen. Constructed stylesheets preserve strict CSP without remote fonts.
Movi's own audio/subtitle menus remain the controls for supported tracks,
and focused-player keyboard shortcuts are enabled. Every video preview stays
in this custom player, including loading and codec errors; no native video
fallback is mounted. Its public buffered TimeRanges drive estimated timeline
coverage and ahead-time feedback, while public source network counters provide
received bytes/rate. A pinned UI adapter paints Movi's documented buffer part
while paused, because its ordinary UI tick can otherwise leave ready ranges
invisible. These ranges are estimates for some HTTP source modes and remain
separate from server preload byte coverage.

## Database safety copies and security

Database copies run independently of uploads:

1. Read a consistent set of durable collections.
2. Compress and encrypt using AES-256-GCM.
3. Persist the encrypted source identity before sending it to Telegram.
4. Verify the received ciphertext before confirming the backup.

Restore needs the matching key and an empty database. It revokes browser
sessions. Original Telegram files and unfinished local payloads are not copied
inside the database archive.

[Operations](operations.md#encrypted-telegram-backups) covers recovery.
[Reference](reference.md#security-and-privacy) covers security.
[Development](development.md) covers tests and release preparation.


### Shared-link identity and re-copying

- Shares reference stable item IDs. Public file/folder requests resolve the
  current location before checking scope, so explicit links survive moves/renames.
- Owner listing includes current names, parent directories and expired entries.
  Access checks and exposure warnings still use only active shares.
- New share capabilities derive from a random share ID and the persistent owner
  secret using domain-separated HMAC-SHA256. The catalog still stores hashes only.
  The owner copy endpoint verifies the derived token against that hash.
- Legacy random-token links remain usable; re-copy requires explicit replacement.
  Secret rotation also makes existing links unreconstructable, without revoking them.

## Diagnostic history

The performance sampler retains live observations and writes bounded minute
aggregates to the existing MongoDB, or local `DATA_DIR/telemetry.sqlite3`.
Recorded graphs retain 31 days; lifetime traffic checkpoints are separate.
Unavailable samples remain gaps and metric scopes remain explicit. Telemetry
is disposable and excluded from encrypted catalog backups; file operations do
not depend on successful diagnostic storage. See [Performance](performance.md#read-the-performance-page).
