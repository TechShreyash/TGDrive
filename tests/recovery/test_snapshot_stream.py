"""The streaming producer retains the authenticated V2 recovery contract."""

import hashlib
import stat
from contextlib import nullcontext
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from bson import json_util

from tests.support.mongo_fixtures import FakeMongoBackend
from tests.support.recovery_fixtures import settings as recovery_settings
from tgdrive.recovery import recovery_file, snapshot_stream
from tgdrive.recovery.backup import BackupError
from tgdrive.recovery.mongo_backup import COLLECTIONS
from tgdrive.recovery.staged_snapshot import stage_snapshot, validate_snapshot
from tgdrive.replication.replica_settings import credentials_from_snapshot
from tgdrive.storage.mongo import MongoBackend
from tgdrive.storage.mongo_store import MongoStore

KEY = bytes.fromhex("a1" * 32)


@pytest.fixture
def source(tmp_path):
    settings = recovery_settings(tmp_path)
    backend = FakeMongoBackend()
    session = object()
    backend.client = SimpleNamespace(
        start_session=Mock(return_value=nullcontext(session))
    )
    store = MongoStore.create(
        cast(MongoBackend, backend), storage_channel=settings.storage_channel
    )
    store.add_file("/", "Report.txt", 41, 3)
    backend.db.sessions.insert_one({"_id": "browser", "token": "sensitive-cookie"})
    backend.db.settings.insert_one(
        {
            "_id": "bootstrap",
            "document": {"STRING_SESSIONS": "sensitive-session"},
        }
    )
    managed = credentials_from_snapshot(settings, {"settings": []})["managed_document"]
    managed["clients"].append(
        {
            "id": "account",
            "label": "Account",
            "kind": "user",
            "enabled": True,
            "credential": "sensitive-session",
            "runtime_name": None,
        }
    )
    backend.db.settings.insert_one({"_id": "telegram", "document": managed})
    try:
        yield settings, backend, store, session
    finally:
        store.close()


def _validate_fixture(path):
    # Production supplies the bounded, disk-backed graph validator. This small
    # fixture also exercises the original complete catalog validation.
    with open(path, "rb") as source:
        envelope = json_util.loads(source.read())
    recovery_file.snapshot_catalog(envelope["database"])


def test_streaming_export_matches_existing_decoder(source, tmp_path):
    settings, backend, store, session = source
    path = tmp_path / "snapshot.tgdrive"
    expected = recovery_file.capture_database(backend)
    reads = []
    for name in COLLECTIONS:
        collection = backend.db[name]
        original = collection.find

        def find(*args, _name=name, _original=original, **kwargs):
            reads.append((_name, kwargs["session"]))
            return _original(*args, **kwargs)

        collection.find = find
    revision, counts, checksum = snapshot_stream.write_recovery_snapshot(
        backend, path, KEY, settings, validate_snapshot=_validate_fixture
    )
    decoded = recovery_file.decode_recovery_file(path.read_bytes(), KEY.hex())
    assert decoded["database"] == expected
    assert decoded["replica_catalog"] is None
    assert decoded["catalog"]["revision"] == revision == store.revision
    assert counts == {name: len(rows) for name, rows in expected.items()}
    assert checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b"sensitive-session" not in path.read_bytes()
    assert all(captured is session for _, captured in reads)
    assert {name for name, _ in reads} == set(COLLECTIONS) - {"sessions"}
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]
    backend.client.start_session.assert_called_with(snapshot=True)


def staged_source(source, tmp_path):
    settings, backend, _store, _session = source
    encrypted = tmp_path / "captured.tgdb"
    plaintext = tmp_path / "captured.json"
    snapshot_stream.write_recovery_snapshot(
        backend, encrypted, KEY, settings, validate_snapshot=validate_snapshot
    )
    snapshot_stream.decrypt_snapshot(encrypted, plaintext, KEY, settings)
    return stage_snapshot(plaintext, tmp_path / "captured.sqlite")


def test_staged_export_retains_existing_v2_mapping_contract(source, tmp_path):
    staged = staged_source(source, tmp_path)
    settings, _backend, _store, _session = source
    path = tmp_path / "replica.tgdb"
    target = -1002234567890
    revision, counts, checksum = snapshot_stream.write_staged_snapshot(
        staged,
        path,
        KEY,
        settings,
        validate_snapshot=validate_snapshot,
        replica_channel=target,
        replica_mapping=iter([(41, 141)]),
    )
    decoded = recovery_file.decode_recovery_file(path.read_bytes(), KEY.hex())
    assert decoded["catalog"]["revision"] == revision
    assert decoded["replica_catalog"]["storage_channel_id"] == target
    assert counts["nodes"] == 2
    assert checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    node = next(iter(decoded["replica_catalog"]["root"]["children"].values()))
    assert node["message_id"] == 141


def test_derived_snapshot_rebases_catalog_and_clears_mutation_receipts(
    source, tmp_path
):
    staged = staged_source(source, tmp_path)
    settings, _backend, _store, _session = source
    path = tmp_path / "rebased.json"
    target = -1002234567890
    snapshot_stream.write_staged_json(
        staged,
        path,
        settings,
        target_channel=target,
        mapping=lambda message: message + 100,
    )
    derived = stage_snapshot(path, tmp_path / "rebased.sqlite")
    assert derived.catalog["storage_channel_id"] == target
    assert derived.catalog["backed_up_revision"] == derived.catalog["revision"]
    assert (
        next(row for row in derived.nodes() if row["kind"] == "file")["message_id"]
        == 141
    )
    for name in ("operations", "deletions", "preparations", "receipts", "sessions"):
        assert list(derived.records(name)) == []


