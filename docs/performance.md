# Streaming, caching and transfer performance

[Docs home](README.md) · [Using TGDrive](usage.md) · [Configuration & tuning](configuration.md)

TGDrive can reuse Telegram data and overlap network requests, but the slowest
link still determines the result. A cache improves repeat access; a cold file
still needs to reach the server from Telegram.

## Follow the data path

| Stage | What can limit it |
| --- | --- |
| Device → server | Your upload connection, server ingress, temporary disk, browser receiving queue |
| Server → Telegram | Account routing/datacenter, round-trip time, request pipeline, Telegram cooldowns, server egress |
| Telegram → server | File datacenter, network route, eligible accounts, request admission, cold-cache misses |
| Server → viewer | Server egress, proxy behavior, viewer connection, slow concurrent viewers |
| Video decoding | Container layout, supported codecs, browser/device decoding capacity |

Telegram accounts and documents can refer to different datacenters. The client
resolves file locations and account-specific authorization. A different server
region can change latency and routing, but changing a setting cannot relocate
an existing Telegram document or guarantee a faster route.

TGDrive retains Telethon with cryptg. It uses bounded upload chunk pipelining,
pooled download reads, and shared caches; it does not depend on a replacement
Telegram client library to enable these features.

## Cache limits

Open **Configuration → Cache settings** for cache storage and preparation,
**Transfer limits** for upload splitting and request limits, and **Timezone**
for display time. Saving changes only the current category; other drafts stay unsaved. TGDrive uses one dark appearance; logout follows the navigation in the scrollable sidebar.
The existing `?view=settings&section=performance` deep link still opens cache controls.

The application defaults are **256 MiB RAM** and **10 GiB disk**, with a
**1 GiB free-disk reserve** and **24-hour disk retention after reuse**.
These are instance-wide byte budgets, shared by playback, downloads, and preloads.
They are separate from the upload staging budget.

These are portable app defaults, not a promise of ideal values for every server.
The controls show the current policy and label the built-in defaults separately.
Startup environment values can supply different initial limits; saved dashboard
preferences in MongoDB take precedence. Both Compose files forward the optional
performance variables. The recommended way to tune an existing installation is
the dashboard, without an environment edit or redeployment. **Reset Changes**
restores the last saved values. Units are binary: 1 GiB = 1024 MiB.

- RAM is further clamped to a quarter of currently available host/container
  memory. Configured and effective limits can therefore differ. RAM blocks
  expire after 10 idle minutes; reuse refreshes that timer. The retention
  control changes disk expiry only, measured from the last reuse.
- Disk bytes are allocated as content arrives; the budget does not reserve
  that disk space in advance. Writes stop before crossing the free-space floor.
  This floor applies to cache writes, not all server activity. Upload staging
  separately observes `MIN_FREE_BYTES`; other disk users can consume space.
  Leave headroom for the operating system, uploads and backups when choosing
  the cache cap and floor.
- Disk blocks include integrity overhead, so disk-only preparation needs
  slightly more space than the raw file. RAM-only preparation counts payload
  bytes; process/index overhead remains outside the retained-byte budget.
- RAM and disk can contain the same hot bytes. Their capacities cannot simply
  be added when deciding whether a whole file fits.
- The bounded disk write queue shares the RAM budget. Slow disk cannot create
  an unbounded in-memory backlog.
- A disk failure falls back to Telegram where possible. A full preparation
  reports failure if verified retained coverage cannot be achieved.

Active responses also share a separate transient payload budget: **32 MiB** by
default, reduced with available host/container memory, with a **2 MiB minimum**
to keep streaming possible when retained RAM caching is disabled. Queued,
completed and currently yielded source blocks hold reservations until consumed
or cancelled. New response heads can reclaim another reader's speculative
read-ahead, including when its consumer is suspended. Awaited/yielded heads
stay protected; cancelled I/O retains its reservation until cleanup finishes.
A stalled download therefore cannot monopolize the entire budget with unused
ahead blocks and block a cached metadata/seek request. This is additional to
retained cache RAM. Transport, ASGI, TLS,
kernel and browser decoder buffers are outside these payload budgets.

Blocks first enter a probation area. Reused blocks enter a protected area,
normally up to 75% of a tier, so one large one-time download is less likely to
evict a frequently watched video. Protection is an eviction preference, not a pin.

The cache is disposable local data. It is neither a file backup nor a CDN.
Clearing it leaves Telegram originals and MongoDB catalog entries intact.

## Automatic read-ahead

An ordinary response reads ahead through ordered **1 MiB blocks**. Its window
is bounded by account capacity, 32 blocks, and its share of the transient
budget. A lone viewer can use the full budget; concurrent consumers share it.
Waiting response heads receive priority over additional prefetch. Multiple healthy accounts can
fetch independent blocks, and simultaneous viewers can share an in-flight fetch
of the same block. The server emits bytes in their original order.

