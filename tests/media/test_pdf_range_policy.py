"""PDF preview purpose retains demand bytes without bulk preload admission."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.media.test_multipart_streaming import MultipartClient, build_parts
from tests.media.test_streaming import FakeStreamClient, make_request, response_body
from tgdrive.media.streaming import stream_telegram_file
from tgdrive.telegram.client_pool import ClientPool


@pytest.mark.asyncio
@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("download", [False, True])
async def test_pdf_preview_and_download_do_not_start_extra_preloading(
    multipart, download
):
    contents = [b"A" * 3 * 1024**2, b"B" * 3 * 1024**2]
    client = (
        MultipartClient(contents) if multipart else FakeStreamClient(b"".join(contents))
    )
    pool = ClientPool([client])
    manager = SimpleNamespace(
        register_file=Mock(),
        observe=Mock(return_value="pdf-file"),
        begin=Mock(),
        delivered=Mock(),
        finished=Mock(),
    )
    pool._preload_manager = manager
    await pool.start()
    try:
        response = await stream_telegram_file(
            pool=pool,
            channel_id=-100,
            message_id=101 if multipart else 7,
            file_name="book.pdf",
            expected_size=6 * 1024**2,
            request=make_request("bytes=0-4194303"),
            preview=True,
            download=download,
            parts=build_parts(contents) if multipart else None,
        )
        assert response.status_code == 206
        assert await response_body(response) == b"".join(contents)[: 4 * 1024**2]
        manager.observe.assert_not_called()
        manager.delivered.assert_not_called()
        manager.register_file.assert_called_once()
    finally:
        await pool.stop()
