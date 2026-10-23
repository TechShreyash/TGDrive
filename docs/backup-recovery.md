# Backup & recovery

[Docs home](README.md) · [Setup](setup.md) · [Operations](operations.md)

TGDrive stores file bytes as Telegram documents and keeps a catalog that maps
those documents to files, folders, split parts and shares. The catalog is stored
in the selected metadata backend: MongoDB or local SQLite.

Split files use the same backup and recovery model: database backups retain
their ordered part mappings, and an enabled independent backup channel copies
the constituent documents and remaps their locations in its recovery catalog.
Premium's larger single-document allowance is unnecessary for this support.
After recovery, the mapped parts still form one logical file for original
downloads, streaming and byte-range seeking. Independent-copy protection applies
to the confirmed restore point; check backup status for any pending parts.

These are different kinds of protection:

| Protection | What it does | What it cannot do |
| --- | --- | --- |
| Live catalog and its database/checkpoint backups | Recovers records and saved metadata | Recreate file bytes missing from the only Telegram channel |
| Optional independent backup channel | Copies completed documents to a second private channel and saves matching recovery catalogs in both channels | Protect against losing both Telegram channels/accounts, a missing recovery key, or bytes never successfully backed up |
| Originals and backups outside Telegram | Provides a recovery route outside the same provider | Replace testing that your chosen copies are readable |

A second channel reduces a single-channel failure risk. Telegram remains the
provider for both copies; access or policy actions can affect both. Keep important
originals and a recovery kit outside Telegram. This feature does not bypass
Telegram restrictions or guarantee permanent access.

## Set up an independent backup channel

The feature is optional. Existing drives continue using their configured main
channel until the owner enables it under **Tools & settings → Storage & Backups
→ Independent backup channel**.

### Prepare the Telegram accounts and channels

1. Keep the main channel and its normal storage bots on your main Telegram
   account. A main channel may have several storage bots; additional bots do not
   guarantee a speed increase or remove Telegram rate limits.
2. Use a separate Telegram account to create a second, **private broadcast
   channel** and one dedicated backup bot through BotFather. Keep recovery access
   to that account independent from the main account.
3. Add the backup bot as an administrator in **both the main and backup
   channels**. It needs to read documents and post, edit/pin and delete messages.
   Its access to the source is necessary for Telegram's server-side document copy.
4. Keep the normal primary bots in the main channel. The backup bot must be a
   different Telegram bot identity from every primary bot. A regenerated token
   for the same bot is still the same identity.
5. Both channels must remain private, with no public usernames. Turn off
   **Restrict Saving Content** on these dedicated channels if it prevents document
   copies. Turn message auto-delete **Off** in both channels. Do not use expiring documents, unrelated posts or protected content as
   TGDrive storage.
6. Do not manually delete/edit document messages still referenced by the catalog
   or a retained restore point, change TGDrive pins, or use the channels for
   unrelated activity. Make file changes through TGDrive. Changing channel
   permissions or bot membership can interrupt protection.

Creating human accounts, BotFather bots, channels and administrator permissions
happens in Telegram. Telegram's bot APIs cannot create these owner accounts for
TGDrive. The web setup checks channel privacy, channel IDs, bot identities and
permissions. It **cannot prove which human account owns a channel or bot**; the
separate-account acknowledgment is the owner's responsibility.

