"""Live client changes: add, remove, repair and retry without a restart."""

import asyncio

import pytest

from tests.telegram.test_client_pool import FakeClient
from tgdrive.telegram.client_pool import ClientPool, ClientPoolStartError


def entry(key, client, identity=None):
    return (key, identity, lambda: client)


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_added_client_starts_and_unchanged_client_keeps_running():
    first, second = FakeClient("bot-a"), FakeClient("bot-b")
    pool = ClientPool([entry("a", first)])
    await pool.start()
    async with pool.lease() as leased:
        await pool.reconfigure([entry("a", FakeClient("other")), entry("b", second)])
        assert leased is first and first.stop_calls == 0
    assert first.start_calls == 1 and second.start_calls == 1
    assert [row.name for row in await pool.status()] == ["bot-a", "bot-b"]
    async with pool.lease(client_name="bot-b") as leased:
        assert leased is second
    await pool.stop()


@pytest.mark.asyncio
async def test_removed_client_finishes_its_lease_before_disconnecting():
    first, second = FakeClient("bot-a"), FakeClient("bot-b")
    pool = ClientPool([entry("a", first), entry("b", second)])
    await pool.start()
    async with pool.lease(client_name="bot-b") as leased:
        await pool.reconfigure([entry("a", first)])
        await settle()
        assert leased is second and second.stop_calls == 0
        assert [row.name for row in await pool.status()] == ["bot-a"]
    await settle()
    assert second.stop_calls == 1 and first.stop_calls == 0
    await pool.stop()


@pytest.mark.asyncio
async def test_same_bot_with_new_token_waits_for_the_old_session():
    old, new = FakeClient("storage-bot-1"), FakeClient("storage-bot-1")
    pool = ClientPool([entry("old", old, "storage-bot-1")])
    await pool.start()
    async with pool.lease() as leased:
        await pool.reconfigure([entry("new", new, "storage-bot-1")])
        await settle()
        # Both clients share one session file, so the new one waits.
        assert leased is old and new.start_calls == 0
        waiter = asyncio.create_task(_lease_name(pool, "storage-bot-1"))
        await settle()
        assert not waiter.done()
    assert await asyncio.wait_for(waiter, 1) is new
    assert old.stop_calls == 1
    await pool.stop()


async def _lease_name(pool, name):
    async with pool.lease(client_name=name) as client:
        return client


async def test_superseded_replacement_never_starts_after_held_lease():
    old, superseded, latest = [FakeClient("storage-bot-1") for _ in range(3)]
    pool = ClientPool([entry("a", old, "storage-bot-1")])
    await pool.start()
    async with pool.lease():
        await pool.reconfigure([entry("b", superseded, "storage-bot-1")])
        await pool.reconfigure([entry("c", latest, "storage-bot-1")])
        await pool.retry_failed()
        await settle()
        assert superseded.start_calls == latest.start_calls == 0
    assert await asyncio.wait_for(_lease_name(pool, "storage-bot-1"), 1) is latest
    assert superseded.start_calls == 0
    await pool.stop()
    assert old.stop_calls == latest.stop_calls == 1


async def test_replacement_waits_until_failed_disconnect_retries_successfully():
    class FlakyDisconnect(FakeClient):
        async def stop(self):
            self.stop_calls += 1
            if self.stop_calls == 1:
                raise ConnectionError("disconnect failed")

    old, new = FlakyDisconnect("storage-bot-1"), FakeClient("storage-bot-1")
    pool = ClientPool([entry("old", old, "storage-bot-1")])
    await pool.start()
    await pool.reconfigure([entry("new", new, "storage-bot-1")])
    await settle()
    assert old.stop_calls == 1 and new.start_calls == 0
    await pool.retry_failed()
    assert await asyncio.wait_for(_lease_name(pool, "storage-bot-1"), 1) is new
    assert old.stop_calls == 2
    await pool.stop()


@pytest.mark.asyncio
async def test_failed_new_client_reports_error_and_retries():
    good, bad = FakeClient("bot-a"), FakeClient("bot-b", fail_start=True)
    pool = ClientPool([entry("a", good)], allow_partial_start=True)
    await pool.start()
    await pool.reconfigure([entry("a", good), entry("b", bad)])
    rows = await pool.status()
    assert not rows[1].started and rows[1].last_error_type == "RuntimeError"
    bad.fail_start = False
    await pool.retry_failed()
    assert (await pool.status())[1].healthy and good.start_calls == 1
    await pool.stop()
    # The failed attempt was disconnected too, so it stops twice in total.
    assert good.stop_calls == 1 and bad.stop_calls == 2


@pytest.mark.asyncio
async def test_failed_pool_starts_once_its_credentials_are_repaired():
    broken, fixed = FakeClient("bot-a", fail_start=True), FakeClient("bot-a")
    pool = ClientPool([entry("old", broken)], allow_partial_start=True)
    with pytest.raises(ClientPoolStartError):
        await pool.start()
    await pool.reconfigure([entry("new", fixed)])
    async with pool.lease() as leased:
        assert leased is fixed
    await pool.stop()
