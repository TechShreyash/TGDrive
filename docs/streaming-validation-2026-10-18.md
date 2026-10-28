# Streaming validation — 18 October 2026

[Docs home](README.md) · [Current performance guide](performance.md)

Historical evidence for a 17 MB playback failure: delivery protocol and transient
buffer contention both contributed. Results and deployment status describe
**18 October 2026**; use the [performance guide](performance.md) for current guidance.
Live browser and synthetic server checks are identified separately. Neither
establishes performance on every network or device.

**Contents**

- [Findings](#findings)
- [Fresh-browser controls](#fresh-browser-controls)
- [Server-side integrity and backpressure](#server-side-integrity-and-backpressure)
- [Player behavior and coverage](#player-behavior-and-coverage)
- [Deployment and limits](#deployment-and-limits)

## Findings

Two independent problems were reproduced:

1. **Delivery protocol.** The same 17,839,845-byte MP4 downloaded in **93.668 seconds over HTTP/3** and
   **1.028 seconds over HTTP/2**. A direct IPv4 control reproduced the difference:
   a 4 MiB range took 20.487 seconds over HTTP/3 and 0.436 seconds over HTTP/2.
   All checksums matched. This isolates a QUIC-path bottleneck on the tested
   connection, without identifying the responsible network component or claiming
   that HTTP/3 is slow everywhere.
2. **Transient buffer contention.** A suspended response could retain the entire global transient buffer. New
   cached seeks and metadata reads then waited for that older client. The new
   regression fails against the old reader and passes with reclamation of
   speculative frames. Awaited/yielded frames and shared fills remain protected.

Increasing retained RAM cannot fix either condition: retained cache and transient
stream-delivery memory serve different purposes.

## Fresh-browser controls

<details>
<summary>Cache isolation and test media</summary>

Each browser run used a fresh Chromium context, disabled browser network caching
through CDP and bypassed service workers. Recorded browser cache hits were zero.
Server-cache state was recorded separately. Targeted eviction for cold runs
removed only this disposable cache entry, preserving the original Telegram file,
catalog and shares.

The MP4 is 30.526667 seconds of 1920×1080 H.264 High/yuv420p with stereo AAC.
Its SHA-256 is
`d6617a009c0c6c9aebf7398d43cad6d1985ddc1b9ab0479e2ea977362b8af5b0`.

</details>

| Test | Ready after | First complete playback, wall time | Mid-play buffering | Frames |
| --- | ---: | ---: | ---: | ---: |
| Existing deployment, server warm, default HTTP/3 | 17.808 s | 63.295 s | 8 events | Frame freezes observed |
| Existing deployment, Telegram cold, controlled HTTP/2 | 1.978 s | 30.626 s | 0 | 901 |
| Corrected local app, disposable cache cold | 0.798 s | 30.571 s | 0 | 901 |
| Existing deployment after proxy mitigation, server warm, default browser | 1.719 s | 30.597 s | 0 | 901 |
| Existing deployment after mitigation, Telegram cold, default browser | 2.070 s | 30.623 s | 0 | 900/901 presented |

The local case uses a synthetic Telegram transport over the exact actual bytes.
The public Telegram-cold case uses the real deployed Telegram transport. Each
transferred all 17,839,845 bytes across the browser network. Replay and seeks to
14/28 seconds also worked in the smooth cases.

A reversible domain-scoped proxy mitigation returned `Alt-Svc: clear`. A fresh
default browser, with no transport flags, selected HTTP/2: the full GET took
**0.835 seconds** and a 4 MiB range took **0.448 seconds**. TLS, original upstream
services and existing gzip rules were preserved. No app/proxy restart was needed.
The application default and both included Compose files carry this policy for
ordinary deployments. A separate isolated Traefik probe caught duplicated
advertisements with backend headers alone, so the Compose middleware applies
the policy at the edge too. Coolify's installed route generator discovers it
automatically; users do not need to supply router names or manual labels.

## Server-side integrity and backpressure

Bulk work ran entirely on the server in an isolated, loopback-only candidate
limited to two CPUs and 4 GiB RAM. Source bytes came from a checksum-verified copy
of a fully cached production MKV. The real HTTP/cache/preload/player paths used a
read-only synthetic Telegram transport. These are server-local HTTP measurements,
not public viewer bandwidth or Telegram-cold throughput.

The actual file contains **1,644,186,037 bytes**. A second logical file exposes
those same bytes through four parts of at most 512 MiB.

| Check | Result |
| --- | --- |
| Full single-document download | 3.738 s; 419.498 MiB/s |
| Full four-part download | 4.083 s; 384.059 MiB/s |
| Whole-file integrity | Both match SHA-256 `7292f1239f3d3c223f41e9090ce455e125036820814eddf188a704431c5bfddc` |
| All three part boundaries | Correct 206, lengths, ranges and matching 2 MiB checksums |
| Five cached seeks with an older unread response still active | 37–48 ms; zero extra source reads |
| Disk-only cached metadata seek | 7.96 ms; zero extra source reads |
| Resume with strong If-Range validator | Correct 206, 8 MiB and checksum |
| Unsatisfiable range | Correct 416 and total |
| Full-file HEAD | Correct size/range headers; zero body |
| Scratch memory | Peak 32 MiB; final bytes/streams/waiters all zero |

The older unread response stayed active while new requests completed. Automated
regressions also verify that the original reader resumes in byte order.

Fresh-browser playback of both candidate MKVs verified actual 1080p H.264 frames,
audible Japanese/English AAC tracks, English SRT/ASS cues and seeks to 120 seconds,
the middle, near the end and back to ten seconds. These used bounded windows
(approximately 327 MB/318 MB), rather than copying either entire MKV to the PC.
Browser cache hits were zero and scratch memory returned to zero afterward.
Initial readiness took 11.52 seconds for the single-document case and 7.79 seconds
for the split case over the private SSH/browser test path. These checks establish
working playback and seeking, not instant startup or public-route startup latency
for large MKVs.
The separate ASS track labelled Forced had no packets in the sampled dialogue
windows; the full English ASS track rendered actual dialogue correctly.

## Player behavior and coverage

The custom player stays visible and usable while opening/seeking. The separate
Start Playback gate and native Compatibility Playback fallback are removed.
Normal play controls respect browser gesture requirements. Unsupported codecs
produce an explicit in-player error; downloading the original remains available.

Loading feedback shows received bytes/rate and estimated buffered coverage/ahead
time. The timeline paints the engine's public buffered ranges, including while
paused. These estimates describe media time, separately from server cache byte
coverage; variable bitrate and decoder state can affect immediate playability.

Browser fixtures now use production-shaped Telegram document identities and
initialize the shared cache. Tests cover RAM/disk cache, actual Uvicorn/TCP
backpressure, disconnect/resume, concurrent readers, multipart seams, conditional
responses and resource cleanup.

**Automated results recorded for this validation:**

| Automated check | Result |
| --- | --- |
| Integrated Python suite, including isolated MongoDB and Node bridge | 947 passed; 15 subtests passed |
| Final browser workflows with pooled-cache fixtures | 62 passed |
| Frontend behavior | 180 Vitest tests passed |
| TypeScript and production frontend build | Passed |
| Packaging/setup/configuration audit | 71 passed |
| Both supported Compose configurations | Validated with Docker Compose |
| Public release packaging after adding this report | 3 passed |
| Changed Python lint/format and whitespace checks | Passed |

<details>
<summary>Cancellation regression synchronization</summary>

A cancellation regression originally waited for 100 zero-delay event-loop turns,
which could finish before a filesystem worker ran. It now awaits an explicit
origin-entry event before canceling; cancellation, shared-fill drainage, memory
and lease assertions are unchanged.

</details>

## Deployment and limits

For current installation and updates, follow [Setup](setup.md) and
[Operations](operations.md#upgrade-and-rollback). At this snapshot, the source
included the player/delivery/header fixes without requiring cache migration or
extra proxy labels. External proxies replacing application headers still needed
their own policy checks.

For diagnostics, distinguish browser-cold/server-warm from Telegram-cold, record
the browser protocol/cache hits and verify actual transferred bytes. Compare
HEAD/full GET, beginning/tail ranges, resume and a seek while an older response
is unread. A fast Python HTTP/1.1 download alone does not prove browser delivery.

These checks do not establish arbitrary concurrency, every ISP, Safari/Firefox
or physical Android/iOS behavior. The live app kept its existing image; only the
temporary domain proxy correction was applied. Source/player/Compose fixes were
local `dev` commits, unpublished and undeployed at that date. Private test
resources were removed and production health remained successful.
