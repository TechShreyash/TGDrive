# Diagnose load and choose capacity

[Docs home](README.md) · [Configuration](configuration.md) · [Performance policies](performance.md)

Use the website's measurements to find what is slowing down before changing
limits or buying a larger server. Cache, server resources and Telegram clients
solve different problems; increasing the wrong one can make queues longer.

**Contents**

- [Start with the website stats](#start-with-the-website-stats)
- [How cache helps](#how-cache-helps)
  - [Cache capacity and preloading](#cache-capacity-and-preloading)
- [Find the bottleneck](#find-the-bottleneck)
- [What a server upgrade changes](#what-a-server-upgrade-changes)
- [Bots and request concurrency](#bots-and-request-concurrency)

## Start with the website stats

1. Identify the slow stage in **Transfers**: sending from your device,
   downloading from a URL, waiting for Telegram, uploading to Telegram or
   publishing. A full device progress bar does not mean Telegram work is done.
2. Open **Monitor → Performance** while the problem is happening. Compare
   server resources, network traffic, Telegram activity and queue waits over
   the same interval. Long averages can hide short bursts; missing samples are
   not zero activity.
3. Open the same file again, or compare its uncached and already cached parts.
   A slow first access followed by a fast repeat points toward the Telegram
   path or cache coverage. If both are slow, inspect server-to-viewer delivery
   and the viewer's device too.
4. Open **Monitor → Preloads** to inspect actual retained coverage. For upload
   space problems, also check **Management → Storage & Backups → Temporary
   Storage**. Keep unfinished upload files; they are not disposable cache.
5. Change one setting or resource, repeat the same workload and keep the change
   only if measured results improve. Do not increase concurrency during a
   Telegram cooldown.

| Website measurement | How to read it |
| --- | --- |
| Container CPU | 100% means all available container cores/quota, not one core. A labelled host-CPU fallback has a different scope. |
| Container RAM | Includes the whole container. Process memory and the retained RAM cache are separate measurements within it. |
| Server network | **Download to server** is incoming traffic; **Upload from server** includes delivery to viewers and Telegram uploads. Compare with the bandwidth included in your server plan. |
| Telegram accounts and queues | Look for busy upload/download slots, active and queued part requests, reduced effective limits and provider waits. A queue estimate is not a promised start time. |
| RAM cache lookups | Hits and misses count block lookups, including preloading. A RAM miss can still hit disk; this is not a percentage of bandwidth saved. |
| Cache filesystem / Temporary Storage | Cache and unfinished uploads can live on different volumes. Check the relevant free-space reading before changing either budget. |

Server-network, HTTP-file and Telegram counters overlap: **do not add their
totals together**. For history, measurement scopes and retention, see
[reading Performance](performance.md#read-the-performance-page).

## How cache helps

TGDrive's server cache retains file bytes already fetched from Telegram:

1. A read can reuse retained bytes from RAM or verified disk cache.
2. Missing blocks are fetched from Telegram. Overlapping readers can share
   work already in progress instead of fetching the same blocks separately.
3. Retained blocks can serve later authorized reads of that same stored file
   and representation, including reads through share links.

For example, if a **1 GiB file is fully cached and stays cached**, three people
downloading that original can reuse the cached bytes. TGDrive can avoid another
Telegram payload download for each person, while the server still sends about
3 GiB to the viewers, plus network overhead. This is an illustration, not a
throughput measurement or a guarantee that the file remains cached.

This is reuse of the **same stored content and byte ranges**, not duplicate-file
detection. Separately uploaded copies or different quality representations do
not automatically share a cache entry. Authorization and metadata checks still
apply; caching avoids repeated file-payload downloads, not every Telegram request.

### Cache capacity and preloading

- More available RAM can keep more frequently reused bytes in memory. More disk
  capacity can keep a larger collection of cached files or parts.
- More cache capacity can let you preload larger files or retain more prepared
  files. It does not automatically increase **Concurrent preloads** or Telegram
  request capacity. Background preparation shares capacity with real viewers
  and uploads.
- Hardware upgrades do not automatically raise **Configuration → Cache settings**
  budgets. Raise them deliberately while leaving space for the app, active
  streams, unfinished uploads and other processes. The effective RAM budget can
  be lower than the configured value when memory is tight.
- A whole-file preload must fit either the effective RAM tier or disk tier,
  including overhead and competing reservations. Their budgets are not added
  together; both tiers can hold copies of the same bytes.
- Ordinary access may leave only part of a file cached; automatic playback
  warming can prepare more, but does not always prepare the whole file. For a
  file you expect many people to open, choose **Preload file**, then wait for
  **Ready** and full coverage in **Monitor → Preloads** before sharing.

Cached data can expire, be evicted, disappear with disk loss or fail an integrity
check. Missing bytes then need Telegram again. Cache is not a backup and does not
keep a sleeping host available. See [cache defaults and retention](performance.md#cache-limits)
and [preloading a shared file](performance.md#prepare-a-file-before-sharing).

## Find the bottleneck

| What you observe | What to check or change |
| --- | --- |
| First access is slow, cached repeat is fast | Inspect Telegram queues, waits and cache coverage. Retain or preload popular content; consider additional clients only if account capacity is limiting cold reads. |
| Cached files slow down with more viewers | Compare server upload bandwidth, CPU, memory and proxy limits. More bots do not carry these cached payloads to viewers. |
| CPU stays near its available limit | Identify server processing versus a slow viewer. Reduce competing work or consider more CPU for a confirmed server bottleneck. |
| RAM is close to its limit or effective cache shrinks | Lower memory cache or concurrency first. More RAM may provide headroom; raising the cache setting without headroom can worsen pressure. |
| Useful cached coverage disappears between repeat accesses | Check retention, effective budgets, free space and competing files. Increase cache capacity only if reuse justifies it. |
| Cache or upload volume is nearly full | Review its own budget and free-space reserve. Clear disposable cache or finish/cancel transfers; preserve unfinished upload data. |
| Telegram slots are busy and queues grow, with server headroom | Separate demand from background preloads. Add eligible clients or test one concurrency change at a time. |
| Telegram reports FloodWait or reduces effective request limits | Observe the retry deadline and reduce pressure. More queued work or higher limits do not shorten the wait. |
| The server is delivering quickly but one viewer buffers | Inspect that viewer's connection, browser, codec support and decoding load. Server CPU and bots cannot guarantee client playback. |
| CPU and network look quiet but requests remain slow | Check Connections, logs, proxy behavior and MongoDB health. The website does not measure every dependency's latency. |

## What a server upgrade changes

| Resource | When it helps | Practical limit |
| --- | --- | --- |
| CPU / vCPU | Server processing is saturated: transfer checksums, encryption, thumbnail generation or document extraction compete for CPU. | Much video decoding runs in the viewer's browser. Additional server CPU does not fix every codec or viewer problem. |
| RAM | The app and concurrent streams need more headroom, or a useful working set cannot remain in RAM cache. | Configure a suitable cache budget; TGDrive still reduces retention under memory pressure. More RAM does not make all reads immediate. |
| Storage capacity | Popular cached content or unfinished uploads do not fit. | Cache and upload staging have separate budgets/reserves. A larger disk does not automatically increase either configured budget. |
| Storage speed | Host measurements show disk reads/writes are limiting cached delivery or staging. Faster SSD/NVMe may help. | A disk-usage bar measures capacity, not I/O performance. The website does not report disk latency or IOPS; use host/provider monitoring. |
| Network capacity and route | Server ingress/egress or its path to Telegram/viewers is the bottleneck. | Viewers still have their own connection limits, and monthly bandwidth allowances can apply. |

Also use provider/host/database monitoring when needed: TGDrive does not report
the server's physical network-port capacity or MongoDB response latency.
Compare improvements with the same mix of cold reads, cached reads and uploads.
Keep **one active TGDrive installation and one app worker per drive**; increasing
workers against the same drive is not the supported way to use a larger server.

## Bots and request concurrency

Additional healthy bots can provide more Telegram account capacity for cold
downloads and uploads. They help when that capacity is the bottleneck; they do
not multiply server bandwidth or eliminate shared channel/provider limits.
Repeated reads served from cache need less Telegram payload capacity already.

These controls affect different layers:

| Control | What it changes |
| --- | --- |
| Telegram clients in Connections | Potential account capacity; only healthy, authorized clients with channel access can do work. |
| Simultaneous uploads per bot | Concurrent Telegram document uploads per bot. One large file can use several document-upload slots. |
| Media operations per account | Upload/download work sharing an account's capacity. |
| Requests per account | Concurrent Telegram media-part and metadata requests, shared by uploads and cold downloads. |
| Upload pipeline | Part requests in flight within one Telegram document; still constrained by the account request limit. |
| Requests per second | TGDrive's pacing ceiling, not a rate promised by Telegram. |
| Maximum queued operations | How much work can wait; a larger queue does not add processing capacity. |

With fewer bots, increasing an appropriate per-account limit can allow more
work at once, but can also increase contention and provider waits under sustained
load. A higher configured value is not evidence of higher throughput. TGDrive
reduces effective request capacity/pacing after flood feedback and gradually
recovers it after successful work.

Start at the defaults, confirm server headroom, then change one control and watch
throughput, waits and queue growth over a representative interval. If waits grow
without useful throughput gains, undo the increase. Add or change clients through
the [connection maintenance workflow](usage.md#settings-and-connections), including
its pending-work and restart requirements.

Telegram documents a required wait before repeating an action after
[`FLOOD_WAIT_X`](https://core.telegram.org/api/errors), and enforces
[administrator and bot limits](https://core.telegram.org/method/channels.editAdmin).
Respect those limits and TGDrive's saved retry deadlines; repeatedly restarting
or rotating identities is not a way to bypass them. TGDrive uses Telethon/MTProto,
so hosted HTTP Bot API broadcast limits are not its media-request capacity.

For current control defaults and bounds, use
[Transfer limits](configuration.md#transfer-limits). For scheduling details, use
[account selection](architecture.md#account-identity-and-selection) and
[the upload pipeline](performance.md#upload-pipeline-and-smaller-splits).
