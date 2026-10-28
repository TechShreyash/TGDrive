# Configuration and API reference

[Docs home](README.md) · [Settings guide](configuration.md) · [Development](development.md)

Use this page to look up exact settings and API contracts. For website tasks,
use the [user guide](usage.md); for settings you can change on screen, use
[Configuration](configuration.md). Expand a section below when you need its details.

| I need to… | Look up |
| --- | --- |
| Set deployment values | [Configuration](#configuration) · [File syntax and secrets](#file-syntax-and-secret-handling) |
| Understand an unfamiliar term | [Glossary](#terms-used-here) |
| Write an API client | [Authentication and paths](#conventions-and-authentication) · [Errors and retries](#errors-and-retry-discipline) |
| Upload files or folders | [Resumable uploads](#resumable-browser-receipts) · [Folder preparation](#atomic-folder-preparation) |
| Read, share or download | [Listings](#listing-and-metadata) · [Previews](#previews-and-media) · [Shares and ranges](#shares-downloads-and-ranges) |
| Monitor server work | [Jobs](#jobs) · [History](#transfer-history) · [Performance](#performance-cache-inventory-and-preparation) |
| Back up or restore | [Recovery API](#independent-channel-backup-and-recovery) · [Website recovery steps](backup-recovery.md) |
| Review access and privacy | [Security boundaries](#security-and-privacy) |

<details>
<summary>Configuration and API sections</summary>

- [Configuration](#configuration)
  - [File syntax and secret handling](#file-syntax-and-secret-handling)
  - [Telegram accounts](#telegram-accounts)
  - [Performance and cache settings](#performance-and-cache-settings)
  - [Owner authentication](#owner-authentication)
  - [Persistence and initialization](#persistence-and-initialization)
  - [File capacity, staging, and work limits](#file-capacity-staging-and-work-limits)
  - [Remote HTTP(S) uploads](#remote-https-uploads)
  - [Deployment variables outside application Settings](#deployment-variables-outside-application-settings)
- [HTTP API](#http-api)
  - [Conventions and authentication](#conventions-and-authentication)
  - [Session, configuration, health, and assets](#session-configuration-health-and-assets)
  - [Independent channel backup and recovery](#independent-channel-backup-and-recovery)
  - [Owner connection management](#owner-connection-management)
  - [Listing and metadata](#listing-and-metadata)
  - [Atomic folder preparation](#atomic-folder-preparation)
  - [Move and bulk actions](#move-and-bulk-actions)
  - [Performance, cache inventory and preparation](#performance-cache-inventory-and-preparation)
  - [Resumable browser receipts](#resumable-browser-receipts)
  - [Archive jobs](#archive-jobs)
  - [Previews and media](#previews-and-media)
  - [Jobs](#jobs)
  - [Transfer history](#transfer-history)
  - [Starred items](#starred-items)
  - [Shares, downloads, and ranges](#shares-downloads-and-ranges)
  - [URL imports and transfer routes](#url-imports-and-transfer-routes)
  - [Torrent imports](#torrent-imports)
  - [Errors and retry discipline](#errors-and-retry-discipline)

</details>

## Terms used here

| Term | Meaning |
| --- | --- |
| Catalog / metadata | Records describing your files and folders; not their contents |
| Receipt | The server's record of upload bytes already received |
| Staging | Temporary server disk holding unfinished upload bytes |
| Bootstrap settings | Saved startup values used to reopen an existing drive |
| Origin request | A request to Telegram for data not already available in cache |
| Byte range | A requested section of a file, addressed by byte positions |
| Manifest | The ordered list of Telegram parts making up one large file |
| Lease / ownership | A temporary lock that keeps two installations from writing at once |
| Share capability | A secret link/token that grants access to specific content |
| Preload | A disposable cached copy prepared for faster access |
| Restore point / checkpoint | A confirmed backup state that recovery can return to |
| Idempotent request | Retrying the same request does not create a second result |

## Configuration

Start with `.env.example` and [Setup](setup.md). Add optional overrides only when
needed. Names are case-insensitive; **unknown names are ignored**, so check spelling.

| Source | When it applies |
| --- | --- |
| Deployment environment / `.env` | Startup values; host environment wins over `.env` |
| Saved MongoDB bootstrap | Fills missing startup values; explicit deployment values take precedence over local recovery fallback |
| Saved **Connections** | Overrides original storage-account credentials after restart |
| Saved dashboard performance preferences | Overrides matching startup defaults and applies to new work immediately |

Recreate Docker containers after changing their environment; a process restart
alone does not load it. Recreating does not erase saved dashboard preferences.

Compose loads `.env` through `env_file`; its `environment` entries override matching
values. The supplied files fix container paths, cookies and proxy policy; the local
override permits localhost HTTP. [Docker environment details](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).

| Deployment flag | Default | Behavior |
| --- | --- | --- |
| `DEMO_MODE` | false | Give the usual password restricted visitor access; protect Demo Files and lock management writes/key exports. A separately configured demo administrator can bypass those restrictions. Requires container recreation/redeploy. See [public demo mode](configuration.md#public-demo-mode). |

> [!IMPORTANT]
> Keep real credentials private. Example placeholders are not working credentials.

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
- ADMIN_PASSWORD= requires a valid explicit hash or the original saved hash
  loaded from an existing MongoDB drive.
- STRING_SESSIONS= disables optional Premium sessions. Legacy storage-only
  setups can omit MAIN_BOT_TOKEN. Blank/omitted bot-interface credentials can
  reuse values saved in MongoDB, so blanks do not disable a configured interface.
  A main bot token requires TELEGRAM_ADMIN_IDS; storage still needs a working bot.
- MAX_FILE_SIZE= means the bounded default logical-file limit.
- With standard Compose auto-secrets enabled, blank SESSION_SECRET and
  MONGODB_BACKUP_KEY reuse original saved keys, or generate them for a new drive.

Invalid settings fail before serving the application. Secret wrappers and
hidden validation inputs reduce accidental disclosure in exception text; they
do not make a configuration file safe to publish.

</details>

### Telegram accounts

<details>
<summary>API credentials, bot tokens and optional accounts</summary>

| Variable | Required/default | Validation and behavior |
| --- | --- | --- |
| `API_ID` | Required unless supplied by saved settings | Positive integer Telegram application ID; the web editor bounds it to 1–2147483647. |
| `API_HASH` | Required unless supplied by saved settings, secret | Exactly 32 hexadecimal characters; surrounding whitespace is removed. |
| `MAIN_BOT_TOKEN` | Required by the new-drive setup, secret | One bot token for the Telegram chat interface; requires TELEGRAM_ADMIN_IDS. On initial setup, it also supplies the first storage bot when no legacy BOT_TOKENS list is provided. Tokens must match the expected numeric-ID and secret format; Telegram verifies actual validity on connection. |
| `BOT_TOKENS` | Legacy bootstrap, secret | Existing comma-separated storage bot lists remain supported; duplicates are rejected. An explicit list is preserved rather than replaced by MAIN_BOT_TOKEN. For new deployments, use MAIN_BOT_TOKEN and add further storage bots through Connections. |
| `STORAGE_CHANNEL` | Required | Negative integer private channel ID. Confirmed catalogs bind to this channel; changing it is not relocation and can trigger RECOVERY_REQUIRED. Use the membership/permissions from [Telegram setup](setup.md#2-prepare-telegram). |
| `STRING_SESSIONS` | Empty by default, secret | Comma-separated Telethon StringSession credentials for optional Premium users; duplicates are rejected. Sessions must already be authorized and identify Premium user accounts. |
| `PUBLIC_URL` | Omitted/empty by default | Optional HTTP(S) website origin for the bot's `/set_folder` link. Otherwise the website address is remembered when saving its bot upload folder. |
| `TELEGRAM_ADMIN_IDS` | Required with MAIN_BOT_TOKEN | Comma-separated positive numeric Telegram user IDs allowed to use the bot in private chat; usernames are not accepted. [Find your ID](setup.md#find-your-telegram-user-id). |

After initialization, saved **Connections** are authoritative for storage
accounts. Changing MAIN_BOT_TOKEN does not replace their saved credentials; it
changes the chat interface's token. If one bot serves both roles and its token
changes, update both places. Existing BOT_TOKENS deployments can continue without
enabling the chat interface.
For legacy Compose setup, keep BOT_TOKENS in the private `.env` file; a shell
export alone is not passed to the container.

Storage bots are the required baseline. The running pool can remain ready with
a healthy storage bot when additional bots or Premium sessions fail.
Premium capability is checked from healthy, started clients with confirmed
connectivity, not merely a nonempty STRING_SESSIONS value. See
[Telegram behavior](setup.md#additional-bots-and-premium-accounts) for account selection and recovery limits.

</details>

### Performance and cache settings

<details>
<summary>Live preferences, defaults and cache limits</summary>

Saved dashboard preferences override startup defaults, persist in MongoDB and
apply to new work immediately. Environment changes require a restart; recreating
a container does not override saved preferences. Telegram credential edits use
the separate restart-based connection editor.

**Discard changes** restores the last saved values. **Restore defaults** creates a
category-scoped draft; Save validates and applies it. Units are binary:
1 GiB = 1024 MiB. Where a default shows units, use the parenthesized integer in `.env`.

| Variable | Default | Meaning and bounds |
| --- | --- | --- |
| `CACHE_MEMORY_BYTES` | 256 MiB (`268435456`) | RAM payload budget, 0–8 GiB (default 256 MiB). Zero disables RAM retention. Available host/container memory can lower the effective limit. RAM blocks expire after 10 idle minutes; reuse refreshes expiry. |
| `CACHE_DISK_BYTES` | 10 GiB (`10737418240`) | Disk budget, 0–2 TiB. Zero disables disk retention. It does not reserve that amount of disk in advance. |
| `CACHE_MIN_FREE_BYTES` | 1 GiB (`1073741824`) | Free-disk floor for cache writes, 0–2 TiB (default 1 GiB). This does not allocate space or constrain other disk users. Upload staging separately uses `MIN_FREE_BYTES`. |
| `CACHE_TTL_SECONDS` | 24 hours (`86400`) | Sliding disk retention, 60–2678400 seconds. Eviction or disk failure can remove bytes sooner. |
| `AUTO_PRELOAD` | true | Enable extra bounded warming after delivered original audio/video playback bytes. Explicit downloads and unclassified range clients stay within requested ranges. Disabling it leaves adaptive response read-ahead and manual **Preload file** available. |
| `PRELOAD_MAX_JOBS` | 2 | Combined manual and automatic preparation jobs, 1–8; foreground work has priority. More jobs share existing account capacity. |
| `TELEGRAM_UPLOAD_PIPELINE` | 4 | Outstanding upload-part requests per document, 1–16. |
| `TELEGRAM_REQUEST_LIMIT` | 8 | Shared per-account media/metadata request concurrency, 1–32. |
| `TELEGRAM_PARTS_PER_SECOND` | 48 | Application request pacing ceiling, 1–128, including scheduled metadata work. This is not a Telegram-provided bandwidth limit. |
| `TELEGRAM_MEDIA_PER_BOT` | 3 | Shared concurrent origin media operations per account, 1–16. Uploads also retain their separate per-bot/user lane limits. |
| `TELEGRAM_MAX_QUEUE` | 256 | Global queued origin operations, 1–4096; overflow returns retry guidance. |
| `UPLOAD_SPLIT_MIB` | 2000 | Default and maximum document size for every account, including Premium. Explicit 1–2000 values choose a smaller preferred cap; 0 also uses 2000 MiB. The minimum grows when needed to stay within 4096 parts, without exceeding 2000 MiB. Persisted plans keep their original boundaries. |
| `TIMEZONE` | Asia/Kolkata | Installation-wide display timezone for owner and public dates/share expiry. Use a valid IANA name; the default is IST. |

RAM includes the asynchronous disk-write backlog. Active responses have a separate
32 MiB transient payload budget, reduced by available memory with a 2 MiB floor;
transport/server buffers are outside these budgets. `cache.scratch` reports current,
effective and configured bytes, peak reservations, waiting heads and active streams.
RAM and disk may duplicate blocks, so their sum is not whole-file capacity.

Preload admission counts complete aligned Telegram blocks, multipart boundaries,
a 32-byte disk integrity prefix per block and competing reservations. Retention
never guarantees a pin: TTL, eviction and disk failure still apply.

Dashboard saves reject enabled disk caching with a free-space floor below 64 MiB
(`422`); a zero disk budget remains valid. Existing environment values are not
rewritten. Read-only defaults cannot be submitted as a preference.

See [performance policy](performance.md), including
[automatic read-ahead](performance.md#automatic-read-ahead), for scheduling and
warming thresholds. Settings do not guarantee bandwidth or free disk.

</details>

### Owner authentication

<details>
<summary>Passwords, sessions and cookies</summary>

| Variable | Required/default | Validation and behavior |
| --- | --- | --- |
| `ADMIN_PASSWORD_HASH` | None by default, secret | Optional existing Argon2id owner credential; memory cost 19,456–262,144 KiB, time cost 2–10, at most 16 lanes. Otherwise set `ADMIN_PASSWORD`; TGDrive hashes it internally. |
| `ADMIN_PASSWORD` | Empty by default, secret | Plaintext fallback when no hash is configured. At least 12 characters after trimming. If a hash is present, clear the example plaintext value. A malformed nonempty plaintext value still fails validation. |
| `DEMO_ADMIN_PASSWORD` | Empty by default, secret | Optional different password granting full access only while `DEMO_MODE=true`. At least 12 characters after trimming. Not stored/restored in bootstrap preferences. |
| `DEMO_ADMIN_PASSWORD_HASH` | None by default, secret | Optional Argon2id alternative to `DEMO_ADMIN_PASSWORD`, with the same validation bounds as `ADMIN_PASSWORD_HASH`. Takes precedence over demo administrator plaintext. Not stored/restored in bootstrap preferences. |
| `SESSION_SECRET` | Auto-generated in standard Compose; otherwise required, secret | Independent random signing secret of at least 32 characters after trimming. Do not reuse an account or password credential. |
| `SESSION_TTL_SECONDS` | 24 hours (`86400`) | Integer 300-2592000 inclusive: absolute browser session lifetime in seconds (24 hours by default). No separate idle timeout. |
| `COOKIE_SECURE` | true | Secure cookies require HTTPS. Local Compose sets false for loopback HTTP. Disabling it does not enable full remote HTTP support; see [HTTP requirements](operations.md#can-tgdrive-run-over-http). |

At least one password representation is required. The hash takes precedence
when both valid forms are configured. The plaintext fallback is hashed in
memory when the application is created; the .env value itself remains
plaintext.

During initial setup MongoDB's `settings` collection stores a bootstrap document
(`_id: "bootstrap"`) containing `ADMIN_PASSWORD_HASH`, never the plaintext
`ADMIN_PASSWORD`. A missing explicit owner credential can reuse that saved hash.
The private `DATA_DIR/replica-bootstrap.json` recovery file also retains the
owner hash so recovery-only login remains possible. Encrypted database backups
can contain those saved credentials. These copies do not reveal the original
password; plaintext remains readable wherever you configured it. Demo
administrator credentials stay in deployment configuration, outside these
bootstrap records. See [reset instructions](operations.md#forgotten-login-password).

Blank or whitespace-only plaintext values are canonicalized to empty and
require a valid explicit or saved bootstrap hash. Nonempty plaintext values must meet the minimum after
trimming.

Use a unique password or passphrase of **15+ characters**; the implementation's
minimum remains 12. There is no built-in MFA or passkey login. The stronger
length recommendation follows [OWASP authentication guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
for password-only authentication.

The owner password is not a Telegram password. Browser session signatures derive
from SESSION_SECRET and the effective configured owner credential, and sessions
are tracked in MongoDB's sessions collection. Loading a changed password/hash or
signing secret invalidates existing owner cookies; an unchanged configuration
preserves sessions across restarts. Share links are separate credentials. See
[rotation and access recovery](operations.md#https-and-private-configuration).

Each successful login creates an independent session. Different devices and
browser profiles may use the same owner credentials concurrently; a later login
does not replace earlier sessions. Logout revokes only the presented session.
Tabs sharing a browser cookie jar share that session. Demo mode adds a signed
`demo_admin` capability only for the separate full-access password. Legacy and
ordinary-password cookies remain restricted in demo mode; clients cannot add
this capability to their cookies. Its signature derives from a separate key
bound to the demo administrator credential and `SESSION_SECRET`. Rotating or
removing the demo administrator password invalidates elevated sessions without
invalidating ordinary sessions. Outside demo mode the extra password is ignored.

Password verification stays limited to one concurrent check. A simultaneous
login can receive `429` with code `LOGIN_BUSY`; the website retries that response
up to three total attempts, waiting 1–2 seconds between tries. Incorrect passwords,
the five-failure throttle and network errors are not automatically retried.

For lost owner access, follow [password reset](operations.md#forgotten-login-password).
For lost drive data, follow [backup recovery](backup-recovery.md).

</details>

### Persistence and initialization

<details>
<summary>MongoDB, backup keys and local paths</summary>

| Variable | Default | Validation and behavior |
| --- | --- | --- |
| `MONGODB_URI` | Required | Secret URI beginning mongodb:// or mongodb+srv://. Missing/invalid configuration or failed database access rejects startup; no local database fallback. |
| `MONGODB_DATABASE` | tgdrive | 1–63 ASCII letters/digits, underscores or hyphens; dedicated to one drive. |
| `MONGODB_BACKUP_KEY` | None | Initial secret 64-hex-character AES-256 recovery key, independent of the login password. Unified files retain an existing journal key; preserve matching keys for older generations. |
| `MONGODB_BACKUP_SECONDS` | 1 hour (`3600`) | Integer 60–604800 seconds between successful automatic standalone safety copies. Separate standalone publication pauses while independent channel protection is enabled. |
| `DATA_DIR` | data | Persistent generated keys, Telegram sessions, helper locks, recovery/activation state and media cache. MongoDB holds the live catalog and durable job/receipt records. |
| `CACHE_DIR` | cache | Persistent unfinished upload payloads and encrypted temporary database backup files. Keep unfinished bytes. |
| `LOG_LEVEL` | INFO | DEBUG, INFO, WARNING, ERROR or CRITICAL. |

DATA_DIR and CACHE_DIR must be writable distinct paths, not a filesystem
root or account home, equal or nested. Compose sets /app/data and /app/cache.
The container runs as UID/GID 10001:10001; custom bind mounts must allow that
account to write. Do not replace missing writable volumes with disposable mounts.

Standard Compose enables `TGDRIVE_AUTO_SECRETS=true`. Missing
`SESSION_SECRET` and `MONGODB_BACKUP_KEY` values are generated once for a new
drive and saved as mode 0600 in `DATA_DIR/deployment-secrets.json`. Explicit
values and existing MongoDB bootstrap settings take precedence. Existing drives
with missing keys fail rather than silently receiving replacement keys.

Unified v2 backups retain the recovery key already recorded in the replica
journal, including after independent protection is disabled. Changing the login
password or this environment value does not re-encrypt existing files. Use
**Save recovery key** for the key matching the downloadable recovery file;
legacy MongoDB archives can still require their original separate key.

The storage_channel_id binding is a nonzero signed 64-bit integer. Switching
channels cannot silently reinterpret message IDs. Use a MongoDB replica set;
the application uses transactions, majority writes and bounded request/pool
settings. Startup initializes only a pristine database under the ownership lock.
Existing records, saved settings or recovery state prevent automatic replacement
of a missing catalog. Configuration, connection, authentication and transaction
failures stop startup. Archive restore refuses a used drive; it accepts only an
empty database or the untouched catalog created by automatic first setup.

</details>

### File capacity, staging, and work limits

<details>
<summary>Exact sizes, staging reservations and concurrency</summary>

| Variable | Default | Validation and behavior |
| --- | --- | --- |
| `MAX_FILE_SIZE` | Unset | Optional positive logical-file byte ceiling; the runtime upload policy supports up to 2 TiB, further bounded by staging. Files above one Telegram document's limit do not require Premium. |
| `STAGING_BUDGET_BYTES` | 16 GiB (`17179869184`) | Positive integer, at most 2 TiB in the runtime upload policy; 16 GiB total reservation budget by default. |
| `MIN_FREE_BYTES` | 1 GiB (`1073741824`) | Nonnegative integer; preserve 1 GiB free space by default. Browser receipt admission reserves remaining bytes; URL downloads also check free space. |
| `UPLOAD_SESSION_TTL_SECONDS` | 86400 | Integer at least 60; incomplete receipt idle lifetime in seconds, refreshed after each committed chunk. There is no declared upper bound in configuration. |
| `MAX_CONCURRENT_TRANSFERS` | 2 | Integer 1–32; bounds local fingerprinting and receive work. Torrent downloads and receipt handlers each use a separate bound of this size, so slow peers do not occupy local upload-preparation slots. It does not cap Telegram uploads across bots. |
| `TELEGRAM_UPLOADS_PER_BOT` | 1 | Integer 1–4; initial per-bot Telegram media limit. **Configuration → Transfer limits** saves an override in MongoDB and applies it immediately. Premium clients remain limited to one media upload each. |
| `TRANSFER_TTL_SECONDS` | 3600 | Integer 60-86400 inclusive; terminal jobs become eligible for cleanup on admission or explicit pruning. Compact server-transfer history is kept separately; files are unaffected. |

New documents use at most **2000 MiB (2,097,152,000 bytes)** for every account,
including Premium. `UPLOAD_SPLIT_MIB=0` uses that cap. Larger logical files use
ordered parts and appear as one item with original downloads and range seeking.
The `/api/config` logical limit is the minimum of `STAGING_BUDGET_BYTES`, optional
`MAX_FILE_SIZE` and 2000 MiB x 4096 parts: **16 GiB by default**, subject to disk.
The multipart format's 4096-part ceiling is separate from the current **2 TiB**
upload-policy maximum; it does not make larger upload settings valid.
TGDrive has no aggregate stored-byte quota; its catalog is bounded to 250,000
file/folder entries, including the root.
Premium is preferred when eligible; bot fallback and existing sender identities
remain supported.

Browser chunks are 8 MiB except the last. At most 1,000 unfinished receipts and
2,000 rows in unfinished browser batches are tracked. Finished browser history
keeps at most 30 tasks/2,000 rows, retaining unresolved tasks. Browser bounds
are per controller; staging and Telegram account limits apply across callers.
Allow disk headroom for URL work, logs, metadata and backups beyond receipt
reservations.

Device receives start at two concurrent files and adapt between one and four.
Each accepted file queues immediately. Staging/account waits still apply; reducing
Telegram concurrency lets active uploads finish. See the canonical
[transfer pipeline](architecture.md#transfer-pipeline) for adaptation, admission,
retry and cancellation rules.

</details>

### Remote HTTP(S) uploads

<details>
<summary>URL fetching, timeouts and network settings</summary>

| Variable | Default | Validation and behavior |
| --- | --- | --- |
| `ALLOW_PRIVATE_REMOTE_URLS` | false | Enables private/internal destinations only when explicitly true. It broadens what the authenticated owner can reach. |
| `REMOTE_DOWNLOAD_CONCURRENCY` | 2 | Integer 1–8; full-speed source downloads. Configuration → Transfer limits saves and immediately applies an override. See [URL intake](performance.md#url-import-intake) for queued connections, pacing and storage bounds. |
| `REMOTE_CONNECT_TIMEOUT` | 20 | Seconds, greater than 0 and at most 300. |
| `REMOTE_READ_TIMEOUT` | 120 | Seconds, greater than 0 and at most 3600; individual read timeout. |
| `REMOTE_TOTAL_TIMEOUT` | 21600 | Seconds, greater than 0 and at most 86400; total operation budget. |
| `REMOTE_MAX_REDIRECTS` | 5 | Integer 0-20 inclusive; manually validated redirect count. |

URL requests validate destinations and redirects, request identity encoding,
and enforce declared and observed sizes. The configured global ceiling still
bounds HTTP retrieval, while runtime admission applies current account
capacity. Telegram flood waits are persisted with a server-provided retry time;
the transport disables Telethon's automatic flood sleeping. Details and limits
are in [transfers](architecture.md#transfer-pipeline) and [security](reference.md#security-and-privacy).

With the default private-network policy, IP literals and every DNS/redirect
destination must be globally routable. Shared/CGNAT addresses (`100.64.0.0/10`),
IPv6 site-local addresses and IPv4-mapped versions of blocked addresses are
rejected alongside private, loopback, link-local, multicast and reserved ranges.

</details>

### Deployment variables outside application Settings

<details>
<summary>Ports, proxy trust and worker settings</summary>

| Variable/control | Shipped behavior |
| --- | --- |
| `BIND_ADDRESS` | Compose interpolation only; defaults to 127.0.0.1 for the host binding. It is not a Settings field. |
| `PORT` | Local Compose's host port defaults to 8000; the supplied container command always listens on internal port 8000. |
| `FORWARDED_ALLOW_IPS` | Optional explicit Uvicorn proxy trust override. The container launcher defaults to loopback plus the private IPs resolved from `TGDRIVE_PROXY_HOSTS`; the hosted Compose file sets that hostname to `coolify-proxy`. Unset or `auto` enables discovery. |
| `TGDRIVE_PROXY_HOSTS` | Deployment-owned, comma-separated internal proxy hostnames. Only their resolved private IPs are trusted. A DNS failure retains loopback trust and logs an actionable warning; it never enables wildcard trust. |
| `TGDRIVE_AUTO_SECRETS` | Standard Compose sets true in the container environment. Generates missing session/backup keys once for a new drive in persistent DATA_DIR. Existing drives must reuse their original keys. |
| `TGDRIVE_RESTART_POLICY` | Compose interpolation only. Laptop override defaults to no, hosted/server mode to unless-stopped. Choose deliberately; restart policies do not restart a running container merely because it is unhealthy. |
| --workers | Must remain 1. This is a launch argument, not an environment feature for scaling the drive. |

`BIND_ADDRESS` only affects the local override. The server override always binds
its published port to 127.0.0.1; Coolify's base Compose publishes no host port.
`COOKIE_SECURE=true`, the internal data/cache paths, automatic first initialization and
the single-worker launcher are enforced by the hosted Compose definitions.

Compose forwards `FORWARDED_ALLOW_IPS` to the container. Trust only the reverse
proxy addresses the app actually sees; keep direct access to the app port restricted.

Apply settings using [operations](operations.md), then diagnose errors using
[troubleshooting](operations.md#troubleshooting). The [development chapter](development.md)
owns the version compatibility baseline.

</details>

## Owner administration

Configure the environment, deploy and sign in. First initialization is automatic;
TGDrive has no maintenance CLI or terminal setup wizard. Use **Connections** for
Telegram login and repairs, **Transfers** for retries and cancellations, and
**Storage & Backups** for protection and archive/channel recovery. Deployment
credentials and service restarts remain in Docker Compose or Coolify; failures
that prevent startup are explained in container logs. See
[recovery](backup-recovery.md) and [password reset](operations.md#forgotten-login-password).

## HTTP API

These endpoints serve the web app. Owner requests use a login cookie; shared
requests use a token scoped to selected content. Read the conventions before
integrating. There are no API keys, per-person accounts or interactive OpenAPI pages.

### Conventions and authentication

<details>
<summary>Cookies, ID paths, bodies and timestamps</summary>

Routes are relative to your TGDrive website address. Use JSON with
`Content-Type: application/json` unless a route specifies raw bytes or multipart data.
The React client uses both versioned `/api/v1` routes and application routes such
as `/api/getDirectory`. Both are active v2 interfaces.

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
| `GET /embed?path=...&auth=...` | Public embed shell | File ID path or `/share_<folder-id>` and share token used by the viewer. | Embeddable React file viewer/folder browser; data requests enforce capability access. |
| `GET /api/config` | Public | None | Includes `status`, `max_file_size` (default 16 GiB logical limit), `timezone`, `recovery_only` and `demo_mode` (default false). |
| `POST /api/checkPassword` | Public, throttled | `{"pass":"<owner-password>"}` | `200 {"status":"ok"}` plus session cookie. |
| `POST /api/logout` | Cookie if present | `{}` | `200 {"status":"ok"}`; revokes an active owner session and clears the cookie. Missing/invalid/expired/revoked cookies do not trigger a session database write. |
| `GET /api/v1/session` | Public | None | `{"authenticated":true}` or false. |
| `GET /api/v1/status` | Owner | None | Connection, capacity, committed revision, backend, latest locally observed encrypted snapshot record and client health. The backup observation reloads at startup and updates during publication; this polling endpoint does not query backup history. |
| `POST /api/v1/backup` | Owner | `{}` | Creates a confirmed unified recovery file; runs the independent checkpoint when enabled, otherwise a standalone main-channel snapshot. Returns recovery status; `503` if not confirmed. |
| `GET /api/v1/backup/recovery-key` | Owner | None | Private no-store download of the backup decryption key. |
| `POST /api/v1/backup/restore` | Owner, disabled in demo | Legacy `TGDRIVE-MONGO-1` archive bytes, `Content-Type: application/octet-stream`, `X-TGDrive-Recovery-Key: <64 hex characters>`, `X-TGDrive-Confirm: RESTORE` | Restores a legacy full database archive into an empty or untouched automatically created drive; returns `restart_required: true`. |
| `GET /health/live` | Public | None | `200 {"status":"ok"}` if HTTP can answer. |
| `GET /health/ready` | Public | None | `200` if runtime/pool ready, otherwise `503`. |
| `GET /healthz` | Public | None | Readiness alias used by Docker. |
| `GET /stream?path=...&auth=...` | Public player shell | Required path, optional share token. | Player HTML; its /file requests enforce authorization. |
| `GET /assets/...`, `GET /static/...`, `GET /third-party/...` | Public | Asset path. | Frontend/player assets, not drive payloads. |

Effective max_file_size is the configured logical-file/staging limit and does
not require Premium. Status includes telegram_ready, demo, max_file_size,
staging_budget_bytes, metadata_revision, backup, clients, restart_required and
admission_paused.

With `DEMO_MODE=true`, restricted visitor requests that mutate `/api/v1/settings`
or its subroutes, `/api/v1/replica` or its subroutes, and `POST /api/v1/backup`
return `403` with `code: "DEMO_MODE_READ_ONLY"`. This includes connection refresh
and Premium login/cancellation. `GET /api/v1/backup/recovery-key` and
`GET /api/v1/replica/key` and `GET /api/v1/replica/file` return the same denial
to prevent secret export.
Status/settings reads and the secret-free replica report remain available under
normal authentication. The status field `demo` identifies a synthetic test
runtime; it is separate from the deployment's `demo_mode` flag. Authenticated
full-access demo sessions can use management and key exports normally.

`GET /api/config` includes `demo_read_only` and `demo_admin`, computed from the
current signed cookie. `GET /api/v1/session` includes these permission fields
and `demo_mode` during demo mode, alongside `authenticated`. Both responses
use `Cache-Control: no-store` so changing access cannot reuse another session's
permissions. No response contains either password or password hash.

Demo Files has the reserved catalog ID `tgdrive-demo-files`. Restricted writes
to it, its descendants, or ancestors whose move/delete would affect it return
`403 DEMO_FILES_READ_ONLY`. Upload destinations, share changes and durable job
retry/cancel are checked before side effects; catalog writes also check ancestry
under the store transaction. Restricted jobs keep their policy in the durable
context as `demo_read_only: true`, reapplied on resume only while demo mode is
enabled. Full-access retries can explicitly authorize privileged work.

The compatibility-named recovery object includes state:protected|pending|error,
backend:mongodb, metadata_revision, protected_revision, last_success_at,
backup_key_configured and optional error/backup_error. Here protected means the
catalog is committed in MongoDB. An independent safety-copy failure does not
make an acknowledged catalog write uncommitted.

POST /api/v1/backup explicitly requests a complete encrypted recovery point,
bypassing the independent worker's batching delay but respecting provider retry
deadlines. Ordinary uploads do not call or wait for it. Recovery file/key
downloads are owner-only and absent from ordinary status/configuration.

This legacy restore route accepts at most 1 GiB of ciphertext and 1 GiB expanded
data. A unified `TGDRIVE-BACKUP-2` file returns `422` with
`RECOVERY_FILE_REQUIRES_UNIFIED_RESTORE`; use the [recovery-file flow](#independent-channel-backup-and-recovery)
to verify channel access and file mappings before activation.
It validates the archive before pausing work, then rechecks the destination
inside the restore transaction. Existing files, jobs, shares or saved settings
changes reject replacement with `409 RESTORE_DESTINATION_NOT_EMPTY`. Browser
sessions are revoked after success. A failure after pausing returns
`503 DATABASE_RESTORE_UNCONFIRMED` and requires a service restart; the website
keeps those instructions visible. This route is available in owner recovery
mode and is excluded from the ordinary 64 KiB JSON-body limit. See
[website recovery steps](backup-recovery.md#restore-a-lost-database-from-telegram).

The Storage & Backups page additionally accounts for unfinished transfers. Queued/active
uploads remain pending; failed/missing-original uploads need attention.
Client health distinguishes configured accounts from healthy started accounts.

</details>

### Independent channel backup and recovery

<details>
<summary>Channel copy, recovery and activation endpoints</summary>

The recovery-file routes also work without a second channel. Optional independent
protection is configured through Storage & Backups; credentials are never put in
URLs. See the [operator guide](backup-recovery.md) for the file/key workflow,
channel preparation and recovery limits.

| Route | Request | Result |
| --- | --- | --- |
| `GET /api/v1/replica` | None | Configuration, phase, channel IDs, revisions, progress, retry guidance, `backup_file_available`, `file_restore_allowed`, bounded activity and optional recovery candidate including `has_database` and `source_is_backup`. File restore is unavailable in demo mode. No bot tokens or key. |
| `POST /api/v1/replica/configure` | `channel_id`, write-only `bot_token`, `independent_accounts: true` | Verifies private distinct channels and a separate backup bot, saves configuration, schedules initial copying. Requires an existing catalog. |
| `POST /api/v1/replica/action` | `action`: `sync`, `pause`, `resume`, `retry` or `disable` | Durable operation control. Resume also re-enables configured protection. |
| `GET /api/v1/replica/key` | None | Private no-store recovery-key JSON. `key` matches the current downloadable file; legacy matching keys/restore-point references remain available for older formats. Save separately from the file. |
| `GET /api/v1/replica/file` | None | Private no-store `tgdrive-backup.tgdb` download of retained confirmed local ciphertext; `404` until a confirmed file is available. Status reads do not download from Telegram. |
| `POST /api/v1/replica/import` | Raw file bytes; `Content-Type: application/octet-stream`, `X-TGDrive-Recovery-Key: <64 hex characters>`; optional `X-TGDrive-Source-Channel` and `X-TGDrive-Bot-Token` | Authenticates and stages the recovery file, tries recorded channels/access and returns a candidate. Overrides repair revoked credentials or select a survivor. Inspection does not activate a drive. Disabled in demo mode. |
| `GET /api/v1/replica/report` | None | Secret-free owner status/report attachment; file/channel names and IDs may still be private. |
| `POST /api/v1/replica/recover` | `source_channel`, write-only `bot_token`, 64-hex `key`, optional positive `message_id` | Compatibility path: downloads/authenticates a pinned recovery copy and records a candidate. |
| `POST /api/v1/replica/recover` | `target_channel`, write-only `target_bot_token`, matching typed `confirm_channel`, optional `catalog_only: true` | Copies the inspected candidate into an empty private destination, or verifies the surviving channel for database-only restoration. A database-only request may omit the target token to reuse verified source access. |
| `POST /api/v1/replica/recover` | `activate: true`, `confirm_channel` matching the pending candidate's destination channel ID | Explicitly activates a completed candidate; service restart is required. Missing/wrong confirmation or incomplete candidates cannot be activated. |

Database connection failures and timeouts report
`error.code: REPLICA_MONGODB_UNAVAILABLE`, phase `waiting` and a Unix-second
`retry_at`. Database access/configuration or ownership failures report
`REPLICA_MONGODB_FAILED`, phase `error` and `retry_at: 0`. Neither advances the
confirmed revision or discards the pending backup. See the
[retry policy](backup-recovery.md#enable-and-confirm-the-first-copy).

All routes require owner authentication and normal same-origin mutation checks.
A recovery-only startup exposes the authenticated recovery surface when the local
catalog cannot open; it must not enable ordinary file mutations against a missing
catalog. Channel IDs are negative integers; source and copy destinations must be
different, except an explicitly requested database-only restore.

New `tgdrive-backup.tgdb` files use `TGDRIVE-BACKUP-2` framing, compressed BSON JSON
and authenticated AES-256-GCM encryption. They hold one database snapshot,
recovery credentials and channel mappings; portable catalogs are derived from
the captured data. A standalone file records main only. Independent protection
adds verified backup mappings to the same artifact. Limits are 64 MiB ciphertext
and 1 GiB expanded data; uploads reject excess data while staging and enforce
the free-space reserve. The import route is outside the ordinary 64 KiB JSON
body limit. Duplicate key/channel/token headers, invalid keys or mappings,
unavailable source documents and wrong sizes block restoration.

The independent worker coalesces changed revisions/settings within 60-second
checkpoint windows. Initial setup, explicit backup/sync and destructive-operation
barriers do not wait for that window; none bypasses saved Telegram waits.
Healthy unchanged points get hourly checks. Browser polling remains read-only.

Full database state is staged with the candidate and validated again during
fenced activation. Restored browser sessions are empty; Telegram user-account
sessions are excluded. Unfinished jobs become canceled history and unsafe
source-channel mutation work is not replayed. Recovery retains the surviving
backup documents and valid independent configuration while rebasing locations to
the verified target. It requires an accessible matching database or an empty
replacement, not access to the previous MongoDB host.

Legacy `tgdrive-replica.tgdb` capsules and `TGDRIVE-MONGO-1` archives remain
readable with their original keys. Pending old-format jobs finish with their
original send identities; they can briefly produce the former two-file pair.
Legacy archives retain their 1 GiB upload/expanded limits and the separate
`/api/v1/backup/restore` compatibility path.

The local `DATA_DIR/channel-backup.sqlite3` journal persists random operation IDs,
confirmed message mappings, configuration and recovery state. Preserve the full
persistent data directory through restarts. Status is confirmed-part progress,
not speculative queued bytes. Confirmed local ciphertext supports downloading
the file. Old remote documents and recovery points are retained; changing the
configured destination does not erase them.

</details>

### Owner connection management

<details>
<summary>Connections, Refresh, saving and Premium login</summary>

Owner cookie and mutation-origin checks apply. Secrets are write-only body fields.
These routes cannot edit the storage channel or owner credentials.

| Method and route | Request | Success |
| --- | --- | --- |
| GET /api/v1/settings | None | Redacted saved configuration, actual running clients, statistics and recovery state. |
| GET /api/v1/settings/uploads | None | Uploads per bot, connected bot count and available configured bot slots. Owner only; no-store. |
| PATCH /api/v1/settings/uploads | uploads_per_bot: integer 1–4 | Save in MongoDB and apply immediately to new Telegram leases. Existing transfers finish; provider waits are preserved. |
| PATCH /api/v1/settings | expected_revision, optional api_id/api_hash/clients | Complete redacted snapshot after isolated verification and atomic save. |
| POST /api/v1/settings/refresh | Empty object | 202 settings snapshot; schedules bounded background saved-credential/channel-permission checks and name refreshes for enabled clients without changing configuration revision. |
| POST /api/v1/settings/premium-login/start | expected_revision, phone, label | challenge_id, stage:code and expires_in_seconds. |
| POST /api/v1/settings/premium-login/verify | challenge_id and exactly one code/password for the current stage | stage:password with the challenge, or stage:complete plus settings snapshot. |
| DELETE /api/v1/settings/premium-login/{challenge_id} | No body | status:cancelled; discards an owned unfinished challenge. |

**Snapshots.** Settings include `revision`, `applied_revision`, `restart_required`,
`admission_paused`, `application_mode:restart`, API credential presence,
`storage_channel`, `clients`, `active_clients`, `stats` and `recovery`. Client rows
include opaque ID, label, bot/user kind, enabled/configured/connected/healthy/
Premium state, active leases and sanitized errors. `active_clients` describes
the applied pool, including accounts removed from the saved candidate.

`connection_check` is queued/checking/waiting/passed/failed, with optional error
and Unix `connection_checked_at`/`connection_retry_at`. Cached `display_name`,
`username`, `identity_refreshing` and `identity_retry_at` are read-only; labels
remain the fallback. Reading settings never calls Telegram. Refresh probes saved
enabled accounts in isolated sessions, including failed starters; it does not
apply pending credentials or reconnect the pool. Probe and runtime health differ.
Account errors include `TELEGRAM_CREDENTIALS_REVOKED`, `TELEGRAM_CHANNEL_ACCESS`
and `TELEGRAM_ACCOUNT_REMOVED`; operation failures return `503` with detail/code.
Owner status exposes `telegram_issues`.

**Saving.** `clients` is the full desired list: retain an existing `id`, omit its
`credential` to keep the secret, and omit removed rows. New rows omit `id` and
require a credential. Omitting `clients` or `api_hash` preserves that whole value;
an empty credential is not a keep-secret marker.

Limits: 50 managed rows, at least one enabled bot, trimmed visible labels 1-80
characters, credentials 1-8192 characters without whitespace, API ID 1-2147483647,
and a 32-hex API hash. Enabled accounts undergo identity/Premium/permission
preflight; disabled rows need not authenticate. Duplicate identities fail.
Management capacity does not imply linear Telegram throughput.

Membership/API changes need settled jobs/receipts, valid database ownership and
a committed catalog. Same-account credential repair may preserve pending work
if API credentials, client IDs/order, kinds and enabled states stay unchanged;
each replacement must verify its original identity. A narrow error-state repair
requires exactly matching confirmed/protected/current revisions and no recovery
conflict. Failed preflight never partially saves.

Errors: `409 SETTINGS_CONFLICT` for revision races, `409 SETTINGS_BUSY` for blocked
changes, `422 SETTINGS_INVALID`, `503 SETTINGS_PREFLIGHT_FAILED`. A successful
save pauses new catalog/upload mutations with `409 SETTINGS_RESTART_REQUIRED`
until restart. Reads, authentication, cancellation and management remain available
unless recovery separately blocks them. Do not replay a stale full client list.

**Premium login.** Owner-bound in-memory challenges last five minutes, at most
two per service. Limits are six starts/15 minutes, one start/phone/minute and five
verification attempts; FloodWait supplies `Retry-After`. Foreign/expired challenges
return `410 TELEGRAM_LOGIN_EXPIRED`. Retry invalid codes/passwords while the
challenge lives; consumed/failed-save challenges need a new login. Non-Premium
accounts fail. Codes/passwords are not saved, and StringSessions never reach the
browser. Cancellation, expiry and failed post-auth save revoke unsaved
authorizations; restart loses challenges.

`stats` includes active/trashed file counts and bytes, folders excluding root,
active/queued/failed jobs, staging budget/reservations and nullable free disk.
These are catalog/resource counts, not Telegram quota or proof of readable files.

</details>

### Listing and metadata

<details>
<summary>Folder listings, name search and single-item actions</summary>

| Method and route | Access | Body/query | Success |
| --- | --- | --- | --- |
| `POST /api/getDirectory` | Owner or valid folder share | `{"path":"/","auth":null}` | `{"status":"ok","data":<directory>,"auth_home_path":null}`. |
| `GET /api/v1/nodes` | Owner | `parent_id=/&offset=0&limit=100&sort=name` | `{"items":[...],"total":42,"offset":0}`. |
| `GET /api/v1/trash` | Owner only | `q=report` (optional, up to 255 characters) | `{"data":{"contents":{...}}}`; top-level Trash items include `parent_names` for their source folder; pending deletions are hidden, failed deletions reappear; never includes live files. |
| `GET /api/v1/search` | Owner only | `q=report&offset=0&limit=100&sort=name&order=asc` | `{"items":[...],"query":"report","has_more":false,"next_offset":null}`. |
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
- Limit is 1–200 (default 100), offset is nonnegative, and sort is name/date/size.
- Optional `order=asc|desc` sets the primary direction for every sort field. Omitting
  it preserves the original defaults: name ascending, date-added newest first, size
  largest first. Folders always come first; equal primary values use ascending
  casefolded name and ID ties.
- The catalog sorts the full matching snapshot before offset/limit
  pagination, so changing direction affects the global result order.
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
Acceptance is not database-removal confirmation. Delete requires Trash entries;
permanent-deletion jobs cannot be cancelled.

Move into a shared destination, or restore into a shared parent, can return
`409` with code `PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED` and a human-readable
detail. After informed owner confirmation, repeat with
`allow_public_exposure:true`. Destination folder shares can grant inherited access.

Move, trash and restore run in one MongoDB transaction per request. A later
invalid item or database failure rolls back earlier changes, revision increments
and share revocations. Public-exposure checks belong to that same transaction.
Skipped move conflicts remain explicit successful results.

Permanent deletion validates the whole selection before scheduling, then journals
independently durable catalog-removal intents. Accepted pending roots disappear
from Trash listings, including `/api/getDirectory` with `/trash`; failures
reappear for retry. One backup protection check covers the bulk request before
its newly started jobs remove records; individual retries and restart recovery
check protection again. Each job removes its database
subtree and shares atomically, retaining original Telegram messages for every
file age. Completed intents no longer retain message references. This is TGDrive's
catalog-only deletion policy; see
[permanent deletion](usage.md#why-permanent-deletion-keeps-the-telegram-files).
Retain/poll each accepted job and refresh actual state after an interrupted
response; selected items do not complete atomically as one batch. Retried deletion jobs also use catalog-only removal. The single-item
routes do not provide the `/api/v1` exposure-confirmation flow.

</details>

### Performance, cache inventory and preparation

<details>
<summary>Performance preferences, telemetry and preload endpoints</summary>

These are owner-session endpoints; public shares do not grant operator access.
Responses expose bounded operational state rather than credentials or raw
Telegram authorization references. Mutations use the usual same-origin checks.

| Method and route | Purpose |
| --- | --- |
| `GET /api/v1/settings/performance` | Saved live performance preferences, revision, read-only built-in defaults, and configuration guide link. |
| `PATCH /api/v1/settings/performance` | Partial lowercase preferences, with returned revision in `If-Match`; persist and apply to new work. |
| `GET /api/v1/performance` | Account activity, bounded live queues/history, request pacing/cooldowns and cache diagnostics. |
| `GET /api/v1/performance/history?start=UNIX&end=UNIX` | Owner-only measured history within the last 31 days and persisted lifetime traffic totals; at most 360 bins. |
| `GET /api/v1/preloads` | Owner cache/preparation inventory, current coverage, and bounded job state. |
| `POST /api/v1/preloads` | `{"path":"/<file-id>"}`; prepare an authorized file within effective capacity. |
| `POST /api/v1/preloads/clear` | `{"file_id":"<inventory-id>"}` evicts one file; `{}` clears cached bytes. |
| `POST /api/v1/preloads/{id}/cancel` | Cancel the named preparation, preserving work other subscribers still need. |

Preference fields use the lowercase names corresponding to the environment
table, such as `cache_disk_bytes`, `telegram_request_limit` and `auto_preload`.
Unknown fields are rejected. Keep and send the returned revision when editing
from multiple tabs; a stale update returns a conflict rather than silently
overwriting another edit.

The same performance endpoint saves upload-storage fields: `max_file_size`,
`staging_budget_bytes` and `min_free_bytes`. File size and temporary budget are
integers from 1 byte through 2 TiB; a save requires the budget to cover the file
maximum and all current upload reservations. Saving an upload-storage change
also requires a free-space reserve of at least 64 MiB. These fields are separate
from `/api/v1/settings/uploads`, which only changes uploads per bot. See the
[upload-storage controls](configuration.md#upload-size-and-available-disk-space).

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
The application samples both histories every three seconds without dashboard
polling. Histories are capped at 120 entries and five minutes, and reset after a
gap longer than 30 seconds or a backward clock jump. `traffic` exposes nullable
`download_bytes_per_second` and `upload_bytes_per_second`; an initial missing
baseline is not a measured zero. `server.process_rss_bytes` reports current Linux
process resident memory when available; `process_peak_rss_bytes` remains a peak.
`server.cpu.percent` is nullable current cgroup CPU utilization, normalized to
available cores/quota, or labelled host busy CPU when cgroup counters are unavailable.
`server.network` exposes the selected default interface, monitor-session
`received_bytes`/`sent_bytes`, and nullable per-second rates. These include URL
imports and Telegram traffic that are outside the narrower `traffic` HTTP counters.
Linux interface counters include protocol overhead; they are not per-file accounting.
`server.history` retains measured timestamped CPU percentage/scope, current cgroup
memory/limit, interface receive/send rates and interface name. It follows the same
three-second/five-minute bounds. Unknown readings are null; an unlimited cgroup
memory limit is null rather than zero. The dashboard splits plots at missing
samples or measurement-scope changes and supports keyboard/touch sample inspection.

The historical endpoint is separate from those live process histories. It validates
finite Unix-second `start < end`, a past interval no older/wider than 31 days,
and returns `422` for invalid bounds or a redacted retryable `503` if storage is
unavailable. It returns actual minute-aligned boundary intervals: `start`, `end`,
`bucket_seconds`, `retention_seconds`, `earliest_at`, `tracked_since`,
`last_updated`, `available`, `samples`, and `totals`. Samples include `end_at`
and per-metric `coverage_seconds`. Rates/gauges are weighted by valid observed
seconds; null gaps are not counted as zero. Mixed interfaces or CPU scopes do
not become a false continuous series. Long ranges are downsampled, so a bin's
average is not its peak. The UI keeps Now readings separate from interval averages.

Lifetime `totals` keys are `network_received_bytes`, `network_sent_bytes`,
`http_upload_bytes`, `http_download_bytes`, `http_requests`, `http_errors`,
`telegram_upload_bytes`, and `telegram_download_bytes`. Unobserved sources are
null. These overlapping accounting layers must not be summed together. Totals
survive process restarts and 31-day chart pruning, but only describe activity
since `tracked_since`; no pre-installation history can be recovered.
Laptop and hosted deployments use MongoDB `telemetry_minutes`/`telemetry_state`. Minute checkpoints and clean shutdown persist
state; an abrupt crash may lose the latest uncheckpointed statistics. Telemetry
is excluded from encrypted catalog backups and never gates file service.

The preparation queue and ordinary media queues have separate bounds.
Cancelling/clearing a preparation leaves originals intact and does not abort
shared work belonging to other authorized subscribers. See
[cache behavior](performance.md#prepare-a-file-before-sharing).

</details>

<a id="zip-archive-jobs"></a>

### Archive jobs

Archive routes require the owner session; public share links cannot start jobs.
See the [user workflow and supported suffixes](usage.md#create-zips-and-extract-archives).

| Route | JSON request | Result |
| --- | --- | --- |
| `POST /api/v1/archives` | `paths`: selected file/folder paths, `destination`: existing folder path, `name`: ZIP filename | Accepted job identity and snapshot: `{id, job}`. |
| `POST /api/v1/archives/extract` | `path`: supported archive/compressed-file path, `destination`: existing folder path, `folder_name`: new folder name or `null` to extract here | Accepted job identity and snapshot: `{id, job}`. |

Both accept `allow_public_exposure`, default `false`, to acknowledge a publicly
shared destination. Poll and control the returned identity through the existing
[job routes](#jobs). Job kinds are `archive` and `extract`; progress reports bytes
for the current phase. Created ZIPs use the ordinary file download route.

<details>
<summary>Formats, storage and recovery boundaries</summary>

- ZIP creation stores entries without recompression. This prioritizes predictable
  resource use and bundles already compressed media without adding compression
  settings. Folder names and relative structure are retained.
- Extraction accepts ZIP, 7z, RAR/RAR5, TAR and gzip/bzip2/xz/Zstandard-compressed
  TAR or single files. ZIP supports Stored, Deflate, BZIP2 and LZMA entries.
  Encrypted and split-volume archives are unsupported; some RAR features remain
  unsupported by the installed native reader. A job accepts up to 1,000 entries,
  including implicit folders, with at most 64 relative path components,
  4,096 path characters, an 8 MiB ZIP
  directory and a 4 MiB expanded extraction manifest. Extraction validates paths,
  sizes and available archive-provided integrity checks before creating output
  folders or files. TAR has no payload checksum, so it cannot detect every
  same-size content change.
  Absolute/traversal paths, encrypted entries, symlinks/special files, duplicate
  paths and empty regular files are rejected. Empty directories are retained.
  Decoded TAR streams may use the remaining staging budget plus an allowance of
  8 MiB + 1,000 KiB for headers and padding; larger decoded tails are rejected.
  Compressed streams are checked through EOF, and native-reader warnings fail
  the job before catalog publication.
- Temporary workspace shares `STAGING_BUDGET_BYTES` and `MIN_FREE_BYTES` with
  uploads and imports. Creation reserves the ZIP bytes plus headers; extraction
  needs space for the source archive and its expanded files together. Defaults are
  16 GiB of shared staging and a 1 GiB free-disk reserve. Normal logical-file
  upload limits and catalog depth/item limits still apply.
- Source files are read from Telegram, including multipart files, using bounded
  reads directly into the ZIP writer. Extraction runs the native reader in a
  separate worker with a 512 MiB address-space ceiling and 600 seconds of CPU
  time. Large dictionaries or unusually expensive archives can exceed these
  limits even when their output would fit in staging. Cancellation kills and
  reaps the worker before workspace cleanup. Outputs use the normal upload,
  catalog and protection pipeline. Existing files are never overwritten.
- Accepted jobs are server-owned. Retry/restart reuse persisted job identities;
  cancellation does not roll back files already committed to the drive. Preserve
  `CACHE_DIR` across restarts. ZIP creation can regenerate missing bytes before
  its upload plan is recorded; later missing ZIP bytes or uncommitted extracted
  files require cancellation and a new job. Completed children and catalogued
  outputs can finalize without staged bytes. Deleting or trashing a source before
  staging, or the destination before commit, can block further work.
- Failed jobs retain staged bytes and their storage reservation for retry.
  Cancel an unwanted failed job to release that space. Completed/cancelled jobs
  remove temporary workspace after their internal upload workers finish cleanup;
  files already committed to the drive remain.

</details>

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
Folder uploads may also send `folder_upload: {id, name, root_path, created_at}`
on creation. `id` is a stable 16–128 character URL-safe selection identity,
`name` is at most 255 characters, `root_path` identifies the uploaded root, and
`created_at` is finite nonnegative Unix seconds. The destination must belong to
that root. The metadata follows the receipt into job polling and archived history;
ordinary file selections and URL imports remain separate tasks.
Send exactly chunk_size bytes except for a smaller final chunk. Empty/oversized
chunks and offsets different from the server's are rejected. Wait for each
acknowledgment. After a lost response, GET/HEAD the authoritative offset before
retransmitting. The final chunk journal is verified against staged bytes.
An offset mismatch returns HTTP `409` with code `UPLOAD_OFFSET_CHANGED`; clients
may recheck the receipt and resume. Other `409` failures are not offset conflicts.
The browser uses [bounded automatic retries](architecture.md#bounds-and-retries)
for temporary failures after it knows the receipt ID; creation without a returned
ID still requires manual retry.

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
<summary>Preview descriptors, bounds and quality selection</summary>

Owner cookie or scoped file/folder capability required; supply `path` and optional
`auth` as for `/file`.

| Route | Response |
| --- | --- |
| GET /api/v1/books | path, optional auth and entry. Without entry: book index with format, title and ordered entries. With entry: passive EPUB chapter JSON or raster page bytes. See [reader limits](reading.md#reader-limits). |
| GET /api/v1/preview | name, size and kind:text/pdf/video/book/unsupported. Text includes text, language, truncated and preview_bytes; PDF includes an authorized local URL; unsupported includes a reason. |
| GET /api/v1/media | original descriptor and variants list from the actual stored message. Each descriptor has id, label, is_original, size, mime_type, width, height and duration where applicable. |
| GET /api/v1/thumbnail | TGDrive-generated JPEG cover when cached, otherwise a provider JPEG/PNG/WebP thumbnail, at most 128 KiB; optional `If-None-Match` can return `304`. |
| GET /api/v1/poster | Authorized `path` and optional `auth`; stored video-poster JPEG up to 1 MiB, `204` when unavailable, or conditional `304`. Fetches only the referenced image, never the original video. |
| GET /api/v1/file-status | Authorized `path`, opaque `request_id`, optional `auth`; request-specific queue estimate, logical size, phase and retry guidance, without listing other callers. |

`file-status` returns `queue`, `size`, `phase`, `code` and `retry_after`. Phase is
`checking_file`, `waiting_for_telegram`, `receiving`, `retry_wait`, `error`, or
`null` when no current phase is known. The initial file check includes metadata
waits even when there is no origin queue entry. Queue positions count operations
and can change as download requests take turns; they are not a file/user order
or a completion ETA.

A queue-full failure records `retry_wait`, `code: TELEGRAM_QUEUE_FULL` and
remaining `retry_after` seconds, matching the response's `503`/`Retry-After`
guidance. A new GET with that request ID clears the previous failure. Other
errors expose no private exception details. Status is bound to the authorized
file, held in a bounded in-memory registry, and may be absent before the GET,
after eviction or after restart. For overlapping GETs with the same ID, phase
reflects the latest GET while the queue estimate aggregates that ID's operations.

**Posters:** authorize before and after reads, cache hits and `304`, using
`Cache-Control: private, no-cache`, `Vary: Cookie` and an ETag. Missing/invalid
optional images allow the player's on-demand fallback. Temporary poster reads
or checksum failures can return `503 POSTER_BUSY`, `Retry-After: 5`; real file,
share and recovery errors remain distinct. [Poster policy](thumbnails.md#video-posters).

Portable file records may include a `poster` object with `message_id`, `size`,
`sha256` and optional `document_id`, all referring to an image in the file's
storage channel. It is not an original byte part and does not change logical
file size or part count.

**Thumbnails:** authorize before and after asynchronous reads, including cache hits
and `304`. Responses use `Cache-Control: private, no-cache`, `Vary: Cookie` and an
ETag. Generated covers precede provider thumbnails; multipart uses the first part.
The grid never fetches original Telegram payloads. [Cached-cover repair](thumbnails.md#existing-files-and-fallback)
retains its size/time bounds and rechecks access/source identity without changing
Telegram or catalog data. Positive covers are reused; new cached data allows
previous misses to be reconsidered.
`204 No Content` means an optional thumbnail is unavailable and the client should
keep its file icon. Missing originals and authorization failures retain their HTTP
errors. Temporary thumbnail failures return `503 THUMBNAIL_BUSY`, `Retry-After: 5`. See [generation limits](thumbnails.md).

**Thumbnail rebuild:** owner-only `GET /api/v1/settings/thumbnails/rebuild`
returns the latest run. `POST` to that route requires `{"confirmed": true}` and
returns **202** immediately, reusing an active run rather than starting duplicates.
`POST /api/v1/settings/thumbnails/rebuild/cancel` stops it. Public demo sessions
can read status but cannot start/cancel management work. Snapshots include `status`,
`phase`, `total`, `processed`, `generated`, `skipped`, `failed`, `source_bytes`,
current filename/read bytes and up to five recent issues. Byte counts include
cache reads, not measured network traffic. See [rebuild policy](thumbnails.md#rebuild-existing-thumbnails).

**Text:** allowlisted formats return at most 256 KiB with a truncation marker.
HTML/XML stay text. UTF-8 (optional BOM) and BOM-marked UTF-16 are supported;
invalid/binary content falls back to download. SVG uses a passive image in the UI;
the descriptor may expose its XML. Ambiguous `.ts`/`.mts` require repeated MPEG
packet evidence to become video; valid TypeScript and `.cts` remain source text.
The source-byte cap does not bound all internal Telegram transfer bytes.

Book raster pages are checked against the [reader dimension limits](reading.md#reader-limits)
before delivery; an oversized or malformed image returns a preview error without
changing its original download.

**PDF:** at most 256 MiB source, lazy authorized ranges without a cumulative
session-byte cap or automatic whole-file preparation. Local PDF.js disables
streaming/full-file prefetch; closing aborts requests. Normal range-cache and
multipart mapping apply. [Reader limits](reading.md#reader-limits) define request,
buffer and parser budgets for PDF/EPUB/CBZ/CBR; they are not total browser-memory
guarantees. Failed previews retain original download.

**Representations:** `/file?quality=<opaque-descriptor-id>` chooses an actual
message-bound variant; it never grants access. Unknown variants return `404`.
Range/HEAD describe the selected representation. `download=1` always selects the
original attachment, overriding `quality`. Arbitrary Telegram locations are rejected.

Browser uploads remain byte-preserving documents (`force_file=True`). Telegram-native
messages may offer real video variants/photo sizes; a photo's original means its
best stored representation, not guaranteed camera bytes. No invented transcodes
or resolutions are exposed. See [video behavior](usage.md#open-preview-and-download)
and [viewer notices/source](../website/public/third-party/README.txt).

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

`GET /api/v1/jobs` includes all retained records, including terminal results and
`history_cleared` markers. The owner interface adapts its request cadence to
activity and visibility; see [browser checks](performance.md#browser-progress-and-health-checks).

Live job snapshots include `telegram_parts: {total, stored}`. `stored` counts
confirmed messages; byte progress includes in-flight uploads. Sender identities
and the recovery journal remain private.

</details>

### Transfer history

<details>
<summary>Owner-only task pages, filters and indexed cursors</summary>

The Transfers UI uses `POST /api/v1/transfers/history/tasks`:

```json
{"status":"finished","page":1,"limit":20,"q":""}
```

Owner-only `DELETE /api/v1/transfers/history` with JSON
`{"exclude_folders":[]}` clears completed/error/cancelled history across every
page and search. It returns `{"cleared":N}` (individual server records, not
folder task count) and `Cache-Control: no-store`. `exclude_folders` accepts up
to 2,000 folder selection IDs for browser work still waiting to reach the server.
Queued/running jobs and their folder members are always kept. Archived summaries
are deleted; terminal live jobs are hidden without removing recovery contexts.
Deletion jobs and stored files are unaffected. The action is safe to repeat;
already cleared records are not counted again. Retrying/resuming a hidden job
makes it visible again. Live job polling includes `history_cleared:true` so
clients can settle progress and dismiss finished rows. Existing backups may
still contain older history.

The response includes `jobs`, `page`, `page_size`, `total_tasks`, `total_pages`,
`has_more`, `offset` and `next_offset`. `jobs` contains every member of each
selected task, so its row count can exceed `limit`. Only explicit folder uploads
group together; ordinary files and URL imports each count once. Grouping, status
and search matching precede pagination. Status precedence is running, queued,
failed, then cancelled or entirely successful. Matching a folder name or one of
its files returns the whole task. A page beyond the end clamps to the last page.

| Body field | Value |
| --- | --- |
| `status` | Same statuses as the cursor API below; default `all` |
| `page`, `limit` | Page starts at 1; limit 1–200, default 20 |
| `q` | Normalized case-insensitive substring, at most 255 characters |
| `offset` | Optional nonnegative task offset; overrides page slicing for browser-only finished tasks; callers handle page clamping |
| `exclude_folders` | Up to 2,000 selection IDs omitted from server task totals/results while the browser owns the remaining folder work |

Grouping uses saved folder-selection metadata. This owner-only request allows up
to 256 KiB of JSON. Exact task counts scan archive summaries in bounded
batches; see [history storage](architecture.md#finished-history).

`GET /api/v1/transfers/history?page=1&limit=20` also returns numbered task pages,
without browser exclusions. Supplying a page and cursor together is invalid.
Omitting `page` selects the indexed cursor protocol:

`GET /api/v1/transfers/history` accepts:

| Query | Value |
| --- | --- |
| `status` | `all` (default), `queued`, `running`, `finished`, `completed`, `error`, `cancelled`; finished combines completed/error/cancelled |
| `limit` | 1–200; default 50 |
| `q` | Optional file-name substring, up to 255 characters; Unicode-normalized case-insensitive search across live and archived transfers before pagination |
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
in complete encrypted database backups. They have no automatic history TTL;
**Clear history** removes them manually.
Existing records are visible; history already deleted before this feature
cannot be reconstructed.

The floating panel's **Clear finished** dismisses browser rows, preserving server
history and files. Per-job dismissals survive reload and merge across open tabs.
The panel shows this browser's work; history also shows other devices' server jobs.
Browser-only queued work becomes durable server history only after a job exists.
[History controls](usage.md#transfer-history).

Owner-only `POST /api/v1/transfers/location` accepts `transfer_id`, `destination` and `completed`. It resolves the current folder and exact saved item without mutation; missing/trashed items return `available:false`.

</details>

### Starred items

<details>
<summary>Owner stars and move/restore behavior</summary>

| Route | Request | Response |
| --- | --- | --- |
| `GET /api/v1/starred?q=...` | Optional literal filename query | `{data:{contents:{<id>:<item>}}}` with current paths and parent names. |
| `PATCH /api/v1/starred` | `{path:"/<id-path>",starred:true}` (strict boolean) | `{item:<updated item>}`; idempotent when already set. |

Owner-only, no-store. Stars follow node moves/renames, hide with trashed subtrees
and reappear after restore. Unstarring never deletes or moves the original.
Snapshots preserve annotations; the drive root cannot be starred.

</details>

### Shares, downloads, and ranges

<details>
<summary>Share tokens and exact download/range behavior</summary>

| Route | Access | Request | Success |
| --- | --- | --- | --- |
| `GET /api/v1/shares` | Owner | None | `{"shares":[...]}` includes active and expired entries, IDs, current path/name/type/size, folder_path/folder_label, created_at/expires_at, optional label and copyable. No bearer tokens. |
| `POST /api/v1/shares` | Owner | `{"path":"/<id>","label":"Alex","expires_in_days":7}`; optional ISO `expires_at` overrides the days. | `201 {"auth":"<bearer-token>","id":"<share-id>","path":"...","expires_at":"<ISO-date>","label":"Alex"}`. Adds an independent link. |
| `POST /api/v1/shares/link` | Owner | `{"share_id":"<share-id>"}` or `{"path":"/<id>"}` (including `/` for the drive root). | Existing active link auth/path/type/node_id; no rotation. `409` if unrecoverable, `410` if expired/revoked. Path selection returns `409` when multiple links exist. No-store response. |
| `PATCH /api/v1/shares` | Owner | `{"share_id":"<share-id>","label":"Alex","expires_at":"<future ISO-date>"}`; omitted fields stay unchanged; explicit null expiry means never. | Updated link metadata; changes only this link. |
| `POST /api/v1/download-links` | Owner | `{"path":"/<id>"}` | Signed attachment `url`, canonical `path`, and `expires_at`; reuse an active copyable file share labelled `Download link` or create one expiring in one day. Other recipient links are left alone. Visible in Shared Links; no-store response. |
| `DELETE /api/v1/shares` | Owner | `{"share_id":"<share-id>"}` | `{"status":"ok"}`. Revokes one link. Alternate `{"path":"/<id>"}` revokes all direct links for that node; `/` selects the drive root. |
| `POST /api/getFolderShareAuth` | Owner | `{"path":"/<folder-id>"}` | Alternate creation: status and auth. |
| `POST /api/getFileShareAuth` | Owner | `{"path":"/<file-id>"}` | Same, for a file. |
| `GET /api/v1/embed` | Valid scoped capability, including a parent folder share | Required `path` (1–4096 characters) and `auth` (1–512 characters). | File viewer metadata: `{id,name,type:"file",size,upload_date,path:""}`. No parent hierarchy or Telegram storage metadata. |
| `GET /file`, `HEAD /file` | Owner or scoped capability | Required path, optional auth/Range; quality selects a discovered preview, preview=1 marks a preview (including the supported PDF path and original media playback), download=1 forces the original attachment. | Bytes/headers or access/not-found error. |

Share expiry accepts 1–3650 days (default 7), null for no expiry, or a future
ISO `expires_at`. Labels are optional, at most 80 characters, and cannot contain
control characters. Labels are organizational, not recipient authentication.
Each item permits up to 100 independent links; reaching the limit rejects the
new link without evicting existing ones. Link creation also appends.
New capabilities are HMAC-SHA256
of a random share ID under the persistent SESSION_SECRET, with the
`tgdrive/share-link/v1` domain separator. Only the capability hash is stored
in the catalog; the owner-only copy endpoint reconstructs it and checks that
the stored hash matches. Listing never includes the token. Preserve SESSION_SECRET
to retain re-copy capability across installations; changing it does not invalidate
previously issued links, but those links then require their original URL or explicit
creation of a separate new link. Store list_shares defaults to active entries
for access/exposure checks; the owner listing opts into expired entries.
The Shared Links page copies by share ID and never creates a replacement link.
Unavailable/expired links keep a disabled copy control with an explanation. Labels
are shown only when supplied; creation time and expiry distinguish separate rows.
Portable backup schema is 4, with optional share
labels and `starred: true` on bookmarked nodes. Trash
revokes subtree shares; explicit
complete MongoDB restore revokes owner browser sessions and preserves share scope/expiry.

File URL: `/file?path=<encoded-id-path>&auth=<encoded-token>`.
Generated folder UI URL: `/?path=%2Fshare_<folder-id>&auth=<encoded-token>`.
The UI creates links from the stable folder ID alone; nested shared browsing
can use ID paths beneath that root.
Authorization resolves stable final IDs after a move; inherited folder access
depends on the new location.

Embed URLs use `/embed?path=%2F<file-id>&auth=<encoded-token>` for files and
`/embed?path=%2Fshare_<folder-id>&auth=<encoded-token>` for folders. Both copy
actions reuse the selected share capability; no extra share or expiry is created.
The embed entry renders only the public viewer, never the owner interface. File
metadata requires an explicit nonempty token even with an owner cookie; folder
browsing uses the existing shared-directory authorization and navigation boundary.
Preview and download requests continue to enforce their own capability checks.
See [embedding steps and iframe example](usage.md#embed-a-file-or-folder-in-a-website).

An owner-only file URL without `auth` needs the owner session cookie. Copying
it into another browser or a download tool does not make it public. The owner's
**Copy download link** action explicitly creates or reuses a share capability;
a newly created link lasts one day, carries the `Download link` label and is
visible/revocable in Shared Links. It does not reuse a separately labelled recipient link.

When `auth` is supplied, it must be valid for the requested scope even if an
owner cookie is also present. Warm caches do not bypass expiry or revocation.
Active shared streams check access again before future blocks; already sent
or browser-buffered bytes cannot be recalled.

- Downloads support full `200` and a single `206` range: `bytes=0-1023`, `bytes=1024-`,
  `bytes=-1024`.
- Download managers may issue several separate single-range requests in parallel
  or resume only missing intervals. Each response returns exactly its requested
  bytes, including across Telegram document boundaries. Cache hits and shared
  in-flight fills avoid duplicate Telegram payload reads where possible.
- Valid ends beyond EOF are clamped.
- Malformed/unsupported multiple ranges return `400`.
- Unsatisfiable ranges return `416` with `Content-Range: bytes */<size>`.
- HEAD has the corresponding headers and no body.
- Conditional requests support `If-None-Match` and return `304` after access
  checks. `If-Range` requires a matching strong ETag; a stale/weak tag or date
  returns a full `200` response so the client can restart safely. A catalog-only
  HEAD for a single-document file has a weak ETag; use the strong ETag from an
  actual GET when resuming.
- Read-ahead for downloads remains inside the requested range. A cold boundary
  can still fetch an entire aligned Telegram block (up to 1 MiB). These requests
  do not start unrelated background preloads; manual **Preload file** remains
  an explicit whole-file operation.
- Cold origin work uses bounded shared admission; cached delivery still uses
  server bandwidth and requires valid access.
- Active document types are attachments; selected images/audio/video may render inline.

</details>

### URL imports and transfer routes

<details>
<summary>Application endpoints and their limits</summary>

All these POST routes are owner-only.

| Route | JSON body | Result |
| --- | --- | --- |
| `/api/getFileInfoFromUrl` | `{"url":"https://example.org/file.bin"}` | status/data with file_name, file_size, content_type; size -1 means unknown. |
| `/api/startFileDownloadFromUrl` | `{"url":"https://example.org/file.bin","path":"/","filename":null,"singleThreaded":false,"client_request_id":"<unique-safe-id>"}` | status/id. |
| `/api/getTransferByRequest` | `{"client_request_id":"..."}` | status/id/data compatibility start snapshot. |
| `/api/getTransferProgress` | `{"id":"<job-id>"}` | status/data base job snapshot. |
| `/api/cancelUpload` | Either id or client_request_id, never both. | status/id/data cancellation snapshot. |

`singleThreaded` defaults to `false` for adaptive parallel ranges with safe
single-stream fallback. `true` forces one stream. Starting a URL import creates a
job before contacting the source: authentication and destination errors remain
HTTP errors, while DNS/response/size/storage checks run in that job. Its initial
phase is `checking_source`. Temporary failures use `source_retry_wait`,
`retry_at`, `source_attempt` and `source_max_attempts` for a visible automatic retry.
Snapshots may include `download_mode` of `queued`,
`warm`, `full` or `waiting_storage`; queued/waiting intake has no completion ETA.
See [URL intake](performance.md#url-import-intake) for exact bounds.

Optional `client_request_id` is 16–128 letters/digits/underscore/hyphen. Reusing
it for a different URL, destination, filename or connection mode fails with `409`. This bounded compatibility mapping is **process memory**, not the
restart-durable receipt ID protocol. Prefer job-ID APIs after job creation.

`POST /api/upload` accepts multipart fields file and path, optionally
client_request_id. It is owner-authenticated and bounded, but receives a whole
multipart request rather than the durable offset/checksum receipt protocol.

</details>

### Torrent imports

`POST /api/startTorrentDownload` is owner-only and accepts
`{"url":"magnet:?xt=urn:btih:...","path":"/","client_request_id":"<unique-safe-id>"}`.
`url` also accepts a public HTTP(S) `.torrent` URL; authentication and destination
checks precede job creation. It returns `{"status":"ok","id":"<job-id>"}`;
use the existing job progress, cancellation and retry routes. Request IDs use
the same process-local compatibility mapping described above; retain the job ID
for recovery across server restarts. Public snapshots omit the source link.

| Policy | Behavior |
| --- | --- |
| Engine | `aria2c` must be on the server's `PATH`; bundled in Docker. |
| Concurrency | At most `MAX_CONCURRENT_TRANSFERS` torrent engines (default 2), independently of local receive/fingerprinting work; Telegram uploading uses its existing admission. |
| Sources | BitTorrent v1 metadata, including a compatible v1 part in hybrid torrents; v2-only torrents are unsupported. |
| Metadata | At most 8 MiB; magnet discovery times out after 120 seconds. |
| Contents | 1–10,000 nonempty files, at most 1,000 folders including the torrent root, and 64 folder levels; the existing destination depth limit also applies. |
| Paths | Unsafe names, traversal, symlinks, duplicate names and file/folder collisions are rejected before payload downloading. |
| Storage | Each file must fit the upload limit; the entire torrent reserves the shared staging budget and free-space reserve. Failed partial data keeps its storage claim. |
| Peer discovery | IPv4 DHT and public UDP trackers. HTTP(S) trackers, web seeds and magnet auxiliary source URLs are ignored; HTTP-only private torrents cannot import. |
| Network | Local peer discovery is disabled. Peers still see the server IP; peer traffic does not inherit the HTTP downloader's private-address filter. Outbound peer access must suit the host's network policy. |
| Timeout | A torrent with no byte progress for five minutes fails with a retry action. |
| Seeding | Pieces may upload while downloading; seeding stops after download completion. |

The job uses `torrent_metadata`, `downloading`, then `torrent_uploading` phases.
Progress resets between source downloading and Telegram uploading;
`torrent_files: {total, completed}` counts files saved during the latter stage.
One parent task appears in Transfers; its internal upload jobs use the normal
splitting, catalog commit and protection rules.

Validated metadata is cached and pinned to the original info hash for retries.
Interrupted torrent downloads can reuse retained pieces after hash checking;
after downloading, retries use staged files and skip completed uploads. Normal
restart recovery resumes active jobs; failed jobs remain available for **Retry**.
Preserve the cache volume and job journal. Missing staging, unavailable peers,
changed destinations or broken Telegram credentials can still prevent recovery.
Cancellation stops unfinished work and reclaims safe temporary data; files
already catalogued remain in the drive. A folder import can therefore leave a
partially populated folder after failure or cancellation.

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

The shared browser API client has a 60-second deadline, including response-body
reading, even when navigation supplies a cancellation signal. Resource-history
and thumbnail controls use 15 seconds; channel status uses 20 seconds; connection
saves, Premium verification and channel-copy actions allow 120 seconds. A client
timeout does not prove that server work stopped.

After a bulk timeout, refresh before replay. After a chunk timeout, read the
server offset. After uncertain completion, reattach to the same receipt/job.
For permanent deletion, retry its retained job rather than submitting another
intent. Do not log cookies, share queries, or signed remote URLs.

Implementation: [app.py](../tgdrive/web/app.py), [api_v1.py](../tgdrive/web/api_v1.py),
[request schemas](../tgdrive/web/schemas.py),
[frontend client](../website/src/shared/api.ts). Update schemas, client types, this
reference and relevant tests together when a contract changes. Proposed routes
belong in [the roadmap](roadmap.md), not in this implemented API reference.

</details>

## Security and privacy

| Boundary | What to know |
| --- | --- |
| Owner access | One shared drive; demo mode optionally separates visitor and full-access passwords. No built-in MFA/passkeys, public signup, teams or tenant isolation. |
| Shared Links | Anyone with a valid scoped link can read its contents |
| Encryption | Database backups are encrypted; original files and local staging are not encrypted by TGDrive |
| Original file safety | A database record cannot recreate a deleted Telegram file |
| Previews | Uploaded HTML/code is not executed; no external Office service receives files |
| Malware | TGDrive does not scan files for malware |
| Secrets | Keep database URIs, bot tokens, sessions, keys and share links private |
| Public access | Use HTTPS and a restricted/trusted reverse proxy |

<details>
<summary>Authentication, browser policy and data handling</summary>

- **Sessions:** HttpOnly, SameSite=Strict cookies with Secure under HTTPS, signed
  nonces and server-side token-hash tracking. Logout revokes the record; changing
  the effective owner credential or `SESSION_SECRET` invalidates owner cookies
  after restart. Shares use separate credentials. Recovery can roll access back;
  [review credentials after restore](operations.md#https-and-private-configuration).
  Credential rotation does not erase old session rows: restoring the old
  credential and signing key can reactivate their still-unexpired cookies.
  Use a fresh signing secret when earlier sessions must stay invalid.
  Sessions have an absolute 24-hour default lifetime, with no separate idle
  timeout. Sensitive settings and recovery-key export accept an active owner
  session without a fresh password prompt.
- **Requests:** mutations check Origin/Fetch Metadata; private routes authenticate
  before body processing. JSON bodies are capped at 64 KiB, receipt PATCH at
  8 MiB. Login allows five failures per client IP in five minutes and bounds
  expensive password verification to one concurrent check. The throttle resets
  after restart and does not stop distributed attempts. Add login rate limiting
  at a public deployment's trusted proxy; there are no per-share traffic quotas.
- **Shares:** hashed bearer credentials with scope, expiry and revocation. Folder
  links include accessible descendants; Trash revokes subtree links. MongoDB
  restore preserves scope/expiry and revokes owner sessions. Previously delivered
  bytes cannot be withdrawn. Compatibility single-item mutations lack v1's public
  exposure confirmation; direct callers must check sharing themselves.
- **Browser:** local scripts/fonts/assets use CSP, nosniff, frame and referrer
  restrictions. Only successful `/embed` shell responses permit cross-origin
  framing (`frame-ancestors *`, no `X-Frame-Options`); other routes retain
  `frame-ancestors 'none'` and `X-Frame-Options: DENY`. Embeds use `no-referrer`
  and a no-store shell. A reverse proxy must preserve this route-specific policy,
  and the containing website must allow TGDrive in its frame policy. The trusted
  video decoder permits WASM compilation, blob workers/
  media, inline styles and `data:` decoder connections; arbitrary script eval and
  inline scripts stay disabled. HTML/code stays passive; PDF scripts, forms and
  annotations do not execute. Selected images/audio/video can render inline;
  active document types download as attachments. See [preview contracts](#previews-and-media).
- **Remote imports:** public-address and redirect validation, no URL credentials,
  identity encoding, no inherited environment proxy, bounded time/bytes/disk.
  `ALLOW_PRIVATE_REMOTE_URLS` explicitly broadens reachability. Signed source URLs
  may persist in private jobs.
- **Backups/deletion:** recovery files use AES-256-GCM before disk or Telegram
  storage and need their matching key, independently of login passwords. New
  files exclude logged-in browser/Telegram account sessions and unsent staging;
  they cannot recreate missing Telegram originals. Permanent deletion removes catalog
  references; it does not erase originals, caches, old backups or recipients'
  copies. Old backups may restore removed records.

Keep `.env`, sessions, hashes, keys, metadata and private traces out of public
reports. Disable raw query/cookie logging at proxies. TGDrive does not encrypt
original payloads or local staging and does not scan for malware. See
[Operations](operations.md), [Backup & recovery](backup-recovery.md) and
[known gaps](roadmap.md).

</details>
