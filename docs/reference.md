# Configuration and security reference

[Docs home](README.md) · [Configuration](configuration.md) · [API reference](api.md)

Use this page to look up exact deployment settings and security boundaries;
HTTP endpoints are in the [API reference](api.md). For website tasks,
use the [user guide](usage.md); for settings you can change on screen, use
[Configuration](configuration.md). Expand a section below when you need its details.

<a id="http-api"></a>
<a id="resumable-browser-receipts"></a>

Moved API topics: [HTTP API](api.md) ·
[Resumable browser receipts](api.md#resumable-browser-receipts).

| I need to… | Look up |
| --- | --- |
| Set deployment values | [Configuration](#configuration) · [File syntax and secrets](#file-syntax-and-secret-handling) |
| Understand an unfamiliar term | [Glossary](#terms-used-here) |
| Review access and privacy | [Security boundaries](#security-and-privacy) · [Stored file encryption](#stored-file-encryption) |
| Write an API client | [HTTP API reference](api.md) |

<details>
<summary>All sections</summary>

- [Terms used here](#terms-used-here)
- [Configuration](#configuration)
  - [File syntax and secret handling](#file-syntax-and-secret-handling)
  - [Telegram accounts](#telegram-accounts)
  - [Performance and cache settings](#performance-and-cache-settings)
  - [Owner authentication](#owner-authentication)
  - [Persistence and initialization](#persistence-and-initialization)
  - [File capacity, staging, and work limits](#file-capacity-staging-and-work-limits)
  - [Remote HTTP(S) uploads](#remote-https-uploads)
  - [Deployment variables outside application Settings](#deployment-variables-outside-application-settings)
- [Owner administration](#owner-administration)
- [Security and privacy](#security-and-privacy)
- [Stored file encryption](#stored-file-encryption)

</details>

## Terms used here

| Term | Meaning |
| --- | --- |
| Catalog / metadata | Records describing your files and folders; not their contents |
| Receipt | The server's record of upload bytes already received |
| Replica set | A MongoDB deployment mode that supports transactions; Atlas clusters are replica sets, a plain standalone `mongod` is not |
| Publication | The final upload step: sending the Telegram message and saving the file record |
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
| Saved **Connections** | Overrides original storage-account credentials; applied to the running client pool on save |
| Saved dashboard performance preferences | Overrides matching startup defaults and applies to new work immediately |

Recreate Docker containers after changing their environment; a process restart
alone does not load it. Recreating does not erase saved dashboard preferences.

Compose loads `.env` through `env_file`; its `environment` entries override matching
values. The supplied files fix container paths, cookies and proxy policy; the local
override permits localhost HTTP. [Docker environment details](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).

| Deployment flag | Default | Behavior |
| --- | --- | --- |
| `DEMO_MODE` | false | Give the usual password restricted visitor access; protect Demo Files and lock management writes/backup downloads. A separately configured demo administrator can bypass those restrictions. Requires container recreation/redeploy. See [public demo mode](configuration.md#public-demo-mode). |

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
- With standard Compose auto-secrets enabled, a blank SESSION_SECRET reuses the
  saved value, or is generated for a new drive. ENCRYPTION_KEY is never generated.

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
| `PUBLIC_URL` | Omitted/empty by default | Optional HTTP(S) website origin for the bot's `/set_folder` link and **Open in TGDrive** button. Otherwise the website address is remembered when saving its bot upload folder. |
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

Managed live resource values are not direct dashboard preferences. Environment
variables seed startup. Saved owner ceilings bound the
[governor](performance.md#automatic-tuning), which updates cache and staging
budgets, free-space reserves and the file ceiling. Configured
concurrency ceilings remain; preloads also stay within 4 jobs per CPU and 8 total. Per-account Telegram
cooldown/recovery is separate and survives resource-only updates.
Pacing (`TELEGRAM_PARTS_PER_SECOND`), queue size (`TELEGRAM_MAX_QUEUE`) and cache
retention (`CACHE_TTL_SECONDS`) keep their startup values.
The managed variable names are `CACHE_MEMORY_BYTES`,
`CACHE_DISK_BYTES`, `CACHE_MIN_FREE_BYTES`, `CACHE_TTL_SECONDS`, `PRELOAD_MAX_JOBS`,
`TELEGRAM_UPLOAD_PIPELINE`, `TELEGRAM_REQUEST_LIMIT`, `TELEGRAM_PARTS_PER_SECOND`,
`TELEGRAM_MEDIA_PER_BOT`, `TELEGRAM_MAX_QUEUE`, `TELEGRAM_UPLOADS_PER_BOT`,
`REMOTE_DOWNLOAD_CONCURRENCY`, `MAX_FILE_SIZE`, `STAGING_BUDGET_BYTES`,
`MIN_FREE_BYTES`.
`PATCH /api/v1/settings/performance` rejects those managed names with `422`.
It also accepts the four nullable owner byte ceilings below, saved in MongoDB.
`null` selects Auto. Values must be strict integers up to `2^53 - 1`; cache
ceilings and reserve allow zero, while staging must be positive.

| JSON key | Auto | Scope |
| --- | --- | --- |
| `cache_memory_limit_bytes` | 256 MiB | Retained media payload in RAM; zero disables this retention tier |
| `cache_disk_limit_bytes` | 10 GiB | Retained media payload on disk; zero disables this retention tier |
| `staging_limit_bytes` | 16 GiB | Temporary staging and the logical-file ceiling |
| `protected_free_bytes` | Larger of 1 GiB and 5% of each relevant filesystem | Extra TGDrive free-space floor; zero removes that floor |

GET returns authoritative `auto_resource_limits` and `auto_free_space_reserve`.
Saves retain revision/ETag conflict checks (`409`) and rollback on apply failure.
Reductions apply immediately; expansion may wait for the five-second governor
pass. Lowering staging preserves unchanged/shrinking admitted claims, not new
or growing claims. Limits reserve no physical space and actual free-space checks
remain. Cover/poster caches and protected recovery files are separate; these
values do not cap process RSS.

Other owner choices are saved in MongoDB:

| Variable | Default | Meaning and bounds |
| --- | --- | --- |
| `AUTO_PRELOAD` | true | Enable extra bounded warming after delivered original audio/video playback bytes. Explicit downloads and unclassified range clients stay within requested ranges. Disabling it leaves adaptive response read-ahead and manual **Preload file** available. |
| `UPLOAD_SPLIT_MIB` | 2000 | TGDrive's default and maximum document size for every account, including Premium. Explicit 1–2000 values choose a smaller part size; 0 also uses 2000 MiB. Persisted plans keep their original boundaries. |
| `TIMEZONE` | Asia/Kolkata | Installation-wide display timezone for owner and public dates/share expiry. Use a valid IANA name; the default is IST. |

RAM includes the asynchronous disk-write backlog. Active responses have a separate
32 MiB transient payload budget, reduced by available memory with a 2 MiB floor;
transport/server buffers are outside these budgets. `cache.scratch` reports current,
effective and configured bytes, peak reservations, waiting heads and active streams.
RAM and disk may duplicate blocks, so their sum is not whole-file capacity.

Preload admission counts complete aligned Telegram blocks, multipart boundaries,
a 32-byte disk integrity prefix per block and competing reservations. Retention
never guarantees a pin: TTL, eviction and disk failure still apply.

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
Encrypted database backups
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
| `ENCRYPTION_KEY` | Required, secret | 64 hex characters. Protects new encrypted Telegram documents (AES-256-CTR); current backup-file keys derive from it. Never stored in MongoDB, backups or logs. The drive remembers a fingerprint; startup stops if the key differs. [Coverage and exceptions](#stored-file-encryption). |
| `MONGODB_DATABASE` | tgdrive | 1–63 ASCII letters/digits, underscores or hyphens; dedicated to one drive. |
| `MONGODB_BACKUP_SECONDS` | 1 day (`86400`) | Integer 60–604800 seconds between successful automatic standalone safety copies, including unchanged catalogs. Manual backup requests an immediate point or retries pending publication; recovery or paused independent protection can block it. Check the confirmed-backup date and errors. Separate standalone publication pauses while independent channel protection is enabled. |
| `DATA_DIR` | data | Persistent generated keys, Telegram sessions, helper locks, recovery/activation state and media cache. MongoDB holds the live catalog and durable job/receipt records. |
| `CACHE_DIR` | cache | Persistent unfinished upload payloads and encrypted temporary database backup files. Keep unfinished bytes. |
| `LOG_LEVEL` | INFO | DEBUG, INFO, WARNING, ERROR or CRITICAL. |

DATA_DIR and CACHE_DIR must be writable distinct paths, not a filesystem
root or account home, equal or nested. Compose sets /app/data and /app/cache.
The container runs as UID/GID 10001:10001; custom bind mounts must allow that
account to write. Do not replace missing writable volumes with disposable mounts.

Standard Compose enables `TGDRIVE_AUTO_SECRETS=true`. A missing
`SESSION_SECRET` is generated once for a new
drive and saved as mode 0600 in `DATA_DIR/deployment-secrets.json`. Explicit
values and existing MongoDB bootstrap settings take precedence. Existing drives
with missing keys fail rather than silently receiving replacement keys.

Backups are encrypted with a key derived from `ENCRYPTION_KEY`; there is no
separate recovery key to save.

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
| `UPLOAD_SESSION_TTL_SECONDS` | 86400 | Integer at least 60; incomplete receipt idle lifetime in seconds, refreshed after each committed chunk. There is no declared upper bound in configuration. |
| `MAX_CONCURRENT_TRANSFERS` | 2 | Integer, at least 1; bounds local fingerprinting and receive work. Torrent downloads and receipt handlers each use a separate bound of this size, so slow peers do not occupy local upload-preparation slots. It does not cap Telegram uploads across bots. |
| `TRANSFER_TTL_SECONDS` | 3600 | Integer, at least 60; terminal jobs become eligible for cleanup on admission or explicit pruning. Compact server-transfer history is kept separately; files are unaffected. |

New documents use at most **2000 MiB (2,097,152,000 bytes)** for every account,
including Premium. `UPLOAD_SPLIT_MIB=0` uses that cap. Larger logical files use
ordered parts and appear as one item with original downloads and range seeking.
The `/api/config` logical limit follows the governor's total staging budget;
new work still needs room after existing reservations and the disk reserve.
TGDrive has no stored-byte quota or configured catalog
item-count quota; MongoDB capacity, request/metadata bounds and recovery resources
still constrain usable workloads.
Premium is preferred when eligible; bot fallback and existing sender identities
remain supported.

Browser chunks are 8 MiB except the last. Unfinished receipts and browser
batches have no count quota; staging space, browser memory and saved recovery
storage still limit them. Finished browser history keeps at most 30 tasks/2,000
rows, retaining unresolved tasks. Browser bounds
are per controller; staging and Telegram account limits apply across callers.
Allow disk headroom for URL work, logs, metadata and backups beyond receipt
reservations.

Device receives start at two concurrent files and adapt between one and four.
Each accepted file queues immediately. Staging/account waits still apply. See the canonical
[transfer pipeline](architecture.md#transfer-pipeline) for adaptation, admission,
retry and cancellation rules.

</details>

### Remote HTTP(S) uploads

<details>
<summary>URL fetching, timeouts and network settings</summary>

| Variable | Default | Validation and behavior |
| --- | --- | --- |
| `ALLOW_PRIVATE_REMOTE_URLS` | false | Enables private/internal destinations only when explicitly true. It broadens what the authenticated owner can reach. |
| `REMOTE_CONNECT_TIMEOUT` | 20 | Seconds, greater than 0. |
| `REMOTE_READ_TIMEOUT` | 120 | Seconds, greater than 0. A source that sends no new bytes for this long counts as stalled and is retried; imports have no overall time limit. |
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
| `TGDRIVE_AUTO_SECRETS` | Standard Compose sets true in the container environment. Generates a missing `SESSION_SECRET` once for a new drive in persistent DATA_DIR. It never generates `ENCRYPTION_KEY`; existing drives must reuse their original secrets. |
| `DATA_BACKING_CAPACITY_DIR` | Optional absolute path to an existing empty directory on the verified backing filesystem for `DATA_DIR`; deployment-only, not restored from backups. |
| `CACHE_BACKING_CAPACITY_DIR` | Equivalent backing probe for `CACHE_DIR`; configure separately for distinct backing filesystems. |
| `TGDRIVE_RESTART_POLICY` | Compose interpolation only. Laptop override defaults to no, hosted/server mode to unless-stopped. Choose deliberately; restart policies do not restart a running container merely because it is unhealthy. |
| --workers | Must remain 1. This is a launch argument, not an environment feature for scaling the drive. |

Backing probes are optional operator configuration. Verify the real host/backing
filesystem first, then expose its existing empty probe directory to the app
(for containers, through a mount) and use the path visible inside the app.
A probe must not be a symlink. Missing, unreadable, nonempty or conflicting
configured probes are unavailable; TGDrive does not silently fall back to guest
capacity. Without a probe, backing capacity remains unknown.

Measurements use the smaller free-space reading of the destination and its
configured backing filesystem. Each writer also applies its reserve and captured
outstanding staging claims as appropriate. Data/cache paths select their matching
probe; equal roots need consistent probe configuration. Reporting exposes no
probe path. **Free after reserve** excludes outstanding claims. These measured
checks and OS write errors do not form an atomic quota or protect against every
external host write. Required measurement failure refuses writes; existing
committed files remain preserved.

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
**Storage and Backups** for protection and archive/channel recovery. Deployment
credentials and service restarts remain in Docker Compose or Coolify; failures
that prevent startup are explained in container logs. See
[recovery](backup-recovery.md) and [password reset](operations.md#forgotten-login-password).

## Security and privacy

| Boundary | What to know |
| --- | --- |
| Owner access | One shared drive; demo mode optionally separates visitor and full-access passwords. No built-in MFA/passkeys, public signup, teams or tenant isolation. |
| Shared Links | Anyone with a valid scoped link can read its contents |
| Encryption | New encrypted uploads, legacy plaintext parts, optional plaintext bot copies and local storage have different coverage. The server operator is trusted. [Details](#stored-file-encryption) |
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
  timeout. Sensitive settings and backup download accept an active owner
  session without a fresh password prompt.
- **Requests:** mutations check Origin/Fetch Metadata; private routes authenticate
  before body processing. JSON controls have [finite body limits](api.md#conventions-and-authentication)
  even for signed-in/demo visitors; receipt PATCH chunks are 8 MiB. Login allows
  five failures per client IP in five minutes and bounds
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
  active document types download as attachments. See [preview contracts](api.md#previews-and-media).
- **Remote imports:** public-address and redirect validation, no URL credentials,
  identity encoding, no inherited environment proxy, bounded bytes and disk.
  There is no overall time limit; stalled sources are retried.
  `ALLOW_PRIVATE_REMOTE_URLS` explicitly broadens reachability. Signed source URLs
  may persist in private jobs.
- **Backups/deletion:** current `.tgdb` recovery files use AES-256-GCM and need the
  original `ENCRYPTION_KEY`, independently of login passwords. Private recovery
  staging can contain plaintext metadata and credentials; protect the host and
  volumes. See [staging and recovery limits](backup-recovery.md#how-backups-work).
  Backups exclude logged-in browser/Telegram account sessions and unsent staging;
  they cannot recreate missing Telegram originals. Permanent deletion removes catalog
  references; it does not erase originals, caches, old backups or recipients'
  copies. Old backups may restore removed records.

Keep `.env`, sessions, hashes, keys, metadata and private traces out of public
reports. Disable raw query/cookie logging at proxies. Local plaintext files and
encryption limits are described [below](#stored-file-encryption); TGDrive does
not scan for malware. See
[Operations](operations.md), [Backup & recovery](backup-recovery.md) and
[known gaps](roadmap.md).

</details>

## Stored file encryption

<details>
<summary>What is encrypted, how and its limits</summary>

| Item | Behavior |
| --- | --- |
| Uploaded parts, posters | AES-256-CTR with `ENCRYPTION_KEY` and a random 128-bit nonce per document, saved in the catalog part record. Sizes are unchanged, so range reads and seeking work. |
| Telegram metadata | Random `<hex>.bin` name, `application/octet-stream`, no caption and no Telegram thumbnail. Previews come from TGDrive's local cover cache. |
| Bot mode, encryption on (default) | The server downloads the forwarded file to `CACHE_DIR/bot-imports`, then uploads it encrypted through the normal upload queue. Slower; uses server bandwidth and disk. |
| Bot mode, encryption off | Telegram copies the message without server download/re-upload; that file remains plaintext. Set in **Configuration → Transfer limits → Bot uploads**. |
| Unencrypted parts | Legacy files uploaded by 2.2 and bot-mode copies made with encryption off have no nonce and are read as plain bytes. Upgrading does not re-encrypt them. |
| Key check | MongoDB `settings._id = "encryption"` holds an HMAC fingerprint, never the key. New drives and the schema-4 upgrader write it; a different key or a missing record after upgrade stops startup. |
| Integrity | Complete-part reads verify a stored plaintext SHA-256 when available; partial reads check document identity/size but have no authenticated block tag. AES-CTR does not authenticate ranges. |

The media cache keeps fetched Telegram bytes: ciphertext for encrypted documents,
plaintext for legacy/plain documents. Decryption happens per read. Local upload
staging, generated covers and parser/preview output can contain plaintext; these
are not encrypted by `ENCRYPTION_KEY`. The host process holds the key and is a
trusted party. HTTPS protects browser delivery; it does not remove that trust.
`ENCRYPTION_KEY` does not encrypt the live MongoDB catalog; protect database
access and private configuration separately.

Losing `ENCRYPTION_KEY` prevents opening the drive, its encrypted documents and
current backups, even when some older parts remain plaintext. Current backups
use a separate derived AES-256-GCM key, not a separately exported secret. Keep
the [off-host recovery material](backup-recovery.md#save-off-host-recovery-material).

</details>

