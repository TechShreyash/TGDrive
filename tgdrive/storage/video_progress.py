"""Bounded owner video position metadata stored on an existing file node."""

from __future__ import annotations

import math
import re

from tgdrive.core.errors import ValidationError

SAVE_INTERVAL_SECONDS = 5
SESSION_INTERVAL_SECONDS = 1
MAX_SUMMARY_FILES = 128
MAX_SEQUENCE = 2**53 - 1
SESSION_PATTERN = r"[A-Za-z0-9_-]{32}"


class VideoProgressError(RuntimeError):
    def __init__(self, detail, *, code, status=409, retry_after=None):
        super().__init__(detail)
        self.code, self.status, self.retry_after = code, status, retry_after


def _seconds(value, name, *, positive=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or abs(value) > 1.7976931348623157e308
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        kind = "positive" if positive else "nonnegative"
        raise ValidationError(f"{name} must be a finite {kind} number")
    return value


def position_record(position_seconds, duration_seconds, updated_at):
    _seconds(position_seconds, "position_seconds")
    _seconds(duration_seconds, "duration_seconds", positive=True)
    _seconds(updated_at, "updated_at")
    if position_seconds > duration_seconds:
        raise ValidationError("position_seconds cannot exceed duration_seconds")
    return {
        "position_seconds": position_seconds,
        "duration_seconds": duration_seconds,
        "updated_at": updated_at,
    }


def validate_video_state(node):
    """Validate optional metadata on capture/import without extending tree URLs."""
    if not {"video_progress", "video_progress_session"}.intersection(node):
        return
    if node.get("kind") != "file":
        raise ValidationError("Video progress belongs to a file")
    if "video_progress" in node:
        value = node["video_progress"]
        if not isinstance(value, dict) or set(value) != {
            "position_seconds",
            "duration_seconds",
            "updated_at",
        }:
            raise ValidationError("Invalid saved video progress")
        position_record(**value)
    if "video_progress_session" in node:
        validate_writer_state(
            node["video_progress_session"], "video_progress" in node, label="video"
        )


def validate_writer_state(value, has_progress, *, label):
    """Video and PDF writers share ordering, pacing and activation rules."""
    if not isinstance(value, dict) or set(value) != {
        "id",
        "sequence",
        "started_at",
        "saved_at",
        "closed",
    }:
        raise ValidationError(f"Invalid {label} writer session")
    if (
        not isinstance(value["id"], str)
        or re.fullmatch(SESSION_PATTERN, value["id"]) is None
    ):
        raise ValidationError(f"Invalid {label} writer identity")
    if (
        isinstance(value["sequence"], bool)
        or not isinstance(value["sequence"], int)
        or not 0 <= value["sequence"] <= MAX_SEQUENCE
        or type(value["closed"]) is not bool
    ):
        raise ValidationError(f"Invalid {label} writer sequence")
    _seconds(value["started_at"], "started_at")
    if value["saved_at"] is not None:
        _seconds(value["saved_at"], "saved_at")
    if value["sequence"] == 0:
        if value["closed"] or value["saved_at"] is not None:
            raise ValidationError(f"Invalid unused {label} writer")
    elif value["saved_at"] is None or not has_progress:
        raise ValidationError(f"{label.capitalize()} writer has no saved position")


def check_writer(session_id, sequence, final, *, label="video"):
    if (
        not isinstance(session_id, str)
        or re.fullmatch(SESSION_PATTERN, session_id) is None
    ):
        raise ValidationError(f"Invalid {label} writer identity")
    if (
        isinstance(sequence, bool) or not isinstance(sequence, int)
    ) or not 1 <= sequence <= MAX_SEQUENCE:
        raise ValidationError(f"Invalid {label} writer sequence")
    if type(final) is not bool:
        raise ValidationError("final must be a boolean")


def check_interval(
    previous, now, interval, *, error_type=VideoProgressError, label="video"
):
    if previous is not None and 0 <= now - previous < interval:
        retry_after = max(1, math.ceil(interval - (now - previous)))
        raise error_type(
            f"Wait before saving {label} progress again.",
            code=f"{label.upper()}_PROGRESS_RATE_LIMITED",
            status=429,
            retry_after=retry_after,
        )
