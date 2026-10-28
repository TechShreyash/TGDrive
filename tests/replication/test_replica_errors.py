"""Database outages cannot discard confirmed or in-flight independent backups."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from pydantic import SecretStr
from pymongo.errors import (
    ConfigurationError,
    ConnectionFailure,
    ExecutionTimeout,
    OperationFailure,
)

from tests.replication.test_replica import BACKUP, dispose, setup
from tests.replication.test_replica import replica_backend as replica_backend
from tgdrive.recovery.backup import BackupError
from tgdrive.replication.replica import ChannelReplica
from tgdrive.replication.replica_transport import ReplicaTransportError
from tgdrive.storage.mongo import DatabaseUnavailable

pytestmark = pytest.mark.usefixtures("replica_backend")
PRIVATE_ERROR = (
    "mongodb://owner:private-password@synthetic.invalid/?token=private-token"
)


@pytest.mark.asyncio
async def test_saved_publication_wait_keeps_deadline_without_retryable_attribute(
    tmp_path,
):
    _, store, _, replica, _ = setup(tmp_path)
    error = BackupError(PRIVATE_ERROR, retry_after=120)
    try:
        assert replica._record_failure(error) is error
        state = replica.status()
        assert state["phase"] == "waiting"
        assert state["error"]["retry_at"] >= time.time() + 119
        assert PRIVATE_ERROR not in json.dumps(state)
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True], ids=["idle", "pending-copy"])
@pytest.mark.parametrize(
    "error",
    [ConnectionFailure(PRIVATE_ERROR), ExecutionTimeout(PRIVATE_ERROR, code=50)],
    ids=["connection", "timeout"],
)
async def test_database_outage_retries_same_backup_after_restart(
    tmp_path, monkeypatch, caplog, pending, error
):
    configured, store, remote, replica, config = setup(tmp_path)
    configured.mongodb_backup_key = SecretStr("a1" * 32)
    runtime = replica.runtime
    collection = runtime.mongo.db.settings
    find_one = collection.find_one
    failed = asyncio.Event()
    report = replica.report

    def record_failure(level, message):
        report(level, message)
        if level == "error":
            failed.set()

    def unavailable(*_args, **_kwargs):
        raise error

    try:
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        kit = replica.recovery_kit()
        originals = dict(remote.messages)
        if pending:
            store.add_file("/", "next.bin", 88, 4)
            remote.messages[configured.storage_channel, 88] = b"next"
            remote.lose_response = True
            with pytest.raises(ReplicaTransportError):
                await replica._sync(config)
        job = replica.journal.get("job")
        intents = replica.journal.db.execute(
            "SELECT * FROM copies ORDER BY key"
        ).fetchall()
        receipts = dict(remote.receipts)
        monkeypatch.setattr(collection, "find_one", unavailable)
        monkeypatch.setattr(replica, "report", record_failure)
        replica.start()
        await asyncio.wait_for(failed.wait(), 2)
        state = replica.status()
        assert state["phase"] == "waiting"
        assert state["error"]["code"] == "REPLICA_MONGODB_UNAVAILABLE"
        assert "MongoDB" in state["error"]["detail"]
        assert "Telegram" not in state["error"]["action"]
        assert PRIVATE_ERROR not in json.dumps(state) + caplog.text
        assert type(error).__name__ in caplog.text
        assert state["protected_revision"] == point["revision"]
        assert replica.journal.get("restore_point") == point
        assert replica.recovery_kit() == kit
        assert replica.journal.get("job") == job
        assert (
            replica.journal.db.execute("SELECT * FROM copies ORDER BY key").fetchall()
            == intents
        )
        assert remote.receipts == receipts
        deadline = state["error"]["retry_at"]
        assert deadline > time.time()
        await replica.close()

        replica = ChannelReplica(configured, runtime, transport_factory=remote.factory)
        assert replica.status()["error"]["retry_at"] == deadline
        assert replica.journal.get("job") == job
        assert replica.recovery_kit() == kit
        calls = list(remote.calls)
        replica.start()
        await asyncio.sleep(0.02)
        assert remote.calls == calls
        assert replica.status()["error"]["retry_at"] == deadline
        monkeypatch.setattr(collection, "find_one", find_one)
        clock = time.time
        monkeypatch.setattr(
            "tgdrive.replication.replica.time.time", lambda: clock() + 31
        )
        replica._wake.set()
        for _ in range(200):
            if replica.status()["phase"] == "protected":
                break
            await asyncio.sleep(0.01)
        assert replica.status()["phase"] == "protected"
        assert replica.status()["error"] is None
        assert replica.status()["protected_revision"] == store.revision
        assert replica.journal.get("job") is None
        assert replica.recovery_kit()["key"] == kit["key"]
        assert replica.recovery_kit()["mongodb_backup_key"] == kit["mongodb_backup_key"]
        assert all(
            remote.messages[identity] == body for identity, body in originals.items()
        )
        assert all(
            remote.receipts[identity] == receipt
            for identity, receipt in receipts.items()
        )
        assert remote.pins[BACKUP]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code",
    [
        (OperationFailure(PRIVATE_ERROR, code=13), "REPLICA_MONGODB_FAILED"),
        (ConfigurationError(PRIVATE_ERROR), "REPLICA_MONGODB_FAILED"),
        (DatabaseUnavailable(PRIVATE_ERROR), "REPLICA_MONGODB_FAILED"),
        (RuntimeError(PRIVATE_ERROR), "REPLICA_FAILED"),
    ],
    ids=["permissions", "configuration", "ownership", "unexpected"],
)
async def test_permanent_errors_stop_without_leaking_or_replacing_backup(
    tmp_path, monkeypatch, caplog, error, code
):
    _, store, remote, replica, config = setup(tmp_path)
    failed = asyncio.Event()
    report = replica.report
    attempts = 0

    def unavailable(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise error

    def record_failure(level, message):
        report(level, message)
        if level == "error":
            failed.set()

    try:
        await replica._sync(config)
        point, kit = replica.journal.get("restore_point"), replica.recovery_kit()
        originals = dict(remote.messages)
        monkeypatch.setattr(replica.runtime.mongo.db.settings, "find_one", unavailable)
        monkeypatch.setattr(replica, "report", record_failure)
        replica.start()
        await asyncio.wait_for(failed.wait(), 2)
        state = replica.status()
        assert state["phase"] == "error"
        assert state["error"]["code"] == code
        assert state["error"]["retry_at"] == 0
        assert PRIVATE_ERROR not in json.dumps(state) + caplog.text
        assert type(error).__name__ in caplog.text
        assert replica.journal.get("restore_point") == point
        assert state["protected_revision"] == point["revision"]
        assert replica.recovery_kit() == kit
        assert remote.messages == originals
        replica._wake.set()
        await asyncio.sleep(0.02)
        assert attempts == 1
    finally:
        await dispose(replica, store)
