# Backup & recovery

[Docs home](README.md) · [Choose a recovery route](#choose-the-right-mongodb-recovery-route) · [Set up backups](#set-up-an-independent-backup-channel) · [Operations](operations.md)

> [!IMPORTANT]
> Save matching keys outside the server, wait for a **confirmed restore point**, and test recovery before relying on it. **Never run `init` to repair an existing drive.**

| Protection | Recovers | Limits |
| --- | --- | --- |
| Encrypted MongoDB archive | Catalog, part mappings, settings and shares | Cannot recreate deleted Telegram file bytes |
| Optional independent channel | Completed documents/parts and matching recovery catalogs | Only confirmed copies; both channels still depend on Telegram |
| Originals/backups outside Telegram | A separate provider recovery route | Check that your copies are readable |

Split files follow the same model: ordered part mappings are backed up, and independent copies include every constituent document. Unfinished uploads and device originals need separate protection.

<details>
<summary>Find Storage & Backups in the interface</summary>

![Storage & Backups showing saved catalog and temporary storage status](assets/storage-backups.png)

The [administration gallery](showcase.md#connections-and-backups) shows the
website's entry points. These captures do not demonstrate a completed backup or
recovery; follow the confirmation and recovery steps below.

</details>

## Choose the right MongoDB recovery route

| Situation | Route |
| --- | --- |
| Restart, update or redeploy | Keep MongoDB, secrets and volumes; no restore or initialization. |
| Server or managed hosting expires | [Keep the drive and move hosts](operations.md#when-a-hosting-plan-expires); preserve the catalog, Telegram access and recovery kit. |
| Machine/app folder lost, MongoDB survives | [Reopen the existing drive](#reopen-the-existing-mongodb-drive). |
| MongoDB lost or deliberate rollback | [Restore a `.tgdb` archive](#restore-a-lost-database-from-telegram) into an empty database. |
| Main Telegram channel lost | [Recover from an independent channel](#recover-a-lost-main-channel-from-a-surviving-channel). |
| Both channels inaccessible, or no matching key | Use surviving MongoDB, another usable backup/key pair or external originals. |

## Set up an independent backup channel

Enable this optional feature under **Tools & settings → Storage & Backups → Independent backup channel**. Normal work continues during synchronization; an upload is not independently protected until its restore point is confirmed.

### Prepare the Telegram accounts and channels

1. Create a private broadcast backup channel and dedicated BotFather bot using a **separate Telegram account**. Keep independent recovery access to it.
2. Give the backup bot administrator access to **both channels**, with **Post Messages**, **Edit Messages** and **Delete Messages**. Use a different bot from every main storage bot; a new token does not change identity.
3. Keep both channels private, auto-delete **Off**, and **Restrict Saving Content** off where it blocks copying. Keep main bots' [standard permissions](setup.md#2-prepare-telegram).
4. Change files through TGDrive. Do not edit/delete referenced documents, alter recovery pins or post unrelated content in these channels.

Prepare accounts and permissions in Telegram. TGDrive checks channel state and bot access, but cannot prove human ownership; the separate-account acknowledgment is your responsibility. Extra bots do not remove rate limits.

### Enable and confirm the first copy

- [ ] Choose **Set up backup channel**; enter its negative numeric ID and backup bot token. Confirm the separate-account preparation, then **Verify and start backup**.
- [ ] Save `tgdrive-channel-recovery-key.json` with **Save recovery key**. Keep it outside Telegram and the server.
- [ ] Let synchronization finish. You can close the tab; the server continues. **Pause** preserves progress; **Resume** continues confirmed work.
- [ ] Wait for **Backup confirmed**, check the revision, and save the key JSON **again**. Its `restore_point` records capsule message IDs in both channels; export again when keeping a newer point.

**100% bytes is not confirmation.** Destination documents and matching encrypted recovery metadata must verify in both channels. Later changes can remain pending.

### What this copy retains

The copy includes completed documents and an AES-256-GCM encrypted `tgdrive-replica.tgdb` capsule: catalogs, remapped part locations, recovery credentials and supported settings/connections. It is separate from a full MongoDB archive; **do not interchange their files or keys**. Document bytes are not separately encrypted by TGDrive.

| Recovery data | Main channel | Independent backup channel |
| --- | --- | --- |
| Encrypted channel recovery capsule | Saved | Saved and pinned |
| Full encrypted MongoDB archive, when using MongoDB | Current archive created or reused | Same confirmed archive copied and verified before **Backup confirmed** |
| Portable catalog, when using SQLite | Normal pinned `drive.json` checkpoint | Included in the encrypted recovery capsule |

Snapshots cover their confirmed revision. A failed setup does not establish a confirmed copy in the new backup channel. Save the recovery-key JSON outside the server and Telegram after confirmation.

Old documents/capsules remain: this is not a deletion mirror. Older recovery points can restore removed records and revoked shares. Review sharing after recovery. Pausing backup can also hold permanent catalog removal until protection resumes or is explicitly disabled; ordinary browsing, uploads and metadata changes continue. See [deletion behavior](usage.md#why-permanent-deletion-keeps-the-telegram-files).

### Rebuild a lost backup from the surviving main channel

<details>
<summary>Replace only the backup destination</summary>

Preserve the working main channel/catalog. Create an empty private backup channel on the independent account and grant the backup bot access to both channels. Choose **Backup settings → Replace backup channel**, verify the new destination, wait for confirmation and save an updated key export. Replacement leaves the old remote channel/capsules intact.

For permission/network trouble, repair access and Retry/Resume the existing operation instead. Disabling automatic backup stops new protection; it does not confirm pending changes or erase existing copies.

</details>

### Recover a lost main channel from a surviving channel

**Only files in the selected confirmed restore point can be recovered.** Keep the source, saved key JSON and persistent `DATA_DIR` intact. Recovery pauses file mutations until activation or explicit cancellation.

<details>
<summary>Inspect, copy, verify and activate a replacement drive</summary>

1. Open **Independent backup channel → Recover a channel or catalog**, or the authenticated **Storage recovery** screen if the catalog cannot open.
2. Enter the surviving channel ID, a backup bot token with access, and the JSON's 64-character **`key` value**, not the whole JSON. Leave the key empty only for a verified plain catalog checkpoint.
3. Leave **Checkpoint message ID** empty to try the latest pinned capsule. If the original main channel's ordinary checkpoint owns the pin, enter `restore_point.source_message_id` from the export. Use `backup_message_id` for that recorded backup-channel point.
4. Choose **Inspect recovery copy** and check its source, revision, files and bytes. Inspection does not activate a drive; a catalog cannot replace missing source documents.
5. Create an **empty private main channel**. Add the surviving backup bot and a separate main storage bot as administrators. Enter its ID/main bot token, repeat the destination ID, then **Copy to new main channel**. The source stays unchanged.
6. Wait for all parts and recovery metadata to verify. Repair errors and resume **the same operation** after reload/restart; preserve its server-side candidate and journal.
7. At **Recovered drive verified**, repeat the channel ID and choose **Activate recovered drive**. Copying alone does not switch drives. Restart the service/container when instructed; browser refresh is insufficient. The former catalog/configuration remain for review.
8. Check representative images, videos, PDFs, split downloads, Starred items, shares and Connections. Restore optional bots/sessions as needed, configure a separate independent backup, confirm it and export the current key/restore point.

</details>

### Recover only the database when the surviving channel is healthy

<details>
<summary>Use the surviving documents without copying the library</summary>

After inspecting a capsule, choose **Restore the database only on this surviving channel**. Supply a main bot distinct from the backup bot and repeat the surviving channel ID.

TGDrive verifies referenced parts/sizes. Activation is still explicit and requires a service restart. The surviving channel becomes main; configure a separate backup afterwards. Use this only when the channel and required documents remain accessible.

</details>

### Interruptions and reports

Retry/Resume the **existing operation** after repairing the underlying problem. Preserve `DATA_DIR`, its journal, bootstrap and activation files across redeployments. Never run competing instances, delete that state or change destinations to bypass an error.

<details>
<summary>Failure handling, key rotation and local recovery state</summary>

| Problem | Action |
| --- | --- |
| FloodWait/network/uncertain send | Observe the retry deadline; resume confirmed work. More bots do not bypass waits. |
| Process stopped/paused | Restart one instance or choose Resume. Later unconfirmed changes remain unprotected. |
| Bot/access/privacy/copying failure | Repair credentials, permissions and private-channel/content settings. |
| Missing document, wrong size, invalid key/capsule | Keep sources; select the correct key/point or another original. Never mark success manually. |
| Disk/database failure | Repair storage/access while retaining journal/catalog, then retry. |

During bot sign-in, Telegram can redirect a fresh connection to another data center. TGDrive retries authorization once after that handoff, within the 30-second sign-in deadline. If setup still fails, choose **Verify and start backup** again; for an existing backup, Retry/Resume the same operation. Message publication retries remain controlled by the saved operation journal. An unresolved numeric channel ID reports which channel needs its ID or bot access corrected.

**Activity and report → Download report** gives progress and actionable failures without tokens/keys. Names and IDs may still be private; review before sharing. Never share raw private state or keys.

The full MongoDB archive's revision can differ from the channel capsule's revision; they are distinct restore points. Keep both keys. In the JSON, `key` and `mongodb_backup_key` match the recorded `restore_point`; `next_backup_key` and `next_mongodb_backup_key` describe the next generation. Preserve the whole export and older pairs. If pending work captured an older MongoDB key, restore that key or verify **Backup settings** again to capture a fresh snapshot.

Private `DATA_DIR/replica-bootstrap.json` retains owner recovery credentials/password hash. It does **not** provide offline login: MongoDB must be reachable with valid permissions, or startup may fail. A missing/corrupt catalog with reachable MongoDB can permit owner web recovery.

Staged capsules and previous archives are retained. Monitor disk space; staging stops before the configured free-space reserve. Keep useful points off-server and rehearse recovery to a separate test destination.

</details>

## Keep the MongoDB recovery kit outside the server

Keep these private and accessible even if TGDrive, Coolify and MongoDB are down:

- [ ] A confirmed `.tgdb` archive or access to its Telegram channel.
- [ ] Its matching **`tgdrive-recovery-key.env`**, exported via **Storage & Backups → Save Recovery Key**.
- [ ] MongoDB URI/database name, owner login, original deployment secrets/configuration and Telegram administrator access.

The `.env` export contains `MONGODB_BACKUP_KEY=` and a 64-character hexadecimal value. Store it separately from archives: anyone with both can read backed-up credentials/catalog. Your login password cannot decrypt an archive.

Container setup may generate this key in `DATA_DIR/deployment-secrets.json`; it still needs an independent copy. Do not regenerate it during redeployment. After rotation, keep old keys with old archives; the export action saves only the current MongoDB key.

## Make and check a MongoDB backup

1. In **Storage & Backups**, choose **Back Up Now**.
2. Wait for **Last Telegram Safety Copy** confirmation. “Saved to MongoDB” alone is not a Telegram backup.
3. Save the matching key separately; optionally download the confirmed `.tgdb` document for an off-server copy.
4. Rehearse restoration into a separate empty database. Stop production while checking the restored installation; keep one active instance per drive.

Automatic snapshots normally run hourly (`MONGODB_BACKUP_SECONDS`). Failures leave the live database intact; check the displayed error/retry time, posting access, key and temporary disk. See [configuration](reference.md#persistence-and-initialization).

## What the encrypted MongoDB archive contains

| Included | Excluded |
| --- | --- |
| Catalog and ordered Telegram part mappings | Original Telegram file bytes |
| Shares, settings, Telegram/owner credentials | MongoDB URI/name and machine paths |
| Transfer, preparation, deletion and recovery records | Unfinished upload bytes, disposable caches and telemetry counters |

<details>
<summary>Archive verification and encryption limits</summary>

Snapshots are compressed app records, limited to 1 GiB expanded; unrelated MongoDB collections are excluded. AES-256-GCM authenticates the archive. TGDrive downloads the uploaded ciphertext and checks SHA-256 before confirming; restore checks authentication, format and completeness before writing.

This encryption covers database archives, not original Telegram payloads or local upload/cache bytes. A catalog cannot recreate a deleted Telegram part.

</details>

### Reopen the existing MongoDB drive

Follow the [reinstall instructions](operations.md#reopen-after-losing-the-local-installation): reuse the existing database and original secrets, preserve volumes, and start **without `init`**. Check Connections, files, split downloads and unfinished work. Missing staged bytes still require the original source.

### Restore a lost database from Telegram

Use a confirmed `.tgdb` archive, its matching key and the matching TGDrive release. Restore into a **new empty MongoDB database with transaction support**. Changes after the snapshot will be absent; **older shares may reappear**.

<details>
<summary>Restore commands and post-recovery checks</summary>

1. Download or copy the archive into the matching release's project folder as `backup.tgdb`. Stop TGDrive and prepare a **new empty MongoDB database with transaction support** ([requirements](setup.md)). Do not initialize it.
2. Create a private `.env.restore` in that folder with only the destination URI/name and archive's original key:

   ```dotenv
   MONGODB_URI=<destination-private-uri>
   MONGODB_DATABASE=<new-empty-database-name>
   MONGODB_BACKUP_KEY=<original-64-character-hexadecimal-key>
   ```

   Use plain `NAME=value` lines with no surrounding quotes: `docker run --env-file` treats quotes literally, unlike Compose's `.env` handling. On Linux/macOS/WSL, restrict access with `chmod 600 .env.restore`.

3. Build the dedicated maintenance image from that project folder. It needs only those three settings:

   ```bash
   docker build -t tgdrive:restore .
   ```

   **Windows PowerShell:**

   ```powershell
   $restoreArchive = (Resolve-Path -LiteralPath .\backup.tgdb).Path
   docker run --rm --env-file .env.restore --mount "type=bind,src=$restoreArchive,dst=/restore/backup.tgdb,readonly" tgdrive:restore python -m tgdrive restore-mongodb --file /restore/backup.tgdb
   ```

   **Linux/macOS/WSL Bash:**

   ```bash
   docker run --rm --user "$(id -u):$(id -g)" \
     --env-file .env.restore \
     --mount "type=bind,src=$(pwd)/backup.tgdb,dst=/restore/backup.tgdb,readonly" \
     tgdrive:restore python -m tgdrive restore-mongodb --file /restore/backup.tgdb
   ```

   Both commands mount the archive read-only and leave deployment volumes intact. Bash uses your host user ID so it can read a private archive. The database must be reachable from the container; URI `localhost` means that container. Permit the restore machine in Atlas Network Access. Coolify users run this on a Docker machine with the matching checkout after stopping production.

4. Point the deployment to the recovered URI/database and recreate/redeploy one instance, preserving the original storage channel/keys. Remove stale credential overrides or supply originals as in [reinstalling](operations.md#reopen-after-losing-the-local-installation). **Do not run `init`.** Sign in again; restored browser sessions are revoked.
5. Check folders, representative downloads, split files, Connections and **restored share permissions**. Keep the old database/archive/key until verified, then make a fresh confirmed backup and export its key.

Restore refuses an existing catalog/nonempty TGDrive collections; it does not merge. Invalid keys/damaged archives fail before catalog writes; database write failure rolls back the transaction. Check the destination rather than deleting records to bypass refusal.

</details>

## Verify your recovery kit periodically

Use the same release and a separate empty database to prove the archive decrypts, the catalog opens and Telegram payloads remain readable. A filename alone is not evidence. Keep unfinished-upload originals until completion.

See [exact archive/API boundaries](reference.md#security-and-privacy) and [operational troubleshooting](operations.md#troubleshooting).
