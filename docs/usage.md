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
| **Starred** | Quickly find files and folders you marked |
| **Trash** | Restore files or delete them permanently |
| **Monitor → Performance** | Inspect current server resources, Telegram activity and measured traffic |
| **Monitor → Preloads** | Inspect cached coverage, stop preparation, or evict cached bytes |
| **Management → Connections** | Manage Telegram bots, Premium accounts and API credentials |
| **Management → Storage & Backups** | Check saved data, temporary storage and database backups |
| **Configuration → Cache settings** | Set RAM/disk budgets, reserves, retention and automatic preparation |
| **Configuration → Transfer limits** | Set upload concurrency, splitting and shared Telegram request budgets |
| **Configuration → Timezone** | Choose the installation's timezone |

The sidebar keeps **Monitor**, **Configuration**, and **Management** visible as
separate groups. Monitor shows activity and reusable media; Configuration changes
instance-wide policies; Management handles connections and recovery.

TGDrive offers Light, Dark and System appearances with shared controls. The whole sidebar
scrolls naturally; Documentation and **Log Out** follow the navigation rather than
reserving fixed space. Logging out closes this browser's access; server transfers continue.
See [Configuration & tuning](configuration.md) for safe defaults, restoring values
and larger-server tuning.

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

A folder appears in just one section: **Running** while any file is active,
**Queued** while queued files remain, then **Finished** once no work is running
or queued. Finished includes successful, failed and cancelled uploads. A folder
with a failed child remains queued while another child is waiting. Filters keep
known batches together; expand a folder to inspect its individual file states.

### Upload speed and queue

Open **Configuration → Transfer limits → Parallel uploads** to choose simultaneous uploads per bot: **1–4**.
The default is **1**; adding bots adds upload slots. A large file's parts use
all eligible client slots, rather than stopping at two simultaneous parts.
Available Premium clients are preferred for upload parts and download payload
windows, with bot fallback when busy or unavailable. Premium's benefit is
potential speed per task; every account uses the same split cap. Telegram
cooldowns and channel pacing still apply.

Your device sends files to the server while Telegram uploads run independently.
Each ready file starts as soon as a slot is free. If temporary storage fills,
remaining device files wait and retry automatically. Keep the tab open until
those files have reached the server.

Changes apply immediately. Reducing the limit lets running uploads finish.
Telegram wait limits still apply; a higher setting does not guarantee more speed.

Upload chunk pipelining and file splitting are separate controls. A smaller
split can use more available account slots for one file, while pipelining
overlaps part requests within a Telegram document. Keep conservative limits
and compare actual throughput before increasing concurrency.
See [performance and caching](performance.md) for the tradeoffs.

### Files above 2 GB

**Large-file splitting is automatic.** TGDrive stores the file in Telegram-sized
parts but shows one item. Downloads rebuild the original; previews and video
seeking use the same file.

**Premium's larger single-document allowance is unnecessary for TGDrive.**
TGDrive splits large files automatically and tracks the ordered parts as one
logical file. Full downloads reconstruct the original bytes; streaming and
byte-range seeking work across part boundaries. Database backups preserve the
part mappings, and an enabled independent backup channel copies the underlying
documents for recovery. These operations do not require 4000 MiB Premium
documents. Premium's intended benefit here is potential upload/download speed
per task. See [backup and recovery](backup-recovery.md) for backup coverage and
requirements.

- Premium is not required.
- Each Telegram document defaults to at most **2000 MiB**, including Premium.
  Reduce **File split size** in Configuration to split sooner; 0 also uses 2000 MiB.
