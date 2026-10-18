# Reliability and performance implementation tracker

This checklist maps the October 18, 2026 owner request to implementation and
validation. Work is committed only to `dev`; pushing and deploying are outside
this change. Telethon remains the Telegram client. Existing unrelated local
changes are preserved separately and restored after integration.

| Request | Acceptance criteria | Status |
| --- | --- | --- |
| Dependency currency | Verify supported stable Python, uv, FastAPI, Telethon and locked dependencies against primary registries; retain compatibility and reproducibility. | In progress |
| 1, 2, 5, 6, 13 | Accurate playback/buffering state, bounded startup, usable chapters, multi-audio/subtitles, keyboard controls and mobile player. | In progress |
| 3, 8 | Correct HEAD/GET/range size and filename; portable revocable manager links; enforce revocation on subsequent and active reads; explicit default IST. | In progress |
| 4, 7, 14, 18 | Responsive task layouts, modal-level copy feedback, device-scoped floating transfer notifications, complete global history. | In progress |
| 9–11, 15, 19, 20, 22–26 | Configurable resource-aware shared caches; demand-prioritized coalesced fills; scan-resistant retention; bounded adaptive preload; manual preparation; inventory with ready ranges and clear/cancel. | In progress |
| 12, 27, 35, 38 | Flood-aware Telethon pipelining, individual part retries, shared budgets, live same-DC endpoints and measurable client load; no wzgram. | In progress |
| 21 | Durable URL-source identity and recovery; refresh must not turn server URL jobs into local file receipts. | In progress |
| 28 | Byte-perfect multipart ranges/concatenation above 2 GiB, browser/download-manager headers and bounded integrity validation. | In progress |
| 29, 30 | Server-side transfer checks and device-to-server upload bottleneck investigation, with isolated test data. | In progress |
| 31–34, 37 | Cached delivery independent of bot origin limits; bounded fair origin queues; demand priority, approximate position/ETA and useful load/traffic/capacity statistics. | In progress |
| 16, 17, 36 | Explained settings, organized public usage/operations/reference docs, refreshed README and honest capability/limit statements. | In progress |

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
