# Reading PDFs

[Docs home](README.md) · [User guide](usage.md) · [Previews](usage.md#open-preview-and-download)

Open a PDF from **My Files** or a shared folder. Enter a page number and press
Enter to jump to it. Use the zoom controls for **Fit width**, **Fit page** or a
specific zoom level. On phones, the magnifier opens these controls. Use arrow
keys while the page controls have focus, or **Enter Fullscreen** for more space.

Password-protected PDFs ask for their password in the viewer. The browser
keeps it in memory until the preview closes. **Download Original** retrieves
the unchanged file.

[PDF screenshot](showcase.md#read-and-preview)

## Resume a PDF

PDFs return to the saved page after the document and its page count are ready.
Your own page selection or scrolling takes precedence over a late saved-position
read. If the saved count differs from the opened PDF, a warning explains that
reading starts at page 1; navigate to the page you want instead. The existing
**10,000-page** and **256 MiB** PDF limits still apply.

Owners and independently authenticated demo administrators save PDF pages in
MongoDB for use across devices. Anonymous share and public-demo positions stay
in separate browser-local storage; a share token cannot read owner progress.
Only the page is restored, not zoom or layout. Failed saves do not block reading
or create a local owner fallback. Reopening in the same tab waits for a pending
close save before reading the saved page. Closing saves are best effort when
leaving the site. Navigate to choose a new saved page if old progress is
unavailable.

## Bandwidth and compatibility

The browser requests sections of the PDF as needed. Visible pages render first,
with one page behind and nearby pages ahead in a five-page render window.
Fonts, character maps, color profiles and image decoders come from TGDrive.
See [PDF preview internals](architecture.md#pdf-and-zip-preview-internals)
for request and memory limits.