Automatic background preloading begins only after **4 MiB of sequential bytes
have actually been delivered** during playback or a download. A first viewing
warms a bounded region ahead;
it does not automatically prepare the whole movie. Metadata, HEAD requests,
and tiny range probes do not qualify as delivered viewing data.

- Whole-file preparation requires repeated use: two sequential runs of at
  least 4 MiB must overlap by at least 4 MiB within ten minutes. This can be
  another viewer or a replay. The file plus overhead must also fit within
  half an effective cache tier.
- Read-ahead adapts to measured consumption and background fill rates, targeting
  roughly 8–16 seconds ahead within 4–32 MiB and a quarter of effective
  preparation capacity. It skips speculation if even 4 MiB does not fit.
- A distant seek abandons that viewer's stale ahead work. When the last viewer
  leaves, bounded ahead work stops; an already justified whole-file preparation
  can finish.
- Ordinary automatic starts have a 30-second per-file cooldown; newly observed
  repeated use can promote a file immediately.
- Manual and automatic preparations share a default maximum of two jobs with
  bounded block concurrency. Raising this limit does not add account capacity.
- Foreground playback/download requests receive priority over speculative work.

Disabling automatic preloading leaves manual **Prepare for Download / Streaming**
available. Ordinary demand reads still fetch requested bytes and use their bounded
response read-ahead; the switch controls additional background preparation.

Cancelling one viewer or preload does not cancel a shared fetch that another
authorized viewer still needs. Bytes already delivered cannot be recalled.

## Prepare a file before sharing

Choose **Prepare for Download / Streaming** from a file's actions in **My Files**,
then inspect progress in **Preloads**.
Preparation uses the same verified block cache as ordinary downloads, including
files stored as multiple Telegram documents.

1. Choose the file and start preparation.
2. Check **ready bytes**, **ready ranges**, and completion.
3. Share the file after it is fully prepared if cold Telegram reads would be
   disruptive for the expected viewers.

Preparation is admitted only when the requested range fits an effective tier.
Concurrent preparations reserve capacity to prevent overcommitting the budget.
The job verifies retained range coverage before reporting ready; bytes merely
downloaded and then evicted do not count as a prepared file.

You can cancel preparation or clear cached bytes without deleting the file.
Lowering a budget, eviction, expiry, disk loss, or restart can change current
coverage. Check the current inventory rather than treating a historical
completion message as a permanent availability guarantee.

## Shared-download load and queues

Warm bytes can be served repeatedly without re-fetching their payload from
Telegram. Missing blocks are shared when viewers request them concurrently.
Many viewers requesting unrelated files still compete for finite server and
Telegram capacity.

TGDrive bounds Telegram media operations, part RPCs, preload work, cache budgets,
and origin wait queues. Upload/download activity shares an account budget;
increasing upload concurrency cannot create unlimited independent read slots.
Demand work has priority over background work. Cache hits avoid origin payload
work but still consume server bandwidth and require access checks.

Warm-cache responses are not restricted by a fixed four-viewer gate; server
bandwidth, sockets, memory and proxy capacity still limit how many viewers can
be served. Cold misses enter bounded origin admission.

Queue position and wait time are **estimates** based on recent observed work
and eligible account capacity. An estimate may be unavailable before enough
samples exist. It can change after a seek, a slow viewer, an account outage,
or a Telegram cooldown. It is not a completion deadline or reserved bandwidth.
An overloaded bounded queue can reject work; clients must respect the returned
status and retry guidance instead of opening unlimited parallel requests.

## Device upload receipts

Browser uploads use sequential **8 MiB** resumable chunks. Live browser progress
shows bytes sent; the confirmed offset advances only after the server has written,
checksummed and synchronized the chunk and committed its receipt. New uploads do
not hash the whole file on the device before sending. Reselecting a file to resume
does verify already-received chunks against their saved checksums.

File writes and hashes run off the HTTP event loop. MongoDB receipt commits run
in the same owned worker, so a slow cloud transaction does not freeze other
playback, download or progress requests. A disconnect drains an in-progress write
and receipt commit before releasing that upload's lock; it cannot acknowledge
uncommitted bytes. SQLite retains its existing transaction ownership.

Device-to-server receipt speed and server-to-Telegram speed are separate stages.
More bots improve eligible Telegram capacity; they cannot increase a device's
uplink or bypass reverse-proxy limits. Compare the transfer's device stage with
HTTP received traffic and current server load before tuning Telegram concurrency.
A server-local receipt benchmark measures application/disk overhead, not a
physical viewer's upload route.

## Upload pipeline and smaller splits

These controls govern different layers:

