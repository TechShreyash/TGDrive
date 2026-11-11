# HTTP API reference

[Docs home](README.md) · [Configuration reference](reference.md) · [Architecture](architecture.md)

These endpoints serve TGDrive's own web app. Each section lists the routes, who
may call them and the errors worth handling. Behavior and recovery guarantees
are explained in [Architecture](architecture.md) and [Performance](performance.md);
this page links there rather than repeating them. There are no API keys,
per-person accounts or OpenAPI pages, and response fields may grow over time.

**Access** column: **Public** needs nothing, **Owner** needs the login cookie,
**Share** accepts a scoped share token in `auth`.

**Contents**

- [Conventions and authentication](#conventions-and-authentication)
- [Session, configuration, health, and assets](#session-configuration-health-and-assets)
- [Independent channel backup and recovery](#independent-channel-backup-and-recovery)
- [Owner connection management](#owner-connection-management)
- [Listing and metadata](#listing-and-metadata)
- [Atomic folder preparation](#atomic-folder-preparation)
- [Move and bulk actions](#move-and-bulk-actions)
- [Performance, cache inventory and preparation](#performance-cache-inventory-and-preparation)
- [Archive jobs](#archive-jobs)
- [Resumable browser receipts](#resumable-browser-receipts)
- [Previews and media](#previews-and-media)
- [Jobs](#jobs)
- [Transfer history](#transfer-history)
- [Recent files](#recent-files)
- [Starred items](#starred-items)
- [Shares, downloads, and ranges](#shares-downloads-and-ranges)
- [URL imports and transfer routes](#url-imports-and-transfer-routes)
- [Torrent imports](#torrent-imports)
- [Errors and retry discipline](#errors-and-retry-discipline)

## Conventions and authentication

- Routes are relative to your TGDrive address. Bodies are JSON unless a route
  says raw bytes or multipart. Both `/api/v1/...` and older `/api/...` routes are
  current.
- Paths contain **IDs**, not names: root is `/`, a nested item is
  `/<folder-id>/<item-id>`. A listed node's `path` is its **parent**; append `id`
  to address it. `parent_id` in listing and upload requests takes a full folder path.
- Owner routes need the `tgdrive_session` cookie (HttpOnly, SameSite=Strict).
  Mutations reject a foreign `Origin`, or `Sec-Fetch-Site: cross-site` without
  one. There is no cross-origin integration mode.
- Dates are ISO strings, except upload `expires_at`, job timestamps and saved-progress `updated_at`,
  which are Unix seconds.
- JSON control bodies are capped at **64 KiB**, including signed-in/demo
  requests. Folder-tree, bulk/move and ZIP-creation requests allow **4 MiB**;
  numbered transfer-history requests allow **256 KiB**. Oversize bodies return
  `413`, including streamed bodies without `Content-Length`. Multipart uploads,
  receipt chunks and binary recovery imports use their own admission paths.

## Session, configuration, health, and assets

| Method and route | Access | Purpose | Notes and errors |
| --- | --- | --- | --- |
| `GET /api/config` | Public | `status`, `max_file_size`, `timezone`, `recovery_only`, `demo_mode`, `demo_read_only`, `demo_admin` | `no-store` |
| `POST /api/checkPassword` | Public | `{"pass": "..."}` signs in and sets the cookie | `401` wrong password; `429` after five failures per IP in five minutes |
| `POST /api/logout` | Cookie | Revokes the session and clears the cookie | |
| `GET /api/v1/session` | Public | `{"authenticated": bool}` plus demo permission fields | `no-store` |
| `GET /api/v1/status` | Owner | Connections, capacity, catalog revision, latest backup, client health, `telegram_issues` | Does not query Telegram backup history |
| `POST /api/v1/backup` | Owner | Requests an immediate backup or retries pending publication | `409` during recovery or while independent protection is paused; `503` if it can't be confirmed |
| `GET /health/live` | Public | `200` when HTTP answers | |
| `GET /health/ready`, `GET /healthz` | Public | `200` when ready or in recovery mode | `503` otherwise |
| `GET /`, `GET /stream`, `GET /embed` | Public | App, standalone player and embed shells | Data requests still check access |
| `GET /assets/…`, `/static/…`, `/third-party/…` | Public | Frontend and player assets | |

**Demo mode.** Restricted visitors get `403 DEMO_MODE_READ_ONLY` for writes under
`/api/v1/settings`, `/api/v1/replica`, `POST /api/v1/backup` and
`GET /api/v1/replica/file`, and `403 DEMO_FILES_READ_ONLY` for changes that touch
Demo Files (ID `tgdrive-demo-files`). See [demo access boundaries](architecture.md#demo-access-boundaries).

## Independent channel backup and recovery

All routes are **Owner**. Credentials go in request bodies or headers, never URLs.
The workflow is in [Backup & recovery](backup-recovery.md); pacing and
verification are in [architecture](architecture.md#independent-backup-pacing-and-verification).

| Method and route | Purpose | Notes and errors |
| --- | --- | --- |
| `GET /api/v1/replica` | Configuration, phase, revisions, progress, retry guidance and any recovery candidate | Never returns tokens or the key |
| `POST /api/v1/replica/configure` | `channel_id`, `bot_token`, `independent_accounts: true`: set up a backup channel | Needs a separate private channel and bot |
| `POST /api/v1/replica/action` | `action`: `sync`, `pause`, `resume`, `retry` or `disable` | |
| `GET /api/v1/replica/file` | Download a locally retained confirmed or unverified `tgdrive-backup.tgdb` | `404` if no local archive is eligible or it disappears before opening; `409` for symlinks, unsafe permissions/file types, other unsafe opens or selected-file hash mismatch; denied in demo mode |
| `GET /api/v1/replica/report` | Secret-free status report | May still contain private names and IDs |
| `POST /api/v1/replica/import` | Raw `application/octet-stream` recovery bytes; optional `X-TGDrive-Source-Channel`, `X-TGDrive-Bot-Token` | Returns a candidate; does not activate. `413` above 2000 MiB encrypted; `415` wrong content type; `422 RECOVERY_FILE_INVALID` for malformed metadata; `507` HTTP staging failure. Resource, recognized legacy and authentication failures use structured `409` codes. Disabled in demo mode |
| `POST /api/v1/replica/restore` | Restore the backup found in the storage channel at startup | Restart when asked. Disabled in demo mode |
| `POST /api/v1/replica/recover` | Three steps: `source_channel` + `bot_token` to inspect; `target_channel` + `target_bot_token` + `confirm_channel` (optional `catalog_only`) to copy; `activate: true` + `confirm_channel` to switch | Wrong confirmation or an incomplete candidate can't activate |
| `POST /api/v1/start-new` | `confirm: "START NEW"` and `password`; in-place reset is unavailable | A valid request returns `409 START_NEW_UNAVAILABLE` without erasing the drive; `403` wrong password or demo mode; `422` invalid confirmation. [Fresh-drive procedure](backup-recovery.md#start-a-new-drive) |

The status endpoint's `backup` list is latest-first and may be empty.
`pin_verified_at` records a pin check at that time; `unverified` discovery does
not establish archive corruption. Local download availability is reported
separately by `backup_file_available` from the replica endpoint; do not infer it
from discovery status alone. Availability and download use the same selection:
a newer pending point or missing local file does not hide an older eligible
archive. The selected archive is hashed against its own stored digest before
delivery; it need not correspond to the first history row.

Saved activation/journal resource rejection uses `ACTIVATION_RESOURCE_LIMIT`.
The replica status keeps its normal snapshot with `phase: "error"`, an error
code/detail/action and preserved input; it does not initialize a fresh catalog
over that state. The effective legacy whole-value limit and separate recovery
prerequisites are in [recovery guidance](backup-recovery.md#restore-after-losing-mongodb).

`BACKUP_KEY_MISMATCH` means the supplied key could not authenticate the file;
altered ciphertext or an invalid authentication tag can also cause it.
`RECOVERY_FILE_INVALID` (`422`) covers malformed recovery data, including
structural truncation and authenticated invalid metadata. These categories do
not distinguish every possible damaged ciphertext from a wrong key.

Database outages report `REPLICA_MONGODB_UNAVAILABLE` with a `retry_at`;
configuration or ownership problems report `REPLICA_MONGODB_FAILED`. Neither
discards the pending backup.

## Owner connection management

All routes are **Owner**. Secrets are write-only body fields; these routes cannot
change the storage channel or the owner password. How saving applies live is
covered in [connection management](architecture.md#connection-management).

| Method and route | Purpose | Notes and errors |
| --- | --- | --- |
| `GET /api/v1/settings` | Redacted configuration, running clients, `stats` and recovery state | Never calls Telegram |
| `PATCH /api/v1/settings` | `expected_revision`, optional `api_id`, `api_hash`, `clients` (the full desired list) | `409 SETTINGS_CONFLICT`, `409 SETTINGS_BUSY`, `422 SETTINGS_INVALID`, `503 SETTINGS_PREFLIGHT_FAILED` |
| `POST /api/v1/settings/refresh` | Re-check saved accounts and channel access in the background | `202` |
| `GET`, `PATCH /api/v1/settings/bot-mode` | Bot upload folder, `site_url` and `encrypt` | `no-store` |
| `POST /api/v1/settings/premium-login/start` | `expected_revision`, `phone`, `label` | Returns `challenge_id`; FloodWait sets `Retry-After` |
| `POST /api/v1/settings/premium-login/verify` | `challenge_id` plus `code` or `password` | `410 TELEGRAM_LOGIN_EXPIRED` |
| `DELETE /api/v1/settings/premium-login/{challenge_id}` | Cancel an unfinished login | |

In `clients`, keep an existing row's `id` and omit `credential` to keep its secret;
omit a row to remove it. Limits: 50 rows, at least one enabled bot, labels 1–80
characters. Account errors include `TELEGRAM_CREDENTIALS_REVOKED`,
`TELEGRAM_CHANNEL_ACCESS` and `TELEGRAM_ACCOUNT_REMOVED`.

## Listing and metadata

| Method and route | Access | Purpose | Notes and errors |
| --- | --- | --- | --- |
| `POST /api/getDirectory` | Owner, Share | `{"path", "auth"}`: whole folder contents keyed by child ID | Older route; the website now uses the paged routes below. Special paths: `/trash`, `/search_<q>` (max 500 results), `/share_<folder-id>` |
| `GET /api/v1/nodes` | Owner | `parent_id` (default `/`), `limit` (1–500; default 100), `sort` (`name`, `size`, `type`, `date`; default `name`), `order` (`asc`, `desc`; default `asc`), optional `type` filter, then one of `cursor` (up to 16,384 characters), `offset` or `around` (an item ID, up to 64 characters) | `{items, total, offset, next_cursor}`, plus `folder` on pages that don't continue a cursor and `index` with `around`. `next_cursor` is `null` on the last page. `422` for an invalid cursor or mixed paging parameters; `404` if the `around` item isn't in the folder |
| `GET /api/v1/shared/nodes` | Share | `auth` (1–512 characters) plus the same parameters as `/api/v1/nodes` | `{items, total, offset, next_cursor, share_home}` plus `folder` and `index` as above, without `starred`; breadcrumbs start at the shared folder; `private, no-store`. `403` invalid share, folder outside it or `around` item not in the folder; `409` during recovery; `422` bad paging |
| `GET /api/v1/search` | Owner | `q` (1–255), `offset`, `limit` (`25`, `50` or `100`; default 50), `sort` (`name`, `date`, `size`), `order` | Word-start match, excludes Trash; ranks at most 1,000 candidates, not all global matches. `{items, total, capped, has_more, next_offset}`; `422` for other limits |
| `GET /api/v1/trash` | Owner | Top-level Trash items, optional `q` | Pending deletions are hidden |
| `POST /api/createNewFolder` | Owner | `{"path", "name"}` | |
| `POST /api/renameFileFolder` | Owner | `{"path", "name"}` | |
| `POST /api/trashFileFolder` | Owner | `{"path", "trash": bool}` | No exposure check; prefer the bulk route |
| `POST /api/deleteFileFolder` | Owner | `{"path"}` of a top-level Trash item | `202` with a deletion job ID |

`type` is `folder`, `media`, `docs`, `audio` or `other`, matching the My Files
filter chips. `folder` is the folder's own record with `breadcrumbs`, as from
`getDirectory`. `around` starts the page at that item, for links that open a file
in a large folder, and `index` is its position under the current sort and filter.
Pass `next_cursor` back as `cursor` to page large folders without a deep offset
scan; counts and other database work still apply. Folders sort first. File rows
include `part_count` but never message IDs or checksums.

Search selects matching candidates in stable item-ID order, excluding directly
trashed items before applying the cap. Descendants of trashed folders can still
consume that cap before ancestry filtering, leaving fewer visible results.

## Atomic folder preparation

`POST /api/v1/folders/tree` (**Owner**) creates a new upload root and all its
subfolders in one step, before any file is sent:

```json
{
  "destination": "/<destination-id>",
  "root_name": "Project",
  "folders": [["alpha"], ["beta"], ["beta", "deep"]],
  "client_request_id": "<16-128 safe characters>"
}
```

`201` returns the `root` item and a `folders` map from relative path to full ID
path. The root uses keep-both naming and never merges with an existing folder.
Repeating the same ID and payload returns the same folders, even after a
restart. A changed payload or a moved, renamed or trashed tree returns
`409 FOLDER_TREE_CONFLICT`. Persist the request ID before sending and reuse it
after an uncertain response.

## Move and bulk actions

| Method and route | Access | Body | Notes and errors |
| --- | --- | --- | --- |
| `POST /api/v1/nodes/move` | Owner | `paths`, `destination`, `conflict` (`keep_both` or `skip`), `allow_public_exposure` | Cycles rejected; no overwrite |
| `POST /api/v1/nodes/bulk` | Owner | `paths`, `action` (`trash`, `restore`, `delete`), `allow_public_exposure` | Delete returns `202` with job IDs; poll them |

Move, trash and restore succeed or roll back together per request. Moving or
restoring into a shared folder returns `409 PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED`;
repeat with `allow_public_exposure: true` after the owner confirms. Permanent
deletion removes catalog records only and can't be cancelled; see
[why Telegram files are kept](usage.md#why-permanent-deletion-keeps-the-telegram-files).

## Performance, cache inventory and preparation

All routes are **Owner**. Field meanings and limits are in
[Performance](performance.md) and [reading the Performance page](performance-tuning.md#read-the-performance-page).

| Method and route | Purpose | Notes and errors |
| --- | --- | --- |
| `GET /api/v1/settings/performance` | Owner preferences, revision and defaults; excludes managed resource values | Live managed values are under `/api/v1/performance` → `tuning` |
| `PATCH /api/v1/settings/performance` | Only `timezone`, `auto_preload` and `upload_split_mib` | Optional revision in `If-Match`; stale edits return `409`. Managed keys (including `max_file_size`, `staging_budget_bytes`, `min_free_bytes`) and unknown fields return `422` |
| `GET /api/v1/performance` | Live accounts, queues, `traffic`, `server`, `drive`/`database` readings, `tuning` and `diagnosis` | Live samples reset on restart; recorded history/totals use the history route |
| `GET /api/v1/performance/history` | `start`, `end` (Unix seconds, within 31 days): up to 360 bins plus lifetime `totals` | `422` bad range; `503` storage unavailable. Don't add the totals together |
| `GET /api/v1/preloads` | Cached files, coverage as `[start, end)` intervals and jobs | |
| `POST /api/v1/preloads` | `{"path"}`: preload a file | Rejected if it can't fit RAM or disk cache |
| `POST /api/v1/preloads/clear` | `{"file_id"}` evicts one file; `{}` clears all cached bytes | Originals stay in Telegram |
| `POST /api/v1/preloads/{job_id}/cancel` | Cancel a preload | Keeps work other readers still need |

The optional `diagnosis` describes recent aggregate observations, not a throughput
controller or a complete bottleneck analysis. It reports `unavailable` when
samples are absent or older than 15 seconds, `observing` until six seconds of
samples have accumulated, then `active` or `idle` for the observed window.
The sampler retains up to 11 samples over 30 seconds. Rates and cache counts
can be null when their inputs are missing or reset. Its observed constraints
cover Telegram queues, queued-account cooldowns and scratch admission waits;
they can coexist and do not establish network capacity or a unique cause.

<a id="zip-archive-jobs"></a>

## Archive jobs

| Method and route | Access | Body | Result |
| --- | --- | --- | --- |
| `POST /api/v1/archives` | Owner | `paths`, `destination`, `name` | `{id, job}` for a ZIP job |
| `POST /api/v1/archives/extract` | Owner | `path`, `destination`, `folder_name` (`null` extracts in place) | `{id, job}` for an extract job |

Both accept `allow_public_exposure`. Follow and control the job with the
[job routes](#jobs). Supported formats are listed in
[the user guide](usage.md#create-zips-and-extract-archives).

| Limit | Value |
| --- | --- |
| ZIP creation | Stored entries, no recompression |
| Extraction formats | ZIP (Stored, Deflate, BZIP2, LZMA), 7z, RAR/RAR5, TAR, gzip/bzip2/xz/Zstandard |
| Rejected | Encrypted or split archives, unsafe or duplicate paths, links, empty regular files |
| Disk space | Shares `STAGING_BUDGET_BYTES` and `MIN_FREE_BYTES` with uploads; extraction needs the archive and its output together |
| Entries | At most 1,000 files/folders, including implicit parent folders; applies to ZIP creation and extraction |
| Paths | At most 64 components and 4,096 characters |
| Metadata | 4 MiB manifest/job-plan bound; ZIP central directory at most 8 MiB, checked before allocating entry objects |
| Worker | Separate process; 512 MiB virtual address-space and 600-second CPU-time limits where supported. Expensive valid archives may be rejected; originals remain downloadable |

Failed jobs keep their staged bytes for **Retry**; cancel to release the space.
Files already saved stay in the drive. Pipeline details:
[archive jobs](architecture.md#archive-jobs).

## Resumable browser receipts

All routes are **Owner**. This is TGDrive's own chunk protocol, not tus. Durability
and reconciliation are in [the transfer pipeline](architecture.md#from-a-browser-to-one-drive-file).

| Method and route | Purpose | Notes and errors |
| --- | --- | --- |
| `POST /api/v1/uploads` | `name`, `parent_id`, `total_size`, optional `folder_upload`: create a receipt | `201`; `413` too large; `507` no staging space |
| `GET /api/v1/uploads/{upload_id}` | Current receipt with chunk checksums | `410` expired |
| `HEAD /api/v1/uploads/{upload_id}` | `Upload-Offset`, `Upload-Length` headers | |
| `PATCH /api/v1/uploads/{upload_id}` | Raw `application/octet-stream` chunk with `Upload-Offset` | `204`; `409 UPLOAD_OFFSET_CHANGED`; `413` chunk above 8 MiB; `415` wrong content type; `422` invalid offset/chunk; empty `499` on a disconnected chunk body |
| `POST /api/v1/uploads/{upload_id}/complete` | Hand the file to a server job | `202 {id, job}`; repeats return the same job |
| `POST /api/v1/uploads/{upload_id}/cancel` | Cancel the receipt | |

Send exactly `chunk_size` bytes (8 MiB) per request, except a smaller last chunk,
and wait for each acknowledgment. After a lost response, read the offset with
`GET` or `HEAD` before resending. Call `complete` only when the offset equals
`total_size`, then poll the job. Receipts expire after
`UPLOAD_SESSION_TTL_SECONDS` without new chunks (default 24 hours). Simultaneous
chunk receivers wait for the receive-work bound; receipt count is not a `429`
quota. Staging admission can still reject new work.

A disconnect while receiving a chunk leaves its durable offset unchanged. The
`499` response may not reach the disconnected client; reconnect and query the
offset before resending, following the lost-response procedure above.

## Previews and media

Access is **Owner** or **Share**; pass `path` and optional `auth` as for `/file`.

| Method and route | Purpose | Notes and errors |
| --- | --- | --- |
| `GET /api/v1/preview` | `kind`: `text`, `pdf`, `video`, `book` or `unsupported` | Text is at most 256 KiB |
| `GET /api/v1/books` | Book index, or one chapter or page with `entry` | [Reader limits](architecture.md#book-reader-internals) |
| `GET /api/v1/media` | Original plus real stored variants | `/file?quality=<id>` picks one; unknown IDs `404` |
| `GET /api/v1/thumbnail` | Cover JPEG up to 128 KiB | `204` none; `304` unchanged; `503 THUMBNAIL_BUSY` with `Retry-After: 5` |
| `GET /api/v1/poster` | Stored video poster up to 1 MiB | `204` none; `304`; `503 POSTER_BUSY` with `Retry-After: 5` |
| `GET /api/v1/file-status` | `request_id`: queue estimate, size, `phase`, `retry_after` | `TELEGRAM_QUEUE_FULL` matches a `503` from `/file` |
| `GET /api/v1/settings/thumbnails/rebuild` | Owner: latest rebuild run | |
| `POST /api/v1/settings/thumbnails/rebuild` | Owner: `{"confirmed": true}` starts a rebuild | `202`; reuses a running one |
| `POST /api/v1/settings/thumbnails/rebuild/cancel` | Owner: stop the rebuild | |

Owner or independently authenticated demo-admin cookies can use video and PDF progress;
a share token or public-demo access alone does not authorize these routes. The
response uses `Cache-Control: no-store` and progress is not included in ordinary
file or share responses.

| Method and route | Body | Result |
| --- | --- | --- |
| `GET /api/v1/files/{file_id}/video-progress` | — | `progress` is null or contains `position_seconds`, `duration_seconds` and `updated_at` |
| `POST /api/v1/files/{file_id}/video-progress/session` | `{}` | Starts a writer and returns `session_id`, current `progress` and `save_interval_seconds` |
| `PUT /api/v1/files/{file_id}/video-progress` | `session_id`, increasing `sequence`, `position_seconds`, `duration_seconds`, optional `final` | Saves progress; a final save closes the writer |
| `GET /api/v1/files/{file_id}/pdf-progress` | — | `progress` is null or contains `page_number`, `page_count`, `updated_at` |
| `POST /api/v1/files/{file_id}/pdf-progress/session` | `{}` | Starts a writer; returns `session_id`, `progress`, `save_interval_seconds` |
| `PUT /api/v1/files/{file_id}/pdf-progress` | `session_id`, increasing `sequence`, `page_number`, `page_count`, optional `final` | Saves the page; final closes the writer |
| `POST /api/v1/video-progress/summaries` | `{"file_ids": [...]}` (0–128 entries before deduplication) | `progress` maps eligible file IDs to saved position/duration/timestamp; unsaved, missing, trashed or nonvideo items are omitted |

A new writer supersedes the previous writer. Identical retries are read-only;
stale sequences and superseded or closed writers return `409`. Session starts
are paced at one second and regular saves at five seconds; a rate rejection
returns `429` with `Retry-After`. Positions must be finite and within a positive
duration. The final save is not subject to the regular save interval. Owner
progress is stored with the file and can be included in later V2 snapshots;
progress saves do not advance the catalog revision or independently force a
backup. Activation restores positions and invalidates writer sessions.

PDF values are strict integers with `1 <= page_number <= page_count <= 10000`;
the page count is client-provided metadata, not a server decode result. The PDF
routes use `PDF_PROGRESS_OWNER_ONLY` (`403`), `PDF_PROGRESS_SESSION_CHANGED` and
`PDF_PROGRESS_SEQUENCE_STALE` (`409`), and `PDF_PROGRESS_RATE_LIMITED` (`429`).
Video summaries grant no share access, return no writer details and do not fetch
media. Public/share clients must use their scoped local history rather than
owner summaries, even when owner cookies are present.

Thumbnails and posters recheck access on every request, including cache hits
and `304`, and use `Cache-Control: private, no-cache`. They never download an
original video. Cover and poster rules: [Thumbnails](thumbnails.md).

## Jobs

All routes are **Owner**.

| Method and route | Purpose | Notes and errors |
| --- | --- | --- |
| `GET /api/v1/jobs` | All retained jobs, including finished ones and `history_cleared` markers | |
| `GET /api/v1/jobs/{job_id}` | One job | `404` |
| `POST /api/v1/jobs/{job_id}/cancel` | Cancel | May stay `cancelling` while Telegram work settles |
| `POST /api/v1/jobs/{job_id}/retry` | Retry with the same operation identity | `409` for invalid transitions |
| `POST /api/v1/jobs/{job_id}/torrent-files` | `{"files": [indexes]}`: choose torrent files | `422` for empty, duplicate or out-of-range indexes |

A job has `id`, `kind` (`upload`, `remote`, `delete`, `archive`, `extract`, …),
`status` (`queued`, `running`, `cancelling`, `completed`, `error`, `cancelled`),
`phase`, `current`/`total`, timestamps, nullable `speed_bytes_per_second` and
`eta_seconds`, `queue` and `telegram_parts`. Treat unknown phases as normal; rates
and ETAs cover only the current phase. The live list is bounded; use
[transfer history](#transfer-history) for older work. Lifecycle:
[the transfer pipeline](architecture.md#transfer-pipeline).

## Transfer history

All routes are **Owner** and `no-store`. Storage: [finished history](architecture.md#finished-history).

| Method and route | Purpose | Notes |
| --- | --- | --- |
| `POST /api/v1/transfers/history/tasks` | `status`, `page`, `limit` (1–200), `q`, optional `offset` and `exclude_folders`: numbered task pages | Folder uploads group into one task |
| `GET /api/v1/transfers/history` | `page` for numbered pages, or `before` + `before_id` cursors; `status`, `limit`, `q` | Page and cursor together is invalid |
| `DELETE /api/v1/transfers/history` | `{"exclude_folders": []}`: clear finished history | `{"cleared": N}`; running work and stored files are kept |
| `POST /api/v1/transfers/location` | `transfer_id`, `destination`, `completed`: find a transfer's folder and item | `available: false` if moved to Trash or missing |

Statuses: `all`, `queued`, `running`, `finished`, `completed`, `error`,
`cancelled`. `finished` covers the last three.

## Recent files

| Method and route | Access | Purpose | Notes and errors |
| --- | --- | --- | --- |
| `GET /api/v1/recent` | Owner | `limit` (`25`, `50` or `100`; default 50), `cursor` (up to 256 characters), optional `type`: files by last upload or owner open | `{items, next_cursor}`; items add `path`, `parent_names`, `active_at`. `422` for other limits |

## Starred items

| Method and route | Access | Purpose |
| --- | --- | --- |
| `GET /api/v1/starred` | Owner | Starred items, optional `q` filter |
| `PATCH /api/v1/starred` | Owner | `{"path", "starred": bool}`; repeating is harmless |

Stars follow moves and renames, hide while in Trash and return on restore. The
root can't be starred.

## Shares, downloads, and ranges

| Method and route | Access | Purpose | Notes and errors |
| --- | --- | --- | --- |
| `GET /api/v1/shares` | Owner | All links, active and expired | Never returns tokens |
| `POST /api/v1/shares` | Owner | `path`, optional `label`, `expires_in_days` (1–3650, default 7; `null` = never) or explicit `expires_at` | `201` with `auth`; up to 100 links per item |
| `POST /api/v1/shares/link` | Owner | Copy an existing link by `share_id` or `path` | `409` unrecoverable or ambiguous; `410` expired |
| `PATCH /api/v1/shares` | Owner | Required `share_id`, plus `label` and/or `expires_at` (`null` = never) | Update by link ID, not item path |
| `DELETE /api/v1/shares` | Owner | Revoke one link by `share_id`, or all direct links by `path` | |
| `POST /api/v1/download-links` | Owner | `path`: attachment URL plus `expires_at` for a download manager | Reuses a copyable "Download link" share with its current expiry; only a new share defaults to one day |
| `POST /api/getFileShareAuth`, `/api/getFolderShareAuth` | Owner | Older link-creation routes | |
| `GET /api/v1/embed` | Share | File metadata for the embed viewer | Token required even with an owner cookie |
| `GET`, `HEAD /file` | Owner, Share | File bytes; `Range`, `quality`, `preview=1`, `download=1` | `206`, `304`, `416`, `503` with `Retry-After` |

URL formats:

| Use | URL |
| --- | --- |
| Shared file viewer | `/embed?path=%2F<file-id>&auth=<token>` |
| Shared folder | `/?path=%2Fshare_<folder-id>&auth=<token>` |
| Embed | `/embed?path=%2F<file-id>&auth=<token>` or `/embed?path=%2Fshare_<folder-id>&auth=<token>` |
| Raw file bytes / older direct file links | `/file?path=<id-path>&auth=<token>` |
| Attachment download | `/file?path=<id-path>&auth=<token>&download=1` |

- `/file` supports one range per request (`bytes=0-1023`, `bytes=1024-`,
  `bytes=-1024`). Multi-range or malformed headers get the full file with `200`.
- `If-Range` needs the strong ETag from a `GET`; otherwise the full file is sent.
- A supplied `auth` must be valid even with an owner cookie. Revocation and expiry
  apply to cached bytes too, but can't recall bytes already sent.
- Share tokens are HMACs of the share ID under `SESSION_SECRET`; only their hash is
  stored. Keep `SESSION_SECRET` to copy links again on a new installation.

Embedding steps: [the user guide](usage.md#embed-a-file-or-folder-in-a-website).
Caching and read-ahead: [Performance](performance.md#shared-download-load-and-queues).

## URL imports and transfer routes

All routes are **Owner** `POST`.

| Route | Body | Result |
| --- | --- | --- |
| `/api/getFileInfoFromUrl` | `{"url"}` | Name, size (`-1` unknown), content type |
| `/api/startFileDownloadFromUrl` | `url`, `path`, `filename`, `singleThreaded`, `client_request_id` | Job `id` |
| `/api/getTransferByRequest` | `{"client_request_id"}` | Job snapshot |
| `/api/getTransferProgress` | `{"id"}` | Job snapshot |
| `/api/cancelUpload` | `id` or `client_request_id`, not both | Cancellation snapshot |
| `/api/upload` | Multipart `file`, `path`, optional `client_request_id` | One-shot upload without resume |

Source, size and storage checks run inside the job, so they show as job errors,
not HTTP errors. Reusing a `client_request_id` with different values returns
`409`. That ID mapping lives in memory and is lost on restart; keep the job ID.
Retry phases and bounds: [URL import intake](performance.md#url-import-intake).

To cancel a URL start whose response is lost or still pending, use the same
`client_request_id` with `/api/cancelUpload` rather than starting another
request. Cancellation can record an intent before the job ID is bound; a
cancelled start cannot be retried with that same request ID. These intents are
process-local, not durable across restarts. Once you have a job ID, retain it
and inspect the job after interruption; cancellation cannot undo an already
committed upload.

## Torrent imports

`POST /api/startTorrentDownload` (**Owner**) takes
`{"url": "magnet:?…" or a public .torrent URL, "path", "client_request_id"}` and
returns a job `id`. Use the [job routes](#jobs) for progress, file choice,
cancel and retry. The source link is never shown in snapshots.

| Policy | Value |
| --- | --- |
| Engine | `aria2c` on `PATH`; included in Docker |
| Concurrency | `MAX_CONCURRENT_TRANSFERS` engines (default 2) |
| Sources | BitTorrent v1 (including hybrid); v2-only unsupported |
| Metadata | `.torrent` up to 256 MiB; magnet lookup times out after 120 seconds |
| File choice | With `select_files: true`, the job waits in `torrent_select` with `torrent_choice`; send indexes to `/torrent-files` |
| Peers | IPv4 DHT and public UDP trackers; HTTP trackers and web seeds are ignored |
| Stall | Five minutes without progress fails with a retry option |
| Automatic retry | Typed timeout/network interruptions only; four total attempts with 15/30/60-second waits. Waiting releases the download slot while retaining staging and file selection |
| Terminal failures / manual retry | No-peer stalls and permanent peer, disk or input errors need owner action. Verified pieces and already uploaded files are kept; manual retry starts a fresh attempt budget |

Phases run `torrent_metadata`, `downloading`, then `torrent_uploading`, with
`torrent_files: {total, completed}`. Cancelling keeps files already saved, so a
folder import can be partly filled. User steps:
[the user guide](usage.md#import-a-torrent-or-magnet-link).

## Errors and retry discipline

Errors usually carry `detail` and sometimes a stable `code`, such as
`PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED`, `FOLDER_TREE_CONFLICT`,
`UPLOAD_OFFSET_CHANGED`, `BACKUP_PENDING` or `RECOVERY_REQUIRED`. Match on codes,
not on message text.

Settings failures use `{detail, code}` with their stated HTTP status, including
`START_NEW_UNAVAILABLE`. Error envelopes vary across older routes.

| Status | Meaning | What to do |
| --- | --- | --- |
| 400 | Bad path, range, action or blocked URL | Fix the request |
| 401 | Not signed in or wrong password | Sign in |
| 403 | Share out of scope, foreign origin or demo restriction | Check access |
| 404 | Missing item, job, receipt or Telegram data | Refresh state |
| 409 | Conflict or confirmation needed | Read current state, then decide |
| 410 | Receipt or history expired | Start again |
| 413 | Too large | Use a smaller file or chunk |
| 415 | Wrong chunk content type | Send `application/octet-stream` |
| 422 | Invalid fields or empty file | Fix the request |
| 429 | Login throttle or concurrent password-verification limit | Wait for `Retry-After` |
| 502 | Remote source failed | Retry later |
| 503 | Not ready or queue full | Wait for `Retry-After` |
| 507 | Not enough staging space | Free space or wait |

A client timeout does not mean server work stopped. After a timeout, read state
before replaying: the receipt offset for uploads, the job for completions and
deletions, the listing for bulk actions. Never log cookies, share tokens or
signed URLs.

Source of truth: [app.py](../tgdrive/web/app.py), [api_v1.py](../tgdrive/web/api_v1.py),
[request schemas](../tgdrive/web/schemas.py) and the
[frontend client](../website/src/shared/api.ts). Change them, this page and their
tests together.
