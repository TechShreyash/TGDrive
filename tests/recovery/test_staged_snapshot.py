"""Disk-backed recovery validation preserves the V2 catalog contract."""

import copy
import hashlib
import io
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from itertools import chain
from types import SimpleNamespace

import pytest
from bson import BSON, Binary, ObjectId, json_util

from tests.support.mongo_fixtures import FakeMongoBackend
from tests.support.recovery_fixtures import settings as recovery_settings
from tgdrive.recovery import staged_snapshot
from tgdrive.recovery.mongo_backup import COLLECTIONS
from tgdrive.recovery.recovery_file import LegacyRecoveryFile, snapshot_catalog
from tgdrive.replication.replica_settings import credentials_from_snapshot
from tgdrive.storage.mongo_store import MongoStore

MAIN = -1001234567890
BACKUP = -1002234567890


@pytest.fixture
def snapshot(tmp_path):
    backend = FakeMongoBackend()
    store = MongoStore.create(backend, storage_channel=MAIN)
    try:
        folder = store.create_folder("/", "Folder")
        store.add_file("/" + folder["id"], "Document.txt", 41, 3)
        records = {name: list(backend.db[name].find({})) for name in COLLECTIONS}
        records["jobs"] = [
            {
                "_id": ObjectId("000000000000000000000001"),
                "finished_at": datetime(2026, 10, 28, tzinfo=UTC),
                "data": Binary(b"example", 128),
            }
        ]
        yield {
            "format": "tgdrive-backup",
            "version": 2,
            "complete": True,
            "created_at": "2026-10-28T10:00:00Z",
            "database": records,
            "replica": {"channel": BACKUP, "messages": {"41": 141}},
            "credentials": credentials_from_snapshot(
                recovery_settings(tmp_path), records
            ),
        }
    finally:
        store.close()


def stage(tmp_path, snapshot):
    path = tmp_path / "snapshot.json"
    path.write_text(json_util.dumps(snapshot), encoding="utf-8")
    return staged_snapshot.stage_snapshot(path, tmp_path / "snapshot.sqlite")


def file_node(snapshot):
    return next(row for row in snapshot["database"]["nodes"] if row["kind"] == "file")


def test_round_trip_typed_rows_and_replica_messages(tmp_path, snapshot):
    result = stage(tmp_path, snapshot)
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert result.catalog == snapshot["database"]["catalog"][0]
    assert result.header["replica"] == {"channel": BACKUP}
    assert result.file_count == 1
    assert result.total_bytes == 3
    assert result.mapping("41") == 141
    assert len(result.digest) == 64
    records = {name: list(result.records(name)) for name in COLLECTIONS}
    assert records == snapshot["database"]
    # Catalog metadata is exposed separately with validated envelope integers;
    # raw BSON rows keep Int64 for insertion back into MongoDB.
    records["catalog"] = [result.catalog]
    assert snapshot_catalog(records) == snapshot_catalog(snapshot["database"])
    assert (
        next(row for row in result.nodes(BACKUP) if row["kind"] == "file")["message_id"]
        == 141
    )
    assert list(result.nodes()) == snapshot["database"]["nodes"]
    with pytest.raises(ValueError, match="mapping"):
        list(result.nodes(MAIN))
    with pytest.raises(KeyError):
        result.mapping("42")


