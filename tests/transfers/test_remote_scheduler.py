"""Offline admission and pacing checks; no network timing benchmarks."""

import asyncio
from contextlib import AsyncExitStack

import pytest

from tgdrive.transfers.remote_scheduler import (
    WARM_BYTES_PER_SECOND,
    URLDownloadScheduler,
)


async def wait(operation):
    return await operation


async def test_two_full_speed_slots_promote_waiting_sources_fifo():
    scheduler = URLDownloadScheduler()
    contexts = [scheduler.admit(wait) for _ in range(4)]
    tickets = [await context.__aenter__() for context in contexts]
    try:
        assert [ticket.full_speed for ticket in tickets] == [True, True, False, False]
        await contexts.pop(0).__aexit__(None, None, None)
        assert [ticket.full_speed for ticket in tickets] == [True, True, True, False]
    finally:
        for context in reversed(contexts):
            await context.__aexit__(None, None, None)
    assert scheduler._tickets == []
    assert scheduler._monitor is None


async def test_storage_eligibility_and_live_settings_control_promotions():
    scheduler = URLDownloadScheduler(1)
    ready = False
    async with AsyncExitStack() as stack:
        first = await stack.enter_async_context(scheduler.admit(wait))
        held = await stack.enter_async_context(scheduler.admit(wait, lambda: ready))
        eligible = await stack.enter_async_context(scheduler.admit(wait))
        await scheduler.configure(2)
        assert first.full_speed and eligible.full_speed and not held.full_speed
        ready = True
        await scheduler.configure(3)
        assert held.full_speed
        await scheduler.configure(1)
        fourth = await stack.enter_async_context(scheduler.admit(wait))
        assert not fourth.full_speed  # Existing full-speed work drains normally.
        assert sum(ticket.full_speed for ticket in scheduler._tickets) == 3


@pytest.mark.parametrize("limit", [0, 9, True, 1.5])
def test_concurrency_is_an_integer_between_one_and_eight(limit):
    with pytest.raises(ValueError):
        URLDownloadScheduler(limit)


async def test_source_connections_are_bounded_and_queued_cancellation_is_clean():
    scheduler = URLDownloadScheduler(1)
    modes = []
    async with AsyncExitStack() as stack:
        for _ in range(9):
            await stack.enter_async_context(scheduler.admit(wait))
        entered = asyncio.Event()

        async def extra():
            async with scheduler.admit(wait, changed=modes.append):
                entered.set()

        pending = asyncio.create_task(extra())
        try:
            await asyncio.sleep(0)
            assert modes == ["queued"]
            assert not entered.is_set()
            assert len(scheduler._tickets) == 9
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert len(scheduler._tickets) == 9
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_waiting_reads_are_paced_and_promotion_wakes_them(monkeypatch):
    scheduler = URLDownloadScheduler(1)
    async with scheduler.admit(wait, lambda: False) as ticket:
        monkeypatch.setattr(
            "tgdrive.transfers.remote_scheduler.time.monotonic", lambda: 10
        )
        ticket.received(WARM_BYTES_PER_SECOND)
        assert ticket.next_read == 11
        waiting = asyncio.create_task(ticket.pace())
        await asyncio.sleep(0)
        assert not waiting.done()
        ticket.can_promote = lambda: True
        await scheduler.refresh()
        await asyncio.wait_for(waiting, 1)
        assert ticket.full_speed
