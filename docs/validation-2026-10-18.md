# Server transfer validation — 18 October 2026

The measurements support keeping Telethon and improving its transfer scheduling.
Bounded part pipelining materially improved uploads, a higher shared request
ceiling improved cold downloads, and cached reads avoided Telegram payload work.
Telegram still imposed flood waits during some repeated-download experiments.
These results do not establish a universal Telegram speed limit.

This report contains no credentials, share capabilities, installation addresses,
or private file titles. Throughput uses binary MiB/s unless stated otherwise.

## Scope and isolation

Tests ran over SSH on the requested server. The new implementation ran from a
private source copy and benchmark environment, without deploying it or restarting
the existing application. Existing MongoDB catalog, settings and share records
were read without importing application ownership/startup code. The existing
video and its shares were never modified or deleted.

The private benchmark used Python 3.12.3, Telethon 1.45.0, cryptg 0.6.0 and uvloop.
FastAPI 0.143.5 was installed only in that benchmark environment because the
logical reader imports the HTTP streaming module. The independently running
deployed application reported Python 3.14.8. This distinction matters when
comparing runtime overhead; the tests were not a deployment of the new runtime.

The bot's home DC and the newly uploaded files' DC were both **DC 5**. Source
copies refreshed the main endpoint from Telegram's live configuration while
preserving the account DC and authorization. Bulk experiments ran sequentially;
ordinary traffic to the existing installation was not disabled.

Each random-payload round used fresh, incompressible 128 MiB data. Comparisons
within a round reused the same uploaded document. Every completed random-data
download was checked against its original SHA-256. The new-implementation rounds
created and deleted four temporary messages; existing messages were read only.
Cleanup verified that both private credential JSON copies, all five private
working directories, bot session files, payloads and disposable test caches were
removed. Sanitized results and source copies remained for examination.

## Earlier network controls

These are earlier measurements from the same examination, **not fresh reruns**
alongside the implementation tests:

| Control | Result |
| --- | --- |
| Nearby Ookla multi-connection download | 2.104 Gbit/s |
| Nearby Ookla multi-connection upload | 6.119 Gbit/s |
| Ookla latency / packet loss | 0.661 ms / 0% |
| Distant European HTTP source, 1 GiB | 27.166 s; 37.695 MiB/s |
| Singapore HTTP source, 1 GiB | 43.740 s; 23.411 MiB/s |

The requested test-file URL returned HTTP 403 with a Cloudflare challenge from
the server. A public OVH test file supplied the earlier 1 GiB control instead.
That payload was highly compressible, so subsequent random-payload measurements
were recorded separately.

The host had substantial network headroom relative to the observed single-bot
Telegram transfers. A nearby multi-connection speed test does not predict
Telegram peering, a remote viewer's connection, or a device's upload speed.

## Earlier client comparison

The earlier **random 128 MiB** matrix measured:

| Implementation | Upload | Download |
| --- | ---: | ---: |
| Original sequential Telethon transport | 3.460 MiB/s; 36.994 s | Not completed in this matrix |
| WZGram 3.1.5 | 15.534 MiB/s; 8.240 s | 17.098 MiB/s; 7.486 s |
| Experimental Telethon, four concurrent part requests | 13.906 MiB/s; 9.205 s | Not measured in this row |

The earlier compressible-file matrix was different: original Telethon uploaded
128 MiB at 5.638 MiB/s and downloaded at 18.576 MiB/s. WZGram's 1 GiB upload
reached 17.790 MiB/s while logging 71 retry warnings. Those rows should not be
mixed with the random-data matrix as if their workloads were identical.

