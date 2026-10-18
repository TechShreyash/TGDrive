from __future__ import annotations

import logging
from pathlib import Path

from tgdrive.logging import configure_logging


def test_logging_redacts_credentials_and_appends_records(tmp_path: Path) -> None:
    log_path = tmp_path / "tgdrive.log"
    configure_logging("INFO", log_path)
    logger = logging.getLogger("test")

    logger.info("password=super-secret")
    logger.info("token 123456:abcdefghijklmnopqrstuvwxyz1234")
    logger.info("GET /file?path=/abc&auth=share-secret-value HTTP/1.1")
    logger.info("Cookie: tgdrive_session=session-secret-value")
    for handler in logging.getLogger().handlers:
        handler.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "super-secret" not in content
    assert "abcdefghijklmnopqrstuvwxyz1234" not in content
    assert "share-secret-value" not in content
    assert "session-secret-value" not in content
    assert "auth=[REDACTED]" in content
    assert content.count("[REDACTED]") == 4
    previous = next(
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.FileHandler)
    )
    configure_logging("INFO")
    assert previous.stream is None
    assert logging.getLogger("uvicorn.access").disabled


def test_exception_traceback_redacts_secret_values(tmp_path: Path) -> None:
    log_path = tmp_path / "traceback.log"
    configure_logging("INFO", log_path)
    try:
        raise RuntimeError(
            "Telegram 123456:abcdefghijklmnopqrstuvwxyz1234 "
            "failed for /file?auth=private-link"
        )
    except RuntimeError:
        logging.getLogger("test").exception("Transfer failed")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = log_path.read_text()
    assert "Traceback" in content
    assert "RuntimeError" in content
    assert "abcdefghijklmnopqrstuvwxyz1234" not in content
    assert "private-link" not in content
