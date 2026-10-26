"""Illustrative demo jobs keep moving without touching real transfer lifecycles."""

from collections import deque
from types import SimpleNamespace

import tgdrive.transfers.tasks as tasks
from tests.support.showcase_data import advance_transfers
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus


async def test_showcase_cycles_stages_queue_slots_and_measured_speed(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(tasks.time, "time", lambda: clock.now)
    monkeypatch.setattr(tasks.time, "monotonic", lambda: clock.now)
    registry = TransferRegistry()
    jobs = []
    for phase in ("downloading", "uploading", "queued", "queued"):
        job = await registry.create(phase, total=32 * 1024**2)
        job.context.update(
            showcase_active=True,
            source_kind="remote" if phase == "downloading" else "upload",
        )
        (
            await registry.update(
                job.id,
                status=TransferStatus.QUEUED
                if phase == "queued"
                else TransferStatus.RUNNING,
                current=0,
            )
        )
        jobs.append(job)
    ordinary = await registry.create("uploading", total=100)
    (await registry.update(ordinary.id, status=TransferStatus.RUNNING, current=50))
    pending = deque(job.id for job in jobs[2:])
    seen = {job.id: set() for job in jobs}
    speeds = []
    for tick in range(1, 140):
        clock.now += 2
        (await advance_transfers(registry, [job.id for job in jobs], pending, tick))
        assert sum(job.status == TransferStatus.RUNNING for job in jobs) == 2
        for job in jobs:
            seen[job.id].add((job.status, job.phase))
            assert job.current != job.total - 1
            if speed := job.as_dict()["speed_bytes_per_second"]:
                speeds.append(speed)
                assert speed > 0
                assert job.as_dict()["eta_seconds"] > 0
        assert ordinary.current == 50
    assert speeds
    for phases in seen.values():
        assert (TransferStatus.RUNNING, "uploading") in phases
        assert (TransferStatus.RUNNING, "finalizing") in phases
        assert (TransferStatus.QUEUED, "queued") in phases
    (
        await registry.update(
            jobs[0].id, status=TransferStatus.CANCELLED, phase="cancelled"
        )
    )
    snapshot = jobs[0].as_dict()
    for tick in range(140, 160):
        clock.now += 2
        (await advance_transfers(registry, [job.id for job in jobs], pending, tick))
    assert jobs[0].as_dict() == snapshot
    registry.close()
