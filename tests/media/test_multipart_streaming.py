from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from starlette.requests import ClientDisconnect

from tests.media.test_streaming import make_request, response_body
from tgdrive.media.streaming import (
    TelegramFileNotFound,
    TelegramVariantNotFound,
    stream_telegram_file,
)
from tgdrive.telegram.client_pool import ClientPool, NoClientAvailable

CHUNK = 1024**2


def build_parts(contents, *, identities=True):
    result = []
    offset = 0
    for index, content in enumerate(contents):
        part = {
            "index": index,
            "offset": offset,
            "size": len(content),
            "message_id": index + 101,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        if identities:
            part["document_id"] = index + 9001
        result.append(part)
        offset += len(content)
    return result


class MultipartClient:
    def __init__(self, contents, *, name="multipart-test"):
        self.contents = {index + 101: content for index, content in enumerate(contents)}
        self.sizes = {
            identity: len(content) for identity, content in self.contents.items()
        }
        self.document_ids = {
            identity: identity - 101 + 9001 for identity in self.contents
        }
        self.name = name
        self.is_connected = False
        self.fetches = []
        self.reads = []
        self.closed = []
        self.fetch_error = None
        self.before_fetch = None
        self.before_chunk = None

    async def start(self):
        self.is_connected = True

    async def stop(self):
        self.is_connected = False

    async def get_messages(self, _channel, *, message_ids):
        self.fetches.append(message_ids)
        if self.before_fetch:
            await self.before_fetch(message_ids)
        if self.fetch_error:
            raise self.fetch_error
        if message_ids not in self.contents:
            return None
        return SimpleNamespace(
            id=message_ids,
            empty=False,
            document=SimpleNamespace(
                id=self.document_ids[message_ids],
                file_size=self.sizes[message_ids],
            ),
        )

    async def stream_media(self, message, *, offset, limit):
        self.reads.append((message.id, offset, limit))
        try:
            for index in range(offset, offset + limit):
                if self.before_chunk:
                    await self.before_chunk(message.id, index)
                content = self.contents[message.id][index * CHUNK : (index + 1) * CHUNK]
                if not content:
                    break
                yield content
        finally:
            self.closed.append(message.id)


async def stream(
    pool,
    contents,
    range_header=None,
    *,
    method="GET",
    name="movie.mp4",
    parts=None,
    **options,
):
    request = make_request(range_header)
    request.scope["method"] = method
    return await stream_telegram_file(
        pool=pool,
        channel_id=-100,
        message_id=101,
        parts=parts if parts is not None else build_parts(contents),
        expected_size=sum(map(len, contents)),
        file_name=name,
        request=request,
        **options,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection",
    [
        None,
        "bytes=0-0",
        f"bytes={CHUNK - 1}-{CHUNK + 1}",
        f"bytes={CHUNK + 15}-{CHUNK + 20}",
        f"bytes={CHUNK + 17}-{CHUNK + 17}",
        "bytes=-9",
        "bytes=5-",
        "bytes=0-999999999",
    ],
)
async def test_logical_ranges_are_byte_exact_across_chunk_and_part_boundaries(
    selection,
):
    from tgdrive.media.ranges import parse_byte_range

    contents = [
        bytes(range(256)) * 4096 + b"FIRST-PART-END!!!?",
        b"second-part-body",
        b"tail-data",
    ]
    whole = b"".join(contents)
    client = MultipartClient(contents)
    pool = ClientPool([client])
    await pool.start()
    try:
        response = await stream(pool, contents, selection)
        selected = parse_byte_range(selection, len(whole))
        body = await response_body(response)
        assert body == whole[selected.start : selected.end + 1]
        assert int(response.headers["content-length"]) == selected.length
        assert response.status_code == (206 if selection else 200)
        if selection:
            assert response.headers["content-range"] == selected.content_range
        assert (
            hashlib.sha256(body).digest()
            == hashlib.sha256(whole[selected.start : selected.end + 1]).digest()
        )
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_boundary_range_reads_only_intersecting_part_and_head_never_streams():
    contents = [b"abc", b"defg", b"hij"]
    client = MultipartClient(contents)
    pool = ClientPool([client])
    await pool.start()
    try:
        response = await stream(pool, contents, "bytes=3-6")
        assert await response_body(response) == b"defg"
        assert client.fetches == [102] and client.reads == [(102, 0, 1)]
        client.fetches.clear()
        client.reads.clear()
        response = await stream(pool, contents, "bytes=-3", method="HEAD")
        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 7-9/10"
        assert response.body == b"" and response.headers["content-length"] == "3"
        assert client.fetches == [103] and client.reads == []
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "status"),
    [
        ("bytes=10-", 416),
        ("bytes=5-4", 416),
        ("bytes=-0", 416),
        ("bytes=0-1,3-4", 400),
        ("garbage", 400),
    ],
)
async def test_invalid_ranges_are_rejected_before_any_telegram_rpc(header, status):
    contents = [b"abc", b"defg", b"hij"]
    client = MultipartClient(contents)
    pool = ClientPool([client])
    await pool.start()
    try:
        result = await stream(pool, contents, header)
        assert result.status_code == status
        assert not client.fetches and not client.reads
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "size", "replacement"])
async def test_bad_first_part_is_rejected_before_response_headers(failure):
    contents = [b"abc", b"def"]
    client = MultipartClient(contents)
    if failure == "missing":
        client.contents.pop(101)
    elif failure == "size":
        client.sizes[101] += 1
    else:
        client.document_ids[101] += 1
    pool = ClientPool([client])
    await pool.start()
    try:
        with pytest.raises(TelegramFileNotFound):
            await stream(pool, contents)
        assert not client.reads and (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "size", "short", "checksum"])
