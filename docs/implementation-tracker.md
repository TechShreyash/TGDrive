# Reliability and performance implementation tracker

This checklist maps the October 18, 2026 owner request to implementation and
validation. Work is committed only to `dev`; pushing and deploying are outside
this change. Telethon remains the Telegram client. Existing unrelated local
changes are preserved separately and restored after integration.

| Request | Acceptance criteria | Status |
| --- | --- | --- |
| Dependency currency | Verify supported stable Python, uv, FastAPI, Telethon and locked dependencies against primary registries; retain compatibility and reproducibility. | Implemented; validation below |
| 1, 2, 5, 6, 13 | Accurate playback/buffering state, bounded startup, usable chapters, multi-audio/subtitles, keyboard controls and mobile player. | Implemented; validation below |
| 3, 8 | Correct HEAD/GET/range size and filename; portable revocable manager links; enforce revocation on subsequent and active reads; explicit default IST. | Implemented; validation below |
| 4, 7, 14, 18 | Responsive task layouts, modal-level copy feedback, device-scoped floating transfer notifications, complete global history. | Implemented; validation below |
| 9–11, 15, 19, 20, 22–26 | Configurable resource-aware shared caches; demand-prioritized coalesced fills; scan-resistant retention; bounded adaptive preload; manual preparation; inventory with ready ranges and clear/cancel. | Implemented; validation below |
| 12, 27, 35, 38 | Flood-aware Telethon pipelining, individual part retries, shared budgets, live same-DC endpoints and measurable client load; no wzgram. | Implemented; validation below |
| 21 | Durable URL-source identity and recovery; refresh must not turn server URL jobs into local file receipts. | Implemented; validation below |
| 28 | Byte-perfect multipart ranges/concatenation above 2 GiB, browser/download-manager headers and bounded integrity validation. | Implemented; validation below |
| 29, 30 | Server-side transfer checks and device-to-server upload bottleneck investigation, with isolated test data. | Implemented; validation below |
| 31–34, 37 | Cached delivery independent of bot origin limits; bounded fair origin queues; demand priority, approximate position/ETA and useful load/traffic/capacity statistics. | Implemented; validation below |
| 16, 17, 36 | Explained settings, organized public usage/operations/reference docs, refreshed README and honest capability/limit statements. | Implemented; validation below |

## Validation approach

- Run behavior tests for cache budgets, eviction, coalescing, priorities,
  cancellation, revocation, pipelined retries, restart recovery and ranges.
- Run frontend unit/type/build checks and synthetic browser checks at desktop and
  mobile sizes; include playback state, keyboard, chapters and transfer recovery.
- Use the authorized server only for isolated bandwidth-heavy tests. Never alter
  or delete original drive files, deployment credentials or configuration.
- Inspect the existing 4.2 GB file read-only where accessible; report precisely
  which bytes/ranges were validated rather than claiming a whole-file hash from
  a sample.
- Record approximate queue estimates honestly; Telegram and network variability
  prevent exact guaranteed completion timestamps.
- Revoke access to new/resumed requests and stop future blocks on active public
  streams. Bytes already delivered to a device cannot be recalled.

## Commit record

Implementation commits and their validation results are recorded below as stages
complete. The final handoff lists every commit and any remaining validation
limits. No request is considered complete solely because a UI control exists.

- Transport stage: bounded Telethon part pipelining, long flood-wait retries,
  shared per-account budgets, priority origin leases, request queue estimates and
  live same-DC endpoint refresh. Root verification: 52 targeted tests passed and
  Ruff passed; broader transport verification by subagent: 139 passed.

## Completed implementation stages

- Player and receipts: advancing-playhead buffering reconciliation, immediate
  chapter labels, keyboard controls, mobile layout, modal-scoped feedback and
  device-only floating receipts. URL jobs retain their remote source through
  local staging and restart recovery.
- Media path: verified shared RAM/disk blocks, frequency-aware eviction,
  operator budgets and reserves, adaptive demand-first warming, manual complete
  preparation, actual ready ranges, clearing/cancellation and aggregate stream
  payload backpressure. Metadata validation coalesces concurrently without a
  stale authorization/metadata TTL.
- HTTP/security: accurate catalog HEAD, range/conditional responses and UTF-8
  filenames; explicit portable capability links; provided revoked links fail
  even for an owner and active reads recheck access. File payloads retain identity
  encoding so proxy compression cannot strip byte-length/range metadata.
- Capacity: configured combined origin limits replace the legacy two-read lane
  and four-viewer gate. Upload/request budgets, flood feedback, queue position
  and estimated waits, account/server/cache/HTTP counters and bounded history
  are visible to the owner. Managed client configuration permits up to 50.
- Operator UI: Preloads and Performance panels, unit-labelled settings,
  revision-aware saves, default IST and configurable IANA timezone, smaller future
  upload splits, and professional usage/operations/reference documentation.
- Stack: Python 3.14.8 retains cryptg wheels; Telethon remains 1.45.0 and uv stays
  0.13.1. FastAPI is patched to 0.143.5 with an updated reproducible lock.

## Verification and practical limits

See [server measurements and validation](validation-2026-10-18.md) for actual
network/Telegram results and full 4.2 GB multipart SHA-256 verification.
Behavior tests cover cancellation, resource pressure, origin fairness, stale
metadata races, individual upload-part retries, range boundaries, share revocation,
source recovery and UI regressions. Live MongoDB checks use disposable uniquely
named test databases; the deployed catalog and original files are unchanged.

Measured queue times are estimates, payload budgets do not cover every operating
system/TLS/decoder allocation, and hardware/network/Telegram capacity remains
finite. Browser behavior is tested in Chromium, not the native FDM application;
FDM compatibility is validated through portable URLs, auth-free client requests,
HEAD/range/filename contracts and complete byte-integrity checks. Real viewing
on the owner's device and sustained arbitrary public load cannot be guaranteed
from server-side tests. No change is pushed or deployed by this work.


Final integration verification: 921 Python/MongoDB checks and 15 subtests passed,
176 frontend tests passed, and 60 distinct Chromium workflows passed across the
broad run and focused corrections. The restored working tree passed a final
13-case browser selection. Final preview/cache and revocation selections passed
93 and two tests respectively. Ruff, Bandit, dependency audits, the frontend
production build and three release-package checks passed. The source-package
builder includes this release's public performance and validation documentation.

Implementation stages are committed separately on `dev`; see the local Git log
for exact hashes. Pre-existing listing-refresh changes were restored after integration and committed
separately following the owner’s subsequent request. Their original safety stash
remains available. No push or deployment occurred.
