from __future__ import annotations

import json
import os
import pickle
import sys
import types
from contextlib import contextmanager

import pytest

from tgdrive.errors import LegacyImportError
from tgdrive.legacy import convert_legacy, restricted_load
from tgdrive.store import SCHEMA_VERSION, DriveStore


@contextmanager
def _historical_classes():
    module_name = "utils.directoryHandler"
    previous = sys.modules.get(module_name)
    previous_parent = sys.modules.get("utils")
    parent = types.ModuleType("utils")
    parent.__path__ = []
    module = types.ModuleType(module_name)
    parent.directoryHandler = module
    classes = {
        name: type(name, (), {"__module__": module_name})
        for name in ("NewDriveData", "Folder", "File")
    }
    for name, value in classes.items():
        setattr(module, name, value)
    sys.modules["utils"] = parent
    sys.modules[module_name] = module
    try:
        yield classes
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        if previous_parent is None:
            sys.modules.pop("utils", None)
        else:
            sys.modules["utils"] = previous_parent


def _valid_legacy_pickle() -> bytes:
    with _historical_classes() as old:
        root = old["Folder"]()
        root.name = "/"
        root.id = "root"
        root.type = "folder"
        root.trash = False
        root.path = "/"
        root.upload_date = "2024-01-02 03:04:05"
        root.auth_hashes = ["OLD-PLAINTEXT-ROOT-TOKEN"]
        root.contents = {}

        folder = old["Folder"]()
        folder.name = "Migrated"
        folder.id = "ABC123"
        folder.type = "folder"
        folder.trash = False
        folder.path = "/"
        folder.upload_date = "2024-01-03 03:04:05"
        folder.auth_hashes = ["OLD-PLAINTEXT-FOLDER-TOKEN"]
        folder.contents = {}

        uploaded = old["File"]()
        uploaded.name = "legacy.txt"
        uploaded.file_id = 765
        uploaded.id = "XYZ789"
        uploaded.size = 99
        uploaded.type = "file"
        uploaded.trash = False
        uploaded.path = "/ABC123"
        uploaded.upload_date = "2024-01-04 03:04:05"
        folder.contents[uploaded.id] = uploaded
        root.contents[folder.id] = folder

        drive = old["NewDriveData"]()
        drive.contents = {"/": root}
        drive.used_ids = [folder.id, uploaded.id]
        drive.isUpdated = False
        return pickle.dumps(drive, protocol=pickle.HIGHEST_PROTOCOL)


def test_valid_legacy_fixture_converts_to_v2_and_persists(tmp_path) -> None:
    destination = tmp_path / "drive.sqlite3"
    store = convert_legacy(_valid_legacy_pickle(), destination)

    assert store.revision == 1
    assert store.dirty is True
    assert store.get_directory("/ABC123")["name"] == "Migrated"
    assert store.get_file("/ABC123/XYZ789")["message_id"] == 765
    assert store.get_file("/ABC123/XYZ789")["size"] == 99
    assert DriveStore.load(destination).export() == store.export()

    raw_json = json.dumps(store.export())
    assert json.loads(raw_json)["schema_version"] == SCHEMA_VERSION
    assert "OLD-PLAINTEXT" not in raw_json


def test_restricted_unpickler_rejects_arbitrary_reduce_without_execution(
    tmp_path,
) -> None:
    marker = tmp_path / "must-not-exist"

    class Evil:
        def __reduce__(self):
            return os.system, (f'echo compromised > "{marker}"',)

    payload = pickle.dumps(Evil(), protocol=pickle.HIGHEST_PROTOCOL)
    with pytest.raises(LegacyImportError, match="forbidden pickle global"):
        restricted_load(payload)
    assert not marker.exists()


def test_restricted_unpickler_rejects_non_drive_and_trailing_payloads() -> None:
    with pytest.raises(LegacyImportError, match="forbidden pickle global"):
        restricted_load(pickle.dumps(ValueError("not metadata")))

    with pytest.raises(LegacyImportError, match="trailing data"):
        restricted_load(_valid_legacy_pickle() + b"unexpected")


def test_invalid_legacy_tree_fails_without_creating_destination(tmp_path) -> None:
    with _historical_classes() as old:
        root = old["Folder"]()
        root.name = "/"
        root.id = "root"
        root.type = "folder"
        root.trash = False
        root.upload_date = "2024-01-02 03:04:05"
        root.contents = {}

        first = old["Folder"]()
        first.name = "Duplicate"
        first.id = "ONE111"
        first.type = "folder"
        first.trash = False
        first.upload_date = "2024-01-02 03:04:05"
        first.contents = {}

        second = old["Folder"]()
        second.name = "duplicate"
        second.id = "TWO222"
        second.type = "folder"
        second.trash = False
        second.upload_date = "2024-01-02 03:04:05"
        second.contents = {}

        root.contents = {first.id: first, second.id: second}
        drive = old["NewDriveData"]()
        drive.contents = {"/": root}
        payload = pickle.dumps(drive, protocol=pickle.HIGHEST_PROTOCOL)

    destination = tmp_path / "drive.sqlite3"
    with pytest.raises(LegacyImportError, match="duplicate"):
        convert_legacy(payload, destination)
    assert not destination.exists()
