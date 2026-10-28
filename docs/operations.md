# Run, protect and recover TGDrive

[Docs home](README.md) · [Backup guide](backup-recovery.md)

> [!IMPORTANT]
> Keep **one app instance and one worker per drive**. Preserve the database, secrets and persistent volumes across restarts and updates.

| I need to… | Start here |
| --- | --- |
| Start, stop or inspect the app | [Run and monitor](#run-and-monitor) · [Logs](#read-useful-logs) |
| Recover interrupted work | [After a shutdown or network loss](#after-a-shutdown-crash-or-network-loss) |
| Move or reinstall | [Hosting ends](#when-a-hosting-plan-expires) · [Reopen the drive](#reopen-after-losing-the-local-installation) |
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

Check **Connections** for Telegram access, **Storage & Backups** for database,
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

Start the **same deployment**, then check **Transfers**, **Connections** and **Storage & Backups**. Reopen interrupted videos/downloads.

| Interrupted work | Next step |
| --- | --- |
| Completed file | Its MongoDB catalog and Telegram documents survive a normal restart. |
| Browser upload | Resume the existing transfer; reselect the same original if asked. A closed browser cannot send remaining bytes. |
| Telegram upload | Preserve both volumes and retry the existing job; recovery uses staged bytes and confirmed work. |
| URL import | Retry its server job in Transfers. Remote downloading may restart; expired URLs need replacement. |
| Torrent import | Retry its server job in Transfers. Keep its staging directory and job journal; completed files stay saved. See [torrent recovery](reference.md#torrent-imports). |
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

## If someone changed the Telegram channel

Keep storage bots as administrators, the channel private and auto-delete off. Repair tokens, membership or permissions in Telegram, then use **Connections → Refresh** and retry; replaced credentials require a restart.

Missing/replaced documents are detected when checked or accessed; cached bytes can temporarily hide damage. Make changes through TGDrive rather than editing Telegram messages or captions. Restore a [confirmed independent copy](backup-recovery.md#recover-a-lost-main-channel-from-a-surviving-channel), or upload the original as a new file. **A database backup cannot recreate missing Telegram bytes.** Do not change channel/database bindings to bypass the error.

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

Keep database access/name, owner login, private configuration, Telegram access,
and your [confirmed recovery file and separate key](backup-recovery.md).
Back up both volumes if unfinished work must survive losing the machine.
The recovery file contains database data and channel locations, not original media.

## When a hosting plan expires

Before server access ends, save what the replacement installation needs.
Completed files can reopen elsewhere without re-uploading if their catalog and
Telegram documents remain accessible.

1. Keep your MongoDB database accessible. If it is on the expiring server, move
   it or save a confirmed recovery file and its matching key before access ends.
   If the database is lost, [restore a confirmed backup](backup-recovery.md#restore-a-lost-database-from-telegram);
   only data captured in that restore point returns.
2. Preserve Telegram channel/bot access, private deployment configuration,
   original secrets and your off-server [recovery kit](backup-recovery.md#keep-the-mongodb-recovery-kit-outside-the-server).
3. Stop the old instance, then run TGDrive on your laptop or a new server and
   [reopen the existing drive](#reopen-after-losing-the-local-installation).
   **Do not initialize a new catalog over it.**

Sharing and background work stop until the app is reachable again. Unfinished
uploads need staged bytes or originals; caches can be rebuilt. Moving hosts
alone cannot repair missing Telegram files, a lost catalog or missing keys.

See [Keep control of your drive](hosting.md#keep-control-of-your-drive) for managed
hosting access and ownership.

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
3. Remove the example password: set `ADMIN_PASSWORD=` and leave `ADMIN_PASSWORD_HASH`, `SESSION_SECRET` and `MONGODB_BACKUP_KEY` blank/omitted to reuse saved bootstrap credentials. If unavailable, supply the originals from your recovery kit; do not generate replacements.
4. Set `COOKIE_SECURE=false` for local HTTP, `true` for HTTPS. Start normally:

   ```bash
   docker compose config --quiet
   docker compose up -d --build
   ```

5. Sign in with the existing password. Check Connections, files, a representative download and unfinished transfers.

For hosting, use [Coolify](setup.md#coolify) or [server Compose](setup.md#docker-compose-on-a-server). Retain surviving persistent volumes. If Atlas paused the cluster, [resume it](https://www.mongodb.com/docs/atlas/pause-terminate-cluster/) first.

## Encrypted Telegram backups

Save one encrypted recovery file and its separate key through
**Storage & Backups → Backups & recovery**. The same file restores lost database
data or rebuilds a lost main channel from an independent surviving backup.
Follow [Backup & recovery](backup-recovery.md) for setup and recovery steps.
Live MongoDB and confirmed Telegram backups are separate.

## Emergency database restore

[Restore a lost database](backup-recovery.md#restore-a-lost-database-from-telegram)
using the confirmed recovery file, its matching key, a surviving recorded channel
and a reachable empty replacement database. A normal reinstall reopens the
existing database instead. Keep the surviving channel intact while recovery is
verified, and switch only through explicit activation.

## Connection changes

Manage accounts in **Connections**, then restart. Keep an enabled bot with
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

| Problem | Repair |
| --- | --- |
| Revoked bot token | Replace with a valid token for the same bot; restart and retry. |
| Expired/removed Premium session | Reconnect the same account through **Connections**, restart and retry. |
| Missing channel permissions | Restore membership/admin rights, Refresh and retry. A still-connected client can resume after a successful check. |
| Network/Telegram wait | Follow reconnection, retry and cooldown status. |

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

After exposure, rotate the affected provider credential and `SESSION_SECRET` as needed. Preserve old backup keys: rotation does not re-encrypt archives. Restores/rollbacks can reintroduce older credentials or shares.

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

Login password resets do not replace the independent recovery key or unlock an
encrypted backup. Keep it separately with your
[recovery kit](backup-recovery.md#keep-the-mongodb-recovery-kit-outside-the-server).
Older files still require their original key after deliberate key rotation.

## Upgrade and rollback

Use a published release tag for a fixed version, or `main` for stable source
updates. `dev` contains ongoing work and may be unstable. Follow
[Get the source](setup.md#get-the-source) when choosing the next version.

1. Finish work, save a confirmed backup and its separate key off-host, then stop safely.
2. Record the running version and source commit; review the new version's requirements.
3. Update the source using your installation method below.
4. Run `docker compose up -d --build`, keeping server `-f` arguments, or redeploy
   in Coolify. Preserve volumes and start only one instance. Check database
   status, Connections and a representative file.

**Release archive:** [Verify and extract](release.md#verify-a-downloaded-source-archive)
into a separate folder. Copy your private `.env` and reviewed Compose changes.
Keep the **same Compose project name**, including any `-p` argument, to reuse
existing volumes. Retain the old folder for rollback. Never replace your real
configuration with `.env.example`.

**Git checkout:** Record `git rev-parse HEAD`. On `main`, use `git switch main`,
then `git pull --ff-only origin main`. For a pinned version, use
`git fetch origin --tags`, then `git switch --detach v2.2.0`, substituting the
new published tag for later releases. If Git refuses, preserve local changes
before continuing.

**Coolify:** Select the published tag or `main`, retaining private variables and mounts.

**Updating from 2.1.0:** No manual MongoDB migration is required. Native installs
need `aria2c` for torrent imports; Docker includes it. Keep older backups and their
original keys. New [unified recovery files](backup-recovery.md) and poster-aware
snapshots need a version that supports those formats. Save a compatible backup
before upgrading; older software may not preserve newer metadata.

Do not run an older app against an incompatible database format. Restore a compatible backup into a separate empty database for a data rollback.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| MongoDB cannot connect | URI/password, Atlas Network Access, cluster availability and permissions |
| Startup exits; website unavailable | Correct MongoDB configuration, authentication, network access and replica-set transaction support. A pristine database initializes automatically; existing or incomplete state requires recovery. See [setup](setup.md). |
| Another installation owns the database | Stop the old app and let its lease expire. |
| Catalog missing | Verify database name and use [recovery](backup-recovery.md); preserve existing state. |
| Connection changes pending | Restart to apply them. |
| Backup fails/decryption fails | Matching key, intact archive, posting access, disk and retry time |
| Upload cannot resume | Reselect its original in the existing transfer; missing staging may require re-upload. |
| Archive job fails | Read its error in Transfers, free temporary server storage if needed, then retry the existing job. Unsupported, encrypted or damaged archives need a corrected source; non-Docker installations also need libarchive. Earlier committed outputs remain; see [archive workflow and limits](usage.md#create-zips-and-extract-archives). |
| Video/download slow or fails | Check [performance](performance.md), connections and the Telegram document; unsupported codecs need original download. |
| Browser slow, server download fast | Check [HTTP delivery](#http-delivery-and-proxy-configuration). |
| Preload incomplete | Check effective cache tier/free space; full coverage includes integrity overhead. |
| Copied private URL fails | Use Copy download link or an explicit share; owner URLs require its cookie. |
| Revoked share still plays | Already received/buffered bytes cannot be recalled. |
| URL import asks for local file | Find/retry the original server job in Transfers. |

## Disposable media cache and preloads

Use owner controls to inspect coverage, cancel preloads, evict files or clear disposable bytes. A completed preload does not pin bytes forever; budgets, headroom and eviction can reduce coverage. See [cache policy and defaults](performance.md).

When stopped, `DATA_DIR/media-cache` can be removed and rebuilt on demand. **Never clear upload receipts, staged payloads or backup sources as cache.** Host access can expose cache contents; HTTP cache hits still require current authorization.

### Tune a busy installation

Use the [load and capacity guide](performance-tuning.md) to investigate queues,
cache reuse and server/bot capacity before increasing limits.

Check headroom and queues first, stop unnecessary preloads, then change one limit at a time. Compare warm/cold and shared/unrelated-file reads. Supported web preferences apply live; environment changes require recreation, credentials require restart.

## HTTP delivery and proxy configuration

TGDrive sends `Alt-Svc: clear`; supported Compose configurations apply it at the edge too. This clears cached alternative-service advertisements so browsers use normal origin HTTPS, ordinarily HTTP/2 through Coolify/Traefik. No extra variable or manual rule is needed.

<details>
<summary>External proxies and existing custom rules</summary>

Traefik can append its own advertisement, so the Compose middleware ensures the final edge response carries the policy. An external CDN/proxy must preserve it, range/length headers and streaming delivery. Avoid compression of byte-addressed responses; TGDrive supplies `Content-Encoding: identity`.

Remove temporary custom rules once the updated app/Compose supplies the policy. Redundant routers can break when domains/services change. Coolify watches dynamic files; main proxy Compose edits need restart and must be saved through its UI.

The default follows one reproduced HTTP/3 delivery slowdown, not a universal QUIC claim: see the [validation report](streaming-validation-2026-10-18.md). References: [Coolify Traefik](https://coolify.io/docs/core/networking/proxy/traefik/overview), [Traefik HTTP/3](https://doc.traefik.io/traefik/reference/install-configuration/entrypoints/#http3), [Alt-Svc specification](https://www.rfc-editor.org/rfc/rfc7838.html#section-3.1).

</details>