WZGram's published uploader uses concurrent media workers, while the original
Telethon upload helper awaited parts sequentially. Its Pyrogram compatibility
claim does not make it a drop-in replacement for TGDrive's Telethon sessions,
types and retry boundaries. The improvement could therefore be pursued within
TGDrive's existing transport. See the [WZGram uploader](https://github.com/rjriajul/wzgram/blob/v3.1.5/pyrogram/methods/advanced/save_file.py)
and [Telethon upload implementation](https://github.com/LonamiWebs/Telethon/blob/v1/telethon/client/uploads.py).

## New transport measurements

The upload pipeline was four 512 KiB protocol parts; the shared request limit
was eight. The initial profile used 24 scheduled requests/second and the older
two-read admission ceiling:

| Random 128 MiB operation | Time | Throughput |
| --- | ---: | ---: |
| Upload | 11.837 s | 10.813 MiB/s |
| Direct download, 24 requests/second | 11.235 s | 11.393 MiB/s |
| Direct download, 48 requests/second | 5.761 s | 22.219 MiB/s |
| Pooled cold download, 24 requests/second | 11.486 s | 11.144 MiB/s |
| Pooled warm read | 0.237 s | 540.690 MiB/s |

With 512 KiB data requests, a 24-request/second ceiling is approximately
12 MiB/s before overhead. The direct-download comparison demonstrated that this
application ceiling was limiting the connection.

After removing the independent two-read ceiling, a 48-request/second profile
with a combined account limit of three produced:

| Random 128 MiB operation | First round | Focused confirmation |
| --- | ---: | ---: |
| Upload | 9.559 s; 13.390 MiB/s | 9.568 s; 13.378 MiB/s |
| Pooled cold download | 7.766 s; 16.483 MiB/s | 8.894 s; 14.391 MiB/s |
| Pooled warm read | 0.239 s; 536.196 MiB/s | 0.348 s; 368.093 MiB/s |

The focused confirmation completed 513 scheduled requests with **zero failed
attempts and zero flood waits**. Cold first-byte time inside the prepared
transport was 0.6568 s; warm first-byte time was 0.0074 s. These exclude browser
startup, public TLS and initial HTTP authorization/metadata work.

Warm figures measure local shared-cache consumption, including local disk/RAM
effects. They are **not HTTP delivery rates to a viewer**. The standalone cache
used its 64 MiB RAM / 512 MiB disk defaults; application-configured budgets can
differ.

### Pressure and failed diagnostic probes

An experimental combined limit of four completed a cold read at only
7.203 MiB/s after a round containing four flood waits. Adaptive feedback reduced
its request budget. A later raw direct-download diagnostic also raised a
`FloodWaitError`; its temporary message was still deleted. The production pooled
reader was then tested separately and completed the confirmation above.

The experiments were ordered and shared a real Telegram account. Cumulative
traffic and regular installation traffic can affect provider feedback, so the
four-operation ceiling cannot be identified as the sole cause of those waits.
The selected defaults are **48 scheduled requests/second and three combined
media operations**, rather than four. This is a measured starting point with
adaptive backoff, not a guaranteed provider allowance or a promise of 20 MiB/s.
Across rounds, endpoint selection, admission and pacing also changed; these are
not isolated single-variable comparisons. The within-round direct 24/48 rate
comparison reused one file and connection.

## Existing 4.2 GiB multipart video

The deployed catalog described **4,562,805,302 bytes**, approximately
**4.249 GiB**, stored in three Telegram documents:

| Part | Bytes | SHA-256 compared with catalog |
| --- | ---: | --- |
| 0 | 2,097,152,000 | Match |
| 1 | 2,097,152,000 | Match |
| 2 | 368,501,302 | Match |

The complete logical stream was read in order in **490.880 seconds**
(8 minutes 10.880 seconds; 8.865 MiB/s). Its concatenated SHA-256 matched the
catalog's whole-file SHA-256. This was a full read, not a sample. The run used
the initial 24-request/second profile and older two-read ceiling, before the
later admission adjustment. It finished below the 600-second verification limit.

Two additional 2 MiB logical reads straddled the stored-document boundaries.
The existing public share then served those exact byte intervals through the
deployed HTTP endpoint. All returned **206**, the exact `Content-Range`, exactly
2,097,152 bytes and matching SHA-256 values. One interval was repeated with
`Accept-Encoding: gzip`; it also matched and remained uncompressed. None of
these file responses contained an HTML body.

The full verification proves agreement with the recorded per-part and logical
hashes. The original device's source file was not available for a new independent
source-side hash calculation. It also does not prove compatibility with every
codec, media container, browser or device.

### Final memory-backpressure sanity check

After the global stream-scratch guard was finalized, the latest private source
copy read one **64 MiB** prefix of the existing video at 48 requests/second and
three combined operations. It used application-like **256 MiB RAM / 10 GiB disk**
cache budgets and a **32 MiB global scratch ceiling**:

- Cold logical read: **4.931 s; 12.980 MiB/s**, first byte **0.5258 s**.
- The observed scratch peak was **12 MiB**, within the configured 32 MiB ceiling.
- After completion: scratch bytes, waiting heads, active streams and active
  account leases were all **zero**. Retained payload RAM was **64 MiB**.
- All **129 scheduled requests** completed with zero failed attempts or floods.
- An independent public **206 Range** read of the same 64 MiB matched its SHA-256.
  That reference took 2.081 s through the same server's public route; it was not
  a remote viewer throughput measurement.

No new upload or Telegram message was needed. This confirms the final guarded
path's byte integrity and resource release on that real input. Its different
payload, length and cache policy prevent treating it as a controlled benchmark
of the scratch guard's exact performance overhead.

## Public download total and proxy compression

A bounded full-file request reproduced the missing-total issue without reading
the entire public HTTP response. Each GET sampled at most 64 KiB and was closed:

| Deployed request | Status | Content-Length | Accept-Ranges | Encoding / framing |
| --- | ---: | --- | --- | --- |
| HEAD, identity or gzip accepted | 200 | 4,562,805,302 | bytes | No compression |
| Full GET, identity accepted | 200 | 4,562,805,302 | bytes | No compression |
| Full GET, gzip accepted | 200 | Missing | Missing | gzip; chunked |
| Cross-part Range GET, identity or gzip accepted | 206 | 2,097,152 | bytes | No compression |

The deployed Traefik compression middleware transformed a full 200 response and
removed its length/range headers. A correct HEAD response therefore did not
guarantee that a browser's full GET retained its total. The source fix explicitly
marks byte-addressed file responses with `Content-Encoding: identity`.
Traefik documents an existing `Content-Encoding` header as a compression
exemption. The implementation change was not deployed during these tests, so
post-deployment header verification remains a separate operational check.
See [Traefik compression activation](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/compress/#compression-activation).

The ordinary application page correctly returned HTML; it was not confused with
a file response. The small shared-file capability sought for an alternative
compression probe was unavailable, and no share was created or revoked.

## Datacenters and parallelism

Telegram documents carry a `dc_id`; clients must access the appropriate file DC
and handle migration responses and authorization transfer. Account routing and
file routing can differ. Changing a local setting does not relocate existing
Telegram content. These are protocol facts in [Telegram's DC documentation](https://core.telegram.org/api/datacenter).

Telegram recommends parallel part queues and separate media connections for
efficient transfers. That supports investigating transport scheduling; it does
not establish a fixed, universal per-file MiB/s cap. Smaller TGDrive documents
can expose additional account slots but also add publication and boundary work.
They do not remove account/channel waits. See [Telegram file transfers](https://core.telegram.org/api/files).

Earlier TCP-connect probes found substantially lower latency to DC 5 than to
the other tested DC endpoints from this server. TCP connection latency is not
a throughput comparison: no same-content, authorized cross-DC throughput matrix
was performed. No forced DC migration or custom media-session pool was deployed.

## Device-to-server upload scope

Read-only deployment inspection found HTTPS redirection and compression
middleware, with no application-specific buffering/body-size/rate middleware.
Browser receipt chunks are **8 MiB** and the application rejects larger chunks.
The proxy's two-second configuration value was a **health-check timeout**, not
an upload timeout.

No explicit request-read-timeout override was found. Traefik documents a
60-second default for reading an incoming request, including its body. As a
diagnostic inference, an 8 MiB chunk on an uplink near or below 1.1 Mbit/s could
approach that duration before overhead. This was not reproduced as an observed
device upload failure. See [Traefik entrypoint timeouts](https://doc.traefik.io/traefik/reference/install-configuration/entrypoints/).

A useful follow-up device test separates browser hashing, receipt transmission,
staging writes and Telegram publication timings. Throttled 8 MiB receipt tests
should use disposable uploads and verify retry/receipt reuse near proxy timeout
boundaries. A server download speed test cannot measure the user's device
uplink. No device uplink or many-viewer public load test is claimed here.

## Dependency and runtime audit

The audit checked the explicit Python and frontend pins against their primary
package registries:

| Item | Finding / selected policy |
| --- | --- |
| Python | 3.15.0 exists; retain tested 3.14.8, the latest 3.14 bugfix release |
| uv | 0.13.1 was already current |
| Telethon | 1.45.0 was already current |
| cryptg | 0.6.0 was already current |
| Uvicorn | 0.54.0 was already current |
| FastAPI | Update 0.143.2 → 0.143.5, the available patch release |
| Other explicit Python production/dev/e2e pins | Current stable versions at audit time |
| Frontend | Existing pins current except a newer Lucide minor and Node 26 type definitions |

Lucide 1.58.0 was retained rather than making an unrelated 1.59.0 minor update.
Node 24 type definitions remain appropriate to the configured Node 24 runtime;
the existence of Node 26 type packages is not a reason to mismatch them.

Python 3.15 was actually installed in an isolated test environment. Production
dependencies resolved without cryptg, and 43 focused tests passed in that
limited environment. cryptg 0.6.0 publishes no CPython 3.15 wheel; its source
build required a Rust toolchain absent from the existing build setup. Full native
3.15 compatibility was not established. Docker image availability was not the
blocker. The deployed 3.14.8 image also successfully resolved
`ZoneInfo("Asia/Kolkata")` using its available timezone data.

Primary references: [Python releases](https://www.python.org/downloads/),
[Telethon releases](https://pypi.org/project/Telethon/),
[cryptg release files](https://pypi.org/project/cryptg/0.6.0/#files),
[uv releases](https://pypi.org/project/uv/),
[FastAPI releases](https://pypi.org/project/fastapi/),
[Uvicorn releases](https://pypi.org/project/uvicorn/), and the
[npm registry](https://registry.npmjs.org/).

## Reproduction and coverage limits

[`scripts/benchmark_transports.py`](../scripts/benchmark_transports.py) accepts a
private JSON configuration containing `api_id`, `api_hash`, `bot_token`,
`channel` and that bot's `channel_hash`. Optional `manifest` records enable the
existing-file hash/boundary checks. An optional `http` object supplies an existing
share's origin/path/capability for `--check-http`; it is never logged.

Run in an isolated source copy with the dependencies installed, for example:

```sh
PYTHONPATH=. python scripts/benchmark_transports.py \
  --config /private/credentials.json --work-dir /private/fresh-run \
  --part-rate 48 --media-limit 3 --manifest-timeout 600 --check-http
```

Use a private fresh working directory. The script deletes its own uploaded
message and random payload; its session state and disposable caches also need
private cleanup after the test. Existing manifest messages are never deleted.
A timeout reports partial byte/part coverage and must not be described as a
whole-file checksum pass. Require `manifest_complete.whole_sha256_match=true`
for that claim.

Offline transport checks covered bounded concurrency, individual-part retry,
repeated long flood waits, cancellation, checksums before publication, stable
publication IDs, endpoint rollback, priority, queue overload and lease cleanup.
The earlier transport-focused handoff ran 139 relevant tests successfully;
the final targeted transport/cache/stream-delivery selection ran **74 tests**
successfully after the memory guard landed. Ruff check and formatting passed for
the benchmark helper. Complete application/UI checks are reported separately
with the implementation.

These measurements cover one server and one bot. They do not establish
100/1,000-viewer capacity, independent-file scaling across many bots, universal
DC performance, real-device video playback, or the behavior of an implementation
that has not yet been deployed. HTTP reads can naturally warm the deployed
disposable media cache; no catalog, share or deployment configuration changes
were made for validation.


## Implementation verification

The implementation remains local on `dev`. Nothing was pushed or deployed.
The pre-existing four-file working-tree change was restored after integration,
with its original 193 additions/four deletions kept uncommitted.

| Check | Result |
| --- | --- |
| Integrated Python run, including live isolated MongoDB and Node bridge | 921 passed; 15 subtests passed |
| Final shared-preview/cache regression selection after that run | 93 passed |
| Revocation during preview/metadata work | 2 passed |
| Frontend behavior | 176 Vitest tests passed |
| TypeScript and production frontend build | Passed |
| Chromium workflows | 60 distinct cases passed across the broad run and focused corrections; final merged-tree selection 13 passed |
| Python lint/format and Bandit | Passed |
| Python dependency audit / npm audit | No known vulnerabilities / zero reported vulnerabilities |
| Public source-package checks | 3 passed; source ZIP and SHA-256 generated |

Live MongoDB tests used disposable, uniquely named databases and removed them.
The browser set includes real media frames/audio/subtitle cues, keyboard behavior,
chapters, mobile layouts, device-origin receipts, URL recovery, preload controls,
portable signed-out downloads and timezone-formatted expiry. HTTP file tests
cover first-byte admission, queue overflow, lengths, ranges, conditional requests,
revocation before and during delivery, multipart seams and cancellation cleanup.

Resource tests include twenty slow readers of different files sharing a 4 MiB
transient budget with retained caching disabled; cancellation releases all frames
and account leases. A lone reader can use 32 concurrent origin fills when account
capacity permits. Simulated memory pressure shrinks retained RAM and transient
limits and allows recovery. Ordinary text preview bytes now use the shared cache
and inventory without qualifying as playback or starting automatic preloading.

The production frontend build still reports existing third-party Movi bundle-size
and CommonJS-in-ESM warnings. Real Chromium playback/codec checks passed; those
warnings are not claimed to be eliminated. Native FDM UI, every browser/device,
sustained arbitrary public load and the post-deployment proxy configuration remain
outside the validated scope. The portable URL and HTTP contracts were tested
without owner cookies, rather than making private authenticated URLs public.
