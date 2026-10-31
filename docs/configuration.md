# Configuration and safe tuning

[Docs home](README.md) · [Setup](setup.md) · [Performance diagnostics](performance.md)

TGDrive sets its own cache sizes, storage budgets and Telegram concurrency from
the server's memory, CPU, disk and Telegram's responses, and keeps adjusting
them while it runs. There is nothing to tune. **Configuration** holds only the
choices that are yours to make.

| I want to… | Where |
| --- | --- |
| See what TGDrive chose and why | **Performance → Automatic tuning** ([how it works](performance.md#automatic-tuning)) |
| Cap TGDrive on a shared server | [Cap TGDrive with Docker limits](#cap-tgdrive-with-docker-limits) |
| Turn playback preloading on or off | [Cache settings](#cache-settings) |
| Split files into smaller Telegram documents | [Transfer limits](#transfer-limits) |
| Change displayed dates | [Timezone](#timezone) |
| Change credentials or hosting behavior | [Deployment-only settings](#deployment-only-settings) |
| Run a shared demo with locked settings | [Public demo mode](#public-demo-mode) |

## Change, discard or restore

| Action | Effect |
| --- | --- |
| **Save changes** | Saves this page; unsaved values on other pages remain drafts. |
| **Discard changes** | Reloads this page's saved values. |
| **Restore defaults** | Loads built-in values for this page; review and save to apply. |

If another session changes settings first, reload before saving.

## Cache settings

| Setting | Default | Meaning |
| --- | --- | --- |
| Preload during playback | On | Prepare upcoming original audio/video bytes. Off still allows requested-range read-ahead and manual **Preload file**. |

Cache memory and disk budgets are automatic. Clearing or expiring cache leaves
Telegram originals and MongoDB records intact.
See [preloading](usage.md#preload-a-file-for-smoother-playback-and-downloads).

**Regenerate all thumbnails** refreshes existing file covers in the background
after confirmation. See [rebuild progress and limits](thumbnails.md#rebuild-existing-thumbnails).

## Transfer limits

| Setting | Default | Meaning |
| --- | --- | --- |
| File split size | 2000 MiB | Largest Telegram document TGDrive creates; 2000 MiB is Telegram's limit. Choose 1–2000; 0 also uses 2000. Existing transfers keep their plan. |

The largest file you can upload is whatever fits in free disk space after a
reserve; uploads per bot, URL downloads and Telegram request limits are
automatic. **Bot uploads** (folder and encryption) also live on this page.

## Cap TGDrive with Docker limits

The automatic tuning reads the container's own memory and CPU limits, so on a
server shared with other apps, cap TGDrive in Docker instead of in TGDrive:

~~~yaml
services:
    app:
        mem_limit: 2g
        cpus: 2
~~~

Environment variables such as `CACHE_MEMORY_BYTES`, `STAGING_BUDGET_BYTES` or
`TELEGRAM_REQUEST_LIMIT` from earlier versions are ignored.

## Timezone

The default is **Asia/Kolkata (IST)**. Use a valid timezone name such as
`Europe/London` or `UTC`, then choose **Save changes**. This controls displayed
dates and share expiry across the installation; stored timestamps remain UTC.

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
download backup files. These restrictions apply to direct API requests too;
credentials remain private. If configuration fails to load, **Try Again** appears
before controls; file features remain accessible.

Normal file features and background work continue with their usual limits.
Visitors still need the demo password to sign in. Database archive restoration
is disabled for **every login** in demo mode; use a separate private deployment
for [recovery](backup-recovery.md#restore-after-losing-mongodb).

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
