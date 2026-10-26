import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tgdrive.media.media_metadata import get_message


async def test_concurrent_validation_coalesces_but_later_request_is_fresh():
    gate = asyncio.Event()
    calls = 0

    async def fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        await gate.wait()
        return calls

    client = SimpleNamespace(get_messages=fetch)
    tasks = [asyncio.create_task(get_message(client, -1001, 5)) for _ in range(12)]
    await asyncio.sleep(0.01)
    assert calls == 1
    tasks[0].cancel()
    gate.set()
    result = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert result[1:] == [1] * 11
    assert await get_message(client, -1001, 5) == 2


async def test_last_waiter_cancel_stops_orphan_validation():
    stopped = asyncio.Event()

    async def fetch(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    client = SimpleNamespace(get_messages=fetch)
    task = asyncio.create_task(get_message(client, 1, 2))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(stopped.wait(), 1)
    client.get_messages = AsyncMock(return_value=None)
    assert await get_message(client, 1, 2) is None


async def test_new_seek_does_not_join_abandoned_metadata_validation():
    cancelling, finish_cleanup = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelling.set()
                await finish_cleanup.wait()
        return "fresh"

    client = SimpleNamespace(get_messages=fetch)
    abandoned = asyncio.create_task(get_message(client, 1, 2))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    abandoned.cancel()
    await cancelling.wait()
    replacement = asyncio.create_task(get_message(client, 1, 2))
    await asyncio.sleep(0)
    assert not replacement.done()
    finish_cleanup.set()
    assert await replacement == "fresh"
    assert calls == 2
    with pytest.raises(asyncio.CancelledError):
        await abandoned


async def test_distinct_metadata_work_is_bounded_without_rejecting_shared_waiters():
    gate = asyncio.Event()

    async def fetch(*args, **kwargs):
        await gate.wait()
        return kwargs["message_ids"]

    client = SimpleNamespace(
        get_messages=fetch, _identity_pool=SimpleNamespace(_max_media_queue=2)
    )
    tasks = [asyncio.create_task(get_message(client, 1, i)) for i in (1, 2)]
    await asyncio.sleep(0.01)
    from tgdrive.telegram.client_pool import MediaQueueFull

    with pytest.raises(MediaQueueFull):
        await get_message(client, 1, 3)
    shared = asyncio.create_task(get_message(client, 1, 1))
    gate.set()
    assert await asyncio.gather(*tasks, shared) == [1, 2, 1]
    assert await get_message(client, 1, 3) == 3
