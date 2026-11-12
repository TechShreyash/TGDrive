# Streaming, caching and transfer performance

[Docs home](README.md) · [Capacity guide](performance-tuning.md) · [Architecture](architecture.md)

For a slow drive, start with the [capacity guide](performance-tuning.md).
This page explains the policies behind its advice: cache retention, request
scheduling, transfer limits and measurement scopes.

**Cold** reads fetch missing bytes from Telegram; **warm** reads reuse the server's
cache. Both still need server-to-viewer delivery. In cache sections, **origin**
means Telegram. **Read-ahead** fetches upcoming bytes before delivery/playback
needs them.

| Topic | Read |
| --- | --- |
| Where time is spent | [Data path](#follow-the-data-path) · [Automatic tuning](#automatic-tuning) · [Performance stats](performance-tuning.md#read-the-performance-page) |
| Cached access and sharing | [Cache limits](#cache-limits) · [Read-ahead](#automatic-read-ahead) · [Preloading](#prepare-a-file-before-sharing) · [Queues](#shared-download-load-and-queues) |
| Transfer throughput | [URL imports](#url-import-intake) · [Device intake](#device-upload-receipts) · [Telegram uploads](#upload-pipeline-and-smaller-splits) · [Archives](#archive-work) |
| Playback and browser behavior | [Video](#video-controls-and-startup) · [Playback buffer](#browser-playback-buffer) · [Background updates](#browser-progress-and-health-checks) |

[Screenshots of Preloads and Performance](showcase.md#preload-and-monitor) show the
interface; their values are examples, not throughput benchmarks.

## Follow the data path

| Stage | Typical constraints |
| --- | --- |
| Device → server | Your upload connection, server bandwidth, temporary upload disk and browser queue |
| URL source → server | Source speed, network route, server bandwidth and temporary upload disk |
| Server → Telegram | Telegram datacenter, request latency, upload pipeline, cooldowns and server upload bandwidth |
| Telegram → server | Network route, available Telegram accounts, request limits and missing cached bytes |
| Server → viewer | Server upload bandwidth, proxy, viewer connection and simultaneous downloads |
| Permanent deletion | Backup protection checks shared across the selection, plus stopping uploads into the selected folders; see [deletion progress](usage.md#trash-and-permanent-deletion) |
| Video decoding | Container/codec support and browser/device capacity |

TGDrive uses Telethon with cryptg, bounded upload pipelining and pooled downloads.
Accounts and documents can use different datacenters; changing server region may
change routing but cannot relocate a document or guarantee improvement.

Upgrade the resource limiting the affected stage. Device intake still depends
on your uplink; shared downloads use server upload bandwidth and each viewer's
connection. [Choosing server capacity](performance-tuning.md#what-a-server-upgrade-changes).

## Automatic tuning

The resource governor sizes budgets at startup, then re-measures the server every
5 seconds and applies resource changes live within saved owner ceilings. Owners
can cap retained RAM cache, payload disk cache and staging, and set a protected
free-space floor. Managed live values are not direct PATCH inputs. Open
**Performance tab → Automatic tuning** to inspect the chosen
values and reasons. The API returns
current values, a reason for each value and the latest measurements under
`tuning` in `GET /api/v1/performance`.

| Value | Rule |
| --- | --- |
| Memory cache | Up to 25% of available memory, bounded by the saved ceiling; Auto ceiling 256 MiB |
| Disk reserves (`min_free_bytes`, `cache_min_free_bytes`) | Saved protected-free-space value, or Auto: larger of 1 GiB and 5% of the limiting measured filesystem capacity |
| Disk cache | Up to 50% of free media-cache disk after reserve, bounded by the saved ceiling; Auto ceiling 10 GiB |
| Total staging budget, logical file ceiling | Free staging disk after reserve plus allocated staging, bounded by the saved ceiling; Auto ceiling 16 GiB. Admission still checks other reservations and actual free space |
| Concurrency | Preserve configured startup ceilings; preloads also stay within 4 jobs per CPU and 8 jobs total. The resource governor does not cut or raise unrelated queues from throughput, FloodWait or database latency |

Memory uses visible cgroup limits and usage, with separately scoped kernel/VM
readings. The effective allocation ceiling is distinct from a finite container
hard limit; neither a VM total nor an allocation policy identifies physical host
RAM. CPU capacity respects visible quotas and affinity before the
OS logical CPU count. Hidden outer constraints can remain unknown; these values
do not identify physical Windows host RAM or CPU. If available memory cannot be
measured, the current retained-cache budget is retained. Cache/staging byte-budget
reductions apply immediately; growth uses 10% hysteresis. Transient stream buffers
use the separate adaptive rule described below. The governor does not sample
MongoDB latency. Per-account request
schedulers separately reduce effective rate/concurrency after FloodWait and
recover toward their configured ceilings after successful requests. Cache and
staging updates preserve that cooldown/recovery state.

<details>
<summary>Limits</summary>

- Cache sizing uses remaining free disk; staging adds its allocated bytes back
  to the total budget so an admitted transfer does not consume its own limit as
  it writes. Unrelated disk exhaustion can still stop it.
- cgroup v2 `memory.current` includes the page cache, so heavy file I/O lowers
  the measured available memory and shrinks the memory cache.
- Owner ceilings are saved; live resource values are recomputed at restart
  within those ceilings from startup values and available measurements. It does not bypass saved
  Telegram waits or learn a new concurrency ceiling from traffic.
- To leave room for other apps on a shared server, set Docker `mem_limit` and
  `cpus`; the governor reads those cgroup limits.

</details>

These ceilings cover retained media payload, not whole-process RAM or every disk
write. Cover and poster caches each have separate 128 MiB disk budgets; recovery
files are separate. VM/WSL filesystem readings do not prove physical backing-disk
headroom. Configured backing probes add a separate measurement; failed required
measurements refuse writes. Checks include captured outstanding staging claims
where applicable but remain observations, not atomic filesystem quotas.

## Cache limits

For how cached bytes serve repeat viewers and shared files, see
[how cache helps](performance-tuning.md#how-cache-helps).

Budgets are set by [automatic tuning](#automatic-tuning) from available memory
and disk; see their current values on **Performance**.
[Exact settings](reference.md#performance-and-cache-settings).

| Budget | Value |
| --- | --- |
| Retained RAM | Up to ¼ of available memory, within the owner ceiling (Auto: 256 MiB) |
| Retained disk | Up to half of free disk after reserve, within the owner ceiling (Auto: 10 GiB) |
| Cache free-disk reserve | Saved reserve or Auto: larger of 1 GiB and 5% of the filesystem |
| Disk expiry | Saved inactivity period; default 24 hours since last reuse, minimum 1 minute |
| RAM expiry | 10 idle minutes |
| Transient streaming payload | Adaptive from a 32 MiB baseline or lower, within the saved ceiling (Auto: 256 MiB), with a 2 MiB floor |

Budgets cover the running installation and are separate from temporary upload
storage. Units are binary (1 GiB = 1024 MiB). **Cache is temporary, not a backup.**
Clearing it leaves Telegram originals and the catalog intact.

**Active stream memory** measures transient payload reservations, separate from
retained cache. Compare its current effective limit with waiting streams; a
configured ceiling is not a measurement of free physical RAM. Neither this
budget nor retained-cache bytes include all process, transport or container
memory. Unused cache or stream capacity alone does not establish a throughput
problem; interpret it alongside the workload and observed waits. Growth requires
foreground memory waits after reclaiming speculative buffers and measured
headroom. Growth is at most 32 MiB per five-second sizing sample, within the
saved ceiling and measured-headroom allowance. Memory pressure, an idle interval
or unknown memory can reduce the limit; unknown memory does not trigger growth. Existing owned reservations
drain safely after a reduction. Fill reservations overlap stream reservations;
do not add them as separate process memory.

<details>
<summary>Memory, disk integrity and eviction</summary>

- Disk space is consumed as blocks arrive, not reserved upfront. Cache writes
  respect their free-space floor; upload staging keeps its own reserve.
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
Playback warms a bounded region ahead of the current position. Choose **Preload
file** when you want to prepare the whole original. A small file can naturally
fit entirely inside the bounded ahead window; repeated visits do not request a
separate whole-file automatic job. Previously cached bytes are not cleared by
this policy change.


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
| Start cooldown | 30 seconds per file; renewal follows delivered progress |
| Renewal | Renew after delivery reaches halfway through the warmed region |
| Concurrent preparations | 2 by default, shared by manual/automatic jobs, with bounded block concurrency |

Metadata/HEAD never qualify. Repeated requests, seeks or different viewer
identities do not promote automatic work into a whole-file preload.
Block alignment/integrity overhead count at admission, so high-rate files can
receive less than the target duration.

Successful playback range responses keep their logical viewer eligible for up
to 5 seconds between requests, so a finite range can warm the next one. The same
byte/cache/job limits apply during this gap. A real response abort cancels owned
ahead work immediately; expiry cancels unfinished work if no next range arrives.
Distant seeks abandon stale automatic ahead work, including pending admission.
Explicit manual whole-file preparation is separate. Demand precedes queued
speculation.
Closing/seeking drains unused read-ahead; cancelling one subscriber does not
abort an in-flight fill still needed by another authorized subscriber. Clearing
cache data isolates the previous response's tracking, so its late progress or
cleanup cannot change a newly opened viewer's preparation.

</details>

Independent blocks can use different healthy clients and shared in-flight
fetches while responses preserve order. Empty/short origin blocks use bounded
retry/failover; incomplete blocks are never marked ready. Requests authorize
before cache access and recheck shared access before later blocks. Sent bytes
cannot be recalled, and read-ahead cannot guarantee uninterrupted playback.

Reopening a preview starts a new browser transfer under private, non-storing
HTTP cache policy. Server-cached blocks can satisfy it; a loading indicator
does not establish a server-cache miss.

## Prepare a file before sharing

[Preload file](usage.md#preload-a-file-for-smoother-playback-and-downloads) prepares
the whole original on the server, including multipart files. Wait for
**Fully cached** and full coverage on the original's row in **Monitor → Preloads**
before relying on it; **Poster cached** covers only the preview image.

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

Requested payloads take turns in **1 MiB blocks**. For file responses, queued
foreground reads and transient-buffer heads also share turns by verified session
and file identity, or by verified share capability and file identity. Parallel
ranges in the same group do not each gain an independent turn. Anonymous viewers
using the same share capability are indistinguishable within that file group;
this is not a per-person quota or equal-throughput guarantee. Admission does not
preempt active Telegram reads or revoke already held response heads. Demand
remains ahead of background preloads, within account cooldowns and compatible
request lanes. Shared cache fills still fetch each block once for subscribers.

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
There is no overall time limit: a source that sends no new bytes for
`REMOTE_READ_TIMEOUT` (120 seconds) counts as stalled and is retried. Server
restart starts a fresh cycle, honoring a saved source retry deadline; no browser tab is required.
Only exhausted attempts or permanent errors need user action.
Neither mode can exceed the source, route or server capacity.

Full-speed URL downloads have a configured startup ceiling of **2** by default;
[automatic resource sizing](#automatic-tuning) preserves that ceiling.
This limit covers source retrieval; Telegram upload has its own
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

## File transfer speed limits

Upload and download are relative to the TGDrive server. **Upload from server**
limits managed outgoing file payloads, including Telegram sends and delivery to
viewers. **Download to server** limits managed incoming payloads, including device
uploads, URL imports and Telegram reads. These are shared directional limits,
not a separate allowance for each file or account. Native torrent averages share
the same allocation; Telegram-native message copies do not move host file bytes.

Actual server-interface traffic is measured separately and can exceed payload
admission. The controls do not cap protocol overhead, hidden provider/Telethon
retransmissions, authentication, MongoDB or status traffic. They are not host
network shaping and do not reserve capacity against other applications.

<details>
<summary>Native torrent allocation and very low rates</summary>

For a finite limit, one aggregate Python participant and each active aria2
process receive a share. With one torrent process, its native allowance can be
half the configured total even when Python is idle; unused shares are not
borrowed. Unlimited avoids these partition caps.

Telegram uploads retain durable 512 KiB parts. At an unusually low 1 KiB/s cap,
admission for a complete part can take about 512 seconds before provider work
starts. Smaller scheduling slices do not remove that startup delay. HTTP body
chunks can be split, but a low setting is not a promise of smooth or immediate
progress. Choose sensible host-measured rates and keep cancellation available.

</details>

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

New receipts avoid redundant offset/snapshot requests. Payload slots are released
after the last acknowledged chunk; at most two completion requests run separately
so verification can overlap the next device file. Eligible batches rotate through
slots. Slow disk, database commits, staging pressure and the uplink can still
limit intake. [Receipt admission and durable handoff](architecture.md#transfer-pipeline)
describe the worker, storage-reservation and commit boundaries.

### Browser progress and health checks

The owner interface checks progress and health less often when idle. Each
interval starts after the previous response, preventing overlapping automatic
requests; upload sending, completion and retry keep their own schedules.

| Check | Work in progress | Healthy idle tab | Healthy idle hidden tab |
| --- | --- | --- | --- |
| Transfer jobs | 1.5 seconds | 30 seconds | 60 seconds |
| Server readiness | 5 seconds | 30 seconds | 60 seconds |

Active jobs, deletions, receipts and remote-request recovery retain fast checks
in hidden tabs. Readiness stays fast during a known connection issue/admission
pause. Returning to the tab refreshes promptly; simultaneous focus/visibility
events share a request. Explicit actions do not wait for the idle interval.
Browser throttling and network latency can delay updates.

Job replies retain final records/history-clearing notices for reconciliation.
Work started elsewhere may take the idle interval to appear, but the server
checks admission on submission. Accepted server jobs continue without the page;
device sending still needs it. [Transfer stages](architecture.md#transfer-pipeline).

### Navigation during uploads

The browser coalesces transient sent-byte display updates into **250 ms** ticks.
Acknowledged offsets, state changes, errors and cancellation remain immediate;
this display cadence does not limit upload bandwidth. Acknowledging a chunk
saves its changed recovery record synchronously. Migration, history retention
and cross-tab history reconciliation use the full recovery path when needed.

Receipt completions share a **1.5-second minimum jobs-request start cadence**;
explicit refresh and retry actions remain immediate. Breadcrumbs reuse the
current listing and known folder names, and cancel obsolete ancestor requests.
Upload-driven listing refreshes reuse share badges; metadata changes and manual
refresh invalidate them.

Directory reads and JSON serialization run off the HTTP event loop. Performance
totals reuse the same committed catalog snapshot; a commit or reload invalidates
them. Frequent owner status reads use the latest backup observation loaded at
startup and updated by publication, while explicit backup history still queries
MongoDB. If that optional startup read fails, status reports no backup observation
until publication observes one; opening the catalog can still succeed. These
policies reduce repeated work; network, proxy, database and disk latency can still
delay requests.

Single-document uploads reuse the identical whole-file and document digest.
Persisted receipt verification and send-time integrity checks still run; durable
offsets continue to require synchronized bytes and a MongoDB receipt commit.

## Upload pipeline and smaller splits

| Control | Startup default |
| --- | --- |
| Concurrent uploads per bot | 1 |
| Outstanding 512 KiB upload RPCs per document | 4; also bounded by account request limit |
| Concurrent account media/metadata RPCs | 8 |
| Account part/metadata pacing | 48 requests/s |
| Combined upload/download operations per account | 3 |
| Waiting origin operations, installation-wide | 256 |
| Preferred document split (owner setting) | 2000 MiB; choose 1–2000, 0 also means 2000 |

These count origin operations/RPCs, not viewers or whole files. More queue space
permits longer waits, not throughput. Rate limits are protective application
ceilings, not Telegram entitlements; provider waits take precedence.
The governor retains these configured ceilings, apart from the preload CPU cap;
per-account feedback can temporarily reduce effective Telegram capacity. Pacing
and queue size keep their configured values.

<details>
<summary>Splits, Premium and tuning tradeoffs</summary>

Smaller parts can engage more slots but add messages, manifests, retries and
boundary setup. Pipelining can improve one document without splitting it.
All new documents stay within 2000 MiB, including Premium. Larger files use
ordered parts; journaled plans are never re-split by a new preference. Staging
and catalog-record bounds still apply.

Ready Premium clients are preferred for parts and payload windows, with eligible
bot fallback when busy, cooling or unhealthy. Dispatched parts retain their
sender on retry. Premium is a potential speed benefit, not a larger TGDrive file
limit. [Telegram's file documentation](https://core.telegram.org/api/files) describes
Premium throttling differences; TGDrive has not measured a live Premium multiplier.

Per-account schedulers back off on provider waits and recover within configured
ceilings; the governor does not raise unrelated limits from aggregate bytes.
Never rotate identities around provider waits.

</details>

## Archive work

ZIP creation stores entries without recompression to keep CPU use low. Extraction
stages the source and decoded files on the server before uploading outputs, so it
uses server disk and Telegram bandwidth. Native decoding runs outside the main
application process and shares the existing transfer slots and staging admission.
Single compressed files have no trusted expanded size; their disk reservation
grows in 8 MiB steps as bytes arrive, with a free-space check before each write.

Solid 7z/RAR entries are consumed sequentially, avoiding a separate decompression
pass per file. Memory, CPU, output and metadata limits keep unusual archives
bounded; they can also reject valid archives with expensive dictionaries. See
[supported formats](usage.md#create-zips-and-extract-archives) and
[exact resource limits](api.md#archive-jobs). No archive throughput target
has been measured.

## Video controls and startup

TGDrive's custom Movi player keeps controls available through loading, buffering,
seeking and errors. It uses browser WebCodecs or software decoding through
WebAssembly (WASM). A file playing in a browser's native player does not establish
that this separate pipeline can decode it smoothly. **Try again** and
**Download Original** remain available for slow starts or unsupported files.
[Player controls](usage.md#open-preview-and-download).

Unknown-duration HTTP media stays subject to network scan limits, including on
localhost; a duration may remain unavailable. Closing the preview also closes
its owned background duration scan. This is separate from bounded server
read-ahead and does not promise fast startup for every container or codec.

The waiting copy follows the player's engine, opening, track-preparation,
buffering and seek phases. Retry appears on an explicit error or a sustained
wait without byte/playback progress; a separate playback deadline still permits
retry when arriving bytes do not make the decoder usable. Source and seek
changes reset that wait tracking.

The animated waiting status shows measured bytes received and current delivery
speed when available. Bytes include repeated reads/seeks, not a whole-file
completion percentage. The compact strip keeps retry inline and omits zero-speed
text; advancing frames clear stale waits. Timeline ranges estimate the browser's
buffer, separately from server cache. A whole-file preload is optional.

If no presented picture advances for over 1.25 seconds, the wait debounce confirms
a stall before holding scheduled audio and the playback clock. Fetching/decoding
continue until the coordinated readiness gate can resume both. Intentional
pauses/seeks, background audio, audio-only playback and completed video tails
remain independent; picture-in-picture still requires advancing pictures.

Video-only files apply decoder backpressure rather than reading ahead for a
nonexistent audio track. At end-of-file, both decoding paths drain pending
pictures before reset; software decoding submits end-of-input through WASM to
preserve delayed B-frames. Audio-only video containers do not wait for pictures.
Closing/replacing the player removes listeners, recurring timers and playback
resources. Cached delivery still needs a device capable of decoding the file.

### Seek and chapter previews

Seek hover uses keyframe previews, with the existing cache and pacing for remote
reads. It does not walk every intermediate frame to produce an exact-time image.
Chapter titles and seek targets appear immediately; image extraction is limited
to visible cards and a small neighbouring margin, one at a time. Hover takes
priority when its card can be shown; while Chapters is open, hidden hover work
is suppressed. Chapter work waits during loading, buffering and seeking. Closing
the chapter panel stops new chapter extraction. No synthetic twenty-image strip
is generated for files without chapter metadata.

The preview decoder initializes on demand, without automatic background warm-up.
It borrows playback's cached head, metadata and retained source bytes before
requesting additional ranges. Newly downloaded preview bytes are also offered
back to that same playback source through a separate, bounded 8 MiB cache, so
playback can reuse complete retained ranges without moving its active window.
This cache is additional browser memory, has no persistent storage, and clears
when the source closes. Evicted ranges can require another download.
An uncached frame can require several reads; typical windows are 2 MiB, with a
5 MiB per-read ceiling. Each new image has a 16 MiB allowance for requested range
payloads, including metadata and retries; cached bytes do not spend it. If more
reads are needed, that image is skipped and playback remains available. This is
not an exact wire-traffic cap or a limit on the whole viewing session.
Generated images are cached by the player for revisits during
that opening; changing source or reopening can require new reads. The isolated
preview WASM/decoder also needs memory and CPU, so previewing high-resolution
software-decoded files can still be slow.

### Browser playback buffer

The custom player fetches compressed bytes independently of the small decoded
picture queue. Its speculative byte target represents **90 seconds at the file's
average byte rate**, or the declared container bitrate when that is higher.
Demuxer cursor jumps during pauses, seeks and metadata reads cannot inflate this
target. Required demux reads can exceed it so variable-bitrate sections still
receive the bytes they need. Once filled, speculative fetching resumes below
75% of the byte goal.

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

Software and lower-resolution mobile decoded-picture targets also account for
estimated RGBA bytes, using 64 MiB on mobile/low-memory devices and 128 MiB
otherwise, while retaining the minimum coordinated-resume cushion. The existing
high-resolution WebCodecs calibration is preserved. These are queue targets,
not a total tab-memory limit: codec references, WASM, audio, graphics surfaces and
temporary copies remain separate. WebCodecs statistics do not claim verified
hardware acceleration; the browser can decode through software internally.

Pause-time demuxing aims for a small resume cushion: five seconds of audio and
60 video packets, subject to the existing packet count and a 16 MiB
compressed-packet target. The packet that crosses a byte limit is retained.
Sparse/interleaved tracks can exceed the time target before both track goals
are satisfied. The HTTP source can still fill its separate read-ahead window;
pausing does not discard bytes already downloaded.

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
server cache and manual whole-file preloads. This is a byte-based target, not a
strict 90-second limit measured from the displayed frame. Retained packets,
variable bitrate, required reads and in-flight responses can extend coverage.
The grey timeline shows approximate loaded coverage; hovering shows an image
and time without adding a second filled range that could resemble a buffer.
Neither the grey range nor a preview image guarantees decoded readiness.

## Delivery edge cases

### Browser transport with a full cache

A ready cache removes origin payload work, not viewer transport. Browser HTTP/3
can differ from curl/Python HTTP/1.1 or HTTP/2. TGDrive clears alternative-service
advertisements by default. Check [proxy configuration](operations.md#http-delivery-and-proxy-configuration)
before attributing
warm-cache stalls to Telegram or raising budgets.

### Distant video seeks

Compressed read-ahead counts only bytes inside the current source window.
Moving to a distant position must fetch its first range even while the demuxer
still reports its old read position; skipped file bytes are not buffered data.
The loading overlay shows time spent waiting and bytes received for that seek,
rather than repeating a cumulative count from earlier playback. It remains a
waiting state until actual playback frames advance. Source delivery, codec/device
support and decoding still affect seek latency.
