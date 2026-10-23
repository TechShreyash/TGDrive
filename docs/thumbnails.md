# Thumbnail generation

[Docs home](README.md) · [Browse files](usage.md#browse-and-organize)

TGDrive creates covers from files staged during upload. The original stays unchanged.

| File | Cover |
| --- | --- |
| JPEG, PNG, WebP, GIF, BMP, TIFF | Scaled image; one frame for animation. |
| MP4/M4V/MOV, MKV/WebM, AVI, WMV/ASF, MPEG | Early frame, falling back to the first frame when the seek fails or times out. |
| PDF | First page. |
| AVIF | Image if the installed decoder supports it. |

## Bandwidth and storage

Grid view loads nearby thumbnails as needed, checking the local generated cover
then Telegram's small thumbnail. A missing cover can also be repaired from a
complete original already in the server cache. The grid never downloads original
payloads from Telegram to create a cover.
Data-saving browsers show icons. Every request checks current owner/share access,
including cache hits and conditional responses.

## Existing files and fallback

New uploads attach the generated cover to the first Telegram part and cache it
locally. For an existing file with no provider cover, TGDrive can create a local
cover when its complete original is cached and no larger than **128 MiB**.
Choose **Preload file**, wait for full **Ready** coverage in **Preloads**, then
return to its folder in Grid view. Repair does not upload a new Telegram cover
or change the catalog; the disposable local JPEG can be reused afterward.

Cold, partial, larger, unsupported, damaged, encrypted or overly complex files
keep provider covers/icons. Thumbnail failures never fail the original upload.
MOV is a container: its codec, resolution and decoding cost still need to fit
the installed decoder and rendering limits. A missing cover does not establish
that the original file is damaged or unplayable.

<details>
<summary>Cache limits, rendering and container dependencies</summary>

Generated JPEGs have a maximum 320-pixel edge and 64 KiB size. The disposable
`CACHE_DIR/thumbnails` cache holds at most 128 MiB/4,096 entries, evicts older
entries and avoids writes below 64 MiB free disk. It is separate from the media
budget and excluded from metadata backups; attached Telegram covers are the fallback.
Expired/deleted files remain inaccessible even if cached bytes survive.

Warm-cache repair runs one file at a time within an eight-second copy/render
deadline. It writes verified blocks to a private temporary file instead of joining
the source in RAM, checks complete coverage and multipart checksums, and requires
the source size plus the larger of 64 MiB or the configured cache free-space floor.
Owned temporary sources stay locked through decoding; startup and later repairs
remove unlocked crash leftovers without deleting active sources. Eviction, clearing,
revocation or cancellation aborts safely and removes the temporary source.
Negative thumbnail results are reconsidered when that file receives new cached
data; metadata probes and cache hits do not trigger repeated rendering of a
failed source. Temporary renderer/space/copy admission failures can retry without
new source data. Ordinary missing-provider results remain briefly cached.

One renderer runs per process, with a two-second admission wait and 12-second
operation timeout. Video extraction tries the one-second frame for up to eight
seconds, then the first frame for up to three seconds after any unsuccessful
attempt; partial JPEG output is rejected. Busy admission or exhausted rendering
limits keep the provider-cover/icon fallback rather than delaying the upload
indefinitely. The Linux renderer has an eight-second CPU limit and a 768 MiB
virtual-address-space ceiling (including decoder reference frames, libraries and
thread stacks, not a reserved amount of RAM), with one decoding/filter thread
and a 64 MiB limit per allocation. These bounds accommodate tested UHD HEVC
MOVs but do not guarantee every codec/resolution or low-memory container;
forced demuxers and file/pipe-only inputs block playlist/network loads.

## Container dependencies

The TGDrive image includes FFmpeg and Poppler's `pdftoppm`; no host tool
installation is needed. Rendering failures fall back to provider covers/icons.
Keep the deployment image updated; resource bounds are not an OS security sandbox.

</details>
