from __future__ import annotations

import base64
import io
import struct
import zlib
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import HTTPException

from tests.support.book_samples import cbr, cbz, epub, png
from tgdrive.media.book_worker import BookError, ChapterParser, RemoteFile, read_book
from tgdrive.media.books import parse_book


def source(data):
    requests = []

    def fetch(start, size):
        requests.append((start, size))
        return data[start : start + size]

    return RemoteFile(len(data), fetch), requests


RASTER_HEADERS = [
    ("png", ".png", "image/png"),
    ("jpeg", ".jpg", "image/jpeg"),
    ("jpeg-progressive", ".jpg", "image/jpeg"),
    ("gif", ".gif", "image/gif"),
    ("webp-lossy", ".webp", "image/webp"),
    ("webp-lossless", ".webp", "image/webp"),
    ("webp-extended", ".webp", "image/webp"),
    ("avif", ".avif", "image/avif"),
    ("avif-sequence", ".avif", "image/avif"),
]


def raster_header(kind, width, height):
    """Authored format headers; fixtures are never passed to an image decoder."""
    if kind == "png":
        content = bytearray(png())
        struct.pack_into(">II", content, 16, width, height)
        struct.pack_into(">I", content, 29, zlib.crc32(content[12:29]))
        return bytes(content)
    if kind.startswith("jpeg"):
        frame = b"\x08" + struct.pack(">HHB", height, width, 1) + b"\x01\x11\x00"
        marker = b"\xff\xc2" if kind.endswith("progressive") else b"\xff\xc0"
        return (
            b"\xff\xd8\xff\xe1\x00\x06meta"
            + marker
            + struct.pack(">H", len(frame) + 2)
            + frame
            + b"\xff\xd9"
        )
    if kind == "gif":
        return (
            b"GIF89a"
            + struct.pack("<HH", width, height)
            + b"\x00\x00\x00\x2c"
            + struct.pack("<HHHHB", 0, 0, width, height, 0)
            + b"\x02\x02\x44\x01\x00\x3b"
        )
    if kind.startswith("webp"):
        if kind.endswith("lossless"):
            tag = b"VP8L"
            content = b"\x2f" + struct.pack("<I", (height - 1) << 14 | (width - 1))
        elif kind.endswith("extended"):
            tag = b"VP8X"
            content = (
                b"\x00" * 4
                + (width - 1).to_bytes(3, "little")
                + (height - 1).to_bytes(3, "little")
            )
        else:
            tag = b"VP8 "
            content = b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", width, height)
        chunk = tag + struct.pack("<I", len(content)) + content
        chunk += b"\x00" * (len(content) & 1)
        return b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk

    def box(tag, content):
        return struct.pack(">I4s", len(content) + 8, tag) + content

    if kind == "avif":
        content = box(b"ispe", b"\x00" * 4 + struct.pack(">II", width, height))
        content = box(b"meta", b"\x00" * 4 + box(b"iprp", box(b"ipco", content)))
        return box(b"ftyp", b"avif\x00\x00\x00\x00mif1") + content
    if kind == "avif-sequence":
        entry = bytearray(78)
        struct.pack_into(">HH", entry, 24, width, height)
        content = box(
            b"stsd", b"\x00\x00\x00\x00\x00\x00\x00\x01" + box(b"av01", entry)
        )
        for tag in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            content = box(tag, content)
        return box(b"ftyp", b"avis\x00\x00\x00\x00msf1") + content
    raise AssertionError(kind)


def book_page(content, suffix):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("page" + suffix, content)
    return output.getvalue()


@pytest.mark.parametrize("kind,suffix,mime", RASTER_HEADERS)
@pytest.mark.parametrize("raw_images", [False, True])
def test_bounded_raster_headers_preserve_original_bytes(kind, suffix, mime, raw_images):
    image = raster_header(kind, 4000, 4000)
    data = book_page(image, suffix)
    result = read_book(source(data)[0], "cbz", "page" + suffix, raw_images=raw_images)
    assert result["mime"] == mime
    assert (
        result["content"] if raw_images else base64.b64decode(result["data"])
    ) == image


@pytest.mark.parametrize("kind,suffix,_mime", RASTER_HEADERS)
def test_compressed_page_dimensions_are_bounded_before_delivery(kind, suffix, _mime):
    data = book_page(raster_header(kind, 4001, 4000), suffix)
    with pytest.raises(BookError, match=r"16 million pixel.*Download"):
        read_book(source(data)[0], "cbz", "page" + suffix, raw_images=True)


