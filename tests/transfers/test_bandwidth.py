import asyncio

import pytest

from tgdrive.transfers.bandwidth import (
    FileBandwidth,
    PayloadBudget,
    payload_timeout,
    provider_timeout,
)


@pytest.mark.asyncio
async def test_live_limit_change_wakes_waiters_without_an_idle_burst():
    budget = PayloadBudget()
    await budget.configure(1024)
    first = asyncio.create_task(budget.acquire(65536))
    second = asyncio.create_task(budget.acquire(65536))
    await asyncio.sleep(0)
    assert budget.snapshot()["waiting"] == 2
    assert budget.admitted_bytes == 0
    await budget.configure(None)
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert budget.snapshot()["waiting"] == 0
    assert budget.admitted_bytes == 131072


@pytest.mark.asyncio
async def test_cancelled_head_does_not_block_followers_or_admit_its_bytes():
    budget = PayloadBudget()
    await budget.configure(1024)
    first = asyncio.create_task(budget.acquire(65536))
    second = asyncio.create_task(budget.acquire(1))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, 1)
    assert budget.admitted_bytes == 1
    assert budget.snapshot()["waiting"] == 0


@pytest.mark.asyncio
async def test_finite_budget_interleaves_consumers_and_counts_partial_cancellation():
    now = [0.0]
    budget = PayloadBudget(clock=lambda: now[0])
    await budget.configure(1024)
    first = asyncio.create_task(budget.acquire(1024))
    second = asyncio.create_task(budget.acquire(102))
    await asyncio.sleep(0)
    now[0] = 0.1
    await budget.configure(1024)
    await asyncio.sleep(0)
    assert budget.admitted_bytes == 102
    now[0] = 0.2
    await budget.configure(1024)
    await asyncio.sleep(0)
    await asyncio.wait_for(second, 1)
    assert not first.done()
    assert budget.admitted_bytes == 204
    assert budget.admitted_bytes <= 1024 * now[0]
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert budget.admitted_bytes == 204
    assert budget.snapshot()["waiting"] == 0


@pytest.mark.asyncio
async def test_finite_rate_change_preserves_work_already_admitted_at_old_rate():
    now = [0.0]
    budget = PayloadBudget(clock=lambda: now[0])
    await budget.configure(1024)
    task = asyncio.create_task(budget.acquire(100))
    await asyncio.sleep(0)
    now[0] += 0.05  # 51.2 bytes at the old rate.
    await budget.configure(2048)
    await asyncio.sleep(0)
    assert budget.admitted_bytes == 0
    now[0] += 0.024  # 49.152 further bytes at the new rate.
    await budget.configure(2048)
    await asyncio.wait_for(task, 1)
    assert budget.admitted_bytes == 100


@pytest.mark.asyncio
async def test_failed_native_stop_does_not_return_its_allocation_or_admit_new_payload():
    limits = FileBandwidth()
    await limits.configure(2048, 2048)
    engine = Engine([])
    context = limits.native(engine)
    await context.__aenter__()
    engine.fail = True

    async def cannot_stop():
        raise OSError("Process termination failed")

    engine.stop_for_bandwidth = cannot_stop
    with pytest.raises(OSError, match="termination failed"):
        await limits.configure(1024, 1024)
    assert limits.snapshot()["native_engines"] == 1
    assert limits.upload.rate == limits.download.rate == 512
    with pytest.raises(OSError, match="termination failed"):
        await context.__aexit__(None, None, None)
    assert limits.snapshot()["native_engines"] == 1


@pytest.mark.asyncio
async def test_stalled_provider_stays_bounded_while_another_part_is_throttled():
    budget = PayloadBudget()
    await budget.configure(1024)
    waiting = None
    try:
        with pytest.raises(TimeoutError):
            async with payload_timeout(0.02):
                waiting = asyncio.create_task(budget.acquire(65536))
                await asyncio.sleep(0)
                async with provider_timeout():
                    await asyncio.Event().wait()
    finally:
        if waiting is not None:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    assert budget.snapshot()["waiting"] == 0
    async with provider_timeout() as deadline:
        assert deadline.when() is None


class Engine:
    def __init__(self, events):
        self.events = events
        self.upload = self.download = None
        self.stopped = False
        self.fail = False

    async def set_bandwidth(self, upload, download):
        self.events.append((self, upload, download))
        if self.fail:
            raise OSError("RPC unavailable")
        self.upload, self.download = upload, download

    async def stop_for_bandwidth(self):
        self.events.append((self, "stop"))
        self.stopped = True


@pytest.mark.asyncio
async def test_native_processes_share_both_caps_and_stop_before_capacity_returns():
    limits = FileBandwidth()
    await limits.configure(1024, 2048)
    events = []
    first, second = Engine(events), Engine(events)
    async with limits.native(first):
        assert first.upload == limits.upload.rate == 512
        assert first.download == limits.download.rate == 1024
        async with limits.native(second):
            assert first.upload == second.upload == 341
            assert limits.upload.rate == 342
            assert first.download == second.download == 682
            assert limits.download.rate == 684
        assert second.stopped
        assert events.index((second, "stop")) < events.index((first, 512, 1024), 1)
        assert limits.upload.rate == first.upload == 512
    assert first.stopped
    assert limits.upload.rate == 1024
    assert limits.download.rate == 2048
    assert limits.snapshot()["native_engines"] == 0


@pytest.mark.asyncio
async def test_limit_apply_failure_stops_only_failed_owned_engine():
    limits = FileBandwidth()
    await limits.configure(4096, None)
    events = []
    first, second = Engine(events), Engine(events)
    async with limits.native(first), limits.native(second):
        first.fail = True
        await limits.configure(1024, 2048)
        assert first.stopped
        assert not second.stopped
        assert isinstance(limits.upload.rate, int) and isinstance(second.upload, int)
        assert isinstance(limits.download.rate, int) and isinstance(
            second.download, int
        )
        assert limits.upload.rate + second.upload == 1024
        assert limits.download.rate + second.download == 2048
        assert limits.snapshot()["native_engines"] == 1


@pytest.mark.asyncio
async def test_cancellation_while_rebalancing_does_not_leak_registration():
    limits = FileBandwidth()
    await limits.configure(1024, None)
    started = asyncio.Event()
    engine = Engine([])

    async def stall(upload, download):
        started.set()
        await asyncio.Event().wait()

    engine.set_bandwidth = stall

    async def use():
        async with limits.native(engine):
            pytest.fail("Cancelled registration must not dispatch payload")

    task = asyncio.create_task(use())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert engine.stopped
    assert limits.snapshot()["native_engines"] == 0
    assert limits.upload.rate == 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [0, -1, True, 1, 1023, 1.5, 2**53])
async def test_invalid_owner_rates_are_rejected_before_live_changes(rate):
    limits = FileBandwidth()
    with pytest.raises(ValueError):
        await limits.configure(rate, None)
    assert limits.snapshot()["upload"]["configured_bytes_per_second"] is None
