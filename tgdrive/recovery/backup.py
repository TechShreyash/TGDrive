"""Shared backup errors."""

from __future__ import annotations


class BackupError(RuntimeError):
    """A checkpoint or explicit restore failed; retry timing contains no credentials."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RecoveryConflict(BackupError):
    """Local metadata cannot safely mutate the configured remote drive."""

    code = "RECOVERY_REQUIRED"
