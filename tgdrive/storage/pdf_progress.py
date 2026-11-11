"""Bounded owner PDF page metadata; page counts come from the viewer."""

from tgdrive.core.errors import ValidationError
from tgdrive.storage.video_progress import (
    VideoProgressError,
    _seconds,
    validate_writer_state,
)

MAX_PDF_PAGES = 10_000


class PdfProgressError(VideoProgressError):
    pass


def page_record(page_number, page_count, updated_at):
    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or not 1 <= page_number <= page_count <= MAX_PDF_PAGES
    ):
        raise ValidationError("PDF pages must be integers within 1..page_count<=10000")
    _seconds(updated_at, "updated_at")
    return {
        "page_number": page_number,
        "page_count": page_count,
        "updated_at": updated_at,
    }


def validate_pdf_state(node):
    if not {"pdf_progress", "pdf_progress_session"}.intersection(node):
        return
    if node.get("kind") != "file":
        raise ValidationError("PDF progress belongs to a file")
    if "pdf_progress" in node:
        value = node["pdf_progress"]
        if not isinstance(value, dict) or set(value) != {
            "page_number",
            "page_count",
            "updated_at",
        }:
            raise ValidationError("Invalid saved PDF progress")
        page_record(**value)
    if "pdf_progress_session" in node:
        validate_writer_state(
            node["pdf_progress_session"], "pdf_progress" in node, label="PDF"
        )
