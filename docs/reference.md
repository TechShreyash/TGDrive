# Configuration and API reference

[Docs home](README.md) · For operators and contributors

**Using the web app? You can skip this page.**
Start with [Setup](setup.md) or [Using TGDrive](usage.md).

Open a section below when you need a specific setting or API contract.
Detailed tables and examples are expandable.

| Looking for… | Go to |
| --- | --- |
| Credentials and defaults | [Configuration](#configuration) |
| File-size and concurrency limits | [Capacity](#file-capacity-staging-and-work-limits) |
| Cache, preloads and media admission | [Performance settings](#performance-and-cache-settings) |
| CLI commands | [Maintenance](#owner-maintenance-commands) |
| Routes and request/response rules | [HTTP API](#http-api) |
| Access, privacy and safe deployment | [Security](#security-and-privacy) |

## Terms used here

| Term | Meaning |
| --- | --- |
| Catalog / metadata | Records describing your files and folders; not their contents |
| Receipt | The server's record of upload bytes already received |
| Manifest | The ordered list of Telegram parts making up one large file |
| Lease / ownership | A temporary lock that keeps two installations from writing at once |
| Share capability | A secret link/token that grants access to specific content |
| Idempotent request | Retrying the same request does not create a second result |

## Configuration

1. Start with private environment variables or `.env`.
2. Saved MongoDB settings fill missing bootstrap values.
3. Telegram connections edited in the web app override the original Telegram
   credential fields and apply after a restart.

Host environment values override `.env`. An edited Docker environment needs
container recreation, rather than only a process restart.

Settings are case-insensitive. Unknown names are ignored, so check for typos.
Never publish real values or use the example placeholders as credentials.

### File syntax and secret handling

<details>
<summary>Formatting, blank values and secrets</summary>

Use one NAME=value assignment per line. Use true or false for booleans,
decimal integers for byte counts, and commas for account/administrator lists.
Whitespace around comma-separated entries is removed. Empty entries are ignored.

Keep .env owner-readable and outside source control. Quote an Argon2 hash in
single quotes because it contains dollar signs. Do not shell-source a
configuration file or put secrets in shell command arguments. Docker's env_file
and Python dotenv parse the file themselves.

Optional does not always mean that a blank value is accepted:

- Omit ADMIN_PASSWORD_HASH when unused; a blank value is treated as absent.
- ADMIN_PASSWORD= is allowed only when a valid hash is supplied.
- DATABASE_BACKUP_MSG_ID= means no historical reference.
- STRING_SESSIONS=, MAIN_BOT_TOKEN=, and TELEGRAM_ADMIN_IDS= disable their
  optional features.
- An empty MAX_FILE_SIZE is invalid; omit or comment it to use the bounded default logical-file limit.

Invalid settings fail before serving the application. Secret wrappers and
hidden validation inputs reduce accidental disclosure in exception text; they
do not make a configuration file safe to publish.

</details>

### Telegram accounts

<details>
<summary>API credentials, bot tokens and optional accounts</summary>

| Variable | Required/default | Validation and behavior |
| --- | --- | --- |
| `API_ID` | Required unless supplied by managed settings | Positive integer Telegram application ID; the web editor bounds it to 1–2147483647. |
| `API_HASH` | Required unless managed, secret | Exactly 32 hexadecimal characters; surrounding whitespace is removed. |
| `BOT_TOKENS` | Required unless managed, secret | At least one comma-separated storage bot token. Tokens must match the expected numeric-ID and secret format; Telegram verifies actual validity on connection. |
| `STORAGE_CHANNEL` | Required | Negative integer private channel ID. Confirmed catalogs bind to this channel; changing it is not relocation and can trigger RECOVERY_REQUIRED. Use the correct membership/permissions from [Telegram setup](setup.md#additional-bots-and-premium-accounts). |
| `STRING_SESSIONS` | Empty by default, secret | Comma-separated Telethon StringSession credentials for optional Premium users. Sessions must already be authorized and identify Premium user accounts. |
| `MAIN_BOT_TOKEN` | Omitted/empty by default, secret | Optional bot-mode token. A nonempty value requires TELEGRAM_ADMIN_IDS. |
| `TELEGRAM_ADMIN_IDS` | Empty by default | Comma-separated positive numeric Telegram user IDs allowed to use bot mode. |
| `DATABASE_BACKUP_MSG_ID` | None by default | Legacy positive checkpoint message ID; blank is accepted. Unused by MongoDB startup and restore-mongodb. |

Storage bots are the required baseline. The running pool can remain ready with
a healthy storage bot when additional bots or Premium sessions fail.
Premium capability is checked from healthy, started clients with confirmed
connectivity, not merely a nonempty STRING_SESSIONS value. See
[Telegram behavior](setup.md#additional-bots-and-premium-accounts) for account selection and recovery limits.

</details>

### Performance and cache settings

Saved owner performance preferences override their startup defaults and apply
to new work without replacing Telegram credentials. MongoDB stores them in the
settings collection, included in encrypted database backups. Environment
changes require process/container restart; recreating a container does not
override a saved preference. The separate connection editor still requires a
restart after credential changes.

| Variable | Default | Meaning and bounds |
| --- | --- | --- |
| `CACHE_MEMORY_BYTES` | 268435456 | RAM payload budget, 0–8 GiB. Effective retained RAM is further bounded by available host/container memory. |
| `CACHE_DISK_BYTES` | 10737418240 | Disk budget, 0–2 TiB. Zero disables disk retention. It does not reserve that amount of disk in advance. |
| `CACHE_MIN_FREE_BYTES` | 1073741824 | Free-disk floor for cache writes, 0–2 TiB. |
| `CACHE_TTL_SECONDS` | 86400 | Sliding disk retention, 60–2678400 seconds. Eviction or disk failure can remove bytes sooner. |
| `AUTO_PRELOAD` | true | Enable bounded speculation after delivered sequential bytes; never bulk preload from HEAD or metadata alone. |
| `PRELOAD_MAX_JOBS` | 2 | Concurrent preparation jobs, 1–8; foreground work has priority. |
| `TELEGRAM_UPLOAD_PIPELINE` | 4 | Outstanding upload-part requests per document, 1–16. |
| `TELEGRAM_REQUEST_LIMIT` | 8 | Shared per-account media/metadata request concurrency, 1–32. |
| `TELEGRAM_PARTS_PER_SECOND` | 48 | Application request pacing ceiling, 1–128, including scheduled metadata work. This is not a Telegram-provided bandwidth limit. |
| `TELEGRAM_MEDIA_PER_BOT` | 3 | Shared concurrent origin media operations per account, 1–16. Uploads also retain their separate per-bot/user lane limits. |
| `TELEGRAM_MAX_QUEUE` | 256 | Global queued origin operations, 1–4096; overflow returns retry guidance. |
| `UPLOAD_SPLIT_MIB` | 2000 | 0 uses the safe account/document maximum: 2000 MiB bots, or an eligible 4000 MiB Premium document. Explicit 1–2000 values choose a smaller preferred cap. The minimum grows when needed to stay within 4096 parts. Persisted plans keep their original boundaries. |
| `TIMEZONE` | Asia/Kolkata | IANA timezone for the owner's displayed dates/times; use a valid zone name. |

The memory budget also covers the bounded asynchronous disk write backlog.
Active responses share an additional transient payload budget, default 32 MiB,
clamped by available memory with a 2 MiB streaming floor. A response's read-ahead
window scales with active consumers; a single consumer can use the full budget.
New response heads reclaim suspended readers' speculative blocks; only their
awaited/yielded heads stay protected. Pending cancellation drains before a
reservation is released, and shared fills continue for remaining subscribers.
This is an internal policy, not an
additional owner setting. `cache.scratch` reports current/effective/configured
bytes, peak reservations, waiting heads and active streams. Transport and
server buffers are outside retained payload-cache and scratch budgets.
RAM/disk can hold duplicate hot blocks; summing the tiers is not a whole-file
retention guarantee. Disk-backed manual preparation includes per-block integrity overhead
and competing active reservations when checking capacity.

Automatic preparation starts after at least 4 MiB of delivered sequential data.
A first viewing warms a bounded ahead region. Whole-file preparation additionally
requires two sequential runs of at least 4 MiB overlapping by at least 4 MiB
within ten minutes, and the file plus overhead must fit half an effective tier.
Read-ahead adapts to consumption and fill rates within 4–32 MiB, or a quarter
of effective capacity if smaller. Leaving the final viewer stops ahead work;
a preparation justified by repeated use may finish. Metadata and small probes
do not qualify. See the [automatic policy](performance.md#automatic-read-ahead).

Background jobs and block reads are bounded separately. Cache admission, TTL and eviction
remain active during and after preparation.

See [performance policy and tuning](performance.md) before raising these values.

### Owner authentication

<details>
<summary>Passwords, sessions and cookies</summary>

| Variable | Required/default | Validation and behavior |
| --- | --- | --- |
| `ADMIN_PASSWORD_HASH` | None by default, secret | Recommended owner credential. Must be a valid Argon2id hash with memory cost at least 19,456 KiB and time cost at least 2. Generate it with the owner CLI. |
| `ADMIN_PASSWORD` | Empty by default, secret | Plaintext fallback when no hash is configured. At least 12 characters after trimming. If a hash is present, clear the example plaintext value. A malformed nonempty plaintext value still fails validation. |
| `SESSION_SECRET` | Required, secret | Independent random signing secret of at least 32 characters after trimming. Do not reuse an account or password credential. |
| `SESSION_TTL_SECONDS` | 86400 | Integer 300-2592000 inclusive: browser session lifetime, in seconds. |
| `COOKIE_SECURE` | true | Secure cookies require HTTPS. Use false only for local plain HTTP on loopback. |

At least one password representation is required. The hash takes precedence
when both valid forms are configured. The plaintext fallback is hashed in
memory when the application is created; the .env value itself remains
plaintext.

Blank or whitespace-only plaintext values are canonicalized to empty and
require a valid hash. Nonempty plaintext values must meet the minimum after
trimming. The generated Argon2id hash setup is recommended.

The owner password is not a Telegram password. Browser sessions are signed
with SESSION_SECRET and tracked in MongoDB's sessions collection. Changing the signing
secret invalidates existing cookies. Changing the password alone is not a
universal session-revocation procedure; see [security](reference.md#security-and-privacy) for rotation
and access recovery.

Generate a hash using either installation's password-hash command. It prompts
twice, enforces a 12-character minimum, and does not require valid Telegram
settings. Encrypted MongoDB restore needs only database access and the recovery key.

</details>

### Persistence and initialization

<details>
<summary>MongoDB, backup keys and local paths</summary>

| Variable | Default | Validation and behavior |
| --- | --- | --- |
| `MONGODB_URI` | None in legacy/test settings | Secret URI beginning mongodb:// or mongodb+srv://. Required by documented deployment. |
| `MONGODB_DATABASE` | tgdrive | 1–63 ASCII letters/digits, underscores or hyphens; dedicated to one drive. |
| `MONGODB_BACKUP_KEY` | None | Secret 64-hex-character AES-256 key. Keep a separate recovery copy. |
| `MONGODB_BACKUP_SECONDS` | 3600 | Integer 60–604800 seconds between successful automatic safety copies. |
| `DATA_DIR` | data | Writable helper/lock directory; no MongoDB catalog/journals are stored here. |
| `CACHE_DIR` | cache | Local receipts, staging, encrypted temporary backups and caches. Keep unfinished bytes. |
| `INITIALIZE_EMPTY_DRIVE` | false | Legacy setting; no automatic startup initialization. |
| `DATABASE_BACKUP_TIME` | 60 | Legacy SQLite checkpoint interval; unused by MongoDB mode. |
| `DATABASE_BACKUP_MSG_ID` | None | Legacy recovery reference; unused by MongoDB startup/snapshots. |
| `LOG_LEVEL` | INFO | DEBUG, INFO, WARNING, ERROR or CRITICAL. |

DATA_DIR and CACHE_DIR must be writable distinct sibling paths, not a filesystem
root or account home, equal or nested. Compose sets /app/data and /app/cache.

The standard Compose deployment enables `TGDRIVE_AUTO_SECRETS=true`. Missing
`SESSION_SECRET` and `MONGODB_BACKUP_KEY` values are generated once for a new
drive and saved as mode 0600 in `DATA_DIR/deployment-secrets.json`. Explicit
values and existing MongoDB bootstrap settings take precedence. Existing drives
with missing keys fail rather than silently receiving replacement keys. Source
installations retain explicit-secret configuration unless this mode is enabled.

The storage_channel_id binding is a nonzero signed 64-bit integer. Switching
channels cannot silently reinterpret message IDs. Use a MongoDB replica set;
the application uses transactions, majority writes and bounded request/pool
settings. Initialization/restore refuses existing catalog state.

</details>

### File capacity, staging, and work limits

<details>
<summary>Exact sizes, disk reservations and concurrency bounds</summary>

| Variable | Default | Validation and behavior |
| --- | --- | --- |
| `MAX_FILE_SIZE` | Unset | Optional positive logical-file byte ceiling, further bounded by staging and the maximum multipart manifest size. It does not require Premium for files above a bot document's limit. |
| `STAGING_BUDGET_BYTES` | 17179869184 | Positive integer; 16 GiB total reservation budget for active browser receipts. |
| `MIN_FREE_BYTES` | 1073741824 | Nonnegative integer; preserve 1 GiB free space by default. Browser receipt admission reserves remaining bytes; URL downloads also check free space. |
| `UPLOAD_SESSION_TTL_SECONDS` | 86400 | Integer at least 60; incomplete receipt idle lifetime in seconds, refreshed after each committed chunk. There is no declared upper bound in configuration. |
| `MAX_CONCURRENT_TRANSFERS` | 2 | Integer 1–32; bounds local fingerprinting, URL retrieval and legacy receive work. Receipt handlers have a separate bound from this value. It does not cap Telegram uploads across bots. |
| `TELEGRAM_UPLOADS_PER_BOT` | 1 | Integer 1–4; initial per-bot Telegram media limit. **Settings → Uploads** saves an override in MongoDB and applies it immediately. Premium clients remain limited to one media upload each. |
| `MAX_CONCURRENT_STREAMS` | 4 | Compatibility configuration; the file-response path uses bounded origin/media admission instead of a fixed viewer-count gate. It does not cap cached viewers at four. |
| `TRANSFER_TTL_SECONDS` | 3600 | Integer 60-86400 inclusive; live terminal-job/context retention. Compact server-transfer history is kept separately from this cleanup; files are unaffected. |

The conservative per-document ceilings are 2000 MiB (2,097,152,000 bytes) for
bots and 4000 MiB (4,194,304,000 bytes) for Premium users. These are binary-MiB
application limits. The logical-file ceiling advertised by /api/config is
the minimum of STAGING_BUDGET_BYTES, optional MAX_FILE_SIZE and 2000 MiB × 4096
parts; the default is therefore 16 GiB. Sufficient free disk is still required.

- Files above the bot document limit use ordered bot-sized messages while retaining one
  drive item and logical progress.
- With automatic split size 0 and a healthy connected Premium account, an eligible
  file above 2000 MiB and at most 4000 MiB may use a single Premium document.
- An explicit split cap of 1–2000 MiB keeps parts bot-compatible and can use bots
  or eligible optional users rather than forcing the Premium-only lane.
- Unknown/disconnected accounts do not unlock that optimization.
- Larger files remain multipart; the logical cap does not depend on Premium.
- Multipart worker count follows healthy eligible account capacity, including live
  uploads-per-bot increases. Occupied or cooling slots still wait at the shared
  lease gate. Bot-compatible parts may use bots or Premium accounts; Premium-sized
  parts use only Premium accounts. Confirmed/replayed parts retain sender identity.
- Accepted receipts retain their durable offset and checksum recovery rules.

Browser chunks are fixed at 8 MiB, except the final chunk. That chunk size and
the maximum 1000 unfinished receipt records are implementation limits, not
environment settings. The staging budget covers browser receipt reservations;
allow separate headroom for URL work, metadata, logs, and backups. Increasing
the budget without adding disk does not create usable capacity.

- The web controller receives one file at a time, independently of Telegram jobs.
  Each received file is queued immediately; it does not wait for the rest of its batch.
- If staging is full or the receipt limit is reached, the next device file stays queued
  and retries automatically while the tab remains open.
- Each connected bot admits one Telegram media upload by default. More bots add slots;
  the saved per-bot setting can increase this to four. Multipart parts consume slots too.
  Lowering the setting waits for running uploads to finish; it never interrupts them.
- Account FloodWait deadlines and shared channel pacing still apply at every setting.
- It tracks up to 2,000 rows in unfinished batches, retaining completed siblings.
- Browser tracking bounds are per controller. Server staging reservations and
  per-bot media slots apply across tabs and API callers.
- Server reservation, job and handler limits still apply independently.
- Finished browser history is pruned by whole batches, up to 30 batches/2,000 rows;
  unresolved batches are retained.

See [transfers](architecture.md#transfer-pipeline) for offsets, cancellation, retries, empty-file
handling, and ordered logical file parts.

</details>

### Remote HTTP(S) uploads

<details>
<summary>URL fetching, timeouts and network settings</summary>

| Variable | Default | Validation and behavior |
| --- | --- | --- |
| `ALLOW_PRIVATE_REMOTE_URLS` | false | Enables private/internal destinations only when explicitly true. It broadens what the authenticated owner can reach. |
| `REMOTE_CONNECT_TIMEOUT` | 20 | Seconds, greater than 0 and at most 300. |
| `REMOTE_READ_TIMEOUT` | 120 | Seconds, greater than 0 and at most 3600; individual read timeout. |
| `REMOTE_TOTAL_TIMEOUT` | 21600 | Seconds, greater than 0 and at most 86400; total operation budget. |
| `REMOTE_MAX_REDIRECTS` | 5 | Integer 0-20 inclusive; manually validated redirect count. |
| `SLEEP_THRESHOLD` | 60 | Integer 0-3600 inclusive. Compatibility setting currently not consumed by transfer or transport code. It does not shorten Telegram flood waits. |

URL requests validate destinations and redirects, request identity encoding,
and enforce declared and observed sizes. The configured global ceiling still
bounds HTTP retrieval, while runtime admission applies current account
capacity. Telegram flood waits are persisted with a server-provided retry time;
the transport disables Telethon's automatic flood sleeping. Details and limits
are in [transfers](architecture.md#transfer-pipeline) and [security](reference.md#security-and-privacy).

</details>

### Deployment variables outside application Settings

<details>
<summary>Ports, proxy trust and worker settings</summary>

| Variable/control | Shipped behavior |
| --- | --- |
| `BIND_ADDRESS` | Compose interpolation only; defaults to 127.0.0.1 for the host binding. It is not a Settings field. |
| `PORT` | Compose's host port defaults to 8000; the supplied container command always listens on internal port 8000. A source launch uses its explicit --port argument. |
| `FORWARDED_ALLOW_IPS` | Optional explicit Uvicorn proxy trust override. The container launcher defaults to loopback plus the private IPs resolved from `TGDRIVE_PROXY_HOSTS`; the hosted Compose file sets that hostname to `coolify-proxy`. Unset or `auto` enables discovery. |
| `TGDRIVE_PROXY_HOSTS` | Deployment-owned, comma-separated internal proxy hostnames. Only their resolved private IPs are trusted. A DNS failure retains loopback trust and logs an actionable warning; it never enables wildcard trust. |
| --workers | Must remain 1. This is a launch argument, not an environment feature for scaling the drive. |

A .env entry read only by Pydantic does not automatically export variables to
Uvicorn in a source launch. Pass --forwarded-allow-ips explicitly there, or set
the actual process environment. The Render template passes environment
variables directly and uses wildcard proxy trust on the assumption that its
managed proxy is the only untrusted-client ingress. Do not copy that assumption
to an exposed standalone app port.

Apply settings using [operations](operations.md), then diagnose errors using
[troubleshooting](operations.md#troubleshooting). The [development chapter](development.md)
owns the version compatibility baseline.

</details>

## Owner maintenance commands

Run initialization/restore with the service stopped.

| Command after python -m tgdrive | Behavior |
| --- | --- |
| password-hash | Locally prompts and prints an Argon2id owner hash. |
| login | Authorizes an optional Premium account and saves a private session export. |
| init | Creates a new MongoDB catalog/settings, refusing an existing drive. |
| restore-mongodb --file FILE | Authenticates/decrypts a complete .tgdb archive and transactionally restores an empty database; revokes browser sessions. |

Restore needs MONGODB_URI, MONGODB_DATABASE and the original
MONGODB_BACKUP_KEY before normal settings are restored. It does not include
local unsent file bytes. The guided wizard inspects/prepares a new or existing
MongoDB drive and checks Telegram access without a backup-pin requirement.

Legacy SQLite init/recover/JSON restore branches remain for older installers
and compatibility tests; they are not the documented MongoDB operation path.

## HTTP API

Owner-only `POST /api/v1/transfers/location` accepts `transfer_id`, `destination` and `completed`. It resolves the current folder and exact saved item without mutation; missing/trashed items return `available:false`.

The web app uses these owner-session and scoped-share endpoints.
There are no API keys or multi-user accounts. Interactive OpenAPI pages are disabled.

### Conventions and authentication

<details>
<summary>Cookies, ID paths, bodies and timestamps</summary>

All routes are relative to the app origin. Use JSON bodies with
`Content-Type: application/json`, except multipart uploads and chunk PATCH.
The React client uses both /api/v1 routes and compatibility routes; compatibility
does not mean unused.

Drive paths contain **IDs**, not display names. Root is `/`; a nested item is
`/<folder-id>/<item-id>`. In a listing, a node's `path` is its **parent** path;
append `id` to address the item. The `parent_id` parameter in receipt/listing
requests currently accepts a complete folder path despite its name.

Private routes require the `tgdrive_session` cookie. Browser requests must use
same-origin cookies. Mutations reject a supplied foreign Origin, or
`Sec-Fetch-Site: cross-site` without an Origin. Non-browser clients may use a
protected cookie jar. There is no general cross-origin integration mode.

Metadata/share dates are ISO strings; receipt `expires_at` is Unix seconds.
Job `current`/`total` mean bytes for media transfers and message counts for
deletion. Additional response fields may appear. This is not a promise of
permanent third-party API compatibility.

</details>

### Session, configuration, health, and assets

<details>
<summary>Login, health, backup and asset endpoints</summary>

| Method and route | Access | Request | Success |
| --- | --- | --- | --- |
| `GET /` | Public shell | None | React HTML; build instructions if frontend source is not built. |
| `GET /api/config` | Public | None | `{"status":"ok","max_file_size":17179869184}` with the default 16 GiB logical limit. |
| `POST /api/checkPassword` | Public, throttled | `{"pass":"<owner-password>"}` | `200 {"status":"ok"}` plus session cookie. |
| `POST /api/logout` | Cookie if present | `{}` | `200 {"status":"ok"}`; revokes supplied session and clears cookie. |
| `GET /api/v1/session` | Public | None | `{"authenticated":true}` or false. |
| `GET /api/v1/status` | Owner | None | Connection, capacity, committed revision, backend, encrypted snapshot history and client health. |
| `POST /api/v1/backup` | Owner | `{}` | `{"recovery":{...}}` after an encrypted MongoDB safety copy is verified; `503` if not confirmed. |
| GET /api/v1/backup/recovery-key | Owner | None | Private no-store download of the backup decryption key. |
| `GET /health/live` | Public | None | `200 {"status":"ok"}` if HTTP can answer. |
| `GET /health/ready` | Public | None | `200` if runtime/pool ready, otherwise `503`. |
| `GET /healthz` | Public | None | Readiness alias used by Docker. |
| `GET /stream?path=...&auth=...` | Public player shell | Required path, optional share token. | Player HTML; its /file requests enforce authorization. |
| `GET /assets/...`, `GET /static/...`, `GET /third-party/...` | Public | Asset path. | Frontend/player assets, not drive payloads. |

Effective max_file_size is the configured logical-file/staging limit and does
not require Premium. Status includes telegram_ready, demo, max_file_size,
staging_budget_bytes, metadata_revision, backup, clients, restart_required and
admission_paused.

The compatibility-named recovery object includes state:protected|pending|error,
backend:mongodb, metadata_revision, protected_revision, last_success_at,
backup_key_configured and optional error/backup_error. Here protected means the
catalog is committed in MongoDB. An independent safety-copy failure does not
make an acknowledged catalog write uncommitted.

POST /api/v1/backup explicitly takes a complete encrypted snapshot. Ordinary
uploads do not call or wait for it. The recovery-key download is owner-only
and absent from ordinary status/configuration.

The Storage & Backups page additionally accounts for unfinished transfers. Queued/active
uploads remain pending; failed/missing-original uploads need attention.
Client health distinguishes configured accounts from healthy started accounts.

</details>

### Owner connection management

<details>
<summary>Connections, Refresh, saving and Premium login</summary>

All routes require the owner cookie and mutation-origin checks. Secret values
are accepted in request bodies only and are never returned by GET/success/error
snapshots. Storage channel and owner-authentication settings cannot be edited
through these endpoints.

| Method and route | Request | Success |
| --- | --- | --- |
| GET /api/v1/settings | None | Redacted saved configuration, actual running clients, statistics and recovery state. |
| GET /api/v1/settings/uploads | None | Uploads per bot, connected bot count and available configured bot slots. Owner only; no-store. |
| PATCH /api/v1/settings/uploads | uploads_per_bot: integer 1–4 | Save in MongoDB and apply immediately to new Telegram leases. Existing transfers finish; provider waits are preserved. |
| PATCH /api/v1/settings | expected_revision, optional api_id/api_hash/clients | Complete redacted snapshot after isolated verification and atomic save. |
| POST /api/v1/settings/refresh | Empty object | 202 settings snapshot; schedules bounded background saved-credential/channel-permission checks and name refreshes for enabled clients without changing configuration revision. Legacy /refresh-names is an alias. |
| POST /api/v1/settings/premium-login/start | expected_revision, phone, label | challenge_id, stage:code and expires_in_seconds. |
| POST /api/v1/settings/premium-login/verify | challenge_id and exactly one code/password for the current stage | stage:password with the challenge, or stage:complete plus settings snapshot. |
| DELETE /api/v1/settings/premium-login/{challenge_id} | No body | status:cancelled; discards an owned unfinished challenge. |

Connection snapshots may include per-client state in
`connection_check` (queued, checking, waiting, passed, failed), a sanitized
`connection_check_error`, and Unix timestamps
`connection_checked_at` / `connection_retry_at`.
These manual-check results describe the last probe; runtime socket health remains
separate and no extra Telegram work is triggered by reading settings.

- A settings snapshot includes revision, applied_revision, restart_required,
  admission_paused, application_mode:restart, api_id, api_hash_configured,
  storage_channel, clients, active_clients, stats and recovery.
- Saved clients have opaque id, label, kind:bot/user, enabled, configured, connected,
  healthy, premium, active_leases and a sanitized error.
- Read-only display_name, username, identity_refreshing and identity_retry_at describe
  optional cached Telegram names and startup name retrieval.
- The saved label remains a local fallback.
- Polling settings never calls Telegram.
- Manual Refresh checks all enabled saved accounts, including ones whose runtime client
  failed to start.
- It validates credentials and channel permissions in isolated sessions, updates cached
  names, and reports probe health separately from runtime health.
- It does not reconnect the live pool or apply pending credential changes.
  active_clients reports the actual applied pool separately, including clients
  removed/disabled in the saved candidate.
- A newly saved account is not thereby connected; null/false health is not a live
  failure diagnosis before it starts.

The clients array in PATCH is a full desired list. Include an existing id to keep
that identity; omit credential to keep its secret. A new row omits id and requires
credential. Omitted existing rows are removed; omitting clients keeps the entire
list. api_hash omission also preserves its secret. Empty-string replacement is
not the keep-secret protocol.

- Limits: at most 50 managed account rows, at least one enabled bot, labels 1–80 visible trimmed
  characters, credentials 1–8192 trimmed characters without whitespace, API ID
  1–2147483647, and a 32-hex-character API hash.
- Fifty accounts is TGDrive's management bound, not a promise that Telegram
  allows every bot/account combination or scales throughput linearly.
- Storage-bot tokens and imported Telethon sessions receive structural checks.
- Enabled accounts receive read-only Telegram identity/Premium/channel-permission
  preflight through isolated temporary sessions.
- Disabled rows can be saved without authenticating an expired account.
- Duplicate Telegram identities and structurally invalid candidate configurations are
  rejected.

- Saving requires no retained unfinished/error jobs, active receipt reservations or
  unavailable database ownership.
- The current catalog must be committed.
- A narrow repair case permits an error state when its confirmed/protected/current
  revisions match exactly and no recovery conflict/work is pending; that does not turn
  the backup display green.
- Revision races return 409 SETTINGS_CONFLICT, busy state returns 409 SETTINGS_BUSY,
  invalid candidates return 422 SETTINGS_INVALID, and verification outages return 503
  SETTINGS_PREFLIGHT_FAILED.
- No candidate is partially committed after a failed preflight.

Successful save atomically replaces private configuration, then pauses new
catalog/upload mutations with 409 SETTINGS_RESTART_REQUIRED until the service
restarts. Current clients are not hot-swapped; read/media requests, auth,
cancellation and management remain available unless a separate recovery
conflict blocks media. Restart applies the saved revision. The browser must not
automatically retry a mutation with a stale full-client list.

- Premium challenges live only in memory for five minutes, at most two per service, and
  belong to the initiating owner session.
- Codes and two-step passwords are not persisted.
- Current rate limits include six starts per 15 minutes, a per-phone one-minute start
  interval and at most five verification attempts; Telegram FloodWait adds an actual
  Retry-After.
- Expired/foreign challenges return 410 TELEGRAM_LOGIN_EXPIRED.
- Invalid code/password can be retried while the challenge survives; consumed/failed-
  save challenges need a new login.
- Non-Premium accounts cannot be added as Premium.
- A successful authorization is passed directly to the private managed-settings path; no
  StringSession is returned to the browser.
- Cancellation/expiry/unsuccessful post-authentication save revoke an unsaved
  authorization.
- Server restart does not preserve login challenges.

Statistics include active file/folder counts and total_bytes, trashed_files/
trashed_bytes (including files under trashed folders), active_jobs, queued_jobs,
failed_jobs, staging_budget_bytes, staging_reserved_bytes and free_disk_bytes
(nullable). Folders exclude the root. These are catalog/local-resource counts,
not a Telegram quota or a proof that every referenced message is readable.

</details>

### Listing and metadata

<details>
<summary>Folder listings, name search and single-item actions</summary>

| Method and route | Access | Body/query | Success |
| --- | --- | --- | --- |
| `POST /api/getDirectory` | Owner or valid folder share | `{"path":"/","auth":null}` | `{"status":"ok","data":<directory>,"auth_home_path":null}`. |
| `GET /api/v1/nodes` | Owner | `parent_id=/&offset=0&limit=100&sort=name` | `{"items":[...],"total":42,"offset":0}`. |
| `GET /api/v1/search` | Owner only | `q=report&offset=0&limit=100&sort=name` | `{"items":[...],"query":"report","has_more":false,"next_offset":null}`. |
| `POST /api/createNewFolder` | Owner | `{"path":"/","name":"Photos"}` | `{"status":"ok"}`; refresh listing for new ID. |
| `POST /api/renameFileFolder` | Owner | `{"path":"/<id>","name":"New name"}` | `{"status":"ok"}`. |
| `POST /api/trashFileFolder` | Owner | `{"path":"/<id>","trash":true}` | Trash; false restores. Prefer v1 bulk for exposure confirmation. |
| `POST /api/deleteFileFolder` | Owner | `{"path":"/<id>"}` | `202 {"status":"ok","id":"<deletion-job-id>"}`; top-level Trash entry required. |

Special getDirectory paths are `/trash` (owner-only top-level Trash),
`/search_<query>` (owner-only search), and `/share_<folder-id-path>` with `auth`
(shared folder). Shared requests resolve the final ID's current location and
check capability scope. `auth_home_path` supplies the shared-navigation boundary.

- Listed nodes contain id, name, type (folder/file), path, upload_date and size.
- File rows also include part_count (1 for ordinary storage, more for logical parts);
  they do not expose part message IDs or checksums.
- Folder size is stored as zero; listing records do not expose a trashed flag.
- Internal file lookup includes remote metadata.
- Directories have `contents`, an object keyed by child ID.
- Listings do not recursively transfer payloads.

For v1 listing, limit is 1–500, offset is nonnegative, and sort is name/size/type/date.
Folders come first, values ascend, ID breaks ties. It currently materializes
and sorts a folder before slicing; this is not database cursor pagination.

- Search trims/NFC-normalizes q (1–255 characters), uses literal Unicode-casefolded
  partial name matching and excludes every trashed descendant.
- Percent signs, underscores and quotes do not become wildcard/query operators.
- Results include files and folders across the owner's active drive, with parent_names
  containing human-readable ancestor names; path remains the opaque ID ancestry.
- Public share tokens cannot authorize global search.
- Limit is 1–200 (default 100), offset is nonnegative, and sort is name/date/size:
  folders first, then casefolded name ascending, date-added newest first or size largest
  first, with deterministic name/ID ties.
- Search pages have no computed total count.
- The compatibility /search_ directory path is bounded to 500 results; use v1 paging for
  a complete matching listing.

</details>

### Atomic folder preparation

<details>
<summary>Folder-tree request, response and retry rules</summary>

`POST /api/v1/folders/tree` is owner-only. It creates a fresh upload root and
its relative descendants atomically, before any file receipts are started:

~~~json
{
  "destination": "/<destination-id>",
  "root_name": "Project",
  "folders": [["alpha"], ["beta"], ["beta", "deep"]],
  "client_request_id": "<persisted-16-to-128-character-safe-id>"
}
~~~

- The request ID accepts letters, digits, underscore and hyphen; the web client uses a
  UUID.
- Persist it before dispatch and reuse it with the same payload on an uncertain
  response. `folders` excludes the root, includes every ancestor, has at most 999
  entries, and each entry has 1–63 relative name segments.
- The root makes at most 1,000 folders.
- The normal 64 KiB JSON/body guard and the store's 128-level/item limits also apply.
- Extra fields are rejected; name normalization is NFC with normalized sibling collision
  checks.

A `201` response contains `root:<DriveItem>` and
`folders:{"":"/<new-root-id>","alpha":"/<new-root-id>/<alpha-id>",...}`.
The root's `path` remains its parent path; map values are full opaque paths.
Map keys exclude the root name and use NFC names separated by /. The root uses
keep-both naming; descendants are strict. It never silently merges with or
overwrites an existing sibling.

Same request ID/payload returns the same prepared IDs after restart. Changed
payload or a prepared hierarchy that was moved, renamed, trashed, deleted or
otherwise changed returns `409`, `code:FOLDER_TREE_CONFLICT`, rather than
creating a replacement. Adding file contents to the intact prepared folders
does not invalidate replay.

- The API prepares metadata only.
- A successful tree request does not queue file bytes, promise all subsequent file
  uploads succeed, or discover local empty directories.
- The web picker separately limits the selection to 1,000 files, skips empty/over-cap
  files, validates root-prefixed file paths, and then queues each accepted file against
  its mapped destination.
- A cancelled/lost response can leave an empty prepared tree; retry the original
  request, not a new ID.
- The current web dialog holds its preparation plan/key in memory; closing or reloading
  before queuing does not restore that dialog.
- Selecting again creates a new preparation.
- Queued per-file batch metadata is persisted separately.
- Destination ancestor shares apply as with ordinary uploads.

</details>

### Move and bulk actions

<details>
<summary>Request examples, conflicts and partial remote effects</summary>

`POST /api/v1/nodes/move`:

~~~json
{
  "paths": ["/<folder-id>/<file-id>"],
  "destination": "/<destination-folder-id>",
  "conflict": "keep_both",
  "allow_public_exposure": false
}
~~~

Conflict is `keep_both` or `skip`; there is no overwrite option. Results contain
original path, status (moved/skipped), and item where available. Cycles and moves
into oneself are rejected. The destination must be a visible folder.

`POST /api/v1/nodes/bulk`:

~~~json
{
  "paths": ["/<item-id>"],
  "action": "trash",
  "allow_public_exposure": false
}
~~~

Action is trash/restore/delete. Both routes accept 1–1000 paths and collapse
duplicate/overlapping selections. Trash/restore returns `200` with
`results: [{path,status}]`, where status is trashed/restored. Delete returns
`202` with result job IDs and status deleting. Poll retained jobs for completion.
Acceptance is not remote-deletion confirmation. Delete requires Trash entries;
permanent-deletion jobs cannot be cancelled.

Move into a shared destination, or restore into a shared parent, can return
`409` with code `PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED` and a human-readable
detail. After informed owner confirmation, repeat with
`allow_public_exposure:true`. Destination folder shares can grant inherited access.

Move, trash and restore run in one MongoDB transaction per request. A later
invalid item or database failure rolls back earlier changes, revision increments
and share revocations. Public-exposure checks belong to that same transaction.
Skipped move conflicts remain explicit successful results.

Permanent deletion validates the whole selection before scheduling, then uses
independently durable Telegram deletion intents. It cannot make remote effects
atomic across items; retain/poll each accepted job and refresh actual state after
an interrupted response. The older single-item compatibility routes do not
provide the v1 exposure-confirmation flow.

</details>

### Performance, cache inventory and preparation

These are owner-session endpoints; public shares do not grant operator access.
Responses expose bounded operational state rather than credentials or raw
Telegram authorization references. Mutations use the usual same-origin checks.

| Method and route | Purpose |
| --- | --- |
| `GET /api/v1/settings/performance` | Saved live performance preferences, revision, and documentation link. |
| `PATCH /api/v1/settings/performance` | Partial lowercase preferences, with returned revision in `If-Match`; persist and apply to new work. |
| `GET /api/v1/performance` | Account activity, bounded queues/history, request pacing/cooldowns and cache diagnostics. |
| `GET /api/v1/preloads` | Owner cache/preparation inventory, current coverage, and bounded job state. |
| `POST /api/v1/preloads` | `{"path":"/<file-id>"}`; prepare an authorized file within effective capacity. |
| `POST /api/v1/preloads/clear` | `{"file_id":"<inventory-id>"}` evicts one file; `{}` clears cached bytes. |
| `POST /api/v1/preloads/{id}/cancel` | Cancel the named preparation, preserving work other subscribers still need. |

Preference fields use the lowercase names corresponding to the environment
table, such as `cache_disk_bytes`, `telegram_request_limit` and `auto_preload`.
Unknown fields are rejected. Keep and send the returned revision when editing
from multiple tabs; a stale update returns a conflict rather than silently
overwriting another edit.

Inventory reports current ready-byte coverage with half-open intervals
`[start, end)`, including observed ordinary downloads. A ready preparation is
not an indefinite pin. The effective memory limit can be below configuration;
current cache coverage, active reservations and integrity overhead decide
whether a preparation fits.

Queue wait estimates are nullable until recent duration samples exist. They
are explicitly estimates, can change with cooldowns or connection changes,
and do not promise completion time. Queue snapshots are bounded rather than a
permanent telemetry database. Public request-specific queue status must not
expose unrelated visitors' identifiers.

The performance snapshot's `traffic` object counts HTTP body bytes and request
activity on `/file` and resumable-upload endpoints. Sent/received counters
include cached file responses and receipt protocol bodies, and exclude settings
polling/assets. Directional account request counters describe Telegram origin
traffic separately. Both histories are process-local and reset on restart;
derived rates are differences between measured samples.

The preparation queue and ordinary media queues have separate bounds.
Cancelling/clearing a preparation leaves originals intact and does not abort
shared work belonging to other authorized subscribers. See
[cache behavior](performance.md#prepare-a-file-before-sharing).

### Resumable browser receipts

<details>
<summary>Chunk upload protocol and recovery rules</summary>

All receipt routes are owner-only. See [transfers](architecture.md#transfer-pipeline) for persistence
and reconciliation boundaries. This is a TGDrive protocol, not tus compliance.

| Route | Request | Success |
| --- | --- | --- |
| `POST /api/v1/uploads` | `{"name":"example.bin","parent_id":"/","total_size":8388647}` | `201` receipt. |
| `GET /api/v1/uploads/{upload_id}` | None | Full current receipt including chunk checksums. |
| `HEAD /api/v1/uploads/{upload_id}` | None | Upload-Offset and Upload-Length headers. |
| `PATCH /api/v1/uploads/{upload_id}` | Raw bytes, Content-Type application/octet-stream, Upload-Offset header. | `204` and acknowledged new Upload-Offset. |
| `POST /api/v1/uploads/{upload_id}/complete` | `{}` | `202 {"id":"<job-id>","job":{...}}`. |
| `POST /api/v1/uploads/{upload_id}/cancel` | `{}` | Updated receipt; queued work delegates to its job. |

The receipt contains:

~~~json
{
  "id": "<32-lowercase-hex-characters>",
  "name": "example.bin",
  "parent_id": "/",
  "parent_node_id": "root",
  "total_size": 8388647,
  "offset": 8388608,
  "checksums": [
    {"offset": 0, "size": 8388608, "sha256": "<64-hex-digest>"}
  ],
  "chunk_size": 8388608,
  "state": "receiving",
  "expires_at": 1792100000.0
}
~~~

parent_id is the current resolved hierarchy path; additive parent_node_id binds
the stable folder identity. GET/HEAD, append and complete refresh the path after
ancestor moves. New request paths must identify an existing visible mutable
folder. A trashed/deleting/missing folder blocks receipt writes/completion while
retaining acknowledged bytes for restore/cancel.

Create checks the current logical-file limit, folder, staging budget and free disk.
Send exactly chunk_size bytes except for a smaller final chunk. Empty/oversized
chunks and offsets different from the server's are rejected. Wait for each
acknowledgment. After a lost response, GET/HEAD the authoritative offset before
retransmitting. The final chunk journal is verified against staged bytes.

- Call complete only after offset equals total_size.
- Repeated completion returns the existing retained job rather than creating another
  file.
- Then poll its job: a complete browser receipt is not yet a completed protected upload.
- The job commits file metadata and durable transfer state in MongoDB, then reports
  completed and releases staging.
- State values include receiving/queued/cancelling/finished/cancelled.
- Append refreshes receipt expiry; inactive receiving sessions expire after
  UPLOAD_SESSION_TTL_SECONDS.

A re-selected file must match the committed prefix's checksums. Bytes never
received cannot yet be verified. There is no trust-this-checksum override.
Already accepted receipts retain append eligibility after optional account loss.
A persisted part plan retains dispatched sender identities; retry may still need
the recorded sender restored even though a new file could use other bots.

Typical receipt failures: `409` state/offset conflict, `410` expiry/history lost,
`413` size, `415` content type, `422` chunk shape/fields, `429` active receipt
capacity, `507` disk/staging budget. HTTP chunking is the browser receipt layer.
After receipt, the server chooses one Telegram document or ordered file parts
within the logical file limit. Full download and one byte range transparently
reconstruct the selected logical interval.

</details>

### Previews and media

<details>
<summary>Descriptors, format bounds and quality selection</summary>

Both descriptors require the owner cookie or a valid file/folder share
capability. Supply path (an ID path) and optional auth query, just as for /file.

| Route | Response |
| --- | --- |
| GET /api/v1/preview | name, size and kind:text/pdf/video/unsupported. Text includes text, language, truncated and preview_bytes; PDF includes an authorized local URL; unsupported includes a reason. |
| GET /api/v1/media | original descriptor and variants list from the actual stored message. Each descriptor has id, label, is_original, size, mime_type, width, height and duration where applicable. |
| GET /api/v1/file-status | Authorized `path`, opaque `request_id`, optional `auth`; request-specific queue estimate and logical size, without listing other callers. |

- Text/code/JSON/CSV and related allowlisted formats return at most the first 256 KiB of
  source text, with a truncation indicator.
- HTML/XML are displayed as text, not executed.
- The web viewer routes SVG files directly to a passive HTML img element using the
  authorized original /file URL; the bounded descriptor API can still return SVG source
  as XML for callers requesting text.
- UTF-8 (optional BOM) and BOM-marked UTF-16 are supported; invalid encodings/binary-
  looking content get a fallback instead of replacement- decoded source.
- Ambiguous .ts/.mts files return kind:video only after bounded repeated MPEG transport-
  packet evidence, with Telegram MIME used as corroboration rather than proof.
- Otherwise valid TypeScript source stays text; .cts source also maps to TypeScript.
- The video viewer uses the same authorized original /file URL, not a remote URL from
  the file.
- This source-byte limit does not promise only that many internal Telegram transfer
  bytes.

PDF descriptors support files up to 20 MiB and point to /file?preview=1.
The UI renders them using locally bundled PDF.js and its worker, not an external
Office service or active document iframe. Unsupported/oversized files retain
original download. These are implementation limits, not environment settings.

For image/video playback, /file accepts quality=<opaque-descriptor-id>.
A variant ID is bound to that source message and does not grant authorization.
Unknown/unavailable variants return 404. Range/HEAD headers describe the selected
representation. No client may supply arbitrary Telegram document locations.

The original download URL uses download=1. That flag always selects the
original stored representation and attachment disposition, even when quality
was also supplied. A preview choice never replaces canonical file metadata.

Browser uploads remain byte-preserving documents with force_file=True.
Telegram-native messages may contain actual alternate video documents or photo
sizes; only discovered choices are exposed. A native photo's original means its
best stored Telegram representation, not proof of pre-upload camera bytes.
There is no guaranteed transcode request, invented resolution list, companion
message or locally generated derivative.

</details>

### Jobs

<details>
<summary>Progress, retry, cancellation and retention</summary>

All routes below require the owner.

| Route | Request | Success |
| --- | --- | --- |
| `GET /api/v1/jobs` | None | `{"jobs":[...]}`. |
| `GET /api/v1/jobs/{job_id}` | None | One job. |
| `POST /api/v1/jobs/{job_id}/cancel` | `{}` | Updated job; remote reconciliation may keep cancellation pending. |
| `POST /api/v1/jobs/{job_id}/retry` | `{}` | Updated/restarted job; invalid transitions return `409`. |

- A v1 job contains id/status/phase/current/total/error and
  name/kind/destination/retry_at.
- It also includes created_at, updated_at, started_at, finished_at, progress_updated_at
  (Unix seconds), elapsed_seconds, speed_bytes_per_second and eta_seconds.
- `queue` adds request-specific origin admission state and nullable wait estimates;
  it is not a reserved start time or a multi-stage completion deadline.
- The UI does not show an elapsed clock for wholly queued uploads.
- Rate/ETA are nullable current-phase estimates, not total multi-stage completion
  promises; paused/non-byte/deletion/stale progress has no invented rate.
- Samples are phase-local and stale after 15 seconds without progress.
- Job retry_at is the effective provider deadline for uploads or database-copy
  publication; the private lineage_retry_at context survives restart.
- A pre-send cancellation can wake a lineage wait.
- Kinds include upload/remote/delete.
- A URL import retains `remote` origin through local staging and Telegram
  upload, including after a restart. A URL job is not a browser file receipt.
- Statuses are queued, running, cancelling, completed, error, cancelled.
- Phase gives detail such as uploading, finalizing, retry_wait, waiting_for_telegram or
  cleanup_pending; clients must tolerate new phases.
- The compatibility phase backup_pending is short MongoDB commit finalization, not a
  wait for an encrypted Telegram safety copy.
- The job retains its stable operation identity across an interrupted commit/journal
  handoff.

Live job records are limited by retention/capacity. Unresolved send/cleanup
effects are protected from ordinary eviction. Do not treat this as a permanent
audit log. Retry retains operation identities. See [transfers](architecture.md#transfer-pipeline).

</details>

### Transfer history

<details>
<summary>Owner-only history, status filters and cursor paging</summary>

`GET /api/v1/transfers/history` accepts:

| Query | Value |
| --- | --- |
| `status` | `all` (default), `queued`, `running`, `completed`, `error`, `cancelled` |
| `limit` | 1–200; default 50 |
| `before` and `before_id` | Both cursor values from the previous page, or neither |

The response contains `jobs`, `has_more`, `next_before` and `next_before_id`.
Rows use public job fields plus `retained`: whether the live job still exists.
`running` includes cancelling work; deletion jobs are excluded.

Pages sort by creation time and ID descending. Live progress replaces duplicate
archived IDs. Reading old history never restores a job to the active queue.
The response requires owner authentication and uses `Cache-Control: no-store`.

Resolved jobs evicted from the live registry become compact summaries. MongoDB
keeps these in archived `jobs` documents, without the private recovery context;
startup excludes them from live registry loading. These summaries are included
in complete encrypted database backups. They have no automatic history TTL.
Existing records are visible; history already deleted before this feature
cannot be reconstructed.

The floating panel's **Clear finished** only dismisses browser display rows.
It does not erase server history or stored files. Browser-only queued work is
not a durable server history record until a job exists.
The floating panel only displays browser-initiated receipts; the global history
continues to show server jobs initiated on other devices.

</details>

### Shares, downloads, and ranges

<details>
<summary>Share tokens and exact download/range behavior</summary>

| Route | Access | Request | Success |
| --- | --- | --- | --- |
| `GET /api/v1/shares` | Owner | None | `{"shares":[...]}` includes active and expired entries, IDs, current path/name/type/size, folder_path/folder_label, created_at/expires_at and copyable. No bearer tokens. |
| `POST /api/v1/shares` | Owner | `{"path":"/<id>","expires_in_days":7}` | `201 {"auth":"<bearer-token>","path":"...","expires_at":"<ISO-date>"}`. |
| `POST /api/v1/shares/link` | Owner | `{"path":"/<id>"}` | Existing active link auth/path/type/node_id; no rotation. `409` if unrecoverable, `410` if expired/revoked. No-store response. |
| `POST /api/v1/download-links` | Owner | `{"path":"/<id>"}` | Signed attachment `url`, canonical `path`, and `expires_at`; reuse an active copyable file share or create one expiring in one day. Visible in Shared Links; no-store response. |
| `DELETE /api/v1/shares` | Owner | `{"path":"/<id>"}` | `{"status":"ok"}`. |
| `POST /api/getFolderShareAuth` | Owner | `{"path":"/<folder-id>"}` | Compatibility creation: status and auth. |
| `POST /api/getFileShareAuth` | Owner | `{"path":"/<file-id>"}` | Same, for a file. |
| `GET /file`, `HEAD /file` | Owner or scoped capability | Required path, optional auth/Range; quality selects a discovered preview, preview=1 enables the supported PDF path, download=1 forces the original attachment. | Bytes/headers or access/not-found error. |

Share expiry accepts 1, 7 (default), 30 days, or null for no expiry. Creating a
replacement rotates the old token for that node. New capabilities are HMAC-SHA256
of a random share ID under the persistent SESSION_SECRET, with the
`tgdrive/share-link/v1` domain separator. Only the capability hash is stored
in the catalog; the owner-only copy endpoint reconstructs it and checks that
the stored hash matches. Listing never includes the token. Preserve SESSION_SECRET
to retain re-copy capability across installations; changing it does not invalidate
previously issued links, but those links then require their original URL or explicit
replacement. Legacy random tokens cannot be reconstructed and are never silently
rotated. Store list_shares defaults to active entries for access/exposure checks;
the owner listing opts into expired entries. Trash revokes subtree shares; explicit
complete MongoDB restore revokes owner browser sessions and preserves share scope/expiry.

File URL: `/file?path=<encoded-id-path>&auth=<encoded-token>`.
Generated folder UI URL: `/?path=%2Fshare_<folder-id>&auth=<encoded-token>`.
The UI creates links from the stable folder ID alone; nested shared browsing
can use ID paths beneath that root.
Authorization resolves stable final IDs after a move; inherited folder access
depends on the new location.

An owner-only file URL without `auth` needs the owner session cookie. Copying
it into another browser or a download tool does not make it public. The owner's
**Copy download link** action explicitly creates or reuses a share capability;
a newly created link lasts one day and is visible/revocable in Shared Links.

When `auth` is supplied, it must be valid for the requested scope even if an
owner cookie is also present. Warm caches do not bypass expiry or revocation.
Active shared streams check access again before future blocks; already sent
or browser-buffered bytes cannot be recalled.

- Downloads support full `200` and a single `206` range: `bytes=0-1023`, `bytes=1024-`,
  `bytes=-1024`.
- Valid ends beyond EOF are clamped.
- Malformed/unsupported multiple ranges return `400`.
- Unsatisfiable ranges return `416` with `Content-Range: bytes */<size>`.
- HEAD has the corresponding headers and no body.
- Multipart ranges and conditional ETag/If-Range are not implemented.
- The stream semaphore spans the full response lifetime.
- Active document types are attachments; selected images/audio/video may render inline.

</details>

### URL imports and older transfer routes

<details>
<summary>Current compatibility endpoints and their limits</summary>

All these POST routes are owner-only.

| Route | JSON body | Result |
| --- | --- | --- |
| `/api/getFileInfoFromUrl` | `{"url":"https://example.org/file.bin"}` | status/data with file_name, file_size, content_type; size -1 means unknown. |
| `/api/startFileDownloadFromUrl` | `{"url":"https://example.org/file.bin","path":"/","filename":null,"singleThreaded":true,"client_request_id":"<unique-safe-id>"}` | status/id. |
| `/api/getTransferByRequest` | `{"client_request_id":"..."}` | status/id/data compatibility start snapshot. |
| `/api/getTransferProgress` | `{"id":"<job-id>"}` | status/data base job snapshot. |
| `/api/getSaveProgress` | Same | Progress alias. |
| `/api/getUploadProgress` | Same | Progress alias. |
| `/api/getFileDownloadProgress` | Same | Progress alias. |
| `/api/cancelUpload` | Either id or client_request_id, never both. | status/id/data cancellation snapshot. |

singleThreaded is retained for compatibility; HTTP download uses one controlled
stream. It is not a parallel download switch. Optional client_request_id is
16–128 letters/digits/underscore/hyphen. Reusing it for a different request fails
with `409`. This bounded compatibility mapping is **process memory**, not the
restart-durable receipt ID protocol. Prefer job-ID APIs after job creation.

`POST /api/upload` accepts multipart fields file and path, optionally
client_request_id. It is owner-authenticated and bounded, but receives a whole
multipart request rather than the durable offset/checksum receipt protocol.

</details>

### Errors and retry discipline

<details>
<summary>HTTP statuses and safe retry actions</summary>

There is no universal versioned error envelope today. Drive/remote errors often
have status/detail; HTTP/auth errors use detail; receipt errors add
`code:"UPLOAD_ERROR"`. Exposure confirmation, prepared-tree conflicts and
committed-upload cancellation have explicit codes
(`PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED`, `FOLDER_TREE_CONFLICT`, `BACKUP_PENDING`,
`RECOVERY_REQUIRED`). Do not parse
every error phrase as a stable enum.

| Status | Meaning/action |
| --- | --- |
| 400 | Invalid path/range/action or blocked URL; correct input. |
| 401 | Missing/expired owner session or wrong password; log in. |
| 403 | Invalid/out-of-scope share or foreign-origin mutation. |
| 404 | Missing node, job, receipt, or Telegram payload. |
| 409 | Conflict/pending state/exposure confirmation; inspect current state. |
| 410 | Receipt or retained history expired. |
| 413 | Body/file/chunk too large. |
| 415 | Wrong chunk content type. |
| 422 | Schema, empty file, or chunk-shape error. |
| 429 | Login throttling/receipt capacity; observe Retry-After where supplied. |
| 502 | Remote-source/network/protocol failure. |
| 503 | Runtime not ready or compatibility request capacity full. |
| 507 | Insufficient staging budget/free space. |

After a bulk timeout, refresh before replay. After a chunk timeout, read the
server offset. After uncertain completion, reattach to the same receipt/job.
For permanent deletion, retry its retained job rather than submitting another
intent. Do not log cookies, share queries, or signed remote URLs.

Implementation: [app.py](../tgdrive/app.py), [api_v1.py](../tgdrive/api_v1.py),
[compatibility schemas](../tgdrive/schemas.py),
[frontend client](../website/src/api.ts). Update schemas, client types, this
reference and relevant tests together when a contract changes. Proposed routes
belong in [the roadmap](roadmap.md), not in this implemented API reference.

</details>

## Security and privacy

| Boundary | What to know |
| --- | --- |
| Owner access | One owner; no public signup, teams or tenant isolation |
| Shared Links | Anyone with a valid scoped link can read its contents |
| Encryption | Database backups are encrypted; original files and local staging are not encrypted by TGDrive |
| Original file safety | A database record cannot recreate a deleted Telegram file |
| Previews | Uploaded HTML/code is not executed; no external Office service receives files |
| Malware | TGDrive does not scan files for malware |
| Secrets | Keep database URIs, bot tokens, sessions, keys and share links private |
| Public access | Use HTTPS and a restricted/trusted reverse proxy |

<details>
<summary>Authentication, browser policy, networking and backup details</summary>

One owner controls the host and Telegram credentials. There is no registration,
tenant isolation, RBAC, OIDC or API-key system. Channel storage is not
application-managed end-to-end encryption; local staging and original payloads
are not encrypted by TGDrive. Protect disks and backups appropriately.

Use a generated Argon2id hash and independent signing secret. Cookies are
HttpOnly, SameSite=Strict and Secure under HTTPS, with a signed nonce and
server-side token-hash tracking. Logout revokes its record. Password changes
alone do not revoke all cookies; after a credential incident, stop, rotate the
hash and SESSION_SECRET, restart and verify old sessions fail. Full old-volume
restore can roll authentication back.

Mutations check browser Origin/Fetch Metadata; private routes authenticate
before body processing. JSON is capped at 64 KiB and receipt PATCH at 8 MiB.
Telegram admission and shared memory budgets bound media work. These are not distributed abuse protection
or per-share quotas. Login limits are process-local.

Shares are bearer credentials stored as hashes, with scoped access, rotation,
expiry and revocation. Folder shares grant accessible descendants; Trash revokes subtree shares. Complete MongoDB restore preserves share scope/expiry
and revokes owner browser sessions. Downloaded copies cannot be retracted.
The v1/UI exposure-confirmation flow is absent from older compatibility mutation
routes.

- Names are normalized metadata, not arbitrary paths.
- React renders text; assets are local with CSP/nosniff/frame/referrer restrictions.
- Scripts and style elements load from this origin.
- The enhanced viewer allows WebAssembly compilation with wasm-unsafe-eval, blob
  workers/media and inline style attributes for its trusted component templates.
- A worker-compatible data: connect source supports trusted decoder JPEG-frame/poster
  conversion; it is not a remote host allowlist.
- The policy does not enable arbitrary JavaScript eval or inline scripts.
- Viewer code/fonts stay local.
- Selected image/audio/video can be inline; active document types are sandboxed
  attachments.
- There is no malware scanning.

- Supported previews use a full-window native dialog with owned browser history, scroll
  locking and focus restoration.
- Unsupported formats use a compact file-details dialog with their reason/type/size/date
  and original download; content is not executed to manufacture a preview.
- PDF pages scroll continuously with a bounded shared rendering budget, selectable text,
  and zoom/fit controls within the same header (an options popover on phones); the
  viewer does not execute annotations, forms or embedded scripts.
- A failed PDF viewer chunk keeps Close and original download usable.

- The enhanced video viewer lazily loads Movi Player 0.4.1 and its separate locally
  served WASM asset.
- It demuxes locally and attempts hardware video decoding with software fallback; EAC3
  audio and subtitle handling use the bundled codec stack.
- The 32 MiB source-buffer setting is applied by Movi after its initial load.
- Initial demux/ prebuffer reads and separate decoder/cache allocations can exceed that
  setting; it is not a total browser-memory cap or a guarantee of buffered seconds or
  realtime decoding on every device.
- All video previews use it by default; audio/subtitle selection stays in its own menus.
- The custom player remains visible and operable while opening/seeking; there is
  no native-browser compatibility fallback or separate Start Playback gate.
- Buffer feedback uses measured received bytes/rate and the engine's approximate
  buffered time ranges, including a loaded-section timeline. Server preload byte
  coverage is separate from device buffering and decode readiness.
- Original download remains available for unsupported codecs.
  [Viewer notices and source access](../website/public/third-party/README.txt) are served under /third-party; the source/relink archive is a manual
  download, not an initial-page dependency.

Remote requests validate public addresses and redirects, reject URL credentials,
request identity encoding, disable environment proxy inheritance, and bound
time/bytes/disk. ALLOW_PRIVATE_REMOTE_URLS deliberately broadens reachability.
Signed source URLs can persist in private job records.

- Complete MongoDB safety copies contain credentials and are encrypted with AES-256-GCM
  before they reach disk/Telegram.
- The separately retained recovery key is required for restore.
- Local upload staging and original Telegram file contents are not encrypted by this
  snapshot feature.
- Telegram is not an application-managed end-to-end encrypted file store.
- A catalog/snapshot cannot recover original Telegram documents that have been removed
  or become inaccessible.
- Restore revokes browser sessions; ordinary restart preserves legitimate registrations
  and share scopes.

Sessions, hashes, secrets, metadata, share URLs and staged bytes are sensitive.
Redaction is conservative; disable raw query/cookie logging in proxies as well.
Never publish .env, StringSessions, session DBs or unreviewed traces.
Confirmed Telegram deletion establishes adapter-visible absence, not physical
erasure from provider backups or copies held by recipients.

Use [operations](operations.md) for HTTPS, incident preservation and recovery;
remaining gaps are in [roadmap](roadmap.md).

</details>

Live job snapshots include telegram_parts {total, stored} for multipart uploads.
The stored count uses confirmed Telegram messages, independently of current byte
progress, which includes in-flight part uploads. Private send identities and the
multipart recovery journal are never included in this summary.
