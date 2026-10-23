"""Small MongoDB failure/scheduling checks without network access."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pymongo.errors import ConnectionFailure

from tgdrive.logging import _redact
from tgdrive.managed_settings import SettingsProblem
from tgdrive.mongo import DatabaseUnavailable
from tgdrive.mongo_backup import MongoProtection
from tgdrive.tasks import TransferRegistry


async def test_recent_snapshot_survives_restart_without_immediate_duplicate(
    monkeypatch,
):
    latest = {"confirmed_at": datetime.now(UTC).isoformat()}
    backend = SimpleNamespace(
        db=SimpleNamespace(
            backups=SimpleNamespace(
                find_one=lambda query, **_kwargs: (
                    latest if query["status"] == "confirmed" else None
                )
            )
        )
    )
    service = MongoProtection(
        SimpleNamespace(mongodb_backup_seconds=3600), object(), backend
    )
    service.snapshot_now = AsyncMock()
    intervals = []

    async def stop_wait(awaitable, timeout):
        intervals.append(timeout)
        awaitable.close()
        return True

    monkeypatch.setattr("tgdrive.mongo_backup.asyncio.wait_for", stop_wait)
    await service._loop(object())
    assert len(intervals) == 1 and 3590 < intervals[0] <= 3600
    service.snapshot_now.assert_not_called()


def test_failed_cloud_journal_does_not_admit_an_unrecorded_job():
    def fail(*_args, **_kwargs):
        raise ConnectionFailure("Synthetic database outage")

    registry = TransferRegistry(
        mongo=SimpleNamespace(find=lambda _query: [], replace_one=fail)
    )
    with pytest.raises(DatabaseUnavailable):
        registry.create("queued")
    assert registry.list() == []


def test_mongodb_connection_credentials_are_redacted():
    uri = "mongodb+srv://fixture_user:fixture_password@cluster.invalid/?appName=test"
    result = _redact("Database failed: " + uri)
    assert "fixture_user" not in result and "fixture_password" not in result
    assert "[REDACTED_MONGODB_URI]" in result


@pytest.mark.parametrize("forced", [False, True])
async def test_encrypted_snapshot_checks_live_channel_before_staging_or_send(
    tmp_path, monkeypatch, forced
):
    cache = tmp_path / "cache"
    backend = SimpleNamespace(
        check_owner=Mock(),
        db=SimpleNamespace(backups=SimpleNamespace(find_one=Mock())),
    )
    pool = SimpleNamespace(lease=Mock())
    service = MongoProtection(
        SimpleNamespace(storage_channel=-1001234567890, resolved_cache_dir=cache),
        pool,
        backend,
    )
    store = SimpleNamespace(storage_channel_id=-1001234567890, revision=1)
    service.verify_lineage = AsyncMock(
        side_effect=SettingsProblem("The storage channel is public.")
    )
    write = Mock()
    monkeypatch.setattr("tgdrive.mongo_backup.write_snapshot", write)
    with pytest.raises(SettingsProblem, match="channel is public"):
        if forced:
            await service.backup(store, force=True)
        else:
            await service.snapshot_now(store)
    service.verify_lineage.assert_awaited_once_with(store)
    backend.db.backups.find_one.assert_not_called()
    pool.lease.assert_not_called()
    write.assert_not_called()
    assert not cache.exists()
