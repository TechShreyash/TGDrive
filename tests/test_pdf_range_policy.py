"""PDF preview purpose retains demand bytes without bulk preload admission."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_multipart_streaming import MultipartClient, build_parts
from test_streaming import FakeStreamClient, make_request, response_body

from tgdrive.client_pool import ClientPool
from tgdrive.streaming import stream_telegram_file


@pytest.mark.asyncio
@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("download", [False, True])
async def test_pdf_preview_skips_automatic_prepare_but_download_keeps_policy(
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
        assert len(await response_body(response)) == 4 * 1024**2
        assert manager.observe.called is download
        assert manager.delivered.called is download
        assert manager.register_file.called is not download
    finally:
        await pool.stop()
