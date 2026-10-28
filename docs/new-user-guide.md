# TGDrive basics

[Docs home](README.md) · [Set up TGDrive](setup.md) · [User guide](usage.md)

TGDrive is a free, open-source web drive for files stored in your own Telegram
channel. Upload files and folders, preview them in your browser and share links.
It is an independent project, not an official Telegram service.

## What TGDrive needs

| Part | Its job |
| --- | --- |
| Private Telegram channel | Stores your files, sometimes split across several messages. |
| MongoDB database | Keeps the **catalog**: names, folders, settings, shares and the locations of every file part. |
| Your computer or server | Runs the website and transfers; holds unfinished uploads and temporary cached copies. |

The host must stay on with access to Telegram and MongoDB for the website and
share links to work. Stopping it does not delete completed files. Telegram
messages alone cannot rebuild your folders or the mappings for split files;
keep the database or a usable backup too.

[Choose local or server hosting](hosting.md#local-computer-or-server) ·
[Where data is saved](operations.md#what-is-saved-where)

## Before your first important upload

1. Upload a non-private sample, wait for **Completed**, then download and open it.
2. Try a share link in a signed-out browser, then revoke it. Share selected files
   instead of giving someone your owner password.
3. [Save a confirmed recovery file and its matching key](backup-recovery.md#keep-the-mongodb-recovery-kit-outside-the-server).
   Keep the file outside the server and Telegram, and the key separately.

In the [public demo](usage.md#try-the-public-demo), use disposable files only:
other visitors can change unprotected content.

## Keep these essentials in mind

- **Wait for the upload status.** Keep the tab open until **You can close this
  tab**. The host can then finish accepted work without your browser, while
  TGDrive keeps running. Keep source files until **Completed**. Optional channel
  protection has a separate **Backup confirmed** status.
- **Let TGDrive manage its channel.** Use a dedicated private broadcast channel
  with auto-delete **Off**. Upload through TGDrive or its configured bot; do not
  edit storage messages, clear channel history, alter recovery pins or add
  cleanup bots. [Telegram setup](setup.md#2-prepare-telegram).
- **Keep another copy of valuable files.** TGDrive has no total storage cap,
  but file-size, database, disk and provider limits apply. Permanent access is
  not guaranteed. A second Telegram channel still depends on the same provider.
- **Keep recovery material usable.** A recovery file saves database data; it
  still needs a surviving channel containing the original bytes. It excludes
  unfinished uploads. Its key is separate from your login password; losing it
  can make that backup unusable. [Backup options](backup-recovery.md).
- **Protect access.** The owner password grants full shared access. Anyone
  holding a valid share link can read its selected content. Use a unique
  password and HTTPS for remote access; keep credentials and keys private.
- **Preserve existing state when fixing problems.** Run one active installation
  per drive. Do not replace its database with an empty one, prune its volumes
  or delete unfinished uploads to clear an error. Use the existing **Retry** or
  resume action and reselect originals when asked. [Recovery and troubleshooting](operations.md).

## Common questions

**Are my files private and encrypted?**  
TGDrive does not add end-to-end encryption to original files, staging or cache.
Trust the hosting operator, storage-channel members and people with owner access.
Encrypted recovery files do not encrypt the originals. For stronger confidentiality,
encrypt files before upload and keep decryption keys separately; ordinary previews
will be unavailable. Only open downloads you trust: TGDrive does not scan for malware.
[Security details](reference.md#security-and-privacy).

**Do I need Premium or paid hosting?**  
No. All app features are free; hosting and database resources may cost money.
Premium is optional, including for automatic splitting of large files.
[Upload limits](configuration.md#upload-size-and-available-disk-space) ·
[Hosting choices](hosting.md).

**Does uploading a folder keep it synchronized?**  
No. It copies the selected files once. Later local edits do not sync; selecting
that folder again can create another copy. Resume the existing upload when
recovering an interruption. [Upload guide](usage.md#upload-files-or-folders).

**Will previews or downloads change the original?**  
Web uploads preserve the original bytes. Choose **Download Original** to retrieve
them; preview support depends on the format and browser. Send photos to the upload
bot **as files** to preserve quality. [Preview and download options](usage.md#open-preview-and-download).

**Where do I go next?**  
[Install TGDrive](setup.md), [learn everyday tasks](usage.md), or
[fix a problem](operations.md#troubleshooting). When asking for help, remove
credentials, private filenames and share/source URLs from screenshots and logs.
