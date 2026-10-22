from __future__ import annotations

import base64
import io
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from book_samples import cbr, cbz, epub, png
from fastapi import HTTPException

from tgdrive.book_worker import BookError, ChapterParser, RemoteFile, read_book
from tgdrive.books import parse_book


def source(data):
    requests = []

    def fetch(start, size):
        requests.append((start, size))
        return data[start : start + size]

    return RemoteFile(len(data), fetch), requests


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
    except (ImportError, OSError):
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
    with pytest.raises(BookError, match="limits"):
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