async def test_late_part_failure_cannot_satisfy_content_length_and_releases_all(
    failure,
):
    contents = [b"abc", b"defgh"]
    client = MultipartClient(contents)
    if failure == "missing":
        client.contents.pop(102)
    elif failure == "size":
        client.sizes[102] += 1
    elif failure == "short":
        client.contents[102] = b"def"
    else:
        client.contents[102] = b"WRONG"  # Same size, same document id: byte corruption.
    pool = ClientPool([client])
    await pool.start()
    received = bytearray()
    try:
        response = await stream(pool, contents)
        with pytest.raises(OSError):
            async for chunk in response.body_iterator:
                received.extend(chunk)
        assert len(received) < int(response.headers["content-length"])
        assert client.closed[0] == 101
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_partial_range_uses_immutable_identity_without_hashing_unread_bytes():
    contents = [b"x" * (3 * CHUNK), b"tail"]
    parts = build_parts(contents)
    parts[0]["sha256"] = "0" * 64
    client = MultipartClient(contents)
    pool = ClientPool([client])
    await pool.start()
    try:
        result = await stream(pool, contents, "bytes=2-5", parts=parts)
        assert await response_body(result) == b"xxxx"
        assert client.reads == [(101, 0, 1)]
        assert client.fetches == [101]
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("at_header", [True, False])
async def test_asgi_send_failure_closes_unstarted_or_started_body_and_releases(
    at_header,
):
    contents = [b"a" * (2 * CHUNK), b"tail"]
    client = MultipartClient(contents)
    pool = ClientPool([client])
    await pool.start()
    try:
        response = await stream(pool, contents)

        async def send(message):
            if (message["type"] == "http.response.start") == at_header:
                raise OSError("synthetic browser disconnect")

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(ClientDisconnect):
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
            )
        assert (await pool.health()).active_leases == 0
        if not at_header:
            assert client.closed == [101]
        assert 102 not in client.fetches
    finally:
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["metadata", "body"])
async def test_cancellation_at_metadata_or_second_part_drains_leases(phase):
    contents = [b"abc", b"def"]
    client = MultipartClient(contents)
    entered = asyncio.Event()

    async def block(identity, *_args):
        if identity == (101 if phase == "metadata" else 102):
            entered.set()
            await asyncio.Event().wait()

    if phase == "metadata":
        client.before_fetch = block
    else:
        client.before_chunk = block
    pool = ClientPool([client])
    await pool.start()

    async def consume():
        response = await stream(pool, contents)
        return await response_body(response)

    task = asyncio.create_task(consume())
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await pool.health()).active_leases == 0
        if phase == "body":
            assert client.closed == [101, 102]
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_premium_fallback_and_unavailable_bot_preserve_original_failure():
    contents = [b"abc", b"def"]
    premium = MultipartClient(contents, name="premium")
    premium.fetch_error = PermissionError("channel unavailable to optional account")
    bot = MultipartClient(contents, name="bot")
    pool = ClientPool([bot], [premium])
    await pool.start()
    try:
        response = await stream(pool, contents)
        assert await response_body(response) == b"abcdef"
        assert len(bot.fetches) == 2 and len(premium.fetches) == 2
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()
    pool = ClientPool([], [premium])
    await pool.start()
    try:
        with pytest.raises(NoClientAvailable):
            await stream(pool, contents)
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_download_forces_original_and_active_content_stays_attachment():
    contents = [b"<script>", b"bad()</script>"]
    client = MultipartClient(contents)
    pool = ClientPool([client])
    await pool.start()
    try:
        with pytest.raises(TelegramVariantNotFound):
            await stream(pool, contents, quality="invented")
        assert not client.fetches
        result = await stream(
            pool, contents, name="unsafe.html", download=True, quality="invented"
        )
        assert result.headers["content-disposition"].startswith("attachment;")
        assert result.headers["content-security-policy"].startswith("sandbox")
        assert await response_body(result) == b"".join(contents)
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_full_part_checksum_withholds_final_block_after_prior_blocks_were_sent():
    contents = [b"a" * (2 * CHUNK), b"tail"]
    client = MultipartClient(contents)
    client.contents[101] = b"a" * CHUNK + b"X" * CHUNK
    pool = ClientPool([client])
    await pool.start()
    received = bytearray()
    try:
        response = await stream(pool, contents)
        with pytest.raises(OSError, match="checksum"):
            async for chunk in response.body_iterator:
                received.extend(chunk)
        assert bytes(received) == b"a" * CHUNK
        assert len(received) < int(response.headers["content-length"])
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("download", [False, True])
async def test_multipart_pdf_cap_is_preview_only_and_head_never_reads_payload(download):
    contents = [b"first", b"last"]
    parts = build_parts(contents)
    from tgdrive.media.streaming import PDF_PREVIEW_BYTES

    cap_chunks = PDF_PREVIEW_BYTES // CHUNK
    parts[0]["size"] = cap_chunks * CHUNK
    parts[1].update(offset=cap_chunks * CHUNK, size=CHUNK)
    client = MultipartClient(contents)
    client.sizes.update({101: cap_chunks * CHUNK, 102: CHUNK})
    pool = ClientPool([client])
    await pool.start()
    request = make_request()
    request.scope["method"] = "HEAD"
    try:
        response = await stream_telegram_file(
            pool=pool,
            channel_id=-100,
            message_id=101,
            file_name="large.pdf",
            expected_size=(cap_chunks + 1) * CHUNK,
            parts=parts,
            request=request,
            preview=True,
            download=download,
        )
        assert response.status_code == (200 if download else 413)
        assert not client.reads
        if download:
            assert response.headers["content-length"] == str((cap_chunks + 1) * CHUNK)
            assert response.headers["content-disposition"].startswith("attachment;")
        else:
            assert not client.fetches
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()
