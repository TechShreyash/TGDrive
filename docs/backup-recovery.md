# Backup & recovery

[Docs home](README.md) · [Operations](operations.md)

TGDrive keeps your whole drive in MongoDB and your files in Telegram. Backups
are encrypted with your `ENCRYPTION_KEY` and pinned in your storage channel, so
you never manage a separate recovery key or file.

> [!IMPORTANT]
> Keep three things safe: your **`.env`** (especially `ENCRYPTION_KEY`), your
> **private storage channel**, and its **bot token**. Keep both channels private,
> never share them, their bots or the key, and turn channel auto-delete off.
> Without `ENCRYPTION_KEY`, no file or backup can be read by anyone.

## What you need after a loss

| You lost | What brings the drive back |
| --- | --- |
| The server | Deploy anywhere with the **same `.env`** and **same `MONGODB_URI`**. Everything opens as before, including backup-channel settings and progress. |
| MongoDB | Deploy with the **same `.env`** and an **empty** MongoDB. TGDrive finds the pinned backup in your storage channel and shows **Restore drive**. One click restores it. |
| MongoDB and the main channel | Same `.env` plus the **backup channel ID and its bot token**, entered on the restore screen. Needs an [independent backup channel](#add-a-backup-channel). |
| `ENCRYPTION_KEY` | Nothing. Files and backups stay unreadable. Store the key in a password manager. |

Same env + same MongoDB is never a restore: the drive simply opens.

## How backups work

- TGDrive saves a backup every hour (`MONGODB_BACKUP_SECONDS`) and when you choose
  **Storage & Backups → Back Up Now**.
- Each backup is one file: every MongoDB collection, compressed and encrypted
  (AES-256-GCM) with a key derived from `ENCRYPTION_KEY`. Browser sessions are
  left out.
- The file is posted and **pinned** in the storage channel, and in the backup
  channel when one is configured. Grant the storage bot **Edit Messages** so it
  can pin.
- **Download backup** gives you the same file for an off-Telegram copy. It opens
  only with the same `ENCRYPTION_KEY`.
- MongoDB is still the live database. Use your MongoDB host's backups too if you
  want point-in-time history.

## Restore after losing MongoDB

1. Create an empty MongoDB database and set `MONGODB_URI` to it. Keep the rest of
   the `.env` unchanged.
2. Start TGDrive and sign in with your usual password.
3. On **Storage recovery**, check the backup's date and file count, then choose
   **Restore drive**.
4. TGDrive checks every stored file part in Telegram, restores the database and
   saves it. **Restart** TGDrive when asked.
5. Check a few files, shares and Connections. Telegram user accounts need to sign
   in again.

If no backup is pinned, TGDrive starts a new empty drive instead. If a pinned
backup exists but `ENCRYPTION_KEY` cannot open it, startup stops and nothing is
changed: start again with the original key.

<details>
<summary>Restore from a downloaded file instead</summary>

Open **Restore backup** on the recovery screen, choose your `.tgdb` file and
**Inspect backup**. If a saved bot token was revoked, open **Channel access
changed?** and enter a working token for the surviving channel. Continue with the
steps the screen shows.

</details>

## Add a backup channel

A second private channel keeps a copy of every file, so losing the main channel
does not lose files.

1. Create a private channel and a separate bot, ideally from **another Telegram
   account**. Add that bot as an administrator in **both channels** with post,
   edit, delete and pin rights.
2. Choose **Storage & Backups → Set up backup channel**, enter the channel ID and
   bot token, confirm, then **Verify and start backup**.
3. Leave TGDrive running until **Backup confirmed**. The browser can close.

Copies happen inside Telegram without downloading files. New changes are grouped
for up to 60 seconds. Encrypted files stay encrypted in the copy.
[Pacing and verification](architecture.md#independent-backup-pacing-and-verification).

### Main channel lost

1. On the recovery screen, open **Main channel lost: restore from the backup
   channel**, enter the backup channel ID and its bot token, then **Find backup**.
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

## Starting fresh

TGDrive only opens databases created with the current version and its
`ENCRYPTION_KEY`. A database from an older version is refused at startup: use a
new empty MongoDB database and a new private storage channel.
