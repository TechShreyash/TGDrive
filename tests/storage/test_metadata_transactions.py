"""Atomic metadata batches roll back names, ancestry, shares and revisions together."""

import copy

import pytest

from tests.support.mongo_fixtures import FakeMongoBackend
from tgdrive.core.errors import (
    NotFoundError,
    PersistenceError,
    RootMutationError,
    TrashedItemError,
)
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import DriveStore


def test_later_mutation_failure_restores_every_prior_change_and_revision():
    store = DriveStore.create()
    try:
        first = store.create_folder("/", "First")
        second = store.create_folder("/", "Second")
        first_path, second_path = "/" + first["id"], "/" + second["id"]
        token = store.create_share(first_path)
        before = store.export()
        with pytest.raises(NotFoundError), store.metadata_batch():
            store.move(second_path, first_path)
            store.set_trashed(first_path, True)
            store.set_trashed("/missing", True)
        assert store.export() == before
        assert store.authorize_directory(first_path, token)
    finally:
        store.close()


def test_caught_inner_error_rolls_back_savepoint_without_corrupting_outer_batch():
    store = DriveStore.create()
    try:
        original = store.create_folder("/", "Original")
        path = "/" + original["id"]
        before = store.revision
        with store.metadata_batch():
            store.rename(path, "Committed")
            with pytest.raises(NotFoundError), store.metadata_batch():
                store.set_trashed(path, True)
                store.get_directory("/missing")
            assert store.get_directory(path)["name"] == "Committed"
        assert store.get_directory(path)["name"] == "Committed"
        assert store.revision == before + 1
    finally:
        store.close()


def test_sqlite_abort_rolls_back_entire_batch_and_connection_remains_usable():
    store = DriveStore.create()
    try:
        node = store.create_folder("/", "Original")
        path = "/" + node["id"]
        before = store.export()
        with pytest.raises(PersistenceError), store.metadata_batch():
            store.rename(path, "Changed")
            with store._transaction(write=True) as db:
                db.execute("INSERT INTO nodes(id) VALUES(?)", ("root",))
        assert store.export() == before
        assert store.rename(path, "After rollback")["name"] == "After rollback"
    finally:
        store.close()


def test_outer_read_cannot_escalate_to_nested_write():
    store = DriveStore.create()
    try:
        before = store.export()
        with (
            pytest.raises(PersistenceError, match="cannot become writable"),
            store._transaction(),
        ):
            store.create_folder("/", "Forbidden")
        assert store.export() == before
    finally:
        store.close()


def test_deletion_preflight_never_mutates_and_accepts_pending_same_root_retry():
    store = DriveStore.create()
    try:
        node = store.create_folder("/", "Target")
        path = "/" + node["id"]
        with pytest.raises(RootMutationError):
            store.validate_deletion("/")
        with pytest.raises(TrashedItemError):
            store.validate_deletion(path)
        store.set_trashed(path, True)
        before = store.export()
        assert store.validate_deletion(path)["id"] == node["id"]
        assert store.export() == before
        store.begin_deletion(path)
        before = store.export()
        assert store.validate_deletion(path)["id"] == node["id"]
        assert store.export() == before
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
def test_pending_deletions_hide_from_trash_across_restart_and_reappear_on_failure(
    backend, tmp_path
):
    database = tmp_path / "deletion.sqlite3"
    mongo = FakeMongoBackend()
    store = (
        DriveStore.create(database) if backend == "sqlite" else MongoStore.create(mongo)
    )
    try:
        node = store.create_folder("/", "Removal")
        path = "/" + node["id"]
        child = store.create_folder(path, "Child")
        store.set_trashed(path + "/" + child["id"], True)
        other = store.create_folder("/", "Other")
        store.set_trashed("/" + other["id"], True)
        store.set_trashed(path, True)
        assert set(store.list_trash()) == {node["id"], other["id"]}

        intent = store.begin_deletion(path)
        assert set(store.list_trash()) == {other["id"]}
        assert store.list_trash("removal") == {}
        assert store.get_directory(path, include_trashed=True)["name"] == "Removal"
        store.close()
        store = DriveStore.load(database) if backend == "sqlite" else MongoStore(mongo)
        assert set(store.list_trash()) == {other["id"]}

        store.fail_deletion(intent["id"], "Backup unavailable")
        assert set(store.list_trash("removal")) == {node["id"]}
        store.confirm_deletion_messages(intent["id"], [])
        assert set(store.list_trash()) == {other["id"]}
        store.finish_deletion(intent["id"])
        assert set(store.list_trash()) == {other["id"]}
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
@pytest.mark.parametrize("action", ["move", "trash", "restore"])
def test_batch_failure_rolls_back_earlier_items_and_share_access(backend, action):
    store = (
        DriveStore.create()
        if backend == "sqlite"
        else MongoStore.create(FakeMongoBackend())
    )
    try:
        first = store.create_folder("/", "First")
        destination = store.create_folder("/", "Destination")
        first_path = "/" + first["id"]
        token = store.create_share(first_path)
        if action == "restore":
            store.set_trashed(first_path, True)
        before = store.export()
        with pytest.raises(NotFoundError):
            if action == "move":
                store.move_many([first_path, "/missing"], "/" + destination["id"])
            elif action == "trash":
                store.set_trashed_many([first_path, "/missing"], True)
            else:
                store.set_trashed_many([first_path, "/missing"], False)
        assert store.export() == before
        if action != "restore":
            assert store.authorize_directory(first_path, token)
        assert store.create_folder("/", "Still usable")["name"] == "Still usable"
    finally:
        store.close()


@pytest.mark.parametrize("count", [2, 24])
@pytest.mark.parametrize("action", ["move", "trash", "restore"])
def test_mongo_bulk_catalog_copies_do_not_grow_with_selection(
    monkeypatch, count, action
):
    store = MongoStore.create(FakeMongoBackend())
    try:
        paths = [
            "/" + store.create_folder("/", f"Item {index}")["id"]
            for index in range(count)
        ]
        destination = "/" + store.create_folder("/", "Destination")["id"]
        if action == "restore":
            store.set_trashed_many(paths, True)
        before_revision = store.revision
        deepcopy = copy.deepcopy
        catalog_copies = []

        def track(value, memo=None):
            if (
                isinstance(value, dict)
                and {"metadata", "nodes", "shares"} <= value.keys()
            ):
                catalog_copies.append(value)
            return deepcopy(value, memo)

        monkeypatch.setattr("tgdrive.storage.mongo_store.copy.deepcopy", track)
        with store.metadata_batch():
            if action == "move":
                results = store.move_many(paths, destination)
            else:
                results = store.set_trashed_many(paths, action == "trash")
        assert len(catalog_copies) <= 2
        assert len(results) == count
        assert store.revision == before_revision + count
        assert {row["id"] for row in results} == {path[1:] for path in paths}
        if action == "move":
            assert set(store.list_directory(destination)) == {
                path[1:] for path in paths
            }
        else:
            actual = (
                store.list_trash() if action == "trash" else store.list_directory("/")
            )
            assert {path[1:] for path in paths} <= actual.keys()
    finally:
        store.close()
