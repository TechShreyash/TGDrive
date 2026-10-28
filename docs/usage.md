# Using TGDrive

[Docs home](README.md) · [Setup](setup.md) · [First-use essentials](new-user-guide.md) · [Troubleshooting](operations.md#troubleshooting)

Start in **My Files**: upload a small file, open it, then try sharing it.
[Watch the walkthroughs](showcase.md) if you prefer a visual introduction.

| Task | Jump to |
| --- | --- |
| Add files | [Upload](#upload-files-or-folders) · [Resume an upload](#can-i-close-the-tab) |
| Find or use files | [Browse](#browse-and-organize) · [Preview and download](#open-preview-and-download) |
| Give someone access | [Share links](#share-and-revoke) · [Embed](#embed-a-file-or-folder-in-a-website) |
| Other ways to import | [URL](#import-from-a-url) · [Torrent](#import-a-torrent-or-magnet-link) · [Telegram bot](#optional-telegram-bot-interface) |
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

The panel shows device progress until every remaining file from this browser is
received and accepted. Accepted files show **Received** during that stage;
Telegram work already runs in the background. The panel then switches to
Telegram progress. A full device bar still needs server confirmation.

Time remaining estimates only the displayed stage; it disappears during stale
progress, blocked work and final confirmation. **Hide/Show** collapses the desktop
panel. On phones, the status button opens **Transfers**.

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
- The picker accepts up to 1,000 files and 1,000 discovered folders, subject to
  path/request limits. It skips empty and oversized files; browser pickers omit empty folders.
- Keep the confirmation open during preparation. **Retry** there reuses the
  tree; closing early and choosing again can leave another empty root.
- Once queued, resume the existing batch. Choose files individually if bulk matching fails.
- Files uploaded into a shared folder inherit its access.

[Folder protocol and bounds](reference.md#atomic-folder-preparation) ·
[Restart recovery](operations.md#after-a-shutdown-crash-or-network-loss)

</details>

### Files above 2 GB

TGDrive splits large files automatically and presents them as one file for
playback/download. Premium is optional. Defaults allow **16 GiB per file**, subject
to available temporary upload space; every account uses Telegram documents of at
most **2000 MiB**. Keep the catalog and every part for recovery.
[Change upload limits](configuration.md#upload-size-and-available-disk-space).

### Upload speed and queue

Device intake and Telegram uploading run independently. Device concurrency
adapts automatically; start with the defaults. Extra bots or upload slots can
help some workloads but do not bypass Telegram waits or guarantee more speed.
[Transfer settings](configuration.md#transfer-limits) · [Find a bottleneck](performance-tuning.md#find-the-bottleneck)

## Browse and organize

| Action | How |
| --- | --- |
| Open | Double-click with a mouse, press Enter on a focused name, or tap on touch |
| Select several items | **Select**, then choose rows/check boxes; **Done** or Escape clears selection |
| Create a folder | **New → New folder** |
| Rename, move or trash | Item menu or selection actions |
| Sort/change view | Name, Date Added or File Size; List/Grid; **View and sort** on phones |

Folders stay first when sorting; click an active column to reverse order. View
choice is remembered. Name conflicts offer **keep both** or **skip**.

Supported uploads generate covers automatically. For missing covers or a full
rebuild, see [thumbnails](thumbnails.md).

### Starred files and folders

Choose **Add to Starred / Remove from Starred** in the item menu. Stars are
bookmarks, not shared access. They follow moves/renames, hide in Trash and return
on restore; removing a star keeps the file.

### Search

Search finds names, not document contents. **My Files** searches the whole drive;
other pages search their own items. **Transfers** includes all history pages.
Press Enter to search, Escape to clear, or **/** / **Ctrl/Cmd+K** to focus search
outside a text field. Switching pages clears the query. On phones, use the search icon.

## Open, preview, and download

Open a file to preview; **Download** gets the original. Escape or browser Back
closes the preview. Use fullscreen where supported. If the device cannot decode
a file, download it for a suitable app.

| File | Preview |
| --- | --- |
| Images, including SVG | Image viewer |
| PDF up to 256 MiB | Scroll, zoom, Fit width and Fit page |
| Video/audio | Player with available track and subtitle controls |
| Supported text/code | Read-only text, first 256 KiB |
| EPUB | Chapters, text size and saved reading position |
| CBZ/CBR | Comic pages, fit and right-to-left navigation |
| Unsupported formats | Original download |

Videos open paused. Choose **Continue from…** for a saved position or **Play** to
start from the beginning. Positions belong to this site's browser profile;
different devices watch independently, while people sharing a profile share
positions. Finishing clears the position. Clearing site data, private browsing,
unavailable local storage or an abrupt shutdown can lose progress. Standalone
stream links use the browser's native player instead.

Videos use a sharp saved poster before playback when available. Older
files or unavailable posters use a representative frame from halfway through
the video; loading can take time and the picture can still be black.
[Poster storage, fallback and limits](thumbnails.md#video-posters).

Loading/buffering can reflect Telegram waits, network speed or device decoding.
Image/audio previews retry a full Telegram queue at most twice while open; then
use **Try Preview Again**. Other failures need manual retry. Downloads use the
browser's controls and need it connected; they have no saved server queue position
after restart. [Playback and delivery policy](performance.md#video-controls-and-startup).

PDFs, EPUB and comics request sections as needed rather than automatically
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

## Share and revoke

1. Use the item's share action and optionally add a label.
2. Choose an expiry: one, seven or 30 days, no expiry, or a custom date/time.
3. Copy the link. Review, edit, copy or revoke it later in **Shared Links**.

**Anyone with the link can read its contents.** Folder links include accessible
current and future descendants. Labels organize links; they do not restrict
access to named people. Each item can have up to 100 independent links.

Links survive moves/renames. Moving into a shared folder asks for confirmation;
moving out removes inherited access but keeps explicit links. Trash revokes links
in its subtree. Revoking one link leaves others working and cannot recall bytes
already delivered or downloaded.

**Shared Links** has **Active / Expired / All** filters. Hover, focus or tap an
expiry for its exact time/timezone. Unavailable links explain why copying is
disabled. If the app cannot reconstruct a URL after a signing-secret change,
its original copy remains valid until expiry/revocation; create a replacement
from **My Files** and revoke the old one separately.

For a download manager, choose **Copy Download Link**. It creates or reuses a
separate share labelled **Download link**; new ones expire after one day. Keep
the URL private and manage it in **Shared Links**.

### Embed a file or folder in a website

Create a share, choose **Copy Embed Link**, then use it as your iframe's `src`.
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
owner login. TGDrive must remain reachable. The embed uses the same share's
access, expiry and revocation; copying it creates no additional link.
Anyone viewing the containing page can obtain the link.

If blocked, allow TGDrive in your site's frame policy and framing of **`/embed`
only** in TGDrive's proxy; keep owner pages protected.
[Browser security policy](reference.md#security-and-privacy).

## Transfer history

**Transfers** lists **Running** and **Queued**, then finished history. Filter by
**All / Successful / Failed / Cancelled**, or search names across history pages.
Each page holds 20 tasks; a folder counts once and expands to its files.
Completed files offer **Show in folder**; folder batches offer **Show folder**.

Finished server history survives restart and appears on other devices. Device-only
recovery stays in its browser. If server history cannot load, local recovery rows
remain available to choose originals, retry or cancel once the endpoints are reachable.

**Clear history** removes finished entries across all pages, ignoring the current
filter/search. It cannot be undone. It keeps stored files, active work and uploads
still needing originals, including failed uploads with resumable receipts.
Folders with unfinished files retain their rows until cleared after completion.
Server history clears for all devices; browser-only history clears here.

The desktop floating panel shows this browser's work; Transfers also shows other
devices' server jobs. Clearing the panel does not erase saved history.
[History storage](architecture.md#finished-history).

<a id="create-and-extract-zip-archives"></a>

## Create ZIPs and extract archives

**Create ZIP:** choose **Select**, select files/folders, then **Create ZIP**.
Name the ZIP and start. It is saved as a normal file in the shown destination;
download it when finished. A folder's menu also offers **Create ZIP**. Structure
and empty folders are preserved; originals remain.

**Extract:** open the archive's menu → **Extract archive**, choose the current
folder or a new folder, then start. Existing names are kept alongside new
outputs; the original archive remains.

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

Encrypted/multi-volume archives, unsafe or duplicate paths, links and empty
regular files are rejected; some RAR features/large dictionaries are unsupported.
ZIP creation uses no compression. Docker includes the archive reader; other
installations need libarchive.

Failure/cancellation may leave finished outputs. Retry the existing job to
continue. Preserve cache across restarts; missing temporary data may require
cancelling and starting again, keeping earlier outputs.
[Exact archive limits](reference.md#archive-jobs).

## Preload a file for smoother playback and downloads

1. Choose **My Files → file ⋯ → Preload file**.
2. Follow **Monitor → Preloads**. **Ready** with full coverage means the original
   is fully cached on the server; blue segments show partial coverage.
3. Play, download or share normally. Recheck coverage before relying on it.

Preloading uses server disk/bandwidth and continues with the tab closed while
TGDrive runs. Foreground playback/downloads take priority. **Cancel** keeps
cached bytes; clearing cache keeps Telegram originals. Cached copies can expire
or be evicted. **A preload is not a backup** and cannot keep a sleeping server online.

<details>
<summary>Read-ahead, download managers and cache space</summary>

### What happens when I just open or stream a file?

Requested sections are cached with bounded read-ahead. **Preload during playback**
defaults On and warms upcoming original audio/video. Opening once does not
automatically preload the whole original. PDFs/books/comics stay sparse.
[Read-ahead policy](performance.md#automatic-read-ahead).

### Parallel and resumed downloads

Paste **Copy Download Link** into your manager. It can request parallel ranges
and resume if it keeps its own received-byte state. Downloads read ahead within
requested ranges rather than warming unrelated gaps or the whole file.

### Storage, progress and limits

A whole-file preload must fit in memory cache **or** disk cache, allowing for
overhead and other jobs; the budgets are not combined. If rejected, free space,
raise **Configuration → Cache settings** or wait. Larger files can still stream
in chunks. Preloads sorts by newest downloaded data. Cache access still needs
valid authorization; network/Telegram delays and decoding can still buffer.
[Cache policy](performance.md#cache-limits).

</details>

## Trash and permanent deletion

Trash shows **Deleted from: My Files / …** to identify each item's source.

- **Move to Trash** hides the item and revokes its shares, keeping Telegram files.
- **Restore** brings it back, subject to name/access checks.
- **Delete permanently** removes its database records, descendants and shares
  after confirmation. It cannot be cancelled or restored from Trash.

Accepted deletions continue on the server after closing the tab or restarting.
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
a source connection/temporary disk. **Configuration → Transfer limits → URL
downloads** changes concurrency; **Compatibility Mode** helps sources that reject
parallel retrieval.

Temporary interruptions retry with a countdown. After retries stop, correct the
error and use **Retry**; intake restarts from the beginning. An expired source
link may need replacing. Private-network URLs are blocked by default. If the
server job is gone, submit a new import; it never needs a device file.
[URL intake limits](performance.md#url-import-intake).

## Import a torrent or magnet link

Choose **New → Import torrent** in the destination folder. Paste a magnet or
public HTTP(S) `.torrent` URL. Every file is imported, keeping nested folders;
a folder torrent creates its own root.

Transfers shows metadata discovery, downloading, then Telegram uploading.
You can close the tab while TGDrive runs. The whole torrent needs temporary
server space before Telegram uploading; downloads need reachable peers.
Use **Retry** or **Cancel** on the existing transfer. Files already saved remain.
[Torrent limits and restart behavior](reference.md#torrent-imports).

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

| Page | Purpose |
| --- | --- |
| **My Files / Starred / Trash** | Browse, bookmark or restore files |
| **Transfers / Shared Links** | Follow work or manage access |
| **Monitor → Performance / Preloads** | Inspect resources or prepare cached files |
| **Management → Connections / Storage & Backups** | Manage accounts, saved data and recovery |
| **Configuration** | Cache, transfer limits, timezone and bot upload destination |

Choose Light, Dark or System in the appearance menu. **Refresh / Check Again**
stay disabled while working, then confirm or show an error. Retry failed refreshes;
configuration drafts are retained, while management actions wait for a successful refresh.

## Settings and connections

Use **Management → Connections** to refresh credentials/channel access, manage
accounts, add storage bots or connect a Premium account. **Refresh** requests
checks; watch each account's result for verification. Keep one working storage bot.

Additional bots need administrator access to the same private channel with
**Post Messages** and **Delete Messages**. Add them here rather than editing token
lists. [Connection setup](setup.md#additional-bots-and-premium-accounts).

Finish/cancel pending transfers before changing accounts, enabled states or API
credentials. Repairing credentials for the same Telegram account can recover
pending work. Saved changes pause new uploads until restart. Blank replacement
fields keep secrets. [Connection maintenance and recovery](operations.md#connection-changes).

### Storage and backup

**Storage & Backups → Saved to MongoDB** confirms the catalog is saved, not that
unfinished uploads are complete. **Temporary storage** holds unfinished work on
server disk, not your total Telegram storage.

**Back up now** creates one encrypted recovery file. Automatic copies normally
run hourly without an independent channel; independent protection batches changes.
Save **Backups & recovery → Download backup** outside the server/Telegram and
**Save recovery key** separately. Reconnect the same MongoDB after losing a local
installation. [Backup setup and restore](backup-recovery.md).

<a name="optional-telegram-bot-interface"></a>

<details>
<summary>Upload through your Telegram bot</summary>

Standard setup enables the main bot for Telegram users listed in
`TELEGRAM_ADMIN_IDS`. Use its **private chat**:

1. On the website, choose **Configuration → Transfer limits → Bot uploads →
   Change folder**, then **Use this folder** (or **Use default folder**).
2. Send or forward files, photos, video or audio to the bot. Send photos as files
   to preserve quality.
3. Wait for **Saving…** to become a filename/destination confirmation.

Default: **My Files / TGDriveBotModeFiles**, created on first use. All allowed
users share the destination; changes apply to new imports without restart.
Moving/deleting it pauses new imports until an available folder is chosen.
Repeated sends create copies; conflicting names are kept alongside existing ones.

If the bot says **Upload could not be confirmed**, check the drive before
resending: its database write may have succeeded. Bot imports lack the web
queue's full restart/resume guarantees.

**/start** and **/help** explain uploads; **/current_folder** shows the destination,
and **/set_folder** links to the website setting. The website address is remembered
when saved; `PUBLIC_URL` can supply it earlier. Installations using only
`BOT_TOKENS` must configure a main bot/allowed IDs and restart to enable this.
[Bot settings](reference.md#telegram-accounts) · [Known recovery gaps](roadmap.md#known-correctness-and-recovery-gaps)

</details>

## Try the public demo

**[Open the demo](https://tgdrive.nexgenbots.us/)** · **Password:** `password12345`

Browse, preview and download the protected **Demo Files** collection. Use
**My Files** outside that collection to try uploads, organizing and sharing.
Configuration/Management are visible with changes locked; recovery keys cannot
be downloaded.

**Use disposable, non-private files only.** Other visitors can change/delete
unprotected files. Demo mode does not automatically reset them or provide
private storage. [Host a demo](configuration.md#public-demo-mode).
