# Run, protect and recover TGDrive

[Docs home](README.md) · [Backup & recovery](backup-recovery.md) · [Configuration](configuration.md)

> [!IMPORTANT]
> Keep **one app instance and one worker per drive**. Preserve the database, secrets and persistent volumes across restarts and updates.

| I need to… | Start here |
| --- | --- |
| Start, stop or inspect the app | [Run and monitor](#run-and-monitor) · [Logs](#read-useful-logs) |
| Recover interrupted work | [After a shutdown or network loss](#after-a-shutdown-crash-or-network-loss) |
| Move or reinstall | [Move to another host](#move-to-another-host) · [Reopen the drive](#reopen-after-losing-the-local-installation) |
| Fix lost access | [Connections](#connection-changes) · [Password reset](#forgotten-login-password) |
| Update the app | [Upgrade and rollback](#upgrade-and-rollback) |
| Diagnose an error | [Troubleshooting](#troubleshooting) · [Backup & recovery](backup-recovery.md) |

## Run and monitor

Run these from the installation folder.

| Action | Local Docker command |
| --- | --- |
| Start | `docker compose up -d` |
| Stop safely | `docker compose stop` |
| Restart | `docker compose restart app` |
| Recent logs | `docker compose logs --tail=100 app` |

Server deployments add `-f compose.yaml -f compose.server.yaml` after
`docker compose` in every command. In Coolify, use Stop, Restart, Logs and Redeploy.
Keep Docker running while TGDrive is in use.

Check **Connections** for Telegram access, **Storage and Backups** for database,
disk and backups, and **Performance** for queues, caches and cooldowns.
Queue times are estimates. `/health/ready` does not test a full file or public domain.

Accepted server jobs continue with the browser closed while TGDrive, MongoDB and
Telegram stay available. A server can keep them running while your laptop is off.

### Read useful logs

Keep `LOG_LEVEL=INFO` for normal use. Match `id`/`job_id` across log events to
follow a transfer. For failures, also read the error and suggested repair in
**Transfers**. Normal entries omit filenames, destinations and source URLs;
routine byte updates and successful polling are quiet.

<details>
<summary>Transfer events and timing fields</summary>

| Event or field | What it tells you |
| --- | --- |
| `Device upload accepted` | Verified server bytes were handed to a durable job; the browser is no longer needed for that file. |
| `receipt_age_seconds` | Time since this receipt was created, including browser pauses and server handoff. Older receipts can report `unknown`. |
| `verify_seconds`, `handoff_seconds` | Verification time and total completion-request time, including waiting for its receipt lock and committing the job. These overlap. |
| `Transfer started`, `Transfer finished` | Bytes, time before first processing and total job lifetime, including waits. |
| `Transfer retry scheduled` or `Transfer waiting` | The cooldown or unavailable Telegram client, automatic retry policy and suggested action. Unchanged waits are not repeated. |
| `Transfer failed` | Job ID, phase, exception type, stable failure category and suggested repair. Inspect the user-facing error in Transfers. |
| Backups, connections, logins, shares, Trash | Saved, skipped or failed backups; which Telegram accounts connected; failed or blocked logins; share links created or revoked (by ID, never token); items trashed, restored or queued for deletion. Passwords and client addresses are never logged. |
| `Automatic tuning` | A resource budget or CPU-based preload cap changed, and why. Telegram account cooldown/recovery is separate from resource sizing. |

</details>

For a reproduced problem, temporarily set `LOG_LEVEL=DEBUG` and recreate/redeploy.
It adds phase changes, tracebacks and provider details; return to INFO afterwards.
Provider warnings/errors remain visible at INFO. Logs go to the process/container
output. Raw HTTP access logs stay disabled because URLs can contain share
credentials. **Review logs before sharing**, even with automatic redaction.

<details>
<summary>Restart policy</summary>

The laptop override leaves TGDrive stopped after reboot. Server restart policy handles process exits; an `unhealthy` check alone does not restart a running container. Inspect logs before restarting. See [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/).

</details>

## After a shutdown, crash or network loss

Start the **same deployment**, then check **Transfers**, **Connections** and **Storage and Backups**. Reopen interrupted videos/downloads.

| Interrupted work | Next step |
| --- | --- |
| Completed file | Its MongoDB [catalog](reference.md#terms-used-here) and Telegram documents survive a normal restart. |
| Browser upload | Resume the existing transfer; reselect the same original if asked. A closed browser cannot send remaining bytes. |
| Telegram upload | Preserve both volumes and retry the existing job; recovery uses staged bytes and confirmed work. |
| URL import | Retry its server job in Transfers. Remote downloading may restart; expired URLs need replacement. |
| Torrent import | Retry its server job in Transfers. Keep its [staging](reference.md#terms-used-here) directory and job journal; completed files stay saved. See [torrent recovery](api.md#torrent-imports). |
| Telegram outage | Restore connectivity and observe the displayed retry/cooldown time; restarting does not bypass it. |
| Database/ownership failure | Restore MongoDB access and stop competing instances before restarting. |
| Backup/channel copy | Check its status. Only the last confirmed restore point is protected. |
| Full disk | Free unrelated space or disposable cache; keep staged uploads and journals. |

Unfinished upload sessions normally expire after 24 hours of inactivity;
longer interruptions can require a fresh upload. Restart cannot recover deleted
file bytes, missing keys or revoked credentials.

<details>
<summary>Ownership leases and unavailable MongoDB</summary>

After an unclean stop, stop the old app, wait about a minute, then restart once.
Never delete internal ownership records or disable single-instance protection.

Unreachable MongoDB or insufficient permissions rejects startup and owner login.
Restore database access first: local recovery configuration provides no offline
login. A reachable database with a missing/corrupt catalog can permit the owner
recovery screen.

</details>

If a running instance has lost database ownership, restoring network access
does not reclaim its lease or resume writes. Stop competing instances, restore
MongoDB access and restart the same deployment once. Telegram transport repair
does not remove this ownership fence.

## If someone changed the Telegram channel

Keep storage bots as administrators, the channel private and auto-delete off. Repair tokens, membership or permissions in Telegram, then use **Connections → Refresh** and retry; replaced credentials apply when you save.

Missing/replaced documents are detected when checked or accessed; cached bytes can temporarily hide damage. Make changes through TGDrive rather than editing Telegram messages or captions. Restore a [confirmed independent copy](backup-recovery.md#main-channel-lost), or upload the original as a new file. **A database backup cannot recreate missing Telegram bytes.** Do not change channel/database bindings to bypass the error.

<details>
<summary>Channel checks</summary>

Guarded Telegram writes check channel privacy and usable bots' permissions. Successful checks are reused for up to 30 seconds; failures block affected work and appear in Connections. This is not a continuous channel audit or a lock on Telegram settings.

</details>

## What is saved where

| Location | Contents |
| --- | --- |
| Telegram | Completed files/parts and encrypted backups |
| MongoDB | Catalog, shares, settings, transfers and recovery state |
| Persistent data/cache volumes | Secrets, sessions, recovery state, unfinished uploads and disposable cache |
| Private `.env` | Database access and deployment configuration |

> [!WARNING]
> **`docker compose down -v` removes persistent volumes**, risking secrets and unfinished uploads. Use `docker compose stop` to stop safely.

Keep the same Compose project name to reuse its volumes. Deleting the app folder
alone does not delete Docker volumes, Telegram documents or MongoDB records;
preserve its private configuration so you can reconnect safely.

### Save these somewhere separate

Keep the [off-host recovery material](backup-recovery.md#save-off-host-recovery-material),
including the original `ENCRYPTION_KEY` and a confirmed downloaded backup.
Back up both volumes if unfinished work must survive losing the machine.
The recovery file contains database data and channel locations, not original media.

## Move to another host

Before server access ends, save what the replacement installation needs.
Completed files can reopen elsewhere without re-uploading if their catalog and
Telegram documents remain accessible.

1. Keep your MongoDB database accessible. If it is on the old server, move
   it or save a confirmed recovery file and the original `ENCRYPTION_KEY` before access ends.
   If the database is lost, [restore a confirmed backup](backup-recovery.md#restore-after-losing-mongodb);
   only data captured in that restore point returns.
2. Preserve Telegram channel/bot access, private deployment configuration,
   original secrets and your [off-host recovery material](backup-recovery.md#save-off-host-recovery-material).
3. Stop the old instance, then run TGDrive on your laptop or a new server and
   [reopen the existing drive](#reopen-after-losing-the-local-installation).
   **Do not initialize a new catalog over it.**

Sharing and background work stop until the app is reachable again. Unfinished
uploads need staged bytes or originals; caches can be rebuilt. Moving hosts
alone cannot repair missing Telegram files, a lost catalog or missing keys.

See [Keep control of your drive](hosting.md#keep-control-of-your-drive) for access
and recovery essentials.

## Reopen after losing the local installation

If MongoDB survives, reconnect to the existing database. Missing staged upload
bytes still need their originals.

1. Stop the old instance. Use the same version or source commit where possible and
   [copy the configuration template](setup.md#3-configure-your-drive) using the
   command for your system. Use the existing values below.

2. Set the **existing** `MONGODB_URI`, `MONGODB_DATABASE`, `API_ID`, `API_HASH` and
   `STORAGE_CHANNEL`. Keep the original `MAIN_BOT_TOKEN` and `TELEGRAM_ADMIN_IDS`
   if configured. Older `BOT_TOKENS` configurations remain supported; do not
   replace an existing list just to reinstall. Saved Connections are reused.
3. Remove the example password: set `ADMIN_PASSWORD=` and leave `ADMIN_PASSWORD_HASH` and `SESSION_SECRET` blank/omitted to reuse saved bootstrap credentials. Keep the original `ENCRYPTION_KEY`; do not generate a replacement.
   This applies only while the original MongoDB survives. Empty-database recovery
   needs an explicit owner password/hash; see the [recovery checklist](backup-recovery.md#save-off-host-recovery-material).
4. Set `COOKIE_SECURE=false` for local HTTP, `true` for HTTPS. Start normally:

   ```bash
   docker compose config --quiet
   docker compose up -d --build
   ```

5. Sign in with the existing password. Check Connections, files, a representative download and unfinished transfers.

For hosting, use [Coolify](setup.md#coolify) or [server Compose](setup.md#docker-compose-on-a-server). Retain surviving persistent volumes. If Atlas paused the cluster, [resume it](https://www.mongodb.com/docs/atlas/pause-terminate-cluster/) first.

## Backups and database restore

Recovery files, backup channels and database restore are covered in
[Backup & recovery](backup-recovery.md). A normal reinstall reopens the existing
database; restore only when MongoDB is lost, and switch only through explicit
activation.

## Connection changes

Manage accounts in **Connections**; saves apply at once, without a restart. Keep an enabled bot with
channel permissions. **Changing channel ID does not move files**; a different
drive needs its own database.

`MAIN_BOT_TOKEN` separately controls the Telegram chat interface. If a bot serves
both storage and chat uploads and its token changes, update its saved token in
**Connections** and `MAIN_BOT_TOKEN` in the deployment, then restart/redeploy.
Changing the deployment variable alone does not replace saved storage credentials.

Finish or cancel unfinished/failed transfers before changing accounts, enabled
states or API credentials. With work pending, repair only the token/session for
the **same account**; TGDrive verifies its identity.

### Revoked tokens, removed sessions and lost channel access

After its three native Telegram connection retries finish, TGDrive automatically
retries transient transport failures with a normal 5–30 second backoff. Account cooldowns and
provider FloodWaits can extend the wait beyond 30 seconds. Each connect and
same-account verification attempt has a 45-second timeout; native connection
cleanup has no hard deadline, so these are not total recovery-time bounds.

Repair reuses the existing session and verifies the original account before
resuming bot actions. It does not sign in again, replace credentials, clear
account cooldowns/quarantine or retry failed upload jobs automatically. Check
**Connections** and **Transfers** after connectivity returns; preserve staged
bytes and retry the existing job when needed.

| Problem / account status code | Repair |
| --- | --- |
| Revoked bot token (`TELEGRAM_CREDENTIALS_REVOKED`) | Replace with a valid token for the same bot, then retry. |
| Expired/removed Premium session (`TELEGRAM_CREDENTIALS_REVOKED`) | Reconnect the same account through **Connections**, then retry. Changed identity also requires verification of the original account. |
| Missing channel permissions | Restore membership/admin rights, Refresh and retry. A still-connected client can resume after a successful check. |
| Network/Telegram wait (`TELEGRAM_RECONNECTING`) | Automatic transport retry is in progress. Restore connectivity and follow retry/cooldown status. |
| Other verification failure (`TELEGRAM_CONNECTION_UNAVAILABLE`) | Automatic repair stopped. Open Connections and verify or refresh the account. |

<details>
<summary>Why an upload may need its original account</summary>

Failed accounts are excluded from new work; healthy accounts can serve eligible reads. An upload part can switch accounts only before dispatch. Afterwards TGDrive preserves its sender, request ID and staged bytes to prevent duplicate sends after a lost acknowledgement. If that sender is revoked, repair it rather than repeatedly restarting. Keep the staging and journal if the account cannot be restored.

The independent backup bot has separate credentials and permissions; check its [backup status](backup-recovery.md) too.

</details>

## HTTPS and private configuration

For public access, use an HTTPS reverse proxy, `COOKIE_SECURE=true` and a private
app port. Trust only your actual proxy's addresses. Preserve authorization/cache
headers; do not let a proxy/CDN cache private responses or share URLs.
Keep `ALLOW_PRIVATE_REMOTE_URLS=false` unless internal-network imports are intentional.

### Can TGDrive run over HTTP?

**On localhost:** the local Compose setup uses `http://127.0.0.1:8000` with
`COOKIE_SECURE=false`. Browsers allow the required APIs on
[loopback addresses](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Secure_Contexts).

**From another device, including on a LAN: use HTTPS.** Plain HTTP can break
upload IDs, resume verification and media actions, and exposes passwords,
sessions and traffic. `COOKIE_SECURE=false` does not fix those browser requirements.
Hosted Compose explicitly enables secure cookies; `.env` alone cannot override it.

Use your own domain with a valid certificate. Coolify's generated
[`sslip.io` address](https://coolify.io/docs/core/networking/domains) uses HTTP;
shared-domain [certificate issuance limits](https://letsencrypt.org/docs/rate-limits/)
can prevent HTTPS setup. They do not limit requests once a valid certificate exists.

### Protect a public deployment

Use a password-manager-generated password or unique passphrase of at least 15
characters, and protect Telegram/MongoDB accounts. TGDrive accepts a 12-character
minimum and has no built-in second factor or passkeys. Public deployments should
also limit login traffic at the reverse proxy; the app's per-IP throttle resets
after restart. Keep secrets and share links out of Git/logs; review even redacted
logs before sharing. See [security boundaries](reference.md#security-and-privacy).

<details>
<summary>Change credentials and verify public access</summary>

Follow [password reset](#forgotten-login-password), or set `ADMIN_PASSWORD_HASH`
with `ADMIN_PASSWORD` empty. Verify the new login works and the old
password/session fails. Restarts with unchanged credentials preserve sessions.
Revoke shares separately in **Shared Links**.

After exposure, rotate the affected provider credential and `SESSION_SECRET` as
needed. Preserve `ENCRYPTION_KEY` and older archives' matching keys; changing a
login or provider credential does not re-encrypt files. Restores/rollbacks can
reintroduce older credentials or shares.

Reusing an old owner credential together with its old `SESSION_SECRET` can make
unexpired old cookies valid again. Choose a fresh signing secret when prior
sessions must remain invalid, and review shared links separately.

After HTTPS setup, test owner login/upload, denied private access in an incognito browser, a scoped share and its revocation. Confirm the direct app port is unreachable publicly.

</details>

## Forgotten login password

TGDrive has no email-based reset or way to reveal a password from its hash. You
can reset access if you control the deployment's private environment or `.env`:

1. Set a new private `ADMIN_PASSWORD` of at least 12 characters and clear
   `ADMIN_PASSWORD_HASH`. TGDrive hashes the password internally. An empty hash
   value is sufficient. Keep the replacement explicitly configured; missing owner
   credentials can reuse the original hash saved during setup.
2. Recreate the app container with `docker compose up -d --force-recreate app`,
   or update the private variables and **Redeploy** in Coolify. Use your existing
   Compose project, volumes and any server `-f` arguments. Do not initialize or
   delete the database or volumes.
3. Confirm the new password works in a private browser window and the old
   password/session fails. Changing the effective credential invalidates that
   password's existing browser sessions after restart. Shared links are separate.

For the demo's private full-access password, set `DEMO_ADMIN_PASSWORD` and clear
`DEMO_ADMIN_PASSWORD_HASH` in the deployment environment, then recreate/redeploy.
It does not change the visitor password. Rotating or removing this credential
invalidates elevated demo sessions; ordinary visitor sessions remain independent.
Rotate `SESSION_SECRET` too when both session types must be invalidated, and
avoid restoring old credentials and signing secrets together.

If you also lost access to the server, recover the hosting/deployment account
first. TGDrive cannot reset a Telegram, MongoDB or hosting provider account.
See [password and session storage](reference.md#owner-authentication).

Login password resets do not change `ENCRYPTION_KEY` or affect backups.

## Upgrade and rollback

Use a published release tag for a fixed version, or `main` for stable source
updates. `dev` contains ongoing work and may be unstable. Follow
[Get the source](setup.md#get-the-source) when choosing the next version.

1. Finish work, save a confirmed backup off-host, then stop safely.

> [!IMPORTANT]
> **Upgrading from 2.0–2.2:** add `ENCRYPTION_KEY` to `.env` first; 2.2 had none.
> Keep the same MongoDB, channel and volumes. On first start, 2.3 upgrades the
> drive in place: see [what the 2.2 upgrade changes](#upgrade-from-22-or-earlier).
2. Record the running version and source commit; review the new version's requirements.
3. Update the source and Compose files to the selected branch or release tag,
   preserving your local edits and deployment changes. Build that checkout with
   `docker compose up -d --build`, keeping server `-f` arguments, or redeploy
   the selected revision in Coolify. Preserve volumes and start only one
   instance. Check database status, Connections and a representative file.

**Release archive:** [Verify and extract](release.md#verify-a-downloaded-source-archive)
into a separate folder. Copy your private `.env` and reviewed Compose changes.
Keep the **same Compose project name**, including any `-p` argument, to reuse
existing volumes. Retain the old folder for rollback. Never replace your real
configuration with `.env.example`.

**Git checkout:** Record `git rev-parse HEAD`. On `main`, use `git switch main`,
then `git pull --ff-only origin main`. To use the latest potentially unstable
source, use `git switch dev` and `git pull --ff-only origin dev` if that branch
is available locally. For a pinned version, use
`git fetch origin --tags`, then `git switch --detach vX.Y.Z`, substituting the
selected published application tag. If Git refuses, preserve local changes
before continuing.

**Coolify:** Select `main`, `dev` or a published tag, retaining private variables
and mounts; redeploy builds that revision. Unpushed local edits are not available
to a remote Git deployment.

Native installs need `aria2c` for torrent imports; Docker includes it.

Do not run an older app against an incompatible database format. Restore a compatible backup into a separate empty database for a data rollback.

### Upgrade from 2.2 or earlier

1. Save a confirmed 2.2 backup, its matching old recovery key and original
   configuration off the server, then stop 2.2. Retain the old independent copy
   until a new restore point is verified.
2. Generate a key with `openssl rand -hex 32`, add it as `ENCRYPTION_KEY` in
   `.env` and [save a copy off the server](setup.md#save-your-encryption-key).
3. Start 2.3 with the same `.env`, MongoDB, channel and Compose project name.
   The log shows `Upgrading the TGDrive 2.2 catalog` and then
   `TGDrive 2.2 catalog upgrade finished`.
4. Check a few files, folders, shares and **Connections**.
5. If 2.2 had an independent backup channel, set it up again in
   [Storage and Backups](backup-recovery.md#add-a-backup-channel).

| What happens | Detail |
| --- | --- |
| Safety copy first | Supported recovery collections, runtime and replica records are written to a unique `DATA_DIR/upgrade-2.2-<time>-<random>.json.gz` with mode `0600`. The file and directory are synced before migration; retries reuse the recorded original. It includes sensitive raw settings, omits optional telemetry and is not a `.tgdb` import. Keep it private; it does not replace an off-host 2.2 backup. |
| Kept | Files, folders, Trash, stars, shares, connections, settings, sessions and share links. `SESSION_SECRET` is kept, so old links still work. |
| Added | Search words, type groups, drive totals, record expiry and the encryption-key check. |
| Removed | The saved 2.2 `MONGODB_BACKUP_KEY` and an unused index. Current backup keys derive from `ENCRYPTION_KEY`; keep the old key for old archives. |
| Backup channel | 2.2's independent channel progress can't be read by 2.3, so it starts from scratch. Its messages stay in Telegram; the old files are renamed `channel-backup.sqlite3.2.2-unused` and `replica-bootstrap.json.2.2-unused`. |
| Telegram files | Not touched. Files uploaded by 2.2 stay unencrypted and readable; new uploads are encrypted. |
| Interrupted | The version is raised last, so the upgrade runs again on the next start. On an upgraded drive it does nothing. |

If a retry reports a missing or unsafe migration copy, stop and preserve the
database and volumes. Recover the original file at its recorded path with
owner-only permissions before restarting. Do not replace it with a dump taken
after a partial migration or remove the recorded copy name to bypass the error.

Do not run 2.2 against the upgraded database. For rollback, keep the original
2.2 archive and matching key for a separate version-compatible restore. An
isolated old-archive restore uses tagged v2.2 and its original key before
upgrading the restored database. Rehearse with the actual deployment versions;
[archive recovery](backup-recovery.md#upgrade-or-restore-an-older-drive) is
separate from the surviving-database upgrade.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| MongoDB cannot connect | URI/password, Atlas Network Access, cluster availability and permissions |
| Startup exits; website unavailable | Correct MongoDB configuration, authentication, network access and replica-set transaction support. A pristine database initializes automatically; existing or incomplete state requires recovery. See [setup](setup.md). |
| Another installation owns the database | Stop the old app and let its lease expire. |
| Catalog missing | Verify database name and use [recovery](backup-recovery.md); preserve the database, volumes, secrets and unfinished bytes. Do not clear a volume to bypass recovery. |
| Old transfer stuck after a fresh install | It is saved in this browser only. Cancel marks it cancelled when the server no longer knows it. |
| A saved account shows an error | It could not connect. Fix the token, session or channel permissions, then use **Connections → Refresh**. |
| Backup fails/decryption fails | Original `ENCRYPTION_KEY`, compatible intact archive, posting/pinning access, disk and retry time; old archives need their old key and version |
| `ACTIVATION_RESOURCE_LIMIT` | Preserve the rejected pending record, database, volumes and keys. Follow the separate-deployment procedure only with an intact compatible archive; [prerequisites and limits](backup-recovery.md#restore-after-losing-mongodb). |
| Upload cannot resume | Reselect its original in the existing transfer; missing staging may require re-upload. |
| Archive job fails | Read its error in Transfers, free temporary server storage if needed, then retry the existing job. Unsupported, encrypted or damaged archives need a corrected source; non-Docker installations also need libarchive. Earlier committed outputs remain; see [archive workflow and limits](usage.md#create-zips-and-extract-archives). |
| Video/download slow or fails | Check [performance](performance.md), connections and the Telegram document; unsupported codecs need original download. |
| Browser slow, server download fast | Check [HTTP delivery](#http-delivery-and-proxy-configuration). |
| Preload incomplete | Check effective cache tier/free space; full coverage includes integrity overhead. |
| Copied private URL fails | Use Copy download link or an explicit share; owner URLs require its cookie. |
| Revoked share still plays | Already received/buffered bytes cannot be recalled. |
| URL import asks for local file | Find/retry the original server job in Transfers. |

## Media cache and slow installations

The media cache is disposable: when TGDrive is stopped, `DATA_DIR/media-cache`
can be removed and rebuilt on demand. Use **Monitor → Preloads** to inspect,
cancel or evict cached files.

> [!WARNING]
> Never clear upload receipts, staged payloads or backup sources as cache.

For queues, cache reuse and server or bot capacity, follow the
[load and capacity guide](performance-tuning.md).

## HTTP delivery and proxy configuration

TGDrive sends `Alt-Svc: clear`; the supplied Compose middleware also sets it at
the Coolify/Traefik edge to clear cached alternative-service advertisements.
An external CDN can apply its own protocol policy. Inspect the browser's actual
connection protocol; a response header alone does not establish it.

<details>
<summary>External proxies and existing custom rules</summary>

Traefik can append its own advertisement, so the Compose middleware ensures the final edge response carries the policy. An external CDN/proxy must preserve it, range/length headers and streaming delivery. Avoid compression of byte-addressed responses; TGDrive supplies `Content-Encoding: identity`.

Remove temporary custom rules once the updated app/Compose supplies the policy. Redundant routers can break when domains/services change. Coolify watches dynamic files; main proxy Compose edits need restart and must be saved through its UI.

The default follows one reproduced slowdown, not a universal QUIC claim: on 18 October 2026 the same 17.8 MB MP4 took 93.7 seconds over HTTP/3 and 1.0 second over HTTP/2 on the tested connection. References: [Coolify Traefik](https://coolify.io/docs/core/networking/proxy/traefik/overview), [Traefik HTTP/3](https://doc.traefik.io/traefik/reference/install-configuration/entrypoints/#http3), [Alt-Svc specification](https://www.rfc-editor.org/rfc/rfc7838.html#section-3.1).

For a Cloudflare-proxied deployment with slow playback, compare the same ranges
and cache state over HTTP/2 and HTTP/3, inspecting browser timing and origin
logs. Cloudflare's [HTTP/3 setting](https://developers.cloudflare.com/speed/optimization/protocol/http3/)
controls the visitor-to-Cloudflare connection, not HTTP/3 at the TGDrive origin.
Change it only when that comparison identifies a problem.

TGDrive's resumable uploads send **8 MiB per request**, below Cloudflare Free's
documented **100 MB** ceiling. A lower configured body limit, rate limit or
timeout can still reject them ([Cloudflare 413 guidance](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/)).
For failed uploads, inspect the failing response, Cloudflare Security Events and
origin logs. If a specific WAF rule is responsible, scope any
[exception](https://developers.cloudflare.com/waf/custom-rules/skip/) to that
hostname, method and receipt path, keeping logging and login protection. Do not
apply a blanket upload-path security bypass.

</details>
