# Backup & recovery

[Docs home](README.md) · [Setup](setup.md) · [Operations](operations.md)

TGDrive keeps **file bytes in your Telegram channel** and **the catalog in MongoDB**.
The catalog tells TGDrive how those bytes form files, folders, split files and shares.
Encrypted Telegram database backups let you recover that catalog if MongoDB is lost.
They are separate from the ordinary MongoDB commits that make uploads complete.

## Keep a recovery kit outside the server

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

## Make and check a backup

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

## What the encrypted archive contains

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

## Choose the right recovery route

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
   Preserve the original deployment secrets and temporary upload volume when available.
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
3. In private `.env` or the maintenance container environment, set the destination
   `MONGODB_URI`, `MONGODB_DATABASE`, and the archive's original `MONGODB_BACKUP_KEY`.
   Do not initialize the destination first.
4. Run one of these commands from the matching TGDrive release:

Source installation:

~~~bash
uv run --frozen python -m tgdrive restore-mongodb --file /absolute/path/backup.tgdb
~~~

Docker Compose installation, with the backup mounted read-only:

~~~bash
docker compose run --rm --no-deps \
  -v /absolute/path/backup.tgdb:/restore/backup.tgdb:ro \
  app python -m tgdrive restore-mongodb --file /restore/backup.tgdb
~~~

The container's service user must be able to read the mounted file. With Coolify,
run this maintenance command using the deployed image and a stopped app's
configuration/network, with the archive mounted read-only; the app terminal of a
running production instance is not an isolated restore environment. Restore needs
only the destination MongoDB access and matching backup key, not working Telegram
credentials, because those saved settings are recovered from the archive.

5. After successful restore, start one TGDrive instance against the recovered
   database. Sign in again: restored browser sessions are explicitly revoked.
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
