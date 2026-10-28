"""Operation receipts identify stored media without losing deletion tombstones."""

import pytest

from tests.support.mongo_fixtures import FakeMongoBackend
from tgdrive.core.errors import ValidationError
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import DriveStore


@pytest.fixture(params=["sqlite", "mongo"])
def store(request):
    instance = (
        DriveStore.create()
        if request.param == "sqlite"
        else MongoStore.create(FakeMongoBackend())
    )
    try:
        yield instance
    finally:
        instance.close()


def test_operation_media_matching_survives_move_and_trash_but_not_deletion(store):
    item = store.add_file("/", "saved.txt", 7, 8, operation_id="saved-operation")
    folder = store.create_folder("/", "Moved")
    path = "/" + item["id"]
    folder_path = "/" + folder["id"]
    store.move(path, folder_path)
    path = folder_path + "/" + item["id"]
    store.set_trashed(path, True)

    assert store.has_file_operation("saved-operation")
    assert store.has_file_operation("saved-operation", message_id=7)
    assert not store.has_file_operation("saved-operation", message_id=9)
    assert not store.has_file_operation("unknown-operation", message_id=7)

    intent = store.begin_deletion(path, channel_id=-1001)
    store.finish_deletion(intent["id"])
    assert store.has_file_operation("saved-operation")
    assert not store.has_file_operation("saved-operation", message_id=7)


@pytest.mark.parametrize("message_id", [True, 0, -1, 1.5, "7", 2**63])
def test_operation_media_matching_rejects_invalid_message_ids(store, message_id):
    with pytest.raises(ValidationError):
        store.has_file_operation("saved-operation", message_id=message_id)


def test_closed_mongo_store_cannot_confirm_operation_absence():
    store = MongoStore.create(FakeMongoBackend())
    store.close()
    with pytest.raises(DatabaseUnavailable):
        store.has_file_operation("unknown-operation")
