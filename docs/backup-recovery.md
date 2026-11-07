# Backup & recovery

[Docs home](README.md) · [Operations](operations.md) · [Configuration](configuration.md)

MongoDB holds the catalog and saved work; Telegram holds file bytes. Current
backups use a key derived from the installation's `ENCRYPTION_KEY`. There is no
additional recovery key to export. Keep a downloaded backup outside Telegram too.

> [!IMPORTANT]
> Keep the [off-host recovery material](#save-off-host-recovery-material) and
> another copy of valuable originals. Keep storage and backup channels private,
> with auto-delete off. A database backup cannot recreate deleted Telegram bytes.
> Losing `ENCRYPTION_KEY` prevents opening this drive, its encrypted files and
> current backups. [Encryption limits](reference.md#stored-file-encryption).

## Save off-host recovery material

Store these privately, outside the TGDrive machine:

- The original **`ENCRYPTION_KEY`**, preferably in a password manager separate
  from the backup file. Never generate a replacement for an existing drive.
- Deployment configuration: `MONGODB_URI`/`MONGODB_DATABASE`, `API_ID`,
  `API_HASH`, `STORAGE_CHANNEL`, working storage bot tokens, and any configured
  `MAIN_BOT_TOKEN`/`TELEGRAM_ADMIN_IDS`. Preserve `SESSION_SECRET` too; generated
  values live in the private `DATA_DIR/deployment-secrets.json` file.
- An owner **`ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH`** for recovery startup.
  A blank credential can reuse the saved hash only while MongoDB survives;
  an empty database cannot supply it.
- A confirmed downloaded **`.tgdb` backup**, its date and TGDrive version.
  Check the actual Telegram recovery pin before relying on channel discovery.
- If independent protection is enabled, the backup channel ID and bot token,
  plus access to the Telegram accounts controlling both channels.

Preserve both persistent volumes if unfinished uploads must survive host loss;
the backup excludes staged payloads and logged-in sessions. Keep the original
**v2.2 archive and its `MONGODB_BACKUP_KEY` or recovery-file key** separately for
[version-compatible recovery](#upgrade-or-restore-an-older-drive). That old key
is different from the `ENCRYPTION_KEY` introduced in 2.3.

## What you need after a loss

| You lost | What brings the drive back |
| --- | --- |
| The server | Reopen the **same MongoDB database** with the original configuration and key. Saved backup settings/progress survive; unfinished uploads still need staged bytes or originals. |
| MongoDB | Use an **empty dedicated database**, the original key, working channel access and an explicit owner password/hash. A discoverable current backup opens **Restore drive**. Follow [database-loss recovery](#restore-after-losing-mongodb). |
| MongoDB and the main channel | The same recovery material plus the **surviving backup channel ID and bot token**. Requires a [confirmed independent backup](#main-channel-lost). |
| `ENCRYPTION_KEY` | TGDrive cannot open the drive, encrypted files or current backups. Legacy plaintext parts are not encrypted by this key, but they do not rebuild the catalog. |

Reopening a surviving database is not archive restore.

## How backups work

**Saved to MongoDB** (API recovery state `protected`) describes database
protection; it does not by itself confirm a Telegram backup. Check **Last
Confirmed Telegram Backup** and verify its pin. A byte counter
at 100% can still be waiting for verification/publication.

`pin_verified_at` records when a recovery pin was checked, not a continuous
guarantee that it remains current. An **unverified** discovery status can mean a
changed pin or unavailable Telegram access; it does not establish archive
corruption. Older archives can remain valid after a newer point becomes pinned.
Preserve an intact downloaded/local copy. TGDrive does not automatically repin an
old point; repair access and publish a fresh backup through the normal controls.

- TGDrive saves a backup once a day (`MONGODB_BACKUP_SECONDS`) and when you choose
  **Storage & Backups → Back Up Now**. Scheduled standalone backups do not skip
  unchanged catalogs. Manual backup requests an immediate point; recovery or
  paused independent protection can still block it.
- Each backup is one file: supported durable MongoDB collections, compressed and
  encrypted (AES-256-GCM) with a key derived from `ENCRYPTION_KEY`. Browser sessions are
  left out, along with diagnostic telemetry and staged file bytes.
- The file is posted and **pinned** in the storage channel, and in the backup
  channel when one is configured. Every storage account that can publish a backup
  needs permission to pin; bots need **Edit Messages**. Check that the newest
  recovery file is actually pinned. A verified upload alone does not establish
  automatic discovery after MongoDB loss.
- Standalone publication stays pending until the verified message is pinned and
  the pin is read back. A failed pin retries the same recorded message, including
  after restart with the same MongoDB; it does not resend verified ciphertext.
  Saved provider retry deadlines survive restart and delay another pin attempt.
  Restore pin permission and leave TGDrive running. Losing MongoDB before
  confirmation also loses that retry state.
- **Download backup** gives you an off-Telegram copy. Backups created by 2.3 need
  the original `ENCRYPTION_KEY`; old 2.2 archives need their old matching key.
  Intact locally retained confirmed or unverified archives are eligible. A newer
  pending point or missing local file does not hide an older eligible copy.
  Download availability is separate from discovery status; the selected file is
  checked against its own stored digest before delivery. Keep Telegram access:
  the archive contains metadata and locations, not the original file bytes.
- MongoDB is still the live database. Keep database-native/provider backups too;
  available history and point-in-time restore depend on the provider and plan.

Normal paged browsing does not establish that snapshot capture, recovery
validation or activation will fit the same host. Monitor backup errors and keep
independent MongoDB backups and original files. A failed publication does not
replace the last confirmed backup; that older point cannot cover later changes.
A second Telegram channel does not establish larger-catalog recovery capacity.
Bounded larger-catalog recovery and its complete compatibility/failure-path and
small-host memory evidence remain outstanding.

## Restore after losing MongoDB

1. Stop the old instance. Create an empty dedicated MongoDB database and point
   `MONGODB_URI`/`MONGODB_DATABASE` at it. Preserve the original `ENCRYPTION_KEY`
   and working Telegram/channel configuration.
2. Set an explicit `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` if the deployment
   had relied on MongoDB's saved hash. Start TGDrive and sign in.
3. On **Storage recovery**, check the backup's date and file count, then choose
   **Restore drive**.
4. TGDrive checks every stored file part in Telegram, restores the database and
   saves it. **Restart** TGDrive when asked.
5. Check a few files, shares and Connections. Telegram user accounts need to sign
   in again.

Discovery checks the channel pin, not its full message history. A missing or
unrecognized pin can lead to an empty drive; that does not establish that no old
drive or backup exists. Preserve state and inspect a downloaded backup if the
expected restore point is missing. If a recognized backup cannot be opened,
TGDrive opens owner recovery mode rather than initializing over it. Check the
original key, channel access, format and metadata limits. Older archives need
[version-compatible recovery](#upgrade-or-restore-an-older-drive); do not erase
the drive to clear the error. A wrong key on a surviving database stops startup
before serving files.

<details>
<summary>Restore from a downloaded file instead</summary>

Open **Restore backup** on the recovery screen, choose your `.tgdb` file and
**Inspect backup**. If a saved bot token was revoked, open **Channel access
changed?** and enter a working token for the surviving channel. Continue with the
steps the screen shows.

</details>

## Add a backup channel

A second private channel protects files included in its last confirmed restore
point if the main channel is lost. Both channels still depend on Telegram.

1. Create a private channel and a separate bot using **another Telegram
   account**. Keep independent access to that account. Add the bot as an
   administrator in **both channels** with post,
   edit, delete and pin rights.
2. Choose **Storage & Backups → Set up backup channel**, enter the channel ID and
   bot token, confirm, then **Verify and start backup**.
3. Leave TGDrive running until **Backup confirmed**. The browser can close.

TGDrive checks bot/channel access, but cannot verify separate human ownership.
Media copies happen inside Telegram without downloading original files. New
changes are grouped
for up to 60 seconds. Encrypted files stay encrypted in the copy.
[Pacing and verification](architecture.md#independent-backup-pacing-and-verification).

### Main channel lost

1. On the recovery screen, open **Main channel lost: restore from the backup
   channel**, enter **Surviving backup channel ID** and **Surviving backup bot
   token**, then **Find backup**.
2. Create an empty private main channel. Add the backup bot and a separate main
   bot as administrators. Enter the new channel ID and main bot token, confirm,
   then **Copy to new main channel**.
3. Wait for **Recovered drive verified**, type the channel ID and choose
   **Activate recovered drive**. Restart when asked.

The backup channel's files stay in place and keep their backup role.

### Backup channel lost

Create a new private backup channel and bot, then **Backup settings → Replace
backup channel**. Wait for **Backup confirmed**. Nothing is erased in Telegram.

## Interruptions

**Pause** keeps progress; **Resume** and **Retry** reuse copies already
confirmed. Progress is saved in MongoDB, so a restart or a new server continues
the same work. Telegram wait times survive restarts. Missing parts or wrong sizes
stop confirmation and keep the last confirmed backup.

**Activity and report → Download report** omits tokens and keys, but file names
and channel IDs can still be private.

## Start a new drive

For a surviving 2.0–2.2 database, use the
[in-place upgrade](operations.md#upgrade-from-22-or-earlier). For an old archive,
use [version-compatible recovery](#upgrade-or-restore-an-older-drive).
**In-place reset is temporarily unavailable.** The start-new API rejects a valid
reset request with `409 START_NEW_UNAVAILABLE` and preserves the existing drive.

For an intentional fresh start, create a separate deployment with a **new
dedicated MongoDB database, new private storage channel and separate persistent
volumes/project name**, following [Setup](setup.md). Give that new drive its own
`ENCRYPTION_KEY`; retain the old database, channel, volumes, configuration and
original key for recovery. Do not clear old volumes or unpin old backups as a
shortcut. Starting an empty drive does not import or erase the old files.

For Compose on the same host, stop the old deployment and include a new project
name such as `-p tgdrive-new` in every new deployment command. The supplied
Compose files name the project explicitly; copying them to another folder alone
does not select separate volumes.

## Upgrade or restore an older drive

A surviving **schema-4 database from TGDrive 2.0–2.2 upgrades in place** before
the drive opens. Follow [upgrade and rollback](operations.md#upgrade-and-rollback),
keeping the database, channel, volumes and old recovery material.

Current archive/channel activation requires **schema 5** and the original
`ENCRYPTION_KEY`. A v2.2 archive is not a current-format import. Recognized legacy
backups report `LEGACY_BACKUP_REQUIRES_UPGRADE`; preserve the original archive
and its old matching key. A validation/key error alone does not establish
archive corruption.

1. Keep the original archive/key and old deployment unchanged. Stop the old
   instance before recovery work against its channels.
2. Restore with **tagged v2.2.0** and its matching old key into an isolated empty
   MongoDB database and separate private volumes. Use the tag's
   [backup guide](https://github.com/TechShreyash/TGDrive/blob/v2.2.0/docs/backup-recovery.md)
   and bundled setup instructions; it has the older backup-key model.
3. Stop v2.2, then follow the [schema-4 upgrade procedure](operations.md#upgrade-from-22-or-earlier)
   on that restored database. Keep the newly configured `ENCRYPTION_KEY` for the upgraded drive
   as well as the original old archive/key for rollback.
4. Verify files, shares, multipart work and a new confirmed restore point before
   retiring old resources. Do not run v2.2 against the upgraded database.

Rehearse with disposable copies and the actual deployment/dependency versions
before relying on this as the only recovery path. Do not replace or reset the
only recoverable copy to work around format incompatibility.
