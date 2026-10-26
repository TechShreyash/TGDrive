"""Portable catalog identities and operation tombstones survive recovery."""

import pytest

from tgdrive.core.errors import ValidationError
from tgdrive.storage.store import DriveStore


def test_drive_identity_and_file_operation_tombstones_roundtrip(tmp_path):
    database = tmp_path / "drive.sqlite3"
    store = DriveStore.create(database)
    identity = store.drive_id
    file = store.add_file("/", "finished.bin", 7, 8, operation_id="finished-operation")
    assert store.has_file_operation("finished-operation")
    assert not store.has_file_operation("unknown-operation")
    path = "/" + file["id"]
    store.set_trashed(path, True)
    intent = store.begin_deletion(path, channel_id=-1001)
    store.confirm_deletion_messages(intent["id"], [7])
    store.finish_deletion(intent["id"])
    assert store.has_file_operation("finished-operation")
    with pytest.raises(ValidationError):
        store.has_file_operation("../unsafe")
    restored = DriveStore._from_payload(store.export())
    assert restored.drive_id == identity
    assert restored.has_file_operation("finished-operation")
    restored.close()
    store.close()
    loaded = DriveStore.load(database)
    assert loaded.drive_id == identity
    loaded.close()
