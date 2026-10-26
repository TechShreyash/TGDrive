from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import tgdrive.transfers.tasks as tasks
from tests.support.mongo_fixtures import FakeMongoBackend
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    value = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(tasks.time, "time", lambda: value.now)
    monkeypatch.setattr(tasks.time, "monotonic", lambda: value.now)
    return value


async def running(registry: TransferRegistry):
    record = await registry.create("queued", total=100)
    record.created_at = 1000.0
    await registry.update(
        record.id, status=TransferStatus.RUNNING, phase="uploading", current=0
    )
    return record


async def test_metrics_are_stage_specific_stale_bounded_and_terminal_frozen(clock):
    registry = TransferRegistry()
    record = await running(registry)
    clock.now += 2
    await registry.update(record.id, current=50)
    snapshot = record.as_dict()
    assert snapshot["speed_bytes_per_second"] == 25
    assert snapshot["eta_seconds"] == 2
    assert snapshot["elapsed_seconds"] == 2
    assert snapshot["started_at"] == 1000
    assert snapshot["progress_updated_at"] == 1002
    clock.now += 16
    assert record.as_dict()["speed_bytes_per_second"] is None
    await registry.update(record.id, phase="finalizing", current=100)
    assert record.as_dict()["eta_seconds"] is None
    await registry.update(record.id, status=TransferStatus.COMPLETED, phase="completed")
    finished = record.as_dict()
    clock.now += 60
    assert record.as_dict() == finished
    assert finished["finished_at"] == 1018


async def test_metrics_restart_when_phase_total_or_counter_changes(clock):
    registry = TransferRegistry()
    record = await running(registry)
    clock.now += 1
    await registry.update(record.id, current=50)
    assert record.as_dict()["speed_bytes_per_second"] == 50
    await registry.update(record.id, phase="downloading", current=0)
    assert record.as_dict()["speed_bytes_per_second"] is None
    clock.now += 1
    await registry.update(record.id, current=25)
    assert record.as_dict()["speed_bytes_per_second"] == 25
    await registry.update(record.id, total=200)
    assert record.as_dict()["speed_bytes_per_second"] is None
    clock.now += 1
    await registry.update(record.id, current=100)
    await registry.update(record.id, current=50)
    assert record.as_dict()["speed_bytes_per_second"] is None
    record.context["kind"] = "delete"
    clock.now += 1
    await registry.update(record.id, current=75)
    assert record.as_dict()["speed_bytes_per_second"] is None


async def test_progress_is_visible_without_per_chunk_commits_and_context_flushes(clock):
    backend = FakeMongoBackend()
    registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    record = await running(registry)
    backend.write = Mock(wraps=backend.write)
    for current in range(1, 50):
        await registry.update(record.id, current=current)
    assert record.as_dict()["current"] == 49
    assert backend.write.call_count == 0
    assert backend.db.jobs.find_one({"_id": record.id})["data"]["current"] == 0
    record.context.update(sender="stable-sender", random_id=123, send_started=True)
    await registry.update(record.id, current=50)
    assert backend.write.call_count == 1
    clock.now += 0.51
    await registry.update(record.id, current=75)
    assert record.current == 75
    assert backend.write.call_count == 1
    assert backend.db.jobs.find_one({"_id": record.id})["data"]["current"] == 50
    await registry.update(
        record.id, status=TransferStatus.COMPLETED, phase="completed", current=100
    )
    assert backend.write.call_count == 2
    finished = record.finished_at
    registry.close()
    reopened = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    restored = reopened.get(record.id)
    assert restored.current == 100
    assert restored.started_at == 1000 and restored.finished_at == finished
    assert restored.as_dict()["speed_bytes_per_second"] is None
    assert restored.context["random_id"] == 123
    reopened.close()


async def test_terminal_job_without_known_start_does_not_invent_elapsed(clock):
    backend = FakeMongoBackend()
    registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    record = await registry.create("failed")
    clock.now = 1010
    await registry.update(
        record.id,
        phase="failed",
        status=TransferStatus.ERROR,
        error="offline",
    )
    registry.close()
    reopened = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    assert reopened.get(record.id).as_dict()["elapsed_seconds"] == 0
    reopened.close()


async def test_url_byte_progress_avoids_commits_but_keeps_recovery_changes_durable(
    clock, monkeypatch
):
    backend = FakeMongoBackend()
    registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    record = await registry.create("downloading", total=100)
    record.context.update(kind="remote", staging_size=100, staging_written=0)
    await registry.update(record.id, status=TransferStatus.RUNNING, current=0)
    persist = Mock(wraps=backend.write)
    monkeypatch.setattr(backend, "write", persist)

    for current in range(1, 50):
        clock.now += 1  # Beyond the normal progress checkpoint interval.
        record.context["staging_written"] = current
        await registry.update(record.id, current=current, checkpoint_progress=False)
    assert persist.call_count == 0
    assert record.as_dict()["current"] == 49
    assert record.as_dict()["speed_bytes_per_second"] == 1
    assert registry._durable[record.id]["context"]["staging_written"] == 0

    # Reservation and dispatch identities still commit immediately, even when
    # advisory byte checkpoints are disabled by the URL pipeline.
    record.context.update(staging_size=200, sender="bot", random_id=123)
    await registry.update(record.id, current=50, checkpoint_progress=False)
    assert persist.call_count == 1
    assert registry._durable[record.id]["context"]["random_id"] == 123
    await registry.update(
        record.id,
        status=TransferStatus.ERROR,
        error="connection interrupted",
        checkpoint_progress=False,
    )
    assert persist.call_count == 2
    assert registry._durable[record.id]["current"] == 50
    assert registry._durable[record.id]["context"]["staging_written"] == 49
    registry.close()


