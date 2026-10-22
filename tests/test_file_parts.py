from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
from test_backup import FakeBackupClient, settings

from tgdrive.backup import TelegramBackupService
from tgdrive.client_pool import ClientPool
from tgdrive.errors import CorruptStoreError, PersistenceError, ValidationError
from tgdrive.file_parts import MAX_FILE_PARTS, validate_parts, validate_sha256
from tgdrive.store import SCHEMA_VERSION, DriveStore

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


def test_durable_deletion_retains_and_confirms_all_parts_before_cascade(store):
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
        with pytest.raises(ValidationError):
            recovered.finish_deletion(intent["id"])
        recovered.confirm_deletion_messages(intent["id"], [102])
        assert recovered.finish_deletion(intent["id"]) == [101, 102]
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


def downgrade_database_to_v3(database):
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP TABLE file_parts")
        connection.execute("ALTER TABLE nodes DROP COLUMN part_count")
        connection.execute("ALTER TABLE nodes DROP COLUMN sha256")
        connection.execute("ALTER TABLE nodes DROP COLUMN starred")
        connection.execute("PRAGMA user_version=3")
        connection.commit()


def test_database_v3_upgrade_changes_format_revision_once_preserves_message_ids(
    tmp_path,
):
    database = tmp_path / "drive.sqlite3"
    source = DriveStore.create(database)
    item = source.add_file("/", "legacy.bin", 333, 13, operation_id="legacy-upload")
    identity, revision = source.drive_id, source.revision
    source.mark_backed_up(revision)
    source.close()
    downgrade_database_to_v3(database)
    upgraded = DriveStore.load(database)
    try:
        assert upgraded.drive_id == identity and upgraded.revision == revision + 1
        assert upgraded.dirty and upgraded.get_file(full(item))["message_id"] == 333
        assert "parts" not in upgraded.get_file(full(item))
        assert upgraded.has_file_operation("legacy-upload")
        assert upgraded.export()["schema_version"] == 4
    finally:
        upgraded.close()
    reopened = DriveStore.load(database)
    assert reopened.revision == revision + 1
    reopened.close()


def test_portable_v3_upgrade_is_explicit_and_keeps_legacy_anchor(store):
    item = store.add_file("/", "legacy.bin", 333, 13)
    payload = store.export()
    payload["schema_version"] = 3
    revision = payload["revision"]
    upgraded = DriveStore._from_payload(payload)
    try:
        assert upgraded.revision == revision + 1 and upgraded.dirty
        assert upgraded.get_file(full(item))["message_id"] == 333
        assert upgraded.get_file(full(item))["part_count"] == 1
        assert upgraded.export()["schema_version"] == SCHEMA_VERSION
    finally:
        upgraded.close()
    payload["root"]["children"][item["id"]]["parts"] = manifest()
    with pytest.raises(CorruptStoreError):
        DriveStore._from_payload(payload)


@pytest.mark.asyncio
async def test_fresh_telegram_recovery_keeps_all_part_ids_without_local_database(
    tmp_path,
):
    source_settings = settings(tmp_path / "source")
    client = FakeBackupClient()
    pool = ClientPool([client])
    await pool.start()
    source_service = TelegramBackupService(source_settings, pool)
    source = DriveStore.create(source_service.store_path)
    recovered = None
    try:
        item = add(source)
        token = source.create_share(full(item))
        await source_service.ensure_revision(source, source.revision)
        remote_bytes = next(iter(client.sent.values()))[1]
        assert json.loads(remote_bytes)["schema_version"] == 4
        recovery_service = TelegramBackupService(settings(tmp_path / "fresh"), pool)
        assert not recovery_service.store_path.exists()
        recovered = await recovery_service.recover_latest()
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
        await pool.stop()


@pytest.mark.asyncio
async def test_pending_v3_checkpoint_bytes_are_never_rewritten_by_database_upgrade(
    tmp_path,
):
    source_settings = settings(tmp_path)
    client = FakeBackupClient()
    pool = ClientPool([client])
    await pool.start()
    service = TelegramBackupService(source_settings, pool)
    source = DriveStore.create(service.store_path)
    source.bind_storage_channel(source_settings.storage_channel)
    source.add_file("/", "legacy.bin", 333, 13)
    payload = source.export()
    payload.update(schema_version=3, backed_up_revision=source.revision)
    content = source._json_bytes(payload)
    checkpoint = service._prepare_checkpoint(source, (source.revision, content))
    immutable = (
        source_settings.resolved_data_dir / "backups" / (checkpoint["id"] + ".json")
    )
    source.close()
    downgrade_database_to_v3(service.store_path)
    upgraded = DriveStore.load(service.store_path)
    try:
        assert upgraded.revision == checkpoint["revision"] + 1
        await service.ensure_revision(upgraded, upgraded.revision)
        assert immutable.read_bytes() == content
        sent = [json.loads(body) for _, body in client.sent.values()]
        assert [document["schema_version"] for document in sent] == [3, 4]
        assert service.protection_status(upgraded)["state"] == "protected"
        assert not service.recovery_required
    finally:
        upgraded.close()
        await pool.stop()


@pytest.mark.asyncio
async def test_fresh_v3_pin_recovery_publishes_v4_at_a_new_revision(tmp_path):
    configured = settings(tmp_path)
    client = FakeBackupClient()
    pool = ClientPool([client])
    await pool.start()
    source = DriveStore.create()
    source.bind_storage_channel(configured.storage_channel)
    item = source.add_file("/", "legacy.bin", 777, 11, operation_id="legacy-operation")
    payload = source.export()
    payload.update(schema_version=3, backed_up_revision=source.revision)
    content = source._json_bytes(payload)
    caption = TelegramBackupService._caption(
        {
            "drive_id": source.drive_id,
            "channel_id": configured.storage_channel,
            "revision": source.revision,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    )
    client.sent[11] = (100, content)
    client.captions[100] = caption
    client.pinned = 100
    service = TelegramBackupService(configured, pool)
    recovered = None
    try:
        recovered = await service.recover_latest()
        assert recovered.revision == source.revision + 1
        assert recovered.get_file(full(item))["message_id"] == 777
        assert recovered.has_file_operation("legacy-operation")
        await service.ensure_revision(recovered, recovered.revision)
        assert client.sent[11][1] == content
        assert json.loads(list(client.sent.values())[-1][1])["schema_version"] == 4
        assert service.protection_status(recovered)["state"] == "protected"
        assert not service.recovery_required
    finally:
        if recovered is not None:
            recovered.close()
        source.close()
        await pool.stop()


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
