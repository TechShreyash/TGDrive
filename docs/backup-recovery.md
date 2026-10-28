# Backup & recovery

[Docs home](README.md) · [Operations](operations.md)

To recover your drive, you need **a `tgdrive-backup.tgdb` recovery file, its
matching key, and access to a Telegram channel recorded in that file**.
The file restores saved database data; Telegram supplies the original file bytes.

> [!IMPORTANT]
> Wait for a confirmed backup. Keep the recovery file outside the server and its
> key separately, preferably in a password manager. Keep irreplaceable originals
> outside Telegram too. Never replace a working drive with an empty database as a repair.

[Save a backup](#keep-the-mongodb-recovery-kit-outside-the-server) ·
[Add a backup channel](#set-up-an-independent-backup-channel) ·
[Recover an older backup](#older-backups)

<a id="choose-the-right-mongodb-recovery-route"></a>

## Choose a recovery route

| Situation | Action |
| --- | --- |
| Restart, update or redeploy | Preserve MongoDB, private configuration and volumes; no restore. |
| App/machine lost, MongoDB survives | [Reopen the existing drive](#reopen-the-existing-mongodb-drive). |
| MongoDB lost | [Restore the saved database](#restore-a-lost-database-from-telegram) using a surviving channel and an empty replacement database. |
| Main channel lost | [Rebuild an empty main channel](#recover-a-lost-main-channel-from-a-surviving-channel) from the surviving independent backup. |
| Backup channel lost, main survives | [Replace the backup channel](#rebuild-a-lost-backup-from-the-surviving-main-channel) and wait for confirmation. |
| Both channels lost or inaccessible | The recovery file cannot recreate missing media. Use another surviving copy or external originals. |

Without an independent channel, the same recovery-file format restores database
data, but it still depends on the original main channel. Large files need every
recorded part. A **restore point** is the saved state at the time of the backup;
later changes are absent.

<a id="keep-the-mongodb-recovery-kit-outside-the-server"></a>

## Save your recovery file and key

1. Open **Tools & settings → Storage & Backups** and choose **Back Up Now**.
2. Wait for **Backup confirmed**, or the completed **Last Telegram Safety Copy**
   when no independent channel is configured. **Saved to MongoDB** alone does not
   mean a Telegram backup has completed.
3. Under **Backups & recovery**, choose **Download backup** and **Save recovery
   key**. Keep the recovery file off-server and the key separately. The restore
   form accepts the 64-character key or the contents of the exported key JSON.
4. Download an updated file after changes you need to recover, and after replacing
   either channel. Keep the previous usable file until the replacement is saved.

**Your login password cannot replace the recovery key.** Changing that password
does not change the key or invalidate backups. Save the key once while unchanged;
if you deliberately rotate it, retain the matching keys for older files.
A working installation can re-export a key it still holds.

TGDrive keeps the unified key privately in MongoDB and its local recovery state.
Reopening the surviving database on a new host keeps that key. A conflicting or
missing historical key requires repair; TGDrive must not silently replace it.
Your separate saved key remains necessary if both database and local state are lost.

Also retain Telegram administrator access and the private deployment/database
connection details needed to run TGDrive again. Revoked bot tokens can be replaced
during recovery; the backup cannot restore revoked access or a hosting account.

## Set up an independent backup channel

An independent channel keeps a second copy of completed files. Uploads continue
while copying; each upload is independently protected only after its restore
point confirms. Both copies still depend on Telegram.

### Prepare the Telegram accounts and channels

1. Create a private broadcast backup channel and dedicated BotFather bot using a
   **separate Telegram account**. Keep independent access to that account.
2. Add the backup bot as an administrator in **both channels**, with posting,
   editing, deletion and pinning access. It must be a different bot from every
   main storage bot; rotating a token does not change bot identity.
3. Keep both channels private, auto-delete **Off**, and content protection off
   where it prevents copying. Keep main bots' [standard permissions](setup.md#2-prepare-telegram).
4. Let TGDrive manage the channel documents. Do not delete referenced messages,
   change recovery pins or post unrelated content.

TGDrive checks access and channel settings, but cannot prove separate human ownership.

### Enable and confirm the first copy

- [ ] Choose **Set up backup channel**, enter its negative numeric ID and bot
  token, confirm the separate-account preparation, then **Verify and start backup**.
- [ ] Leave TGDrive running until **Backup confirmed**. The browser can close;
  the server continues. **100% bytes is not confirmation**: file parts and the
  recovery file must also verify.
- [ ] Download the confirmed recovery file and save its key outside the server.

First setup starts immediately. Later changes are grouped for up to 60 seconds
so nearby upload completions share a recovery file.
**Back Up Now**, **Sync now** and protection before permanent removal request a
backup without that batching wait. Telegram cooldowns and copying/verification
can still delay completion.

Copying happens within Telegram, without downloading and reuploading the media.
Each restore point saves an encrypted recovery file in both channels; separate
hourly database backups pause while this protection is enabled.
See [pacing and verification](architecture.md#independent-backup-pacing-and-verification).

### What this copy retains

The recovery file contains the saved database and both channels' file locations.
Original media remains in Telegram; encryption of the recovery file does not
encrypt the originals.

Previous remote files and recovery points remain available: this is not a
remote deletion mirror. Older backups can restore removed records or revoked
shares. Review sharing after restoration.

### Rebuild a lost backup from the surviving main channel

Keep the working main channel and database. Create an empty private backup channel
on the independent account, grant the backup bot access to both channels, then
choose **Backup settings → Replace backup channel**. Wait for confirmation and
download the updated recovery file. The previous remote channel is not erased.

For temporary permission/network trouble, repair access and resume the existing
operation rather than replacing the channel.

### Recover a lost main channel from a surviving channel

Use a private installation for recovery-file restores; this action is disabled
in demo mode, including for the demo administrator.

1. Open **Backups & recovery → Restore backup**, or **Storage recovery** if the
   catalog cannot open. Select your saved file, paste its key and choose
   **Inspect backup**. Keep the browser open until inspection finishes.
2. TGDrive tries the channels and bot access recorded in the file. If access has
   changed, open **Channel access changed?**, enter a working replacement bot
   token and optionally identify the surviving channel. Grant that bot the
   required access in Telegram first.
3. Create an **empty private main channel**. Add the surviving bot and a separate
   main storage bot as administrators. In the recovery form, leave database-only
   recovery unchecked, enter the new channel ID/main bot token and repeat the
   channel ID to confirm, then choose **Copy to new main channel**.
4. Wait for every recorded part and the recovered data to verify. If MongoDB was
   lost too, use a reachable empty or untouched automatically initialized
   replacement database; the same file supplies its saved data.
5. At **Recovered drive verified**, repeat the destination ID and choose
   **Activate recovered drive**. TGDrive switches only after verification and
   explicit activation. Restart the service/container when instructed; refreshing
   the browser is insufficient.
6. Check files, split downloads, shares and Connections. The surviving backup's
   documents remain intact and its valid backup configuration is retained.
   Reconnect optional Telegram account sessions as needed, then save the updated
   confirmed recovery file.

The server can continue copying with the browser closed. Preserve `DATA_DIR` and
resume the **same operation** after interruptions; do not change destinations or
clear the journal to bypass an error.

### Recover only the database when the surviving channel is healthy

After inspecting the file, choose **Restore the database only on this surviving
channel**. A replacement main bot token is optional if the saved access still
works. Repeat the displayed channel ID, then **Verify and prepare database
restore** and activate only after verification. Restart when instructed.

The selected channel becomes main. To keep a surviving backup channel in its
backup role after losing main, use the replacement-main flow above instead.

### Interruptions and reports

**Pause** preserves progress; **Resume** and **Retry** reuse confirmed copies.
Telegram waits survive restart and cannot be bypassed with extra bots or manual
Sync. Temporary MongoDB connection failures retry while the server runs;
permissions, invalid settings or lost ownership need repair. Missing documents,
wrong sizes or an invalid key block confirmation and leave the last confirmed
restore point intact. Permanent removal can remain blocked while protection is
paused or unconfirmed.

**Activity and report → Download report** omits tokens and keys, but file names
and channel IDs may still be private. Keep raw journals, session files and keys
private. **Download backup** keeps the latest confirmed local file. After its
replacement confirms, older known local copies can be removed; pending retry
files and remote restore points remain. Monitor disk space for interrupted or
older staging files; recovery staging respects the free-space reserve.

<a id="make-and-check-a-mongodb-backup"></a>

## Automatic database backups

Use [Save the recovery file and key](#keep-the-mongodb-recovery-kit-outside-the-server).
Without independent channel protection, automatic safety copies normally run
hourly (`MONGODB_BACKUP_SECONDS`). Failures leave the live database intact; repair
the displayed error and retry. Ordinary uploads do not wait for these snapshots.

<a id="what-the-encrypted-mongodb-archive-contains"></a>

## What the recovery file contains

New recovery files use one format for database and channel recovery:

| Included | Excluded or made inactive |
| --- | --- |
| Catalog, shares and recorded channel/part locations | Original Telegram media and unfinished local upload bytes |
| Saved settings, bot access and recovery credentials | Logged-in Telegram account sessions and browser sessions |
| Saved history and application records safe to restore | Unfinished jobs and in-progress changes are not replayed against the recovered channel |

Restored unfinished jobs become canceled history. Optional Telegram account
connections require sign-in again. The file does not restore MongoDB hosting,
network permissions, disposable caches or telemetry counters. Recheck old share
permissions after recovery.

The encrypted file is authenticated with AES-256-GCM, limited to **64 MiB
encrypted** and **1 GiB expanded**. TGDrive verifies the recovery file and
referenced media before confirmation or activation.
See [formats, limits and API contracts](reference.md#independent-channel-backup-and-recovery).

### Reopen the existing MongoDB drive

If MongoDB survives, follow [reinstallation](operations.md#reopen-after-losing-the-local-installation)
using the existing database and private configuration. Do not restore over it
merely because the app was reinstalled. Missing staged uploads still need their
original source files.

### Restore a lost database from Telegram

Prepare a private deployment with a reachable empty MongoDB database with
transaction support. Use the recovery file and key with **Restore backup**, then
follow [database-only recovery](#recover-only-the-database-when-the-surviving-channel-is-healthy)
if the main channel survives, or [replacement-main recovery](#recover-a-lost-main-channel-from-a-surviving-channel)
if it does not. A previously used unrelated database is not a recovery target.
Keep the old data/file/key until restoration is verified.

## Older backups

<details>
<summary>Existing database archives, channel capsules and their separate keys</summary>

Existing backups remain readable with their original keys. During an upgrade,
an already journaled old-format operation finishes in that format; you may
briefly see the former database archive plus `tgdrive-replica.tgdb` pair. New
restore points use the unified file. Do not delete old files or keys during
migration.

For a pinned older channel capsule, open **Restore backup → Older backups:
recover from a channel**. Supply its surviving channel, working bot token and
original key. Leave **Checkpoint message ID** empty to try the latest pin, or
enter the recorded message ID. A verified plain catalog checkpoint alone can
omit the key; it does not include the complete database.

For an older full encrypted MongoDB archive, open **Older backups: restore a
database archive**, under **Reopen an existing drive or restore an older archive**
in Storage & Backups, or directly on Storage recovery. Use the original archive
and its matching `MONGODB_BACKUP_KEY` from the saved `.env` key export, type
`RESTORE` and keep the browser open until the result. This legacy route accepts
up to 1 GiB and requires an empty or untouched automatically initialized database.
Restart after success; browser sessions are revoked.

Old channel and MongoDB keys can differ, and their recorded revisions can differ.
Legacy key exports retain those distinctions; never substitute a new key for an
old file's matching key. Review restored shares and credentials before resuming.

</details>

## Verify your recovery kit periodically

Rehearse with the same TGDrive version or source commit in a separate recovery
destination. Confirm the key opens the file, the recorded channel is accessible,
and representative files and split downloads work. Stop production while
checking a restored installation; keep one active instance per drive. A filename
or screenshot alone is not evidence of successful recovery.