- The default limit is **16 GiB per file**, subject to free disk space.
- The limit is configurable; it is not unlimited storage.
- See [exact capacity limits](reference.md#file-capacity-staging-and-work-limits).

### What upload progress means

| Stage | What is happening |
| --- | --- |
| Uploading from your device | Your browser is sending bytes to the TGDrive machine |
| Queued | Waiting for an available upload slot |
| Uploading to Telegram | Sending bytes, including parts that are not yet channel messages |
| Publishing to the channel | Finishing channel messages and saving the file record |
| Completed | The file and its record are confirmed |

For large files, **Uploading to Telegram** counts bytes across parallel
parts, including unfinished parts. Each part appears in the Telegram channel
only after its upload finishes. **Parts in Channel** counts confirmed channel
messages; TGDrive adds the complete file to My Files after all parts are confirmed.

A full **device** progress bar does not mean the Telegram upload is complete.
Device progress updates as bytes are sent, including within a large chunk.
“Waiting for server confirmation” means that the browser finished sending the
chunk but has not received its acknowledgement. Only confirmed offsets are saved
for recovery; a failed chunk can move the displayed progress back to that offset.
For a single file, the bar and byte count describe the current stage. Telegram
upload starts from zero after the device transfer. Multi-file batches show actual
Telegram bytes, including partial uploads; device bytes are not counted twice.
Sizes show up to two decimal places. Progress counters share the unit when possible,
and active transfers show bytes remaining above 95% so rounding cannot hide the tail.
Active rows show speed and **About … remaining** when an estimate is available.
Both describe the current stage, not the complete device-to-Telegram journey.
Queued batches have no elapsed timer.

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
verified, sending resumes from the server's acknowledged offset. Speed measures
browser-to-server sending or Telegram uploading according to the current stage.
When the original is needed, **Choose file** appears beside
the resume message as the main recovery action.

Use **Retry** on the existing failed transfer to avoid starting a duplicate.
Cancel can take time if Telegram has already received part of the work.
**Cancel all** asks for confirmation before stopping the selected unfinished uploads;
files already in your drive are kept. New uploads added after opening the
confirmation are not included.

On desktop, **Hide/Show** collapses or expands the floating panel's details without stopping
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

Transfers show **Running** first, **Queued** next, and **Finished** below them.
Finished includes successful, failed and cancelled work. Its **All**,
**Successful**, **Failed** and **Cancelled** filters apply only to finished
history; current running and queued work stays visible above them.

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

Open **Transfers** to follow current work and browse finished uploads.

- Current progress and past server transfers appear together.
- On phones, the bottom upload status row opens **Transfers**. It occupies its
  own space and never covers file rows. **New** sits in the top bar; file browsing
  uses one scrolling workspace. The navigation drawer opens from the top edge.
- The desktop floating upload panel shows work started from this browser. Uploads
  discovered from another device remain visible in **Transfers**, without
  opening a new floating completion panel on this device.
- Finished records include dates and sizes. **Previous** and **Next** replace
  the current history page; the page number shows your position. **Latest**
  returns to the newest page. Changing the finished-status filter starts at page 1.
- **Search Transfers** finds names across saved history, including older pages,
  and filters current browser uploads too. Changing the search starts at page 1.
- Older pages show their own records, while live running and queued work remains
  at the top. Recent local completions are not added to every older page.
- Clearing the floating panel does not erase this history.
- Completed/cancelled server records stay in MongoDB after live-job cleanup and restart.
- Previously expired records cannot be recreated.

Start uploads from a chosen folder in **My Files**. There is no upload button
on the history page. Clicking the sidebar's **New** from another page first
opens My Files so the destination is visible.

<details>
<summary>History storage and limits</summary>

History pages load up to 50 server records at a time, rather than accumulating
the entire archive in the browser. TGDrive remembers up to 128 previous page
cursors for back navigation; **Latest** is always available on older pages.
Known browser batches remain grouped. Older server records may appear as
individual files when the original browser's batch information is unavailable.
Compact finished summaries
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
| Sort | Choose Name, Date Added or File Size, then Ascending or Descending |
| Change view | Choose List view or Grid view |

Sorting works in folders and search results. Ascending means A–Z, oldest first
or smallest first; Descending reverses that order. Folders stay first in either
direction. Clicking the active list column heading reverses its order.
The list/grid choice is remembered in this browser.

On phones, **View and sort** combines the view, sort field and direction in one
menu beside the title. The selection icon keeps Select/Done available.

For new supported uploads TGDrive generates a small cover from the staged file:
a scaled image, an early video frame or the first PDF page. It keeps the original
unchanged. Grid tiles request only the small thumbnail when visible or nearby;
they never fetch the whole original to manufacture a cover. See
[Thumbnail generation](thumbnails.md) for supported formats and existing files.
A browser reporting data-saving mode uses icons without thumbnail requests.

A name conflict lets you keep both or skip; it does not silently replace another
file.

### Starred files and folders

Choose **Add to Starred** from a file or folder's actions menu. **Starred** in the
sidebar collects those originals in one place, with their current locations.
Open, search, sort or switch list/grid views as usual. **Remove from Starred**
removes only the bookmark; the file stays in its folder. Stars follow renames and
moves, stay hidden while an item or its parent is in Trash, and return on restore.
They are owner metadata and are included in backups, not shared-folder access.

### Search

Search sits beside the page heading and stays within that page. On phones,
choose the search icon to open the field; it stays compact while unused:

| Page | What search finds |
| --- | --- |
| **My Files** | File and folder names across your drive, with their locations |
| **Transfers** | Current upload names and saved transfer names across all history pages |
| **Trash** | Names of the trashed items available to restore or delete |
| **Starred** | Names in your starred files and folders |
| **Shared Links** | Names in your shared-link inventory |
| **Preloads** | Cached filenames and preparation jobs |

It does not search inside documents. Switching pages clears the search so a
query cannot silently hide items in a different page.

- Press Enter to search immediately.
- In My Files, clear the search or press Escape to return to your previous folder.
  On the other pages, this clears the filter without leaving the page.
- Press `/` or Ctrl/Cmd+K to open and focus search when you are not editing another field.
- On phones, Close Search or Escape collapses an empty search field.

## Open, preview, and download

Double-click a supported file to open a full-window preview.
Use the header's **Download** action for the original file. **Enter Fullscreen**
in the preview header fills the display for supported previews; **Exit Fullscreen**
returns to the browser. Some mobile browsers restrict native fullscreen.
For PDFs, **Fit width** fills the available reading width; **Fit page** shows the
whole current page. Manual zoom leaves the selected fit mode. The temporary
“Rendering page…” message means the browser is fetching missing PDF data and
drawing that page's text/images.

| File | Preview |
| --- | --- |
| Images, including SVG | Image viewer |
| PDFs up to 256 MiB | Continuous scrolling, zoom, Fit width and Fit page |
| Video | Enhanced player with its own audio/subtitle controls |
| Audio | Browser audio player |
| Supported text and code | Read-only text; first 256 KiB |
| EPUB | Chapter reader, text size and saved reading position |
| CBZ and CBR | Comic pages, page fit and right-to-left navigation |
| Other unsupported formats | Original download |

- PDFs load the sections needed for visible pages instead of downloading the
  whole document first. The preview keeps canvas/text work bounded and has a
  64 MiB reading budget per opening; large or complex books may reach it.
  **Download Original** remains available. The 256 MiB file ceiling protects
  browser memory; it does not mean that all those bytes are downloaded.
- EPUB and comic readers request archive sections and the selected chapter/page.
  They do not start a whole-file download or automatic preload. Complex or solid
  archives can require more bytes; reader limits keep that work bounded. See
  [Reading books and comics](reading.md) for controls and supported formats.
- **Escape** or browser **Back** closes the preview.
- Every video uses the custom player. Its normal play control remains available
  while opening; your browser may require a tap before playback with sound.
- The timeline highlights estimated loaded source ranges. Loading status shows
  measured received bytes and speed; it does not turn downloaded bytes into a
  playback-readiness percentage. Decoding audio and video can still take time.
  This source window is separate from the server's preload coverage.
- TGDrive reads ahead using available clients and reuses recently fetched data.
  Replaying or seeking within cached content avoids fetching those bytes again.
- Real chapters list their titles and times immediately, without waiting for
  preview thumbnails. Open **Chapters** from the player controls; it follows
  their visibility and also works in fullscreen.
- Focus the player for **K/Space** to play/pause, **M** to mute, and arrow-key
  seeking. Audio/subtitle menus show the tracks available in that file.
- For a slow start, use **Reload Video** within the custom player.
- Download the original if your device cannot decode the file.
- Codec support depends on the file and your device.
- **Preview quality** appears only when Telegram actually provides alternatives.
  It never changes the original download.
- Web uploads preserve their source bytes. TGDrive does not promise that
  Telegram will create lower-quality versions.

### Cache and prepare playback

The server keeps verified media blocks within your configured RAM/disk budgets.
Repeated viewing and popular shared files reuse those bytes. Background work
gives priority to people actively watching or downloading.

Choose **Prepare for Download / Streaming** from the file's actions before a
screening or a large share. Open **Preloads** to check cached bytes and the
coverage bar for each file: blue segments show partial coverage, and a full
green bar with **Ready** means the whole file is cached. Clear one file or all
cached data after confirmation; the originals remain in Telegram. Check progress
and completion, then share it.
Preparation must fit an effective cache tier; increase the budget or free space
if it is rejected. You can cancel preparation or evict cached bytes without
deleting the original.

Preloads lists the most recently downloaded server data first. New data for an
existing file moves it to the top. Refreshing the page or reading already-cached
bytes does not reorder the list. Older cache indexes without a recorded download
time follow entries with known activity.

Cached bytes can expire or be evicted. A file being prepared once does not
guarantee that it stays fully cached forever. Cache access still requires a
valid owner session or share link. [See how caching works](performance.md).

## Share and revoke

1. Create a link from the item's share action.
2. Optionally label it, for example **Alex** or **Project review**.
3. Choose one day, seven days, 30 days, no expiry, or a custom date and time.
4. Copy the link. You can copy it again from **Shared Links**.

You can create up to 100 independent links for the same file or folder. Each
has its own label and expiry; creating one leaves existing links working.
**Anyone with the link can read its contents.** Labels help you organize links;
they do not restrict a link to a named person. A folder link includes its
accessible descendants.

In **Shared Links**:

| Action | What it does |
| --- | --- |
| **Active / Expired / All** | Filter links by whether they still grant access. |
| **Copy link** | Copy this active link again. |
| **Edit link** | Change this link's label or expiry, including a custom date or no expiry. |
| Click the name or **Show in folder** | Open its current directory and highlight the item. |
| **Revoke link** | Confirm before blocking this link. Other links keep working. |
| **New link** | Create a separate link when an old link is expired or cannot be copied. |

Rows show the item, link label, current directory and a short expiry date. Hover,
focus or tap an expiry date for the exact time and instance timezone.

New links can be copied again without replacing them. Older links stored only
as random-token hashes keep working, but cannot be reconstructed. **New link**
does not revoke them; revoke an old link separately when you want to stop access.

A small link icon in **My Files** identifies items shared directly or through
a parent folder. Explicit file and folder links keep working after rename or
move; the Shared Links page shows the current name and directory.

Moving or restoring an item into a shared folder can expose it; TGDrive asks
for confirmation. Moving it out removes inherited access, but an explicit
file link may still work. Trash revokes links for the affected subtree.
Revocation cannot remove copies someone already downloaded.

An owner-only download address can depend on your signed-in browser cookie.
Copying that address into an incognito window or a download tool does not
grant access. Use **Copy download link** to create or reuse an explicit shared
capability labelled **Download link**; this stays separate from recipient links.
A newly created download link lasts one day and appears in
**Shared Links**, where you can revoke it. Treat the copied URL as a secret.

A supplied invalid, expired, or revoked token is rejected even if the same
browser is signed in as the owner. Revocation also stops future authorized
blocks of an active stream; bytes already delivered remain with the recipient.

## Trash and permanent deletion

Each item shows **Deleted from: My Files / …** so you can identify its source
folder before restoring or deleting it.

- **Move to Trash:** hides the item while keeping its Telegram files.
- **Restore:** brings it back, subject to name and access checks.
- **Delete permanently:** removes the selected file/folder records, descendant
  records and their shares from TGDrive's database after you confirm. Original
  Telegram messages remain in the storage channel. It cannot be cancelled or
  restored from Trash after completion.

### Why permanent deletion keeps the Telegram files

TGDrive's permanent-delete operation uses database-only removal for every file
age. It does not attempt to erase the original Telegram messages or require a
Telegram user session. Completion confirms that the selected database records
and their TGDrive access were removed; it does not confirm remote erasure.

Permanent deletion removes TGDrive's references and access through TGDrive; it
does not erase Telegram's copies, copies held by recipients, local caches or
older database/channel backups. Channel members with access may still read the
original messages. Older backups may restore the removed records. If you need
to remove the originals, manage them separately in Telegram with an authorized
user account; that is outside TGDrive's permanent-delete operation.

Previously failed Telegram-deletion jobs can be retried after upgrading. They
remove the remaining database records without attempting further remote deletion;
messages already deleted by an older version cannot be brought back by this change.

Bulk deletion has one combined operation notice, rather than upload rows for
every file. If some items fail, expand the failed-item details and retry them.
Wait for the completion notice; merely disappearing from a list is not confirmation.
The notice appears as soon as deletion is accepted, shows how many selected items
are still processing and how many have finished, and keeps the result visible
until dismissed. Items stay in Trash while their jobs run; the list refreshes
automatically after completion. Backup checks or upload cleanup may delay removal.
If progress checks lose connection, TGDrive displays a warning and retries. A
temporary failure refreshing the file list is also retried automatically.
When independent channel backup is enabled, its existing protection check still
applies before the catalog is removed; see [backup and recovery](backup-recovery.md).

## Import from a URL

Choose **New → Upload from URL** and enter a public HTTP(S) file URL.
TGDrive downloads it to the server, then sends it to Telegram.

Use the existing transfer's Retry action after an error. URL imports may restart
from the beginning. Private network URLs are blocked by default.

After submission, a URL import is a server job and continues when the tab is
closed. Refreshing the browser retains that job's identity and URL-import kind;
it must not ask you to choose a local original. If the server job no longer
exists, start a new URL import rather than selecting an unrelated device file.

## Settings and connections

Use **Management → Connections** for bots, Premium accounts and Telegram API credentials.
Use **Configuration → Cache settings** for instance-wide storage and preparation
budgets, **Transfer limits** for transfer policy, and **Timezone** for
display timezone. Each category shows only its related controls; advanced Telegram
limits stay collapsed until needed. Saving applies only the current category;
unsaved changes in other categories remain drafts. Upload concurrency has its
own save action.

Existing links to `?view=settings&section=performance` continue to open cache
settings. Switching between these settings categories preserves unsaved drafts.
The former standalone `?view=settings` page opens **My Files**; use the categorized
Admin links to change server settings.

| Action | Result |
| --- | --- |
| **Refresh** | Checks every enabled client's saved credentials and channel permissions; updates names |
| **Manage** | Rename the local label, replace a credential, enable, disable or remove a client |
| **Add storage bot** | Connect another administrator bot in the same channel |
| **Connect Premium account** | Sign in with a phone number, Telegram code and optional two-step password |

Refresh runs in the background. A failed check keeps the saved name and shows
an error; temporary failures retry with Telegram's wait times. Disabled accounts
are skipped. Keep at least one working storage bot.

If Telegram revokes a token/session or removes channel access, TGDrive excludes
that connection and shows a warning with the repair action. Downloads can use
another healthy account; upload parts switch only before message dispatch.
Already dispatched parts retain their sender and staged bytes for safe retry.
Restore channel permissions and Refresh, or replace a revoked credential for the
same account and restart. See [connection recovery](operations.md#revoked-tokens-removed-sessions-and-lost-channel-access).

Finish or cancel unfinished transfers before changing the account list, enabled
states or Telegram API credentials. A replacement bot token or Premium session
for the **same Telegram account** can repair a broken credential while transfers
remain pending; keep all other connection settings unchanged. TGDrive checks the
replacement identity and preserves pending work. A different account needs the
normal idle workflow.
Saved changes require a restart; the UI pauses new uploads until they apply.
For Docker, restart from the installation folder:

~~~bash
docker compose restart app
~~~

A blank replacement field keeps its current secret. Secrets are never shown
back in the page. The web password and storage channel are installation settings.

### Storage and backup

On **Management → Storage & Backups**, **Saved to MongoDB** means the drive's record is saved
in the cloud. Database status is not repeated in the app header.
It does not mean an unfinished upload is complete.

- **Temporary storage** is disk reserved for unfinished uploads, not a Telegram quota.
- **Back up now** makes a complete encrypted database copy in your private channel.
  Automatic copies normally run hourly.
- **Save recovery key** downloads the key needed to restore those copies.
  Store it separately.

Deleting the local installation can be recovered by reconnecting the same
MongoDB database. Losing the database itself needs the separate
[Backup & recovery guide](backup-recovery.md).

<details>
<summary>Optional Telegram bot interface</summary>

Set `MAIN_BOT_TOKEN` and `TELEGRAM_ADMIN_IDS` to enable the optional bot interface.
Only those administrators can use it, in a private chat with the bot.

1. Open **Configuration → Transfer limits → Bot uploads** on the website.
2. Click **Change folder**, browse to a destination, then click **Use this folder**.
   To restore the default, click **Use default folder**.
3. Send or forward files, photos, videos or audio to the bot. It shows **Saving…**,
   then confirms the filename and destination. Send photos as files to preserve
   their original quality.

The default is **My Files / TGDriveBotModeFiles**. The folder is created on the
first bot upload and reused afterwards. One website setting controls the
upload destination for every bot administrator. Changes apply to the next file
without a restart; files already being imported keep their original destination.

Use `/set_folder` in Telegram for a link to this website setting. The website
address is remembered when you save the bot upload folder. Optionally set
`PUBLIC_URL=https://your-drive.example` to provide the link before that first
save, or to specify the public address when accessing the site locally.
The link opens the normal owner login and contains no authentication tokens.

`/start` and `/help` explain how to send files. `/current_folder` (also the
**Current folder** button) shows the current destination. Folder selection is
handled on the website. If the selected folder is moved or deleted, imports
pause until you choose an available folder; they do not fall back to the root.

Bot-mode imports do not use the web interface's durable upload queue.
See [known limits](roadmap.md#known-correctness-and-recovery-gaps).

</details>
