from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.media.test_pooled_media import account
from tgdrive.media.books import read_range
from tgdrive.media.media_reader import BLOCK
from tgdrive.media.media_scheduler import effective_priority, media_priority
from tgdrive.media.previews import lease_media
from tgdrive.telegram.client_pool import ClientPool


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_cold_book_range_uses_single_account_slot_without_nested_reservation(
    tmp_path, background
):
    payload = b"a" * BLOCK + b"book tail"
    client = account("only-bot", payload)
    metadata = client.get_messages.side_effect
    priorities = []

    def fetch(*args, **kwargs):
        priorities.append(effective_priority(media_priority.get()))
        return metadata(*args, **kwargs)

    client.get_messages = AsyncMock(side_effect=fetch)
    pool = ClientPool([client], media_per_bot=1, media_cache_dir=tmp_path)
    await pool.start()
    try:
        result = await asyncio.wait_for(
            read_range(
                pool,
                -1000000000123,
                {"message_id": 77, "size": len(payload)},
                11,
                len(payload) - 11,
                AsyncMock(),
                background=background,
            ),
            2,
        )
        assert result == payload[11:]
        assert priorities == ["preload" if background else "demand"]
        assert media_priority.get() == "demand"
        assert client.native.requests and client.native.peak == 1
        assert not pool._origin_waiters and not pool._origin_active
        assert all(row.active_leases == 0 for row in await pool.status())
    finally:
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [OSError("metadata failed"), asyncio.CancelledError()]
)
async def test_background_metadata_failure_releases_lease_and_priority(error):
    client = account("only-bot", b"book")

    async def fail(*_args, **_kwargs):
        assert effective_priority(media_priority.get()) == "preload"
        raise error

    client.get_messages = AsyncMock(side_effect=fail)
    pool = ClientPool([client], media_per_bot=1)
    await pool.start()
    try:
        with pytest.raises(type(error)):
            async with lease_media(pool, -1000000000123, 77, background=True):
                pytest.fail("Failed metadata cannot yield a media lease")
        assert media_priority.get() == "demand"
        assert not pool._origin_waiters and not pool._origin_active
        assert all(row.active_leases == 0 for row in await pool.status())
    finally:
        await pool.stop()
