# Configuration and safe tuning

[Docs home](README.md) · [Setup](setup.md) · [Performance diagnostics](performance.md)

Start with the defaults. Budgets are ceilings, not allocations; available
RAM/disk can reduce them. Change one setting, then repeat the same workload.

[Cache](#cache-settings) · [Transfers](#transfer-limits) · [File size & disk space](#upload-size-and-available-disk-space) · [Tuning](#a-modest-server-and-a-larger-server) · [Public demo](#public-demo-mode)

[See the configuration screens](showcase.md#configure-your-drive) for the cache,
upload, URL import and timezone controls. Captures show the interface; follow the
defaults and limits below when tuning your own installation.

## Public demo mode

Set `DEMO_MODE=true` in the demo deployment's environment, then recreate the
Compose container or redeploy through Coolify. It defaults to `false`.
Configure the demo's accounts, limits and backups before enabling it.

Use the same login form with either of these passwords:

| Password | Demo access |
| --- | --- |
| Existing `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` | Visitor access: settings/management stay read-only; files outside Demo Files remain editable. |
| Separate `DEMO_ADMIN_PASSWORD` or `DEMO_ADMIN_PASSWORD_HASH` | Full access, including settings, backups and the sample files. Keep this password private. |

The full-access password is optional and only works while `DEMO_MODE=true`.
If omitted, every web login remains restricted. Use a different password of at
least 12 characters, or generate an Argon2id hash with the
[password-hash command](operations.md#forgotten-login-password). Hashes take
precedence over their corresponding plaintext values. Both demo administrator
variables are deployment-only; they are not restored from the database.

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

**Temporary Coolify recovery state:** the `DEMO_ADMIN_PASSWORD` mapping is
currently removed from `compose.yaml` so an old variable can be deleted and
recreated without legacy escaping. Restore
`DEMO_ADMIN_PASSWORD: ${DEMO_ADMIN_PASSWORD:-}` under `services.app.environment`
before redeploying with a plaintext demo administrator password.

<details>
<summary>Recreate a variable with Coolify's “Old escaping” warning</summary>

Coolify 4.5.0 passes new variables exactly as entered but preserves legacy
escaping for existing variables. That can make a password containing quotes
differ from the value received by TGDrive. See the
[Coolify release notes](https://github.com/coollabsio/coolify/releases/tag/v4.5.0).

1. Pause automatic deployments before pushing the temporary Compose change.
2. Remove the affected variable's mapping from the deployment branch's Compose
   file, push it, then select **General → Reload Compose** in Coolify. For a
   Git-backed application, change the file in Git; Coolify's editor is read-only.
3. Delete and re-add the environment variable in Coolify. Confirm that the
   **Old escaping** warning is gone.
4. Restore the original Compose mapping, push it and **Reload Compose** again.
5. Redeploy, then restore the previous automatic deployment setting.

Do not deploy the intermediate Compose file. Removing its mapping prevents that
file from passing the plaintext demo administrator password to the container;
it does not disable demo mode. A configured `DEMO_ADMIN_PASSWORD_HASH` still
takes precedence.

</details>

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
If a different file or folder already has the name **Demo Files**, disable demo
mode, rename that item and enable demo mode again; existing content is preserved.

Use a separate MongoDB database, Telegram storage channel and credentials for
the public demo. Visitors share one drive and can change or permanently delete
files outside the protected sample folder, so use disposable content there.
Demo mode does not isolate visitors, reset content or add quotas. Closing a visitor's browser follows
the [usual transfer rules](usage.md#what-upload-progress-means).

To administer the demo, use the full-access password. To return to the normal
single-password deployment, set `DEMO_MODE=false` and recreate/redeploy. Demo
variables cannot be changed in the app. See [password storage and reset](operations.md#forgotten-login-password).

Keep the public address and visitor sign-in instructions in the
[public demo guide](usage.md#try-the-public-demo). Keep the full-access password
private.

## Change, discard or restore

Open **Configuration → Cache settings / Transfer limits / Timezone**.

| Action | Effect |
| --- | --- |
| **Save Performance Settings** | Applies this category; other categories remain drafts. |
| **Discard changes** | Reloads this category's saved values. |
| **Restore defaults** | Loads built-in values into the form; save to apply. |
| **Parallel uploads** actions | Save, discard or restore concurrency separately. |

Saved dashboard values override startup environment values. Restoring defaults
does not remove environment variables or reset files, accounts, credentials or
recovery keys. A stale save requires reloading; invalid values are rejected.
An apply failure attempts rollback and reports if a restart is still needed.

## Cache settings

| Setting | Default | Meaning |
| --- | --- | --- |
| Memory cache | 256 MiB | Shared retained media; effective RAM budget may be smaller. 0 disables retention. |
| Disk cache | 10 GiB | Disposable downloaded bytes; 0 disables it. |
| Cache free-space floor | 1 GiB | Stop writes before disk drops below this floor. Dashboard minimum with caching enabled: 64 MiB. |
| Disk retention | 24 hours | Time since last reuse; capacity pressure can evict sooner. |
| Concurrent preloads | 2 | Manual/automatic jobs; consider 1 on a constrained server. |
| Preload during playback | On | Warm bounded upcoming original audio/video. Downloads do not start extra warming. |

**Regenerate all thumbnails** refreshes existing file covers in the background
after confirmation. New uploads generate them automatically. See
[progress, bandwidth and rebuild limits](thumbnails.md#rebuild-existing-thumbnails).

Clearing/expiring cache keeps Telegram originals and MongoDB metadata.
Active-stream RAM and upload staging have separate budgets but share host resources.
Disabling playback warming retains requested-range read-ahead and manual
**Preload file**. See [preloading](usage.md#preload-a-file-for-smoother-playback-and-downloads).

## Transfer limits

| Setting | Default | Meaning |
| --- | --- | --- |
| Simultaneous uploads per bot | 1 | Whole uploads per healthy bot; add eligible bots before raising this. |
| File split size | 2000 MiB | Default/maximum for every account. Choose 1–2000 MiB; 0 uses 2000 MiB. |

<details>
<summary>Advanced Telegram limits</summary>

| Setting | Default | Meaning |
| --- | --- | --- |
| Upload pipeline | 4 | Outstanding part requests within one document |
| Requests per account | 8 | Shared upload/download/metadata requests |
| Requests per second | 48 | App pacing ceiling, not a Telegram allowance |
| Media operations per account | 3 | Origin uploads/downloads; cached reads do not use these slots |
| Maximum queued operations | 256 | Waiting origin work before excess work must retry |

The server rejects invalid values and enforces [bounds](reference.md#configuration).
More slots share the same bandwidth; Telegram cooldowns always win.
Raise one limit only after measuring throughput and provider waits.

</details>

## Upload size and available disk space

These are technical capacity settings, not paid feature tiers. Self-hosted and
[managed hosting](hosting.md#optional-managed-hosting) have the same TGDrive features.
Hosting plans add no caps on file size or upload/download counts; configured
limits, available disk and [server capacity](hosting.md#capacity-and-practical-limits)
still apply.

In **Transfer limits → Upload storage**, the owner can change:

| Setting | Default | Meaning |
| --- | --- | --- |
| Maximum file size | 16 GiB | Largest new file; Telegram document splitting is separate. |
| Temporary upload budget | 16 GiB | Combined reservation for all unfinished uploads. Must be at least the per-file maximum. |
| Upload free-space reserve | 1 GiB | Space left free on the actual upload volume after unfinished uploads are accounted for. Minimum configurable reserve is 64 MiB. |

**Uploading a 32 GiB file?** Raise **both** file maximum and temporary budget to
at least 32 GiB. A 64 GiB budget can reserve two such files if real space permits.
Both controls support up to 2048 GiB. Existing admissions retain their size;
shrinking below active reservations is rejected.

Browser uploads and full-speed URL imports reserve the whole file and check real
free space, including other uploads' unwritten reservations. Full-speed unknown-length
URL imports reserve the admitted maximum; provide Content-Length or lower the file
limit if this prevents admission. Waiting URL sources reserve smaller windows.
**Transfer limits → URL downloads** controls simultaneous full-speed imports
(default **2**, range **1–8**); see [URL intake](performance.md#url-import-intake)
for slow queued connections, storage waits and link-expiry limits. Space is rechecked during receipt, but other disk
users can still fill it. Browser disk-full failures retain the acknowledged resume
offset; URL retrieval may restart from zero. See [URL intake](performance.md#url-import-intake).
These limits describe local staging, not Telegram storage capacity.

## A modest server and a larger server

Test an upload, cold playback, a seek and repeat playback before tuning.
On small/shared servers, consider one preload and one upload per bot.
Check actual container limits and other disk users, not just host specifications.

| Observation | Investigate before increasing limits |
| --- | --- |
| Container RAM approaches its limit | Lower RAM cache/concurrency and inspect active stream/process usage. |
| CPU stays near capacity | Media decoding/preview work and concurrent operations; more requests may worsen it. |
| Telegram work waits or accounts cool down | Account eligibility, provider waits and shared request limits. |
| Cold playback is slow but repeat access is fast | Telegram route/latency and cache reuse, not browser cache alone. |
| URL upload receives data but viewers are slow | Separate server ingress from outbound/viewer traffic. |
| Disk free space falls | Upload staging, cache, logs and platform images; raising cache worsens it. |

<details>
<summary>Optional larger-server experiment</summary>

On an 8 vCPU / 16 GiB RAM / 300 GiB disk server with ample actual headroom,
try 2 GiB RAM cache, then 4 GiB; 100–150 GiB disk cache; and a 20–30 GiB
free-space floor. Leave Telegram request limits unchanged initially.
These are measured experiments, not portable defaults or performance guarantees.

</details>

Use **Monitor → Performance** and [performance diagnostics](performance.md)
to compare resources, provider waits, cold reads and cache reuse.

## Timezone and deployment-only settings

Timezone defaults to **Asia/Kolkata (IST)**. Use an IANA zone or UTC;
stored timestamps remain UTC. Dashboard upload-storage changes affect new
admissions immediately. Deployment-only environment changes need restart/redeploy.
See [Setup](setup.md) for credentials/storage and the
[variable reference](reference.md#configuration) for exact names and bounds.