@pytest.mark.parametrize(
    "location", ["envelope", "collection", "record", "nested", "mapping"]
)
def test_duplicate_json_fields_reject_and_remove_partial_index(
    tmp_path, snapshot, location
):
    raw = json_util.dumps(snapshot)
    if location == "envelope":
        raw = raw.replace('"version": 2', '"version": 2, "version": 2', 1)
    elif location == "collection":
        raw = raw.replace('"sessions": []', '"sessions": [], "sessions": []', 1)
    elif location == "record":
        raw = raw.replace('"name": "/"', '"name": "/", "name": "/"', 1)
    elif location == "nested":
        raw = raw.replace('"data": {', '"data": {"x": 1, "x": 2,', 1)
    else:
        raw = raw.replace('"41": 141', '"41": 141, "41": 141', 1)
    source, destination = tmp_path / "bad.json", tmp_path / "bad.sqlite"
    source.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        staged_snapshot.stage_snapshot(source, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_collection",
        "unknown_collection",
        "unknown_header",
        "duplicate_id",
        "missing_parent",
        "cycle",
        "sibling",
        "invalid_name",
        "invalid_timestamp",
        "bad_parts",
        "extra_mapping",
        "missing_mapping",
        "duplicate_destination",
        "bad_channel",
        "browser_session",
        "user_session",
        "bootstrap_session",
        "invalid_bson",
        "nonfinite",
        "nonfinite_decimal",
        "operation_folder",
        "missing_share_node",
    ],
)
def test_invalid_snapshot_is_refused_before_rows_are_exposed(
    tmp_path, snapshot, mutation
):
    database = snapshot["database"]
    node = file_node(snapshot)
    if mutation == "missing_collection":
        database.pop("receipts")
    elif mutation == "unknown_collection":
        database["surprise"] = []
    elif mutation == "unknown_header":
        snapshot["surprise"] = True
    elif mutation == "duplicate_id":
        database["jobs"].append(copy.deepcopy(database["jobs"][0]))
    elif mutation == "missing_parent":
        node["parent_id"] = "absent"
    elif mutation == "cycle":
        folder = next(row for row in database["nodes"] if row["name"] == "Folder")
        folder["parent_id"] = folder["_id"]
    elif mutation == "sibling":
        database["nodes"].append(dict(node, _id="another-file"))
    elif mutation == "invalid_name":
        node.update(name="../bad", name_key="../bad")
    elif mutation == "invalid_timestamp":
        node["upload_date"] = "yesterday"
    elif mutation == "bad_parts":
        node["parts"] = [
            {"index": 0, "offset": 1, "message_id": 41, "size": 3, "sha256": "a" * 64}
        ]
    elif mutation == "extra_mapping":
        snapshot["replica"]["messages"]["42"] = 142
    elif mutation == "missing_mapping":
        snapshot["replica"]["messages"].clear()
    elif mutation == "duplicate_destination":
        snapshot["replica"]["messages"]["42"] = 141
    elif mutation == "bad_channel":
        snapshot["replica"]["channel"] = MAIN
    elif mutation == "browser_session":
        database["sessions"] = [{"_id": "browser", "token": "secret"}]
    elif mutation == "user_session":
        database["settings"] = [
            {
                "_id": "telegram",
                "document": {"clients": [{"kind": "user", "credential": "secret"}]},
            }
        ]
    elif mutation == "bootstrap_session":
        database["settings"] = [
            {"_id": "bootstrap", "document": {"STRING_SESSIONS": "secret"}}
        ]
    elif mutation == "invalid_bson":
        database["jobs"][0]["bad"] = {"$numberInt": "broken"}
    elif mutation == "nonfinite":
        database["jobs"][0]["bad"] = {"$numberDouble": "NaN"}
    elif mutation == "nonfinite_decimal":
        database["jobs"][0]["bad"] = {"$numberDecimal": "NaN"}
    elif mutation == "operation_folder":
        database["operations"] = [
            {"_id": "op", "operation_id": "op", "node_id": "root"}
        ]
    elif mutation == "missing_share_node":
        database["shares"] = [
            {
                "_id": "share",
                "id": "share",
                "node_id": "absent",
                "token_hash": "a" * 64,
                "created_at": snapshot["created_at"],
                "expires_at": None,
            }
        ]
    with pytest.raises(ValueError):
        stage(tmp_path, snapshot)
    assert not (tmp_path / "snapshot.sqlite").exists()


def test_records_beyond_old_catalog_ceiling_are_accepted(tmp_path, snapshot):
    snapshot["database"]["jobs"] = [{"_id": f"job-{index}"} for index in range(10_001)]
    result = stage(tmp_path, snapshot)
    assert sum(1 for _ in result.records("jobs")) == 10_001


@pytest.mark.parametrize("change", [None, "messages", "path", "active", "completed"])
def test_deletion_receipt_matches_retained_subtree(tmp_path, snapshot, change):
    database = snapshot["database"]
    folder = next(row for row in database["nodes"] if row["name"] == "Folder")
    folder["trashed"] = True
    intent = {
        "_id": "deletion",
        "id": "deletion",
        "root_id": folder["_id"],
        "path": "/" + folder["_id"],
        "channel_id": MAIN,
        "state": "pending",
        "created_at": snapshot["created_at"],
        "error": None,
        "message_ids": [41],
        "pending_message_ids": [41],
    }
    database["deletions"] = [intent]
    if change == "messages":
        intent.update(message_ids=[42], pending_message_ids=[42])
    elif change == "path":
        intent["path"] = "/wrong"
    elif change == "active":
        folder["trashed"] = False
    elif change == "completed":
        intent.update(state="completed", pending_message_ids=[])
    if change is None:
        result = stage(tmp_path, snapshot)
        assert result.file_count == 1
        assert result.counters == {
            "files": 0,
            "folders": 0,
            "total_bytes": 0,
            "trashed_files": 1,
            "trashed_bytes": 3,
        }
    else:
        with pytest.raises(ValueError):
            stage(tmp_path, snapshot)


