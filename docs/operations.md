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

TGDrive also saves **a complete copy of its database** in the private channel.
This is separate protection in case MongoDB itself is lost.

| Question | Answer |
| --- | --- |
| When? | Automatically, normally hourly; or choose **Back up now** |
| What is included? | File/folder records, shares, settings, credentials and transfer/deletion records |
| Are credentials exposed? | The copy is encrypted before it reaches disk or Telegram |
| What is excluded? | Original file contents, unfinished local bytes and the MongoDB login URI |
| Does an upload wait for it? | No; uploads complete after their Telegram files and MongoDB records are confirmed |
| What do I need to restore? | A confirmed `.tgdb` copy, its matching recovery key and an empty MongoDB database |

**Save the recovery key outside TGDrive and MongoDB.**
A backup cannot be decrypted without its matching key. Keep old keys if you
rotate them.

Database backups remember where Telegram files are stored; they cannot
recreate files that have been deleted from Telegram. A backup also represents
the time it was taken, so later changes are not included.

<details>
<summary>Technical backup behavior</summary>

- Snapshots use a consistent database read, compression and AES-256-GCM encryption.
- Telegram's received ciphertext is downloaded and checked with SHA-256 before confirmation.
- Retry reuses the saved sender/request identity for the same encrypted file.
- Telegram FloodWait deadlines are honored. Restart preserves the last confirmed
  schedule instead of creating a duplicate immediately.
- If a pending encrypted temporary file is lost, that attempt is marked interrupted;
  a new snapshot gets a new send identity.
- Durable TGDrive collections are included. The expiring active-owner lease is excluded;
  indexes are rebuilt during restore.
- The current expanded snapshot limit is 1 GiB.
- Remote backup documents are retained. They do not replace the channel pin.

</details>

## Emergency database restore

Use this **only if MongoDB data is lost**. Ordinary reinstalls use the steps above.

1. Download a confirmed `.tgdb` backup from your Telegram channel.
2. Prepare an empty MongoDB database.
3. Stop TGDrive before restoring to that destination.
4. Put the destination URI/name and the backup's `MONGODB_BACKUP_KEY` in private `.env`.
5. Run the restore command:

~~~bash
uv run --frozen python -m tgdrive restore-mongodb --file /absolute/path/backup.tgdb
~~~

6. Start TGDrive and sign in again.
7. Check the file list and a few downloads before removing any old database.

Restore refuses a nonempty destination. It validates/decrypts the archive before
writing. Saved app settings are restored; browser sessions are revoked and share
scope/expiry is preserved.

<details>
<summary>Restore using Docker instead</summary>

Mount the backup read-only and run the maintenance command:

~~~bash
docker compose run --rm --no-deps \
  -v /absolute/path/backup.tgdb:/restore/backup.tgdb:ro \
  app python -m tgdrive restore-mongodb --file /restore/backup.tgdb
~~~

The container's nonroot user must be able to read the mounted file.
Use the backup's original key. A newer key cannot decrypt an older copy.

</details>

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
| Video will not play | Try Reload video or download the original; device/codec support varies |
| File download also fails | Check connections and whether the Telegram document still exists |
| Database is full | Review catalog/index/history growth or increase the database plan |

If MongoDB loses connectivity or TGDrive loses ownership, it stops accepting
unsafe writes. Restore connectivity, stop any competing app, then restart.
Do not delete internal records or temporary files just to clear an error.


### Disposable media cache

Recently streamed bytes are kept in DATA_DIR/media-cache, capped at 512 MiB and
24 hours. The cache can be removed while TGDrive is stopped; it rebuilds on demand.
It is separate from unfinished uploads, which must be kept. Access checks still
apply when bytes come from cache. Cache files are private to the service account.
