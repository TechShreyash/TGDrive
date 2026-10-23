# Reading books and comics

[Docs home](README.md) · [All previews](usage.md#open-preview-and-download)

Open a supported file from **My Files** or a shared folder. **Download** keeps the
original available if previewing fails.

## Choose a format

| Format | Controls | Fetching |
| --- | --- | --- |
| PDF, up to 256 MiB | Scroll, zoom, fit, selectable text | Visible-page ranges, up to 64 MiB per opening. |
| EPUB | Chapters, text size | Metadata, selected chapter and raster images. |
| CBZ / CBR | Pages, fit, right-to-left | Selected image; solid RAR may also need earlier compressed entries. |

EPUB/comics remember your place in this browser for up to 50 books. Use
**Reading options** for text size or comic fit/direction, and **Enter Fullscreen**
where supported. Arrow keys navigate while focused; right-to-left mode reverses
them. Escape or browser Back closes the reader.

<details>
<summary>Reader screenshot — placeholder</summary>

<!-- MEDIA: Replace assets/media-preview.svg with a screenshot of a PDF, EPUB or comic reader with its controls visible. -->
![Placeholder for a screenshot of the book and comic reader](assets/media-preview.svg)

</details>

## Bandwidth and compatibility

Readers use the authorized range cache without automatically preloading the whole
book. Small files may fit in one fetched block; solid RAR can require earlier
entries. Byte savings vary.

EPUB supports passive text, tables and raster images in TGDrive's theme.
Publisher scripts/styles, remote resources, fixed layouts, SVG illustrations,
interactive features and DRM/encryption (including encrypted font metadata) are
unsupported. Comics accept PNG/JPEG/GIF/WebP/AVIF when the browser can decode them;
password-protected archives are unsupported.

<details>
<summary>Reader limits and container dependencies</summary>

## Reader limits

| Bound | Limit |
| --- | --- |
| One index/chapter/image operation | 32 MiB fetched; aligned 1 MiB reads and a four-block parser cache |
| Archive | 10,000 entries; 2 MiB ZIP directory; stored/deflate ZIP methods |
| EPUB / entries | 5,000 spine entries; 2 MiB/text entry; 16 MiB/image |
| Parser work | Two simultaneous operations, bounded queue, 45-second timeout |
| Linux container parser process | 384 MiB address space, 10 CPU-seconds on every supported host |

Unsafe/duplicate names and extreme compression ratios fail. Busy readers receive
retry guidance. Budgets apply per operation; cached content may be reused.
Authorization is rechecked during reads. Revocation stops later reads through
that link, while independent links remain valid; delivered bytes cannot be recalled.

## Running the reader

The TGDrive image includes CBR's native libarchive runtime; no host library
installation is needed. If CBR parsing fails, download the original for a
dedicated reader. The bounded parser process is not an OS security sandbox;
keep the deployment image updated.

</details>
