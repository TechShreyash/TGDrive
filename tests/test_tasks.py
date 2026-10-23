from __future__ import annotations

import asyncio
import errno
import logging
import time

import pytest

from tgdrive.tasks import (
    TransferNotFound,
    TransferRegistry,
    TransferStatus,
)


def test_lifecycle_logs_skip_progress_and_unchanged_waits(caplog) -> None:
    caplog.set_level(logging.INFO, logger="tgdrive.tasks")
    registry = TransferRegistry()
    record = registry.create("queued", total=100)
    record.context.update(kind="upload", name="private-name", url="private-url")
    registry.update(record.id, status=TransferStatus.RUNNING, phase="uploading")
    for current in range(100):
        registry.update(record.id, current=current)
        registry.persist(record)
    registry.update(record.id, phase="waiting_for_telegram")
    for _ in range(10):
        registry.update(record.id, phase="waiting_for_telegram")
    record.context["retry_at"] = time.time() + 60
    for _ in range(10):
        registry.persist(record)
    registry.update(record.id, phase="retry_wait")
    registry.update(
        record.id, status=TransferStatus.COMPLETED, phase="completed", current=100
    )
    registry.persist(record)

    messages = [row.getMessage() for row in caplog.records]
    assert len(messages) == 4
    assert sum("Transfer started" in message for message in messages) == 1
    assert sum("Transfer finished" in message for message in messages) == 1
    assert sum("Transfer waiting" in message for message in messages) == 1
    assert sum("Transfer retry scheduled" in message for message in messages) == 1
    assert "telegram_cooldown" in caplog.text
    assert "private-name" not in caplog.text and "private-url" not in caplog.text


@pytest.mark.asyncio
async def test_failure_log_has_cause_type_and_action_without_private_detail(caplog):
    caplog.set_level(logging.INFO, logger="tgdrive.tasks")
    registry = TransferRegistry()
    record = registry.create("uploading")

    async def fail(_record):
        raise TimeoutError("private-source-url password=secret")

    await registry.spawn(record.id, fail)

    assert "error_type=TimeoutError retryable=True" in caplog.text
    assert "code=TRANSPORT_INTERRUPTED" in caplog.text
    assert "restore the connection and retry" in caplog.text
    assert "private-source-url" not in caplog.text
    assert "secret" not in caplog.text
    assert "Traceback" not in caplog.text


def test_failure_diagnostics_classify_repairs_without_private_messages():
    from tgdrive.connection_errors import TelegramConnectionError
    from tgdrive.mongo import DatabaseUnavailable
    from tgdrive.remote import RemoteHTTPError
    from tgdrive.tasks import _failure_diagnostic

    for error, expected in (
        (DatabaseUnavailable("private database URI"), "DATABASE_UNAVAILABLE"),
        (OSError(errno.ENOSPC, "private path"), "STAGING_FULL"),
        (RemoteHTTPError(403, "https://private-source"), "REMOTE_HTTP_403"),
        (
            TelegramConnectionError("private account", "TELEGRAM_CHANNEL_ACCESS"),
            "TELEGRAM_CHANNEL_ACCESS",
        ),
    ):
        code, action = _failure_diagnostic(error)
        assert code == expected
        assert "private" not in action


def test_reloaded_transfer_does_not_repeat_existing_lifecycle(caplog, tmp_path):
    caplog.set_level(logging.INFO, logger="tgdrive.tasks")
    database_path = tmp_path / "jobs.sqlite3"
    registry = TransferRegistry(database_path=database_path)
    record = registry.create("uploading", total=10)
    registry.update(record.id, status=TransferStatus.RUNNING)
    registry.close()
    caplog.clear()

    resumed = TransferRegistry(database_path=database_path)
    resumed.persist(resumed.get(record.id))
    assert not caplog.records
    resumed.close()


def test_registry_updates_and_prunes_terminal_records() -> None:
    registry = TransferRegistry(ttl_seconds=60)
    record = registry.create("saving", total=10)
    registry.update(
        record.id,
        status=TransferStatus.COMPLETED,
        current=10,
        total=10,
    )
    record.updated_at = 1.0

    registry.prune(now=62.0)
    with pytest.raises(TransferNotFound):
        registry.get(record.id)


def test_registry_cancellation_sets_event() -> None:
    registry = TransferRegistry()
    record = registry.create("downloading")

    registry.cancel(record.id)
    assert record.cancel_event.is_set()
    assert record.status is TransferStatus.CANCELLED
    assert record.phase == "cancelling"
    assert record.error == "Transfer cancelled"


def test_late_progress_cannot_mutate_cancelled_snapshot() -> None:
    registry = TransferRegistry()
    record = registry.create("uploading", total=100)
    registry.update(
        record.id,
        status=TransferStatus.RUNNING,
        current=40,
    )

    registry.cancel(record.id)
    cancelled_snapshot = record.as_dict()
    registry.update(
        record.id,
        status=TransferStatus.COMPLETED,
        phase="completed",
        current=100,
    )

    assert record.as_dict() == cancelled_snapshot
    assert record.status is TransferStatus.CANCELLED
    assert record.current == 40 and record.total == 100
    assert record.error == "Transfer cancelled"


@pytest.mark.asyncio
async def test_spawn_observes_errors() -> None:
    registry = TransferRegistry()
    record = registry.create("uploading")

    async def fail(_record: object) -> None:
        raise RuntimeError("network failed")

    task = registry.spawn(record.id, fail)
    await task

    assert record.status is TransferStatus.ERROR
    assert record.error == "network failed"


