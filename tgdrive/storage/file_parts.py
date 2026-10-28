"""Pure validation for logical-file parts and their optional poster sidecar."""

from __future__ import annotations

import re
from typing import Any

from tgdrive.core.errors import ValidationError

MAX_FILE_PARTS = 4096
MAX_FILE_SIZE = (1 << 63) - 1
MAX_POSTER_BYTES = 1024 * 1024
_PART_KEYS = {"index", "offset", "size", "message_id", "sha256"}
_OPTIONAL_KEYS = {"document_id"}
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def validate_sha256(value: Any, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValidationError("sha256 must contain exactly 64 hexadecimal characters")
    return value.lower()


def validate_poster(
    value: Any, *, message_id: int, parts: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    if value is None:
        return None
    required = {"message_id", "size", "sha256"}
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - {"document_id"}
    ):
        raise ValidationError("poster has missing or unknown fields")
    if (
        type(value["message_id"]) is not int
        or not 0 < value["message_id"] <= MAX_FILE_SIZE
        or type(value["size"]) is not int
        or not 0 < value["size"] <= MAX_POSTER_BYTES
    ):
        raise ValidationError(
            "poster must have a positive message id and size up to 1 MiB"
        )
    if value["message_id"] == message_id or any(
        value["message_id"] == part["message_id"] for part in parts or []
    ):
        raise ValidationError("poster cannot reuse an original file message")
    if "document_id" in value and (
        type(value["document_id"]) is not int
        or value["document_id"] == 0
        or not -(1 << 63) <= value["document_id"] < (1 << 63)
    ):
        raise ValidationError(
            "poster document_id must be a non-zero signed 64-bit integer"
        )
    return {**value, "sha256": validate_sha256(value["sha256"], optional=False)}


def validate_parts(parts: Any, *, size: int, message_id: int) -> list[dict[str, Any]]:
    """Return independent canonical records; reject gaps, overlaps and aliases."""
    if type(size) is not int or not 0 < size <= MAX_FILE_SIZE:
        raise ValidationError("multipart file size must be a positive 64-bit integer")
    if type(message_id) is not int or not 0 < message_id <= MAX_FILE_SIZE:
        raise ValidationError("multipart anchor must be a positive message id")
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_FILE_PARTS:
        raise ValidationError(f"file parts must contain 1-{MAX_FILE_PARTS} records")
    result = []
    offset = 0
    identities = set()
    for index, part in enumerate(parts):
        if (
            not isinstance(part, dict)
            or not set(part) >= _PART_KEYS
            or set(part) - _PART_KEYS - _OPTIONAL_KEYS
        ):
            raise ValidationError("file part has missing or unknown fields")
        if any(
            type(part[key]) is not int
            for key in ("index", "offset", "size", "message_id")
        ):
            raise ValidationError("file part offsets, sizes and ids must be integers")
        if part["index"] != index or part["offset"] != offset:
            raise ValidationError(
                "file parts must be contiguous and start at index/offset zero"
            )
        if (
            not 0 < part["size"] <= MAX_FILE_SIZE
            or not 0 < part["message_id"] <= MAX_FILE_SIZE
        ):
            raise ValidationError(
                "file part size and message id must be positive 64-bit integers"
            )
        if "document_id" in part and (
            type(part["document_id"]) is not int
            or part["document_id"] == 0
            or not -(1 << 63) <= part["document_id"] < (1 << 63)
        ):
            raise ValidationError(
                "document_id must be a non-zero signed 64-bit integer"
            )
        if part["message_id"] in identities:
            raise ValidationError("file parts cannot repeat a Telegram message")
        identities.add(part["message_id"])
        offset += part["size"]
        if offset > size:
            raise ValidationError("file parts exceed the logical file size")
        result.append(
            {**part, "sha256": validate_sha256(part["sha256"], optional=False)}
        )
    if offset != size or result[0]["message_id"] != message_id:
        raise ValidationError(
            "file parts must match the logical size and first message id"
        )
    return result