| Control | Effect |
| --- | --- |
| Uploads per bot | Concurrent Telegram documents on a bot; default 1, allowed 1–4 |
| Upload pipeline | Outstanding 512 KiB part-upload RPCs within a document; default 4 |
| Account request limit | Upper bound on concurrent Telegram media/metadata RPCs; default 8, allowed 1–32 |
| Account part rate | Application pacing ceiling including scheduled metadata work; default 48 requests/second, allowed 1–128 |
| Combined media limit | Shared upload/download operations per account; default 3, allowed 1–16 |
| Maximum queued operations | Waiting Telegram origin operations across the installation; default 256, allowed 1–4096. More queue space allows longer waits, not higher throughput. |
| File split size | Preferred document cap, 1–2000 MiB; 0 uses the safe account maximum, including an eligible Premium document |

The defaults balance observed throughput and cooldowns on the tested deployment;
different accounts, datacenters and routes can need lower values. More parallel
work can trigger waits and reduce throughput. The rate ceiling is TGDrive's
protective application setting, **not a published Telegram bandwidth entitlement**.
Real provider waits override it.

The pipeline is also bounded by the shared account request limit. Media and
queue limits count Telegram origin work, including missing-block reads; they
are not limits of three viewers or 256 whole files. Warm payloads do not need
Telegram download slots. Keep the defaults initially, measure a representative
workload, and change one control at a time. Reduce concurrency or pacing if
cooldowns grow or playback competes with uploads; raise them only when observed
throughput improves without those costs.

Smaller documents can engage more available account slots for one file. They
also add messages, manifests, retry/cleanup work, and download boundary setup.
They do not change the original bytes or automatically increase download speed.
Chunk pipelining can improve an individual document without splitting it.
Automatic sizing can use an eligible Premium document up to 4000 MiB; bots use
the 2000 MiB safe maximum. An explicit cap at or below 2000 MiB creates
bot-compatible parts that can use the available bot and optional-user pool.
For a very large file, the planner raises a tiny requested split if needed to
stay within the 4096-part ceiling. Changing the preference does not re-split
an already journaled transfer.

Keep one upload per bot initially. Compare a smaller split and a modest
pipeline on representative files, then keep changes only when useful throughput
improves without degrading streaming or increasing sustained cooldowns.
Never rotate around an account/channel wait to defeat Telegram restrictions.

Completed parts retain their durable identities. New preferences do not
re-split an upload that already has a journaled plan. The logical file remains
bounded by staging, file-size configuration, and a 4096-part manifest ceiling.

## Video controls and startup

The enhanced player loads locally when a video is opened. Supported embedded
audio and subtitle tracks use the player's controls; codec support depends on
the media and device. Real chapter titles/times appear without waiting for
remote thumbnail generation, including in fullscreen.

Focus the player to use its keyboard shortcuts, including **K/Space** for
play/pause, **M** for mute, and arrow-key seeking. Text fields keep their normal
editing behavior. Chapter controls are keyboard accessible.

Startup and buffering are distinct. Advancing playback clears stale buffering
feedback, and short interruptions do not immediately cover the picture.
The custom player and its normal controls remain visible during opening and
seeking. There is no separate Start Playback screen or native-browser
compatibility mode. A mobile browser may still require a tap on the player's
normal play control before it permits playback with sound. For a slow start,
choose **Reload Video**. An unsupported codec stays in the custom player with
a useful error; **Download Original** remains available in the header.

The loading panel shows measured received bytes and transfer rate when
available. It does not turn source-byte coverage into a playback-ready percentage
or seconds ahead: decoded audio and video may still be waiting. The timeline
highlights the engine's buffered time
ranges, including before playback and after a seek. Movi estimates some HTTP
time ranges from its source window and media rate, so these are approximate.
They describe the player's current buffer, not the server's preload inventory
or a promise that every frame can decode in real time. Server-cached bytes
can still require network transfer to the device and local decoding.

## Operator checklist

1. Measure server HTTP download and upload capacity separately, using more than
   one reliable endpoint. One external host can be the bottleneck.
2. Compare cold Telegram reads with a repeat of the same authorized range.
   A warm-cache result does not measure Telegram origin throughput.
3. Watch cache hit/miss counts, effective RAM, disk headroom, queued operations,
   account cooldowns, and server CPU/network utilization.
4. Compare the real pooled HTTP path with any standalone client benchmark.
   They use different caching, admission, and backpressure paths.
5. Test video startup and seeks, then same-file and different-file viewers.
   Do not infer many-viewer performance from one sequential download.
6. Adjust one limit at a time. Keep default limits until measurements identify
   which stage needs attention.

