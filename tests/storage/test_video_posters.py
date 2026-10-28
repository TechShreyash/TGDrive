"""Poster sidecars survive catalog recovery and share original deletion ownership."""

from __future__ import annotations

import copy
import sqlite3
from contextlib import closing

import pytest

from tests.support.mongo_fixtures import FakeMongoBackend
from tgdrive.core.errors import CorruptStoreError, ValidationError
from tgdrive.replication.replica_activation import _mongo_nodes
from tgdrive.replication.replica_state import files_in, parts_in, remap_catalog
from tgdrive.storage.file_parts import MAX_POSTER_BYTES
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import DriveStore

POSTER = {"message_id": 201, "size": 1234, "sha256": "ab" * 32, "document_id": -9}
PARTS = [
    {"index": 0, "offset": 0, "size": 4, "message_id": 101, "sha256": "cd" * 32},
    {"index": 1, "offset": 4, "size": 6, "message_id": 102, "sha256": "ef" * 32},
]


@pytest.fixture(params=["sqlite", "mongodb"])
def store(request):
    result = (
        DriveStore.create()
        if request.param == "sqlite"
        else MongoStore.create(FakeMongoBackend())
    )
    yield result
    result.close()


def add(store, poster=POSTER):
    return store.add_file(
        "/", "video.mkv", 101, 10, parts=PARTS, poster=poster, operation_id="video"
    )


def full(item):
    return item["path"].rstrip("/") + "/" + item["id"]


def test_poster_roundtrips_without_changing_original_parts_or_public_listing(store):
    source = copy.deepcopy(POSTER)
    source["sha256"] = source["sha256"].upper()
    item = add(store, source)
    source["message_id"] = 999
    assert store.get_file(full(item))["poster"] == POSTER
    assert store.get_file(full(item))["parts"] == PARTS
    assert "poster" not in next(iter(store.list_directory("/").values()))
    assert add(store)["id"] == item["id"]
    with pytest.raises(ValidationError):
        add(store, {**POSTER, "message_id": 202})
    with closing(DriveStore._from_payload(store.export())) as recovered:
        assert recovered.get_file(full(item))["poster"] == POSTER
        assert recovered.message_ids(full(item)) == [101, 102, 201]
    if isinstance(store, MongoStore):
        reopened = MongoStore(store.backend)
        try:
            assert reopened.get_file(full(item))["poster"] == POSTER
        finally:
            reopened.close()


@pytest.mark.parametrize(
    "poster",
    [
        {},
        {**POSTER, "extra": True},
        {**POSTER, "message_id": True},
        {**POSTER, "message_id": 0},
        {**POSTER, "message_id": 1 << 63},
        {**POSTER, "message_id": 101},
        {**POSTER, "message_id": 102},
        {**POSTER, "size": True},
        {**POSTER, "size": 0},
        {**POSTER, "size": MAX_POSTER_BYTES + 1},
        {**POSTER, "sha256": None},
        {**POSTER, "sha256": "bad"},
        {**POSTER, "document_id": True},
        {**POSTER, "document_id": 0},
        {**POSTER, "document_id": 1 << 63},
    ],
)
def test_invalid_poster_never_commits_file_metadata(store, poster):
    before = store.export()
    with pytest.raises(ValidationError):
        add(store, poster)
    assert store.export() == before


def test_pending_purge_and_recovery_include_the_poster(store):
    item = add(store)
    store.set_trashed(full(item), True)
    intent = store.begin_deletion(full(item))
    assert intent["message_ids"] == [101, 102, 201]
    store.confirm_deletion_messages(intent["id"], [101, 102])
    with closing(DriveStore._from_payload(store.export())) as recovered:
        assert recovered.pending_deletions()[0]["pending_message_ids"] == [201]
    damaged = store.export()
    damaged["deletions"][0]["message_ids"] = [101, 102]
    damaged["deletions"][0]["pending_message_ids"] = []
    with pytest.raises(CorruptStoreError):
        DriveStore._from_payload(damaged)
    assert store.finish_deletion(intent["id"]) == [101, 102, 201]


def test_replication_maps_poster_as_a_sidecar_without_changing_file_layout(store):
    store.bind_storage_channel(-1001234567890)
    add(store)
    catalog = store.export()
    node = next(files_in(catalog))
    assert [part["message_id"] for part in parts_in(node)] == [101, 102, 201]
    with pytest.raises(KeyError):
        remap_catalog(catalog, -1002234567890, {"101": 301, "102": 302})
    copied = remap_catalog(
        catalog, -1002234567890, {"101": 301, "102": 302, "201": 401}
    )
    result = next(files_in(copied))
    assert result["poster"] == {**POSTER, "message_id": 401}
    assert result["size"] == 10
    assert [part["offset"] for part in result["parts"]] == [0, 4]
    assert node["poster"] == POSTER


def test_v6_sqlite_upgrade_preserves_existing_catalog_and_allows_posters(tmp_path):
    database = tmp_path / "drive.sqlite3"
    with closing(DriveStore.create(database)) as original:
        original.add_file("/", "older.bin", 1, 10)
        before = original.export()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE nodes DROP COLUMN poster")
        connection.execute("PRAGMA user_version=6")
        connection.commit()
    with closing(DriveStore.load(database)) as upgraded:
        assert upgraded.export() == before
        item = add(upgraded)
        assert upgraded.get_file(full(item))["poster"] == POSTER
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7


def test_channel_activation_rebuilds_poster_links_in_valid_mongo_records(store):
    store.bind_storage_channel(-1001234567890)
    add(store)
    catalog = remap_catalog(
        store.export(), -1002234567890, {"101": 301, "102": 302, "201": 401}
    )
    metadata = {
        "_id": "catalog",
        "generation": 1,
        **{
            key: catalog[key]
            for key in (
                "schema_version",
                "drive_id",
                "revision",
                "backed_up_revision",
                "storage_channel_id",
            )
        },
    }
    rebuilt = MongoStore.validate_snapshot_records(
        {
            "catalog": [metadata],
            "nodes": _mongo_nodes(catalog),
            "shares": [],
            "operations": [],
            "deletions": [],
            "preparations": [],
        }
    )
    assert rebuilt == catalog
    assert next(files_in(rebuilt))["poster"]["message_id"] == 401
