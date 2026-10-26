from __future__ import annotations

import pytest

from tgdrive.transfers.tasks import TransferRegistry, TransferStatus
from tgdrive.web.request_ids import (
    RequestIdCancelled,
    RequestIdConflict,
    RequestIdNotFound,
    TransferStarts,
)


def test_repeated_start_is_idempotent_but_different_payload_conflicts() -> None:
    starts = TransferStarts()
    entry, created = starts.claim("a" * 24, ("local", "/", "file.txt", "12"))
    again, created_again = starts.claim("a" * 24, ("local", "/", "file.txt", "12"))
    assert created
    assert not created_again
    assert entry is again
    with pytest.raises(RequestIdConflict):
        starts.claim("a" * 24, ("local", "/", "other.txt", "12"))


def test_cancel_before_start_prevents_late_transfer() -> None:
    starts = TransferStarts()
    entry = starts.cancel("cancel-before-start")
    assert entry.snapshot()["status"] == "cancelled"
    assert entry.snapshot()["id"] == ""
    with pytest.raises(RequestIdCancelled):
        starts.claim("cancel-before-start", ("remote", "/", "https://example.com"))


async def test_bound_record_is_visible_during_save_and_after_completion() -> None:
    starts = TransferStarts()
    registry = TransferRegistry()
    record = await registry.create("saving", total=12)
    entry, _ = starts.claim("b" * 24, ("local", "/", "file.txt", "12"))
    assert not starts.bind(entry, record)
    assert starts.get("b" * 24).snapshot()["id"] == record.id
    await registry.update(record.id, status=TransferStatus.COMPLETED, phase="completed")
    starts.cancel("b" * 24)
    assert not entry.cancelled
    assert entry.snapshot()["status"] == "completed"
    assert starts.claim("b" * 24, entry.fingerprint)[1] is False


async def test_cancel_intent_reaches_record_bound_later() -> None:
    starts = TransferStarts()
    entry, _ = starts.claim("c" * 24, ("local", "/", "file.txt", "12"))
    starts.cancel("c" * 24)
    record = await TransferRegistry().create("saving")
    assert starts.bind(entry, record)


def test_failed_start_has_an_authoritative_recovery_result() -> None:
    starts = TransferStarts()
    entry, _ = starts.claim("d" * 24, ("local", "/", "file.txt", "12"))
    starts.fail(entry, "Destination no longer exists")
    assert entry.snapshot()["status"] == "error"
    assert entry.snapshot()["error"] == "Destination no longer exists"


def test_unknown_start_is_not_found() -> None:
    with pytest.raises(RequestIdNotFound):
        TransferStarts().get("missing-request-id")


async def test_request_capacity_is_bounded_without_evicting_active_work() -> None:
    starts = TransferStarts(max_entries=1)
    entry, _ = starts.claim("e" * 24, ("local",))
    starts.bind(entry, await TransferRegistry().create("saving"))
    with pytest.raises(RuntimeError, match="Too many transfer requests"):
        starts.cancel("f" * 24)
    assert starts.get("e" * 24) is entry
