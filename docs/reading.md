# Reading books and comics

[Docs home](README.md) · [All previews](usage.md#open-preview-and-download)

Open a PDF, EPUB, CBZ or CBR from **My Files** or a shared folder. Use the toolbar
to navigate and adjust the display; EPUB/comics also offer **Reading options**.
If a preview fails,
**Download** retrieves the unchanged original for another reader.

## Choose a format

| Format | Controls | How it loads |
| --- | --- | --- |
| PDF, up to 256 MiB | Scroll, zoom, Fit width, Fit page, selectable text | Sections of the file as needed |
| EPUB | Chapters, text size/font, image-page fit, right-to-left | Book details, selected chapter and images |
| CBZ / CBR | Pages, fit, right-to-left | Selected image; solid RAR may also need earlier compressed data |

EPUB and comics remember your place for up to 50 books **in this browser**.
EPUB text preferences are saved here too. Internal book links navigate to their
target chapter or position. Resource filenames appear as readable cover,
illustration or section labels.

For manga or image-only EPUB chapters, choose **Fit width** for readable panels
or **Fit page** for a whole page. **Right to left** reverses arrow-key navigation;
it does not reorder the book. Prose EPUBs use a narrower serif column by default.

Arrow keys navigate while focused. **Enter Fullscreen** is available where the
browser supports it; Escape or browser Back closes the reader.

[PDF, EPUB and comic screenshots](showcase.md#read-and-preview).

## Bandwidth and compatibility

Readers fetch requested sections through TGDrive's authorized cache rather than
automatically preloading the whole book. Small books can fit in one fetched
block; solid RAR and complex PDFs may need more data before displaying a page.
There is no fixed startup-byte or speed guarantee.

Pages keep their original image bytes. TGDrive does not upscale, transcode or
recompress them. Oversized or malformed images show a preview error; the original
remains downloadable. Images load near the visible EPUB page, and offscreen
image data is released while placeholders preserve scroll position.

| File or feature | Compatibility |
| --- | --- |
| Password-protected PDF | Asks for its password; used only in PDF.js memory and discarded when closed |
| EPUB | Passive text, tables and raster images in TGDrive's theme |
| Publisher styling or interactive EPUB | Exact fixed layouts, scripts/styles, remote resources, SVG illustrations and interactive features are unsupported |
| Encrypted/DRM EPUB | Unsupported, including encrypted font metadata |
| Comics | PNG/JPEG/GIF/WebP/AVIF pages when the browser can decode them; password-protected archives are unsupported |

PDF rendering happens in your browser. Fonts, character maps, color profiles and
image decoders come from TGDrive itself. Visible pages render before nearby
pages; shared PDF images are loaded only when a rendered page needs them.

Image progress counts received bytes after server preparation. Compressed HTTP
responses may show no total; the image limit still applies after decompression.
Temporary image failures get up to three attempts, with 2/4-second waits or a
server retry hint up to 30 seconds. Permanent errors stop retries. Leaving the
page cancels that browser's download and retries.

<details>
<summary>Technical limits, caching and server dependencies</summary>

## Reader limits

EPUB chapters with images and at most 80 surrounding text characters use the
illustrated-page layout. More complex publisher layouts can differ.

PDF.js retains a source buffer up to the **256 MiB** file limit. HTTP requests use
at most **1 MiB** ranges with **two** reader requests active. Combined active
response buffers normally stay within **64 MiB**; a larger metadata request runs
alone, bounded by source size. Queued requests hold descriptors, not response
buffers. Success/failure releases claims. Reading more than 64 MiB over time does
not close the document. Canvas/page/text budgets also apply; these limits do not
guarantee a complex PDF will fit every device's memory.

| Bound | Limit |
| --- | --- |
| Index/chapter/thumbnail operation | 32 MiB fetched; aligned 1 MiB reads and a four-block parser cache |
| One selected original raster image | 64 MiB encoded image; up to 96 MiB source reads without loading unrelated ZIP entries |
| Raster dimensions | 16 million pixels and 32,768 pixels per edge, including declared animation frames; checked before bytes leave the parser |
| Archive | 10,000 entries; 2 MiB ZIP directory; stored/deflate ZIP methods |
| EPUB / entries | 5,000 spine entries; 2 MiB/text entry; 16 MiB thumbnail-source image, separate from the 64 MiB reader-page limit |
| Parser work | Two simultaneous operations, 16 pending requests, 45-second parser timeout; 55-second total route deadline including shared-result waits |
| Linux container parser process | 384 MiB address space and 10 CPU-seconds on every supported host |

The server caches extracted pages and parsed chapters/indexes for up to **five
minutes**, bounded to **64 MiB and 16 results**. Concurrent requests for the same
result share one extraction. Eviction/restart requires parsing again; normal
source-range cache data can still be reused. Private browser entries are
revalidated; **304** reuses unchanged bytes. Every request rechecks current access
and source identity, including cache hits and 304s. Revocation stops later reads
through that link; it cannot recall delivered bytes or revoke independent links.

Raster bytes cross the parser boundary as bounded binary output without base64
expansion. Header checks do not decode or resize images. JPEG dimensions must
appear within the first 1 MiB; excessive/malformed headers are rejected. The
pixel limit is about 61 MiB for one RGBA frame, with decoder, GPU and animation
memory additional. Unsafe/duplicate archive names and extreme compression ratios
fail. Solid CBR may exhaust its read budget before a page; busy readers receive
retry guidance. Budgets apply per operation and allow cached-data reuse.

## Running the reader

Docker includes CBR's native libarchive runtime; no host installation is needed.
The bounded parser is not an OS security sandbox: keep the deployment image
updated. If it cannot parse a book, download the original for a dedicated reader.

</details>
