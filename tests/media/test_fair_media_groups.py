"""Queued fair-group draft; fresh owner approval is required before execution."""

import asyncio
import time

import pytest

from tests.media.test_media_scheduler import Account, wait_until
from tgdrive.media.media_reader import BLOCK, _ScratchBudget
from tgdrive.media.media_scheduler import (
    MediaFairScope,
    MediaScheduler,
    current_fair_group,
    media_fair_scope,
)
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.bandwidth import PayloadBudget


async def grouped(group, operation):
    token = media_fair_scope.set(MediaFairScope(group))
    try:
        return await operation()
    finally:
        media_fair_scope.reset(token)


@pytest.mark.asyncio
async def test_parallel_request_ids_for_one_file_do_not_gain_pool_turns():
    pool = ClientPool([Account()], media_per_bot=1)
    await pool.start()
    tasks, order = [], []

    async def read(label):
        async with pool.lease(download=True, request_id=label):
            order.append(label)

    token = media_fair_scope.set(MediaFairScope("private-group-a"))
    try:
        async with pool.lease(download=True, request_id="active-original"):
            for label, group in [
                ("a1", "private-group-a"),
                ("a2", "private-group-a"),
                ("a3", "private-group-a"),
                ("b", "private-group-b"),
            ]:
                tasks.append(
                    asyncio.create_task(grouped(group, lambda label=label: read(label)))
                )
                await wait_until(lambda: len(pool._origin_waiters) == len(tasks))
            tasks[1].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[1]
            snapshot = repr(pool.performance_snapshot())
            assert "private-group" not in snapshot
            assert "a1" in snapshot and "b" in snapshot
        await asyncio.wait_for(asyncio.gather(tasks[0], tasks[2], tasks[3]), 2)
        assert order == ["b", "a1", "a3"]
        assert not pool._origin_waiters and not pool._origin_active
    finally:
        media_fair_scope.reset(token)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.stop()
    assert current_fair_group() is None


@pytest.mark.asyncio
async def test_account_rpc_groups_rotate_without_preempting_the_active_request():
    scheduler = MediaScheduler(limit=1, rate=512)
    tasks, order = [], []

    async def read(label):
        async with scheduler.request():
            order.append(label)

    token = media_fair_scope.set(MediaFairScope("a"))
    try:
        async with scheduler.request():
            for label, group in [("a1", "a"), ("a2", "a"), ("b", "b")]:
                tasks.append(
                    asyncio.create_task(grouped(group, lambda label=label: read(label)))
                )
                await wait_until(lambda: len(scheduler._waiting) == len(tasks))
            assert not order
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert order == ["b", "a1", "a2"]
        assert not scheduler._active_groups
    finally:
        media_fair_scope.reset(token)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_scratch_round_robin_limits_many_connections_to_one_group_turn():
    scratch = _ScratchBudget(2 * BLOCK)
    assert scratch.try_acquire() and scratch.try_acquire()
    tasks, order = [], []

    async def head(label):
        await scratch.acquire()
        try:
            order.append(label)
        finally:
            scratch.release()

    try:
        for label, group in [("a1", "a"), ("a2", "a"), ("a3", "a"), ("b", "b")]:
            tasks.append(
                asyncio.create_task(grouped(group, lambda label=label: head(label)))
            )
            await wait_until(lambda: scratch.waiters == len(tasks))
        scratch.release()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert order[:2] == ["a1", "b"]
        assert order[2:] == ["a2", "a3"]
        assert scratch.used == 1
        assert not scratch._heads and not scratch._head_groups
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        while scratch.used:
            scratch.release()


@pytest.mark.asyncio
async def test_cached_byte_quanta_rotate_groups_and_preserve_live_cancel_accounting():
    now = [0.0]
    budget = PayloadBudget(clock=lambda: now[0])
    await budget.configure(1024)
    order, tasks = [], []

    async def take(label):
        await budget.acquire(102)
        order.append(label)

    try:
        for label, group in [("a1", "a"), ("a2", "a"), ("a3", "a"), ("b", "b")]:
            tasks.append(
                asyncio.create_task(grouped(group, lambda label=label: take(label)))
            )
            await asyncio.sleep(0)
        now[0] = 0.1
        await budget.configure(1024)
        await wait_until(lambda: order == ["a1"])
        now[0] = 0.2
        await budget.configure(1024)
        await wait_until(lambda: len(order) == 2)
        assert order == ["a1", "b"]
        assert budget.admitted_bytes == 204
        assert budget.admitted_bytes <= now[0] * 1024
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        await budget.configure(None)
        await asyncio.gather(tasks[0], tasks[2], tasks[3])
        assert order == ["a1", "b", "a3"]
        assert budget.admitted_bytes == 306
        assert not budget._queue
        assert "private-group" not in repr(budget.snapshot())
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_group_cancel_preserves_account_cooldown_and_compatible_lane_bypass():
    bot, user = Account(), Account()
    bot.name, user.name = "bot", "user"
    pool = ClientPool([bot], [user], media_per_bot=1)
    await pool.start()
    blocked = None
    try:
        async with pool.lease(premium=True, download=True):
            blocked = asyncio.create_task(
                grouped(
                    "premium",
                    lambda: pool.lease(premium=True, download=True).__aenter__(),
                )
            )
            await wait_until(lambda: len(pool._origin_waiters) == 1)

            async def ready_bot():
                async with pool.lease(download=True) as ready:
                    assert ready is bot

            await asyncio.wait_for(grouped("new-viewer", ready_bot), 1)
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked
        await pool.defer_client("bot", time.time() + 60)
        blocked = asyncio.create_task(
            grouped("cancelled-viewer", lambda: pool.lease(download=True).__aenter__())
        )
        await wait_until(lambda: len(pool._origin_waiters) == 1)
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        assert not pool._origin_waiters and not pool._origin_active
        assert (
            next(slot for slot in pool._slots if slot.name == "bot").cooldown_until
            > time.time() + 59
        )
    finally:
        if blocked is not None:
            blocked.cancel()
            await asyncio.gather(blocked, return_exceptions=True)
        await pool.stop()
