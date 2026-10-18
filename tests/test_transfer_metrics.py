from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import tgdrive.tasks as tasks
from tgdrive.tasks import TransferRegistry, TransferStatus


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    value = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(tasks.time, "time", lambda: value.now)
    monkeypatch.setattr(tasks.time, "monotonic", lambda: value.now)
    return value


def running(registry: TransferRegistry):
    record = registry.create("queued", total=100)
    record.created_at = 1000.0
    registry.update(
        record.id, status=TransferStatus.RUNNING, phase="uploading", current=0
    )
    return record


def test_metrics_are_stage_specific_stale_bounded_and_terminal_frozen(clock):
    registry = TransferRegistry()
    record = running(registry)
    clock.now += 2
    registry.update(record.id, current=50)
    snapshot = record.as_dict()
    assert snapshot["speed_bytes_per_second"] == 25
    assert snapshot["eta_seconds"] == 2
    assert snapshot["elapsed_seconds"] == 2
    assert snapshot["started_at"] == 1000
    assert snapshot["progress_updated_at"] == 1002
    clock.now += 16
    assert record.as_dict()["speed_bytes_per_second"] is None
    registry.update(record.id, phase="finalizing", current=100)
    assert record.as_dict()["eta_seconds"] is None
    registry.update(record.id, status=TransferStatus.COMPLETED, phase="completed")
    finished = record.as_dict()
    clock.now += 60
    assert record.as_dict() == finished
    assert finished["finished_at"] == 1018


def test_metrics_restart_when_phase_total_or_counter_changes(clock):
    registry = TransferRegistry()
    record = running(registry)
    clock.now += 1
    registry.update(record.id, current=50)
    assert record.as_dict()["speed_bytes_per_second"] == 50
    registry.update(record.id, phase="downloading", current=0)
    assert record.as_dict()["speed_bytes_per_second"] is None
    clock.now += 1
    registry.update(record.id, current=25)
    assert record.as_dict()["speed_bytes_per_second"] == 25
    registry.update(record.id, total=200)
    assert record.as_dict()["speed_bytes_per_second"] is None
    clock.now += 1
    registry.update(record.id, current=100)
    registry.update(record.id, current=50)
    assert record.as_dict()["speed_bytes_per_second"] is None
    record.context["kind"] = "delete"
    clock.now += 1
    registry.update(record.id, current=75)
    assert record.as_dict()["speed_bytes_per_second"] is None


def test_progress_is_visible_without_per_chunk_commits_and_context_flushes(
    clock, tmp_path
):
    path = tmp_path / "jobs.sqlite3"
    registry = TransferRegistry(database_path=path)
    record = running(registry)
    commits = []
    registry._db.set_trace_callback(
        lambda sql: commits.append(sql) if sql == "COMMIT" else None
    )
    for current in range(1, 50):
        registry.update(record.id, current=current)
    assert record.as_dict()["current"] == 49
    assert not commits
    raw = registry._db.execute("SELECT data FROM jobs").fetchone()[0]
    assert json.loads(raw)["current"] == 0
    record.context.update(sender="stable-sender", random_id=123, send_started=True)
    registry.update(record.id, current=50)
    assert len(commits) == 1
    clock.now += 0.51
    registry.update(record.id, current=75)
    assert len(commits) == 2
    registry.update(
        record.id, status=TransferStatus.COMPLETED, phase="completed", current=100
    )
    assert len(commits) == 3
    finished = record.finished_at
    registry.close()
    reopened = TransferRegistry(database_path=path)
    restored = reopened.get(record.id)
    assert restored.current == 100
    assert restored.started_at == 1000 and restored.finished_at == finished
    assert restored.as_dict()["speed_bytes_per_second"] is None
    assert restored.context["random_id"] == 123
    reopened.close()


def test_legacy_terminal_job_without_known_start_does_not_invent_elapsed(
    tmp_path: Path, clock
):
    registry = TransferRegistry(database_path=tmp_path / "jobs.sqlite3")
    record = registry.create("failed")
    payload = dict(
        id=record.id,
        phase="failed",
        status="error",
        current=0,
        total=0,
        error="offline",
        created_at=1000,
        updated_at=1010,
        context={},
    )
    registry._db.execute("UPDATE jobs SET data=?", (json.dumps(payload),))
    registry._db.commit()
    registry.close()
    reopened = TransferRegistry(database_path=tmp_path / "jobs.sqlite3")
    assert reopened.get(record.id).as_dict()["elapsed_seconds"] == 0
    reopened.close()


def test_unknown_length_reports_rate_without_inventing_an_eta(clock):
    registry = TransferRegistry()
    record = registry.create("queued")
    registry.update(
        record.id,
        status=TransferStatus.RUNNING,
        phase="downloading",
        current=0,
        total=0,
    )
    clock.now += 2
    registry.update(record.id, current=50, total=0)
    snapshot = record.as_dict()
    assert snapshot["speed_bytes_per_second"] == 25
    assert snapshot["eta_seconds"] is None


def test_waiting_for_capacity_does_not_start_elapsed_and_completion_freezes_it(
    clock, tmp_path
):
    path = tmp_path / "jobs.sqlite3"
    registry = TransferRegistry(database_path=path)
    record = registry.create("queued", total=100)
    clock.now += 60
    assert record.as_dict()["elapsed_seconds"] == 0
    assert record.as_dict()["started_at"] is None
    registry.close()
    registry = TransferRegistry(database_path=path)
    record = registry.get(record.id)
    assert record.as_dict()["elapsed_seconds"] == 0
    registry.update(
        record.id, status=TransferStatus.RUNNING, phase="uploading", current=0
    )
    clock.now += 2
    registry.update(record.id, current=50)
    assert record.as_dict()["elapsed_seconds"] == 2
    registry.update(
        record.id, status=TransferStatus.COMPLETED, phase="completed", current=100
    )
    clock.now += 120
    assert record.as_dict()["elapsed_seconds"] == 2
    registry.close()


def test_cancel_before_start_has_no_elapsed_transfer_time(clock):
    registry = TransferRegistry()
    record = registry.create("queued", total=100)
    clock.now += 60
    registry.cancel(record.id)
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
def test_terminal_jobs_retain_provider_deadlines_until_they_expire(clock, context):
    registry = TransferRegistry(ttl_seconds=1, max_entries=1)
    record = registry.create("queued")
    record.context.update(context)
    registry.cancel(record.id)
    clock.now = 1010.0
    registry.prune()
    assert registry.get(record.id) is record
    with pytest.raises(RuntimeError, match="too many active"):
        registry.create("queued")
    clock.now = 1101.0
    registry.prune()
    assert registry.list() == []
    assert registry.create("queued")
