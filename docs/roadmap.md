# Status and roadmap

[Docs home](README.md) · [What has been tested](development.md#ci-and-release-evidence)

**Implemented** means the feature exists. It does not mean every device, file
or failure scenario has been tested.

## What works now

| Feature | Current behavior |
| --- | --- |
| Web file manager | Shared dark component theme, folders, selection, move, rename and scoped search |
| File and folder uploads | Preserved hierarchy, combined batches, retry/resume and bounded concurrency |
| Transfer history | Running/Queued/Finished sections, result filters, scoped name search and cursor-paged persistent history |
| Files above 2 GB | Automatic ordered parts; one item for download, preview and seeking |
| Previews | Full-window images/text/video; PDF ranges up to 256 MiB with a 64 MiB reading budget; original download for unsupported books/comics |
| Owner monitoring | Current container CPU/RAM, network direction, 31-day measured history and lifetime traffic totals; compact positional cache coverage |
| Multiple bots | Balanced work, account limits and persisted Telegram waits |
| Connections page | Add/manage bots, Premium login, refresh names and verify credentials/channel access |
| Sharing | Scoped links, expiry, rotation and revocation |
| Trash and deletion | Reversible Trash; permanent database removal with retry, retaining Telegram originals |
| MongoDB persistence | Reopen the same drive after reinstalling the app |
| Telegram database backups | Complete encrypted copies, separate recovery key and empty-database restore |
| Guided installation | Docker setup wizard; source installation is also supported |

The default logical-file limit is 16 GiB, subject to configuration and disk.
Large-file support does not require Premium.

## Before public release

The MongoDB version is available for **public beta testing on `dev`**. The
original automated baseline passed on 2026-10-18. Later streaming, cache,
transfer and interface checks are recorded in the development guide; live/platform
limits remain below.

| Remaining check | Status |
| --- | --- |
| Latest local regression on 2026-10-20 | Passed: 1,018 Python, 213 frontend and 100 Chromium tests; 84.07% Python coverage |
| Live above-2-GB integrity on MongoDB | Verified: 4,562,805,302 bytes across three Telegram documents, full SHA-256 and boundary ranges. Broad physical-device playback remains unverified. |
| Current deployment artifacts | Passed: AMD64 container build/runtime smoke; source ZIP integrity/manifest/private-state exclusion checks. Earlier clean dependency/install tests are recorded in the development guide. |
| Near-cap Premium uploads and actual Premium acceleration | Unverified; no Premium test account |
| Deletion of a disposable message older than 48 hours | Unverified; no suitable message |
| Prolonged multi-bot outages and crashes during a real Telegram send | Further live coverage needed |
| Full deployment move, physical ARM and Render hosting | Not certified |

Earlier 2.16 GB upload/download/playback evidence is useful but predates MongoDB.
The [validation record](development.md#ci-and-release-evidence) separates those
results from current tests. GitHub workflow results are tracked separately from
local validation. A public beta branch does not imply a hosted deployment.

## Known correctness and recovery gaps

- **Credential replacement:** revoked accounts are excluded while healthy accounts
  continue eligible work. Same-account token/session repair still needs a restart;
  verified channel-permission repair can re-enable a live client through Refresh.
- **Bot-mode imports:** do not use the web queue's full durable job model.
- **History cleanup:** operation/receipt history needs reviewed compaction that
  preserves unresolved effects and retry identities.
- **Large folders:** current listing slices an in-memory result; database cursor
  pagination is a follow-up.
- **Older APIs:** request-start mappings can be process-local and error formats vary.
- **Changing storage channels:** needs an explicit migration design.

For current recovery actions, use [Troubleshooting](operations.md#troubleshooting).

## Next improvements

| Area | Useful follow-up |
| --- | --- |
| Video playback | Broaden physical-device and codec coverage; measure first-frame delay and seek audio/video synchronization |
| Folder uploads | Investigate preserving empty directories the picker cannot see |
| Recovery | Make emergency restore easier through a guided interface |
| Book and manga readers | CBZ can read individual ZIP image entries; EPUB also needs isolated XHTML/CSS and spine/navigation support. CBR needs a reviewed RAR decoder, including solid-archive resource limits. These readers are not implemented. |
| Backup safety | Consider independent file export/redundancy and optional payload encryption |
| File details | Consider real modification timestamps |

Prioritize integrity, recovery, useful speed and understandable status.
A new feature needs clear limits and retry/cancel/restart behavior, as well as
appropriate tests and updated docs.

## Outside the current scope

Multi-user accounts, teams, tenant permissions and clustered workers.
The current product is a single-owner, self-hosted drive.
