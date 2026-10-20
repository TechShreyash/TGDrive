# Configuration and safe tuning

[Docs home](README.md) · [Setup](setup.md) · [Performance diagnostics](performance.md)

Start with the built-in defaults. They are conservative budgets, not allocations:
TGDrive reduces retained RAM when host/container headroom is low, and stops disk
cache writes before the free-space floor is crossed. More CPU or RAM alone does
not remove Telegram pacing, network-route or client-device limits.

## Change, discard or restore

The sidebar separates **Monitor** (Performance, Preloads), **Configuration**
(Cache settings, Transfer limits, Timezone) and **Management** (Connections,
Storage & Backups). TGDrive uses one consistent dark appearance.

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
| Preload during playback/downloads | On | Prepare useful upcoming bytes after sequential delivery | Turn off to minimize speculative traffic. Manual Prepare remains available. Active demand has priority. |

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
| File split size | 2000 MiB | Document size for newly planned large uploads | Keep the default for ordinary bot uploads. 0 selects the safe eligible-account maximum. Smaller parts increase document and request overhead. |
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
variables and bounds, including upload staging, file-size and session limits.
Deployment-only changes require a restart/redeploy. Do not shrink staging beneath
an active workload or increase file-size limits beyond available temporary space.
No new mandatory environment variable or extra service is needed for dashboard
default restoration, history, the shared component library or the single theme.
