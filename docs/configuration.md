# Configuration and safe tuning

[Docs home](README.md) · [Setup](setup.md) · [Performance diagnostics](performance.md)

Use **Configuration** to change cache, transfer and timezone settings. Saved
changes persist across restarts and apply to new work immediately. Start with
the defaults, change one setting at a time and compare the same workload.

Memory and disk budgets are ceilings, not space allocated in advance. Work
still needs actual free resources.

[See the configuration screens](showcase.md#configure-your-drive) for a visual
tour. Use the defaults and limits below when configuring your installation.

**Contents**

- [Change, discard or restore](#change-discard-or-restore)
- [Cache settings](#cache-settings)
- [Transfer limits](#transfer-limits)
  - [Parallel uploads](#parallel-uploads)
  - [Upload splitting](#upload-splitting)
  - [URL downloads](#url-downloads)
  - [Upload size and available disk space](#upload-size-and-available-disk-space)
  - [Advanced Telegram limits](#advanced-telegram-limits)
- [Timezone](#timezone)
- [A modest server and a larger server](#a-modest-server-and-a-larger-server)
- [Deployment-only settings](#deployment-only-settings)
- [Public demo mode](#public-demo-mode)
  - [Keep sample files available](#keep-sample-files-available)
  - [Return to a private deployment](#return-to-a-private-deployment)

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

For a startup override, set `REMOTE_DOWNLOAD_CONCURRENCY` in the private `.env`
or hosting environment, then recreate/redeploy. A saved dashboard value takes
precedence; see [deployment settings](#deployment-only-settings).

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

Start with the defaults. On a small/shared server, consider one preload and one
upload per bot. More RAM or disk does not automatically increase the configured
cache budgets, and larger queues do not add processing capacity.

Use the [load and capacity guide](performance-tuning.md) to read website stats,
diagnose queues and choose cache, server or bot capacity for your workload.
Change one limit at a time and compare the same workload before keeping it.

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

Set `DEMO_MODE=true` in the demo deployment's environment, then recreate the
Compose container or redeploy through Coolify. It defaults to `false`.
Configure the demo's accounts, limits and backups before enabling it.

Use the same login form with either of these passwords:

| Password | Demo access |
| --- | --- |
| Existing `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` | Visitor access: settings/management stay read-only; files outside Demo Files remain editable. |
| Separate `DEMO_ADMIN_PASSWORD` or `DEMO_ADMIN_PASSWORD_HASH` | Administrator access to settings, backups and sample files, except database archive restoration. Keep this password private. |

The administrator password is optional and only works while `DEMO_MODE=true`.
If omitted, every web login remains restricted. Use a different password of at
least 12 characters, or supply an existing compatible Argon2id hash. See
[password storage and reset](operations.md#forgotten-login-password). Hashes take
precedence over their corresponding plaintext values. Both demo administrator
variables are deployment-only; they are not restored from the database.

Database archive restoration is disabled for every login while demo mode is on.
Use a separate private deployment for a restore or recovery rehearsal; see
[database recovery](backup-recovery.md#restore-a-lost-database-from-telegram).

Plaintext environment passwords are supported; hashes are optional. For example,
keep your existing visitor password and add a private full-access password:

```dotenv
DEMO_MODE=true
DEMO_ADMIN_PASSWORD=<your-different-private-password>
DEMO_ADMIN_PASSWORD_HASH=
```

Replace the placeholder, then recreate/redeploy. Keep the matching `_HASH`
variable empty when using plaintext. A hash lets deployment configuration store
a one-way Argon2id fingerprint instead of the actual password; at login, always
enter the password you chose. See [password storage and reset](operations.md#forgotten-login-password).

Coolify may prevent deleting a variable declared by Compose. To switch to a
plaintext password, clear the corresponding hash variable's **Value**, save it
and redeploy. A populated hash always takes precedence over plaintext.

“Visitor” access describes permissions, not a separate account or an automatic
content reset. Both login types use the configured session lifetime (24 hours
by default). Logging out and using the other password switches access.

Restricted visitors can open every **Configuration** and **Management** page
and inspect the current values and status, but cannot change settings, add/remove accounts,
start Premium login, refresh Telegram connections, or run backup/recovery
management actions. Recovery-key downloads are also disabled; credentials
remain private. The server rejects these requests even when sent outside the UI.
If deployment configuration cannot load, these pages offer **Try Again** before
showing controls; file features remain accessible.

Uploads, URL imports, browsing, organizing, previews, downloads, sharing and
manual preloads keep their normal behavior and limits. Automatic backups and
other configured background work continue while TGDrive runs. Demo mode does
not bypass login: give visitors the demo deployment's password separately.

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

Use a separate MongoDB database, Telegram storage channel and credentials for
the public demo. Visitors share one drive and can change or permanently delete
files outside the protected sample folder, so use disposable content there.
Demo mode does not isolate visitors, reset content or add quotas. Closing a visitor's browser follows
the [usual transfer rules](usage.md#what-upload-progress-means).

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
