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

## What is saved where

| Place | What it holds |
| --- | --- |
| **Telegram channel** | Uploaded files, large-file parts and encrypted database backups |
| **MongoDB** | Names, folders, file-part records, shares, app settings, transfer history and recovery state |
| **Local temporary storage** | Bytes from unfinished uploads and disposable caches |
| **Private .env** | Database access and machine-specific configuration |

Deleting the app folder does **not** delete completed files from Telegram or
their records from MongoDB. Deleting temporary storage can break unfinished
uploads.

### Save these somewhere separate

- Your MongoDB access details and database name.
- Your web password.
- The file from **Storage & Backups → Save recovery key**.
- Access to the Telegram account/channel holding your files and backups.

## Reopen after losing the local installation

Use this if the app folder or machine is gone, but MongoDB still exists.

1. Stop any old running TGDrive instance.
2. Install TGDrive again.
3. Create a private `.env` with the **same** database access:

~~~dotenv
MONGODB_URI=<your-existing-private-uri>
MONGODB_DATABASE=tgdrive
~~~

4. Start TGDrive without running `init`.
5. Sign in with your existing web password.

The app loads its saved settings and file list from MongoDB.
Set `COOKIE_SECURE=false` for local HTTP, or `true` when using HTTPS.
For Docker, build the image if needed, then run `docker compose up -d`.

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

- Finish or cancel unfinished/error transfers before editing.
- Keep at least one enabled bot with channel permissions.
- Use the same channel for the existing drive.
- A different drive/channel needs its own database; changing the channel ID
  does not move files.

## HTTPS and private configuration

For access beyond your own machine:

- Put the app behind an HTTPS reverse proxy.
- Set `COOKIE_SECURE=true`.
- Restrict direct access to the app port.
- Trust only the actual proxy in forwarded-header configuration.
- Keep credentials, recovery keys and share links out of public logs and Git.

See [security reference](reference.md#security-and-privacy) for exact boundaries.
Logs redact common secrets, but review them before sharing.

## Upgrade and rollback

1. Finish active work and stop the app safely.
2. Keep a recent confirmed database backup and its key.
3. Update the source and rebuild the image or frontend.
4. Start one instance; check database status, connections and a representative file.

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
