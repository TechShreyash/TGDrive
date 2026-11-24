import io
import struct
from zipfile import ZipFile

import pytest

from tgdrive.media.archive_preview import parse_archive
from tgdrive.media.archive_preview_worker import RemoteFile, ZipPreviewError, list_zip
from tgdrive.media.previews import describe_preview


def sample_zip():
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("large.bin", b"x" * (4 * 1024**2))
        archive.writestr("Documents/notes.txt", b"hello")
        archive.writestr("empty/", b"")
        archive.writestr("<script>alert(1)</script>.txt", b"inert")
    return output.getvalue()


@pytest.mark.asyncio
async def test_zip_index_is_sparse_and_contains_no_extracted_data():
    content = sample_zip()
    reads = []

    async def fetch(start, size):
        reads.append((start, size))
        return content[start : start + size]

    result = await parse_archive({"name": "files.zip", "size": len(content)}, fetch)
    assert result["kind"] == "archive"
    assert result["entries"][0] == {
        "name": "large.bin",
        "directory": False,
        "size": 4 * 1024**2,
        "encrypted": False,
    }
    assert result["entries"][2]["directory"] is True
    assert sum(size for _, size in reads) < 2 * 1024**2
    assert all(start >= len(content) - 2 * 1024**2 for start, _ in reads)


def test_encrypted_zip_can_be_listed_without_reading_entry_payloads():
    content = bytearray(sample_zip())
    at = content.index(b"PK\x01\x02")
    struct.pack_into("<H", content, at + 8, 1)
    source = RemoteFile(len(content), lambda start, size: content[start : start + size])
    assert list_zip(source)["entries"][0]["encrypted"] is True
    with pytest.raises(ZipPreviewError, match="without extracting"):
        list_zip(source, "large.bin")


@pytest.mark.parametrize("name", ["../secret", "/absolute", "bad\\path", "dir/../"])
def test_zip_preview_refuses_unsafe_paths(name):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(name, b"")
    content = output.getvalue()
    source = RemoteFile(len(content), lambda start, size: content[start : start + size])
    with pytest.raises(ZipPreviewError, match="path"):
        list_zip(source)


def test_empty_zip_has_an_empty_index():
    output = io.BytesIO()
    with ZipFile(output, "w"):
        pass
    content = output.getvalue()
    source = RemoteFile(len(content), lambda start, size: content[start : start + size])
    assert list_zip(source) == {"kind": "archive", "entries": []}


@pytest.mark.asyncio
async def test_zip_descriptor_needs_no_provider_read():
    result = await describe_preview(
        pool=None,
        channel_id=-100,
        message_id=1,
        file_name="Files.ZIP",
        size=5_000_000_000,
        path="/id",
        auth=None,
    )
    assert result["kind"] == "archive"
