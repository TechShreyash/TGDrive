"""Recovery envelope integrity and durable replica journal fault contracts."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import Binary, Int64
from pydantic import SecretStr

from tests.support.mongo_fixtures import FakeMongoBackend, new_store
from tgdrive.replication.replica import ChannelReplica
from tgdrive.replication.replica_settings import POLICIES
from tgdrive.replication.replica_state import (
    BoundedPrivateReader,
    PrivateRecoveryFileTooLarge,
    ReplicaJournal,
    atomic_private,
    files_in,
    parts_in,
    private_reader,
    read_legacy_value,
    read_private,
    remap_catalog,
)
from tgdrive.settings.managed_settings import ManagedClient, ManagedDocument

MAIN = -1001234567890
BACKUP = -1002234567890
KEY = "a1" * 32


def payload():
    store = new_store()
    try:
        store.bind_storage_channel(MAIN)
        folder = store.create_folder("/", "Records")
        path = "/" + folder["id"]
        first = store.add_file(path, "Report.txt", 11, 7)
        store.add_file(path, "Second.txt", 12, 8)
        store.set_starred(path + "/" + first["id"], True)
        store.create_share(path, label="Team", expires_at=None)
        catalog = store.export()
    finally:
        store.close()
    mapping = {
        str(part["message_id"]): part["message_id"] + 100
        for node in files_in(catalog)
        for part in parts_in(node)
    }
    return {
        "format": "tgdrive-replica",
        "version": 1,
        "complete": True,
        "created_at": "2026-10-22T10:00:00Z",
        "catalog": catalog,
        "replica_catalog": remap_catalog(catalog, BACKUP, mapping),
        "credentials": {
            "api_id": 12345,
            "api_hash": "a" * 32,
            "session_secret": "s" * 48,
            "bot_tokens": ["123456:" + "x" * 30],
            "managed_document": json.loads(
                ManagedDocument(
                    revision=1,
                    api_id=12345,
                    api_hash=SecretStr("a" * 32),
                    clients=[
                        ManagedClient(
                            id="storage-bot",
                            label="Storage bot",
                            kind="bot",
                            credential=SecretStr("123456:" + "x" * 30),
                        )
                    ],
                ).private_bytes()
            ),
            "preferences": {
                filename: model.model_validate({}).model_dump()
                for filename, model in POLICIES.items()
            },
        },
    }


def test_remap_does_not_mutate_original_and_requires_every_part():
    original = payload()["catalog"]
    preserved = copy.deepcopy(original)
    with pytest.raises(KeyError):
        remap_catalog(original, BACKUP, {"11": 111})
    assert original == preserved


def journal_for(tmp_path, backend):
    return ReplicaJournal(tmp_path, backend)


def test_journal_retains_exact_intent_and_confirmation_across_restarts(tmp_path):
    backend = FakeMongoBackend()
    journal = journal_for(tmp_path, backend)
    identity, receipt = journal.intent("drive:source:target:part", 123)
    assert type(identity) is int and identity > 0 and receipt is None
    journal.set("job", {"phase": "copying", "checkpoint": 17})
    journal.confirm("drive:source:target:part", 901)
    # A new server with the same MongoDB sees the same journal.
    journal = journal_for(tmp_path / "another-server", backend)
    assert journal.intent("drive:source:target:part", 123) == (identity, 901)
    assert journal.get("job") == {"phase": "copying", "checkpoint": 17}
    with pytest.raises(ValueError, match="size changed"):
        journal.intent("drive:source:target:part", 124)
    assert journal.intent("drive:source:different-target:part", 123)[0] != identity
    assert not list(tmp_path.glob("*.sqlite3"))


def test_large_journal_values_are_chunked_and_round_trip(tmp_path):
    journal = journal_for(tmp_path, FakeMongoBackend())
    value = {"database": "x" * (ReplicaJournal.CHUNK * 2 + 5)}
    journal.set("job", value)
    assert journal.get("job") == value
    assert journal.backend.db.replica_chunks.count_documents({"key": "job"}) == 3
    journal.set("job", None)
    assert journal.get("job", "missing") is None
    assert journal.backend.db.replica_chunks.count_documents({"key": "job"}) == 1


def test_atomic_state_rejects_unserializable_value_without_changing_any_key(tmp_path):
    backend = FakeMongoBackend()
    journal = journal_for(tmp_path, backend)
    journal.set_many({"restore_point": {"revision": 17}, "restore_point_key": KEY})
    with pytest.raises(ValueError):
        journal.set_many(
            {"restore_point": {"revision": 18}, "restore_point_key": float("nan")}
        )
    reopened = journal_for(tmp_path, backend)
    assert reopened.get("restore_point") == {"revision": 17}
    assert reopened.get("restore_point_key") == KEY


def test_failed_write_cannot_pair_new_restore_point_with_old_keys(
    tmp_path, monkeypatch
):
    old = {
        "restore_point": {"revision": 17, "backup_message_id": 901},
        "restore_point_key": KEY,
        "restore_point_mongodb_key": "b2" * 32,
        "credentials_digest": "a" * 64,
    }
    new = {
        "restore_point": {"revision": 18, "backup_message_id": 902},
        "restore_point_key": "c3" * 32,
        "restore_point_mongodb_key": None,
        "credentials_digest": "d" * 64,
    }
    backend = FakeMongoBackend()
    journal = journal_for(tmp_path, backend)
    journal.set_many(old)
    state = backend.db.replica_state
    replace = state.replace_one

    def fail_on_key(query, document, **kwargs):
        if query["_id"] == "restore_point_key":
            raise RuntimeError("injected key write failure")
        return replace(query, document, **kwargs)

    monkeypatch.setattr(state, "replace_one", fail_on_key)
    with pytest.raises(RuntimeError, match="injected"):
        journal.set_many(new)
    monkeypatch.undo()
    reopened = journal_for(tmp_path, backend)
    assert {key: reopened.get(key) for key in old} == old
    reopened.set_many(new)
    assert {key: reopened.get(key) for key in new} == new
    assert reopened.get("restore_point_mongodb_key", "stale") is None


def test_private_archive_write_and_read_reject_world_readable_or_symlink(tmp_path):
    target = tmp_path / "checkpoint.tgdb"
    atomic_private(target, b"encrypted archive")
    assert read_private(target) == b"encrypted archive"
    target.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_private(target)
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        read_private(link)
    assert target.read_bytes() == b"encrypted archive"


def test_private_record_oversize_is_typed_and_preserves_the_sparse_file(tmp_path):
    maximum = 64 * 1024**2
    target = tmp_path / "activation.json"
    with target.open("wb") as record:
        record.write(b"preserve this activation")
        record.truncate(maximum + 1)
    target.chmod(0o600)
    before = target.stat()
    with pytest.raises(PrivateRecoveryFileTooLarge) as failure:
        read_private(target)
    assert failure.value.maximum_bytes == maximum
    assert target.stat().st_size == before.st_size
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    with target.open("rb") as record:
        assert record.read(24) == b"preserve this activation"
    assert list(tmp_path.iterdir()) == [target]


def test_private_reader_bounds_growth_on_the_same_checked_file(tmp_path, monkeypatch):
    import tgdrive.replication.replica_state as state

    target = tmp_path / "activation.json"
    atomic_private(target, b"a")
    monkeypatch.setattr(state, "_private_maximum_bytes", lambda: 4)
    with private_reader(target) as source:
        with target.open("ab") as growing:
            growing.write(b"bcde")
        with pytest.raises(PrivateRecoveryFileTooLarge) as failure:
            source.read()
        assert failure.value.maximum_bytes == 4
    assert target.read_bytes() == b"abcde"
    reader = BoundedPrivateReader(io.BytesIO(b"abcde!"), 5)
    assert reader.read(3) == b"abc"
    with pytest.raises(PrivateRecoveryFileTooLarge):
        reader.read(3)
    assert reader.consumed_bytes == 5


@pytest.mark.parametrize("source_chunk", [97, 1024**2])
@pytest.mark.parametrize("extra", [0, 1])
def test_legacy_reader_enforces_exact_serialized_utf8_limit_including_whitespace(
    source_chunk, extra
):
    from tgdrive.recovery.staged_snapshot import MAX_VALUE_BYTES, RecoveryResourceLimit

    class WhitespaceSource:
        def __init__(self):
            self.position = 0

        def read(self, size):
            count = min(size, source_chunk, MAX_VALUE_BYTES + extra - self.position)
            prefix = b"null"[self.position : self.position + count]
            self.position += count
            return prefix + b" " * (count - len(prefix))

    source = WhitespaceSource()
    assert MAX_VALUE_BYTES == 32 * 1024**2
    if extra:
        with pytest.raises(RecoveryResourceLimit):
            read_legacy_value(source)
        assert source.position == MAX_VALUE_BYTES + 1
    else:
        assert read_legacy_value(source) is None
        assert source.position == MAX_VALUE_BYTES


def test_journal_resource_rejection_is_memoized_until_a_replacement_write(
    tmp_path, monkeypatch
):
    import tgdrive.recovery.staged_snapshot as staged
    from tgdrive.replication.replica_activation import ActivationError

    backend = FakeMongoBackend()
    journal = journal_for(tmp_path, backend)
    raw = json.dumps({"items": [None] * 16})
    journal._write_state({"candidate": raw}, None)  # A historical writer's value.
    original = backend.db.replica_chunks.find
    reads = []

    def read(*args, **kwargs):
        reads.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(backend.db.replica_chunks, "find", read)
    monkeypatch.setattr(staged, "MAX_VALUE_ITEMS", 8)
    for _ in range(3):
        with pytest.raises(ActivationError) as failure:
            journal.get("candidate")
        assert failure.value.code == "ACTIVATION_RESOURCE_LIMIT"
        assert failure.value.status == 409
    assert len(reads) == 1
    memo = journal.resource_error()
    assert memo["key"] == "candidate" and memo["code"] == "ACTIVATION_RESOURCE_LIMIT"
    memo["detail"] = "changed outside the journal"
    assert journal.resource_error()["detail"] != memo["detail"]
    assert backend.db.replica_chunks.find_one({"key": "candidate"})["data"] == raw
    journal.set("candidate", {"repaired": True})
    assert journal.resource_error() is None
    before_refresh = len(reads)
    assert journal.get("candidate") == {"repaired": True}
    assert len(reads) == before_refresh + 1


def test_new_journal_values_must_remain_readable_before_any_related_write(
    tmp_path, monkeypatch
):
    import tgdrive.recovery.staged_snapshot as staged
    from tgdrive.replication.replica_activation import ActivationError

    journal = journal_for(tmp_path, FakeMongoBackend())
    journal.set_many({"config": {"keep": True}, "candidate": {"old": True}})
    monkeypatch.setattr(staged, "MAX_VALUE_ITEMS", 8)
    with pytest.raises(ActivationError) as failure:
        journal.set_many(
            {"config": {"keep": False}, "candidate": {"items": [None] * 16}}
        )
    assert failure.value.code == "ACTIVATION_RESOURCE_LIMIT"
    assert journal.get_many({"config": None, "candidate": None}) == {
        "config": {"keep": True},
        "candidate": {"old": True},
    }
    assert journal.resource_error() is None


def test_journal_chunk_stream_keeps_utf8_characters_and_legacy_bson_types(
    tmp_path, monkeypatch
):
    journal = journal_for(tmp_path, FakeMongoBackend())
    monkeypatch.setattr(journal, "CHUNK", 7)
    value = {
        "name": "漢😀" * 30,
        "at": datetime(2026, 10, 28, tzinfo=UTC),
        "large": Int64(2**40),
        "data": Binary(b"abc", 128),
    }
    journal.set("candidate", value)
    assert journal.get("candidate") == value
    chunks = journal.backend.db.replica_chunks
    assert chunks.count_documents({"key": "candidate"}) > 1
    assert journal.backend.db.replica_state.find_one({"_id": "candidate"})[
        "length"
    ] == len(
        "".join(
            row["data"] for row in chunks.find({"key": "candidate"}).sort("index", 1)
        )
    )
    chunks.update_one({"key": "candidate", "index": 1}, {"$set": {"index": 2}})
    with pytest.raises(ValueError, match="incomplete"):
        journal.get("candidate")


def test_duplicate_journal_fields_remain_invalid_not_resource_exhaustion(tmp_path):
    journal = journal_for(tmp_path, FakeMongoBackend())
    journal._write_state({"candidate": '{"same":1,"same":2}'}, None)
    with pytest.raises(ValueError, match="Duplicate"):
        journal.get("candidate")
    assert journal.resource_error() is None


def test_related_journal_reads_cannot_mix_a_concurrent_commit(tmp_path, monkeypatch):
    journal = journal_for(tmp_path, FakeMongoBackend())
    old = {"config": {"enabled": True}, "status": {"phase": "protected"}}
    new = {"config": {"enabled": False}, "status": {"phase": "disabled"}}
    journal.set_many(old)
    first_read, writer_attempted = threading.Event(), threading.Event()
    original_lock = journal.lock

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "journal-writer":
                writer_attempted.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    journal.lock = ObservedLock()
    original_get = journal.get

    def read(key, default=None):
        value = original_get(key, default)
        if key == "config" and threading.current_thread().name == "journal-reader":
            first_read.set()
            assert writer_attempted.wait(2)
        return value

    monkeypatch.setattr(journal, "get", read)
    results, errors = [], []

    def run(operation):
        try:
            operation()
        except Exception as error:
            errors.append(error)

    reader = threading.Thread(
        target=lambda: run(lambda: results.append(journal.get_many(old))),
        name="journal-reader",
    )
    writer = threading.Thread(
        target=lambda: run(lambda: journal.set_many(new)), name="journal-writer"
    )
    reader.start()
    try:
        assert first_read.wait(2)
        writer.start()
    finally:
        reader.join(3)
        if writer.ident is not None:
            writer.join(3)
    assert not reader.is_alive() and not writer.is_alive()
    assert not errors
    assert results == [old]
    assert journal.get_many(old) == new


@pytest.mark.parametrize("operation", ["mutation", "publish"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_blocking_replica_work_keeps_loop_responsive_and_drains_cancellation(
    tmp_path, operation, cancel
):
    replica = ChannelReplica.__new__(ChannelReplica)
    replica.directory = tmp_path
    replica._operation = asyncio.Lock()
    replica.journal = SimpleNamespace(get=lambda *_args: {"phase": "paused"})
    entered, release, completed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    loop_thread = threading.get_ident()

    def blocked(*_args):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3)
        completed.set()
        if operation == "mutation":
            return None
        return "a" * 64, tmp_path / "bundle", 2, "b" * 64, 0, "c" * 64

    if operation == "mutation":
        replica._mutation_config = blocked
        task = asyncio.create_task(replica.before_mutation())
    else:
        replica._stage_bundle = blocked
        task = asyncio.create_task(
            replica._publish(None, -1001, -1002, {}, {}, {}, "key")
        )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert completed.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_copy_progress_coalesces_without_coalescing_durable_receipts(
    monkeypatch,
):
    import tgdrive.replication.replica as replica_module

    replica = ChannelReplica.__new__(ChannelReplica)
    updates, receipts = [], []
    replica.update = lambda **values: updates.append(values)
    replica.journal = SimpleNamespace(
        get=lambda *_args: {},
        intent=lambda identity, size: (size, None),
        confirm=lambda identity, message: receipts.append((identity, message)),
    )
    monkeypatch.setattr(replica_module, "time", SimpleNamespace(monotonic=lambda: 0))
    transport = SimpleNamespace(copy=AsyncMock(side_effect=[111, 112]))
    catalog = payload()["catalog"]
    restored = await replica._copy_catalog(transport, catalog, BACKUP)
    assert len(updates) == 2
    assert updates[-1]["done_files"] == 2
    assert updates[-1]["done_parts"] == 2
    assert updates[-1]["done_bytes"] == 15
    assert len(receipts) == transport.copy.await_count == 2
    assert [node["message_id"] for node in files_in(restored)] == [111, 112]
