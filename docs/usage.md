# Using TGDrive

[Docs home](README.md) · [Setup](setup.md) · [Help with problems](operations.md#troubleshooting)

TGDrive is a personal drive for the owner of one installation.
Other people can view or download only what you share with them.

## Find your way around

| Sidebar page | Use it to… |
| --- | --- |
| **My Files** | Browse, search and organize files |
| **Transfers** | See queued/running work and past completed, failed or cancelled transfers |
| **Shared Links** | Review or revoke links |
| **Trash** | Restore files or delete them permanently |
| **Connections** | Manage Telegram bots, Premium accounts and API credentials |
| **Storage & Backups** | Check saved data, temporary storage and database backups |
| **Settings** | Choose appearance or sign out |

Choose **Light**, **Dark** or **System** from the appearance menu.
System follows your device's preference.

## Upload files or folders

1. Open the destination folder in **My Files**.
2. Choose **New → Upload files** or **New → Upload folder**.
3. Select the files or folder. For a folder, confirm the short upload summary.
4. Follow the batch in **Transfers** and wait for **Completed**.

You can also drag files into the file area.
A single file has one row with its progress and actions. Multiple files appear
as a combined batch. Select its name or chevron to expand the files underneath.
Batch rows align the name, destination, total size and completion date. Folder
uploads keep their folder name and file count, including folders with one file.
Filters inside a batch appear only when its files have different statuses.
A folder header's chevron and name only expand/collapse its files. Use the
separate **Show Folder** action to open its directory. Rapid clicks on the
disclosure never navigate. Expanded children omit repeated group locations
and dates; nested or changed locations still appear when useful.

A folder appears in just one section: Running while any file is active, Queued
when only queued work remains, and Completed only when every file is complete.
Files needing attention and partly cancelled folders retain their own status.
Page filters keep the folder together; expand it to inspect individual file states.

### Upload speed and queue

Open **Settings → Uploads** to choose simultaneous uploads per bot: **1–4**.
The default is **1**; adding bots adds upload slots. A large file's parts use
all eligible client slots, rather than stopping at two simultaneous parts.
Bot-sized parts can also use connected Premium clients; larger Premium-only
parts require those accounts. Telegram cooldowns and channel pacing still apply.

Your device sends files to the server while Telegram uploads run independently.
Each ready file starts as soon as a slot is free. If temporary storage fills,
remaining device files wait and retry automatically. Keep the tab open until
those files have reached the server.

Changes apply immediately. Reducing the limit lets running uploads finish.
Telegram wait limits still apply; a higher setting does not guarantee more speed.

### Files above 2 GB

**Large-file splitting is automatic.** TGDrive stores the file in Telegram-sized
parts but shows one item. Downloads rebuild the original; previews and video
seeking use the same file.

- Premium is not required.
- The default limit is **16 GiB per file**, subject to free disk space.
- The limit is configurable; it is not unlimited storage.
- See [exact capacity limits](reference.md#file-capacity-staging-and-work-limits).

### What upload progress means

| Stage | What is happening |
| --- | --- |
| Sending from your device | Your browser is sending bytes to the TGDrive machine |
| Queued | Waiting for an available upload slot |
| Sending data to Telegram | Sending bytes, including parts that are not yet channel messages |
| Publishing to the channel | Finishing channel messages and saving the file record |
| Completed | The file and its record are confirmed |

For large files, **Sending data to Telegram** counts bytes across parallel
parts, including unfinished parts. Each part appears in the Telegram channel
only after its upload finishes. **Parts in Channel** counts confirmed channel
messages; TGDrive adds the complete file to My Files after all parts are confirmed.

A full **device** progress bar does not mean the Telegram upload is complete.
For a single file, the bar and byte count describe the current stage. Telegram
upload starts from zero after the device transfer. Multi-file batches show actual
Telegram bytes, including partial uploads; device bytes are not counted twice.
Sizes show up to two decimal places. Progress counters share the unit when possible,
and active transfers show bytes remaining above 95% so rounding cannot hide the tail.
Speed and remaining time appear when useful; queued batches have no elapsed timer.

### Can I close the tab?

- **Still sending from your device:** keep the tab open, or reselect the original
  files later to resume.
- **Received by the server:** Telegram uploading can continue with the tab closed,
  while TGDrive itself stays running.
- **Completed:** the local source is no longer needed by that upload.

To resume, choose **Choose files**, **Choose folder** or **Choose file** in the
existing batch. Select the same originals. TGDrive checks the received bytes
before continuing. This local file check keeps the received-byte progress in place;
it does not resend those bytes or show hashing speed as upload speed. Once
verified, sending resumes from the server's acknowledged offset. **Device**
speed measures browser-to-server sending; **Telegram** speed measures Telegram uploading.
When the original is needed, **Choose file** appears beside
the resume message as the main recovery action.

Use **Retry** on the existing failed transfer to avoid starting a duplicate.
Cancel can take time if Telegram has already received part of the work.
**Cancel all** asks for confirmation before stopping the selected unfinished uploads;
files already in your drive are kept. New uploads added after opening the
confirmation are not included.

**Hide/Show** collapses or expands the floating panel's details without stopping
uploads. **Clear finished** appears when completed/cancelled groups can be removed
from the list; it does not delete their files. Upload through the sidebar's
**New** menu.

<details>
<summary>Folder upload limits and interrupted preparation</summary>

- The chosen folder becomes a new root. An existing name is kept alongside it;
  folders are not silently merged or overwritten.
- Relative subfolders and same-named files in different folders are preserved.
- The picker supports up to 1,000 files and 1,000 discovered folders, subject to
  path and request-size limits.
- Empty folders are not supplied by the browser's picker. Empty or oversized
  files are listed as skipped.
- Keep the confirmation open while TGDrive prepares the folder tree. Retry in
  that dialog reuses the preparation. Closing before files are queued can leave
  an empty tree; choosing again can create another root.
- Once files are queued, use the existing batch to resume. If copied files no
  longer match in bulk, choose them individually.
- Files uploaded into a shared folder inherit that folder's access.

[Protocol and exact bounds](reference.md#atomic-folder-preparation)

</details>

## Transfer history

Transfers are separated into **Running**, **Queued**, **Needs attention**,
**Completed** and **Cancelled**. Use the filters to focus on one status.

- Finished rows show the size and date, without repeated progress or elapsed time.
- Rows use columns on wide screens and compact stacked details on smaller screens.
  Long filenames and paths truncate; hover to read their full text.
- The location shows its current TGDrive folder.
- **Double-click a completed transfer**, or choose **Show in folder**, to open that folder
  with the exact saved item highlighted. Preview or manage it there as usual.
- Completed folder upload groups also have **Show folder**.
- Running, queued, paused, failed and cancelled uploads have no location action
  or double-click navigation.
- Moves and renames are followed automatically. Trashed or deleted items show
  an unavailable state; TGDrive never selects another file with the same name.

Open **Transfers** and choose **All**, **Queued**, **Running**, **Completed**,
**Failed** or **Cancelled**.

- Current progress and past server transfers appear together.
- Finished records include dates and sizes. **Show older transfers** loads more.
- Clearing the floating panel does not erase this history.
- Completed/cancelled server records stay in MongoDB after live-job cleanup and restart.
- Previously expired records cannot be recreated.

Start uploads from a chosen folder in **My Files**. There is no upload button
on the history page. Clicking the sidebar's **New** from another page first
opens My Files so the destination is visible.

<details>
<summary>History storage and limits</summary>

History pages load up to 50 server records at a time. Compact finished summaries
are kept in MongoDB and included in encrypted database backups; they omit
payloads, source URLs and private recovery context. They do not expire with the
short-lived live-job journal. History consumes database space.

Device-only work that has not become a server job remains browser-scoped.
Queued/resumable device files still need their originals.

</details>

## Browse and organize

| Action | How |
| --- | --- |
| Select | Click once; selection mode shows checkboxes |
| Open a file or folder | Double-click, use Enter on its focused name, or choose Open |
| Open on a touch screen | Tap twice |
| Select several items | Use Select or Ctrl/Cmd-click |
| Clear selection | Done or Escape |
| Create a folder | New → New folder |
| Rename, move or trash | Open the item's menu, or use the selection actions |
| Sort | Choose Name, Date added or File size |

Folders stay first. A name conflict lets you keep both or skip; it does not
silently replace another file.

### Search

Type in the search bar to find file and folder **names across your drive**.
It does not search inside documents.

- Press Enter to search immediately.
- Results show each item's folder location.
- Clear the search or press Escape to return to your previous folder.
- Press `/` or Ctrl/Cmd+K to focus search when you are not editing another field.

## Open, preview, and download

Double-click a supported file to open a full-window preview.
Use the header's **Download** action for the original file.

| File | Preview |
| --- | --- |
| Images, including SVG | Image viewer |
| PDFs up to 20 MiB | Continuous scrolling, zoom and fit width |
| Video | Enhanced player with its own audio/subtitle controls |
| Audio | Browser audio player |
| Supported text and code | Read-only text; first 256 KiB |
| Unsupported formats | Compact file details and a download button |

- **Escape** or browser **Back** closes the preview.
- Video starts after you choose to play; loading and buffering show feedback.
- TGDrive reads ahead using available clients and reuses recently fetched data.\n  Replaying or seeking within cached content avoids downloading those bytes again.\n- If playback fails, try **Reload video** or download the original.
- Codec support depends on the file and your device.
- **Preview quality** appears only when Telegram actually provides alternatives.
  It never changes the original download.
- Web uploads preserve their source bytes. TGDrive does not promise that
  Telegram will create lower-quality versions.

## Share and revoke

1. Create a link from the item's share action.
2. Choose one day, seven days, 30 days or no expiry.
3. Copy the link. You can copy it again from **Shared Links**.

**Anyone with the link can read its contents.** A folder link includes its
accessible descendants.

In **Shared Links**:

| Action | What it does |
| --- | --- |
| **Active / Expired / All** | Filter links by whether they still grant access. |
| **Copy link** | Copy the same active link again. |
| **Show in folder** or double-click the name | Open its current directory and highlight the item. |
| **Revoke link** | Confirm before blocking access through this link. |
| **Renew link** | Choose an expiry and create a replacement for an expired link. |
| **Replace link** | Replace an older, unrecoverable link. The previous link stops working. |

New links can be copied again without replacing them. Older links that were
stored only as random-token hashes keep working, but cannot be reconstructed.

A small link icon in **My Files** identifies items shared directly or through
a parent folder. Explicit file and folder links keep working after rename or
move; the Shared Links page shows the current name and directory.

Moving or restoring an item into a shared folder can expose it; TGDrive asks
for confirmation. Moving it out removes inherited access, but an explicit
file link may still work. Trash revokes links for the affected subtree.
Revocation cannot remove copies someone already downloaded.

## Trash and permanent deletion

- **Move to Trash:** hides the item while keeping its Telegram files.
- **Restore:** brings it back, subject to name and access checks.
- **Delete permanently:** removes Telegram messages and the drive record after
  you confirm. It cannot be cancelled.

Bulk deletion has one combined operation notice, rather than upload rows for
every file. If some items fail, expand the failed-item details and retry them.
Wait for the completion notice; merely disappearing from a list is not confirmation.

## Import from a URL

Choose **New → Upload from URL** and enter a public HTTP(S) file URL.
TGDrive downloads it to the server, then sends it to Telegram.

Use the existing transfer's Retry action after an error. URL imports may restart
from the beginning. Private network URLs are blocked by default.

## Settings and connections

Use **Connections** for bots, Premium accounts and Telegram API credentials.

| Action | Result |
| --- | --- |
| **Refresh** | Checks every enabled client's saved credentials and channel permissions; updates names |
| **Manage** | Rename the local label, replace a credential, enable, disable or remove a client |
| **Add storage bot** | Connect another administrator bot in the same channel |
| **Connect Premium account** | Sign in with a phone number, Telegram code and optional two-step password |

Refresh runs in the background. A failed check keeps the saved name and shows
an error; temporary failures retry with Telegram's wait times. Disabled accounts
are skipped. Keep at least one working storage bot.

Finish or cancel unfinished transfers before changing connections.
Saved changes require a restart; the UI pauses new uploads until they apply.
For Docker, restart from the installation folder:

~~~bash
docker compose restart app
~~~

A blank replacement field keeps its current secret. Secrets are never shown
back in the page. The web password and storage channel are installation settings.

### Storage and backup

On **Storage & Backups**, **Saved to MongoDB** means the drive's record is saved
in the cloud. Database status is not repeated in the app header.
It does not mean an unfinished upload is complete.

- **Temporary storage** is disk reserved for unfinished uploads, not a Telegram quota.
- **Back up now** makes a complete encrypted database copy in your private channel.
  Automatic copies normally run hourly.
- **Save recovery key** downloads the key needed to restore those copies.
  Store it separately.

Deleting the local installation can be recovered by reconnecting the same
MongoDB database. Losing the database itself needs the separate
[backup restore procedure](operations.md#emergency-database-restore).

<details>
<summary>Optional Telegram bot interface</summary>

The web interface is the main interface. If bot mode is configured, only
`TELEGRAM_ADMIN_IDS` can use it. Use `/start` or `/help` for its commands.

Bot-mode imports do not use the web interface's durable upload queue.
See [known limits](roadmap.md#known-correctness-and-recovery-gaps).

</details>
