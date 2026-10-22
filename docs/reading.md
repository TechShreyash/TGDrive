# Reading books and comics

Open a supported file from My Files or a shared folder. **Download** always keeps
the original available. These readers favor opening a chapter or page over
fetching a whole large book before you can inspect it.

## Choose a format

| Format | Reading controls | What is fetched |
| --- | --- | --- |
| PDF, up to 256 MiB | Continuous scrolling, zoom, fit width/page, selectable text | Document metadata and ranges needed by visible pages, subject to a 64 MiB reading budget per opening. |
| EPUB | Chapter menu, previous/next chapter, adjustable text size | ZIP directory, book metadata, selected chapter and its raster images. |
| CBZ | Page menu, previous/next page, fit width/page, right-to-left navigation | ZIP directory and the selected image. |
| CBR | The same comic controls | RAR metadata and selected image; some archives need intervening compressed data too. |

EPUB and comic readers remember the last chapter/page in this browser for up to
50 books. This is not synced between devices. On phones the page/chapter selector
stays visible; **Reading options** contains text size or comic fit/direction.
Use **Enter Fullscreen** in the preview header to read without browser chrome
where the browser permits it. Use arrow keys while focused in the reading area. In right-to-left mode, Left
advances and Right goes back. Escape or browser Back closes the reader.

## Bandwidth and compatibility

Selective reading uses the existing authorized Telegram range cache. It does
not automatically prepare the whole book. Small files can fit inside one fetched
block, so opening a small book may still fetch all of its bytes. CBZ and EPUB
usually permit direct entry reads; solid RAR files may require reading earlier
entries before the chosen page. No reader can promise a fixed byte saving for
every archive. Download the original in a dedicated app when a file cannot be
read within the limits below.

EPUB shows book text, headings, lists, tables and supported images in TGDrive's
reading theme. Publisher scripts, styles, embedded players, forms and remote
resources do not run or load. Fixed-layout books, SVG illustrations, interactive
EPUB features and DRM/encrypted books are not supported. Encrypted font metadata
is also rejected. Comic pages support PNG, JPEG, GIF, WebP and AVIF when the
browser supports their image format. Password-protected archives are unsupported.

## Reader limits

These bounds protect the server from malformed or excessively complex books:

- At most 32 MiB fetched for each index, chapter or image operation; aligned
  1 MiB reads with a four-block parser cache. This is an operation budget, not an
  all-session cap. Revisiting content can reuse the server's normal range cache.
- At most 10,000 archive entries, a 2 MiB ZIP directory, 5,000 EPUB spine entries,
  2 MiB per text entry and 16 MiB per image entry. ZIP methods must be stored or
  deflate. Unsafe names, duplicate names and extreme compression ratios fail.
- At most two parser operations run at once, with a bounded queue. Busy readers
  receive retry guidance. Each operation has a 45-second wall-clock timeout;
  on Linux its separate parser process also has a 384 MiB address-space ceiling
  and 10 CPU-second limit. Windows does not provide those Unix resource limits.

Authorization is checked when opening and during source reads. Expiring or
revoking one link stops later reads through that link; already delivered content
cannot be withdrawn. Other independent links to the same item remain valid.

## Running the reader

The official Docker image includes the native libarchive library used for CBR.
For a source installation, install your operating system's libarchive runtime
before starting TGDrive (for example `libarchive13t64` on Debian Trixie/Ubuntu
systems that provide that package). Other platforms may use a differently named
package. The pinned Python dependencies are installed by `uv sync --frozen`.
If libarchive is unavailable, EPUB and CBZ still work; CBR shows download guidance.
The parser is a separate resource-bounded process, not an operating-system
security sandbox. Keep the application and native packages updated.
