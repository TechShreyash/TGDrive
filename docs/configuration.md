# Configuration and safe tuning

[Docs home](README.md) · [Setup](setup.md) · [Performance diagnostics](performance.md)

Open **Configuration** to change cache, transfer and timezone settings. Saved
changes persist across restarts and apply to new work immediately. Start with
the defaults; change one setting at a time.

Memory and disk budgets are ceilings, not space allocated in advance. Work
still needs actual free resources.

| I want to… | Setting |
| --- | --- |
| Adjust memory, disk use or preloading | [Cache settings](#cache-settings) |
| Upload larger files or change transfer concurrency | [Transfer limits](#transfer-limits) |
| Change displayed dates | [Timezone](#timezone) |
| Change credentials or hosting behavior | [Deployment-only settings](#deployment-only-settings) |
| Run a shared demo with locked settings | [Public demo mode](#public-demo-mode) |

## Change, discard or restore

Open **Configuration → Cache settings**, **Transfer limits** or **Timezone**.

| Action | Effect |
| --- | --- |
| **Save changes** | Saves this page; unsaved values on other pages remain drafts. |
| **Discard changes** | Reloads this page's saved values. |
| **Restore defaults** | Loads built-in values for this page; review and save to apply. |

**Parallel uploads** has its own save controls, described
[below](#parallel-uploads).

Saved dashboard values override startup environment values. Restoring defaults
does not remove environment variables or reset files, accounts, credentials or
recovery keys. If another session changes settings first, reload before saving.
Invalid values are rejected. If applying a change fails, TGDrive attempts to
restore the previous values and reports whether a restart is needed.

## Cache settings

Open **Configuration → Cache settings**.

| Setting | Default | Meaning |
| --- | --- | --- |
| Memory cache | 256 MiB | Recently read file bytes kept in memory; available RAM can lower the effective budget. 0 disables retention. |
| Disk cache | 10 GiB | Temporary file copies for repeat access. 0 disables disk retention. |
| Cache free-space floor | 1 GiB | Stop cache writes before free disk falls below this level. Saving with disk caching enabled requires at least 64 MiB. |
| Disk cache retention | 24 hours | Time since last reuse; capacity pressure can remove cached data sooner. |
| Concurrent preloads | 2 | Files prepared in the background at once; consider 1 on a small server. |
| Preload during playback | On | Prepare bounded upcoming original audio/video bytes. Downloads do not start extra warming. |

Clearing or expiring cache leaves Telegram originals and MongoDB records intact.
Active streams and unfinished uploads have separate budgets but share the host's
resources. Turning off playback preloading still allows requested-range
read-ahead and manual **Preload file**.
See [preloading](usage.md#preload-a-file-for-smoother-playback-and-downloads).

**Regenerate all thumbnails** refreshes existing file covers in the background
after confirmation. New uploads generate them automatically.
See [rebuild progress, bandwidth and limits](thumbnails.md#rebuild-existing-thumbnails).

## Transfer limits

Open **Configuration → Transfer limits**. Its controls appear in the order below.

### Parallel uploads

| Setting | Default | Meaning |
| --- | --- | --- |
| Simultaneous uploads per bot | 1 | Concurrent Telegram document uploads per healthy bot; choose 1–4. A large file can use multiple document-upload slots. |

Start with one upload per bot; add eligible bots before increasing the value.
Existing transfers finish when the limit is lowered. Premium accounts keep one
upload slot each.

This section has separate **Save Changes**, **Discard upload changes** and
**Restore upload defaults** actions. Restoring its default loads 1; choose
**Save Changes** to apply it. The page's other save controls do not save this value.

### Upload splitting

| Setting | Default | Meaning |
| --- | --- | --- |
| File split size | 2000 MiB | Maximum preferred size of each Telegram document, including for Premium accounts. Choose 1–2000 MiB; 0 also uses 2000 MiB. |

This controls how TGDrive stores a file in Telegram, rather than the largest
file you can upload. Existing transfers keep their original split plan.
For the whole-file limit, use [Upload storage](#upload-size-and-available-disk-space).

### URL downloads

| Setting | Default | Meaning |
| --- | --- | --- |
| Full-speed URL downloads | 2 | URL imports downloading at full speed together; choose 1–8. |

This controls the source-to-server stage. Telegram uploading uses its own slots.
Waiting sources may receive data slowly while connection and storage limits
permit; expired links can still fail. See [URL intake](performance.md#url-import-intake)
for queue behavior and retries.

For a startup default, use `REMOTE_DOWNLOAD_CONCURRENCY` in
[deployment settings](#deployment-only-settings). A saved dashboard value takes precedence.

### Upload size and available disk space

Open **Transfer limits → Upload storage**.

| Setting | Default | Meaning |
| --- | --- | --- |
| Maximum file size | 16 GiB | Largest new file accepted. Telegram document splitting is separate. |
| Temporary upload budget | 16 GiB | Space all unfinished uploads may use together; it must cover the per-file maximum. |
| Upload free-space reserve | 1 GiB | Space uploads must leave free on their storage volume after other uploads' reservations are accounted for. Minimum configurable reserve: 64 MiB. |

To upload a **32 GiB file**, raise both the file maximum and temporary budget to
at least 32 GiB. A 64 GiB budget can reserve two such files if actual disk space
permits. Both controls support up to **2048 GiB (2 TiB)**. Existing uploads retain
their reservations; reducing the budget below those reservations is rejected.

Browser uploads and full-speed URL imports reserve the whole file and check
actual free space, including other uploads' unwritten reservations.
A full-speed unknown-length URL import reserves its admitted maximum. If that
prevents admission, use a source that reports its file size or lower the file limit.
Waiting URL sources reserve smaller windows; see
[URL storage policy](performance.md#url-import-intake).

Torrent imports reserve the entire torrent against the same temporary budget
before downloading file contents. Each file must fit the file maximum; the
combined torrent must fit the temporary budget and actual free space. The URL
download concurrency setting controls HTTP imports, not torrent peer traffic.
See [torrent limits](reference.md#torrent-imports).

Space is rechecked during receipt, but other disk users can still fill it.
Browser disk-full failures retain the confirmed resume position; URL retrieval
may restart from zero. These settings control temporary server disk use;
they do not set your total Telegram storage capacity.

### Advanced Telegram limits

<details>
<summary>Request and media-operation controls</summary>

| Setting | Default | Meaning |
| --- | --- | --- |
| Upload pipeline | 4 | Parallel Telegram upload-part requests within one document |
| Requests per account | 8 | Shared upload, download and metadata requests |
| Requests per second | 48 | App pacing ceiling, not a Telegram allowance |
| Media operations per account | 3 | Telegram uploads/downloads; cached delivery does not use these slots |
| Maximum queued operations | 256 | Waiting Telegram work before excess requests must retry |

The server rejects invalid values and enforces
[exact bounds](reference.md#performance-and-cache-settings).
More slots share the same bandwidth; Telegram cooldowns still apply.
Raise one limit only after measuring throughput and provider waits.

</details>

## Timezone

Open **Configuration → Timezone**. The default is **Asia/Kolkata (IST)**.
Use a valid timezone name such as `Europe/London` or `UTC`, then choose
**Save changes**. This controls displayed dates and share expiry across the
installation; stored timestamps remain UTC.

## A modest server and a larger server

On a small/shared server, consider one preload and one upload per bot. Added RAM
or disk does not increase configured cache budgets; larger queues do not add
processing capacity. Use the [load and capacity guide](performance-tuning.md)
before raising limits, and compare the same workload after each change.

## Deployment-only settings

Use the private `.env` or hosting environment for initial setup and values
without app controls, including MongoDB access, owner credentials, session
policy, proxy trust and demo mode. See [Setup](setup.md) and the
[variable reference](reference.md#configuration) for exact names and bounds.

After editing Docker environment values, recreate the container or redeploy.
Restarting the existing container alone does not load the edited values.
Saved dashboard preferences still take precedence over startup defaults.

MongoDB is required for every deployment. Invalid configuration or failed
database access stops startup; no local database is substituted. A pristine
database is initialized automatically.
See [startup and recovery boundaries](architecture.md#lifecycle).

## Public demo mode

Use a **separate database, Telegram channel and credentials** for a public demo.
Visitors share one drive and can permanently delete files outside **Demo Files**.
Demo mode does not isolate visitors, reset content or add quotas.

Configure accounts, limits and backups first. Then set `DEMO_MODE=true` in the
deployment environment and recreate/redeploy; the default is `false`.

Use the same login form with either of these passwords:

| Password | Demo access |
| --- | --- |
| Existing `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` | Visitor access: settings/management stay read-only; files outside Demo Files remain editable. |
| Separate `DEMO_ADMIN_PASSWORD` or `DEMO_ADMIN_PASSWORD_HASH` | Administrator access to settings, backups and sample files, except database archive restoration. Keep this password private. |

The optional administrator password works only in demo mode. Without it, all
web logins are restricted. Keep your existing visitor password and add a
**different private password of at least 12 characters**:

```dotenv
DEMO_MODE=true
DEMO_ADMIN_PASSWORD=<your-different-private-password>
DEMO_ADMIN_PASSWORD_HASH=
```

Replace the placeholder and recreate/redeploy. A compatible Argon2id hash is
optional; a populated `_HASH` always overrides plaintext. To use plaintext in
Coolify, clear the hash variable's **Value**, save and redeploy if deletion is
unavailable. Always sign in with the password, not its hash.
[Password storage and reset](operations.md#forgotten-login-password).

Both demo administrator variables are deployment-only and are not restored from
the database. Log out and use the other password to switch access. Both login
types use the configured session lifetime, **24 hours** by default.

Visitors can inspect Configuration and Management status but cannot change
settings/accounts, start Premium login, refresh connections, manage backups or
download recovery keys. These restrictions apply to direct API requests too;
credentials remain private. If configuration fails to load, **Try Again** appears
before controls; file features remain accessible.

Normal file features and background work continue with their usual limits.
Visitors still need the demo password to sign in. Database archive restoration
is disabled for **every login** in demo mode; use a separate private deployment
for [recovery](backup-recovery.md#restore-a-lost-database-from-telegram).

### Keep sample files available

TGDrive creates **Demo Files** in **My Files** when demo mode starts. Sign in
with the full-access password to populate it with sample videos, images,
documents and other file types. Visitors can browse, preview and download its
contents. Attempts to upload, rename, move, delete or otherwise edit this folder
or its descendants are rejected, with a popup directing visitors to **My Files**
for their own changes. These restrictions also apply to direct API requests.

The protected identity survives a full-access user's rename or move. Keep it in
the root with its default name so visitors can find it. Only a full-access user
can remove its samples; demo mode does not supply or regenerate sample content.
If the reserved folder is missing, the next demo startup recreates an empty one.
If another file or folder already uses the name **Demo Files**,
[return to a private deployment safely](#return-to-a-private-deployment), rename
that item, then enable demo mode again. Existing content is preserved.

Use disposable content outside the protected folder. Closing a visitor's browser
follows the [usual transfer rules](usage.md#what-upload-progress-means).

### Return to a private deployment

1. Restrict public access and replace any published visitor credential with a
   private owner password or hash. Follow the
   [password-change instructions](operations.md#forgotten-login-password).
2. Set `DEMO_MODE=false` and recreate/redeploy. Sign in with the private owner
   password.

The usual owner password grants full access once demo mode is off. The separate
`DEMO_ADMIN_*` password does not become the normal owner credential. Demo variables
cannot be changed in the app.

Keep the public address and visitor sign-in instructions in the
[public demo guide](usage.md#try-the-public-demo). Keep the full-access password
private.
