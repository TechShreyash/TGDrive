# Screenshots and demo videos

[Docs home](../README.md) · [Visual walkthroughs](../showcase.md) · [Project README](../../README.md)

These are real captures of [TGDrive's public demo](../usage.md#try-the-public-demo),
recorded on **24 October 2026** using original, disposable sample files. The
README highlights the drive, video player and mobile layout; the
[visual guide](../showcase.md) groups the full collection by workflow.

The video captures predate the switch to Movi's native chapter/settings UI.
Follow the [current controls guide](../usage.md#open-preview-and-download) for
labels and behavior; replace these captures during the next live media refresh.

## What is included

| PNG images | Coverage |
| --- | --- |
| `drive-overview`, `drive-list`, `drive-dark`, `mobile-drive` | Grid/list browsing, appearance and compact layout |
| `file-actions`, `search-and-sort`, `starred-files`, `trash-restore` | Organization, search and recovery from Trash |
| `upload-folder`, `url-import`, `upload-progress`, `upload-completed`, `transfer-history` | Upload entry points, live device progress and finished tasks |
| `media-preview`, `video-settings`, `image-preview`, `audio-preview`, `text-preview` | Player controls and file previews |
| `pdf-reader`, `epub-reader`, `comic-reader` | Page/chapter navigation and reading preferences |
| `share-link`, `shared-folder`, `shared-links` | Expiry choices, recipient view and link management |
| `preloads`, `performance-overview`, `performance-resources` | Cache coverage, traffic and server resources |
| `cache-settings`, `transfer-settings`, `regional-settings`, `thumbnail-rebuild` | Configuration and rebuild confirmation |
| `connections`, `storage-backups` | Safe management entry points and backup status |

Most desktop images are 1536 × 695; the recipient view is 1536 × 639. Connections
is cropped to its identifier-free 1260 × 230 top panel. The mobile image is
390 × 843, captured with a 390 × 844 browser viewport, not a physical phone.

| Silent MP4 | Length | Recorded workflow |
| --- | --- | --- |
| [Organize files](organize-files.mp4) | About 3:26 | Views, search, sort, favorites, folder creation, rename and move |
| [Upload and share](upload-and-share.mp4) | About 5:20 | Device upload, progress, history, Show in Folder, share label/expiry, copying and revocation confirmation |
| [Previews and readers](readers-and-preview.mp4) | About 5:51 | Image/audio/text previews, PDF/EPUB/comic navigation and video playback/settings |

[Find a feature by its video timestamp](../showcase.md#see-tgdrive-in-action).

Videos use continuous Windows Graphics Capture of the live browser, encoded as
H.264 MP4 at 30 fps and normal speed. A large cursor overlay follows logged click
coordinates, with click highlights and captions in a separate 96-pixel bottom
band. The upload video joins two chapters with a two-second title card and adds
five-second end holds; captured actions are not accelerated. Browser audio is
not captured. Browser suggestions are masked in the organization video, and the
browser infobar is cropped from the sharing chapter. Raw captures and interaction
logs remain in ignored local staging. These recordings illustrate workflows,
not transfer benchmarks, device compatibility or recovery guarantees.

The device file picker is outside the browser capture. Link generation happens
between the upload and management chapters; the recipient view is shown in a
screenshot. The video stops at the revocation confirmation, and the temporary
link was revoked after recording.

The folder-upload image shows the **New** menu. Directory selection could not be
completed through the capture tool; the videos demonstrate ordinary file uploads.
Follow the [folder-upload guide](../usage.md#upload-files-or-folders) for selection
and confirmation. URL import, thumbnail rebuild, Telegram connections and backup
captures show controls; they do not establish a completed import, rebuild,
authentication or recovery. Server settings were not changed for these captures.

## Replace or extend a capture

1. Use an isolated collection with disposable, original or redistributable files.
   For these captures, the image, six-second video, audio, PDF, ebook and comic
   contents were created specifically for the demonstration.
2. Capture the feature in a useful, settled state. Keep the navigation and controls
   legible; dismiss unrelated notifications. Use a portrait viewport for mobile
   examples and restore the original viewport/appearance afterward.
   Record continuously at normal speed, keep the cursor visible and pause long
   enough to read each step. When capture omits the cursor, base any cursor overlay
   on the actual recorded interactions. Inspect the export for missing or duplicate
   pointers, unreadable captions and abrupt transitions before replacing it.
3. Exclude passwords, tokens, recovery keys, phone numbers, account names, channel
   IDs and unrelated files. Capture sharing options before a URL is generated;
   revoke disposable demonstration links afterward. Crop management screens to
   safe controls rather than publishing identifiers.
4. Save the final PNG or silent MP4 here with a descriptive name. Keep raw frames,
   browser/session data and temporary files in ignored local staging. Preserve
   the actual image format when converting captures.
5. Update the relevant caption and link in the [visual guide](../showcase.md),
   then the README or canonical user guide where it helps. Add the exact path to
   `PUBLIC_DOCS` in [build_release.py](../../scripts/build_release.py); remove
   obsolete assets and references together.

The current PNGs and MP4s are stored in the repository and included in public
source releases. Video posters link directly to the local MP4, so they also work
in an extracted source archive. GitHub may open a file/download page instead of
playing a repository-relative video inline; these are not uploaded GitHub video
attachments. If a maintainer later uploads a video attachment through GitHub's
Markdown editor, replace the link with that real attachment URL.

