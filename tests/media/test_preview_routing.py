"""Ambiguous suffixes require bounded content evidence, not a filename/MIME guess."""

from types import SimpleNamespace

import pytest

from tgdrive.media.previews import TEXT_PREVIEW_BYTES, describe_preview
from tgdrive.telegram.client_pool import ClientPool


class RoutingClient:
    name = "preview-routing"
    is_connected = False

    def __init__(self, content, mime_type=None):
        self.content = content
        self.mime_type = mime_type
        self.windows = []
        self.closed = False

    async def start(self):
        self.is_connected = True

    async def stop(self):
        self.is_connected = False

    async def get_messages(self, channel, *, message_ids):
        # Match the facade, whose small media summary omits native MIME.
        return SimpleNamespace(
            id=message_ids,
            document=SimpleNamespace(file_size=len(self.content)),
            native=SimpleNamespace(document=SimpleNamespace(mime_type=self.mime_type)),
        )

    async def stream_media(self, message, *, limit, offset):
        self.windows.append((limit, offset))
        try:
            yield self.content[: 1024**2]
        finally:
            self.closed = True


async def preview(content, *, name="sample.ts", mime_type=None):
    client = RoutingClient(content, mime_type)
    pool = ClientPool([client])
    await pool.start()
    try:
        result = await describe_preview(
            pool=pool,
            channel_id=-100,
            message_id=7,
            file_name=name,
            size=len(content),
            path="/id",
            auth=None,
        )
        assert client.windows == [(1, 0)]
        assert client.closed
        assert (await pool.health()).active_leases == 0
        return result
    finally:
        await pool.stop()


def transport_packets(count=5, stride=188, *, invalid_header=False):
    # Transport sync/header and binary payload, independent of any file suffix.
    packet = b"\x47\x40\x00" + (b"\x00" if invalid_header else b"\x10")
    packet += b"\x00" * 184
    if stride == 192:
        return (b"\x00" * 4 + packet) * count
    return (packet + b"\xff" * (stride - 188)) * count


@pytest.mark.parametrize("mime_type", [None, "text/plain", "video/mp2t"])
@pytest.mark.parametrize("name", ["sample.ts", "module.mts", "module.cts"])
async def test_typescript_stays_inert_text_even_with_video_mime(mime_type, name):
    content = b"export async function main(): Promise<string> { return '<video/>'; }"
    result = await preview(content, name=name, mime_type=mime_type)
    assert result["kind"] == "text"
    assert result["language"] == "typescript"
    assert result["text"].encode() == content


async def test_packet_shaped_printable_source_does_not_override_text():
    content = (b"GAA3 " + b" " * 183) * 8
    result = await preview(content, mime_type="video/mp2t")
    assert result["kind"] == "text"
    assert result["text"].encode() == content


@pytest.mark.parametrize("stride", [188, 192, 204])
@pytest.mark.parametrize("mime_type", [None, "text/plain", "video/mp2t"])
async def test_transport_packet_content_is_video_despite_wrong_or_missing_mime(
    stride, mime_type
):
    result = await preview(transport_packets(stride=stride), mime_type=mime_type)
    assert result["kind"] == "video"
    assert "text" not in result
    assert "url" not in result


async def test_timestamp_prefixed_mts_is_also_content_classified():
    result = await preview(
        transport_packets(stride=192), name="camera.mts", mime_type="text/plain"
    )
    assert result["kind"] == "video"


async def test_mime_only_corroborates_repeated_complete_packet_evidence():
    sample = transport_packets(count=3)
    assert (await preview(sample, mime_type="video/mp2t"))["kind"] == "video"
    assert (await preview(sample, mime_type="text/plain"))["kind"] == "unsupported"


@pytest.mark.parametrize(
    "content",
    [
        b"binary\x00data",
        b"\x47" + b"\x00" * 1000,
        transport_packets(count=1),
        transport_packets(invalid_header=True),
        b"\xff\xfa\xfe\x00" * 100,
    ],
)
async def test_unproven_binary_does_not_enter_either_source_or_video(content):
    result = await preview(content, mime_type="video/mp2t")
    assert result["kind"] == "unsupported"
    assert "Download the original" in result["reason"]
    assert "text" not in result


async def test_utf16_typescript_and_utf8_truncation_remain_readable():
    source = "export const greeting = 'hello ☃';"
    result = await preview(source.encode("utf-16"), mime_type="video/mp2t")
    assert result["kind"] == "text"
    assert result["text"] == source
    content = b"x" * (TEXT_PREVIEW_BYTES - 1) + "🙂".encode() + b"tail"
    result = await preview(content, name="large.ts")
    assert result["kind"] == "text"
    assert result["truncated"]
    assert result["text"] == "x" * (TEXT_PREVIEW_BYTES - 1)
    assert "�" not in result["text"]


async def test_invalid_text_encoding_offers_original_instead_of_garbled_source():
    result = await preview(b"hello\xffworld", name="sample.txt")
    assert result["kind"] == "unsupported"
    assert "supported encoding" in result["reason"]
