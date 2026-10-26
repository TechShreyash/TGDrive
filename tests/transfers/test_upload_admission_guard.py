"""MongoDB upload admission is independent of optional Telegram snapshots."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.support.recovery_fixtures import settings
from tgdrive.recovery.backup import RecoveryConflict
from tgdrive.recovery.mongo_backup import MongoProtection
from tgdrive.storage.mongo import DatabaseUnavailable


def protection(tmp_path):
    configured = settings(tmp_path)
    backend = SimpleNamespace(check_owner=Mock())
    pool = Mock()
    service = MongoProtection(configured, pool, backend)
    store = SimpleNamespace(storage_channel_id=configured.storage_channel)
    return service, backend, pool, store


def test_upload_admission_does_not_wait_for_optional_telegram_backup(tmp_path):
    service, backend, pool, store = protection(tmp_path)
    service._last_error = "Telegram snapshot is waiting before retry"
    service.check_upload_allowed(store)
    backend.check_owner.assert_called_once_with()
    assert pool.mock_calls == []


def test_unavailable_database_ownership_blocks_upload_admission(tmp_path):
    service, backend, pool, store = protection(tmp_path)
    backend.check_owner.side_effect = DatabaseUnavailable(
        "MongoDB ownership is unavailable"
    )
    with pytest.raises(DatabaseUnavailable):
        service.check_upload_allowed(store)
    assert pool.mock_calls == []


def test_changed_channel_blocks_upload_admission_without_telegram_rpc(tmp_path):
    service, _, pool, store = protection(tmp_path)
    store.storage_channel_id = -100999
    with pytest.raises(RecoveryConflict, match="different Telegram channel"):
        service.check_upload_allowed(store)
    assert pool.mock_calls == []