@pytest.mark.asyncio
async def test_shutdown_cancels_and_drains_tasks() -> None:
    registry = TransferRegistry(ttl_seconds=60)
    record = registry.create("uploading")
    started = asyncio.Event()

    async def wait_forever(_record: object) -> None:
        started.set()
        await asyncio.Event().wait()

    registry.spawn(record.id, wait_forever)
    await started.wait()
    expired = registry.create("completed")
    registry.update(expired.id, status=TransferStatus.COMPLETED)
    expired.updated_at = time.monotonic() - 61
    await registry.shutdown()

    assert record.cancel_event.is_set()
    assert record.status is TransferStatus.CANCELLED


@pytest.mark.asyncio
async def test_uncooperative_cancel_is_forced_after_grace_period() -> None:
    registry = TransferRegistry(cancel_grace_seconds=0.01)
    record = registry.create("uploading")
    started = asyncio.Event()

    async def ignore_event(_record: object) -> None:
        started.set()
        await asyncio.Event().wait()

    task = registry.spawn(record.id, ignore_event)
    await started.wait()
    registry.cancel(record.id)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert record.status is TransferStatus.CANCELLED
    assert record.cancel_handle is None


@pytest.mark.asyncio
async def test_cancelling_one_transfer_does_not_affect_another() -> None:
    registry = TransferRegistry(cancel_grace_seconds=0.01)
    first = registry.create("uploading")
    second = registry.create("uploading")
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    second_release = asyncio.Event()

    async def hold_first(record: object) -> None:
        registry.update(first.id, status=TransferStatus.RUNNING)
        first_started.set()
        await asyncio.Event().wait()

    async def finish_second(record: object) -> None:
        registry.update(second.id, status=TransferStatus.RUNNING)
        second_started.set()
        await second_release.wait()
        registry.update(
            second.id,
            status=TransferStatus.COMPLETED,
            phase="completed",
        )

    first_task = registry.spawn(first.id, hold_first)
    second_task = registry.spawn(second.id, finish_second)
    await asyncio.gather(first_started.wait(), second_started.wait())

    registry.cancel(first.id)

    assert first.status is TransferStatus.CANCELLING
    assert not second.cancel_event.is_set()
    assert second.status is TransferStatus.RUNNING

    second_release.set()
    await second_task
    with pytest.raises(asyncio.CancelledError):
        await first_task

    assert second.status is TransferStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancelled_running_task_cannot_be_pruned_or_evicted() -> None:
    registry = TransferRegistry(max_entries=1)
    record = registry.create("uploading")
    started = asyncio.Event()

    async def hold(_record: object) -> None:
        started.set()
        await asyncio.Event().wait()

    registry.spawn(record.id, hold)
    await started.wait()
    registry.cancel(record.id)
    record.updated_at = 1
    registry.prune(now=10000)

    assert registry.get(record.id) is record
    with pytest.raises(RuntimeError, match="too many active"):
        registry.create("uploading")

    await registry.shutdown()
    with pytest.raises(RuntimeError, match="shutting down"):
        registry.create("uploading")


@pytest.mark.asyncio
async def test_shutdown_retries_cancellation_of_stalled_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tgdrive.tasks as tasks_module

    monkeypatch.setattr(tasks_module, "_SHUTDOWN_DRAIN_SECONDS", 0.01)
    registry = TransferRegistry()
    record = registry.create("uploading")
    started = asyncio.Event()

    async def stall_cleanup(_record: object) -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.Event().wait()

    task = registry.spawn(record.id, stall_cleanup)
    await started.wait()
    await asyncio.wait_for(registry.shutdown(), timeout=1)

    assert task.done()
    assert task.cancelled()


def test_multipart_progress_distinguishes_sent_bytes_from_channel_publication():
    from tgdrive.tasks import TransferRecord

    record = TransferRecord(
        id="multipart-counter",
        phase="uploading",
        current=3200,
        total=4500,
        context={
            "multipart_plan": {
                "parts": [
                    {"size": 2000, "confirmed": False, "sender": "private-client"},
                    {"size": 2000, "confirmed": False},
                    {"size": 500, "confirmed": False},
                ]
            }
        },
    )
    snapshot = record.as_dict()
    assert snapshot["current"] == 3200
    assert snapshot["telegram_parts"] == {"total": 3, "stored": 0}
    assert "private-client" not in str(snapshot)
    record.context["multipart_plan"]["parts"][0]["confirmed"] = True
    assert record.as_dict()["telegram_parts"] == {"total": 3, "stored": 1}


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (TimeoutError("provider timeout"), True),
        (ConnectionError("connection lost"), True),
        (OSError(errno.ENETUNREACH, "network unreachable"), True),
        (OSError(errno.ENOSPC, "disk full"), False),
        (PermissionError("no permission"), False),
        (ValueError("invalid configuration"), False),
    ],
)
async def test_only_temporary_connection_errors_are_marked_for_restart(
    tmp_path, error, retryable
):
    registry = TransferRegistry(database_path=tmp_path / "jobs.sqlite3")
    record = registry.create("uploading")
    record.context["kind"] = "upload"

    async def fail(_record):
        raise error

    try:
        await registry.spawn(record.id, fail)
        assert record.status is TransferStatus.ERROR
        assert bool(record.context.get("restart_retryable")) is retryable
        if retryable:
            assert "restart TGDrive" in record.error
        registry.retry(record.id)
        assert "restart_retryable" not in record.context
    finally:
        registry.close()
