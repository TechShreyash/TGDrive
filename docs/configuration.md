# Configuration and safe tuning

[Docs home](README.md) · [Setup](setup.md) · [Performance diagnostics](performance.md)

Start with the built-in defaults. They are conservative budgets, not allocations:
TGDrive reduces retained RAM when host/container headroom is low, and stops disk
cache writes before the free-space floor is crossed. More CPU or RAM alone does
not remove Telegram pacing, network-route or client-device limits.

## Public demo mode

Set `DEMO_MODE=true` in the demo deployment's environment, then recreate the
Compose container or redeploy through Coolify. It defaults to `false`.
Configure the demo's accounts, limits and backups before enabling it.

Visitors can open every **Configuration** and **Management** page and inspect
the current values and status, but cannot change settings, add/remove accounts,
start Premium login, refresh Telegram connections, or run backup/recovery
management actions. Recovery-key downloads are also disabled; credentials
remain private. The server rejects these requests even when sent outside the UI.
If deployment configuration cannot load, these pages offer **Try Again** before
showing controls; file features remain accessible.

Uploads, URL imports, browsing, organizing, previews, downloads, sharing and
manual preloads keep their normal behavior and limits. Automatic backups and
other configured background work continue while TGDrive runs. Demo mode does
not bypass login: give visitors the demo deployment's password separately.

