# Run, protect and recover TGDrive

[Docs home](README.md) · [Setup](setup.md) · [Troubleshooting](#troubleshooting)

## Run and monitor

From the installation folder:

| I want to… | Docker command |
| --- | --- |
| Start | `docker compose up -d` |
| Stop safely | `docker compose stop` |
| Restart | `docker compose restart app` |
| Read recent logs | `docker compose logs --tail=100 app` |

For a source installation, use the [source startup command](setup.md#source-installation).
Keep one app instance and one worker per drive.

Use **Connections** for Telegram health and **Storage & Backups** for database,
disk and backup status. The public `/health/ready` endpoint reports readiness.

Use the owner's performance controls to inspect cache usage, active media
operations, queued work, and account cooldowns. Queue waits are estimates from
recent activity, not promised start or completion times. The
[performance guide](performance.md) explains each layer and its limits.

Local commands above use the laptop override and keep the service stopped after
a reboot. For a server configured with `compose.server.yaml`, add
`-f compose.yaml -f compose.server.yaml` to every Compose command. In Coolify,
use the application's Stop, Restart, Logs and Redeploy controls.

Docker's restart policy handles process exits; an `unhealthy` health check alone
does not restart a running container. Inspect logs and the owner pages before
restarting. Readiness checks do not verify every Telegram file or the public
domain. See [Docker's restart policy behavior](https://docs.docker.com/engine/containers/start-containers-automatically/).

## After a shutdown, crash or network loss

Start the **same** deployment with `docker compose up -d`, or restart it with
`docker compose restart app` if it is still running. Wait for startup, sign in,
then check **Transfers**, **Connections** and **Storage & Backups**. Reopen a video
or retry its download if the old browser request was interrupted.

| What was happening | What survives and what to do |
| --- | --- |
| A completed file was idle | Its catalog remains in MongoDB and its documents remain in Telegram. Restarting does not require re-uploading it. |
| The browser was sending a file | Keep the original. Resume the existing transfer and reselect that same file if asked. The server verifies acknowledged bytes; it cannot read files from a closed browser. |
| Telegram uploading had begun | Durable jobs and staged bytes allow recovery from confirmed work. Leave both volumes intact; inspect the existing job before submitting another upload. |
| A URL import was running | Find its server job in Transfers and use Retry if needed. The remote download may restart; an expired signed URL needs a fresh source. |
| Telegram was unreachable | Restore connectivity. Startup reconnection and eligible retryable uploads can retry; Telegram cooldowns still apply after restarting. Use the displayed retry time or Retry action. |
| MongoDB was unreachable or ownership expired | Restore database access, stop competing instances, then restart this app. Unsafe writes stop when ownership cannot be confirmed; restart does not repair an unavailable database. |
| A backup or independent channel copy was in progress | Confirm its status after restart. The last confirmed restore point remains the recovery boundary; partially copied work is not a confirmed backup. |
| The disk filled up | Free unrelated disk space, or use TGDrive's disposable cache controls. Retry the existing transfer; do not delete its staged bytes or journals. |

Browser upload receipts expire after inactivity, normally 24 hours; their
retention is configurable. A laptop asleep for longer can need a fresh upload.
A restart also cannot recreate a deleted local payload, a deleted Telegram
document, a lost database without a usable backup, revoked credentials, or a
missing recovery key. Preserve the displayed error and remaining data before
attempting recovery.

After an unclean stop, another process's ownership lease can briefly remain.
Stop the old app, wait about a minute, then restart once. Do not delete the
database's internal ownership records or disable the single-instance protection.

If MongoDB is completely unreachable or its permissions prevent access, TGDrive
may fail startup and owner login will be unavailable. Restore database access
first, then restart. Saved local recovery configuration does not provide an
offline login or replace the database. A reachable database with a missing or
corrupt catalog can instead permit the owner recovery interface.

## If someone changed the Telegram channel

Keep every storage bot an administrator, keep the channel private, and disable
message auto-delete. **Connections → Refresh** checks the saved accounts and
required channel state. Fix revoked tokens, removed membership or missing
permissions in Telegram, then refresh and restart. Also check the independent
backup status if that feature is enabled.

Before starting/resuming guarded Telegram writes, TGDrive checks the private
channel state and required permissions of usable storage bots. Successful checks
are reused for up to 30 seconds to limit Telegram requests; this is not an
instantaneous lock on Telegram settings. A detected problem blocks that work
and appears in Connections. Correct the channel setting, then retry the existing
operation or restart; failed checks are not cached.

If a file's document was deleted or replaced, access can report that the stored
file is missing or changed in Telegram. Detection happens when the relevant
document is checked or accessed, rather than as a continuous full-channel audit.
Existing valid cached bytes can temporarily mask damage. Editing a caption does
not reliably update TGDrive metadata; make all file changes in TGDrive.

Restore from a confirmed independent channel copy, or re-upload the original
into TGDrive as a new file. Follow [channel recovery](backup-recovery.md) before
changing database/channel bindings. A MongoDB backup restores the catalog, not
the missing file bytes. Restarting cannot undo Telegram message deletion, and
TGDrive does not silently attach a different document to repair it.

## What is saved where

| Place | What it holds |
| --- | --- |
| **Telegram channel** | Uploaded files, large-file parts and encrypted database backups |
| **MongoDB** | Names, folders, file-part records, shares, app settings, transfer history and recovery state |
| **Persistent Docker data/cache volumes** | Generated secrets, Telegram sessions, helper/recovery state, unfinished upload bytes and disposable media cache |
| **Private .env** | Database access and machine-specific configuration |

Deleting the app folder does **not** delete completed files from Telegram or
their records from MongoDB. Removing Docker volumes can lose local secrets and
unfinished uploads. `docker compose stop` preserves containers and volumes;
`docker compose down -v` removes volumes. Keep the same Compose project name so
a later start reuses the correct volumes.

### Save these somewhere separate

- Your MongoDB access details and database name.
- Your web password.
- The file from **Storage & Backups → Save recovery key**.
- Access to the Telegram account/channel holding your files and backups.
- Private deployment configuration and backups of both persistent volumes if
  unfinished uploads or local recovery state must survive losing the machine.

Only encrypted database snapshots use the recovery key. It is not a spare copy
of file contents. Keep at least one confirmed archive and the matching key,
and rehearse recovery into a separate empty database before depending on them.

## Reopen after losing the local installation

Use this if the app folder or machine is gone, but MongoDB still exists.

1. Stop any old running TGDrive instance.
2. Install TGDrive again using the same release where possible. In its project
   folder, create a private configuration file:

~~~bash
cp .env.example .env
chmod 600 .env
~~~

3. Edit `.env` privately. Replace `MONGODB_URI` and `MONGODB_DATABASE` with the
   **existing** database access/name. Supply the original `API_ID`, `API_HASH`,
   `BOT_TOKENS` and `STORAGE_CHANNEL`, which Compose requires even when saved
   MongoDB settings are available. Remove the sample `ADMIN_PASSWORD` placeholder:
   set `ADMIN_PASSWORD=` and leave `ADMIN_PASSWORD_HASH`, `SESSION_SECRET` and
   `MONGODB_BACKUP_KEY` blank/omitted to reuse the database's original bootstrap
   credentials. If an older drive lacks those saved values, supply its original
   credentials/keys from your recovery kit. Do not generate replacements.
4. Start TGDrive **without `init`**:

~~~bash
docker compose config --quiet
docker compose up -d --build
~~~

5. Open the local address and sign in with your existing web password. Check
   Connections, the file list, a representative download and unfinished transfers.

The app loads its saved settings and file list from MongoDB.
Set `COOKIE_SECURE=false` for local HTTP, or `true` when using HTTPS.
For hosted HTTPS, recreate it through [Coolify](setup.md#coolify) or use the
[server Compose commands](setup.md#docker-compose-on-a-server) instead of the
local commands above. Keep its persistent volumes if they survived the move.

Missing local bytes from unfinished uploads still need their original files.

If Atlas has paused an idle free cluster, resume it before starting.
[Atlas pause/resume help](https://www.mongodb.com/docs/atlas/pause-terminate-cluster/)

## Encrypted Telegram backups

The dedicated [Backup & recovery guide](backup-recovery.md) explains what is saved,
what is excluded, how to keep the decryption key, and how to rehearse recovery.
The live MongoDB drive and its encrypted Telegram safety copies are separate.

## Emergency database restore

Follow [Restore a lost database from Telegram](backup-recovery.md#restore-a-lost-database-from-telegram).
A confirmed archive, its matching recovery key and an empty destination database
are required. Ordinary reinstalls reopen the existing MongoDB drive instead.

## Connection changes

Manage accounts in **Connections**, then restart to apply saved changes.
See [web instructions](usage.md#settings-and-connections).

- Finish or cancel unfinished/error transfers before changing account identities,
  enabled membership or Telegram API credentials. To repair an expired/revoked
  credential while work is pending, replace only the token/session for the **same
  account**; keep the account list, enabled states and API credentials unchanged.
  TGDrive verifies the replacement identity before saving.
- Keep at least one enabled bot with channel permissions.
- Use the same channel for the existing drive.
- A different drive/channel needs its own database; changing the channel ID
  does not move files.

### Revoked tokens, removed sessions and lost channel access

TGDrive detects account failures when Telegram rejects an operation or you use
**Connections → Refresh**. A revoked/expired token or session, disabled Telegram
account, or missing channel permissions excludes that connection from new work.
The owner sees a connection warning and a specific repair instruction; other
healthy connections continue serving eligible work. Credentials and raw provider
request text are not included in these errors.

Downloads and streaming can retry through another account with access to the
same channel. An upload part can switch accounts only before message dispatch.
After dispatch, its sender, request ID and staged bytes are retained so a lost
acknowledgement cannot cause TGDrive to create a duplicate on another account.
A missing/revoked original sender produces a repair error rather than an endless
wait. Previously completed files remain mapped to their Telegram documents.

- **Revoked bot token:** manage that bot in Connections and replace its token
  with a valid token for the same bot, then restart and retry the transfer.
- **Removed/expired Premium session:** authorize the same Telegram account again
  and replace its session in Connections, then restart and retry. A private local
  session can be generated with `uv run --frozen python -m tgdrive login`.
- **Lost channel access:** restore membership and required administrator
  permissions, then Refresh. A successful check re-enables a still-connected
  runtime client blocked only by channel permissions; retry the failed operation.
  Revoked credentials still require a replacement and restart.
- **Temporary network interruption or Telegram wait:** existing reconnect and
  bounded retry/cooldown handling applies. Waiting uploads show their connection
  state; working accounts remain available where the saved sender permits it.

If the original Telegram account can never be restored, an ambiguously dispatched
part cannot safely be reassigned automatically. Keep the staged file and journal
for recovery; a restart cannot recreate revoked credentials or grant permissions.
An independent backup bot has its own credentials and channel permissions; check
its [backup status and recovery instructions](backup-recovery.md) separately.

## HTTPS and private configuration

For access beyond your own machine:

- Put the app behind an HTTPS reverse proxy.
- Set `COOKIE_SECURE=true`.
- Restrict direct access to the app port.
- Trust only the actual proxy in forwarded-header configuration.
- Keep credentials, recovery keys and share links out of public logs and Git.
- Use a long unique owner password and protect Telegram and MongoDB accounts
  with their available account security controls. TGDrive itself has one owner
  login; it does not provide multi-user roles or a second login factor.
- Keep `ALLOW_PRIVATE_REMOTE_URLS=false` unless you deliberately need URL imports
  to reach your internal network. Shared visitors cannot initiate owner imports.

After setting up HTTPS, check that the owner can sign in and upload, an incognito
browser cannot read a private file, a scoped test share works, and revoking that
share blocks a new request. Ensure the direct app port is unreachable from the
public internet. Avoid proxy/CDN caching of authenticated responses or share URLs;
preserve TGDrive's authorization and cache headers.

To change the owner password, stop the app, update its private environment
credential and recreate/redeploy it. Use a valid `ADMIN_PASSWORD_HASH` with
`ADMIN_PASSWORD` empty, or remove a stale hash when switching to the plaintext
fallback. After the new effective credential is loaded, old owner sessions fail;
ordinary restarts with the same credential keep them valid. Verify the old
password/session fails and the new password works. Changing the credential does
not revoke existing share links; revoke those in **Shared Links** separately.

After suspected secret exposure, rotate the compromised credential at its
provider, update TGDrive, rotate `SESSION_SECRET` as needed, and test again.
Keep the original backup key for old archives: replacing it does not re-encrypt
existing backups. A database/volume rollback may restore older credentials or
shares, so inspect access after a restore.

See [security reference](reference.md#security-and-privacy) for exact boundaries.
Logs redact common secrets, but review them before sharing.

## Upgrade and rollback

1. Finish active work and stop the app safely.
2. Keep a recent confirmed database backup and its key.
3. Update the source and rebuild the image or frontend.
4. Start one instance; check database status, connections and a representative file.

For a Git checkout, record the current revision privately with `git rev-parse HEAD`,
read release notes, and use `git pull --ff-only` from the same project folder.
If Git reports local changes or a conflict, preserve them and resolve the update;
do not reset the checkout to force it. Rebuild/recreate with
`docker compose up -d --build` (include server `-f` arguments when applicable).
Coolify users deploy the chosen revision while keeping existing variables and mounts.
Keep a confirmed pre-upgrade database archive and matching recovery key off-host.

Do not run an older app against a database format it cannot read.
For a data rollback, restore a compatible backup into a separate empty database.

<details>
<summary>Legacy SQLite installations</summary>

The development MongoDB switch started a fresh test catalog. It did not import
or delete the old SQLite files. Legacy commands remain for compatibility;
they are not the documented MongoDB storage path.

</details>

## Troubleshooting

| What you see | What to do |
| --- | --- |
| MongoDB cannot connect | Check the URI/password, Atlas Network Access, cluster availability and database-user permissions |
| Container is running but first-time website is unavailable | A new drive waits for explicit initialization. Verify the intended empty database, run the documented `init` once, then restart. Existing drives must reuse their original database |
| “Another installation owns the database” | Stop the old app; after a crash, let its ownership lease expire |
| Drive catalog missing | Verify the database name; do not initialize a replacement for an existing drive |
| Bot disconnected or check failed | Open **Connections → Refresh**; check the token and channel permissions |
| Connection changes waiting | Restart the app to apply the saved configuration |
| Backup needs attention | Check the recovery key, channel posting access, temporary disk and displayed retry time |
| Backup will not decrypt | Use its matching key and an intact archive |
| Upload paused or cannot resume | Reselect the original in the existing transfer; missing staging may require a new upload |
| Video will not play | Check Performance for scratch waiters, source requests and cache readiness; use Reload Video in the custom player, and download the original if its codec is unsupported |
| Repeat viewing still slow | Check current cached coverage, hit/miss counters, eviction, disk headroom and server egress |
| Browser slow but a server/Python download is fast | Compare the actual browser protocol and cache state; see [HTTP delivery](#http-delivery-and-proxy-configuration) |
| Preload rejected or incomplete | Increase the effective cache tier or free space; the complete range must remain cached, including integrity overhead |
| Downloads waiting under load | Inspect account/media queues and cooldowns; reduce competing work before increasing limits |
| A copied URL fails outside your browser | Use Copy download link or an explicit share link; private owner URLs need the owner cookie |
| A revoked link still appears in an open player | Revoke blocks future authorized data; the browser may still play bytes it already received |
| URL upload asks for a local file | Refresh and find the original server job in Transfers; retry it, or submit the URL again if that job no longer exists |
| File download also fails | Check connections and whether the Telegram document still exists |
| “Stored file is missing or changed in Telegram” | Preserve the catalog; follow [channel damage recovery](#if-someone-changed-the-telegram-channel). Restart cannot restore deleted messages |
| Browser requests fail with cross-origin 403 behind HTTPS | Preserve Host and forwarded scheme at the proxy; trust only that proxy's actual source IP. Do not disable the origin check |
| Password hash fails validation after editing deployment variables | Use the CLI-generated Argon2id hash; keep `$` literal with single quotes in `.env` or Coolify's Literal control. Clear the unused plaintext/hash override |
| “Missing deployment keys” or invalid deployment-secrets permissions | Preserve the volume/file and supply the original keys or repair its private 0600 permissions; do not delete it to force new keys |
| Database is full | Review catalog/index/history growth or increase the database plan |

If MongoDB loses connectivity or TGDrive loses ownership, it stops accepting
unsafe writes. Restore connectivity, stop any competing app, then restart.
Do not delete internal records or temporary files just to clear an error.


## Disposable media cache and preloads

Recently streamed bytes live under `DATA_DIR/media-cache`. Application defaults
are 256 MiB RAM, 10 GiB disk and a 1 GiB free-disk reserve. Effective RAM can be
smaller because of host/container headroom. Reused blocks normally remain on
disk for up to 24 hours after reuse; eviction can remove them sooner.

Use the owner controls to inspect files, ready byte ranges, current completion,
cache hits/misses, active preloads and errors. Cancel preparation to stop its
background work. Evict one file or clear cached data to reclaim disposable
bytes; other readers retain work they still share.

A successful preload verifies retained coverage, but does not pin it forever.
Full-file preparation needs to fit an individual effective cache tier, including
integrity overhead. RAM plus disk is not one additive whole-file capacity.
Shrinking a budget, running out of disk or consuming memory elsewhere can make
current cached coverage smaller than an earlier preparation result.

You can remove the media cache directory while TGDrive is stopped; it rebuilds
on demand. **Do not remove upload receipts, staged payloads or encrypted backup
sources to clear a media cache.** Missing unfinished bytes require recovery from
their original source. Cache files are private to the service account; anyone
with host-level access to those files may still read cached file contents.

All cache hits continue to require current authorization. A copied private
owner URL is not a share capability, and a revoked/expired token cannot be
made usable by a warm cache. For shared streams, revocation applies to future
authorized blocks; it cannot recall data already sent or buffered by a viewer.

### Tune a busy installation

1. Check effective RAM and disk headroom before raising a cache budget.
2. Let demand reads outrank preloads; stop unnecessary background preparations.
3. Watch shared account operation/request limits and provider cooldowns.
4. Compare warm and cold downloads, and same-file versus unrelated-file viewers.
5. Raise one limit at a time and watch whether streaming or errors get worse.

Performance preferences saved through the owner settings apply live where
supported. Changes made to container environment values need container
recreation. Connection credentials still follow the separate restart workflow.

## HTTP delivery and proxy configuration

TGDrive's standard response policy sends `Alt-Svc: clear`. Browsers clear cached
alternative-service advertisements and use the origin's normal HTTPS transport,
ordinarily HTTP/2 through Coolify/Traefik. Source installs receive the app policy.
TLS, authorization, range delivery and resumability remain enabled. No extra
environment value or manual proxy rule is required.

Both supported Compose files also declare the matching edge response middleware.
Coolify automatically attaches it to the domain routes it generates, preserving
normal upstream discovery, HTTPS and gzip configuration across redeploys. This is
necessary because Traefik can append its own advertisement to backend headers;
the edge middleware ensures a single `Alt-Svc: clear` reaches the browser.

This default follows a reproduced deployment where a fully cached 17 MB file
transferred in about 94 seconds over HTTP/3 and about one second over HTTP/2.
That result describes the tested path, not all QUIC connections. See the
[streaming validation report](streaming-validation-2026-10-18.md).

An external CDN/proxy can replace or append application headers; configure that
edge to preserve the final policy if necessary. Preserve range/length headers, forward bytes
without waiting for an entire response, and avoid compressing byte-addressed
file responses. TGDrive supplies `Content-Encoding: identity` for those responses.

A temporary domain-scoped proxy rule can help an old app while an upgrade is
pending. Remove it after the updated Compose/app supplies the policy. Ordinary new
installations use the standard Compose/source instructions instead. Redundant
custom routers can need maintenance when domain/service names change.

Coolify watches custom dynamic files without a proxy restart. Main proxy Compose
edits require a restart and must be saved through Coolify's configuration UI to
survive later restarts. References:
[Coolify Traefik configuration](https://coolify.io/docs/core/networking/proxy/traefik/overview),
[Traefik HTTP/3 entrypoints](https://doc.traefik.io/traefik/reference/install-configuration/entrypoints/#http3),
and the [Alt-Svc clear specification](https://www.rfc-editor.org/rfc/rfc7838.html#section-3.1).
