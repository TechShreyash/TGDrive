# Thumbnail generation

TGDrive generates covers from supported files already staged on the server:

| File | Cover |
| --- | --- |
| JPEG, PNG, WebP, GIF, BMP, TIFF | A scaled image; animated formats use a frame. |
| MP4/M4V/MOV, MKV/WebM, AVI, WMV/ASF, MPEG | An early video frame, falling back to the first frame for short videos. |
| PDF | The first page. |
| AVIF | An image when the installed decoder supports it. |

The original is never transcoded or replaced. Each derivative is a JPEG with a
long edge of at most 320 pixels and a maximum size of 64 KiB. The first upload
part carries the generated thumbnail even when TGDrive splits the original.
TGDrive also saves the generated JPEG locally, so the grid can show it even when
Telegram does not return a provider thumbnail. Telegram remains the storage and
recovery fallback for the attached cover.

## Bandwidth and storage

Grid browsing loads thumbnails only for visible or nearby tiles. It first checks
TGDrive's generated-cover cache, then a small advertised Telegram thumbnail.
It never downloads the original or assembles multipart files to generate a cover
just because you opened Grid view. Browsers reporting data-saving mode use icons.
Every request checks the current owner session or share permission, including
cache hits and conditional responses.

Generated covers use a disposable `CACHE_DIR/thumbnails` cache capped at 128 MiB
and 4,096 entries, separate from the media cache budget. It avoids new writes when
less than 64 MiB of disk space is free. It is not part of a metadata backup;
Telegram's attached cover is the fallback after cache loss. Expired or deleted
files cannot be viewed through the thumbnail endpoint even if cache bytes remain.
Old cache entries are evicted as new covers arrive.

## Existing files and fallback

Existing uploads keep their available Telegram thumbnails. They are not silently
downloaded again to backfill missing covers. New uploads through TGDrive generate
covers from the local staged source. Files arriving through a Telegram-only import
may still use provider thumbnails because TGDrive has no staged source to render.

Thumbnail creation is optional: damaged, encrypted, unsupported or excessively
large/complex media keeps its file-type icon. An unavailable decoder, a busy
renderer or a thumbnail transport failure must not fail the original upload.
One renderer runs per process, with a two-second admission wait and a 12-second
operation timeout. On Unix, its native processes also inherit CPU/address-space
limits; forced demuxers and file/pipe-only inputs prevent playlist/network loads.

## Source installations

The Docker image includes **FFmpeg** and **Poppler's pdftoppm**. For source installs,
install `ffmpeg` and `poppler-utils` (or your platform's equivalent) on the runtime
PATH. If a tool is missing, that format keeps its provider cover or file-type icon.
Keep these native packages updated. Rendering is isolated in a resource-bounded
process; it is not an operating-system security sandbox.
