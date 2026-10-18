"""Atomic metadata batches roll back names, ancestry, shares and revisions together."""

import pytest

from tgdrive.errors import (
    NotFoundError,
    PersistenceError,
    RootMutationError,
    TrashedItemError,
)
from tgdrive.store import DriveStore


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