def test_null_replica_and_duplicate_numeric_database_ids(tmp_path, snapshot):
    snapshot["replica"] = None
    result = stage(tmp_path, snapshot)
    assert result.header["replica"] is None
    snapshot["database"]["jobs"] = [{"_id": 1}, {"_id": 1.0}]
    (tmp_path / "snapshot.sqlite").unlink()
    with pytest.raises(ValueError):
        stage(tmp_path, snapshot)


@pytest.mark.parametrize("limit", ["token", "record", "nesting"])
def test_single_value_limits_do_not_require_catalog_limits(
    tmp_path, snapshot, monkeypatch, limit
):
    if limit == "token":
        monkeypatch.setattr(staged_snapshot, "MAX_TOKEN_BYTES", 512)
        monkeypatch.setattr(staged_snapshot, "READ_BYTES", 64)
        snapshot["database"]["jobs"][0]["bad"] = "x" * 4096
    elif limit == "record":
        monkeypatch.setattr(staged_snapshot, "MAX_BSON_BYTES", 256)
        snapshot["database"]["jobs"][0]["bad"] = "x" * 4096
    else:
        monkeypatch.setattr(staged_snapshot, "MAX_NESTING", 8)
        snapshot["database"]["jobs"][0]["bad"] = [[[[[[[[[0]]]]]]]]]
    with pytest.raises(ValueError, match="limit"):
        stage(tmp_path, snapshot)


def test_validation_uses_and_removes_a_private_index(tmp_path, snapshot):
    source = tmp_path / "snapshot.json"
    source.write_text(json_util.dumps(snapshot), encoding="utf-8")
    staged_snapshot.validate_snapshot(source)
    assert list(tmp_path.iterdir()) == [source]


def test_existing_destination_is_preserved(tmp_path, snapshot):
    destination = tmp_path / "snapshot.sqlite"
    destination.write_bytes(b"preserve this index")
    with pytest.raises(FileExistsError):
        stage(tmp_path, snapshot)
    assert destination.read_bytes() == b"preserve this index"


def test_decoder_refuses_an_unterminated_token_before_accumulating_it(monkeypatch):
    monkeypatch.setattr(staged_snapshot, "MAX_TOKEN_BYTES", 4096)
    monkeypatch.setattr(staged_snapshot, "READ_BYTES", 128)
    source = io.BytesIO(b'{"large":"' + b"x" * 1024**2)
    parser = staged_snapshot._Parser(source)
    parser.expect("start_map")
    parser.expect("map_key")
    with pytest.raises(ValueError, match="token"):
        parser.value()
    assert source.tell() <= 4096 + 128


def test_document_boundary_accepts_exact_size_and_rejects_one_extra_byte(
    tmp_path, snapshot, monkeypatch
):
    limit = 4096
    row = {"_id": "boundary", "data": ""}
    row["data"] = "x" * (limit - len(BSON.encode(row)))
    snapshot["database"]["jobs"] = [row]
    monkeypatch.setattr(staged_snapshot, "MAX_BSON_BYTES", limit)
    result = stage(tmp_path, snapshot)
    assert next(result.records("jobs")) == row
    result.path.unlink()
    row["data"] += "x"
    with pytest.raises(ValueError, match="limit"):
        stage(tmp_path, snapshot)


def test_json_record_progress_is_bounded_between_small_tokens(
    tmp_path, snapshot, monkeypatch
):
    monkeypatch.setattr(staged_snapshot, "MAX_VALUE_BYTES", 1024)
    monkeypatch.setattr(staged_snapshot, "READ_BYTES", 64)
    snapshot["database"]["jobs"][0]["values"] = ["x"] * 1000
    with pytest.raises(ValueError, match="record exceeds"):
        stage(tmp_path, snapshot)


