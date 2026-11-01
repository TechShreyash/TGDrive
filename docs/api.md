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
- Dates are ISO strings, except upload `expires_at` and job timestamps, which are
  Unix seconds.

## Session, configuration, health, and assets

| Method and route | Access | Purpose | Notes and errors |
| --- | --- | --- | --- |
| `GET /api/config` | Public | `status`, `max_file_size`, `timezone`, `recovery_only`, `demo_mode`, `demo_read_only`, `demo_admin` | `no-store` |
| `POST /api/checkPassword` | Public | `{"pass": "..."}` signs in and sets the cookie | `401` wrong password; `429` after five failures per IP in five minutes |
| `POST /api/logout` | Cookie | Revokes the session and clears the cookie | |
| `GET /api/v1/session` | Public | `{"authenticated": bool}` plus demo permission fields | `no-store` |
| `GET /api/v1/status` | Owner | Connections, capacity, catalog revision, latest backup, client health, `telegram_issues` | Does not query Telegram backup history |
| `POST /api/v1/backup` | Owner | Creates a confirmed recovery file now | `503` if it can't be confirmed |
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
| `GET /api/v1/replica/file` | Download the latest confirmed `tgdrive-backup.tgdb` | `404` until one exists; denied in demo mode |
| `GET /api/v1/replica/report` | Secret-free status report | May still contain private names and IDs |
| `POST /api/v1/replica/import` | Raw recovery-file bytes; optional `X-TGDrive-Source-Channel`, `X-TGDrive-Bot-Token` | Returns a candidate; does not activate. Disabled in demo mode |
| `POST /api/v1/replica/restore` | Restore the backup found in the storage channel at startup | Restart when asked. Disabled in demo mode |
| `POST /api/v1/replica/recover` | Three steps: `source_channel` + `bot_token` to inspect; `target_channel` + `target_bot_token` + `confirm_channel` (optional `catalog_only`) to copy; `activate: true` + `confirm_channel` to switch | Wrong confirmation or an incomplete candidate can't activate |
| `POST /api/v1/start-new` | `confirm: "START NEW"` and `password`: erase this drive and start an empty one | `403` wrong password or demo mode; `409` already running; `422` missing confirmation. [What is erased](backup-recovery.md#start-a-new-drive) |

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
| `POST /api/getDirectory` | Owner, Share | `{"path", "auth"}`: folder contents keyed by child ID | Special paths: `/trash`, `/search_<q>` (max 500 results), `/share_<folder-id>` |
| `GET /api/v1/nodes` | Owner | `parent_id`, `limit` (1–500), `sort` (`name`, `size`, `type`, `date`), `order` (`asc`, `desc`), optional `type` filter, then `cursor` (up to 1024 characters) or `offset` | `{items, total, offset, next_cursor}`; `next_cursor` is `null` on the last page. `422` for an invalid cursor or a cursor with a non-zero offset |
| `GET /api/v1/shared/nodes` | Share | `auth` (1–512 characters) plus the same parameters as `/api/v1/nodes` | `{items, total, offset, next_cursor, share_home}`, without `starred`; `private, no-store`. `403` invalid share or folder outside it; `409` during recovery; `422` bad paging |
| `GET /api/v1/search` | Owner | `q` (1–255), `offset`, `limit` (1–200), `sort`, `order` | Name-only, literal match, excludes Trash; `{items, has_more, next_offset}` |
| `GET /api/v1/trash` | Owner | Top-level Trash items, optional `q` | Pending deletions are hidden |
| `POST /api/createNewFolder` | Owner | `{"path", "name"}` | |
| `POST /api/renameFileFolder` | Owner | `{"path", "name"}` | |
| `POST /api/trashFileFolder` | Owner | `{"path", "trash": bool}` | No exposure check; prefer the bulk route |
| `POST /api/deleteFileFolder` | Owner | `{"path"}` of a top-level Trash item | `202` with a deletion job ID |

`type` is `folder`, `media`, `docs`, `audio` or `other`, matching the My Files
filter chips. Pass `next_cursor` back as `cursor` to page large folders; it stays as fast as the
first page, while deep `offset` pages slow down. Folders sort first. File rows include `part_count` but never message IDs or checksums.

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
| `GET /api/v1/settings/performance` | Saved preferences, revision and defaults | |
| `PATCH /api/v1/settings/performance` | Partial lowercase preferences, including `max_file_size`, `staging_budget_bytes`, `min_free_bytes` | Send the revision in `If-Match`; stale edits conflict; unknown fields rejected |
| `GET /api/v1/performance` | Live accounts, queues, `traffic`, `server` readings and `tuning` | Process-local; resets on restart |
| `GET /api/v1/performance/history` | `start`, `end` (Unix seconds, within 31 days): up to 360 bins plus lifetime `totals` | `422` bad range; `503` storage unavailable. Don't add the totals together |
| `GET /api/v1/preloads` | Cached files, coverage as `[start, end)` intervals and jobs | |
| `POST /api/v1/preloads` | `{"path"}`: preload a file | Rejected if it can't fit RAM or disk cache |
| `POST /api/v1/preloads/clear` | `{"file_id"}` evicts one file; `{}` clears all cached bytes | Originals stay in Telegram |
| `POST /api/v1/preloads/{job_id}/cancel` | Cancel a preload | Keeps work other readers still need |

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
| Worker | Separate process, no memory or CPU cap; the kernel stops it first under memory pressure |

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
| `PATCH /api/v1/uploads/{upload_id}` | Raw `application/octet-stream` chunk with `Upload-Offset` | `204`; `409 UPLOAD_OFFSET_CHANGED`; `415`; `422` |
| `POST /api/v1/uploads/{upload_id}/complete` | Hand the file to a server job | `202 {id, job}`; repeats return the same job |
| `POST /api/v1/uploads/{upload_id}/cancel` | Cancel the receipt | |

Send exactly `chunk_size` bytes (8 MiB) per request, except a smaller last chunk,
and wait for each acknowledgment. After a lost response, read the offset with
`GET` or `HEAD` before resending. Call `complete` only when the offset equals
`total_size`, then poll the job. Receipts expire after
`UPLOAD_SESSION_TTL_SECONDS` without new chunks (default 24 hours). `429` means
too many active receipts.

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
| `POST /api/v1/shares` | Owner | `path`, optional `label`, `expires_in_days` (1–3650, default 7) or `expires_at` | `201` with `auth`; up to 100 links per item |
| `POST /api/v1/shares/link` | Owner | Copy an existing link by `share_id` or `path` | `409` unrecoverable or ambiguous; `410` expired |
| `PATCH /api/v1/shares` | Owner | Change `label` or `expires_at` (`null` = never) | |
| `DELETE /api/v1/shares` | Owner | Revoke one link by `share_id`, or all direct links by `path` | |
| `POST /api/v1/download-links` | Owner | Signed one-day attachment URL for a download manager | Reuses an active "Download link" share |
| `POST /api/getFileShareAuth`, `/api/getFolderShareAuth` | Owner | Older link-creation routes | |
| `GET /api/v1/embed` | Share | File metadata for the embed viewer | Token required even with an owner cookie |
| `GET`, `HEAD /file` | Owner, Share | File bytes; `Range`, `quality`, `preview=1`, `download=1` | `206`, `304`, `416`, `503` with `Retry-After` |

URL formats:

| Use | URL |
| --- | --- |
| File | `/file?path=<id-path>&auth=<token>` |
| Shared folder | `/?path=%2Fshare_<folder-id>&auth=<token>` |
| Embed | `/embed?path=%2F<file-id>&auth=<token>` or `/embed?path=%2Fshare_<folder-id>&auth=<token>` |

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
| Retry | Verified pieces and already uploaded files are kept |

Phases run `torrent_metadata`, `downloading`, then `torrent_uploading`, with
`torrent_files: {total, completed}`. Cancelling keeps files already saved, so a
folder import can be partly filled. User steps:
[the user guide](usage.md#import-a-torrent-or-magnet-link).

## Errors and retry discipline

Errors usually carry `detail` and sometimes a stable `code`, such as
`PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED`, `FOLDER_TREE_CONFLICT`,
`UPLOAD_OFFSET_CHANGED`, `BACKUP_PENDING` or `RECOVERY_REQUIRED`. Match on codes,
not on message text.

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
| 429 | Login throttle or receipt capacity | Wait for `Retry-After` |
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
