# Thumbnail generation

[Docs home](README.md) · [Browse files](usage.md#browse-and-organize)

TGDrive creates covers from files staged during upload. The original stays unchanged.

| File | Cover |
| --- | --- |
| JPEG, PNG, WebP, GIF, BMP, TIFF | Scaled image; one frame for animation. |
| MP4/M4V/MOV, MKV/WebM, AVI, WMV/ASF, MPEG | Early frame, or first frame for short videos. |
| PDF | First page. |
| AVIF | Image if the installed decoder supports it. |

## Bandwidth and storage

Grid view loads nearby thumbnails as needed, checking the local generated cover
then Telegram's small thumbnail. It never fetches originals to create missing covers.
Data-saving browsers show icons. Every request checks current owner/share access,
including cache hits and conditional responses.

## Existing files and fallback

New uploads attach the generated cover to the first Telegram part and cache it
locally. Existing files and Telegram-only imports keep available provider covers;
there is no automatic backfill. Unsupported, damaged, encrypted or overly complex
files keep icons. Thumbnail failures never fail the original upload.

<details>
<summary>Cache limits, rendering and container dependencies</summary>

Generated JPEGs have a maximum 320-pixel edge and 64 KiB size. The disposable
`CACHE_DIR/thumbnails` cache holds at most 128 MiB/4,096 entries, evicts older
entries and avoids writes below 64 MiB free disk. It is separate from the media
budget and excluded from metadata backups; attached Telegram covers are the fallback.
Expired/deleted files remain inaccessible even if cached bytes survive.

One renderer runs per process, with a two-second admission wait and 12-second
operation timeout. The Linux container renderer also has CPU/address-space limits;
forced demuxers and file/pipe-only inputs block playlist/network loads.

## Container dependencies

The TGDrive image includes FFmpeg and Poppler's `pdftoppm`; no host tool
installation is needed. Rendering failures fall back to provider covers/icons.
Keep the deployment image updated; resource bounds are not an OS security sandbox.

</details>