@pytest.mark.parametrize("kind,suffix,_mime", RASTER_HEADERS)
def test_truncated_raster_headers_are_rejected(kind, suffix, _mime):
    image = raster_header(kind, 48, 72)
    image = image[:10] if kind.startswith("jpeg") else image[:-1]
    with pytest.raises(BookError, match=r"invalid or overly complex.*Download"):
        read_book(source(book_page(image, suffix))[0], "cbz", "page" + suffix)


@pytest.mark.parametrize("width,height", [(0, 720), (480, 0), (32769, 1)])
def test_impossible_or_extreme_page_edges_are_rejected(width, height):
    data = book_page(raster_header("png", width, height), ".png")
    with pytest.raises(BookError, match=r"dimensions|32768-pixel edge"):
        read_book(source(data)[0], "epub", "page.png", raw_images=True)


def test_jpeg_standalone_tem_marker_preserves_original_bytes():
    image = raster_header("jpeg", 480, 720)
    image = image[:2] + b"\xff\x01" + image[2:]
    data = book_page(image, ".jpg")
    result = read_book(source(data)[0], "cbz", "page.jpg", raw_images=True)
    assert result["mime"] == "image/jpeg"
    assert result["content"] == image


@pytest.mark.parametrize(
    "kind,suffix", [("png", ".png"), ("gif", ".gif"), ("webp", ".webp")]
)
def test_small_canvas_cannot_hide_an_oversized_frame(kind, suffix):
    if kind == "png":
        image = raster_header("png", 480, 720)
        frame = struct.pack(">IIIIIHHBB", 0, 4001, 4000, 0, 0, 1, 10, 0, 0)
        chunk = (
            struct.pack(">I", len(frame))
            + b"fcTL"
            + frame
            + struct.pack(">I", zlib.crc32(b"fcTL" + frame))
        )
        image = image[:33] + chunk + image[33:]
    elif kind == "gif":
        image = bytearray(raster_header("gif", 48, 72))
        struct.pack_into("<HH", image, 18, 4001, 4000)
        image = bytes(image)
    else:
        image = bytearray(raster_header("webp-extended", 48, 72))
        image += raster_header("webp-lossless", 4001, 4000)[12:]
        struct.pack_into("<I", image, 4, len(image) - 8)
        image = bytes(image)
    with pytest.raises(BookError, match="16 million pixel"):
        read_book(source(book_page(image, suffix))[0], "cbz", "page" + suffix)


@pytest.mark.parametrize("raw_images", [False, True])
async def test_worker_reports_pixel_limit_without_emitting_image(raw_images):
    data = book_page(raster_header("png", 4001, 4000), ".png")

    async def fetch(start, count):
        return data[start : start + count]

    with pytest.raises(HTTPException, match="16 million pixel") as error:
        await parse_book(
            {"name": "oversized.cbz", "size": len(data)},
            fetch,
            "page.png",
            raw_images=raw_images,
        )
    assert error.value.status_code == 422


def test_epub_index_and_selected_chapter_do_not_download_unused_payload():
    data = epub(padding=12 * 1024**2, unsafe=True)
    stream, requests = source(data)
    index = read_book(stream, "epub")
    assert index["title"] == "The little journey"
    assert [entry["label"] for entry in index["entries"]] == [
        "A quiet morning",
        "Across the city",
    ]
    chapter = read_book(stream, "epub", index["entries"][0]["name"])
    text = str(chapter)
    assert "The station was quiet" in text
    assert "bookExecuted" not in text and "external.invalid" not in text
    assert "javascript:" not in text
    assert sum(count for _, count in requests) < len(data) / 2


def test_cbz_natural_page_order_and_one_resource():
    data = cbz(padding=12 * 1024**2)
    stream, requests = source(data)
    index = read_book(stream, "cbz")
    assert [entry["name"] for entry in index["entries"]] == [
        "pages/1.png",
        "pages/2.png",
        "pages/10.png",
    ]
    page = read_book(stream, "cbz", "pages/2.png")
    assert base64.b64decode(page["data"]) == png(2)
    assert page["mime"] == "image/png"
    assert sum(count for _, count in requests) < 4 * 1024**2


def test_cbr_real_rar_archive_and_page():
    try:
        import libarchive  # noqa: F401
    except ImportError, OSError, AttributeError:
        pytest.skip("Install libarchive for the native CBR test")
    stream, _ = source(cbr())
    index = read_book(stream, "cbr")
    assert [entry["name"] for entry in index["entries"]] == ["1.png", "2.png"]
    page = read_book(stream, "cbr", "1.png")
    assert base64.b64decode(page["data"]) == png(1)


