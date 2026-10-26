"""Shared backup errors and bounded portable recovery JSON decoding."""

from __future__ import annotations

import json
from typing import Any

from tgdrive.storage.store import MAX_STORE_BYTES


class BackupError(RuntimeError):
    """A checkpoint or explicit restore failed; retry timing contains no credentials."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RecoveryConflict(BackupError):
    """Local metadata cannot safely mutate the configured remote drive."""

    code = "RECOVERY_REQUIRED"


def decode_catalog_snapshot(content: bytes) -> object:
    if len(content) > MAX_STORE_BYTES:
        raise BackupError("metadata backup exceeds the safe size limit")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise BackupError("metadata backup contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BackupError("metadata backup is not valid bounded UTF-8 JSON") from exc
