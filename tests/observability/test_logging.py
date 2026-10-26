from __future__ import annotations

import logging
from pathlib import Path

from tgdrive.core.logging import configure_logging


def flush_logs(log_path: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return log_path.read_text(encoding="utf-8")


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


def test_default_logs_show_app_lifecycle_and_provider_warnings(tmp_path: Path) -> None:
    log_path = tmp_path / "default.log"
    configure_logging("INFO", log_path)
    provider = logging.getLogger("telethon.client.updates")
    provider.info("Got difference for channel 123 updates")
    provider.warning("Connection requires attention")
    logging.getLogger("httpx").info("HTTP Request: GET /private?auth=secret")
    logging.getLogger("tgdrive.transfers.uploads").info(
        "Device upload accepted id=example"
    )

    content = flush_logs(log_path)
    assert "Got difference" not in content
    assert "HTTP Request" not in content
    assert "Connection requires attention" in content
    assert "Device upload accepted" in content


def test_debug_restores_provider_diagnostics_and_info_reconfiguration(tmp_path: Path):
    log_path = tmp_path / "debug.log"
    configure_logging("DEBUG", log_path)
    logging.getLogger("telethon.client.updates").debug("provider diagnostic")
    assert "provider diagnostic" in flush_logs(log_path)
    configure_logging("INFO", log_path)
    logging.getLogger("telethon.client.updates").info("provider chatter")
    assert "provider chatter" not in flush_logs(log_path)


def test_provider_logging_respects_stricter_global_level(tmp_path: Path):
    log_path = tmp_path / "errors.log"
    configure_logging("ERROR", log_path)
    logging.getLogger("telethon.client.updates").warning("provider warning")
    logging.getLogger("telethon.client.updates").error("provider error")
    content = flush_logs(log_path)
    assert "provider warning" not in content
    assert "provider error" in content
    configure_logging("INFO")
