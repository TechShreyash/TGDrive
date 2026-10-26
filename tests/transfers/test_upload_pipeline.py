from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import functions, types
from telethon.errors import FloodPremiumWaitError, FloodWaitError

from tgdrive.media.media_scheduler import MediaScheduler
from tgdrive.telegram.clients import Client

PART = 512 * 1024


class Native:
    def __init__(self):
        self.parts = {}
        self.calls = Counter()
        self.active = 0
        self.maximum = 0
        self.published = []
        self.fail_part = None
        self.flood_part = None
        self.flood_seconds = 1
        self.flood_count = 1
        self.flood_error = FloodWaitError
        self.hold = None
        self.get_input_entity = AsyncMock(return_value="channel")

    async def __call__(self, request):
        if isinstance(
            request,
            (
                functions.upload.SaveFilePartRequest,
                functions.upload.SaveBigFilePartRequest,
            ),
        ):
            self.calls[request.file_part] += 1
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                if self.hold is not None:
                    await self.hold.wait()
                await asyncio.sleep(0.005 if request.file_part == 0 else 0.001)
                if (
                    self.calls[request.file_part] <= self.flood_count
                    and request.file_part == self.flood_part
                ):
                    raise self.flood_error(request, capture=self.flood_seconds)
                if (
                    self.calls[request.file_part] == 1
                    and request.file_part == self.fail_part
                ):
                    raise TimeoutError("transient part failure")
                self.parts[request.file_part] = request.bytes
                return True
            finally:
                self.active -= 1
        self.published.append(request)
        return SimpleNamespace(updates=[])


def client(native, *, limit=2):
    value = Client.__new__(Client)
    value.native = native
    value.name = "storage-bot-1"
    value._upload_pipeline = 4
    value._media_scheduler = MediaScheduler(limit=limit, rate=512)
    value._sent = AsyncMock(return_value="stored")
    value._defer_read = AsyncMock()
    return value


@pytest.mark.asyncio
async def test_pipeline_retries_only_failed_part_and_preserves_window_hash_and_dispatch(
    tmp_path,
):
    payload = b"a" * PART + b"b" * PART + b"tail"
    path = tmp_path / "staged"
    path.write_bytes(b"prefix" + payload + b"suffix")
    native = Native()
    native.fail_part = 1
    adapter = client(native)
    progress = AsyncMock()
    boundary = AsyncMock()
    assert (
        await adapter.send_staged(
            123,
            path,
            file_name="file.bin",
            offset=6,
            size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            random_id=987,
            progress=progress,
            before_dispatch=boundary,
        )
        == "stored"
    )
    assert b"".join(native.parts[index] for index in sorted(native.parts)) == payload
    assert native.calls == {0: 1, 1: 2, 2: 1}
    assert 1 < native.maximum <= 2
    request = native.published[0]
    assert request.random_id == 987
    assert (
        request.media.file.md5_checksum
        == hashlib.md5(payload, usedforsecurity=False).hexdigest()
    )
    boundary.assert_awaited_once()
    assert progress.await_args.args == (len(payload), len(payload))


