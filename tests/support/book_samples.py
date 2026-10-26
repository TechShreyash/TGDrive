"""Self-generated book fixtures; no third-party pages or copyrighted media."""

import hashlib
import io
import struct
import zlib
from zipfile import ZIP_STORED, ZipFile

PDF_SAMPLE_PASSWORD = "fixture-pdf-unlock"


def _pdf_objects(objects, *, trailer=b""):
    """Serialize small authored PDF fixtures with exact byte offsets."""
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(output)
    count = len(objects) + 1
    output += f"xref\n0 {count}\n0000000000 65535 f \n".encode()
    output += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    output += (
        f"trailer\n<< /Size {count} /Root 1 0 R ".encode()
        + trailer
        + f">>\nstartxref\n{start}\n%%EOF".encode()
    )
    return bytes(output)


def _pdf_stream(content):
    return f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"


def encrypted_pdf(password=PDF_SAMPLE_PASSWORD):
    """Deterministic PDF Standard Security R2 fixture, never application crypto."""
    padding = bytes.fromhex(
        "28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a"
    )

    def padded(value):
        return (value.encode("latin-1") + padding)[:32]

    def md5(value):
        # MD5 and RC4 are required by this deliberately old PDF test format.
        return hashlib.md5(value, usedforsecurity=False).digest()

    def rc4(key, value):
        state = list(range(256))
        position = 0
        for index in range(256):
            position = (position + state[index] + key[index % len(key)]) % 256
            state[index], state[position] = state[position], state[index]
        first = second = 0
        output = bytearray()
        for byte in value:
            first = (first + 1) % 256
            second = (second + state[first]) % 256
            state[first], state[second] = state[second], state[first]
            output.append(byte ^ state[(state[first] + state[second]) % 256])
        return bytes(output)

    identity = hashlib.sha256(b"TGDrive encrypted PDF fixture").digest()[:16]
    owner = rc4(md5(padded("fixture-owner-password"))[:5], padded(password))
    permissions = -4
    key = md5(padded(password) + owner + struct.pack("<i", permissions) + identity)[:5]
    user = rc4(key, padding)
    content = b"BT /F1 22 Tf 24 540 Td (Encrypted PDF fixture opened) Tj ET"
    stream_key = md5(key + (5).to_bytes(3, "little") + b"\x00\x00")[:10]
    return _pdf_objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 420 594] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            _pdf_stream(rc4(stream_key, content)),
            b"<< /Filter /Standard /V 1 /R 2 /Length 40 /P -4 /O <"
            + owner.hex().encode()
            + b"> /U <"
            + user.hex().encode()
            + b"> >>",
        ],
        trailer=b"/Encrypt 6 0 R /ID [<"
        + identity.hex().encode()
        + b"><"
        + identity.hex().encode()
        + b">] ",
    )


def resource_pdf(font_data):
    """Exercise local fonts, a packed CMap, and CMYK ICC/WASM conversion."""
    # The named Shift-JIS CMap maps ASCII A to CID 264. Embed the shipped font
    # and its actual glyph index so a system substitution cannot mask a broken
    # resource request with a different visible character.
    cmap_offset = None
    for index in range(struct.unpack_from(">H", font_data, 4)[0]):
        offset = 12 + index * 16
        if font_data[offset : offset + 4] == b"cmap":
            cmap_offset = struct.unpack_from(">I", font_data, offset + 8)[0]
            break
    if cmap_offset is None:
        raise ValueError("The fixture font has no Unicode character map")
    glyph = None
    for index in range(struct.unpack_from(">H", font_data, cmap_offset + 2)[0]):
        relative = struct.unpack_from(">I", font_data, cmap_offset + 8 + index * 8)[0]
        table = cmap_offset + relative
        if struct.unpack_from(">H", font_data, table)[0] != 4:
            continue
        segments = struct.unpack_from(">H", font_data, table + 6)[0] // 2
        ends = table + 14
        starts = ends + segments * 2 + 2
        deltas = starts + segments * 2
        ranges = deltas + segments * 2
        for segment in range(segments):
            start = struct.unpack_from(">H", font_data, starts + segment * 2)[0]
            end = struct.unpack_from(">H", font_data, ends + segment * 2)[0]
            if not start <= 65 <= end:
                continue
            delta = struct.unpack_from(">h", font_data, deltas + segment * 2)[0]
            range_offset = struct.unpack_from(">H", font_data, ranges + segment * 2)[0]
            if range_offset:
                address = ranges + segment * 2 + range_offset + (65 - start) * 2
                glyph = struct.unpack_from(">H", font_data, address)[0]
                glyph = (glyph + delta) % 65536 if glyph else 0
            else:
                glyph = (65 + delta) % 65536
            break
        if glyph:
            break
    if not glyph:
        raise ValueError("The fixture font does not map the letter A")
    glyph_map = bytearray(265 * 2)
    struct.pack_into(">H", glyph_map, 264 * 2, glyph)
    unicode_map = (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        b"/CMapName /FixtureUnicode def /CMapType 2 def\n"
        b"1 begincodespacerange <00> <ff> endcodespacerange\n"
        b"1 beginbfchar <41> <0041> endbfchar\n"
        b"endcmap CMapName currentdict /CMap defineresource pop end end"
    )
    content = (
        b"BT /F1 18 Tf 24 540 Td (Local PDF rendering resources) Tj ET\n"
        b"BT /F2 24 Tf 24 490 Td (ABC) Tj ET\n"
        b"BT /F3 24 Tf 24 440 Td <41> Tj ET\n"
        b"q 0 1 1 0 k 24 250 160 100 re f Q\n"
    )
    return _pdf_objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 420 594] "
            b"/Resources << /Font << /F1 4 0 R /F2 5 0 R /F3 6 0 R >> >> "
            b"/Contents 10 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Symbol >>",
            b"<< /Type /Font /Subtype /Type0 /BaseFont /Arial "
            b"/Encoding /90ms-RKSJ-H /DescendantFonts [7 0 R] /ToUnicode 9 0 R >>",
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Arial "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 2 >> "
            b"/FontDescriptor 8 0 R /CIDToGIDMap 12 0 R /DW 1000 /W [264 [667]] >>",
            b"<< /Type /FontDescriptor /FontName /Arial /Flags 32 "
            b"/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 880 "
            b"/Descent -120 /CapHeight 880 /StemV 80 /FontFile2 11 0 R >>",
            _pdf_stream(unicode_map),
            _pdf_stream(content),
            _pdf_stream(font_data),
            _pdf_stream(bytes(glyph_map)),
        ]
    )


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
