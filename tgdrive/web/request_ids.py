"""Bounded idempotency keys and cancellation intents for transfer starts."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from tgdrive.transfers.tasks import TransferRecord


class RequestIdConflict(ValueError):
    pass


class RequestIdNotFound(KeyError):
    pass


class RequestIdCancelled(ValueError):
    pass


class RequestCapacityError(RuntimeError):
    pass


@dataclass(slots=True)
class TransferStart:
    request_id: str
    fingerprint: tuple[str, ...] | None = None
    record: TransferRecord | None = None
    cancelled: bool = False
    error: str | None = None
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def active(self) -> bool:
        return self.record is not None and not self.record.terminal

    def snapshot(self) -> dict[str, str | int | None]:
        if self.record is not None:
            return self.record.as_dict()
        status = "cancelled" if self.cancelled else "error" if self.error else "queued"
        return {
            "id": "",
            "status": status,
            "phase": status if status != "queued" else "accepting",
            "current": 0,
            "total": 0,
            "error": "Transfer cancelled" if self.cancelled else self.error,
        }


class TransferStarts:
    """Single-event-loop state; active records are never expired or evicted."""

    def __init__(self, *, ttl_seconds: int = 3600, max_entries: int = 1000) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: dict[str, TransferStart] = {}

    def _prune(self) -> None:
        deadline = time.monotonic() - self._ttl
        for key, entry in list(self._entries.items()):
            updated = entry.record.updated_at if entry.record else entry.updated_at
            if not entry.active and updated <= deadline:
                del self._entries[key]

    def _new(self, request_id: str) -> TransferStart:
        self._prune()
        if len(self._entries) >= self._max_entries:
            raise RequestCapacityError("Too many transfer requests; try again later")
        entry = TransferStart(request_id)
        self._entries[request_id] = entry
        return entry

    def claim(
        self, request_id: str, fingerprint: tuple[str, ...]
    ) -> tuple[TransferStart, bool]:
        self._prune()
        entry = self._entries.get(request_id)
        created = entry is None
        if entry is None:
            entry = self._new(request_id)
        if entry.cancelled:
            raise RequestIdCancelled("Transfer cancelled")
        if entry.fingerprint is not None and entry.fingerprint != fingerprint:
            raise RequestIdConflict(
                "Request ID was already used for a different upload"
            )
        entry.fingerprint = fingerprint
        return entry, created

    def bind(self, entry: TransferStart, record: TransferRecord) -> bool:
        entry.record = record
        entry.updated_at = time.monotonic()
        return entry.cancelled

    def fail(self, entry: TransferStart, detail: str) -> None:
        entry.error = detail[:500]
        entry.updated_at = time.monotonic()

    def get(self, request_id: str) -> TransferStart:
        self._prune()
        try:
            return self._entries[request_id]
        except KeyError as exc:
            raise RequestIdNotFound(request_id) from exc

    def cancel(self, request_id: str) -> TransferStart:
        self._prune()
        entry = self._entries.get(request_id) or self._new(request_id)
        if entry.record is not None and entry.record.terminal:
            return entry
        entry.cancelled = True
        entry.updated_at = time.monotonic()
        return entry