async def test_unknown_length_reports_rate_without_inventing_an_eta(clock):
    registry = TransferRegistry()
    record = await registry.create("queued")
    await registry.update(
        record.id,
        status=TransferStatus.RUNNING,
        phase="downloading",
        current=0,
        total=0,
    )
    clock.now += 2
    await registry.update(record.id, current=50, total=0)
    snapshot = record.as_dict()
    assert snapshot["speed_bytes_per_second"] == 25
    assert snapshot["eta_seconds"] is None


async def test_waiting_for_capacity_does_not_start_elapsed_and_completion_freezes_it(
    clock,
):
    backend = FakeMongoBackend()
    registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    record = await registry.create("queued", total=100)
    clock.now += 60
    assert record.as_dict()["elapsed_seconds"] == 0
    assert record.as_dict()["started_at"] is None
    registry.close()
    registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
    record = registry.get(record.id)
    assert record.as_dict()["elapsed_seconds"] == 0
    await registry.update(
        record.id, status=TransferStatus.RUNNING, phase="uploading", current=0
    )
    clock.now += 2
    await registry.update(record.id, current=50)
    assert record.as_dict()["elapsed_seconds"] == 2
    await registry.update(
        record.id, status=TransferStatus.COMPLETED, phase="completed", current=100
    )
    clock.now += 120
    assert record.as_dict()["elapsed_seconds"] == 2
    registry.close()


async def test_cancel_before_start_has_no_elapsed_transfer_time(clock):
    registry = TransferRegistry()
    record = await registry.create("queued", total=100)
    clock.now += 60
    await registry.cancel(record.id)
    assert record.as_dict()["elapsed_seconds"] == 0
    assert record.as_dict()["speed_bytes_per_second"] is None
    assert record.as_dict()["eta_seconds"] is None


@pytest.mark.parametrize(
    "context",
    [
        {"retry_at": 1100.0},
        {"channel_retry_at": 1100.0},
        {"multipart_plan": {"parts": [{"sender": "bot-one", "retry_at": 1100.0}]}},
    ],
)
async def test_terminal_jobs_retain_provider_deadlines_until_they_expire(
    clock, context
):
    registry = TransferRegistry(ttl_seconds=1, max_entries=1)
    record = await registry.create("queued")
    record.context.update(context)
    await registry.cancel(record.id)
    clock.now = 1010.0
    await registry.prune()
    assert registry.get(record.id) is record
    with pytest.raises(RuntimeError, match="too many active"):
        await registry.create("queued")
    clock.now = 1101.0
    await registry.prune()
    assert registry.list() == []
    assert await registry.create("queued")


@pytest.mark.parametrize("mode", ["warm", "queued", "waiting_storage"])
async def test_waiting_url_does_not_extrapolate_a_completion_eta(clock, mode):
    registry = TransferRegistry()
    record = await running(registry)
    record.context.update(source_kind="remote", download_mode=mode)
    await registry.update(record.id, phase="downloading", current=0)
    clock.now += 2
    await registry.update(record.id, current=50)
    snapshot = record.as_dict()
    assert snapshot["download_mode"] == mode
    assert snapshot["speed_bytes_per_second"] == 25
    assert snapshot["eta_seconds"] is None
    record.context["download_mode"] = "full"
    assert record.as_dict()["eta_seconds"] == 2
    # Intake pacing must not suppress the subsequent Telegram-stage ETA.
    record.context["download_mode"] = mode
    await registry.update(record.id, phase="uploading", current=0)
    clock.now += 2
    await registry.update(record.id, current=50)
    assert record.as_dict()["eta_seconds"] == 2
    assert "download_mode" not in record.as_dict()


async def test_source_retry_status_exposes_its_deadline_without_telegram_eta(clock):
    registry = TransferRegistry()
    record = await running(registry)
    record.context.update(source_attempt=2, source_retry_at=clock.now + 4)
    await registry.update(record.id, phase="source_retry_wait", current=0)
    snapshot = record.as_dict()
    assert snapshot["source_attempt"] == 2
    assert snapshot["source_max_attempts"] == 4
    assert snapshot["retry_at"] == clock.now + 4
    assert snapshot["speed_bytes_per_second"] is None
    assert snapshot["eta_seconds"] is None
    await registry.update(record.id, phase="uploading")
    assert record.as_dict()["retry_at"] is None
    assert "source_attempt" not in record.as_dict()
