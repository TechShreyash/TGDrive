"""Application persistence never substitutes a local database or settings file."""

from types import SimpleNamespace

import pytest
from pymongo.errors import ConnectionFailure

from tgdrive.core.security import SessionManager
from tgdrive.settings.managed_settings import SettingsProblem, read_document
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.transfers.tasks import TransferRegistry
from tgdrive.transfers.uploads import ResumableUploads


def test_transfer_registry_rejects_local_database_path(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    with pytest.raises(ValueError, match="requires MongoDB"):
        TransferRegistry(database_path=path)
    assert not path.exists()


def test_managed_settings_do_not_read_legacy_local_document(tmp_path):
    path = tmp_path / "telegram-settings.json"
    path.write_text('{"local": "obsolete"}')
    with pytest.raises(SettingsProblem, match="require a configured MongoDB"):
        read_document(tmp_path)
    assert path.read_text() == '{"local": "obsolete"}'


def test_session_loading_rejects_mongodb_connection_failure():
    def unavailable(_query):
        raise ConnectionFailure("synthetic private URI")

    backend = SimpleNamespace(
        db=SimpleNamespace(sessions=SimpleNamespace(find=unavailable))
    )
    with pytest.raises(DatabaseUnavailable, match="Owner sign-in requires") as error:
        SessionManager(b"x" * 32, 3600, mongo_backend=backend)
    assert "synthetic private URI" not in str(error.value)


def test_upload_receipts_cannot_fall_back_to_sqlite(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        resolved_data_dir=tmp_path / "data",
        resolved_cache_dir=tmp_path / "cache",
    )

    def unavailable(_settings):
        raise DatabaseUnavailable("MongoDB is unavailable")

    monkeypatch.setattr("tgdrive.storage.mongo.for_settings", unavailable)
    with pytest.raises(DatabaseUnavailable, match="MongoDB is unavailable"):
        ResumableUploads(settings, object())
    assert not (settings.resolved_data_dir / "uploads.sqlite3").exists()