def test_invalid_staged_mapping_preserves_previous_confirmed_ciphertext(
    source, tmp_path
):
    staged = staged_source(source, tmp_path)
    settings, _backend, _store, _session = source
    path = tmp_path / "replica.tgdb"
    path.write_bytes(b"last good backup")
    with pytest.raises(ValueError):
        snapshot_stream.write_staged_snapshot(
            staged,
            path,
            KEY,
            settings,
            validate_snapshot=validate_snapshot,
            replica_channel=-1002234567890,
            replica_mapping=iter([(41, 141), (41, 241)]),
        )
    assert path.read_bytes() == b"last good backup"
    assert not any(path.name.startswith(".derived-") for path in tmp_path.iterdir())


@pytest.mark.parametrize(
    "limit",
    ["max_records", "max_database_bytes", "max_expanded_bytes", "max_recovery_bytes"],
)
def test_oversized_export_preserves_previous_file(source, tmp_path, limit):
    settings, backend, _store, _session = source
    path = tmp_path / "snapshot.tgdrive"
    path.write_bytes(b"last good backup")
    with pytest.raises(ValueError, match="limit"):
        snapshot_stream.write_recovery_snapshot(
            backend,
            path,
            KEY,
            settings,
            validate_snapshot=_validate_fixture,
            **{limit: 1},
        )
    assert path.read_bytes() == b"last good backup"
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


def test_export_reads_rows_and_writes_bytes_in_bounded_chunks(
    source, tmp_path, monkeypatch
):
    settings, backend, _store, _session = source
    backend.db.jobs.insert_one({"_id": "large", "summary": "x" * 200_000})
    writes = []
    reads = []
    original_flush = snapshot_stream._Writer.flush
    original_open = snapshot_stream.Path.open

    def flush(self):
        class Destination:
            def write(_self, chunk):
                writes.append(len(chunk))
                return destination.write(chunk)

        destination = self.stream
        self.stream = Destination()
        try:
            return original_flush(self)
        finally:
            self.stream = destination

    def open_path(path, mode="r", *args, **kwargs):
        opened = original_open(path, mode, *args, **kwargs)
        if mode != "rb" or "-json-" not in path.name or kwargs.get("buffering") != 0:
            return opened

        class Source:
            def __enter__(self):
                opened.__enter__()
                return self

            def __exit__(self, *args):
                return opened.__exit__(*args)

            def read(self, size):
                reads.append(size)
                assert size == snapshot_stream.IO_CHUNK_BYTES
                return opened.read(size)

        return Source()

    batch_sizes = []
    for name in COLLECTIONS:
        collection = backend.db[name]
        original = collection.find

        def find(*args, _original=original, **kwargs):
            rows = _original(*args, **kwargs)

            class Cursor:
                def batch_size(self, size):
                    batch_sizes.append(size)
                    return self

                def __iter__(self):
                    return iter(rows)

                def __len__(self):
                    raise AssertionError("A MongoDB cursor must not be materialized")

            return Cursor()

        collection.find = find
    monkeypatch.setattr(snapshot_stream._Writer, "flush", flush)
    monkeypatch.setattr(snapshot_stream.Path, "open", open_path)
    snapshot_stream.write_recovery_snapshot(
        backend,
        tmp_path / "snapshot.tgdrive",
        KEY,
        settings,
        validate_snapshot=_validate_fixture,
    )
    assert reads
    assert max(writes) <= snapshot_stream.IO_CHUNK_BYTES
    assert batch_sizes == [snapshot_stream.CURSOR_BATCH_RECORDS] * (
        len(COLLECTIONS) - 1
    )


@pytest.mark.parametrize("error", [ValueError, KeyboardInterrupt])
def test_invalid_source_never_publishes(source, tmp_path, error):
    settings, backend, _store, _session = source
    path = tmp_path / "snapshot.tgdrive"
    path.write_bytes(b"last good backup")

    def reject(path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        raise error("Invalid source catalog")

    with pytest.raises(error, match="Invalid source catalog"):
        snapshot_stream.write_recovery_snapshot(
            backend, path, KEY, settings, validate_snapshot=reject
        )
    assert path.read_bytes() == b"last good backup"
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


def test_encryption_rechecks_disk_reserve_after_validation(
    source, tmp_path, monkeypatch
):
    settings, backend, _store, _session = source
    path = tmp_path / "snapshot.tgdrive"
    path.write_bytes(b"last good backup")

    def validate(path):
        _validate_fixture(path)
        monkeypatch.setattr(
            snapshot_stream.shutil,
            "disk_usage",
            lambda _: SimpleNamespace(free=settings.min_free_bytes),
        )

    with pytest.raises(BackupError, match="Not enough temporary space"):
        snapshot_stream.write_recovery_snapshot(
            backend, path, KEY, settings, validate_snapshot=validate
        )
    assert path.read_bytes() == b"last good backup"
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]