For exact environment bounds and owner endpoints, see the
[reference](reference.md#performance-and-cache-settings). Restart after changing
deployment environment values; saved live performance preferences apply through
the owner's settings controls.

### Read the Performance page

**Monitor → Performance** separates **server network traffic**, **TGDrive HTTP
file traffic**, and **Telegram origin activity**. These layers overlap. Do not
add their totals together: a Telegram download followed by viewer delivery crosses
multiple layers. Network counters include protocol overhead, URL imports and
other traffic on the selected interface. In Docker this normally measures the
container interface; a source installation can share an interface with other apps.

The two network lines are **Download to server** (received, blue) and **Upload
from server** (sent, dashed). Hover anywhere in a graph to inspect the nearest
recorded timestamp. The sample scrubber provides the same values for keyboard
and touch. Labels marked **Now** remain current readings, even when an older
period is selected.

Choose **Live**, **Last hour**, **Last day**, **Last week**, **Last month** (30 days)
or **Custom**. Live keeps five minutes of three-second samples. Recorded history
keeps 31 days of measured minute aggregates, reduced to at most 360 points for
larger ranges. Recorded points show averages over seconds actually observed;
they are not peaks or an assumption that unobserved time was idle. Custom inputs
are explicitly UTC, with the applied interval shown in the configured timezone.
There is no backfill for time before monitoring was installed. Missing/offline
readings stay gaps, and incompatible measurement scopes are not joined together.

**All-time totals** are stored separately and survive restarts and rolling
history pruning. “All-time” means **since the displayed tracking start date**.
Server network, Telegram upload/download and tracked HTTP body totals stay
separate. The HTTP counters cover `/file` and resumable-upload bodies, including
cached delivery and small receipt messages; assets and settings polling are
excluded. Existing detailed process counters still reset on restart. No total is
presented as the physical network capacity or as a bandwidth-savings percentage.

History and totals use the existing MongoDB in production. Local SQLite installs
use `DATA_DIR/telemetry.sqlite3`. There is no extra metrics service, volume or
required environment value. Checkpoints run once per minute and at clean
shutdown. An abrupt crash can lose roughly the last minute of statistics;
a diagnostics-storage outage can lose chart observations. This does not affect
file data. Telemetry is disposable and excluded from encrypted catalog backups.

CPU uses a fixed 0–100% scale, measured against available container cores and
quota. RAM shows current cgroup usage and its finite limit when present. If no
limit is configured, the chart explicitly uses an observed scale rather than
implying that the container is full. Host CPU is a labelled fallback; load
averages are diagnostic context, not CPU percentages. Process memory, container
memory, host availability and retained cache bytes measure different things.

### Investigate a slowdown

Compare signals during the actual problem; a high value by itself does not
identify a cause. Use the same time window for the network, CPU and RAM graphs.
Account activity, provider cooldowns and detailed queue observations are current
or recent process state; they are not a month-long per-request log.

| What you observe | What to check next |
| --- | --- |
| Cold reads wait, warm reads are quick | Telegram account capacity, request queues/cooldowns and whether the requested ranges are cached |
| Bot slots are full and demand is queued | Active foreground work versus manual preloads; compare throughput before adding capacity |
| High CPU with poor throughput | Container CPU quota and workload; compare a raw download with video processing |
| RAM approaches its configured limit | Container usage, active stream memory and effective cache budget; host totals alone do not show container headroom |
| Cache filesystem reaches its reserve | Free disk and configured budgets; cache may stop admitting bytes even though originals remain available |
| Server delivery is quick but one browser is slow | Browser protocol, viewer route, device decoding and actual client buffered data; a warm server cache does not prove fast browser delivery |
| URL import completed but current download rate is zero | Check the historical interval and lifetime network receive total; current idle rate correctly returns to zero |

Change one setting at a time and compare the same workload. Queue position and
wait estimates can change with eligible bots, ongoing requests and Telegram
cooldowns. They cannot promise a precise completion time. Cache lookup hits and
misses count lookups, not bytes saved.

**Preloads** lists ready and partial files first. A ready file has verified cached
coverage. Every row shows bytes, percent and a positional coverage bar: blue
segments mark cached regions in a **Partial** file; a full green bar and **Ready**
label mark complete coverage. Matching preparation jobs show their loading,
queue or error status with the file. Clear one file or the whole cache after
confirmation; Telegram originals stay unchanged. Change instance budgets in
**Configuration → Cache settings**.

### Browser transport with a full cache

A ready server cache removes Telegram payload work; it does not measure delivery
to the viewer. Browser-selected HTTP/3 can behave differently from Python/curl
HTTP/1.1 or HTTP/2 on a particular connection. TGDrive defaults to clearing
alternative-service advertisements for predictable HTTPS delivery. See
[proxy operation](operations.md#http-delivery-and-proxy-configuration) and the
[fresh-browser validation](streaming-validation-2026-10-18.md) before attributing
a warm-cache stall to Telegram or raising cache budgets.
