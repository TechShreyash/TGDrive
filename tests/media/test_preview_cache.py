from __future__ import annotations

import asyncio

import pytest

from tests.media.test_pooled_media import account
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import BLOCK
from tgdrive.media.preloading import PreloadManager
from tgdrive.media.previews import TEXT_PREVIEW_BYTES, describe_preview
from tgdrive.telegram.client_pool import ClientPool

CHANNEL = -1000000000123


async def setup_preview(tmp_path, *, enabled, gate=None):
    payload = b"line\n" * BLOCK
    client = account("preview-bot", payload, gate)
    pool = ClientPool([client], media_cache_dir=tmp_path)
    await pool.start()
    manager = PreloadManager(
        pool,
        policy=MediaCachePolicy(
            memory_max_bytes=2 * BLOCK if enabled else 0,
            disk_max_bytes=4 * BLOCK if enabled else 0,
            min_free_bytes=0,
            auto_preload=True,
        ),
    )
    return manager, pool, client, payload


async def preview(pool, size):
    return await describe_preview(
        pool=pool,
        channel_id=CHANNEL,
        message_id=77,
        file_name="server.log",
        size=size,
        path="/server.log",
        auth=None,
    )


@pytest.mark.asyncio
async def test_disabled_preview_cache_never_retains_in_transport(tmp_path):
    manager, pool, client, payload = await setup_preview(tmp_path, enabled=False)
    try:
        for _ in range(2):
            result = await preview(pool, len(payload))
            assert result["kind"] == "text"
            assert result["text"] == payload[:TEXT_PREVIEW_BYTES].decode()
            assert result["truncated"]
        assert sum(client.native.requests.values()) == 4
        assert not hasattr(client, "_download_block_cache")
        state = await manager.cache.snapshot()
        assert state["files"] == []
        assert state["memory"]["bytes"] == state["disk"]["bytes"] == 0
        assert state["scratch"]["bytes"] == 0
        assert not manager._observed and not manager._jobs
        assert client.get_messages.await_count == 2  # Warm metadata is never reused.
        assert all(row.active_leases == 0 for row in await pool.status())
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_preview_prefix_appears_in_shared_inventory_without_preload(tmp_path):
    manager, pool, client, payload = await setup_preview(tmp_path, enabled=True)
    try:
        for _ in range(2):
            result = await preview(pool, len(payload))
            assert result["preview_bytes"] == TEXT_PREVIEW_BYTES
            assert result["size"] == len(payload)
        await manager.cache.flush()
        state = await manager.cache.snapshot()
        assert len(state["files"]) == 1
        row = state["files"][0]
        assert row["name"] == "server.log"
        assert row["size"] == len(payload)
        assert row["ready_ranges"] == [[0, BLOCK]]
        assert row["disk_ranges"] == [[0, BLOCK]]
        assert not row["complete"]
        assert sum(client.native.requests.values()) == 2
        assert not hasattr(client, "_download_block_cache")
        assert not manager._observed and not manager._jobs
        assert client.get_messages.await_count == 2
        assert all(iterator.closed for iterator in client.native.iterators)
    finally:
        await manager.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_cancelled_preview_drains_shared_fill_and_reservations(
    tmp_path, monkeypatch
):
    gate = asyncio.Event()
    manager, pool, client, payload = await setup_preview(
        tmp_path, enabled=True, gate=gate
    )
    entered = asyncio.Event()
    make_iterator = client.native.iter_download

    def observe_origin(*args, **kwargs):
        iterator = make_iterator(*args, **kwargs)
        entered.set()
        return iterator

    monkeypatch.setattr(client.native, "iter_download", observe_origin)
    task = asyncio.create_task(preview(pool, len(payload)))
    try:
        # Cache admission can consult the real filesystem in a worker thread.
        # Await actual origin entry rather than a fixed number of loop turns.
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert client.native.active == 0
        assert all(iterator.closed for iterator in client.native.iterators)
        assert not manager.cache.memory.flights
        assert (await manager.cache.snapshot())["scratch"]["bytes"] == 0
        assert all(row.active_leases == 0 for row in await pool.status())
        assert not manager._observed and not manager._jobs
    finally:
        gate.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()
        await pool.stop()
