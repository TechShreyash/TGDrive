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
Poster [publication](reference.md#terms-used-here) has its own durable retry identity. If a send may have reached
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
already have covers. While it runs, TGDrive limits its own database work so
browsing and search stay responsive, so a large rebuild takes longer.
**Cancel rebuild** stops further work and keeps generated covers. The source-byte
counter includes cache reads, so it is not a network meter.

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

Exact cache sizes, render deadlines and decoder limits are in
[thumbnail rendering internals](architecture.md#thumbnail-rendering-internals).

## Container dependencies

Docker includes FFmpeg and Poppler's `pdftoppm`; no host installation is needed.
Keep the deployment image updated. Resource limits are not an OS security sandbox.