@pytest.mark.parametrize(
    "name", ["../page.png", "/page.png", "a/../page.png", "a\\page.png"]
)
def test_archive_paths_are_rejected(name):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(name, png())
    with pytest.raises(BookError, match="path"):
        read_book(source(output.getvalue())[0], "cbz")


def test_zip_bomb_and_duplicate_resources_are_rejected():
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("page.png", b"x" * (17 * 1024**2))
    with pytest.raises(BookError, match="16 MiB"):
        read_book(source(output.getvalue())[0], "cbz", "page.png")
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("page.png", png())
        with pytest.warns(UserWarning):
            archive.writestr("page.png", png(2))
    with pytest.raises(BookError, match="Duplicate"):
        read_book(source(output.getvalue())[0], "cbz")


def test_chapter_parser_drops_active_content_and_external_resources():
    parser = ChapterParser("OPS/chapter.xhtml")
    parser.feed(
        '<p id="start" onclick="alert(1)">Hello <em>reader</em></p>'
        "<style>body{display:none}</style><script>alert(1)</script>"
        '<img src="//external.invalid/x"/><a '
        'href="../next.xhtml#x">Next</a>'
    )
    text = str(parser.root)
    assert "onclick" not in text and "alert" not in text and "external" not in text
    assert "next.xhtml#x" in text and "reader" in text


async def test_real_isolated_worker_protocol_and_fetch_bounds():
    data = cbz(padding=8 * 1024**2)
    reads = []

    async def fetch(start, count):
        reads.append((start, count))
        return data[start : start + count]

    index = await parse_book({"name": "comic.cbz", "size": len(data)}, fetch)
    assert len(index["entries"]) == 3
    assert sum(n for _, n in reads) < len(data) / 2

    async def broken(_start, count):
        return b"x" * count

    with pytest.raises(HTTPException) as error:
        await parse_book({"name": "broken.epub", "size": 100}, broken)
    assert error.value.status_code == 422


async def test_large_selected_page_keeps_original_bytes_without_reading_whole_archive():
    from tgdrive.media.book_worker import BLOCK

    image = png() + b"p" * (39 * BLOCK)
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("unused.bin", b"x" * (48 * BLOCK))
        archive.writestr("page.png", image)
    data = output.getvalue()
    reads = []

    async def fetch(start, count):
        reads.append((start, count))
        return data[start : start + count]

    result = await parse_book(
        {"name": "large.cbz", "size": len(data)}, fetch, "page.png", raw_images=True
    )
    assert result["content"] == image
    assert "data" not in result
    assert sum(count for _, count in reads) < 43 * BLOCK
    assert all(count <= BLOCK for _, count in reads)


def test_selected_image_limit_does_not_expand_text_or_legacy_thumbnail_limits():
    from tgdrive.media.book_worker import BLOCK, MAX_PAGE_READ

    image = png() + b"p" * (17 * BLOCK)
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("page.png", image)
        archive.writestr("chapter.xhtml", b"x" * (3 * BLOCK))
    data = output.getvalue()

    def stream():
        return RemoteFile(
            len(data),
            lambda start, count: data[start : start + count],
            maximum=MAX_PAGE_READ,
        )

    with pytest.raises(BookError, match="16 MiB"):
        read_book(stream(), "cbz", "page.png")
    with pytest.raises(BookError, match="2 MiB"):
        read_book(stream(), "epub", "chapter.xhtml", raw_images=True)


async def test_book_cache_bounds_expiry_and_same_page_waiters(monkeypatch):
    import asyncio

    import tgdrive.media.books as books

    clock = [100]
    monkeypatch.setattr(books.time, "monotonic", lambda: clock[0])
    cache = books.BookCache(maximum=8, entries=2, ttl=5)

    def result(content):
        return {"kind": "image", "mime": "image/png", "content": content}

    cache.put("a", result(b"12345"))
    cache.put("b", result(b"6789"))
    assert cache.get("a") is None and cache.bytes == 4
    clock[0] += 6
    assert cache.get("b") is None and cache.bytes == 0
    calls = []

    async def request():
        async with cache.hold("shared"):
            value = cache.get("shared")
            if value is None:
                calls.append(1)
                await asyncio.sleep(0)
                value = cache.put("shared", result(b"page"))
            return value[0]

    assert await asyncio.gather(request(), request()) == [b"page", b"page"]
    assert len(calls) == 1 and not cache.locks
