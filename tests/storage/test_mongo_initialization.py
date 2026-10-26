"""MongoDB-only startup must not replace saved or partially lost drive state."""

from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from pymongo.errors import InvalidURI

from tgdrive.core.errors import CorruptStoreError
from tgdrive.recovery.mongo_backup import COLLECTIONS
from tgdrive.storage.mongo import (
    DatabaseUnavailable,
    MongoBackend,
    for_settings,
    initialize_if_empty,
)


class Collections(dict):
    __getattr__ = dict.__getitem__


@pytest.fixture
def backend():
    collections = Collections(
        {
            name: SimpleNamespace(find_one=Mock(return_value=None))
            for name in COLLECTIONS
        }
    )
    return SimpleNamespace(
        db=collections,
        fence=Mock(),
        transaction=lambda callback: callback(None),
    )


def test_missing_mongodb_configuration_has_no_local_backend_fallback():
    with pytest.raises(DatabaseUnavailable, match="MONGODB_URI is required"):
        for_settings(SimpleNamespace(mongodb_uri=None))


def test_malformed_uri_error_does_not_expose_database_credentials(monkeypatch):
    secret = "mongodb://private-user:private-password@synthetic.invalid"
    monkeypatch.setattr(
        "tgdrive.storage.mongo.MongoClient", Mock(side_effect=InvalidURI(secret))
    )
    with pytest.raises(DatabaseUnavailable, match="configuration is invalid") as caught:
        MongoBackend(secret, "tgdrive")
    assert "private-user" not in str(caught.value)
    assert "private-password" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("collection", ["settings", "jobs", "nodes", "backups"])
def test_missing_catalog_with_saved_records_is_never_initialized(
    tmp_path, monkeypatch, backend, collection
):
    backend.db[collection].find_one.return_value = {"_id": "saved"}
    create = Mock()
    monkeypatch.setattr("tgdrive.storage.mongo_store.MongoStore.create", create)
    with pytest.raises(CorruptStoreError, match="saved drive state"):
        initialize_if_empty(SimpleNamespace(resolved_data_dir=tmp_path), backend)
    create.assert_not_called()


@pytest.mark.parametrize(
    "marker",
    ["mongodb-drive.json", "replica-bootstrap.json", "channel-activation.json"],
)
def test_preserved_installation_does_not_recreate_an_empty_database(
    tmp_path, monkeypatch, backend, marker
):
    path = tmp_path / marker
    path.write_bytes(b"preserved installation state")
    create = Mock()
    monkeypatch.setattr("tgdrive.storage.mongo_store.MongoStore.create", create)
    with pytest.raises(CorruptStoreError, match="existing drive or recovery state"):
        initialize_if_empty(SimpleNamespace(resolved_data_dir=tmp_path), backend)
    create.assert_not_called()
    assert path.read_bytes() == b"preserved installation state"


def test_existing_catalog_is_reused_without_initialization(
    tmp_path, monkeypatch, backend
):
    backend.db.catalog.find_one.return_value = {"_id": "catalog"}
    create = Mock()
    monkeypatch.setattr("tgdrive.storage.mongo_store.MongoStore.create", create)
    assert not initialize_if_empty(SimpleNamespace(resolved_data_dir=tmp_path), backend)
    create.assert_not_called()


def test_first_setup_rolls_back_catalog_when_initial_settings_cannot_be_saved(
    tmp_path, monkeypatch
):
    from tests.support.app_fixtures import make_settings
    from tests.support.mongo_fixtures import register_test_backend

    settings = make_settings(tmp_path)
    backend = register_test_backend(monkeypatch, settings)
    backend.db.settings.insert_many = Mock(side_effect=RuntimeError("write failed"))

    with pytest.raises(RuntimeError, match="write failed"):
        initialize_if_empty(settings, backend)

    assert backend.db.catalog.find_one({}) is None
    assert backend.db.nodes.find_one({}) is None
    assert not (settings.resolved_data_dir / "mongodb-drive.json").exists()


def test_first_setup_applies_initial_connections_without_a_restart(
    tmp_path, monkeypatch
):
    from tests.support.app_fixtures import make_settings
    from tests.support.mongo_fixtures import register_test_backend
    from tgdrive.settings.management import ConnectionManager

    settings = make_settings(tmp_path)
    backend = register_test_backend(monkeypatch, settings)
    assert initialize_if_empty(settings, backend)

    manager = ConnectionManager(settings, SimpleNamespace(mongo=backend))
    assert settings.managed_revision == 1
    assert tuple(row.id for row in manager.active) == settings.managed_client_ids
    assert not manager.restart_required


def test_archive_restore_pause_blocks_old_writers_but_allows_restore_transaction():
    backend = object.__new__(MongoBackend)
    backend._write_lock = RLock()
    backend._writes_paused = False
    session = Mock()
    backend.client = MagicMock()
    backend.client.start_session.return_value.__enter__.return_value = session
    backend.pause_writes()

    with pytest.raises(DatabaseUnavailable, match="restore has paused"):
        backend.transaction(Mock())
    with pytest.raises(DatabaseUnavailable, match="restore has paused"):
        backend.write(Mock(), "replace_one", {}, {})
    backend.client.start_session.assert_not_called()

    restore = Mock()
    backend.transaction(restore, allow_paused=True)
    assert session.with_transaction.call_args.args[0] is restore

    with pytest.raises(DatabaseUnavailable, match="restore has paused"):
        backend.transaction(Mock())