Use a separate MongoDB database, Telegram storage channel and credentials for
the public demo. Visitors share one drive and can change or permanently delete
its files, so populate it only with disposable content. Demo mode does not
isolate visitors, reset content or add quotas. Closing a visitor's browser follows
the [usual transfer rules](usage.md#what-upload-progress-means).

To change the deployment's settings again, set `DEMO_MODE=false` and
recreate/redeploy. The flag is deployment-only and cannot be changed in the app.

## Change, discard or restore

The sidebar separates **Monitor** (Performance, Preloads), **Configuration**
(Cache settings, Transfer limits, Timezone) and **Management** (Connections,
Storage & Backups). Choose Light, Dark or System from the appearance menu.

- **Save Performance Settings** applies only the open configuration category.
  Other edited categories remain drafts. New work uses the updated limits.
- **Discard changes** returns this category to its last saved values.
- **Restore defaults** loads this category's built-in defaults into the form.
  Review the values, then save to apply. It does not change anything immediately.
- **Parallel uploads** has its own Save Changes, Restore upload defaults and
  Discard upload changes actions. Its default is one upload per bot.

Defaults come from the server, independently of environment overrides. Saving
restored defaults creates a saved policy; it does not remove an environment
variable. Saved dashboard values take precedence over deployment startup values.
Stale saves from another tab return a conflict and require reloading current values.
Invalid values are rejected before writing; an apply failure attempts to restore
the previous policy. An error is displayed if reconciliation still needs a restart.

Restoring resource defaults never resets credentials, connected accounts,
MongoDB, files, shared links, backup keys or deployment identity. Connections
remain subject to credential/channel checks; do not replace those settings merely
to tune performance. Keep the [recovery kit](backup-recovery.md) independently.

## Cache settings

| Setting | Built-in default | What it controls | When to change it |
| --- | --- | --- | --- |
| Memory cache | 256 MiB | Recently fetched media retained in RAM, shared by viewers | Increase for repeated views when the container has sustained free headroom. The effective budget can be smaller than configured. 0 disables retention. |
| Disk cache | 10 GiB | Disposable downloaded bytes kept for reuse | Increase if repeated reads miss cache and the cache volume has space after upload staging and system needs. 0 disables it. |
| Cache free-space floor | 1 GiB | Free space cache writes must leave on their volume | Raise for active uploads, a small/shared disk or system growth. It does not allocate space. Dashboard changes cannot enable disk caching with less than 64 MiB left free; normal use should retain at least the 1 GiB default. |
| Disk retention | 24 hours | Time since each disk block was last reused | Increase for files repeatedly used across days. It does not pin files; capacity pressure can evict them sooner. |
| Concurrent preloads | 2 | Manual and automatic preparation jobs in progress | Reduce to 1 on a constrained server. Raise only after measuring cold reads and Telegram capacity; it creates no extra provider bandwidth. |
| Preload during playback | On | Warm a bounded upcoming region during original audio/video playback | Turn off to minimize speculative Telegram traffic and cache use. Requested ranges and adaptive response read-ahead still work; downloads do not start extra warming. Manual **Preload file** remains available. |

RAM and disk cache do not permanently store the originals. Original file payloads
remain in Telegram; catalog metadata remains in MongoDB. Retention and clearing
cache do not delete those originals. Active stream memory is a separate temporary
budget, although it still counts toward the container's total RAM.

A cache budget is not the same as the free-space floor: a 10 GiB budget caps cached
bytes, while a 1 GiB floor stops new writes if the disk becomes too full. Other
processes can still fill the volume. Upload staging has its own budget and reserve.

## Transfer limits

| Setting | Built-in default | What it controls | Tuning advice |
| --- | --- | --- | --- |
| Simultaneous uploads per bot | 1 | Whole uploads leased to each connected healthy bot | Add eligible bots before increasing parallelism. Extra slots share each account's request/media limits. |
| File split size | 2000 MiB | Document size for newly planned large uploads | Default and maximum for all accounts, including Premium. Choose 1–2000 MiB to reduce it; 0 also uses 2000 MiB. Smaller parts increase document and request overhead. |
| Upload pipeline | 4 | Upload part requests outstanding within one document | Try a small increase only when outbound throughput is low and there are no provider waits. |
| Requests per account | 8 | Shared in-flight Telegram requests | Uploads, downloads and metadata compete for these slots. Increasing can hurt cold playback. |
| Requests per second | 48 | Application pacing ceiling per account | Telegram cooldowns always win. This is not guaranteed throughput or a provider allowance. |
| Media operations per account | 3 | Concurrent origin upload/download operations | Warm cached viewers do not use these download slots. More operations can split the same bandwidth. |
| Maximum queued operations | 256 | Waiting origin work before excess work is asked to retry | Increasing permits longer queues, not faster processing. Prefer fixing the bottleneck. |

Advanced limits are bounded by the server, not only by the form. Fractions,
negative values, booleans pretending to be numbers, unsupported fields and values
above the allowed ceilings are rejected. Reducing limits does not enlarge the
underlying server or Telegram capacity. Supported large budgets are still operator
choices, not a guarantee that every workload will fit available resources.

## Upload size and available disk space

In **Transfer limits → Upload storage**, the owner can change:

| Setting | Default | Meaning |
| --- | --- | --- |
| Maximum file size | 16 GiB | Largest new file; Telegram document splitting is separate. |
| Temporary upload budget | 16 GiB | Combined reservation for all unfinished uploads. Must be at least the per-file maximum. |
| Upload free-space reserve | 1 GiB | Space left free on the actual upload volume after unfinished uploads are accounted for. Minimum configurable reserve is 64 MiB. |

To accept a 32 GiB file, raise both the file maximum and temporary budget to at
least 32 GiB. A budget of 64 GiB allows two such reservations when actual free
space also permits them. Raising a ceiling does not allocate space or bypass the
free-space check. Both controls support up to 2048 GiB. Existing receipts retain
their admitted size; lowering the budget below active reservations is rejected.

Before a resumable browser upload sends file chunks, the server reserves the
whole declared file against its staging budget and checks the upload volume's
real free space, subtracting other uploads' unwritten reservations and keeping
the reserve. URL imports and multipart receipts share this accounting. For a URL
without a known length, the server conservatively reserves the admitted maximum
until the source ends. This can reject an unknown-length import on a small disk;
provide a source with a Content-Length or reduce the per-file limit.

Space is rechecked while receiving data. Other applications, cache growth,
storage quotas, disconnects and device failures can still cause a later problem;
these cannot be ruled out by an initial check. Disk-full receipt writes return a
clear recoverable storage message and retain the acknowledged offset for resume.
Use separate persistent cache/staging volumes when appropriate. No upload limit
or staging budget is a claim about available storage in Telegram.

## A modest server and a larger server

For a fresh deployment, keep the defaults and run a representative upload,
cold playback, seek and repeat playback. Do not raise every number together.
On a small/shared instance, consider one preload and one upload per bot; retain
free disk space and a realistic container memory limit in the deployment platform.
The application cannot protect the machine from unrelated processes or an
undersized container, and the largest playable codec/file is device-dependent.

On a server like 8 vCPU / 16 GiB RAM / 300 GiB disk, first check actual container
limits and other disk consumers. A measured starting experiment can raise RAM
cache to 2 GiB, then 4 GiB if there is ample headroom; disk cache to 100–150 GiB;
and the free-space floor to 20–30 GiB. Leave Telegram request limits unchanged
initially. These are optional experiments, not replacements for the portable defaults.

Change one limit, repeat the same workload, then compare:

| Observation | Investigate before increasing limits |
| --- | --- |
| Container RAM approaches its limit | Lower RAM cache/concurrency and inspect active stream/process usage. |
| CPU stays near capacity | Media decoding/preview work and concurrent operations; more requests may worsen it. |
| Telegram work waits or accounts cool down | Account eligibility, provider waits and shared request limits. |
| Cold playback is slow but repeat access is fast | Telegram route/latency and cache reuse, not browser cache alone. |
| URL upload receives data but viewers are slow | Separate server ingress from outbound/viewer traffic. |
| Disk free space falls | Upload staging, cache, logs and platform images; raising cache worsens it. |

**Overview** shows network history, live HTTP file activity and persistent lifetime
totals by traffic source. **Resources** owns CPU/RAM history and current storage
budgets. Their Time range is shared. **Accounts** shows live Telegram capacity,
queues and a recent five-minute activity window. Current values and lifetime totals
stay independent of historical chart selection. Monitoring data begins when this
version starts collecting; gaps and earlier history are not invented.

See [performance diagnostics](performance.md) for network scope, protocol behavior,
cache measurement and reproducible throughput comparisons.

## Timezone and deployment-only settings

Timezone defaults to Asia/Kolkata (IST). It affects displayed dates and share-expiry
times, not stored UTC timestamps or transfer speed. Use an IANA name or UTC.

Start with [Setup](setup.md) for required credentials and persistent storage.
The [configuration reference](reference.md#configuration) lists all environment
variables and bounds, including environment fallbacks for upload storage and session limits.
Dashboard upload storage changes apply to new admissions without redeploying;
other deployment-only changes require a restart/redeploy. Do not shrink staging beneath
an active workload or increase file-size limits beyond available temporary space.
No new mandatory environment variable or extra service is needed for dashboard
default restoration, history, the shared component library or appearance preferences.
