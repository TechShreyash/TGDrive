# Using TGDrive

[Docs home](README.md) · [Setup](setup.md) · [First-use essentials](new-user-guide.md) · [Troubleshooting](operations.md#troubleshooting)

Start in **My Files**: upload a small file, open it, then try sharing it.
[Watch the walkthroughs](showcase.md) if you prefer a visual introduction.
If login fails, focus returns to the password field so you can correct it.

| Task | Jump to |
| --- | --- |
| Add files | [Upload](#upload-files-or-folders) · [Resume an upload](#can-i-close-the-tab) |
| Find or use files | [Browse](#browse-and-organize) · [Preview and download](#open-preview-and-download) |
| Give someone access | [Share links](#share-and-revoke) · [Embed](#embed-a-file-or-folder-in-a-website) |
| Other ways to import | [URL](#import-from-a-url) · [Torrent](#import-a-torrent-or-magnet-link) · [Telegram bot](#upload-through-your-telegram-bot) |
| Manage work | [Transfers](#transfer-history) · [Preloads](#preload-a-file-for-smoother-playback-and-downloads) · [Settings](#settings-and-connections) |

## Upload files or folders

1. Open the destination folder in **My Files**.
2. Choose **New → Upload files** or **New → Upload folder**, or drag files into the file area.
3. For a folder, review and confirm the upload summary.
4. Follow **Transfers**. Keep the tab open until **You can close this tab**;
   keep your originals until **Completed**.

Each selected file is a separate task. A folder upload forms an expandable group;
open it to see individual files, or choose **Show Folder** to find its directory.
A folder marked **Finished** can include failed or cancelled files: check its results.

### What upload progress means

| Stage | Meaning |
| --- | --- |
| Uploading from your device | The browser is sending bytes to TGDrive |
| Queued | Waiting for an upload slot |
| Uploading to Telegram | Sending bytes; channel messages may not exist yet |
| Publishing to the channel | Finishing messages and saving the file record |
| Completed | The file and its record are confirmed |

An expanded folder tracks device progress until TGDrive accepts all of its
files. Accepted files show **Received** as their Telegram work continues.
The folder summary then switches to Telegram progress. The compact desktop
panel can show both stages while they overlap.

The compact panel shows the filename or folder, its current stage, and a ring
with the measured percentage when the size is known. Within the same stage
the percentage uses the combined bytes and combined sizes. An unknown size
can still show bytes transferred. A stage that reports only its percentage
shows that reading for its own transfer. When the bytes reach 100%, TGDrive
may still be confirming or publishing the file. The new stage has its own
readout. Select the panel to expand individual transfers.

Time remaining applies to the displayed stage and disappears when progress
stalls or the server is confirming completion. On phones, tap the filename
and progress ring to open **Transfers**. If a transfer needs attention, its
filename and recovery status appear there first. The **Transfers** tab includes
server jobs from other devices.
In short landscape and other views with little vertical space, routine uploads
use the tab count. Uploads from this browser that need attention still get a
status button.

[Upload screenshots and walkthrough](showcase.md) ·
[Background progress updates](performance.md#browser-progress-and-health-checks)

### Can I close the tab?

| State | What to do |
| --- | --- |
| Still sending from your device | Keep the tab open, or reselect the originals later to resume |
| **You can close this tab** | Close it if you like; TGDrive must keep running to finish |
| **Completed** | This upload no longer needs its source file |

To resume, choose **Choose files / Choose folder / Choose file** in the existing
batch and select the same originals. TGDrive checks confirmed bytes and sends
what is missing. Reselecting a folder skips files already received, active,
completed or cancelled; different names/sizes and unrelated files are unmatched.

Temporary connection/server errors retry automatically after an upload session
is confirmed. Keep the tab and originals available. If retries stop, use **Retry**
or **Retry Failed** in the existing task to avoid duplicates. An interrupted
session-creation request needs manual retry.

Only one tab in the same browser can send a saved upload at once. Device uploads
need HTTPS or localhost and browser support for Web Locks. If recovery information
cannot be saved, keep the original tab open: another tab may lack its latest state.

**Cancel** waits for requests and cleanup before confirming. **Cancel all** applies
to the unfinished work you confirm; completed files and later uploads remain.
**Clear finished** removes panel entries, not stored files or saved history.

<details>
<summary>Folder selection and interrupted preparation</summary>

- A folder creates a new root and preserves subfolders. Existing names are kept
  alongside it rather than merged or overwritten.
- There is no fixed batch-count quota, but browser memory, request metadata and
  staging limits apply. Empty and oversized files are skipped; browser pickers
  omit empty folders.
- Keep the confirmation open during preparation. **Retry** there reuses the
  tree; closing early and choosing again can leave another empty root.
- Once queued, resume the existing batch. Choose files individually if bulk matching fails.
- Files uploaded into a shared folder inherit its access.

[Folder protocol and bounds](api.md#atomic-folder-preparation) ·
[Restart recovery](operations.md#after-a-shutdown-crash-or-network-loss)

</details>

### Files above 2 GB

TGDrive splits large files automatically and presents them as one file for
playback/download. Premium is optional. New work must fit the file ceiling,
existing staging reservations and actual free disk after the reserve. Every
account uses documents of at most **2000 MiB**. Keep the
[catalog](reference.md#terms-used-here) and every part for recovery.
[Transfer limits](configuration.md#transfer-limits).

### Upload speed and queue

Device intake and Telegram uploading run independently. Device concurrency
adapts automatically; start with the defaults. Extra bots or upload slots can
help some workloads but do not bypass Telegram waits or guarantee more speed.
Transfers shows the current stage and a retry countdown when the server has a
deadline. In **Performance → Tuning**, choose **Upload and download speed limits**
to configure the two directions independently.
[Transfer settings](configuration.md#transfer-limits) · [Find a bottleneck](performance-tuning.md#find-the-bottleneck)

## Browse and organize

| Action | How |
| --- | --- |
| Open | Double-click with a mouse, press Enter on a focused name, or tap on touch |
| Return to the parent listing | Use browser Back after opening a folder to return to its row, including on later pages. |
| Select an item | Click with a mouse or touch and hold. On a public link, a mouse click focuses the name. |
| Select several items | Click an item, then Shift-click another to select the range on the current page. Ctrl-click (Command-click on Mac) adds or removes an item. You can also choose **Select** or touch and hold an item, then use the rows or checkboxes. **Done** or Escape clears selection. |
| Create a folder | **New → New folder** |
| Rename, move or trash | Item menu or selection actions |
| Sort/change view | Name, Date Added or File Size; List/Grid; **View and sort** on phones |
| Filter by type | Chips above the list or **All types** when the file area is narrow. The control shows the active filter. |

Files and folders start sorted by **Date Added**, newest first. Folders stay
first. Click the active column to reverse the order. Large
folders load 200 items first and more as you page. Sort and type filters apply
to the whole folder and return to page 1. Until a folder is fully loaded, every
type chip stays visible; a filter with no matches shows **Choose All to see
everything in this folder**. Open **Recent** to find files you recently added
or opened. A new browser opens in List view and remembers your choice when you
switch views. Grid shows the file name, preview, and actions. It fits
up to five cards per row and uses fewer columns as the available space narrows.
Use List to compare file sizes and added dates. Name conflicts offer
**keep both** or **skip**.

If a search has no results, **Back to folder** returns to the folder where
you started. In Trash, Starred, Shared Links and Transfers, **Reset search**
clears the search within that view. If TGDrive stalls while opening after an
update, **Reload page** retries the current URL, including a shared link.

On phones and smaller tablets, use **Toggle Navigation** to open the sidebar.
On a phone you can also open an item's action menu to read its full name.

If a page fails to load, choose **Try Again**. On later pages, **Previous Page**
returns to loaded items.

Use the arrow keys in **View and sort** and **Appearance**, or type the start of
an option's name. Escape closes the menu and returns focus to its button.
Ctrl-click or Command-click a sidebar link or breadcrumb to open it in a new tab.

Supported uploads generate covers automatically. For missing covers or a full
rebuild, see [thumbnails](thumbnails.md).

### Recent

**Recent** sits between **My Files** and **Starred** (in the **More** drawer on
phones). It lists files you uploaded or opened, newest first, 100 at a time.
**More available** shows while older files remain. Rows show their folder and
the last open or upload time under **Activity**, and the item menu's **Show in
Folder** opens that folder with the file selected. Type chips filter the list;
sort, search and upload are hidden here.

Opening counts previews, playback and downloads by the owner, at most once per
file every 10 minutes. Share-link and embed visitors are never recorded. Files in
Trash are left out. Recording an open is best effort and does not block file
access; busy or failed tracking can leave an open out of Recent.

Right-click a file or folder, or press **Shift+F10** while its name has focus,
to open its actions. The three-dot button opens the same actions. Public
shared folders offer **Open** and, for files, **Download**.

### Move files and folders

**Move** shows the source separately from the destination. If your selection
comes from several folders, the destination picker starts at **My Files**.
Check the full destination path before choosing **Move Here**. If that folder
becomes unavailable, **Choose another folder** keeps the same items selected.

### Starred files and folders

Choose **Add to Starred / Remove from Starred** in the item menu. Stars are
bookmarks, not shared access. They follow moves/renames, hide in Trash and return
on restore; removing a star keeps the file.

### Search

Search finds names, not document contents. It matches the **start of words**,
ignoring case and accents: `frie` finds *Frieren*. Names split at spaces,
punctuation, camelCase and letter/digit changes, so `s04e03`, `1080` and `x265`
work, and numbers match without leading zeros (`3` finds `03`). Within the bounded
candidate set, exact names rank first, then names starting with your words, then
whole words in order; newest first among equals. TGDrive ranks at most 1,000
candidates, so it does not promise the globally best matches in a larger set.
When results are capped, add a word to narrow the search. **My Files** searches the whole drive;
other pages search their own items. **Transfers** includes all history pages.
Press Enter to search, Escape to clear, or **/** / **Ctrl/Cmd+K** to focus search
outside a text field. Switching pages clears the query. On phones, use the search icon.

## Open, preview, and download

Open a file to preview; **Download** gets the original. Escape or browser Back
closes the preview. When a format has no browser preview, the file details put
**Download File** above the metadata. In a very short window, the download
action comes before the file details. If a preview fails, **Try Preview Again**
or **Reload Viewer** appears above the error details. Use fullscreen where supported. If the
device cannot decode a video, choose **Play compatible version** when offered,
or download the original for a suitable app. The compatible version starts
playing when ready. If your browser requests another gesture, use the player's
Play control.

Compatible playback converts requested portions on the server to H.264/AAC at up
to 720p, including supported ProRes MOV and WMV sources. It uses the first audio
track; original subtitle/track and quality choices are unavailable in this mode.
The original file stays unchanged and saved progress still belongs to that file.
Conversion uses server CPU and bandwidth. Busy servers, insufficient memory or
files outside the conversion limits can still require an original download.
[Conversion limits](performance.md#compatible-video-playback).

| File | Preview |
| --- | --- |
| Images, including SVG | Image viewer |
| PDF up to 256 MiB | Page box, zoom menu with Fit width and Fit page |
| Video/audio | Player with available track and subtitle controls |
| Supported text/code | Read-only text, first 256 KiB |
| ZIP | Browse files and nested folders without extracting; file sizes and password-protected entries are shown |
| Word, Excel, PowerPoint, OpenDocument, ebooks, comics, and other unsupported formats | Original download |

Markdown previews render headings, lists, emphasis and code. Use Show source
to read the original markup. The preview displays external image captions and
link text without loading external resources.

To add, change, or remove a PDF password, choose **PDF password** from the
file's actions. Enter the current password when changing or removing it.
TGDrive saves a protected or unlocked copy in the same folder and keeps
the original. If the current password is incorrect, open **Transfers** and
choose **Retry** to enter the passwords again.
Follow the operation in **Transfers**. PDFs up to 256 MiB are supported.

ZIP previews read directory metadata rather than file contents. They support
single-volume ZIP/ZIP64 archives with up to 10,000 entries and a 2 MiB directory.
Encrypted file contents can still be listed when their directory is readable;
the preview does not unlock or extract them. Download the original if its
directory is damaged, encrypted or exceeds these preview limits.

Videos open paused; opening a file never starts playback. Once a saved position
is known, **Play** resumes there. Use the timeline to return to the beginning. A
position within the first or final two seconds starts at zero instead.

If you press Play before saved progress arrives, playback continues without a
late jump. A valid later bookmark offers **Continue from…** or **Keep playing
from here**; seeking manually also chooses your new position. A failed progress
read offers retry without replacing the old bookmark.

Signed-in owners and demo administrators save video and PDF positions in MongoDB
for continuation on another device, including independently authenticated shared
or embedded viewers. Share tokens alone grant no access to owner progress.
Anonymous shares and public-demo sessions keep separate browser-local progress;
existing local positions are not imported into owner history.

A thin bar along a video thumbnail's bottom shows its saved position relative to
its duration, **not which sections were watched**. Old local time-only bookmarks
can resume without showing a guessed fraction. Shared thumbnails use local
history even if an owner is also signed in.

Progress is advisory: failed saves do not stop playback or create a local owner
fallback. Closing or losing a connection can lose recent progress; clearing site
data can remove local progress. Positions can appear in later recovery snapshots,
but saving progress alone does not trigger a backup or advance its revision.
Older direct `/file` stream links use browser-native handling; shared viewer
links use TGDrive's player. [PDF resume and reader limits](reading.md).

Videos use a saved poster when available. Without one, videos up to 4 MiB
can show a frame from the midpoint. Larger videos show the opening frame so
playback can use the same bytes.
[Poster storage, fallback and limits](thumbnails.md#video-posters).

Loading/buffering can reflect Telegram waits, network speed or device decoding.
Image/audio previews retry a full Telegram queue at most twice while open; then
use **Try Preview Again**. Other failures need manual retry. Downloads use the
browser's controls and need it connected; they have no saved server queue position
after restart. [Playback and delivery policy](performance.md#video-controls-and-startup).

Some MKVs with ASS/SSA subtitles and embedded fonts need additional reads and
initialization before playback. File layout, seek cues and selected tracks
matter; there is no fixed startup-byte requirement ([Matroska ordering](https://www.matroska.org/technical/ordering.html)
and [font attachments](https://www.matroska.org/technical/attachments.html)).
[Preload file](#preload-a-file-for-smoother-playback-and-downloads) can help when
Telegram retrieval is the bottleneck. Server delivery and browser decoding still
take time; a fully cached file does not guarantee instant playback.

PDFs request sections as needed rather than automatically
preloading the whole file. [Reader controls and limits](reading.md).

<details>
<summary>Video and keyboard controls</summary>

| Control | Action |
| --- | --- |
| **K/Space**, **M**, arrows while focused | Play/pause, mute, seek |
| Double-tap picture left/right | Seek 10 seconds; further taps add 10 seconds |
| Single touch | Show/hide controls or dismiss an open menu |
| Settings arrows, Home/End, Enter/Space | Navigate and select |
| Escape | Back out of settings, then close the menu before the preview |
| **Chapters** | Jump to embedded chapters, including in fullscreen |
| **Settings → Quality** | Choose available Telegram alternatives without losing position |
| **Try again** after a long wait | Retry while preserving position/play state |
| Subtitle menu → **Add subtitle…** | Use a local subtitle in this browser |

Phones put chapters/picture in picture in Settings when available. Audio/subtitle
tracks, HDR and other controls depend on the file/browser. **Ambient mode** follows
picture colors; **Stable volume** softens volume changes; **Aspect** changes picture fit.
Image alternatives use **Preview quality**. Original web uploads stay unchanged;
Telegram may not create alternatives.

Hover the seek bar for frame previews. Playback buffers ahead and can start
before the whole file arrives. **Software decoding on this device** can limit
playback even with a fast connection; use **Download Original** for another player.
[Buffering and chapter preview details](performance.md#video-controls-and-startup).

</details>

The video player's **Settings → Copy playback diagnostics** copies the build,
player mode, playback position and available buffering readings. You can also
copy them from the playback recovery controls. If clipboard permission is
unavailable, TGDrive shows selectable text. A report leaves out file names,
paths, URLs and access tokens.

## Share and revoke

If a public link has expired or been revoked, ask the owner for a new link.
Use **Try Again** when a temporary connection problem prevents the link from
loading.

1. Use the item's share action and optionally add a label.
2. Choose an expiry: one, seven or 30 days, no expiry, or a custom date/time.
3. Copy the link. Review, edit, copy or revoke it later in **Shared Links**.

Custom expiry uses the installation timezone shown beside the date/time field,
including when editing a link. If a clock change skips that time or makes it
occur twice, choose another expiry time; TGDrive rejects it rather than guessing
an offset. Editing only a label preserves the existing expiry.

New file shares open TGDrive's viewer at `/embed`; unsupported formats offer an
original download. That file Share Link also works as an iframe source. Folder
shares open the shared file browser and offer **Copy Embed Link** for an iframe.
**Copy Download Link** creates an attachment URL for download managers. Older
direct `/file` links remain usable for file bytes.

Folder links include accessible current and future descendants. To require a
password, enter one when creating the link or edit the link in **Shared Links**.
Recipients open the link in a browser and enter its password before viewing or
downloading. If unlocking fails, focus returns to the password field for another
attempt. The browser unlock lasts up to eight hours. Changing the password
or revoking the link ends existing unlocks immediately. Labels organize links,
and each item can have up to 100 independent links.

Links survive moves/renames. Moving into a shared folder asks for confirmation;
moving out removes inherited access but keeps explicit links. Trash revokes links
in its subtree. Revoking one link leaves others working and cannot recall bytes
already delivered or downloaded.

**Shared Links** has **Active / Expired / All** filters. Hover, focus or tap an
expiry for its exact time/timezone. Unavailable links explain why copying is
disabled. If the app cannot reconstruct a URL after a signing-secret change,
its original copy remains valid until expiry/revocation; create a replacement
from **My Files** and revoke the old one separately.

**Link opens** counts a visit after the public share or embed displays. A later
visit or page reload counts again. Failed unlocks, child navigation, pagination
and file downloads do not add an open. Only the owner sees the total and its
start date. Refresh Shared Links to see new totals. Counts go into the next
catalog snapshot. Older links start counting with their first tracked visit.

For a download manager, choose **Copy Download Link**. It creates or reuses a
separate share labelled **Download link**; new ones expire after one day. Keep
the URL private and manage it in **Shared Links**.

### Embed a file or folder in a website

For a file, use its ordinary **Share Link** as the iframe's `src`. For a folder,
choose **Copy Embed Link**. Both use the existing share's access and expiry.
If a browser cannot keep the unlock inside a password-protected embed, use
**Open in a new tab** and enter the password there.

For example, replace the URL here with your copied link:

```html
<iframe
  src="https://drive.example.com/embed?path=%2Fshare_FOLDER_ID&amp;auth=TOKEN"
  title="Shared project files"
  width="100%"
  height="600"
  allow="fullscreen; picture-in-picture"
  allowfullscreen
  loading="lazy"
  referrerpolicy="no-referrer"
></iframe>
```

Files open in the viewer; folders allow browsing, previews and downloads without
owner login. Folders load an initial page. **Show more** appends another
batch. With keyboard activation, focus stays on the button during loading
and moves to the first new item when the batch arrives.
In a small frame, long paths show the shared root, parent, and current folder.
TGDrive must remain reachable. The embed uses the link's access, password,
expiry and revocation. For a password-protected embed, HTTPS lets browsers
store a separate unlock for the embedding site. Use **Open in a new tab** if
the browser blocks cookies in an iframe. Anyone viewing the containing page
can obtain the link.

If blocked, allow TGDrive in your site's frame policy and framing of **`/embed`
only** in TGDrive's proxy; keep owner pages protected.
[Browser security policy](reference.md#security-and-privacy).

## Transfer history

Running and queued transfers use compact rows above the history filters. While
uploads are active, the Transfers page shows one tab notice for the queue.

**Transfers** lists **Running** and **Queued**, then history once a task finishes.
On narrow screens, use the chevron beside a long filename to read its full name.
When there are no transfers, **Open My Files** takes you to the file browser.
The **Finished** count covers all pages of the current search and filter.
Filter by **All / Successful /
Failed / Cancelled**, or search names across history pages.
Each page holds 20 tasks; a folder counts once and expands to its files.
Completed files offer **Show in folder**; folder batches offer **Show folder**.
**Cancel All** appears next to the first group when there is work you can
cancel. It opens a confirmation before changing any transfers.

Returning to **Transfers** from another page keeps the outer history page and
filter, showing the last loaded rows while checking for updates. Leaving clears
its search and closes expanded folder batches. If refresh fails, matching rows
remain with an out-of-date warning; choose **Try Again** to reload them. History
polling stops while the page is hidden. Logging out or entering a public share
or recovery view resets this retained history state.

Finished server history survives restart and appears on other devices. Device-only
recovery stays in its browser. If server history cannot load, local recovery rows
remain available to choose originals, retry or cancel once the endpoints are reachable.

**Clear history** removes finished entries across all pages, ignoring the current
filter/search. It cannot be undone. It keeps stored files, active work and uploads
still needing originals, including failed uploads with resumable [receipts](reference.md#terms-used-here).
Folders with unfinished files retain their rows until cleared after completion.
Server history clears for all devices; browser-only history clears here.

The desktop transfer panel floats on your drive pages. Open **Transfers** for
the full queue. Click the floating panel’s header to collapse it. Your choice
stays in effect as you navigate. The compact view shows the
active filename or folder, its stage and a progress
ring. Concurrent files in the same stage share a byte-weighted percentage. The
byte count and estimated time refer to that stage. A folder also shows how
many files the server has received or finished. When the total is unknown, the
ring indicates activity. A waiting transfer shows its reason.

Click the summary to expand the queue and see individual files, retry or cancel.
**All transfers** opens the full history. Scroll files above the panel to reach
their actions.
**Transfers** also shows server jobs from other devices. Use the history page
to manage saved records.
[History storage](architecture.md#finished-history).

<a id="create-and-extract-zip-archives"></a>

## Create ZIPs and extract archives

**Create ZIP:** choose **Select**, select files/folders, then **Create ZIP**.
Name the ZIP and start. It is saved as a normal file in the shown destination;
download it when finished. A folder's menu also offers **Create ZIP**. Structure
and empty folders are preserved; originals remain. Enter a ZIP password to
encrypt the files with AES-256. ZIP filenames remain visible.

**Extract:** open the archive's menu → **Extract archive**, choose the current
folder or a new folder, then start. Existing names are kept alongside new
outputs; the original archive remains. For an encrypted ZIP, enter its password.
TGDrive uses the password for that attempt. If the password is wrong, TGDrive
removes the partial extraction and keeps the downloaded ZIP for retry.
Open **Transfers** and choose **Retry** to enter the password again.

| Extract | Extensions |
| --- | --- |
| ZIP, 7-Zip, RAR | `.zip`, `.7z`, `.rar` |
| TAR | `.tar` |
| Compressed TAR | `.tar.gz` / `.tgz`, `.tar.bz2` / `.tbz` / `.tbz2`, `.tar.xz` / `.txz`, `.tar.zst` / `.tzst` |
| Single compressed file | `.gz`, `.bz2`, `.xz`, `.zst` |

Follow, retry or cancel in **Transfers**. Accepted jobs continue with the tab
closed while TGDrive runs. Work downloads source bytes and uploads outputs,
using temporary server disk and Telegram bandwidth. Outputs in a shared folder
inherit its access; review the warning.

Split archives, encrypted formats other than ZIP, unsafe or duplicate paths,
links and empty regular files are rejected. Some RAR features and large
dictionaries are unsupported.
ZIP creation uses no compression. Docker includes the archive reader; other
installations need libarchive.

Failure/cancellation may leave finished outputs. Retry the existing job to
continue. Preserve cache across restarts; missing temporary data may require
cancelling and starting again, keeping earlier outputs.
[Exact archive limits](api.md#archive-jobs).

## Preload a file for smoother playback and downloads

1. Choose **My Files → file ⋯ → Preload file**.
2. Open **Preloads**. Look for **Fully cached** with full coverage
   on the original file's row; **Partial** rows show a small coverage bar.
   A row such as **Poster for Movie.mkv** identifies a preview image.
   **Poster cached** means only that image is complete, not the original video.
3. Play, download or share normally. Recheck coverage before relying on it.

The **Cached entries** list includes originals and posters. Older entries can
acquire names from matching catalog records in the background; this is not
instant. Missing or ambiguous catalog matches can leave an entry labelled
**Cached data · message 123** rather than guessing a filename.
Labels describe one cache entry. A legacy multipart entry can contain only one
stored part; use **Preload file** to prepare and verify the whole logical file.

Preloading uses server disk/bandwidth and continues with the tab closed while
TGDrive runs. Foreground playback/downloads take priority. **Cancel** keeps
cached bytes; clearing cache keeps Telegram originals. Cached copies can expire
or be evicted. **A preload is not a backup** and cannot keep a sleeping server online.

<details>
<summary>Read-ahead, download managers and cache space</summary>

### What happens when I just open or stream a file?

Requested sections are cached with bounded read-ahead. **Preload during playback**
defaults On and warms upcoming original audio/video. Opening once does not
automatically preload the whole original. PDFs stay sparse.
[Read-ahead policy](performance.md#automatic-read-ahead).

### Parallel and resumed downloads

Paste **Copy Download Link** into your manager. It can request parallel ranges
and resume if it keeps its own received-byte state. Downloads read ahead within
requested ranges rather than warming unrelated gaps or the whole file.

### Storage, progress and limits

A whole-file preload must fit in memory cache **or** disk cache, allowing for
overhead and other jobs; the budgets are not combined. If rejected, free space,
free server memory or disk (cache budgets follow them automatically) or wait. Larger files can still stream
in chunks. Preloads sorts by newest downloaded data. Cache access still needs
valid authorization; network/Telegram delays and decoding can still buffer.
[Cache policy](performance.md#cache-limits).

</details>

## Trash and permanent deletion

Trash shows **Deleted from: My Files / …** to identify each item's source.

- **Move to Trash** revokes every share link for the item. Trashing a folder
  also revokes links for everything inside it. Telegram files are kept.
- **Restore** brings the item back, subject to name and access checks. Anyone
  using a revoked link needs a new one. An item pending permanent deletion
  cannot be restored.
- **Delete permanently** removes its database records, descendants and shares
  after confirmation. It cannot be cancelled or restored from Trash.

You can keep browsing as soon as you confirm a deletion. Trash shows progress
after the server accepts it. Accepted deletions continue after you close the tab
or restart TGDrive.
Failed items reappear; use **Retry Failed deletions** in the Trash notice.

### Why permanent deletion keeps the Telegram files

**Original Telegram messages remain.** Channel members may still read them;
older backups may restore their records. Recipient copies and caches also remain.
Removing an item from TGDrive does not securely erase its contents. Erasing
Telegram originals separately requires an authorized account.
[Deletion and backup protection](backup-recovery.md).

## Import from a URL

Choose **New → Upload from URL** and paste a public HTTP(S) file URL.
**Transfers** shows **Checking source…**, source downloading, then Telegram uploading.
You can close the tab after submission while TGDrive runs.

By default, two imports receive at full speed. Others receive slowly or wait for
a source connection/temporary disk; TGDrive tunes how many run at full speed.
**Compatibility Mode** helps sources that reject parallel retrieval.

Temporary interruptions retry with a countdown. After retries stop, correct the
error and use **Retry**; intake restarts from the beginning. An expired source
link may need replacing. Private-network URLs are blocked by default. If the
server job is gone, submit a new import; it never needs a device file.
[URL intake limits](performance.md#url-import-intake).

## Import a torrent or magnet link

Choose **New → Import torrent** in the destination folder. Paste a magnet or
public HTTP(S) `.torrent` URL. For a torrent with several files, TGDrive reads
its file list first and asks in **Transfers** which files to import: all are
selected; untick the ones you don't want, then choose **Import N files**. Only
the chosen files are downloaded, reserve temporary space and are saved, keeping
their nested folders; a folder torrent creates its own root.

Transfers shows metadata discovery, downloading, then Telegram uploading.
You can close the tab while TGDrive runs. The chosen files need temporary
server space before Telegram uploading; downloads need reachable peers.
Temporary timeout/network interruptions retry automatically with a countdown.
After the attempt budget is exhausted, or for peer, disk or input errors, fix
the cause and use **Retry** on the existing transfer. **Cancel** remains
available; files already saved remain.
[Torrent limits and restart behavior](api.md#torrent-imports).

Peers can see the server's IP and receive downloaded pieces while downloading.
TGDrive stops seeding after download. Import only content you are entitled to use.

## Use several devices

Open your reachable TGDrive website on any supported browser and sign in with
the owner password. Logins have separate sessions; **Log Out** affects only that
session. Tabs in one browser profile share a session.

Everyone using the owner password has full access to the same files/settings;
there are no per-person accounts or permissions. Give others read-only share
links when they need only selected files. For access while your laptop is off,
[run TGDrive on a server](hosting.md#local-computer-or-server).

## Find your way around

Expand **Tools and settings** in the sidebar to reach owner tools. Opening a
tool directly reveals its active link in the group.

| Page | Purpose |
| --- | --- |
| **My Files / Recent / Starred / Trash** | Browse, reopen, bookmark or restore files |
| **Transfers / Shared Links** | Follow work or manage access |
| **Performance / Preloads** | Inspect resources or prepare cached files |
| **Connections / Backups** | Manage accounts, saved data and recovery |
| **Settings** | Cache, transfer limits, timezone and bot upload destination. Saves apply to the selected section. |

Choose Light, Dark or System in the appearance menu. **Refresh / Check Again**
stay disabled while working, then confirm or show an error. Retry failed refreshes;
configuration drafts are retained, while management actions wait for a successful refresh.

## Settings and connections

Use **Connections** to refresh credentials/channel access, manage
accounts, add storage bots or connect a Premium account. **Refresh** requests
checks; each account row shows its own result or error. Keep one working storage bot.

Additional bots need administrator access to the same private channel with
**Post Messages** and **Delete Messages**. Add them here rather than editing token
lists. [Connection setup](setup.md#additional-bots-and-premium-accounts).

Saved changes connect at once. Add accounts at any time; finish/cancel pending
transfers before removing, disabling or replacing one, or changing API
credentials. Repairing credentials for the same Telegram account can recover
pending work. Blank replacement fields keep secrets. [Connection maintenance and recovery](operations.md#connection-changes).

If you disconnect while saving connection, bot-upload or performance settings,
a save that has begun committing may still finish. Reload the settings and check
the displayed values before retrying; disconnecting does not undo the save.
The accepted write and its apply or rollback finish before another save in that
settings service is admitted.

### Storage and backup

**Backups → Saved to MongoDB** confirms the catalog is saved, not that
unfinished uploads are complete. **Temporary storage** holds unfinished work on
server disk, not your total Telegram storage.

**Back Up Now** creates one encrypted recovery file. Automatic copies normally
run daily without an independent channel, including unchanged catalogs;
independent protection batches changes.
Each backup is pinned in your channel and opens only with your encryption key.
[Backup setup and restore](backup-recovery.md).

## Upload through your Telegram bot

Your server operator chooses which Telegram users may use the bot
([bot access settings](configuration.md#telegram-bot-access)). Use its **private chat**:

1. On the website, choose **Settings → Transfer limits → Bot uploads →
   Change folder**, then **Use this folder** (or **Use default folder**).
2. Send or forward files, photos, video or audio to the bot. Send photos as files
   to preserve quality.
3. The bot acknowledges each queued file. When its turn starts, a progress
   message updates in place, about every
   4 seconds and at 5% steps when the total is known. Unknown totals show received
   bytes. Status moves from **Receiving · 42%** to **Saving**, **Finalizing** or **Verifying**,
   then **Saved ✓** with the name, size and folder. When the website address is
   known, an **Open in TGDrive** button opens that folder. Several files can be
   sent at once; each gets its own status message and waits its turn.

Users who are not allowed cannot import files. The bot can reply with their
Telegram user ID so the owner can allow them.

Default: **My Files / TGDriveBotModeFiles**, created on first use. All allowed
users share the destination. Each queued file keeps its chosen folder and
encryption mode. Wait for pending bot imports to finish or cancel them before
changing these settings; no restart is needed.
Moving/deleting it pauses new imports until an available folder is chosen.
Repeated sends create copies; conflicting names are kept alongside existing ones.

**Encrypt bot uploads** (on by default) makes the server download each file and
upload it encrypted, which takes longer for large files. Turn it off for
Telegram-side copies that stay unencrypted and readable to anyone with channel
access. If encrypted importing is unavailable, encryption-on fails instead of
falling back to a plaintext copy. [Encryption details](reference.md#stored-file-encryption).

Bot files join the website **Transfers** queue. TGDrive processes one bot file
through its entire receive-and-save pipeline before starting the next; sending
50 files does not start 50 downloads. Other website transfers and streams still
use their existing schedulers and shared speed/storage limits.

The bot confirms the queued file's folder and encryption mode. When temporary
upload storage is full, the first file waits for space and appears as
**Waiting for temporary storage** in Transfers. Encrypted imports then progress
from **Receiving from Telegram bot** to the usual Telegram upload. Cancel in Transfers, or reply **/cancel** to the original file
message in the bot chat. Cancellation can require cleanup and does not delete
already saved files. A copy with an uncertain Telegram outcome may require owner
review; check the drive before resending it.

If Telegram times out during a retryable import, Transfers shows **Failed**
and “Telegram timed out. Try again.” The bot points you to **Retry** in
Transfers when Telegram can deliver its reply. Use that action to continue the
same import.

Reprocessing the same incoming message reuses its transfer instead of downloading
twice. After restart, files still waiting to start resume when the main bot
reconnects. An interrupted running import shows **Failed** and waits for **Retry**.
An incomplete download restarts from zero. A fully received upload retains its
staged file for retry. Telegram must still retain the source attachment and allow
the bot to read it.

### Pool bot downloads through a private intake channel

To let available storage clients fetch different chunks of an encrypted bot
import, configure **BOT_INTAKE_CHANNEL** with a separate private channel ID.
Add the main bot and every participating storage bot to that channel; the main
bot needs permission to post and delete its temporary messages. Keep this channel
separate from the drive's storage channel and independent backup channel.
Recreate the app after changing this deployment setting.

**Temporary copies are plaintext.** Channel members can read incoming originals
until they are deleted, even when **Encrypt bot uploads** is on. Source chunks
also pass through the server's private local media cache and upload staging.
Only enable this on a channel whose membership you control. Final drive files
still follow your encryption setting.

For its active file, the queue copies the source to the intake channel and uses
the pooled reader to receive it into staging, with foreground scheduling and
the shared download-speed limit. Adding bots can increase
available download capacity; it cannot guarantee a particular speed. Other files
wait their turn. With no intake channel configured, receipt uses the main bot as
before. Encryption-off imports keep their direct Telegram copy path.

Temporary messages and their exact cache entries are cleaned up when no longer
needed. Network or permission failures can delay deletion; pending cleanup must
be resolved before temporary plaintext can be considered removed. An uncertain
copy result is kept for review instead of blindly duplicating or deleting files.
The bot queue pauses until known copies are cleaned up or the owner reviews an
unknown copy outcome. For an unknown outcome, check the private intake channel in
Telegram and remove the temporary copy there if you can identify it. In
**Transfers**, choose **Confirm intake channel reviewed** on the affected row and
confirm to let the next file proceed. TGDrive records your confirmation and keeps
the transfer outcome. Confirming does not retry the import or delete messages.
Website transfers and streaming keep their own scheduling.
Pending cleanup records remain visible in Transfers even after the final file is
saved or the import is cancelled. Clearing history preserves these records, and
restarting preserves the next retry time for known copies.
The sender's original private-chat message is never a cleanup target.

If the bot says **Upload could not be confirmed**, check the drive before
resending: its database write may have succeeded.

**/start** shows a short welcome, destination and encryption mode. **Help** or
**/help** shows commands and encrypted-import cancellation guidance. Progress
shows the stage, percentage and bytes without repeating the quoted filename.
**/current_folder** shows the destination,
and **/set_folder** links to the website setting.
[Bot access settings](configuration.md#telegram-bot-access) · [Known recovery gaps](roadmap.md#known-correctness-and-recovery-gaps)

## Try the public demo

**[Open the demo](https://tgdrive.nexgenbots.us/)** · **Password:** `password12345`

Browse, preview and download the protected **Demo Files** collection. Use
**My Files** outside that collection to try uploads, organizing and sharing.
Configuration/Management are visible with changes locked; backups cannot be
downloaded.

**Use disposable, non-private files only.** Other visitors can change/delete
unprotected files. Demo mode does not automatically reset them or provide
private storage. [Host a demo](configuration.md#public-demo-mode).
