# Streaming, caching and transfer performance

[Docs home](README.md) · [Using TGDrive](usage.md) · [Configuration](reference.md#configuration)

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

The application defaults are **256 MiB RAM** and **10 GiB disk**, with a
**1 GiB free-disk reserve** and **24-hour disk retention after reuse**.
These are instance-wide byte budgets, shared by playback, downloads, and preloads.
They are separate from the upload staging budget.

- RAM is further clamped to a quarter of currently available host/container
  memory. Configured and effective limits can therefore differ.
- Disk bytes are allocated as content arrives; the budget does not reserve
  that disk space in advance. Writes stop before crossing the free-space floor.
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
have actually been delivered**. A first viewing warms a bounded region ahead;
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
- Background work defaults to at most two jobs with bounded block concurrency.
- Foreground playback/download requests receive priority over speculative work.

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

## Upload pipeline and smaller splits

These controls govern different layers:

| Control | Effect |
| --- | --- |
| Uploads per bot | Concurrent Telegram documents on a bot; default 1, allowed 1–4 |
| Upload pipeline | Outstanding 512 KiB part-upload RPCs within a document; default 4 |
| Account request limit | Upper bound on concurrent Telegram media/metadata RPCs; default 8, allowed 1–32 |
| Account part rate | Application pacing ceiling including scheduled metadata work; default 48 requests/second, allowed 1–128 |
| Combined media limit | Shared upload/download operations per account; default 3, allowed 1–16 |
| File split size | Preferred document cap, 1–2000 MiB; 0 uses the safe account maximum, including an eligible Premium document |

The defaults balance observed throughput and cooldowns on the tested deployment;
different accounts, datacenters and routes can need lower values. More parallel
work can trigger waits and reduce throughput. The rate ceiling is TGDrive's
protective application setting, **not a published Telegram bandwidth entitlement**.
Real provider waits override it.

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
available, with an estimated buffered percentage and seconds ahead once the
engine has a duration. Unknown totals remain indeterminate rather than showing
a fabricated percentage. The timeline highlights the engine's buffered time
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

The Performance page separates **HTTP sent/received bytes** from Telegram
origin bytes. HTTP measurements cover `/file` and resumable-upload request and
response bodies, including warm-cache responses and small receipt messages;
settings polling and static assets are excluded. Throughput plots use actual
counter differences between samples. These are process-local application
observations, not a physical network-interface speed test or permanent history.
Restart resets the counters and bounded sample history.

### Browser transport with a full cache

A ready server cache removes Telegram payload work; it does not measure delivery
to the viewer. Browser-selected HTTP/3 can behave differently from Python/curl
HTTP/1.1 or HTTP/2 on a particular connection. TGDrive defaults to clearing
alternative-service advertisements for predictable HTTPS delivery. See
[proxy operation](operations.md#http-delivery-and-proxy-configuration) and the
[fresh-browser validation](streaming-validation-2026-10-18.md) before attributing
a warm-cache stall to Telegram or raising cache budgets.
