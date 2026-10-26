# New-user guide: read before storing your files

[Docs home](README.md) · [Setup](setup.md) · [How to use TGDrive](usage.md) · [Backup & recovery](backup-recovery.md)

Everyone using or operating a TGDrive installation should understand this page,
including people using managed hosting. Installation commands live in
[Setup](setup.md); this guide explains what to expect and how to protect your drive.

## What TGDrive is

TGDrive is a free, open-source web app that gives files in your own Telegram
channel a drive-style interface: folders, search, uploads, previews, readers and
share links. It is an independent project, not an official Telegram storage service.
The [MIT License](../LICENSE) permits use, modification and redistribution.

| Part | What it holds or does |
| --- | --- |
| Your private Telegram storage channel | Completed file documents, including the parts of split files |
| Your MongoDB database | The file/folder catalog, part locations, settings and other saved app state |
| Your TGDrive computer or server | Runs the app, handles transfers and previews, and holds temporary uploads, caches and private configuration |

You need the app and its dependencies available to use the drive. A folder full
of Telegram documents is not a replacement for the catalog that organizes them.
[Where data is saved](operations.md#what-is-saved-where).

**No total stored-byte quota does not mean unlimited file size, permanent
retention or immunity from account restrictions.** Telegram access, app limits,
database capacity, server resources and backups still matter. A private channel
restricts membership; TGDrive does not add end-to-end encryption to stored files.

## The essentials everyone must know

- **Keep valuable files outside Telegram too.** Do not make TGDrive your only
  copy of an irreplaceable file. A second Telegram channel still uses the same provider.
- **Wait for the right status.** Keep the upload tab open until **You can close
  this tab**, and keep source files until **Completed**. Independent protection
  requires its own **Backup confirmed** status.
- **Treat the storage channel as app-managed.** Use a dedicated private broadcast
  channel with auto-delete **Off**. Make file changes through TGDrive.
- **Protect the catalog and recovery material.** Preserve MongoDB, channel access,
  private configuration and persistent volumes. Save matching recovery keys and
  restore-point exports outside both the server and Telegram.
- **Know who can access your drive.** The owner password grants full shared
  access. Anyone holding a valid share link can read its scope.

## Do and don't

| To keep the drive working and your data recoverable | Avoid |
| --- | --- |
| Keep storage bots as channel administrators with the [required permissions](setup.md#2-prepare-telegram), and preserve owner access to Telegram and MongoDB. | Removing bots, revoking tokens or changing the configured channel/database without following the maintenance or recovery guide. |
| Keep channels private, auto-delete off and recovery pins under TGDrive's control. Use TGDrive or its configured upload bot to add files. | Clearing channel history, manually editing/deleting storage messages, posting unrelated content, or adding cleanup/moderation bots. |
| Leave real disk space for unfinished uploads and caches; respect displayed Telegram cooldowns. | Assuming Telegram storage eliminates server disk needs, or repeatedly retrying/restarting to bypass a provider wait. |
| Use existing **Retry** or resume controls and reselect the same originals when asked. | Deleting upload staging as if it were disposable cache, or assuming a full device progress bar means the file is finished. |
| Run one active TGDrive instance per drive; preserve its saved state during upgrades or a move. | Running competing instances, reinitializing an existing drive, or deleting persistent volumes as a troubleshooting shortcut. |
| Confirm backups, export matching keys and rehearse recovery using the [backup guide](backup-recovery.md). | Treating an enabled backup switch, 100% copied bytes, a recovery key alone or a database-only archive as a complete file backup. |
| Use HTTPS for remote access, a unique owner password, Telegram two-step verification and secure database access. Share selected items through scoped links. | Publishing credentials, channel invitations or recovery keys, or giving the owner password to people who should only download a file. |
| Store and share files you are entitled to use, following [Telegram's terms](https://telegram.org/tos). Keep independent copies of important originals. | Assuming a private channel permits prohibited activity or guarantees that Telegram will never restrict access. |

If something breaks, keep the existing state and read the error before changing
configuration. Start with [Operations](operations.md#troubleshooting). **Never
run `init` to repair an existing drive.**

## Frequently asked questions

<details>
<summary>Why use TGDrive if I can already upload files to a Telegram group?</summary>

Uploading in Telegram is useful on its own. TGDrive adds a file/folder catalog,
browser previews and readers, resumable uploads and scoped sharing. Its large
uploads can span several Telegram documents while appearing as one file in TGDrive.

TGDrive's storage destination must be a dedicated **private broadcast channel**,
not an ordinary group chat. Posting files directly into that channel is not the
supported import workflow. Use the website or the configured Telegram upload bot.
[Upload workflows](usage.md#upload-files-or-folders).

</details>

<details>
<summary>Is it the same as Google Drive or OneDrive?</summary>

It offers similar file-management tasks, using Telegram for file storage.
That comparison does not promise identical features or service guarantees.
TGDrive is a web drive, not an automatic desktop-folder sync client or an office
collaboration suite. It needs a running host, Telegram access and its catalog.
[See the features](../README.md#features) and [local versus server use](hosting.md#local-computer-or-server).

</details>

<details>
<summary>Is TGDrive free and open source? Do I have to buy hosting?</summary>

TGDrive is MIT-licensed and all app features are available for free. Run it on
your computer or your chosen server. Hosting, internet access and database
resources can have their own costs. [Managed hosting](hosting.md) is optional;
it is help operating the same app, not a paid feature tier.
Contributions, feedback, bug reports and feature requests are welcome on [GitHub](https://github.com/TechShreyash/TGDrive/issues).

</details>

<details>
<summary>Does unlimited storage mean my files stay forever?</summary>

No. TGDrive does not meter the total bytes stored in your channel, and Telegram
advertises [free unlimited cloud storage](https://telegram.org/faq_premium#q-is-telegram-still-free).
That does not establish a permanent-retention guarantee. Deletion, lost access,
account or channel restrictions, provider changes and operational failures can
make files unavailable. Keep an independent backup of important files.

</details>

<details>
<summary>Can I upload any size of file? Do I need Telegram Premium?</summary>

Telegram describes [2 GB uploads for ordinary accounts and 4 GB with Premium](https://telegram.org/blog/700-million-and-premium#4-gb-uploads).
TGDrive handles larger logical files by splitting them into bounded Telegram
documents and reconstructing reads/downloads. Premium is optional for this.
The catalog's part mappings and all required parts must survive.

TGDrive defaults to **16 GiB per file** and a **16 GiB temporary upload budget**.
Larger files require raising the relevant limits and enough actual staging disk;
configured and technical ceilings still apply. The catalog also has a
**250,000 file/folder limit**. See [upload settings](configuration.md#upload-size-and-available-disk-space)
and [exact capacity limits](reference.md#file-capacity-staging-and-work-limits).

</details>

<details>
<summary>Can I rely on a 3–4 TB library working?</summary>

A multi-terabyte **library** and a single multi-terabyte **file** are different
claims. No TGDrive aggregate-byte quota does not establish tested reliability or
performance at every scale. Item count, file sizes, database capacity, server disk,
network and Telegram behavior all matter.

An older release's personal storage experience is not a current-release scale
benchmark. Do not present 3–4 TB as verified without workload-specific evidence.
Start with representative files, grow gradually and keep a separate recovery
copy. [Performance factors](performance.md#follow-the-data-path).

</details>

<details>
<summary>Can Telegram see or act on files in a private channel? Can it ban an account?</summary>

Private does not mean invisible to the provider. TGDrive uses Telegram cloud
storage, not Secret Chats. Telegram's [privacy policy](https://telegram.org/privacy#3-3-1-cloud-chats)
describes cloud storage, abuse checks and possible account restrictions or bans.
TGDrive cannot promise ban immunity or quote a reliable ban probability or safe
storage-size threshold. Seeing other large channels is not evidence of either.

Follow [Telegram's terms](https://telegram.org/tos), keep account access secure
and plan for a loss of access. A restriction on one account does not prove that
every channel copy is lost, but neither does another channel guarantee recovery.

</details>

<details>
<summary>Are my files end-to-end encrypted? Who else can read them?</summary>

TGDrive does not encrypt original payloads or local staging/cache bytes. Its
encrypted database backups do not make the original files end-to-end encrypted.
Treat the hosting operator, people with server or storage-channel access, and
people with owner credentials as trusted parties. See the
[security boundaries](reference.md#security-and-privacy) and Telegram's distinction
between [Cloud Chats and Secret Chats](https://telegram.org/faq#q-how-do-you-encrypt-data).

For files needing stronger confidentiality, encrypt them yourself before upload
and keep decryption keys separately. TGDrive will not decrypt those files for
ordinary previews; expect to download and decrypt them locally. TGDrive also
does not scan uploads for malware, so only open downloads you trust.

</details>

<details>
<summary>Do two channels on different accounts make my data ban-proof?</summary>

No. TGDrive can optionally maintain a recovery copy in a second channel using a
separately controlled Telegram account and backup bot. Configure it explicitly,
wait for **Backup confirmed**, and save the matching recovery-key export outside
Telegram and the server. Newer changes can still be pending.

Recovery depends on a surviving accessible copy, the selected confirmed restore
point and usable recovery material. It requires the recovery procedure, not
automatic failover. Both channels remain on Telegram; keep another copy outside
that provider for risks affecting both. [Independent backup setup and recovery](backup-recovery.md#set-up-an-independent-backup-channel).

</details>

<details>
<summary>What does a database backup protect? Is Completed enough?</summary>

A database backup protects the catalog, part locations and saved app state; it
cannot recreate missing Telegram file bytes. Independent channel protection
also copies completed documents and their recovery metadata. Neither includes
unsent device originals or unfinished uploads as completed files.

**Completed** means the upload finished, not that an independent backup is
confirmed or the file is permanently safe. The upload no longer requires its
local source, but valuable files should still have a separate readable copy.
[Choose a recovery route](backup-recovery.md#choose-the-right-mongodb-recovery-route).

</details>

<details>
<summary>Can I close my browser, turn off my laptop or let hosting expire?</summary>

Keep the upload tab open until **You can close this tab** appears. After that,
the server can finish accepted work while TGDrive runs; keep source files until
**Completed**. URL imports and manual preloads can also continue on a running host.

If the laptop is the host, sleeping or stopping it stops the service. A separate
server can keep working independently. Shared links need a reachable running
app. Before hosting expires, preserve the database, credentials, volumes and
recovery kit, then follow the [host-move guide](operations.md#when-a-hosting-plan-expires).

</details>

<details>
<summary>Can I clean up Telegram messages, change channels or reinstall to fix an error?</summary>

Do not edit/delete TGDrive's Telegram storage messages, enable auto-delete or
change its recovery pins. Those documents may be live file parts or recovery
material. A catalog backup cannot restore their deleted bytes.

A reinstall should reopen the existing drive with its original database,
configuration and channel access. Preserve persistent volumes and upload staging;
do not reinitialize, prune volumes or switch channel/database IDs to bypass an
error. Follow [channel damage](operations.md#if-someone-changed-the-telegram-channel)
or [restart/recovery guidance](operations.md#after-a-shutdown-crash-or-network-loss).

</details>

<details>
<summary>What if my Telegram owner account becomes inactive or I lose my number?</summary>

Secure the owning account and its recovery methods, review active sessions and
Telegram's inactivity-deletion setting, and keep independent recovery access.
The [current privacy policy](https://telegram.org/privacy#10-4-account-self-destruction)
lists a configurable 18-month default inactivity period. Do not assume bot traffic
keeps the human owner's account active.

Account deletion does not necessarily delete every channel: Telegram says created
channels can become orphaned while other administrators retain rights. That still
puts ownership and recovery at risk. [Telegram's account deletion explanation](https://telegram.org/faq#q-how-do-i-delete-my-account).

</details>

<details>
<summary>Can several people use the drive? Do share links identify a particular person?</summary>

People signing in with the owner password share full access, including files and
settings. There are no separate personal accounts or per-person permissions.
Give that password only to fully trusted co-owners. TGDrive has no built-in second
factor for its owner login; Telegram two-step verification protects the Telegram
account, not the TGDrive web password.

For selected read-only access, use a share link. Its label is for your organization,
not recipient authentication. Anyone with the valid URL can read its scope; a
folder link includes accessible descendants, including files added later.
Expiry or revocation stops future access, not copies already downloaded.
[Sharing rules](usage.md#share-and-revoke).

</details>

<details>
<summary>Do recipients need Telegram? Can every file play in the browser?</summary>

Recipients can use a valid TGDrive share link in a browser without the owner's
Telegram login. The TGDrive host still needs its Telegram access and must remain
reachable. Preview support depends on the file format, codec, browser, device
and reader limits; storing a file does not guarantee an inline preview.
Use **Download Original** when needed. [Preview and download guide](usage.md#open-preview-and-download).

</details>

<details>
<summary>Does no storage quota mean unlimited speed or no cooldowns?</summary>

No. Server resources, your connection and recipients' connections affect speed.
Telegram's [file API](https://core.telegram.org/api/files) also describes transfer
limits and wait responses. Respect TGDrive's displayed retry deadlines; more bots
or higher concurrency do not cancel provider limits. Cache/preload data is
temporary acceleration, not a backup. [Performance guide](performance.md).

</details>

<details>
<summary>Does permanent deletion securely erase all copies?</summary>

No. Trash supports restoration. Permanent deletion removes TGDrive catalog
references and associated access; it cannot be undone through Trash. It does not
erase Telegram originals, caches, old backups or recipients' copies. Older
restore points can reintroduce records or shares; review access after recovery.
[Deletion behavior](usage.md#why-permanent-deletion-keeps-the-telegram-files).

</details>

<details>
<summary>What is a clear explanation I can share with someone new?</summary>

> TGDrive is a free, open-source web drive for files stored in your own Telegram
> channel. It adds folders, previews, readers, large split-file uploads and share
> links. TGDrive has no quota on total stored bytes, but individual uploads and
> server resources have limits. You run it on a computer or server, and important
> files still need independent backups. It cannot guarantee permanent storage or
> immunity from Telegram restrictions.

Send this guide with the [live demo instructions](usage.md#try-the-public-demo).
The public demo is shared and should only receive disposable, non-private files.

</details>

Telegram policy references were reviewed on **26 October 2026**. Provider rules
can change; the linked official pages are the source for Telegram's current policy.
