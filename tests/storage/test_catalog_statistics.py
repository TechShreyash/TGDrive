"""Catalog totals reuse only the same immutable committed snapshot."""

import pytest

from tests.support.mongo_fixtures import FakeMongoBackend
from tgdrive.storage.mongo_store import MongoStore


def test_statistics_reuse_snapshot_and_invalidate_after_catalog_changes(monkeypatch):
    store = MongoStore.create(FakeMongoBackend())
    try:
        folder = store.create_folder("/", "Folder")
        path = "/" + folder["id"]
        store.add_file(path, "report.txt", 101, 7)
        resolve_path = store._path
        visits = []

        def count(identity):
            visits.append(identity)
            return resolve_path(identity)

        monkeypatch.setattr(store, "_path", count)
        expected = {
            "files": 1,
            "folders": 1,
            "total_bytes": 7,
            "trashed_files": 0,
            "trashed_bytes": 0,
        }
        assert store.statistics() == expected
        first_visits = len(visits)
        assert first_visits > 0
        changed = store.statistics()
        changed["files"] = 999
        assert store.statistics() == expected
        assert len(visits) == first_visits

        store.set_trashed(path, True)
        assert store.statistics() == dict(
            expected,
            files=0,
            folders=0,
            total_bytes=0,
            trashed_files=1,
            trashed_bytes=7,
        )
        store.set_trashed(path, False)
        assert store.statistics() == expected
        store.add_file(path, "second.txt", 102, 3)
        assert store.statistics() == dict(expected, files=2, total_bytes=10)
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


def test_reloaded_snapshot_invalidates_statistics_even_at_the_same_revision():
    backend = FakeMongoBackend()
    store = MongoStore.create(backend)
    try:
        item = store.add_file("/", "report.txt", 101, 7)
        assert store.statistics()["total_bytes"] == 7
        revision = store.revision
        backend.db.nodes.update_one({"_id": item["id"]}, {"$set": {"size": 12}})
        store._reload()
        assert store.revision == revision
        assert store.statistics()["total_bytes"] == 12
    finally:
        store.close()
