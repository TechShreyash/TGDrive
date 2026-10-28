# Run, protect and recover TGDrive

[Docs home](README.md) · [Backup guide](backup-recovery.md)

> [!IMPORTANT]
> Keep **one app instance and one worker per drive**. Preserve the database, secrets and persistent volumes across restarts and updates.

**Contents**

- [Run and monitor](#run-and-monitor)
  - [Read useful logs](#read-useful-logs)
- [After a shutdown, crash or network loss](#after-a-shutdown-crash-or-network-loss)
- [If someone changed the Telegram channel](#if-someone-changed-the-telegram-channel)
- [What is saved where](#what-is-saved-where)
- [When a hosting plan expires](#when-a-hosting-plan-expires)
- [Reopen after losing the local installation](#reopen-after-losing-the-local-installation)
- [Encrypted Telegram backups](#encrypted-telegram-backups)
- [Emergency database restore](#emergency-database-restore)
- [Connection changes](#connection-changes)
  - [Revoked tokens, removed sessions and lost channel access](#revoked-tokens-removed-sessions-and-lost-channel-access)
- [HTTPS and private configuration](#https-and-private-configuration)
  - [Can TGDrive run over HTTP?](#can-tgdrive-run-over-http)
- [Forgotten login password](#forgotten-login-password)
- [Upgrade and rollback](#upgrade-and-rollback)
- [Troubleshooting](#troubleshooting)
- [Disposable media cache and preloads](#disposable-media-cache-and-preloads)
  - [Tune a busy installation](#tune-a-busy-installation)
- [HTTP delivery and proxy configuration](#http-delivery-and-proxy-configuration)

## Run and monitor

[Preloads and Performance screenshots](showcase.md#preload-and-monitor) show
where to inspect cache coverage, traffic and server resources in the website.

Run these from the installation folder.

| Action | Local Docker command |
| --- | --- |
| Start | `docker compose up -d` |
| Stop safely | `docker compose stop` |
| Restart | `docker compose restart app` |
| Recent logs | `docker compose logs --tail=100 app` |

Server deployments add `-f compose.yaml -f compose.server.yaml` after `docker compose` in every command. Coolify provides Stop, Restart, Logs and Redeploy controls. Keep Docker Desktop/the Docker daemon running while TGDrive is in use.

Check **Connections** for Telegram health, **Storage & Backups** for database/disk/backups, and [performance controls](performance.md) for caches, queues and cooldowns. Queue times are estimates. `/health/ready` reports readiness, not a full file or public-domain check.

An always-on server can keep file access, sharing and
[background work](hosting.md#local-computer-or-server) available while your laptop
is off. TGDrive must keep running and connected to MongoDB and Telegram; closing
the browser does not stop accepted server jobs.

### Read useful logs

Keep `LOG_LEVEL=INFO` for normal operation. Logs show startup/shutdown, accepted
device uploads and transfer starts/completion/cancellation. Match the same `id`
across events (`job_id` links receipt acceptance to its server job) to follow one
file without logging its name, destination or source
URL. Byte callbacks, successful polling and routine Telegram update messages do
not produce normal-operation entries.

| Event or field | What it tells you |
| --- | --- |
| `Device upload accepted` | Verified server bytes were handed to a durable job; the browser is no longer needed for that file. |
| `receipt_age_seconds` | Time since this receipt was created, including browser pauses and server handoff. Older receipts can report `unknown`. |
| `verify_seconds`, `handoff_seconds` | Verification time and total completion-request time, including waiting for its receipt lock and committing the job. These overlap. |
| `Transfer started`, `Transfer finished` | Bytes, time before first processing and total job lifetime, including waits. |
| `Transfer retry scheduled` or `Transfer waiting` | The cooldown or unavailable Telegram client, automatic retry policy and suggested action. Unchanged waits are not repeated. |
| `Transfer failed` | Job ID, phase, exception type, stable failure category and suggested repair. Inspect the user-facing error in Transfers. |

Provider warnings/errors remain visible. For a reproduced problem, temporarily
set `LOG_LEVEL=DEBUG` in private configuration and recreate/redeploy the app.
DEBUG adds phase transitions, exception tracebacks and provider diagnostics;
return to INFO afterwards. Raw HTTP access logs stay disabled because query
strings can contain share credentials. Logs go to the container/process output,
with conservative credential redaction; review diagnostic logs before sharing.

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

Browser receipts normally expire after 24 hours of inactivity; longer interruptions can require a fresh upload. Restart cannot recover deleted payloads, missing keys or revoked credentials.

<details>
<summary>Ownership leases and unavailable MongoDB</summary>

After an unclean stop, stop the old app, wait about a minute, then restart once. Never delete internal ownership records or disable single-instance protection.

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

Keep database access/name, owner login, private deployment configuration, Telegram access and the matching [backup keys and confirmed archives](backup-recovery.md). Back up both volumes if unfinished work must survive losing the machine. Keys decrypt their matching archives; they do not contain file bytes.

## When a hosting plan expires

**Your drive stays yours.** Server or managed-plan expiry does not itself delete
completed Telegram files or prevent running TGDrive elsewhere. Reopening the same
catalog with access to its Telegram documents does not require re-uploading them.

1. Keep your MongoDB database accessible. If it is on the expiring server, move
   it or save a confirmed encrypted archive and matching key before access ends.
   If the database is lost, [restore a confirmed backup](backup-recovery.md#restore-a-lost-database-from-telegram);
   only data captured in that restore point returns.
2. Preserve Telegram channel/bot access, private deployment configuration,
   original secrets and your off-server [recovery kit](backup-recovery.md#keep-the-mongodb-recovery-kit-outside-the-server).
3. Stop the old instance, then run TGDrive on your laptop or a new server and
   [reopen the existing drive](#reopen-after-losing-the-local-installation).
   **Do not initialize a new catalog over it.**

Hosting expiry interrupts sharing and background work until the app is reachable
again. Unfinished uploads still need staged server bytes or their originals;
disposable caches/preloads can be rebuilt. Missing Telegram originals, a lost
catalog or missing recovery keys cannot be repaired by changing hosts alone.

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

Use [Backup & recovery](backup-recovery.md) to set up independent file copies, save matching keys and rehearse recovery. Live MongoDB and confirmed Telegram backups are separate.

## Emergency database restore

[Restore a lost database from Telegram](backup-recovery.md#restore-a-lost-database-from-telegram) requires a confirmed archive, matching key and empty destination database. A normal reinstall reopens the existing database instead.

## Connection changes

Manage accounts in **Connections**, then restart. Keep an enabled bot with channel permissions. Changing channel ID does not move files; a different drive needs its own database.

`MAIN_BOT_TOKEN` separately controls the Telegram chat interface. If a bot serves
both storage and chat uploads and its token changes, update its saved token in
**Connections** and `MAIN_BOT_TOKEN` in the deployment, then restart/redeploy.
Changing the deployment variable alone does not replace saved storage credentials.

Finish/cancel unfinished or failed transfers before changing account identities, enabled states or API credentials. While work is pending, credential repair must replace only the token/session for the **same account**, leaving those other settings unchanged. TGDrive verifies its identity.

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

For public access, use an HTTPS reverse proxy, `COOKIE_SECURE=true`, a private app port and trusted-proxy settings limited to your actual proxy. Preserve authorization/cache headers; avoid proxy/CDN caching of private responses or share URLs. Keep `ALLOW_PRIVATE_REMOTE_URLS=false` unless internal-network imports are intentional.

### Can TGDrive run over HTTP?

**Yes, on localhost.** The local Compose setup serves
`http://127.0.0.1:8000` with `COOKIE_SECURE=false`. Browsers treat localhost and
loopback addresses as [secure contexts](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Secure_Contexts),
so the required browser APIs remain available.

**Use HTTPS when opening TGDrive from another device**, including over a LAN.
The current frontend uses secure-context browser APIs for upload IDs, resume
verification and media actions. An ordinary HTTP server IP or `sslip.io` address
does not qualify; these features can fail even with `COOKIE_SECURE=false`.
Plain HTTP also leaves passwords, sessions and file traffic unencrypted.
The hosted Compose file sets secure cookies explicitly; a dashboard or `.env`
value alone does not override that setting.

[Coolify's generated `sslip.io` address](https://coolify.io/docs/core/networking/domains)
uses HTTP. Automatic HTTPS certificate issuance for the shared domain can hit
[Let's Encrypt certificate limits](https://letsencrypt.org/docs/rate-limits/).
These limits affect **creating certificates**, rather than requests to your
TGDrive pages or files. A valid certificate can serve HTTPS normally; use your own
domain with a valid certificate for public TGDrive access.

### Protect a public deployment

Use a password-manager-generated password or unique passphrase of at least 15
characters, and protect Telegram/MongoDB accounts. TGDrive accepts a 12-character
minimum and has no built-in second factor or passkeys. Public deployments should
also limit login traffic at the reverse proxy; the app's per-IP throttle resets
after restart. Keep secrets and share links out of Git/logs; review even redacted
logs before sharing. See [security boundaries](reference.md#security-and-privacy).

<details>
<summary>Change credentials and verify public access</summary>

Stop the app, update the private credential and recreate/redeploy. Use `ADMIN_PASSWORD_HASH` with `ADMIN_PASSWORD` empty, or remove a stale hash when using plaintext fallback. Verify the new login works and the old password/session fails. Ordinary restarts with unchanged credentials preserve sessions. Revoke share links separately in **Shared Links**.

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

## Upgrade and rollback

Use a published release tag for a fixed version, or `main` for stable source
updates. `dev` contains ongoing work and may be unstable. Follow
[Get the source](setup.md#get-the-source) when choosing the next version.

- [ ] Finish work, stop safely, and save a confirmed backup plus its key off-host.
- [ ] Record the running version and source commit; review the target version's requirements.
- [ ] **Release archive:** [verify and extract](release.md#verify-a-downloaded-source-archive) the new archive into a separate folder. Copy your private `.env` into it and retain any reviewed Compose customizations. Keep the **same Compose project name**, including any `-p` argument, so Docker reuses the existing volumes. Keep the old folder for rollback; never copy the new `.env.example` over your real configuration.
- [ ] **Git checkout:** record `git rev-parse HEAD`. For a `main` clone, use `git switch main`, then `git pull --ff-only origin main`. For a version-pinned checkout, use `git fetch origin --tags`, then `git switch --detach v2.0.0` (choose the new published tag for later updates). Preserve local changes if Git refuses the switch or update. **Coolify:** select the chosen published release tag or `main`, retaining variables/mounts.
- [ ] Rebuild from the updated project folder with `docker compose up -d --build` (server `-f` arguments where needed), or redeploy in Coolify. Preserve existing volumes; startup reopens the drive.
- [ ] Start one instance; check database status, Connections and a representative file.

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
