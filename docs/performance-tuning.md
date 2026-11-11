# Diagnose load and choose capacity

[Docs home](README.md) · [Configuration](configuration.md) · [Performance policies](performance.md)

Use this guide when uploads, playback or shared downloads are slow. Find the
busy stage first, then change one resource or account. Cache, server capacity and
Telegram accounts help at different stages; higher limits can lengthen queues.

Judge useful utilization by completed transfers, playback startup/seek delay and
responsiveness during concurrent work. CPU and RAM do not need to stay full.
Cached delivery, an idle workload or a provider wait can leave resources unused;
raising limits just to fill a meter can add contention and speculative reads.

**Contents**

- [Start with the website stats](#start-with-the-website-stats)
- [Read the Performance page](#read-the-performance-page)
- [How cache helps](#how-cache-helps)
  - [Cache capacity and preloading](#cache-capacity-and-preloading)
- [Find the bottleneck](#find-the-bottleneck)
- [What a server upgrade changes](#what-a-server-upgrade-changes)
- [Bots and request concurrency](#bots-and-request-concurrency)

## Start with the website stats

1. Find the slow stage in **Transfers**: device intake, URL downloading, Telegram
   waiting/uploading or publishing. A full device bar still needs server confirmation.
2. Open **Monitor → Performance** while the problem is happening. Compare
   server resources, network traffic, Telegram activity and queue waits over
   the same interval. Long averages can hide short bursts; missing samples are
   not zero activity.
3. Open the same file again. A slow first access followed by a fast cached repeat
   points toward Telegram or cache coverage. If both are slow, also inspect
   server-to-viewer delivery and the viewer's device.
4. Open **Monitor → Preloads** to inspect actual retained coverage. For upload
   space problems, also check **Management → Storage & Backups → Temporary
   Storage**. Keep unfinished upload files; they are not disposable cache.
5. Change one thing and repeat the same workload. Keep it only if results improve.
   Respect Telegram cooldowns before trying more concurrent work.

| Website measurement | How to read it |
| --- | --- |
| Container CPU | 100% means all available container cores/quota, not one core. A labelled host-CPU fallback has a different scope. |
| Container RAM | Includes the whole container. Process memory and the retained RAM cache are separate measurements within it. |
| Active stream memory | Transient payload reservations across streams, separate from retained cache and total process/container memory. A memory waiter reports admission pressure within this budget. |
| Server network | **Download to server** is incoming traffic; **Upload from server** includes delivery to viewers and Telegram uploads. Compare with the bandwidth included in your server plan. |
| Telegram accounts and queues | Look for busy upload/download slots, active and queued part requests, reduced effective limits and provider waits. A queue estimate is not a promised start time. |
| RAM cache lookups | Hits and misses count block lookups, including preloading. A RAM miss can still hit disk; this is not a percentage of bandwidth saved. |
| Cache filesystem / Temporary Storage | Cache and unfinished uploads can live on different volumes. Check the relevant free-space reading before changing either budget. |
| MongoDB size | TGDrive shows `dbStats.totalSize`, indexes and filesystem capacity where available. These are not Atlas's logical quota; use the [provider's quota monitoring](https://www.mongodb.com/docs/atlas/reference/free-shared-limitations/) for plan capacity. |

Server-network, HTTP-file and Telegram counters overlap: **do not add their
totals together**. For history, measurement scopes and retention, see
[reading the Performance page](#read-the-performance-page).

## Read the Performance page

**Overview** starts with **Account and request status**, such as **Accounts
ready** or **Waits or request errors observed**, with supporting detail. This
summarizes account availability, waits and request errors; it does not establish
whole-server health or available throughput. Insufficient recent-error history
is shown as unavailable rather than as zero failures.

**Current activity** shows recent observed workload and supported waits:
Telegram admission queues, cooldowns during queued work and stream-buffer
admission. Several waits can appear together; expand their evidence for measured
values. The panel distinguishes collecting samples, idle activity and stale or
unavailable evidence. A missing measurement is not zero, and no reported wait
does not establish spare capacity or identify a bottleneck.

Expand **Measurements and scope** to compare Telegram inbound with
HTTP bytes delivered and cache lookups over the observed window. In-flight
request counts are not user counts. The live strip, **Network traffic** and
**Resources** meters remain visible. Expand **Account capacity and cooldowns**
for detailed account rows, **Automatic tuning** for sizing values and reasons,
or **All-time totals** for persistent counters. The **Accounts** and
**Resources** tabs hold further detail; chart scopes and memory/cache
explanations are expandable there.

**Monitor → Performance** separates server network, TGDrive HTTP file traffic
and Telegram origin activity. They overlap: **do not add their totals**. Network
counts include protocol/other interface traffic; Docker normally measures its
container interface. In **Network traffic**, the solid line is **Download to
server** and the dashed line is **Upload from server**.
Hover or use the keyboard/touch sample scrubber. **Now** always means current.

Compare measurements over the same observation window, with the same workload,
cache coverage and container limits. Check the observation time: stale, missing
or insufficient samples cannot establish that the drive is idle or unconstrained.
A quiet window cannot establish the speed available to a later busy workload.

<details>
<summary>History, totals and resource measurement</summary>

- Live: five minutes of three-second samples. Recorded: 31 days, at minute
  detail for the last 3 days and hourly before that; charts over more than 3
  days, or any interval starting outside the last 3 days, use buckets of at
  least one hour, at most 360 plotted points. Presets include Last Month (30 days).
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

## How cache helps

The server retains bytes fetched from Telegram in RAM or verified disk cache.
Later authorized readers, including share-link viewers, can reuse them. Readers
needing the same missing blocks can also share an in-progress fetch.

For example, if a **1 GiB original stays fully cached**, three downloads can avoid
refetching that payload from Telegram. The server still sends about **3 GiB** to
viewers plus overhead. This illustrates the data path; it is not a measured speed.

Reuse follows the same stored file, quality representation and byte ranges.
Separately uploaded copies do not automatically share cache. Authorization and
metadata checks still apply, so even cached access can make Telegram requests.

### Cache capacity and preloading

- RAM retains frequently reused bytes; disk can hold a larger collection. More
  capacity does not increase preload or Telegram request concurrency.
  Background preparation still shares resources with viewers and uploads.
- Cache budgets follow measured memory and disk space; see [automatic tuning](performance.md#automatic-tuning).
  Leave room for the app, active streams, unfinished uploads and other processes;
  memory pressure can reduce the effective RAM budget.
- A whole-file preload must fit either the effective RAM cache or disk cache,
  including overhead/reservations. Adding their budgets overstates capacity:
  both may hold the same bytes.
- Ordinary playback may cache only part of a file. Before a busy share, choose
  **Preload file** and wait for **Fully cached** with full coverage on the
  original's row in **Monitor → Preloads**. **Poster cached** covers only the
  preview image, not the original video.

Expiry, capacity pressure, disk loss or a failed integrity check can remove
cached data; missing bytes need Telegram again. Cache is not a backup and does
not keep a sleeping host available. [Cache defaults and retention](performance.md#cache-limits).

## Find the bottleneck

An observed queue, cooldown, disk-admission refusal or stream-memory wait
identifies work that is waiting. CPU or memory pressure is only a likely cause
when sustained during relevant activity; one high sample is insufficient.
Several constraints can coexist. HTTP delivery backpressure means the response
cannot advance freely, but does not locate the cause at the viewer. Low
throughput alone cannot distinguish Telegram, its network route, server egress
or the viewer's connection; unmeasured capacity stays unknown.

| What you observe | What to check or change |
| --- | --- |
| First access is slow, cached repeat is fast | Inspect Telegram queues, waits and cache coverage. Retain or preload popular content; consider additional clients only if account capacity is limiting cold reads. |
| Cached files slow down with more viewers | Compare server upload bandwidth, CPU, memory and proxy limits. More bots do not carry these cached payloads to viewers. |
| CPU stays near its available limit | Identify server processing versus a slow viewer. Reduce competing work or consider more CPU for a confirmed server bottleneck. |
| RAM is close to its limit or effective cache shrinks | TGDrive shrinks its cache automatically. Stop competing work, or give the container more memory. |
| Useful cached coverage disappears between repeat accesses | Check free disk and competing files. More disk or memory raises the automatic cache budgets. |
| Cache or upload volume is nearly full | Clear disposable cache or finish or cancel transfers; preserve unfinished upload data. |
| Telegram slots are busy and queues grow, with server headroom | Separate demand from background preloads, then add eligible bots or a Premium account in Connections. |
| Telegram reports FloodWait or reduces effective request limits | Observe the retry deadline and reduce pressure. More queued work or higher limits do not shorten the wait. |
| The server is delivering quickly but one viewer buffers | Inspect that viewer's connection, browser, codec support and decoding load. Server CPU and bots cannot guarantee client playback. |
| CPU and network look quiet but requests remain slow | Check Connections, logs, proxy behavior and MongoDB health. The website does not measure every dependency's latency. |

## What a server upgrade changes

| Resource | When it helps | Practical limit |
| --- | --- | --- |
| CPU / vCPU | Server processing is saturated: transfer checksums, encryption, thumbnail generation or document extraction compete for CPU. | Much video decoding runs in the viewer's browser. Additional server CPU does not fix every codec or viewer problem. |
| RAM | The app and concurrent streams need more headroom, or a useful working set cannot remain in RAM cache. | The cache budget follows available memory automatically; leave room for the app and competing processes. More RAM does not make all reads immediate. |
| Storage capacity | Popular cached content or unfinished uploads do not fit. | Cache and upload budgets follow free disk automatically, each after its reserve. |
| Storage speed | Host measurements show disk reads/writes are limiting cached delivery or staging. Faster SSD/NVMe may help. | A disk-usage bar measures capacity, not I/O performance. The website does not report disk latency or IOPS; use host/provider monitoring. |
| Network capacity and route | Server ingress/egress or its path to Telegram/viewers is the bottleneck. | Viewers still have their own connection limits, and monthly bandwidth allowances can apply. |

Also use provider/host/database monitoring when needed: TGDrive does not report
the server's physical network-port capacity or MongoDB latency. Use database
dashboards to inspect latency, quotas and connection pressure alongside TGDrive's
resource readings.
Compare improvements with the same mix of cold reads, cached reads and uploads.
Keep **one active TGDrive installation and one app worker per drive**; increasing
workers against the same drive is not the supported way to use a larger server.

## Bots and request concurrency

Healthy bots can add Telegram capacity for cold downloads and uploads. They do
not increase server bandwidth or eliminate channel/provider limits. Cached reads
already need less Telegram payload capacity.

Startup values define concurrency ceilings, request pacing and queue sizes.
The governor sizes resources and caps preloads by CPU; Telegram's per-account
schedulers apply cooldown and recover effective limits within configured
ceilings after successful requests. Resource resizing preserves that feedback.
See [automatic tuning](performance.md#automatic-tuning).
Your levers are the accounts in **Connections** and the server's resources. Add
or change accounts through the [connection maintenance workflow](usage.md#settings-and-connections).

Respect saved retry deadlines and Telegram's
[`FLOOD_WAIT_X`](https://core.telegram.org/api/errors) / [account limits](https://core.telegram.org/method/channels.editAdmin).
Restarting or rotating identities does not bypass them. TGDrive uses
Telethon/MTProto; hosted HTTP Bot API broadcast limits are a different system.

Resource sizing and per-account feedback are separate; see
[automatic tuning](performance.md#automatic-tuning). For scheduling details, use
[account selection](architecture.md#account-identity-and-selection) and
[the upload pipeline](performance.md#upload-pipeline-and-smaller-splits).
