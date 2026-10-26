from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import dataclass

from tgdrive.telegram.client_pool import (
    ClientPool,
    ClientPoolStartError,
    NoPremiumClientAvailable,
    PoolState,
)


@dataclass
class StartTracker:
    active: int = 0
    maximum: int = 0


class FakeClient:
    def __init__(
        self,
        name: str,
        *,
        fail_start: bool = False,
        tracker: StartTracker | None = None,
    ) -> None:
        self.name = name
        self.fail_start = fail_start
        self.tracker = tracker
        self.start_calls = 0
        self.stop_calls = 0
        self.is_connected = False

    async def start(self) -> FakeClient:
        self.start_calls += 1
        if self.tracker is not None:
            self.tracker.active += 1
            self.tracker.maximum = max(self.tracker.maximum, self.tracker.active)
        try:
            await asyncio.sleep(0)
            if self.fail_start:
                raise RuntimeError("synthetic start failure")
            self.is_connected = True
            return self
        finally:
            if self.tracker is not None:
                self.tracker.active -= 1

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_connected = False
        await asyncio.sleep(0)


class ClientPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_clients_and_async_factories_start_concurrently(self) -> None:
        tracker = StartTracker()
        first = FakeClient("first", tracker=tracker)
        second = FakeClient("second", tracker=tracker)
        factory_calls = 0

        async def second_factory() -> FakeClient:
            nonlocal factory_calls
            factory_calls += 1
            return second

        pool = ClientPool([first, second_factory])
        await pool.start()

        self.assertEqual(factory_calls, 1)
        self.assertEqual(tracker.maximum, 2)
        self.assertEqual(pool.state, PoolState.RUNNING)
        await pool.stop()

    async def test_partial_start_failure_stops_every_materialized_client(self) -> None:
        healthy = FakeClient("healthy")
        broken = FakeClient("broken", fail_start=True)
        pool = ClientPool([healthy, broken])

        with self.assertRaises(ClientPoolStartError) as raised:
            await pool.start()

        self.assertEqual(pool.state, PoolState.FAILED)
        self.assertEqual(healthy.stop_calls, 1)
        self.assertEqual(broken.stop_calls, 1)
        self.assertEqual(raised.exception.failures[0][0], "broken")

        await pool.stop()
        self.assertEqual(healthy.stop_calls, 1)
        self.assertEqual(broken.stop_calls, 1)

    async def test_leases_prefer_least_active_and_release_in_finally(self) -> None:
        first = FakeClient("first")
        second = FakeClient("second")
        pool = ClientPool([first, second])
        await pool.start()

        with self.assertRaisesRegex(RuntimeError, "lease body"):
            async with pool.lease() as first_lease, pool.lease() as second_lease:
                self.assertIs(first_lease, first)
                self.assertIs(second_lease, second)
                raise RuntimeError("lease body failed")

        status = await pool.status()
        self.assertEqual([item.active_leases for item in status], [0, 0])
        async with pool.lease() as next_lease:
            self.assertIs(next_lease, first)
        await pool.stop()

    async def test_missing_premium_client_has_clear_error(self) -> None:
        pool = ClientPool([FakeClient("bot")])
        await pool.start()

        with self.assertRaisesRegex(
            NoPremiumClientAvailable,
            "no premium user clients are configured",
        ):
            async with pool.lease(premium=True):
                self.fail("lease should not be entered")
        await pool.stop()

    async def test_premium_lease_uses_user_clients_only(self) -> None:
        bot = FakeClient("bot")
        user = FakeClient("premium")
        pool = ClientPool([bot], [user])
        await pool.start()

        async with pool.lease(premium_required=True) as leased:
            self.assertIs(leased, user)
        await pool.stop()

    async def test_stop_drains_active_lease_and_is_idempotent(self) -> None:
        client = FakeClient("bot")
        pool = ClientPool([client])
        await pool.start()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_lease() -> None:
            async with pool.lease():
                entered.set()
                await release.wait()

        holder = asyncio.create_task(hold_lease())
        await entered.wait()
        stopping = asyncio.create_task(pool.stop())
        await asyncio.sleep(0)

        self.assertFalse(stopping.done())
        self.assertEqual(client.stop_calls, 0)
        release.set()
        await holder
        self.assertEqual(await stopping, ())
        self.assertEqual(client.stop_calls, 1)
        self.assertEqual(pool.state, PoolState.STOPPED)

        self.assertEqual(await pool.stop(), ())
        self.assertEqual(client.stop_calls, 1)

    async def test_health_and_manual_health_marking(self) -> None:
        bot = FakeClient("bot")
        user = FakeClient("user")
        pool = ClientPool([bot], [user])
        await pool.start()

        health = await pool.health()
        self.assertTrue(health.ready)
        self.assertEqual(health.bot_clients, 1)
        self.assertEqual(health.premium_clients, 1)

        await pool.mark_unhealthy(bot, RuntimeError("do not expose this detail"))
        status = await pool.status()
        self.assertFalse(status[0].healthy)
        self.assertEqual(status[0].last_error_type, "RuntimeError")
        self.assertNotIn("do not expose", repr(status[0]))
        self.assertFalse((await pool.health()).ready)

        await pool.mark_healthy(bot)
        self.assertTrue((await pool.health()).ready)
        await pool.stop()

    async def test_preferred_premium_uses_healthy_user_before_less_busy_bot(
        self,
    ) -> None:
        bot = FakeClient("bot")
        first = FakeClient("premium-one")
        second = FakeClient("premium-two")
        pool = ClientPool([bot], [first, second])
        await pool.start()

        async with (
            pool.lease(prefer_premium=True) as one,
            pool.lease(prefer_premium=True) as two,
        ):
            self.assertIs(one, first)
            self.assertIs(two, second)
            self.assertEqual(pool.kind_of(one), "user")
        self.assertEqual((await pool.health()).active_leases, 0)
        await pool.stop()

    async def test_preferred_premium_falls_back_for_missing_or_unhealthy_users(
        self,
    ) -> None:
        bot = FakeClient("bot")
        user = FakeClient("premium")
        pool = ClientPool([bot], [user])
        await pool.start()

        await pool.mark_unhealthy(user, ConnectionError("unavailable"))
        async with pool.lease(prefer_premium=True) as leased:
            self.assertIs(leased, bot)
        await pool.mark_healthy(user)
        user.is_connected = False
        async with pool.lease(prefer_premium=True) as leased:
            self.assertIs(leased, bot)
        self.assertEqual((await pool.health()).active_leases, 0)
        await pool.stop()

        pool = ClientPool([bot])
        await pool.start()
        async with pool.lease(prefer_premium=True) as leased:
            self.assertIs(leased, bot)
        await pool.stop()

    async def test_preference_does_not_weaken_required_premium_lease(self) -> None:
        pool = ClientPool([FakeClient("bot")])
        await pool.start()
        with self.assertRaises(NoPremiumClientAvailable):
            async with pool.lease(premium=True, prefer_premium=True):
                self.fail("a bot cannot satisfy a required user lease")
        await pool.stop()

    async def test_media_prefers_premium_then_falls_back_without_changing_sender(self):
        bot, user = FakeClient("bot"), FakeClient("premium")
        pool = ClientPool([bot], [user])
        await pool.start()
        try:
            options = dict(media=True, allow_users=True, prefer_premium=True)
            async with pool.lease(**options) as first:
                self.assertIs(first, user)
                async with pool.lease(**options) as second:
                    self.assertIs(second, bot)
            # A recorded bot sender wins even while Premium is available.
            async with pool.lease(**options, client_name=bot.name) as fixed:
                self.assertIs(fixed, bot)
            await pool.defer_client(user.name, time.time() + 60)
            async with pool.lease(**options) as cooling_fallback:
                self.assertIs(cooling_fallback, bot)
            self.assertEqual((await pool.health()).active_leases, 0)
        finally:
            await pool.stop()

    async def test_partial_start_keeps_healthy_bot_and_cleans_failed_accounts(
        self,
    ) -> None:
        bot = FakeClient("healthy-bot")
        bad_bot = FakeClient("failed-bot", fail_start=True)
        premium = FakeClient("failed-premium", fail_start=True)
        pool = ClientPool([bot, bad_bot], [premium], allow_partial_start=True)
        await pool.start()

        self.assertEqual(pool.state, PoolState.RUNNING)
        self.assertEqual(bot.stop_calls, 0)
        self.assertEqual(bad_bot.stop_calls, 1)
        self.assertEqual(premium.stop_calls, 1)
        health = await pool.health()
        self.assertTrue(health.ready)
        self.assertEqual(health.healthy_clients, 1)
        self.assertEqual(health.total_clients, 3)
        async with pool.lease(prefer_premium=True) as leased:
            self.assertIs(leased, bot)
        await pool.stop()
        self.assertEqual(bot.stop_calls, 1)
        self.assertEqual(bad_bot.stop_calls, 1)
        self.assertEqual(premium.stop_calls, 1)

    async def test_partial_start_contains_invalid_optional_session_factory(
        self,
    ) -> None:
        def invalid_session() -> FakeClient:
            raise ValueError("synthetic malformed session")

        pool = ClientPool(
            [FakeClient("bot")], [invalid_session], allow_partial_start=True
        )
        await pool.start()
        self.assertTrue((await pool.health()).ready)
        status = await pool.status()
        self.assertFalse(status[1].started)
        self.assertEqual(status[1].last_error_type, "ValueError")
        await pool.stop()

    async def test_partial_start_fails_without_any_healthy_bot(self) -> None:
        bot = FakeClient("failed-bot", fail_start=True)
        user = FakeClient("healthy-user")
        pool = ClientPool([bot], [user], allow_partial_start=True)

        with self.assertRaises(ClientPoolStartError):
            await pool.start()
        self.assertEqual(pool.state, PoolState.FAILED)
        self.assertEqual(bot.stop_calls, 1)
        self.assertEqual(user.stop_calls, 1)
        self.assertFalse((await pool.health()).ready)
        await pool.stop()


if __name__ == "__main__":
    unittest.main()
