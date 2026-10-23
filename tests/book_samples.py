"""Self-generated book fixtures; no third-party pages or copyrighted media."""

import io
import struct
import zlib
from zipfile import ZIP_STORED, ZipFile


def png(page=1):
    width, height = 480, 720

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    rows = []
    for y in range(height):
        color = (220, 235, 245) if page % 2 else (237, 226, 215)
        if 70 < y < 280 or 340 < y < 630:
            color = (65 + page * 10, 90, 110)
        rows.append(b"\x00" + bytes(color) * width)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def epub(*, padding=0, unsafe=False):
    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_STORED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            (
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:co'
                'ntainer"><rootfiles><rootfile '
                'full-path="OPS/book.opf"/></rootfiles></container>'
            ),
        )
        archive.writestr(
            "OPS/book.opf",
            (
                '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>The '
                "little journey</dc:title></metadata><manifest><item "
                'id="one" href="one.xhtml" '
                'media-type="application/xhtml+xml"/><item id="two" '
                'href="two.xhtml" media-type="application/xhtml+xml"/><item'
                ' id="nav" href="nav.xhtml" '
                'media-type="application/xhtml+xml" '
                'properties="nav"/></manifest><spine><itemref '
                'idref="one"/><itemref idref="two"/></spine></package>'
            ),
        )
        archive.writestr(
            "OPS/nav.xhtml",
            (
                '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><a '
                'href="one.xhtml">A quiet morning</a><a '
                'href="two.xhtml">Across the city</a></nav></body></html>'
            ),
        )
        malicious = (
            (
                "<script>window.bookExecuted=true</script><img "
                'src="https://external.invalid/track.png"/><iframe '
                'src="https://external.invalid/"></iframe><a '
                'href="javascript:alert(1)">Bad link</a><form><input '
                'name="secret"/></form>'
            )
            if unsafe
            else ""
        )
        archive.writestr(
            "OPS/one.xhtml",
            (
                '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>A '
                "quiet morning</h1><p>The station was quiet when Mira "
                "arrived. Her journey began with a map, a notebook and time"
                " to look around.</p><p>This is a local sample book. "
                "Chapters and images load only when needed.</p><a "
                'href="two.xhtml#arrival">Continue to the next chapter</a>'
            )
            + malicious
            + "</body></html>",
        )
        archive.writestr(
            "OPS/two.xhtml",
            (
                '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1 '
                'id="arrival">Across the city</h1><p>Beyond the station, '
                "the city was waking up. Mira followed the river until the "
                'streets became familiar.</p><img src="images/page.png" '
                'alt="Illustrative sample page"/></body></html>'
            ),
        )
        archive.writestr("OPS/images/page.png", png())
        if padding:
            archive.writestr("unused/padding.bin", b"x" * padding)
    return out.getvalue()


def cbz(*, padding=0):
    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_STORED) as archive:
        for page in (10, 2, 1):
            archive.writestr(f"pages/{page}.png", png(page))
        if padding:
            archive.writestr("unused/padding.bin", b"x" * padding)
    return out.getvalue()


def cbr():
    def header(kind, flags, fields=b""):
        body = struct.pack("<BHH", kind, flags, 7 + len(fields)) + fields
        return struct.pack("<H", zlib.crc32(body) & 0xFFFF) + body

    output = bytearray(b"Rar!\x1a\x07\x00")
    output += header(0x73, 0, b"\x00" * 6)
    for page in (2, 1):
        name, data = f"{page}.png".encode(), png(page)
        fields = (
            struct.pack(
                "<LLBLLBBHL",
                len(data),
                len(data),
                3,
                zlib.crc32(data),
                0,
                20,
                0x30,
                len(name),
                0x81A4,
            )
            + name
        )
        output += header(0x74, 0x8000, fields) + data
    output += header(0x7B, 0)
    return bytes(output)


def manga_epub():
    """A two-page image-based EPUB using the same generated panels as CBZ."""
    output = io.BytesIO()
    with (
        ZipFile(io.BytesIO(epub())) as original,
        ZipFile(output, "w", ZIP_STORED) as archive,
    ):
        for info in original.infolist():
            content = original.read(info.filename)
            if info.filename in {"OPS/one.xhtml", "OPS/two.xhtml"}:
                content = (
                    b'<html xmlns="http://www.w3.org/1999/xhtml"><body><div><img '
                    b'src="images/page.png" alt="Manga page"/></div></body></html>'
                )
            archive.writestr(info.filename, content)
    return output.getvalue()
