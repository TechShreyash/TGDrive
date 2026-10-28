from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from tgdrive.core.errors import (
    CorruptStoreError,
    PersistenceError,
    UnsupportedSchemaError,
    ValidationError,
)
from tgdrive.recovery.backup import decode_catalog_snapshot
from tgdrive.storage.file_parts import MAX_FILE_PARTS, validate_parts, validate_sha256
from tgdrive.storage.store import SCHEMA_VERSION, DriveStore

DATA = b"complete logical file"
SHA = hashlib.sha256(DATA).hexdigest()


def manifest():
    pieces = [DATA[:8], DATA[8:]]
    return [
        {
            "index": index,
            "offset": 0 if index == 0 else len(pieces[0]),
            "size": len(content),
            "message_id": 101 + index,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for index, content in enumerate(pieces)
    ]


def full(item):
    return item["path"].rstrip("/") + "/" + item["id"]


@pytest.fixture
def store():
    result = DriveStore.create()
    yield result
    result.close()


def add(store, *, operation_id="logical-upload-operation", parts=None):
    return store.add_file(
        "/",
        "large-video.mp4",
        101,
        len(DATA),
        parts=manifest() if parts is None else parts,
        sha256=SHA,
        operation_id=operation_id,
    )


def test_manifest_is_canonical_and_independent():
    original = manifest()
    original[0]["sha256"] = original[0]["sha256"].upper()
    result = validate_parts(original, size=len(DATA), message_id=101)
    assert result == manifest()
    result[0]["message_id"] = 999
    assert original[0]["message_id"] == 101
    assert validate_sha256(SHA.upper()) == SHA
    assert validate_sha256(None) is None
    with pytest.raises(ValidationError):
        validate_sha256(None, optional=False)


@pytest.mark.parametrize(
    "damage",
    [
        "empty",
        "limit",
        "not-list",
        "unknown-field",
        "missing-sha",
        "invalid-sha",
        "null-sha",
        "bool-index",
        "index-gap",
        "offset-gap",
        "overlap",
        "zero-size",
        "negative-size",
        "overflow",
        "duplicate-message",
        "anchor",
        "sum-short",
        "sum-long",
    ],
)
def test_invalid_manifests_never_create_partial_catalog_metadata(store, damage):
    parts = manifest()
    if damage == "empty":
        parts = []
    elif damage == "limit":
        parts = [parts[0]] * (MAX_FILE_PARTS + 1)
    elif damage == "not-list":
        parts = tuple(parts)
    elif damage == "unknown-field":
        parts[0]["token"] = "not-allowed"
    elif damage == "missing-sha":
        parts[0].pop("sha256")
    elif damage == "invalid-sha":
        parts[0]["sha256"] = "not-a-checksum"
    elif damage == "null-sha":
        parts[0]["sha256"] = None
    elif damage == "bool-index":
        parts[0]["index"] = False
    elif damage == "index-gap":
        parts[1]["index"] = 2
    elif damage == "offset-gap":
        parts[1]["offset"] += 1
    elif damage == "overlap":
        parts[1]["offset"] -= 1
    elif damage == "zero-size":
        parts[0]["size"] = 0
    elif damage == "negative-size":
        parts[0]["size"] = -1
    elif damage == "overflow":
        parts[0]["size"] = 1 << 63
    elif damage == "duplicate-message":
        parts[1]["message_id"] = 101
    elif damage == "anchor":
        parts[0]["message_id"] = 99
    elif damage == "sum-short":
        parts[1]["size"] -= 1
    elif damage == "sum-long":
        parts[1]["size"] += 1
    revision = store.revision
    with pytest.raises(ValidationError):
        add(store, parts=parts)
    assert store.list_directory("/") == {}
    assert store.revision == revision
    assert not store.has_file_operation("logical-upload-operation")


def test_atomic_commit_idempotency_and_public_records_do_not_expose_provider_parts(
    store,
):
    before = store.revision
    item = add(store)
    result = store.get_file(full(item))
    assert result["parts"] == manifest() and result["sha256"] == SHA
    assert result["message_id"] == 101 and result["part_count"] == 2
    assert store.revision == before + 1
    public = store.list_directory("/")[item["id"]]
    assert public["part_count"] == 2
    assert not {"parts", "sha256", "message_id"}.intersection(public)
    assert store.search_page("large")["items"][0]["part_count"] == 2
    repeated = store.add_file(
        "/",
        "renamed-on-retry.mp4",
        101,
        len(DATA),
        parts=manifest(),
        sha256=SHA,
        operation_id="logical-upload-operation",
        keep_both=True,
    )
    assert repeated["id"] == item["id"] and repeated["name"] == "large-video.mp4"
    assert store.revision == before + 1
    changed = manifest()
    changed[1]["message_id"] = 103
    with pytest.raises(ValidationError):
        add(store, parts=changed)
    assert store.revision == before + 1
    result["parts"][0]["message_id"] = 999
    assert store.get_file(full(item))["parts"] == manifest()


def test_part_insert_failure_rolls_back_node_operation_and_revision(store, monkeypatch):
    original = store._insert_parts
    before = store.revision

    def fail(database, identity, parts):
        original(database, identity, parts[:1])
        raise PersistenceError("simulated disk failure after first part")

    monkeypatch.setattr(store, "_insert_parts", fail)
    with pytest.raises(PersistenceError):
        add(store)
    assert store.revision == before and not store.list_directory("/")
    assert not store.has_file_operation("logical-upload-operation")
    with store._transaction() as database:
        assert database.execute("SELECT count(*) FROM file_parts").fetchone()[0] == 0
    monkeypatch.setattr(store, "_insert_parts", original)
    assert add(store)["part_count"] == 2


def test_multipart_move_share_trash_and_recover_keep_one_logical_identity(store):
    item = add(store)
    folder = store.create_folder("/", "Movies")
    token = store.create_share(full(item))
    moved = store.move(full(item), full(folder))
    file_path = full(folder) + "/" + item["id"]
    assert moved["id"] == item["id"]
    assert store.authorize_file(file_path, token)
    store.set_trashed(file_path, True)
    store.set_trashed(file_path, False)
    restored = DriveStore._from_payload(store.export(), revoke_shares=True)
    try:
        result = restored.get_file(file_path)
        assert result["parts"] == manifest() and result["sha256"] == SHA
        assert result["id"] == item["id"]
        assert not restored.authorize_file(file_path, token)
        assert restored.has_file_operation("logical-upload-operation")
    finally:
        restored.close()


def test_catalog_deletion_forgets_all_parts_without_remote_confirmations(store):
    item = add(store)
    store.set_trashed(full(item), True)
    intent = store.begin_deletion(full(item), channel_id=-1001234567890)
    assert intent["message_ids"] == [101, 102]
    store.confirm_deletion_messages(intent["id"], [101])
    recovered = DriveStore._from_payload(store.export())
    try:
        assert (
            recovered.get_file(full(item), include_trashed=True)["parts"] == manifest()
        )
        pending = recovered.pending_deletions()[0]
        assert pending["pending_message_ids"] == [102]
        assert recovered.finish_deletion(intent["id"]) == [101, 102]
        assert recovered.pending_deletions() == []
        assert recovered.export()["deletions"][0]["message_ids"] == []
        with recovered._transaction() as database:
            assert (
                database.execute("SELECT count(*) FROM file_parts").fetchone()[0] == 0
            )
        assert recovered.has_file_operation("logical-upload-operation")
        again = DriveStore._from_payload(recovered.export())
        again.close()
    finally:
        recovered.close()


def test_portable_v4_rejects_missing_delete_reference_or_changed_manifest(store):
    item = add(store)
    store.set_trashed(full(item), True)
    store.begin_deletion(full(item))
    payload = store.export()
    damaged = copy.deepcopy(payload)
    damaged["deletions"][0]["message_ids"] = [101]
    damaged["deletions"][0]["pending_message_ids"] = [101]
    with pytest.raises(CorruptStoreError):
        DriveStore._from_payload(damaged)
    damaged = copy.deepcopy(payload)
    damaged["root"]["children"][item["id"]]["parts"][1]["offset"] += 1
    with pytest.raises(CorruptStoreError):
        DriveStore._from_payload(damaged)


def test_portable_catalog_recovery_keeps_all_part_ids_and_revokes_shares():
    source = DriveStore.create()
    recovered = None
    try:
        item = add(source)
        token = source.create_share(full(item))
        payload = decode_catalog_snapshot(source.backup_snapshot()[1])
        assert payload["schema_version"] == SCHEMA_VERSION
        recovered = DriveStore._from_payload(payload, revoke_shares=True)
        result = recovered.get_file(full(item))
        assert result["parts"] == manifest() and result["sha256"] == SHA
        assert result["message_id"] == 101 and result["id"] == item["id"]
        assert recovered.drive_id == source.drive_id
        assert recovered.has_file_operation("logical-upload-operation")
        assert not recovered.authorize_file(full(item), token)
    finally:
        if recovered is not None:
            recovered.close()
        source.close()


def test_exact_4096_part_boundary_is_supported_atomically(store):
    checksum = hashlib.sha256(b"x").hexdigest()
    parts = [
        {
            "index": index,
            "offset": index,
            "size": 1,
            "message_id": index + 1,
            "sha256": checksum,
        }
        for index in range(MAX_FILE_PARTS)
    ]
    item = store.add_file("/", "many-parts.bin", 1, MAX_FILE_PARTS, parts=parts)
    result = store.get_file(full(item))
    assert result["part_count"] == MAX_FILE_PARTS
    assert result["parts"] == parts
    assert len(store.message_ids(full(item))) == MAX_FILE_PARTS


@pytest.mark.parametrize("document_id", [1, -1, (1 << 63) - 1, -(1 << 63)])
def test_optional_immutable_document_id_roundtrips_without_public_leak(
    store, document_id
):
    parts = manifest()
    parts[0]["document_id"] = document_id
    item = add(store, parts=parts)
    assert store.get_file(full(item))["parts"] == parts
    restored = DriveStore._from_payload(store.export())
    try:
        assert restored.get_file(full(item))["parts"] == parts
        assert "document_id" not in json.dumps(restored.list_directory("/"))
        assert "document_id" not in parts[1]
        changed = copy.deepcopy(parts)
        changed[0]["document_id"] = 999
        with pytest.raises(ValidationError):
            add(restored, parts=changed)
    finally:
        restored.close()


@pytest.mark.parametrize("document_id", [None, 0, True, "123", 1 << 63, -(1 << 63) - 1])
def test_present_document_identity_must_be_a_nonzero_signed_integer(store, document_id):
    parts = manifest()
    parts[0]["document_id"] = document_id
    with pytest.raises(ValidationError):
        add(store, parts=parts)
    assert not store.list_directory("/")


@pytest.mark.parametrize("version", range(1, 6))
def test_previous_database_schema_is_rejected_without_upgrade(tmp_path, version):
    database = tmp_path / "drive.sqlite3"
    source = DriveStore.create(database)
    source.add_file("/", "current.bin", 333, 13)
    source.close()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()
    with pytest.raises(UnsupportedSchemaError, match="database schema"):
        DriveStore.load(database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version


@pytest.mark.parametrize("version", range(1, SCHEMA_VERSION))
def test_previous_portable_schema_is_rejected(store, version):
    payload = store.export()
    payload["schema_version"] = version
    with pytest.raises(UnsupportedSchemaError, match="metadata schema"):
        DriveStore._from_payload(payload)
