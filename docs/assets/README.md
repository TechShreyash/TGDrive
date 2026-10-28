# Screenshots and demo videos

[Docs home](../README.md) · [Gallery and video chapters](../showcase.md) · [Project README](../../README.md)

All current media was captured on **28 October 2026** from TGDrive's current UI.
The collection contains **25 JPEG screenshots** and one **62-second silent tour**.
It illustrates features, not throughput, physical-device compatibility or
successful recovery. Some sample files show an earlier **Added** date; that is
not the screenshot date.

<a id="readme-banner"></a>

## README lead image

The README uses [drive-overview.jpg](drive-overview.jpg), a real file-browser
capture. Keep its lead image current and free of private information. If its
filename changes, update the README, gallery and `PUBLIC_DOCS` in
[build_release.py](../../scripts/build_release.py) together.

## What is included

| JPEG basenames | Coverage |
| --- | --- |
| `drive-overview`, `drive-list`, `new-menu`, `file-actions`, `mobile-drive` | Browsing, upload/import entry points, actions and emulated phone layout |
| `torrent-import`, `create-zip`, `extract-archive`, `archive-result`, `url-import` | Import/archive dialogs and the completed Notes.zip |
| `media-preview`, `video-continue`, `video-settings`, `image-preview`, `audio-preview` | Current player, Continue, built-in settings, images and audio |
| `pdf-reader`, `epub-reader`, `comic-reader` | Document, ebook and comic readers |
| `preloads`, `performance-overview`, `performance-resources` | Local demo cache coverage and illustrative resource/traffic readings |
| `cache-settings`, `transfer-settings`, `thumbnail-rebuild` | Local demo configuration and rebuild confirmation |
| `recovery-file` | Local demo of the unified recovery-file/key form |

The file-browser, import/archive, player and reader captures use disposable
original samples in the public demo's **Showcase** workspace. Configuration,
monitoring and recovery captures use a **local synthetic fixture**, labelled
**Demo**. Its data and metrics are illustrative. The phone screenshot uses an
emulated viewport.

## Feature tour

[**current-features.mp4**](current-features.mp4) is **62 seconds**, approximately
**0.94 MiB**, encoded as H.264 at **1536 × 760 / 30 fps**. The original
1536 × 639 app view is preserved above a caption band.
[Chapter timestamps](../showcase.md#watch-the-feature-tour)

The recording uses actual browser screenshots sampled at **2 fps**, assembled
with edited cuts and captions, plus a four-second hold of the actual extraction
screenshot and five seconds of intro/closing stills. It is not continuous real-time
footage; encoding at 30 fps does not add captured motion. No simulated cursor was added.

| Shown | What actually happened |
| --- | --- |
| ZIP creation | Notes.zip was created from disposable public-demo samples; the job completed and the ZIP appeared in the drive. |
| Torrent and extraction dialogs | Their setup/options were opened; neither operation was submitted. |
| Continue and player settings | The sample video was played, paused and reopened; Continue was chosen. Settings show the current built-in menu. |
| Recovery, configuration and monitoring | Local demo screens only. No recovery began, no key was exported and no thumbnail rebuild started. |

## Replace or extend a capture

1. Use disposable, original or redistributable samples. Prefer an isolated
   collection; keep recovery rehearsals and provider tests separate from a UI tour.
2. Show the current feature in a settled, legible state. Preserve the app view;
   restore viewport/appearance changes afterwards. Label local fixtures and
   emulated devices, and distinguish completed actions from dialogs only.
3. Exclude passwords, tokens, recovery keys, phone/account/channel identifiers and
   unrelated files. Capture share options before generating a URL; revoke any
   disposable links afterward. Keep raw frames, logs and session data in ignored staging.
4. Save final images/video here with descriptive names and matching format
   extensions. Disclose cuts, frame sampling, captions and overlays. Inspect
   legibility, cropping and playback before replacing the public assets.
5. Update captions/links in the [gallery](../showcase.md) and README. Update exact
   paths in `PUBLIC_DOCS`; remove obsolete media and references together.

These repository-local assets ship in public source releases. GitHub may open an
MP4 download page instead of playing it inline. Use a GitHub attachment URL only
after uploading the real video through its Markdown editor.
