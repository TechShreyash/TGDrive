"""Catalog totals reuse only the same immutable committed snapshot."""

import pytest

from tests.support.mongo_fixtures import FakeMongoBackend
from tgdrive.storage.mongo_store import MongoStore, catalog_counters


def test_statistics_are_counters_kept_in_the_write_transaction():
    store = MongoStore.create(FakeMongoBackend())
    try:
        folder = store.create_folder("/", "Folder")
        path = "/" + folder["id"]
        nested = store.create_folder(path, "Nested")
        store.add_file(path, "report.txt", 101, 7)
        store.add_file(path + "/" + nested["id"], "deep.txt", 102, 5)
        expected = {
            "files": 2,
            "folders": 2,
            "total_bytes": 12,
            "trashed_files": 0,
            "trashed_bytes": 0,
        }
        assert store.statistics() == expected
        store.set_trashed(path, True)
        trashed = dict(
            expected,
            files=0,
            folders=0,
            total_bytes=0,
            trashed_files=2,
            trashed_bytes=12,
        )
        assert store.statistics() == trashed
        assert store.statistics() == catalog_counters(store.export())
        store.set_trashed(path, False)
        assert store.statistics() == expected
        store.add_file(path, "second.txt", 103, 3)
        assert store.statistics() == dict(expected, files=3, total_bytes=15)
        store.set_trashed(path, True)
        intent = store.begin_deletion(path)
        store.finish_deletion(intent["id"])
        assert store.statistics() == dict.fromkeys(expected, 0)
    finally:
        store.close()


def test_statistics_do_not_cache_mutable_or_rolled_back_batches():
    store = MongoStore.create(FakeMongoBackend())
    try:
        before = store.statistics()
        with pytest.raises(ValueError, match="Roll back"), store.metadata_batch():
            store.create_folder("/", "First")
            assert store.statistics()["folders"] == 1
            store.create_folder("/", "Second")
            assert store.statistics()["folders"] == 2
            raise ValueError("Roll back")
        assert store.statistics() == before
        store.create_folder("/", "Committed")
        assert store.statistics()["folders"] == 1
    finally:
        store.close()