def test_flat_catalog_is_streamed_without_a_total_record_ceiling(tmp_path, snapshot):
    source = tmp_path / "large.json"
    template = file_node(snapshot)
    root = next(row for row in snapshot["database"]["nodes"] if row["_id"] == "root")
    snapshot["replica"] = None
    with source.open("w", encoding="utf-8") as target:
        target.write("{")
        for name, value in snapshot.items():
            if name != "database":
                target.write(json.dumps(name) + ":" + json_util.dumps(value) + ",")
        target.write('"database":{')
        for collection_index, name in enumerate(COLLECTIONS):
            if collection_index:
                target.write(",")
            target.write(json.dumps(name) + ":[")
            rows = snapshot["database"][name]
            if name == "nodes":
                rows = chain(
                    [root],
                    (
                        dict(
                            template,
                            _id=f"file-{index}",
                            parent_id="root",
                            name=f"{index}.txt",
                            name_key=f"{index}.txt",
                        )
                        for index in range(25_000)
                    ),
                )
            for index, row in enumerate(rows):
                if index:
                    target.write(",")
                target.write(json_util.dumps(row))
            target.write("]")
        target.write("}}")
    result = staged_snapshot.stage_snapshot(source, tmp_path / "large.sqlite")
    assert result.file_count == 25_000
    assert result.total_bytes == 75_000
    assert result.counters == {
        "files": 25_000,
        "folders": 0,
        "total_bytes": 75_000,
        "trashed_files": 0,
        "trashed_bytes": 0,
    }
    assert sum(1 for _ in result.nodes()) == 25_001


