"""Independent full MongoDB archive replication; no actual credentials/network."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pymongo.errors import ConnectionFailure, OperationFailure

from tests.replication.test_replica import BACKUP, dispose, setup
from tests.replication.test_replica import replica_backend as replica_backend
from tgdrive.replication.replica_mongo import mirror_mongodb
from tgdrive.replication.replica_transport import ReplicaTransportError
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.mongo import DatabaseUnavailable

pytestmark = pytest.mark.usefixtures("replica_backend")


class Snapshot:
    def __init__(self, configured, store, remote):
        self.count = 0
        self.rows = []
        self.configured, self.store, self.remote = configured, store, remote

    async def snapshot_now(self, store, *, legacy=False):
        assert legacy
        self.count += 1
        self.remote.messages[self.configured.storage_channel, 900] = (
            b"encrypted full MongoDB database"
        )
        self.rows = [
            {
                "id": "full-mongo-snapshot",
                "status": "confirmed",
                "channel_id": self.configured.storage_channel,
                "message_id": 900,
                "format": "mongodb-encrypted-v1",
                "revision": store.revision,
                "sha256": "a" * 64,
                "created_at": "2026-10-22T10:00:00Z",
            }
        ]
        return True

    def history(self, *, limit=None):
        return self.rows


class Pool:
    def __init__(self, remote):
        self.remote = remote

    @asynccontextmanager
    async def lease(self):
        async def get(channel, *, message_ids):
            body = self.remote.messages.get((channel, message_ids))
            return SimpleNamespace(
                document=SimpleNamespace(
                    file_size=len(body) if body is not None else None,
                    file_name="tgdrive-full-mongo-snapshot.tgdb",
                )
            )

        yield SimpleNamespace(get_messages=get)


@pytest.mark.asyncio
async def test_no_mongodb_installation_never_requests_snapshot(tmp_path):
    _, store, _, replica, _ = setup(tmp_path)
    replica.runtime.mongo = None
    try:
        assert (
            await mirror_mongodb(
                replica,
                None,
                source_channel=replica.settings.storage_channel,
                destination_channel=BACKUP,
            )
            is None
        )
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_missing_key_blocks_without_changing_other_backup_settings(tmp_path):
    _, store, _, replica, _ = setup(tmp_path)
    replica.runtime.mongo = object()
    replica.runtime.backup = SimpleNamespace(snapshot_now=AsyncMock())
    try:
        with pytest.raises(SettingsProblem) as error:
            await mirror_mongodb(
                replica,
                None,
                source_channel=replica.settings.storage_channel,
                destination_channel=BACKUP,
            )
        assert error.value.code == "REPLICA_MONGODB_KEY_REQUIRED"
        replica.runtime.backup.snapshot_now.assert_not_called()
        assert replica.settings.mongodb_backup_key is None
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_full_archive_retry_keeps_source_ciphertext_and_operation(
    tmp_path,
):
    configured, store, remote, replica, _ = setup(tmp_path)
    configured.mongodb_backup_key = SecretStr("b2" * 32)
    replica.runtime.mongo = object()
    service = Snapshot(configured, store, remote)
    replica.runtime.backup, replica.runtime.pool = service, Pool(remote)
    transport = remote.factory(configured, "234567:" + "b" * 24)
    await transport.start()
    try:
        remote.lose_response = True
        with pytest.raises(ReplicaTransportError):
            await mirror_mongodb(
                replica,
                transport,
                source_channel=configured.storage_channel,
                destination_channel=BACKUP,
                revision=store.revision,
            )
        assert service.count == 1
        result = await mirror_mongodb(
            replica,
            transport,
            source_channel=configured.storage_channel,
            destination_channel=BACKUP,
            revision=store.revision,
        )
        assert service.count == 1, (
            "The chosen confirmed ciphertext must survive retries"
        )
        assert result["format"] == "mongodb-encrypted-v1"
        assert result["source_message_id"] == 900
        assert result["replica_channel"] == BACKUP
        assert (
            remote.messages[BACKUP, result["replica_message_id"]]
            == remote.messages[configured.storage_channel, 900]
        )
        attempts = [call for call in remote.calls if call[0] == "copy"]
        assert len(attempts) == 2 and attempts[0][-1] == attempts[1][-1]
        await mirror_mongodb(
            replica,
            transport,
            source_channel=configured.storage_channel,
            destination_channel=BACKUP,
            revision=store.revision,
        )
        assert len([call for call in remote.calls if call[0] == "copy"]) == 2
        assert any(call[0] == "verify" for call in remote.calls)
    finally:
        await transport.close()
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_same_catalog_with_changed_settings_gets_a_fresh_full_snapshot(tmp_path):
    configured, store, remote, replica, _ = setup(tmp_path)
    configured.mongodb_backup_key = SecretStr("b2" * 32)
    replica.runtime.mongo = object()
    service = Snapshot(configured, store, remote)
    replica.runtime.backup, replica.runtime.pool = service, Pool(remote)
    transport = remote.factory(configured, "234567:" + "b" * 24)
    await transport.start()
    revision = store.revision
    try:
        for fingerprint, expected in [("a1" * 32, 1), ("a1" * 32, 1), ("b2" * 32, 2)]:
            await mirror_mongodb(
                replica,
                transport,
                source_channel=configured.storage_channel,
                destination_channel=BACKUP,
                revision=revision,
                settings_fingerprint=fingerprint,
            )
            assert service.count == expected
            assert store.revision == revision
        # An older generation remains replayable; switching back does not
        # overwrite either durable snapshot selection or request a third one.
        await mirror_mongodb(
            replica,
            transport,
            source_channel=configured.storage_channel,
            destination_channel=BACKUP,
            revision=revision,
            settings_fingerprint="a1" * 32,
        )
        assert service.count == 2
        with pytest.raises(SettingsProblem, match="fingerprint"):
            await mirror_mongodb(
                replica,
                transport,
                source_channel=configured.storage_channel,
                destination_channel=BACKUP,
                settings_fingerprint="not-a-digest",
            )
        assert service.count == 2
    finally:
        await transport.close()
        await dispose(replica, store)


def test_pending_mongo_ciphertext_must_match_current_recovery_key(tmp_path):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from tests.support.recovery_fixtures import settings
    from tgdrive.recovery.mongo_backup import MAGIC
    from tgdrive.replication.replica_mongo import _check_pending_key

    configured = settings(tmp_path)
    root = configured.resolved_cache_dir / "mongo-backups"
    root.mkdir(parents=True)
    path = root / "pending.tgdb"
    nonce = b"n" * 12
    key = "a1" * 32
    path.write_bytes(
        MAGIC
        + nonce
        + AESGCM(bytes.fromhex(key)).encrypt(nonce, b"compressed records", MAGIC)
    )
    snapshot = path.read_bytes()
    service = SimpleNamespace(
        backend=SimpleNamespace(
            db=SimpleNamespace(
                backups=SimpleNamespace(
                    find_one=lambda *args, **kwargs: {"local_path": str(path)}
                )
            )
        )
    )
    _check_pending_key(service, configured, key)
    with pytest.raises(SettingsProblem) as error:
        _check_pending_key(service, configured, "b2" * 32)
    assert error.value.code == "REPLICA_MONGODB_KEY_MISMATCH"
    assert path.read_bytes() == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["flood", "network"])
async def test_full_snapshot_errors_are_retryable_and_never_echo_provider_secrets(
    tmp_path, kind
):
    from tgdrive.recovery.backup import BackupError

    configured, store, remote, replica, _ = setup(tmp_path)
    configured.mongodb_backup_key = SecretStr("a1" * 32)
    replica.runtime.mongo = object()
    error = (
        BackupError("credential=private", retry_after=123)
        if kind == "flood"
        else TimeoutError("credential=private")
    )
    replica.runtime.backup = SimpleNamespace(snapshot_now=AsyncMock(side_effect=error))
    transport = remote.factory(configured, "234567:" + "b" * 24)
    try:
        with pytest.raises(ReplicaTransportError) as raised:
            await mirror_mongodb(
                replica,
                transport,
                source_channel=configured.storage_channel,
                destination_channel=BACKUP,
            )
        assert raised.value.retryable
        assert "credential=private" not in str(raised.value)
        if kind == "flood":
            assert raised.value.retry_after == 123
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,retryable",
    [
        (ConnectionFailure("credential=private"), True),
        (OperationFailure("credential=private", code=13), False),
        (DatabaseUnavailable("credential=private"), False),
    ],
)
async def test_snapshot_database_errors_reach_shared_safe_failure_handling(
    tmp_path, error, retryable
):
    configured, store, remote, replica, _ = setup(tmp_path)
    configured.mongodb_backup_key = SecretStr("a1" * 32)
    replica.runtime.mongo = object()
    replica.runtime.backup = SimpleNamespace(snapshot_now=AsyncMock(side_effect=error))
    transport = remote.factory(configured, "234567:" + "b" * 24)
    try:
        with pytest.raises(type(error)) as raised:
            await mirror_mongodb(
                replica,
                transport,
                source_channel=configured.storage_channel,
                destination_channel=BACKUP,
            )
        assert raised.value is error
        replica.runtime.backup.snapshot_now.assert_awaited_once_with(store, legacy=True)
        failure = replica._record_failure(raised.value)
        assert failure.retryable is retryable
        assert replica.status()["phase"] == ("waiting" if retryable else "error")
        assert "credential=private" not in str(failure)
        assert "credential=private" not in str(replica.status())
        assert replica.journal.get("restore_point") is None
        assert not remote.calls
    finally:
        await dispose(replica, store)
