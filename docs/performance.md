# Streaming, caching and transfer performance

[Docs home](README.md) · [Using TGDrive](usage.md) · [Configuration](configuration.md)

Find the slow stage before changing limits. **Cold** reads fetch bytes from
Telegram; **warm** reads reuse bytes cached on the TGDrive server. Cached files
still need a working server and a connection to the viewer.

| Goal | Start here |
| --- | --- |
| Prepare a video or shared file | [Preload the original](#prepare-a-file-before-sharing) |
| Diagnose slow transfers | [Read Performance](#read-the-performance-page), then [check the symptoms](#investigate-a-slowdown) |
| Tune capacity | [Cache budgets](#cache-limits) and [upload controls](#upload-pipeline-and-smaller-splits) |

[See Preloads and Performance](showcase.md#preload-and-monitor) for screenshots of
cache coverage, network traffic and server resources. Values in the captures are
examples from the recorded session, not throughput benchmarks.

## Follow the data path

| Stage | Typical constraints |
| --- | --- |
| Device → server | Your upload connection, server bandwidth, temporary upload disk and browser queue |
| URL source → server | Source speed, network route, server bandwidth and temporary upload disk |
| Server → Telegram | Telegram datacenter, request latency, upload pipeline, cooldowns and server upload bandwidth |
| Telegram → server | Network route, available Telegram accounts, request limits and missing cached bytes |
| Server → viewer | Server upload bandwidth, proxy, viewer connection and simultaneous downloads |
| Video decoding | Container/codec support and browser/device capacity |

TGDrive uses Telethon with cryptg, bounded upload pipelining and pooled downloads.
Accounts and documents can use different datacenters; changing server region may
change routing but cannot relocate a document or guarantee improvement.

A server with stronger connectivity/resources can improve upload/download speeds
when it removes the current bottleneck. Device uploads still depend on your
uplink; shared downloads use server egress and each viewer's connection.

For [managed hosting](hosting.md#optional-managed-hosting), plans scale server resources
for concurrent visitors and transfers. All TGDrive features remain included;
larger plans increase capacity without promising a fixed transfer speed.

## Cache limits

Tune the running instance in **Configuration → Cache settings**. Saved MongoDB
preferences override initial environment values. Save affects the visible category;
**Discard changes** returns to its last saved values. Environment changes need a
restart. See [exact settings](reference.md#performance-and-cache-settings).

| Budget | Default |
| --- | --- |
| Retained RAM | 256 MiB; clamped to ¼ of available host/container memory |
| Retained disk | 10 GiB |
| Cache free-disk reserve | 1 GiB |
| Disk expiry | 24 hours since last reuse |
| RAM expiry | 10 idle minutes |
| Transient streaming payload | 32 MiB, availability-clamped with a 2 MiB floor |

Budgets cover the running installation and are separate from temporary upload
storage. Units are binary (1 GiB = 1024 MiB). **Cache is temporary, not a backup.**
Clearing it leaves Telegram originals and the catalog intact.

<details>
<summary>Memory, disk integrity and eviction</summary>

- Disk space is consumed as blocks arrive, not reserved upfront. Cache writes
  respect their free-space floor; upload staging separately uses `MIN_FREE_BYTES`.
  Leave headroom for the OS, uploads and backups.
- Disk blocks include integrity overhead. RAM and disk may retain the same hot
  bytes, so their budgets cannot be added to decide whole-file capacity.
- A bounded asynchronous disk-write queue shares the RAM budget. Atomic files
  and block digests reject partial/corrupt entries; entry count scales with the
  byte budget. Disk inventory survives clean restart; updates arriving during an
  index flush trigger another flush.
- New blocks enter probation; reuse promotes them into a protected area,
  normally up to 75% of a tier. Protection favors reuse but does not pin bytes.
- Disk failure falls back to origin where possible. Whole-file preparation fails
  if it cannot establish verified retained coverage.
- Separate transient reservations cover queued, completed and yielded 1 MiB
  source blocks until consumption/cancellation. New response heads can reclaim
  other readers' speculative blocks, including from suspended consumers, while
  awaited/yielded heads remain protected. Cancelled I/O holds its reservation
  until cleanup finishes.
- Transport, ASGI, TLS, kernel and decoder buffers remain outside these payload
  budgets. The pooled reader bypasses the old per-client payload cache to avoid
  duplicating retained-byte budgets.

</details>

## Automatic read-ahead

TGDrive adapts reads in two places:

| Kind | Scope |
| --- | --- |
| Inside a response | Ordered 1 MiB blocks within the requested interval; paused consumers shed speculation |
| Beyond playback responses | Optional warming for original audio/video; excludes explicit downloads, unknown range clients, document readers and alternate-quality previews |

Downloads keep speculation inside each requested interval, including parallel
ranges and retries. Aligned cold Telegram boundary blocks may be larger than
the requested interval; the HTTP response bytes remain exact.

**Preload during playback** defaults to enabled. Disabling it leaves in-response
read-ahead and manual whole-file preloads available. Manual preparation always
targets the original, even when a lower-quality preview is selected.

<details>
<summary>Playback qualification and preparation thresholds</summary>

In-response reads begin with a head and one probe. Delivery, backpressure and
remote-block latency adjust the window within account capacity, 32 blocks,
remaining requested blocks and fair transient-budget share. Beyond-response
warming requires `preview=1` or an audio/video fetch destination.

Valid Telegram duration supplies average media byte rate; otherwise sustained
delivery cadence supplies an estimate. Fill cadence is measured per file, not
borrowed from unrelated cache hits. Neither estimate measures device decoding
or the bitrate of each variable-rate scene.

| Policy | Threshold |
| --- | --- |
| Qualify playback | Deliver roughly 4 seconds at estimated media rate, clipped to 1–4 MiB; 4 MiB when duration is unknown |
| Ahead target | 8 seconds; extend to 16 when same-file fills lag estimated demand |
| Ahead byte bounds | Block-rounded 1–128 MiB, at most ¼ of effective preparation capacity; skip if 1 MiB cannot fit |
| Whole-file promotion | Qualifying, overlapping runs from distinct identified viewers within 10 minutes; overlap reaches qualification threshold; file plus overhead fits ½ of an effective tier |
| Start cooldown | 30 seconds per file; new repeated use may promote immediately |
| Renewal | Renew after delivery reaches halfway through the warmed region |
| Concurrent preparations | 2 by default, shared by manual/automatic jobs, with bounded block concurrency |

Metadata/HEAD never qualify. Parallel requests, retries or replays by one viewer
cannot count as distinct viewers; anonymous ranges cannot earn whole-file promotion.
Block alignment/integrity overhead count at admission, so high-rate files can
receive less than the target duration.

Successful playback range responses keep their logical viewer eligible for up
to 5 seconds between requests, so a finite range can warm the next one. The same
byte/cache/job limits apply during this gap. A real response abort cancels owned
ahead work immediately; expiry cancels unfinished work if no next range arrives.
Distant seeks abandon stale ahead work, including pending admission; already
justified whole-file preparation may finish. Demand precedes queued speculation.
Closing/seeking drains unused read-ahead; cancelling one subscriber does not
abort an in-flight fill still needed by another authorized subscriber. Clearing
cache data isolates the previous response's tracking, so its late progress or
cleanup cannot change a newly opened viewer's preparation.

</details>

Independent blocks can use different healthy bots/Premium clients and shared
in-flight fills, while responses preserve original order. Empty or short origin
blocks enter the bounded account retry/failover path; incomplete bytes are never
retained as ready cache blocks. Every request must pass authorization before
cached bytes; shared access is rechecked before future blocks. No window estimate promises buffer-free playback or recalls sent bytes.
Reopening a preview starts a new browser transfer: file responses use private,
non-storing HTTP cache policy. Retained server blocks can satisfy that transfer
without another Telegram payload download; loading UI alone does not indicate a
server-cache miss.

## Prepare a file before sharing

[Preload file](usage.md#preload-a-file-for-smoother-playback-and-downloads) requests
the whole original through the same verified cache, including multipart files.
It runs on the server without downloading to your device.

1. Choose **My Files → file ⋯ → Preload file**.
2. Open **Monitor → Preloads** and wait for **Ready** with full coverage.
   Blue segments show partial coverage.

Preloading starts only when the whole file fits the effective RAM or disk cache
budget. Concurrent preparations reserve capacity and verify retained ranges
before completion; bytes downloaded and then evicted do not count.
Manual multipart preparation also checks complete
part checksums. Cancellation/clearing does not delete the file. Eviction, expiry,
reduced budgets, disk loss and restart can change coverage, so inspect current
inventory before a planned viewing.

## Shared-download load and queues

Warm bytes avoid Telegram payload work and have no fixed four-viewer gate, but
still need authentication, server bandwidth, sockets, memory and proxy capacity.
Cold misses share bounded account media/request admission and origin queues;
uploads compete within the same account budget. Demand precedes background work.

Requested payloads take turns in **1 MiB blocks**. After admitting a download
block, its request's remaining queued blocks move behind other waiting requests
at the same priority. This keeps one file's read-ahead from taking every next
turn. Anonymous responses get their own internal request identity; identified
parallel ranges share their supplied identity. This is request fairness, not a
per-person quota, and running reads are not interrupted. Shared cache fills still
fetch each block once for their subscribers.

Uploads hold an operation slot for a Telegram document part. If configured upload
concurrency fills every combined media slot, downloads must wait for a part to
finish; the default one upload and three combined operations leave read capacity.
Background thumbnail source metadata does not reserve a payload slot while its
pooled reader waits for one, including with a one-operation account limit.

Queue position/wait are estimates from recent durations and eligible capacity.
Without enough samples, no ETA is invented. Seeks, outages and cooldowns can
change estimates; overloaded queues can reject work. Respect response status
and retry guidance rather than opening unlimited requests.
Positions count origin operations, not files or people. The download queue and
request-status observations are held in memory and do not survive a restart.
Verified disk-cache blocks can still be reused after restart.

## URL import intake

URL imports download to the server before uploading to Telegram. **Downloading
from URL** shows the source-to-server rate; Telegram capacity affects the next
stage. The browser can close after submission while the server keeps working.

Large, known-length sources with a strong ETag (the source's content-version
identifier) use parallel HTTP ranges. TGDrive starts with two streams and probes
four, then eight when aggregate
throughput improves. Every range must match the original representation and exact
requested interval. Unsuitable range responses abandon the partial attempt and
restart as a single stream within the retry budget; rate-limit responses respect
`Retry-After`.
Small files, unknown lengths, sources without a strong ETag, and **Compatibility
Mode** use one stream.
Temporary source connection failures, timeouts, incomplete bodies and HTTP
408/429/500/502/503/504 are retried automatically, with at most **four source
attempts per running cycle**, including a parallel-to-single-stream fallback.
Ordinary backoff is **2, 4, then 8 seconds**; a valid `Retry-After` can make it
longer. Unsupported ranges can fall back immediately. After a transport failure,
remaining attempts use one stream. Invalid/unsafe URLs, TLS verification failures,
permanent HTTP errors, size limits and local storage errors do not enter this loop.

**Source interrupted · retrying automatically** shows the next attempt and wait.
Before waiting, TGDrive drains writers, deletes the unpublished partial, and
releases source slots and staging claims. The retry rejoins normal admission and
checks storage again, then restarts at byte zero. It never combines bytes across
attempts or sends a partial original to Telegram. Cancellation interrupts the wait.
The existing total timeout includes retries and backoff. Server restart starts a
fresh cycle, honoring a saved source retry deadline; no browser tab is required.
Only exhausted attempts or permanent errors need user action.
Neither mode can exceed the source, route or server capacity.

**Transfer limits → URL downloads → Full-speed URL downloads** defaults to **2**,
configurable from **1 to 8** without a restart. Lowering it lets existing full-speed
jobs finish. This limit covers source retrieval; Telegram upload has its own
scheduling. Eligible waiting jobs are promoted in arrival order. A job that cannot
reserve its full staging requirement stays slow while another eligible job can run.

Waiting sources use one stream paced at **64 KiB/s** each, shown as **Queued ·
receiving slowly**. Open source jobs are bounded to the configured full-speed limit
plus **8**; further jobs show **Waiting for a source connection**. Waiting sources
reserve **32 MiB** staging windows as needed, capped by the file size; full-speed
jobs reserve their entire known size, or the admitted maximum for unknown lengths.
**Waiting for temporary storage** pauses reads until space becomes available.
Promotion preserves received bytes and can switch the remaining body to ranges.
Slow reception can help with idle timeouts, but does **not** extend signed-token
expiry, guarantee a server will keep a request open, or protect links beyond the
connection/storage bounds. Submit expiring links close to when capacity is available.
The total operation timeout includes queuing and slow reception.

Full-speed intake has no intentional bandwidth cap or per-chunk database
transaction. Reads and receive buffers are up to **1 MiB**; waiting reads are up to
**16 KiB**. Blocking writes and free-space checks run in workers. Parallel range
responses have a shared **16-response** bound in addition to the source-job bound.
Live written-byte counters reduce unwritten reservations without scanning receipts
for every chunk; storage is checked when claims grow and before disk writes.

Download progress stays in memory. Initial/window admissions, phase/status changes
and recovery identities commit with their updates; a promotion claims space in
memory immediately and journals it with the next progress/lifecycle update. File and directory synchronization
remain at final publication. These counters do not promise byte-range resume:
an interrupted URL retrieval may restart from zero. See
[transfer durability](architecture.md#transfer-pipeline).

Comparisons with a server-local downloader should use the same URL and comparable
time windows; simultaneous downloads compete for bandwidth. These policies do not
establish a measured speedup or guarantee the same rate as another client.

## Device upload receipts

Browser uploads use sequential **8 MiB** resumable chunks. Live progress counts
sent bytes; acknowledgement follows server write, checksum, synchronization and
MongoDB receipt commit. New files need no whole-device prehash; resumed files
verify acknowledged chunks. Writes, hashing and receipt commits run off the HTTP
event loop. Disconnect drains an active commit before releasing its lock.

Device receipt and Telegram dispatch are separate stages. More bots cannot
improve the device uplink or bypass a proxy. Compare device progress with HTTP
received traffic and server load; a server-local benchmark excludes the viewer's
route. See [transfer durability](architecture.md#transfer-pipeline).

New receipts avoid redundant offset/snapshot requests. Browser payload slots are
released after the final acknowledged chunk; up to two server completion requests
run separately, so receipt verification does not hold the next file's device slot.
Eligible batches rotate through those slots. Slow disk, database commits, staging
pressure and the uplink can still limit intake.

Creating an upload receipt keeps its initial file synchronization and database
commit off the HTTP event loop, with staging reserved before it yields. See the
[transfer pipeline](architecture.md#transfer-pipeline) for admission and retry
boundaries.

Job-state commits are awaited worker operations. A slow commit holds back its
state transition without blocking reads of the last acknowledged job state.
Bulk move, Trash and restore also share batch working copies rather than copying
the full catalog for each item; name, ancestry and share checks still depend on
catalog size.

## Upload pipeline and smaller splits

| Control | Default / allowed values |
| --- | --- |
| Concurrent uploads per bot | 1 / 1–4 |
| Outstanding 512 KiB upload RPCs per document | 4; also bounded by account request limit |
| Concurrent account media/metadata RPCs | 8 / 1–32 |
| Account part/metadata pacing | 48 requests/s / 1–128 |
| Combined upload/download operations per account | 3 / 1–16 |
| Waiting origin operations, installation-wide | 256 / 1–4096 |
| Preferred document split | 2000 MiB / 1–2000 MiB; 0 also means 2000 MiB |

These count origin operations/RPCs, not viewers or whole files. More queue space
permits longer waits, not throughput. Rate limits are protective application
ceilings, not Telegram entitlements; provider waits take precedence.

<details>
<summary>Splits, Premium and tuning tradeoffs</summary>

Smaller parts can engage more slots but add messages, manifests, retries and
boundary setup. Pipelining can improve one document without splitting it.
All new documents stay within 2000 MiB, including Premium. Very large files
raise tiny split preferences to stay within 4096 parts; journaled plans are never
re-split by a new preference. Staging/file-size limits still apply.

Ready Premium clients are preferred for parts and payload windows, with eligible
bot fallback when busy, cooling or unhealthy. Dispatched parts retain their
sender on retry. Premium is a potential speed benefit, not a larger TGDrive file
limit. [Telegram's file documentation](https://core.telegram.org/api/files) describes
Premium throttling differences; TGDrive has not measured a live Premium multiplier.

Keep one upload per bot initially. Change one limit at a time against a representative
workload. Retain increases only when throughput improves without harming playback
or increasing cooldowns; reduce concurrency/pacing when contention grows. Never
rotate identities around provider waits.

</details>

## Video controls and startup

The local custom player keeps controls available through loading, buffering,
seeking and errors. It supports the media's available tracks/chapters and focused
keyboard shortcuts (**K/Space**, **M**, arrows); mobile autoplay may require tapping
Play. Slow starts offer **Try again**; unsupported codecs retain **Download Original**.

The animated waiting status shows measured bytes received and current delivery
speed when the player's HTTP source exposes them. Received bytes include repeated
reads and seeks, so they are not a whole-file completion percentage. The compact
status strip keeps retry inline and omits zero-speed and explanatory paragraphs;
advancing presented frames clear a stale wait. The timeline's source-buffer time ranges are estimates,
separate from server cache coverage. Playback does not require a whole-file preload.

The pinned player applies video-decoder backpressure to video-only files rather
than reading ahead for a nonexistent audio track. This protects decoder admission;
it does not establish smooth playback on every codec/device.

TGDrive retains its custom playback pipeline for all video previews, including
HEVC. Native browser playback support does not establish that the custom
WebCodecs/WASM pipeline can decode and render the same file smoothly.

Fast server delivery cannot make every device decode a 4K codec smoothly. A long
wait retains retry/download actions. Cached bytes still need delivery and decoding.
At EOF, the hardware decoder drains pending pictures; the seek reset path must
not recreate the HEVC decoder and discard its final pictures.

### Browser playback buffer

The custom player fetches compressed bytes independently of the small decoded
picture queue. It aims for **90 seconds ahead**, using the greater of the file's
average byte rate, declared bitrate and observed consumption rate. Higher bitrate
sections increase that estimate. Seeks reset sampling instead of counting the
skipped bytes as consumption. Once filled, fetching resumes below 75% of the goal
(roughly 67 seconds), before the playhead approaches the buffer edge.

The source starts with at most **32 MiB** and grows to meet that time goal as
bitrate information becomes available. Its memory ceiling is one-sixteenth of
the browser's approximate `navigator.deviceMemory` hint, bounded to **32–512 MiB**:

| Reported device memory | Compressed source ceiling |
| --- | --- |
| 1 GiB | 64 MiB |
| 2 GiB | 128 MiB |
| 4 GiB | 256 MiB |
| 8 GiB or more | 512 MiB |
| Hint unavailable or invalid | 128 MiB |

The hint is rounded/privacy-limited and unavailable in some browsers; it is
**not free RAM**. The ceiling is not allocated upfront and does not cover decoder,
GPU, WASM or transport memory. Growth preserves cached bytes, offsets and stream
state. During growth the old and new allocations can temporarily coexist until
garbage collection. If allocation fails, playback retains the working buffer and
reduces its ceiling for this opening instead of retrying larger allocations.

For longer videos, the time goal uses at most 80% of the allocated window to leave
transport/compaction headroom. Very high bitrate files may still retain less than
90 seconds on a constrained device. Clips up to two minutes that fit the device
budget can cache completely. Non-range origins select sequential storage before
writes and do not grow it during writes. Range requests scale with the byte goal, up to
8 MiB; one upcoming response can already be in flight when fetching pauses.
Neither a request size nor the decoded frame queue is the playback buffer goal.

Seeking inside retained source bytes reuses them while decoding from a suitable
keyframe. A seek outside the retained window fetches its new range. Closing the
preview cancels its source requests. This is browser memory, separate from the
server cache and manual whole-file preloads. Timeline coverage remains an estimate
for variable bitrate/interleaved files, not a guarantee of decoded readiness.

## Operator checklist

- [ ] Measure ingress/egress using multiple reliable endpoints.
- [ ] Compare a cold authorized range with a repeat; warm results exclude origin work.
- [ ] Watch effective budgets, disk headroom, queues, cooldowns, CPU and network.
- [ ] Test startup/seeks and same-file/different-file viewers through the real HTTP path.
- [ ] Change one limit and repeat the same workload.

### Read the Performance page

**Monitor → Performance** separates server network, TGDrive HTTP file traffic
and Telegram origin activity. They overlap: **do not add their totals**. Network
counts include protocol/other interface traffic; Docker normally measures its
container interface. Blue is **Download to server**; dashed is **Upload from server**.
Hover or use the keyboard/touch sample scrubber. **Now** always means current.

<details>
<summary>History, totals and resource measurement</summary>

- Live: five minutes of three-second samples. Recorded: 31 days of minute
  aggregates, at most 360 plotted points. Presets include Last Month (30 days).
- Averages cover observed seconds only. Missing/offline periods remain gaps;
  incompatible scopes are not joined and earlier periods are not backfilled.
  Custom inputs use UTC; the applied interval uses the configured timezone.
- All-time totals are separate persistent checkpoints **since the displayed
  tracking start date**. Network, Telegram and HTTP totals stay separate.
  HTTP covers `/file` and resumable-upload bodies, including cached delivery
  and receipt messages; it excludes assets/settings polling. Detailed process
  counters reset on restart.
- MongoDB stores history/totals without another metrics service. Checkpoints run
  each minute and at clean shutdown; abrupt crashes can lose roughly a minute.
  Diagnostic outages can lose observations without affecting file data. Telemetry
  is excluded from encrypted backups.
- CPU uses 0–100% of available container cores/quota. RAM uses cgroup usage and
  a finite limit where present; otherwise its observed scale is explicit.
  Host CPU is a labelled fallback. Load average is not CPU percentage; process,
  container, host-available and cache memory are different measurements.

</details>

### Investigate a slowdown

| Observation | Check next |
| --- | --- |
| Cold slow, warm quick | Account capacity, cooldowns, queue and requested-range coverage |
| Full bot slots and waiting demand | Foreground versus preloads; measured effect of capacity changes |
| High CPU, low throughput | CPU quota; raw download versus video processing |
| RAM near limit | Container usage, stream memory, effective cache budget |
| Cache reaches disk reserve | Free space and cache/staging budgets |
| Server fast, one browser slow | Protocol, viewer route, decoder and client buffer |
| Completed import, zero current rate | Historical interval and lifetime receive total |

Compare graphs over the same interval. Account/request details are current/recent
state, not a month-long request log. Cache hits/misses count lookups, not saved
bytes. **Preloads** shows current verified coverage and jobs; clearing cached
bytes requires confirmation and leaves originals intact.

### Browser transport with a full cache

A ready cache removes origin payload work, not viewer transport. Browser HTTP/3
can differ from curl/Python HTTP/1.1 or HTTP/2. TGDrive clears alternative-service
advertisements by default. Check [proxy configuration](operations.md#http-delivery-and-proxy-configuration)
and [fresh-browser validation](streaming-validation-2026-10-18.md) before attributing
warm-cache stalls to Telegram or raising budgets.

### Distant video seeks

Compressed read-ahead counts only bytes inside the current source window.
Moving to a distant position must fetch its first range even while the demuxer
still reports its old read position; skipped file bytes are not buffered data.
The loading overlay shows time spent waiting and bytes received for that seek,
rather than repeating a cumulative count from earlier playback. It remains a
waiting state until actual playback frames advance. Source delivery, codec/device
support and decoding still affect seek latency.