In Telegram's channel administrator screen, enable **Post Messages**, **Edit
Messages** and **Delete Messages** for the backup bot in both channels. Editing
permission supports the recovery pin; unrelated administrator permissions are
not required. Keep the primary storage bots' permissions from
[standard Telegram setup](setup.md#2-prepare-telegram).

### Enable and confirm the first copy

1. Open **Independent backup channel → Set up backup channel**.
2. Enter the backup's negative numeric channel ID and backup bot token. Confirm
   that you prepared it with a separate Telegram account. Tokens are write-only;
   they are not returned in status or reports.
3. Choose **Verify and start backup**. Verification must succeed before the
   configuration is saved. Existing completed files, including every part of a
   split file, are included in the initial synchronization.
4. Choose **Save recovery key** in this section. Keep the downloaded
   `tgdrive-channel-recovery-key.json` outside Telegram and the server.
5. Follow the current file, confirmed file/part counts and bytes. You can close
   the tab; the operation runs on the server. **Pause** saves its place; **Resume**
   continues from confirmed work.
6. Wait for **Backup confirmed**, then check the last confirmed restore point and
   catalog revision. Save the recovery-key JSON again after this first confirmed
   copy: its restore-point section records the recovery capsule message IDs in
   both channels. Save a fresh export when you want to retain a newer restore
   point's IDs.

Normal reads, uploads and metadata changes continue during initial background
synchronization and later catch-up. An upload completed in the main channel can
still be pending in the independent backup; check the protected revision and
confirmed restore point separately.

Permanent deletion removes catalog records and shares while retaining original
Telegram documents. While independent backup is enabled, TGDrive still requires
the relevant backup protection before discarding those records. Pausing backup
therefore pauses permanent catalog removal until you resume backup or explicitly
disable that protection. It does not freeze normal browsing, uploads, rename/move
or trash changes. Older restore points may contain permanently removed records
and their Telegram copies; permanent deletion is not an erasure of backup history.
See [why Telegram originals are retained](usage.md#why-permanent-deletion-keeps-the-telegram-files).
A full channel recovery freezes file mutations until activation or explicit
cancellation of the unfinished recovery.

A file count or 100% byte meter alone is not a complete restore point. TGDrive
confirms the destination documents and publishes their matching encrypted
recovery metadata to both channels before advancing the protected revision. New
changes can be pending while an earlier restore point remains usable. Only the
last confirmed revision is promised by the displayed backup state.

### What this copy retains

The backup contains independent Telegram document messages and an encrypted
`tgdrive-replica.tgdb` recovery capsule. The capsule contains the catalog for each
channel, remapped message/part locations, API/instance recovery credentials and
saved app preferences/connection configuration supported by that capsule version.
Review restored connections before enabling optional accounts or additional bots.
The capsule uses authenticated AES-256-GCM encryption with the independent-channel
recovery key. Original document bytes are copied as Telegram
messages; TGDrive does not encrypt those originals separately.

This is a recoverable set of file copies and restore points, **not a destructive
message-for-message delete mirror**. Old copied payloads and old remote capsules
are retained rather than automatically erased. A newer catalog reflects file
renames, folders, Starred items, shares, trash and completed removals; restoring an
older catalog restores that older state. Review shares after an older recovery,
since a later revocation may not be present in it.

Unsent upload bytes, unfinished staging, disposable caches and device originals
are not protected by a channel copy. The independent capsule is not the full
MongoDB `.tgdb` database archive described below: preserve both recovery kits if
you use both features, and do not interchange their keys or files.

### Rebuild a lost backup from the surviving main channel

1. Preserve the main channel, its files and the working TGDrive catalog.
2. Create a new empty private backup channel using your independent backup
   account. Add the backup bot to both channels as administrator.
3. Open **Independent backup channel → Backup settings → Replace backup channel**.
4. Enter the new backup channel and bot token, verify, and let synchronization
   finish. Existing files are copied again where needed. Replacing the configured
   destination does not erase the old remote channel or recovery capsules.
5. Wait for confirmation and save an updated recovery-key export.

If only permissions or connectivity changed, repair those and use **Retry** or
**Resume** instead of creating a second operation. **Disable automatic backup**
stops copying new changes; it does not make the current catalog independently
protected. Keep existing channels and keys. **Enable backup** starts protection
again.

### Recover a lost main channel from a surviving channel

1. Preserve the surviving private channel and the saved recovery-key JSON.
   If the local catalog is missing or cannot open, TGDrive serves an authenticated
   **Storage recovery** screen instead of silently creating an empty drive.
   Otherwise open **Independent backup channel → Recover a channel or catalog**.
2. Enter the surviving channel ID, a backup bot token with access to it, and the
   64-character `key` value from the recovery-key JSON for an encrypted copy. Do
   not paste the complete JSON document into the key field. Leave the key empty
   only when recovering a verified plain catalog checkpoint; this cannot decrypt
   a channel capsule without its matching key.
3. Leave **Checkpoint message ID** empty to try the surviving channel's latest
   pinned recovery capsule. If the surviving channel is the original main
   channel, its ordinary catalog checkpoint may own the pin. Enter the
   `source_message_id` from the saved JSON's `restore_point` in that case. For the
   backup channel, `backup_message_id` selects that recorded restore point.
4. Choose **Inspect recovery copy**. TGDrive authenticates the capsule and shows
   the catalog revision, files, bytes and source channel. Inspection does not
   activate a replacement drive. A verified older `drive.json` catalog checkpoint
   can also be selected from its original channel. That fallback does not contain
   application preferences or unfinished transfers; preserve the original
   `SESSION_SECRET` in deployment configuration to retain public share links.
   For this legacy path, download and save the newly generated channel recovery
   key before relying on subsequent encrypted recovery copies.
5. Create a new empty private main channel in Telegram. Add the surviving backup
   bot and a separate main storage bot as administrators there. The surviving
   backup bot copies documents; the new main bot is verified for ordinary service.
6. Enter the new main channel ID and new main bot token. Type the destination
   channel ID again, then choose **Copy to new main channel**. File mutations are
   paused during recovery so the catalog cannot diverge while message IDs are
   remapped. The source channel remains unchanged.
7. Wait until all required parts and recovery metadata are verified. Repair any
   listed errors and resume the same operation. A page reload or process restart
   must use the saved server-side candidate and journal, not start another drive.
8. Review **Recovered drive verified**, type its channel ID, and choose **Activate
   recovered drive**. Activation is explicit; successful copying alone does not
   switch TGDrive.
9. Restart the TGDrive service/container when instructed. Browser refresh alone
   does not restart the process. The former catalog/configuration are preserved
   for review rather than silently discarded.
10. Check representative images, videos, PDFs, split-file downloads, Starred
    items, shares and Connections. Re-add additional primary bots or optional
    account sessions as needed. Then configure/confirm an independent backup for
    the recovered main channel and save the current recovery key/restore point.

Only files present in the selected, confirmed restore point can be recovered.
TGDrive cannot synthesize a missing source document, recreate a part never copied,
or recover an inaccessible channel without a surviving copy.

### Recover only the database when the surviving channel is healthy

After inspecting a recovery capsule, select **Restore the database only on this
surviving channel**. Supply a main bot token for that channel that is distinct from
the surviving backup bot, and type the surviving channel ID to confirm.

TGDrive verifies each referenced file part and expected size without copying the
whole library to another channel. Activation remains a separate explicit step,
followed by a service restart. This surviving channel becomes the main channel;
set up a separate independent backup again afterwards. Do not select this option
when the channel or its required documents are unavailable.

### Interruptions and reports

| State or error | What to do |
| --- | --- |
| Telegram wait / FloodWait | Keep the service running or restart normally. Observe the retry deadline; adding more bots does not override it. |
| Network timeout or uncertain send result | Resume/retry the same operation. Confirmed mappings and persisted operation IDs are reused. |
| Process or server stops | Preserve `DATA_DIR` and restart one instance. The journal retains confirmed parts and pending work. |
| Paused | Choose Resume. Unconfirmed later changes are not protected yet. |
| Bot no longer authorized or lacks permissions | Restore access in both channels, or configure the appropriate replacement bot/channel. |
| Channel public or content protection enabled | Make the dedicated channels private and permit document copying before retrying. |
| Missing document, wrong size or invalid capsule/key | Keep the existing sources; check the correct key/restore point. Try another confirmed point or an independent original. Never mark the operation successful manually. |
| Disk space or database write failure | Free space or repair storage/permissions while retaining the journal and old catalog. Retry only after the underlying issue is corrected. |
| Neither channel/account is available | Use originals/backups outside Telegram. A second Telegram channel cannot restore access to the provider. |

Open **Activity and report** for recent events and **Download report** for the
owner-facing status report. It contains progress and actionable failures, not bot
tokens or decryption keys. Channel/file names and IDs can still be private, so
review it before sharing. Do not post the recovery key or raw private state files
with a support request.

MongoDB deployments also retain encrypted full-database archives separately.
Independent synchronization copies a confirmed archive to the backup channel when
one is available and records its archive metadata. Its database revision may be
newer than the portable channel catalog's confirmed revision if the live database
changed during copying; these are separate restore points, not interchangeable
revision numbers. Keep the MongoDB archive's matching key as well as the channel
capsule key. If a downloaded recovery-key JSON includes both, preserve the whole
file; the web capsule key field still takes the independent channel key value.

The channel key export pairs `key` and `mongodb_backup_key` with its recorded
`restore_point`. The `next_backup_key` and `next_mongodb_backup_key` fields describe
the next configured backup generation; they may differ after key rotation. Keep
the whole export and older key/restore-point pairs. If a pending snapshot captured
an older MongoDB key, restore that key to resume it, or verify **Backup settings**
again to capture a fresh snapshot with the current key. TGDrive refuses to label
an archive with a key that does not match its captured generation.

After independent backup is configured, a private `replica-bootstrap.json` in
`DATA_DIR` retains owner recovery credentials and a password hash for subsequent
recovery. It does not create a replacement database or make login work while
MongoDB is unreachable. Restore MongoDB connectivity/access, or configure a
reachable recovery destination, then restart. Startup can fail until database
access is restored. Owner web recovery can help with a missing/corrupt catalog
when its database is reachable. Preserve this private file, the channel journal and
activation state with the data volume. A brand-new container with no saved drive
still waits for explicit initialization; never run `init` to repair a lost drive.

Local staged capsules and prior recovery archives are retained. Monitor free disk
space and preserve useful restore points outside the server; staging stops with
an actionable error before consuming the configured free-space reserve.

The local journal and pending recovery files belong to the persistent `DATA_DIR`.
Do not delete that directory, run two instances against it, initialize a new drive
over it, or change destinations to bypass an error. Keep it through container
redeployments. Telegram-owned content and local disk failures are outside the
journal's control; rehearse recovery periodically with a separate test destination.

## Keep the MongoDB recovery kit outside the server

Save these in a private password manager or another protected location you can
access even when TGDrive, Coolify and MongoDB are unavailable:

| Keep | Why |
| --- | --- |
| **Recovery key** (`tgdrive-recovery-key.env`) | Decrypts a `.tgdb` backup; your login password cannot replace it |
| **A confirmed `.tgdb` backup** or access to the channel containing it | The database snapshot to restore |
| **Telegram account and channel access** | Retrieves backups and the original files; keep channel administrator access |
| **MongoDB connection details and database name** | Reopens the current drive, or identifies a new recovery destination |
| **Owner login and private deployment configuration** | Signs in and recreates the deployment; preserve existing secrets and volume configuration |

Open **Management → Storage & Backups → Save Recovery Key**. The download contains
`MONGODB_BACKUP_KEY=` followed by a 64-character hexadecimal value. Keep the whole
file private. Store the key separately from the encrypted archives: anyone with
both can read the backed-up credentials and catalog.

Standard container setup can generate the key automatically and retain it in
`DATA_DIR/deployment-secrets.json`; manual setup can supply it through the
`MONGODB_BACKUP_KEY` environment value. **An automatically generated key still
needs an independent saved copy.** A copy inside a lost database or its encrypted
backup cannot unlock that backup for you. Do not regenerate it during redeployment.

If you change the key, keep the old key with the backups it encrypted. The current
**Save Recovery Key** action exports the current key, not every historical key.

## Make and check a MongoDB backup

1. Open **Management → Storage & Backups** and choose **Back Up Now**.
2. Wait for confirmation and check **Last Telegram Safety Copy**. “Saved to MongoDB”
   describes the live database; it does not by itself confirm a Telegram backup.
3. Save the recovery key separately. Download a confirmed `.tgdb` document from
   the private Telegram storage channel and keep a second copy if needed.
4. Before relying on the kit, rehearse the restore below using a separate, empty
   test database. Keep the production app stopped while checking the restored
   installation: one active TGDrive instance per drive.

Automatic snapshots normally run hourly. The interval is controlled by
`MONGODB_BACKUP_SECONDS`; see [configuration](reference.md#persistence-and-initialization).
A failed backup does not erase the live MongoDB drive. Check the displayed error
and retry time, channel posting access, key configuration and temporary disk space.

## What the encrypted MongoDB archive contains

| Included | Excluded |
| --- | --- |
| File/folder catalog and Telegram part locations | Original file bytes: these remain in Telegram |
| Shares and their saved scope/expiry | Bytes from unfinished local uploads |
| Saved app settings, Telegram credentials and owner authentication settings | MongoDB login URI/name and deployment-specific local paths |
| Transfer, preparation, deletion and recovery records | Disposable media caches and telemetry history/lifetime counters |

Snapshots use compression and **AES-256-GCM authenticated encryption**. TGDrive
uploads the encrypted document, downloads the received ciphertext and checks its
SHA-256 before confirming the copy. Restore checks authentication, format and
completeness before writing. Snapshots are bounded to 1 GiB of expanded database
content. They are app-record backups, not a dump of unrelated MongoDB collections.

This encryption protects database backups. TGDrive does not separately encrypt
original Telegram payloads or local upload/cache bytes. A catalog backup cannot
recreate a Telegram file or part that has been deleted from the channel.

## Choose the right MongoDB recovery route

| What happened? | Action |
| --- | --- |
| Normal update, redeploy or restart | Keep the same MongoDB, secrets and volumes; no restore or initialization |
| Server/app folder lost, MongoDB survives | Reinstall and point to the same MongoDB; do not run `init` |
| MongoDB lost or a deliberate database rollback | Restore a matching `.tgdb` archive into a new empty database |
| Original Telegram file/part deleted | A database snapshot cannot recover the missing payload |
| Archive exists but its matching key is lost | It cannot be decrypted; use surviving MongoDB or another backup/key pair |

### Reopen the existing MongoDB drive

1. Stop the old app if it is still running.
2. Recreate the deployment with the same `MONGODB_URI` and `MONGODB_DATABASE`.
   Preserve the original deployment secrets and both persistent volumes when
   available. The supplied Compose file also needs its required original
   Telegram fields; follow the complete [reinstall commands](operations.md#reopen-after-losing-the-local-installation).
3. Start TGDrive normally, **without `init`**. Sign in using the existing owner login.
4. Check Connections, the file list, a split-file download and any unfinished uploads.

Saved app/connection settings load from MongoDB. Use `COOKIE_SECURE=true` with
HTTPS; use `false` only for local HTTP. Missing staged upload bytes still need
recovery from their original source. Coolify redeployment should preserve its
MongoDB settings, secrets and mounted volumes.

### Restore a lost database from Telegram

1. Download the confirmed `.tgdb` document from the storage channel. Match it to
   its saved key and note its date: later changes will not be in this snapshot.
2. Prepare a **new, empty MongoDB database** with transaction support, as required
   by [Setup](setup.md). Stop TGDrive before restoring.
3. In private `.env` for source setup, or a separate `.env.restore` for the
   maintenance container, set the destination
   `MONGODB_URI`, `MONGODB_DATABASE`, and the archive's original `MONGODB_BACKUP_KEY`.
   Restrict this file to your user (`chmod 600 .env.restore`). Do not initialize
   the destination first. A Docker maintenance file needs only these three values:

~~~dotenv
MONGODB_URI=<destination-private-uri>
MONGODB_DATABASE=<new-empty-database-name>
MONGODB_BACKUP_KEY=<original-64-character-hexadecimal-key>
~~~
4. Run one of these commands from the matching TGDrive release:

Source installation:

~~~bash
uv run --frozen python -m tgdrive restore-mongodb --file /absolute/path/backup.tgdb
~~~

Docker installation: build a maintenance image from the matching source release.
Use a dedicated container with the backup mounted read-only, leaving the stopped
deployment and its volumes intact. These commands run in Linux/WSL from the
project folder; replace the absolute backup path with your own:

~~~bash
docker build -t tgdrive:restore .
docker run --rm --user "$(id -u):$(id -g)" \
  --env-file .env.restore \
  --mount type=bind,src=/absolute/path/backup.tgdb,dst=/restore/backup.tgdb,readonly \
  tgdrive:restore python -m tgdrive restore-mongodb --file /restore/backup.tgdb
~~~

The temporary container runs as your Linux user so it can read your privately
owned archive without making it world-readable. The destination database must
be reachable from this container; `localhost` in a MongoDB URI refers to the
container itself. Permit the restore machine's IP in Atlas Network Access.
For Coolify, stop the production app and run this maintenance container on a
machine with Docker and the matching source checkout; the terminal of a running
production app is not an isolated restore environment.

Restore needs only the destination MongoDB access and matching backup key, not
working Telegram credentials, because saved settings are recovered from the
archive. Using the app's full Compose service for maintenance would additionally
require its Telegram interpolation values; the dedicated command avoids that.

5. After successful restore, start one TGDrive instance against the recovered
   database. Update the app's deployment URI/name to that destination, preserve
   the original storage channel and keys, and recreate/redeploy; do not run init.
   Remove stale example/credential overrides or supply the originals as described
   in [reopening a drive](operations.md#reopen-after-losing-the-local-installation).
   Sign in again: restored browser sessions are explicitly revoked.
6. Check folders, representative downloads, split files, Connections, and sharing
   permissions. **Review restored share links**: restoring an older snapshot also
   restores its sharing state, which can predate a later revocation.
7. Keep the old database/archive and key until you have checked the recovery.
   Then take a fresh confirmed backup and save its matching key.

Restore refuses an existing catalog or nonempty TGDrive destination collections.
It does not merge databases. Invalid keys or damaged archives fail before catalog
writes; a database write failure rolls back the restore transaction. Confirm the
selected destination rather than deleting records to get past a refusal.

## Verify your recovery kit periodically

A useful drill proves that the archive decrypts, the catalog opens, and original
Telegram files are still accessible. Use a separate empty database and the same
release, then follow the checks above. It is not enough to see a `.tgdb` filename.
Keep originals for unfinished uploads until their transfers are completed.

For exact archive/API boundaries, see the [reference](reference.md#security-and-privacy).
For connectivity, ownership or disk errors, see [troubleshooting](operations.md#troubleshooting).
