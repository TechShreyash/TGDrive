"""Centralized logging configuration with conservative secret redaction."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)mongodb(?:\+srv)?://[^\s\"']+"), "[REDACTED_MONGODB_URI]"),
    (re.compile(r"\b\d{5,15}:[A-Za-z0-9_-]{20,}\b"), "[REDACTED]"),
    (
        re.compile(
            r"(?i)(mongodb[_-]?backup[_-]?key|x-tgdrive-recovery-key)"
            r"\s*[=:]\s*[^\s,]+"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(r"(?i)(password|api[_-]?hash|session[_-]?secret)\s*[=:]\s*[^\s,]+"),
        "[REDACTED]",
    ),
    (
        re.compile(r"(?i)([?&](?:auth|token|access_token)=)[^&\s\"']+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)(\b(?:authorization|cookie)\s*:\s*)[^\r\n]+"),
        r"\1[REDACTED]",
    ),
)


def _redact(message: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


class RedactingFormatter(logging.Formatter):
    """Redact the rendered traceback as well as the ordinary log message."""

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.getMessage())
        record.args = ()
        return True


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure the root logger once for console and optional rotated file output."""

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    # Share URLs contain bearer credentials.  Uvicorn formats access records on
    # its own handler, outside the root redaction filter, so disable that unsafe
    # raw-query log at the source.
    logging.getLogger("uvicorn.access").disabled = True
    # Telethon's routine update/difference messages can dominate a busy drive's
    # logs. Keep provider warnings/errors visible; DEBUG deliberately restores
    # its connection/update diagnostics for a short troubleshooting session.
    diagnostic_level = (
        logging.NOTSET
        if root.level == logging.DEBUG
        else max(root.level, logging.WARNING)
    )
    logging.getLogger("telethon").setLevel(diagnostic_level)
    logging.getLogger("httpx").setLevel(diagnostic_level)
    logging.getLogger("httpcore").setLevel(diagnostic_level)
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redact = RedactingFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redact)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        handler.addFilter(redact)
        root.addHandler(handler)
