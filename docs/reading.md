# Reading books and comics

[Docs home](README.md) · [User guide](usage.md) · [Previews](usage.md#open-preview-and-download)

Open a PDF, EPUB, CBZ or CBR from **My Files** or a shared folder. Use the toolbar
to navigate and adjust the display; EPUB/comics also offer **Reading options**.
If a preview fails,
**Download** retrieves the unchanged original for another reader.

## Choose a format

| Format | Controls | How it loads |
| --- | --- | --- |
| PDF, up to 256 MiB | Page box, zoom menu (Fit width, Fit page, 50–200%), selectable text | Sections of the file as needed |
| EPUB | Page box, chapter list, Reading options (text size, font, page fit, direction) | Book details, selected chapter and images |
| CBZ / CBR | Page box, Reading options (page fit, direction) | Selected image; solid RAR may also need earlier compressed data |

EPUB and comics remember your place for up to 50 books **in this browser**.
EPUB text preferences are saved here too. Internal book links navigate to their
target chapter or position. Resource filenames appear as readable cover,
illustration or section labels.

To jump to a page, type its number in the page box and press Enter; numbers out
of range go to the first or last page. PDFs open at **Fit width**, capped at a
960 px page width, so there is no sideways scrolling until you zoom in. Comic pages
use the same cap. On narrow windows and phones, the magnifier button opens the
zoom controls.

For manga or image-only EPUB chapters, open **Reading options** and choose
**Fit width** for readable panels or **Fit page** for a whole page. **Right to
left** reverses arrow-key navigation; it does not reorder the book. Prose EPUBs
use a narrower serif column by default.

Arrow keys navigate while focused. **Enter Fullscreen** is available where the
browser supports it; Escape or browser Back closes the reader.

[PDF, EPUB and comic screenshots](showcase.md#read-and-preview).

## Bandwidth and compatibility

Readers fetch requested sections through TGDrive's authorized cache rather than
automatically preloading the whole book. After the selected comic page is usable,
the reader fetches **one next page**; EPUBs similarly prepare one next chapter
after the current chapter loads. Turning forward can reuse that result.
Jumping elsewhere cancels the read-ahead; closing the reader
drops it. PDFs keep one page behind the visible one and use the rest of their
five-page render window ahead. Small books can fit in one fetched block; solid
RAR and complex PDFs may need more data before displaying a page.
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

## Reader limits

Very large or complex books may not open on every device. If a book won't open,
download the original for a dedicated reader. Docker includes the CBR runtime,
so no host installation is needed. Exact transfer, parser and cache bounds are
in [book reader internals](architecture.md#book-reader-internals).