@pytest.mark.asyncio
async def test_big_file_pipeline_retries_one_part_without_resending_acked_parts(
    tmp_path,
):
    # Telegram uses SaveBigFilePart/InputFileBig above 10MiB. Keep a nonaligned
    # window/tail so total-parts and neighboring staged bytes are both checked.
    payload = bytes(range(256)) * (10 * 1024 * 1024 // 256) + b"last-part" * 57
    total_parts = (len(payload) + PART - 1) // PART
    path = tmp_path / "large-stage"
    path.write_bytes(b"prefix" + payload + b"suffix")

    class BigNative(Native):
        async def __call__(self, request):
            if isinstance(request, functions.upload.SaveBigFilePartRequest):
                assert request.file_total_parts == total_parts
            assert not isinstance(request, functions.upload.SaveFilePartRequest)
            return await super().__call__(request)

    native = BigNative()
    native.fail_part = 7
    adapter = client(native, limit=3)
    progress = AsyncMock()
    await adapter.send_staged(
        123,
        path,
        file_name="large.bin",
        offset=6,
        size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        random_id=987,
        progress=progress,
    )
    assert b"".join(native.parts[index] for index in range(total_parts)) == payload
    assert native.calls == {
        index: 2 if index == native.fail_part else 1 for index in range(total_parts)
    }
    assert 1 < native.maximum <= 3
    assert len(native.published) == 1
    uploaded = native.published[0].media.file
    assert isinstance(uploaded, types.InputFileBig)
    assert uploaded.parts == total_parts
    assert uploaded.name == "large.bin"
    assert progress.await_args.args == (len(payload), len(payload))
    assert adapter._media_scheduler.active == 0
    assert adapter._media_scheduler.snapshot()["queued"] == 0


@pytest.mark.asyncio
async def test_pipeline_cancellation_drains_all_parts_and_does_not_publish(tmp_path):
    path = tmp_path / "staged"
    path.write_bytes(b"x" * (PART * 8))
    native = Native()
    native.hold = asyncio.Event()
    adapter = client(native)
    task = asyncio.create_task(
        adapter.send_staged(
            123,
            path,
            file_name="file.bin",
            random_id=1,
        )
    )
    async with asyncio.timeout(2):
        while native.active < 2:  # noqa: ASYNC110 - observe the synthetic RPC boundary.
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert native.active == 0
    assert native.published == []
    assert adapter._media_scheduler.active == 0
    assert adapter._media_scheduler.snapshot()["queued"] == 0


@pytest.mark.asyncio
async def test_pipeline_refuses_publication_after_integrity_failure(tmp_path):
    path = tmp_path / "staged"
    path.write_bytes(b"actual bytes")
    native = Native()
    adapter = client(native)
    boundary = AsyncMock()
    with pytest.raises(ValueError, match="changed"):
        await adapter.send_staged(
            123,
            path,
            file_name="file.bin",
            random_id=1,
            expected_sha256="0" * 64,
            before_dispatch=boundary,
        )
    assert native.published == []
    boundary.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds, occurrences", [(1, 1), (45, 4)])
@pytest.mark.parametrize("flood_error", [FloodWaitError, FloodPremiumWaitError])
async def test_pipeline_flood_feedback_retries_individual_part(
    tmp_path, seconds, occurrences, flood_error
):
    path = tmp_path / "staged"
    path.write_bytes(b"x" * (PART * 3))
    native = Native()
    native.flood_part = 1
    native.flood_seconds = seconds
    native.flood_count = occurrences
    native.flood_error = flood_error
    adapter = client(native, limit=4)
    original = adapter._media_scheduler.flood

    async def feedback(seconds):
        assert seconds >= native.flood_seconds
        await original(0)

    adapter._media_scheduler.flood = feedback
    await adapter.send_staged(123, path, file_name="file.bin", random_id=1)
    assert native.calls == {0: 1, 1: occurrences + 1, 2: 1}
    assert adapter._defer_read.await_count == occurrences
    assert adapter._media_scheduler.floods == occurrences
    assert adapter._media_scheduler.effective_limit == max(1, 4 // 2**occurrences)


class EndpointNative:
    def __init__(self, fail=False):
        self.session = SimpleNamespace(
            dc_id=5,
            server_address="old",
            port=443,
            auth_key=object(),
            save=Mock(),
        )
        self.session.set_dc = self.set_dc
        self.disconnect = AsyncMock()
        self.connect = AsyncMock(
            side_effect=[OSError("unreachable"), None] if fail else None
        )
        self.is_user_authorized = AsyncMock(return_value=True)

    def set_dc(self, dc_id, server_address, port):
        self.session.dc_id = dc_id
        self.session.server_address = server_address
        self.session.port = port

    async def __call__(self, request):
        assert isinstance(request, functions.help.GetConfigRequest)
        return SimpleNamespace(
            dc_options=[
                SimpleNamespace(
                    id=4,
                    ip_address="wrong-dc",
                    port=443,
                    media_only=False,
                    cdn=False,
                    ipv6=False,
                ),
                SimpleNamespace(
                    id=5,
                    ip_address="media-only",
                    port=443,
                    media_only=True,
                    cdn=False,
                    ipv6=False,
                ),
                SimpleNamespace(
                    id=5,
                    ip_address="current",
                    port=443,
                    media_only=False,
                    cdn=False,
                    ipv6=False,
                ),
            ]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_live_endpoint_refresh_preserves_dc_auth_and_rolls_back_on_failure(fail):
    native = EndpointNative(fail)
    auth_key = native.session.auth_key
    adapter = client(native)
    await adapter._refresh_main_endpoint()
    assert native.session.dc_id == 5
    assert native.session.auth_key is auth_key
    assert native.session.server_address == ("old" if fail else "current")
    assert native.connect.await_count == (2 if fail else 1)
    assert native.disconnect.await_count == (2 if fail else 1)
