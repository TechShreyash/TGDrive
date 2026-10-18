import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tgdrive.media_metadata import get_message


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
