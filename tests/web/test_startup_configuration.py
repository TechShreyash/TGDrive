"""Startup failures must name the fix without disclosing private inputs."""

import logging

import pytest
from pydantic import ValidationError

import tgdrive.web.app as application
from tgdrive.storage.mongo import DatabaseUnavailable


@pytest.mark.parametrize("database_failure", [False, True])
def test_configuration_failure_is_logged_before_the_app_is_created(
    monkeypatch, caplog, database_failure
):
    error = (
        DatabaseUnavailable("MongoDB is unavailable. Check database access.")
        if database_failure
        else ValidationError.from_exception_data(
            "Settings",
            [{"type": "missing", "loc": ("MONGODB_URI",), "input": {}}],
            hide_input=True,
        )
    )

    def fail():
        raise error

    monkeypatch.setattr(application, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(application, "get_settings", fail)
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        application.create_app()
    assert "MongoDB" in caplog.text or "MONGODB_URI" in caplog.text
