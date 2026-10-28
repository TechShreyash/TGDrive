# Thumbnails and video posters

[Docs home](README.md) · [Browse files](usage.md#browse-and-organize)

TGDrive creates covers during upload without changing originals. A file-type icon
usually means no usable cover was available; it does not prove the file is damaged.
For one older file, try [cached-cover repair](#existing-files-and-fallback).
For a library, use [background rebuilding](#rebuild-existing-thumbnails).

| File | Cover |
| --- | --- |
| JPEG, PNG, WebP, GIF, BMP, TIFF | Scaled image; one frame for animation |
| MP4/M4V/MOV, MKV/WebM, AVI, WMV/ASF, MPEG | Frame between 40% and 60% of the duration, with first-frame fallback |
| PDF | First page |
| EPUB / CBZ / CBR | Declared EPUB raster cover or first image; first naturally ordered comic page |
| MP3, FLAC, M4A, Ogg/Opus, WAV, AAC | Embedded artwork when available to the decoder; otherwise an icon |
| DOCX/PPTX/XLSX, ODT/ODP/ODS | Embedded raster preview when present |
| AVIF | Image when the installed decoder supports it |

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
Poster publication has its own durable retry identity. If a send may have reached
Telegram, the job keeps its journal and staged data for retry or cancellation
instead of forgetting a possibly published image. Accepted server uploads can
finish with the browser closed; on-demand preview decoding needs the viewer open.

## Bandwidth and storage

Grid view loads nearby covers: generated local JPEG first, Telegram thumbnail
second, then an icon. It **never downloads an original from Telegram just to
make a grid cover**. A complete original already cached on the server may support
local repair.

Loaded covers stay on the current grid page (up to 100 items). Unfinished
requests cancel offscreen and retry when visible. Changing views, folders,
file details or access can request them again. Browsers revalidate saved covers;
a **304** response reuses the image bytes. Data-saving browsers show icons.
Access is checked on every request, including cache hits and 304s.

An optional missing cover returns **204 No Content**, leaving the format icon.
Missing artwork, unsupported decoders and rendering limits also leave icons;
thumbnail failure never fails an original upload.

## Rebuild existing thumbnails

1. Open **Tools & settings → Cache settings → Regenerate all thumbnails**.
2. Review the bandwidth/runtime warning, then choose **Start background rebuild**.
3. Watch checked, regenerated, skipped and failed counts, the current file and
   recent issues on that page. You can close the browser and return later.

**Rebuilding can read substantial data from Telegram.** It includes files that
already have covers. **Cancel rebuild** stops further work and keeps generated
covers. The source-byte counter includes cache reads, so it is not a network meter.

[See the rebuild control](assets/thumbnail-rebuild.jpg). This capture shows its
entry point, not a completed full-library rebuild.

The worker handles one file at a time and waits while transfers or media requests
are active. Reads use the lower-priority queue, paced to 2 MiB/s before block/cache
overhead. Already running requests cannot be preempted; rebuilding still shares
server CPU, disk and network.

Successful covers replace local cached covers; originals, catalog records and
Telegram messages stay unchanged. Failures keep old covers. Changed/removed files
are skipped; later uploads use normal automatic generation. Cache eviction can
remove earlier results from a large rebuild.

Progress is saved locally between files and during reads when possible. After
a server restart, the run is **interrupted**: start another rebuild to refresh
covers. It does not restart a bandwidth-heavy run silently. Demo visitors cannot
start or cancel rebuilding.

## Existing files and fallback

New uploads attach the generated cover to their first Telegram part and cache
it locally. To repair one older file with no provider cover:

1. Choose **Preload file** and wait for full **Ready** coverage in **Preloads**.
2. Return to its folder in Grid view.

Repair needs a complete cached original no larger than **128 MiB**. It creates a
disposable local JPEG, without changing the catalog or uploading a Telegram cover.
Partial, cold, larger, unsupported, damaged, encrypted or overly complex sources
keep their provider cover/icon. A container such as MOV still needs a supported
codec and decoding cost; a missing cover does not establish playback failure.

<details>
<summary>Technical budgets, rendering and dependencies</summary>

Generated JPEGs have a maximum **320-pixel edge and 64 KiB size**. The separate
`CACHE_DIR/thumbnails` cache holds at most **128 MiB / 4,096 entries**, evicts older
entries and avoids writes below **64 MiB free disk**. It is excluded from metadata
backups; attached Telegram covers remain the fallback. Cached bytes do not grant
access to expired/deleted files.

Video posters use a separate `CACHE_DIR/posters` cache with the same
**128 MiB / 4,096-entry** bounds and free-space floor. A bounded RAM cache
coalesces poster requests; each response rechecks current file/share access and
verifies the saved image's size and SHA-256. A fresh player waits at most ten
seconds for its optional poster lookup before trying the representative frame.
Poster publication follows the original parts, with a **15-second attempt
window**. Transfer byte progress counts the original file, so it can remain at
100% while poster publication and metadata protection finish. A send that may
have reached Telegram retains its retry journal instead of being silently skipped.
Generating both sizes normally decodes the source video once, then derives the
small thumbnail from that picture; unsuccessful poster generation can fall back
to the existing small-cover renderer within its operation deadline.

**Warm-cache repair:** one file at a time, with an **eight-second** copy/render
deadline. Verified blocks are copied to a locked private temporary file rather
than joined in RAM. Complete coverage and multipart checksums are checked; free
space must cover the source plus the larger of 64 MiB or the configured cache
free-space floor. Startup/later repairs remove unlocked crash leftovers. Eviction,
cache clearing, revocation or cancellation aborts safely and removes the source.
Failed rendering is reconsidered when new cache data arrives for that file;
metadata/cache hits alone do not repeat it. Temporary admission/space/copy
failures may retry sooner. Ordinary missing-provider results are briefly cached.

**Manual rebuild reads:** video/images use a private loopback FFmpeg range bridge
through the shared cache. Books/office files read their index and cover. Most
sources have a **64 MiB cumulative read budget and four-minute deadline**;
books retain their **32 MiB** parser budget. PDFs need seekable local bytes and
may read the complete original up to **256 MiB**, with free-space checks and
crash-cleaned private staging. Larger or overly demanding sources are skipped.
Manual rendering has its own lower-OS-priority slot and a **120-second** native
deadline for paced reads; only its token-protected loopback source is allowed.

**Automatic renderer:** one slot per process, **two-second** admission wait,
**12-second** operation timeout. Video metadata probing allows **1.5 seconds**.
The first frame attempt seeks randomly between 40% and 60% of duration, or ten
seconds in if duration is unknown, with a **six-second** timeout; first-frame
fallback allows **three seconds**. Partial JPEGs are rejected; a nonblack frame
is not guaranteed. Admission/decoder failures retain the cover/icon fallback.

Large JPEGs above **8 MiB** use decoder downsampling. Book/office covers use the
bounded archive parser (**32 MiB** reads, **16 MiB** per image), reject remote
resources/archive-path extraction and force recognized raster bytes through the
image decoder. Existing covers change only on new upload, explicit rebuild or
eligible repair.

The Linux renderer allows **eight CPU-seconds, 768 MiB virtual address space,
one decoding/filter thread and 64 MiB per allocation**. Address space includes
libraries, thread stacks and decoder frames; it is not reserved RAM. Tested UHD
HEVC MOVs fit, but every codec/resolution or low-memory container is not guaranteed.
Forced demuxers and file/pipe-only local inputs block playlist/network loads.

## Container dependencies

Docker includes FFmpeg and Poppler's `pdftoppm`; no host installation is needed.
Keep the deployment image updated. Resource limits are not an OS security sandbox.

</details>