@pytest.mark.parametrize("late", [False, True])
def test_disk_reserve_is_checked_before_and_during_staging(
    tmp_path, snapshot, monkeypatch, late
):
    calls = 0

    def usage(_directory):
        nonlocal calls
        calls += 1
        return SimpleNamespace(free=1024**2 if late and calls < 4 else 128 * 1024)

    snapshot["database"]["jobs"] = [{"_id": f"job-{index}"} for index in range(500)]
    source = tmp_path / "snapshot.json"
    source.write_text(json_util.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(staged_snapshot.shutil, "disk_usage", usage)
    with pytest.raises(OSError, match="temporary space"):
        staged_snapshot.stage_snapshot(
            source, tmp_path / "index.sqlite", min_free_bytes=128 * 1024
        )
    assert not (tmp_path / "index.sqlite").exists()
    assert calls == (4 if late else 1)


def test_legacy_schema_retains_the_version_matched_recovery_guidance(
    tmp_path, snapshot
):
    snapshot["database"]["catalog"][0]["schema_version"] = 4
    with pytest.raises(LegacyRecoveryFile, match=r"tagged v2\.2"):
        stage(tmp_path, snapshot)


def test_preparation_receipts_validate_their_own_historical_ancestry(
    tmp_path, snapshot
):
    from tgdrive.storage.store import _folder_manifest, _manifest_json

    manifest = _folder_manifest("/", "Prepared", [["Child"]])
    record = {
        "_id": "request-1234567890",
        "request_id": "request-1234567890",
        "fingerprint": hashlib.sha256(_manifest_json(manifest).encode()).hexdigest(),
        "manifest": manifest,
        "created_at": snapshot["created_at"],
        "result": {
            "root": {
                "id": "prepared",
                "name": "Prepared",
                "path": "/",
                "size": 0,
                "type": "folder",
                "upload_date": snapshot["created_at"],
            },
            "folders": {"": "/prepared", "Child": "/prepared/child"},
        },
    }
    snapshot["database"]["preparations"] = [record]
    result = stage(tmp_path, snapshot)
    assert next(result.records("preparations")) == record
    result.path.unlink()
    record["result"]["folders"]["Child"] = "/unrelated/child"
    with pytest.raises(ValueError):
        stage(tmp_path, snapshot)


def test_stateless_node_cursor_can_advance_on_different_workers(tmp_path, snapshot):
    result = stage(tmp_path, snapshot)
    rows, sequence = [], 0
    with (
        ThreadPoolExecutor(max_workers=1) as first,
        ThreadPoolExecutor(max_workers=1) as second,
    ):
        while found := first.submit(result.node_after, sequence, BACKUP).result():
            sequence, row = found
            rows.append(row)
            if found := second.submit(result.node_after, sequence, BACKUP).result():
                sequence, row = found
                rows.append(row)
        assert rows == list(result.nodes(BACKUP))


@pytest.mark.parametrize(
    "value",
    [
        {"$numberInt": "2147483648"},
        {"$numberLong": True},
        {"$numberLong": "9223372036854775808"},
        {"$numberLong": " 1 "},
        {"$binary": {"base64": "!!!!", "subType": "00"}},
        {"$binary": {"base64": "YQ==", "subType": "00"}, "ignored": True},
        {"$binary": "!!!!", "$type": "00"},
        {"$date": True},
        {"$date": 1.5},
        {"$timestamp": {"t": True, "i": 1}},
        {"$undefined": False},
        {"$undefined": True, "ignored": True},
        {"$regex": "x", "$options": "z"},
        {"$regularExpression": {"pattern": "x", "options": "z"}},
        {"$numberDecimal": "not-a-number"},
    ],
)
def test_malformed_extended_bson_is_not_silently_coerced(tmp_path, snapshot, value):
    snapshot["database"]["jobs"][0]["bad"] = value
    with pytest.raises(ValueError):
        stage(tmp_path, snapshot)


@pytest.mark.parametrize("failure", [False, True])
def test_node_iterator_closes_nested_records_before_returning_to_another_thread(
    tmp_path, snapshot, monkeypatch, failure
):
    result = stage(tmp_path, snapshot)
    records = []
    original = staged_snapshot.StagedSnapshot.records
    remap = staged_snapshot.StagedSnapshot._remap_node

    def tracked(snapshot, collection):
        iterator = original(snapshot, collection)
        records.append(iterator)
        return iterator

    def failed_remap(db, row, channel):
        if row["_id"] != "root":
            raise ValueError("Synthetic remapping failure")
        return remap(db, row, channel)

    monkeypatch.setattr(staged_snapshot.StagedSnapshot, "records", tracked)
    if failure:
        monkeypatch.setattr(
            staged_snapshot.StagedSnapshot, "_remap_node", staticmethod(failed_remap)
        )

    def consume():
        iterator = result.nodes(BACKUP)
        try:
            next(iterator)
            if failure:
                with pytest.raises(ValueError, match="remapping failure"):
                    next(iterator)
            else:
                iterator.close()
            assert records and all(record.gi_frame is None for record in records)
        finally:
            iterator.close()
            for record in records:
                record.close()

    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(consume).result()


@pytest.mark.parametrize(
    "value,items",
    [
        ([None] * 8, 8),
        ({"a": [{}, [None, None]], "b": {"c": None}}, 7),
    ],
)
def test_record_item_budget_combines_nested_fields_and_array_items(
    monkeypatch, value, items
):
    raw = json.dumps(value).encode()
    monkeypatch.setattr(staged_snapshot, "MAX_VALUE_ITEMS", items)
    assert staged_snapshot._Parser(io.BytesIO(raw)).value() == value
    monkeypatch.setattr(staged_snapshot, "MAX_VALUE_ITEMS", items - 1)
    with pytest.raises(ValueError, match="item limit"):
        staged_snapshot._Parser(io.BytesIO(raw)).value()


def test_record_item_budget_resets_between_independent_values(monkeypatch):
    monkeypatch.setattr(staged_snapshot, "MAX_VALUE_ITEMS", 2)
    parser = staged_snapshot._Parser(io.BytesIO(b"[[null,null],[null,null]]"))
    parser.expect("start_array")
    assert parser.value() == [None, None]
    assert parser.value() == [None, None]
    parser.expect("end_array")


def test_structural_amplification_is_refused_before_bson_encoding(
    tmp_path, snapshot, monkeypatch
):
    snapshot["database"]["jobs"] = [
        {
            "_id": "amplified",
            "values": [None] * (staged_snapshot.MAX_VALUE_ITEMS + 1),
        }
    ]
    original = staged_snapshot._pack

    def pack(value):
        if value.get("_id") == "amplified":
            pytest.fail("An excessive record reached BSON encoding")
        return original(value)

    monkeypatch.setattr(staged_snapshot, "_pack", pack)
    with pytest.raises(ValueError, match="item limit"):
        stage(tmp_path, snapshot)
    assert not (tmp_path / "snapshot.sqlite").exists()


def test_valid_large_binary_fits_the_json_token_budget(tmp_path, snapshot):
    payload = Binary(b"x" * (13 * 1024**2), subtype=128)
    snapshot["database"]["jobs"] = [{"_id": "large-binary", "data": payload}]
    result = stage(tmp_path, snapshot)
    with closing(result.records("jobs")) as rows:
        row = next(rows)
        assert row["_id"] == "large-binary"
        assert row["data"] == payload


def test_parser_resource_errors_are_distinct_from_duplicate_fields(monkeypatch):
    monkeypatch.setattr(staged_snapshot, "MAX_VALUE_ITEMS", 3)
    with pytest.raises(staged_snapshot.RecoveryResourceLimit):
        staged_snapshot._Parser(io.BytesIO(b'{"x":[null,null,null]}')).value()
    with pytest.raises(ValueError, match="Duplicate") as invalid:
        staged_snapshot._Parser(io.BytesIO(b'{"x":1,"x":2}')).value()
    assert not isinstance(invalid.value, staged_snapshot.RecoveryResourceLimit)
