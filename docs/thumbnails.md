# Thumbnails and video posters

[Docs home](README.md) · [User guide](usage.md) · [Browse files](usage.md#browse-and-organize)

TGDrive creates covers during upload without changing originals. A file-type icon
usually means no usable cover was available; it does not prove the file is damaged.
For one older file, try [cached-cover repair](#existing-files-and-fallback).
For a library, use [background rebuilding](#rebuild-existing-thumbnails).

**Contents**

- [Video posters](#video-posters)
- [Bandwidth and storage](#bandwidth-and-storage)
- [Rebuild existing thumbnails](#rebuild-existing-thumbnails)
- [Existing files and fallback](#existing-files-and-fallback)
- [Container dependencies](#container-dependencies)

<details>
<summary>Supported cover sources by file type</summary>

| File | Cover |
| --- | --- |
| JPEG, PNG, WebP, GIF, BMP, TIFF | Scaled image; one frame for animation |
| MP4/M4V/MOV, MKV/WebM, AVI, WMV/ASF, MPEG | Frame between 40% and 60% of the duration, with first-frame fallback |
| PDF | First page |
| EPUB / CBZ / CBR | Declared EPUB raster cover or first image; first naturally ordered comic page |
| MP3, FLAC, M4A, Ogg/Opus, WAV, AAC | Embedded artwork when available to the decoder; otherwise an icon |
| DOCX/PPTX/XLSX, ODT/ODP/ODS | Embedded raster preview when present |
| AVIF | Image when the installed decoder supports it |

</details>

## Video posters

Supported videos staged by TGDrive for device, URL or torrent uploads generate
two JPEGs from the same selected picture: the small grid thumbnail and a poster
up to **1920 × 1080**, preserving aspect ratio without enlarging smaller videos.
The poster is limited to **1 MiB**. It is stored as a separate ordinary Telegram
document, so MKVs and other document uploads do not depend on Telegram's native
video-cover support. The original video bytes stay unchanged.

The file's catalog links its video parts and poster. Posters follow files through
renames, moves, Trash and restoration. Permanent deletion removes their catalog
references under the [same Telegram-retention policy](usage.md#why-permanent-deletion-keeps-the-telegram-files)
as originals. Metadata backups retain the reference, and an independent channel
backup copies and remaps the poster with the video. Local poster cache loss only
requires fetching the saved JPEG again, not decoding the movie again.
Restore poster-aware backups with a TGDrive version that supports posters;
older versions can omit or reject these optional references.

The player opens paused with the stored poster. Missing/unusable posters fall
back to a frame halfway through the video, keeping the displayed time at zero;
**Play** starts from the beginning and **Continue from…** keeps its saved meaning.
Seeking and decoding can take time, and the selected picture can still be black.
Files already on Telegram, including direct bot intake, do not automatically get
a durable poster. Thumbnail repair/rebuilding still refreshes small covers only;
there is no automatic whole-library poster rebuild.

Generation and pre-dispatch poster failures retain the normal video upload.
Poster [publication](reference.md#terms-used-here) has its own durable retry identity. If a send may have reached
Telegram, the job keeps its journal and staged data for retry or cancellation
instead of forgetting a possibly published image. Accepted server uploads can
finish with the browser closed; on-demand preview decoding needs the viewer open.

## Bandwidth and storage

List and Grid views lazily load nearby covers. A missing local cover can be generated from the
file's verified saved poster, fetching only that JPEG when needed. Older local
or Telegram-provided covers remain fallbacks, followed by cached-original repair
or an icon. Neither view **downloads an original from Telegram just to make a
cover**.

Loaded covers stay in the current List or Grid layout (up to 100 items per
page). Unfinished requests cancel offscreen and retry when visible. Changing views, folders,
file details or access can request them again. Browsers revalidate saved covers;
a **304** response reuses the image bytes. Data-saving browsers show icons.
Access is checked on every request, including cache hits and 304s.

An optional missing cover returns **204 No Content**, leaving the format icon.
Missing artwork, unsupported decoders and rendering limits also leave icons;
thumbnail failure never fails an original upload.

## Rebuild existing thumbnails

1. Open **Configuration → Cache settings → Regenerate all thumbnails**.
2. Review the bandwidth/runtime warning, then choose **Start background rebuild**.
3. Watch checked, regenerated, skipped and failed counts, the current file and
   recent issues on that page. You can close the browser and return later.

**Rebuilding can read substantial data from Telegram.** It includes files that
already have covers. Selected database rechecks are paced to reduce competition
with browsing; this does not reserve database capacity or guarantee response time.
**Cancel rebuild** stops further work and keeps generated covers. The source-byte
counter includes cache reads, so it is not a network meter.

[See the older rebuild control](assets/thumbnail-rebuild.jpg). This capture
predates the current navigation and does not show a completed full-library rebuild.

For videos with a usable saved poster, rebuilding first tries to derive the
small cover from that JPEG; it falls back to the bounded original-read path if
needed. It does not create missing durable posters.

The worker handles one file at a time and waits while transfers or media requests
are active. If foreground work is encountered before fetching another source
block, it cleans up the current renderer and temporary source, waits, then retries
the same file. This policy wait does not increment checked or skipped counts;
you can still cancel it. Already running requests cannot be preempted, and
rebuilding still shares server CPU, disk and network.

Per-file limits remain **64 MiB of admitted source reads** (**256 MiB for PDFs**)
and **960 seconds across attempts**, excluding deliberate foreground waits.
Native rendering guards also still apply.

<details>
<summary>Read budgets, rendering time and progress counters</summary>

Reads use the lower-priority queue, paced to 2 MiB/s before block/cache overhead.
Each block missing from the rebuild source's small hot-block cache is
charged before fetching, including interrupted attempts; retries do not refund
that charge. Returned blocks can come from shared cache, not just Telegram.
A poster attempt is charged its declared size before loading. The displayed
source-byte counter counts fully returned blocks and loaded poster bytes, so
it can be below an exhausted admission budget.

The cumulative attempt budget covers renderer-slot waiting, source reads,
pacing, rendering and cleanup across retries. The initial
foreground wait and deliberate foreground waits after cleanup are excluded.
Background renderer-slot admission waits cancellably within that budget;
foreground preview and cached-cover repair retain their two-second admission
wait. Other native rendering guards still apply. A time-budget exhaustion is
reported explicitly; waiting or retrying does not guarantee a cover.

Retries retain the same source identity, hot blocks and accumulated admission
charges. Source-limit failures appear in recent issues without exposing raw
renderer diagnostics or source URLs.

</details>

Successful covers replace local cached covers; originals, catalog records and
Telegram messages stay unchanged. Failures keep old covers. Changed/removed files
are skipped; later uploads use normal automatic generation. Cache eviction can
remove earlier results from a large rebuild.

Progress is saved locally between files and during reads when possible. After
a server restart, the run is **interrupted**: start another rebuild to refresh
covers. It does not restart a bandwidth-heavy run silently. Demo visitors cannot
start or cancel rebuilding.

## Existing files and fallback

New encrypted uploads cache generated covers locally and do not attach plaintext
Telegram thumbnails. Legacy plaintext uploads can have an attached provider
cover. For a missing cover:

1. Return to the folder in List or Grid view. If the file has a usable saved poster,
   TGDrive can generate a small cover from it without preloading the original.
2. If no usable poster or provider cover is available, choose **Preload file**
   and wait for **Fully cached** with full coverage on the original's row in
   **Preloads**, then reopen the folder.

Cached-original repair needs a complete original no larger than **128 MiB**. It
creates a disposable local JPEG, without changing the catalog or uploading a
Telegram cover.
Partial, cold, larger, unsupported, damaged or overly complex sources
keep their provider cover/icon. A container such as MOV still needs a supported
codec and decoding cost; a missing cover does not establish playback failure.

Exact cache sizes, render deadlines and decoder limits are in
[thumbnail rendering internals](architecture.md#thumbnail-rendering-internals).

## Container dependencies

Docker includes FFmpeg and Poppler's `pdftoppm`; no host installation is needed.
Keep the deployment image updated. Resource limits are not an OS security sandbox.
