"""Integration tests must never discover the deployment database implicitly."""

import pytest

from tests.storage.test_mongodb import mongo_uri


def test_opt_in_mongo_requires_explicit_test_uri(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TGDRIVE_TEST_MONGODB_URI", raising=False)
    monkeypatch.setenv("MONGODB_URI", "mongodb://deployment.example/")
    (tmp_path / ".env").write_text("MONGODB_URI=mongodb://deployment.example/\n")
    with pytest.raises(pytest.UsageError, match="TGDRIVE_TEST_MONGODB_URI"):
        mongo_uri()


def test_explicit_test_uri_is_used(monkeypatch):
    monkeypatch.setenv("TGDRIVE_TEST_MONGODB_URI", "mongodb://127.0.0.1:27017/")
    assert mongo_uri() == "mongodb://127.0.0.1:27017/"
