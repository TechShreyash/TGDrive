import asyncio

import pytest

from tgdrive.storage import pacing


@pytest.mark.asyncio
async def test_pacer_spaces_operations_and_never_bursts(monkeypatch):
    clock = [100.0]
    slept = []

    async def sleep(seconds):
        slept.append(round(seconds, 6))
        clock[0] += seconds

    monkeypatch.setattr(pacing.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pacing.asyncio, "sleep", sleep)
    pacer = pacing.Pacer(10)  # 10 ops/s: one every 0.1 s.
    await pacer.wait()  # First one goes at once.
    await pacer.wait(2)  # Waits for the first slot; reserves two.
    await pacer.wait()
    assert slept == [0.1, 0.2]
    clock[0] += 5  # Idle time is not banked as a burst.
    await pacer.wait()
    await pacer.wait()
    assert slept[-1] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_background_budget_stays_well_under_the_free_tier():
    started = asyncio.get_running_loop().time()
    pacer = pacing.Pacer(pacing.BACKGROUND_OPERATIONS_PER_SECOND)
    for _ in range(10):
        await pacer.wait(2)
    elapsed = asyncio.get_running_loop().time() - started
    # The 10th wait starts after 9 earlier waits of 2 operations each: 18/30 s.
    assert elapsed >= 18 / pacing.BACKGROUND_OPERATIONS_PER_SECOND - 0.01
    assert pacing.BACKGROUND_OPERATIONS_PER_SECOND <= 50
