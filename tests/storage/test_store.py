from __future__ import annotations

import gc
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

import tgdrive.storage.store as store_module
from tgdrive.core.errors import (
    CorruptStoreError,
    DuplicateNameError,
    InvalidNameError,
    InvalidPathError,
    InvalidRevisionError,
    NotFoundError,
    PersistenceError,
    RootMutationError,
    TrashedItemError,
    UnsupportedSchemaError,
    ValidationError,
)
from tgdrive.storage.store import SCHEMA_VERSION, DriveStore


def _full_path(record: dict[str, object]) -> str:
    parent = record["path"]
    item_id = record["id"]
    return f"/{item_id}" if parent == "/" else f"{parent}/{item_id}"


def test_create_persist_load_and_frontend_shape(tmp_path) -> None:
    database = tmp_path / "drive.sqlite3"
    store = DriveStore.create(database)

    assert store.revision == 1
    assert store.dirty is True
    folder = store.create_folder("/", "Résumé")
    folder_path = _full_path(folder)
    uploaded = store.add_file(folder_path, "notes+100%.txt", 41, 123)
    file_path = _full_path(uploaded)

    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", str(folder["id"]))
    assert set(folder) == {"id", "name", "path", "size", "type", "upload_date"}
    assert folder["path"] == "/"
    assert folder["size"] == 0
    assert uploaded["path"] == folder_path
    assert uploaded["name"] == "notes+100%.txt"
    assert uploaded["message_id"] == 41

    loaded = DriveStore.load(database)
    assert loaded.export() == store.export()
    assert loaded.get_file(file_path)["message_id"] == 41
    directory = loaded.get_directory(folder_path)
    assert directory["contents"][uploaded["id"]]["name"] == "notes+100%.txt"
    assert database.read_bytes().startswith(b"SQLite format 3\x00")
    assert store.export()["schema_version"] == SCHEMA_VERSION


def test_corrupt_and_unsupported_metadata_fail_closed(tmp_path) -> None:
    database = tmp_path / "drive.sqlite3"
    database.write_bytes(b"{definitely not json")
    with pytest.raises(CorruptStoreError):
        DriveStore.load(database)

    unsupported = DriveStore.create()
    payload = unsupported.export()
    unsupported.close()
    payload["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(UnsupportedSchemaError):
        DriveStore._from_payload(payload)

    valid = DriveStore.create().export()
    valid["root"]["children"] = {
        "first": {
            "children": {},
            "id": "first",
            "name": "Same",
            "share_hashes": [],
            "trashed": False,
            "type": "folder",
            "upload_date": valid["root"]["upload_date"],
        },
        "second": {
            "children": {},
            "id": "second",
            "name": "same",
            "share_hashes": [],
            "trashed": False,
            "type": "folder",
            "upload_date": valid["root"]["upload_date"],
        },
    }
    with pytest.raises(CorruptStoreError, match="duplicate"):
        DriveStore._from_payload(valid)


@pytest.mark.parametrize(
    "bad_path",
    ["relative", "/../escape", "/./child", "/child//file", "/child\\file"],
)
def test_paths_reject_traversal_and_ambiguous_forms(bad_path) -> None:
    store = DriveStore.create()
    with pytest.raises(InvalidPathError):
        store.get_directory(bad_path)


def test_root_mutations_are_rejected_and_trailing_slash_is_normalized() -> None:
    store = DriveStore.create()
    folder = store.create_folder("/", "Folder")
    folder_path = _full_path(folder)

    assert store.get_directory(f"{folder_path}/")["id"] == folder["id"]
    with pytest.raises(RootMutationError):
        store.rename("/", "new root")
    with pytest.raises(RootMutationError):
        store.set_trashed("/", True)
    with pytest.raises(RootMutationError):
        store.delete("/")


def test_unicode_names_and_casefolded_sibling_duplicates() -> None:
    store = DriveStore.create()
    store.create_folder("/", "Résumé")

    with pytest.raises(DuplicateNameError):
        store.add_file("/", "RÉSUMÉ", 1, 0)
    with pytest.raises(InvalidNameError):
        store.create_folder("/", " leading")
    with pytest.raises(InvalidNameError):
        store.create_folder("/", "bad/name")
    with pytest.raises(InvalidNameError):
        store.create_folder("/", "bad\nname")


def test_sqlite_failure_rolls_back_rows_and_revision(tmp_path) -> None:
    database = tmp_path / "drive.sqlite3"
    store = DriveStore.create(database)
    folder = store.create_folder("/", "Before")
    folder_path = _full_path(folder)
    previous_revision = store.revision
    connection = sqlite3.connect(database)
    connection.execute("""CREATE TRIGGER fail_update BEFORE UPDATE ON nodes
        BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END""")
    connection.commit()
    connection.close()
    with pytest.raises(PersistenceError, match="simulated storage failure"):
        store.rename(folder_path, "After")
    assert store.revision == previous_revision
    assert DriveStore.load(database).get_directory(folder_path)["name"] == "Before"


def test_backup_revision_race_cannot_clear_newer_mutation() -> None:
    store = DriveStore.create()
    snapshot_revision, snapshot = store.snapshot()
    assert json.loads(snapshot)["revision"] == snapshot_revision

    store.create_folder("/", "arrived during upload")
    newest_revision = store.revision
    assert newest_revision > snapshot_revision
    assert store.mark_backed_up(snapshot_revision) is True
    assert store.dirty is True
    assert store.mark_backed_up(newest_revision) is False
    assert store.dirty is False

    with pytest.raises(InvalidRevisionError):
        store.mark_backed_up(newest_revision + 1)


def test_trash_listing_and_search_hide_entire_trashed_subtrees() -> None:
    store = DriveStore.create()
    folder = store.create_folder("/", "Archive")
    folder_path = _full_path(folder)
    secret = store.add_file(folder_path, "Secret Plan.txt", 50, 5)
    secret_path = _full_path(secret)
    visible = store.add_file("/", "Secret Public.txt", 51, 6)

    store.set_trashed(folder_path, True)
    assert set(store.search("secret")) == {visible["id"]}
    assert set(store.search("secret", include_trashed=True)) == {
        secret["id"],
        visible["id"],
    }
    assert set(store.list_trash()) == {folder["id"]}
    assert folder["id"] not in store.list_directory("/")
    with pytest.raises(TrashedItemError):
        store.get_file(secret_path)

    store.set_trashed(folder_path, False)
    store.set_trashed(secret_path, True)
    assert set(store.list_trash()) == {secret["id"]}
    assert store.get_file(secret_path, include_trashed=True)["id"] == secret["id"]


def test_recursive_delete_returns_all_telegram_message_ids() -> None:
    store = DriveStore.create()
    outer = store.create_folder("/", "Outer")
    outer_path = _full_path(outer)
    first = store.add_file(outer_path, "one.bin", 101, 1)
    inner = store.create_folder(outer_path, "Inner")
    inner_path = _full_path(inner)
    second = store.add_file(inner_path, "two.bin", 102, 2)

    with pytest.raises(ValidationError, match="permanent deletion intent"):
        store.delete(outer_path)
    store.set_trashed(outer_path, True)
    intent = store.begin_deletion(outer_path, channel_id=-100123)
    assert set(intent["message_ids"]) == {101, 102}
    assert set(store.finish_deletion(intent["id"])) == {101, 102}
    assert store.export()["deletions"][0]["message_ids"] == []
    assert store.list_directory("/") == {}
    assert first["id"] != second["id"]


def test_message_ids_reads_file_and_nested_folder_without_mutation() -> None:
    store = DriveStore.create()
    outer = store.create_folder("/", "Outer")
    outer_path = _full_path(outer)
    first = store.add_file(outer_path, "one.bin", 301, 1)
    inner = store.create_folder(outer_path, "Inner")
    inner_path = _full_path(inner)
    store.add_file(inner_path, "two.bin", 302, 2)
    revision = store.revision
    dirty = store.dirty

    assert store.message_ids(_full_path(first)) == [301]
    assert set(store.message_ids(outer_path)) == {301, 302}
    assert store.revision == revision
    assert store.dirty is dirty


def test_message_ids_rejects_invalid_and_hidden_targets() -> None:
    store = DriveStore.create()
    folder = store.create_folder("/", "Trash me")
    folder_path = _full_path(folder)
    store.add_file(folder_path, "inside.bin", 401, 1)
    store.set_trashed(folder_path, True)

    with pytest.raises(TrashedItemError):
        store.message_ids(folder_path)
    assert store.message_ids(folder_path, include_trashed=True) == [401]
    with pytest.raises(NotFoundError):
        store.message_ids("/missing")
    with pytest.raises(InvalidPathError):
        store.message_ids("/../escape")


def test_share_tokens_are_hashed_and_strictly_scoped() -> None:
    store = DriveStore.create()
    shared = store.create_folder("/", "Shared")
    shared_path = _full_path(shared)
    nested = store.create_folder(shared_path, "Nested")
    nested_path = _full_path(nested)
    shared_file = store.add_file(nested_path, "inside.bin", 201, 1)
    shared_file_path = _full_path(shared_file)
    sibling = store.add_file("/", "outside.bin", 202, 1)
    sibling_path = _full_path(sibling)

    folder_token = store.create_share(shared_path)
    file_token = store.create_share(sibling_path)

    assert store.authorize_directory(shared_path, folder_token) == shared_path
    assert store.authorize_directory(nested_path, folder_token) == shared_path
    assert store.authorize_file(shared_file_path, folder_token) is True
    assert store.authorize_file(sibling_path, folder_token) is False
    assert store.authorize_file(sibling_path, file_token) is True
    assert store.authorize_directory(shared_path, file_token) is None
    assert store.authorize_file(shared_file_path, "wrong") is False

    exported = json.dumps(store.export(), sort_keys=True)
    assert folder_token not in exported
    assert file_token not in exported
    assert hashlib.sha256(folder_token.encode()).hexdigest() in exported
    assert hashlib.sha256(file_token.encode()).hexdigest() in exported

    store.set_trashed(shared_path, True)
    assert store.authorize_file(shared_file_path, folder_token) is False


def test_share_token_history_is_bounded() -> None:
    store = DriveStore.create()
    folder = store.create_folder("/", "Shared")
    folder_path = _full_path(folder)
    tokens = [
        store.create_share(folder_path)
        for _ in range(store_module.MAX_SHARE_TOKENS_PER_ITEM)
    ]
    with pytest.raises(store_module.ValidationError, match="100 links"):
        store.create_share(folder_path)
    assert store.authorize_directory(folder_path, tokens[0]) == folder_path
    assert store.authorize_directory(folder_path, tokens[-1]) == folder_path

    exported = store.export()
    child = next(iter(exported["root"]["children"].values()))
    assert len(child["share_hashes"]) == store_module.MAX_SHARE_TOKENS_PER_ITEM


def test_committed_metadata_survives_process_exit_without_close(tmp_path) -> None:
    database = tmp_path / "crash.sqlite3"
    subprocess.run(  # noqa: S603 - fixed interpreter and trusted test script
        [
            sys.executable,
            "-c",
            "import os,sys; from tgdrive.storage.store import DriveStore; "
            "s=DriveStore.create(sys.argv[1]); "
            "s.add_file('/', 'survives.bin', 123, 4, operation_id='crash-operation'); "
            "os._exit(0)",
            str(database),
        ],
        check=True,
    )
    recovered = DriveStore.load(database)
    row = next(iter(recovered.list_directory("/").values()))
    assert recovered.get_file(_full_path(row))["message_id"] == 123
    again = recovered.add_file(
        "/", "survives.bin", 123, 4, operation_id="crash-operation"
    )
    assert again["id"] == row["id"]
    assert len(recovered.list_directory("/")) == 1


def test_independent_connections_serialize_writers_without_lost_updates(
    tmp_path,
) -> None:
    database = tmp_path / "concurrent.sqlite3"
    first = DriveStore.create(database)
    second = DriveStore.load(database)
    with ThreadPoolExecutor(max_workers=2) as workers:
        left = workers.submit(
            lambda: [first.create_folder("/", f"left-{n}") for n in range(20)]
        )
        right = workers.submit(
            lambda: [second.create_folder("/", f"right-{n}") for n in range(20)]
        )
        left.result()
        right.result()
    assert first.revision == second.revision == 41
    assert len(DriveStore.load(database).list_directory("/")) == 40


def test_deletion_intent_survives_restart_with_partial_confirmations(tmp_path) -> None:
    database = tmp_path / "deletion.sqlite3"
    store = DriveStore.create(database)
    folder = store.create_folder("/", "Removal")
    path = _full_path(folder)
    store.add_file(path, "one.bin", 101, 1)
    store.add_file(path, "two.bin", 102, 2)
    store.set_trashed(path, True)
    intent = store.begin_deletion(path, channel_id=-100123)
    store.confirm_deletion_messages(intent["id"], [101])
    store.fail_deletion(intent["id"], "transport lost")
    store.close()
    recovered = DriveStore.load(database)
    resumed = recovered.pending_deletions()[0]
    assert resumed["channel_id"] == -100123
    assert resumed["message_ids"] == [101, 102]
    assert resumed["pending_message_ids"] == [102]
    assert recovered.message_ids(path, include_trashed=True) == [101, 102]
    assert recovered.begin_deletion(path)["id"] == intent["id"]
    with pytest.raises(ValidationError, match="pending permanent deletion"):
        recovered.set_trashed(path, False)
    with pytest.raises(ValidationError, match="outside"):
        recovered.confirm_deletion_messages(intent["id"], [999])
    assert recovered.finish_deletion(intent["id"]) == [101, 102]
    assert recovered.finish_deletion(intent["id"]) == []
    assert recovered.pending_deletions() == []
    assert recovered.export()["deletions"][0]["message_ids"] == []
    assert recovered.export()["deletions"][0]["pending_message_ids"] == []


def test_deletion_backups_preserve_refs_and_reject_forged_confirmation() -> None:
    store = DriveStore.create()
    file = store.add_file("/", "delete.bin", 99, 1)
    path = _full_path(file)
    store.set_trashed(path, True)
    intent = store.begin_deletion(path)
    payload = store.export()
    restored = DriveStore._from_payload(payload)
    assert restored.pending_deletions() == store.pending_deletions()
    payload["deletions"][0]["message_ids"] = [1000]
    payload["deletions"][0]["pending_message_ids"] = [1000]
    with pytest.raises(CorruptStoreError, match="retained subtree"):
        DriveStore._from_payload(payload)
    assert intent["pending_message_ids"] == [99]


def test_move_updates_ancestry_without_changing_ids_and_rejects_cycles() -> None:
    store = DriveStore.create()
    source = store.create_folder("/", "Source")
    target = store.create_folder("/", "Target")
    nested = store.create_folder(_full_path(source), "Nested")
    file = store.add_file(_full_path(nested), "document.txt", 10, 7)
    token = store.create_share(_full_path(nested))
    moved = store.move(_full_path(nested), _full_path(target))
    assert moved["id"] == nested["id"]
    new_path = store.resolve_id(file["id"])
    assert new_path == _full_path(moved) + "/" + file["id"]
    assert store.authorize_file(new_path, token)
    assert store.list_shares()[0]["path"] == _full_path(moved)
    with pytest.raises(InvalidPathError, match="own subtree"):
        store.move(_full_path(target), _full_path(moved))
    with pytest.raises(NotFoundError):
        store.get_directory(_full_path(nested))


def test_move_conflicts_skip_or_keep_both_preserving_extension() -> None:
    store = DriveStore.create()
    target = store.create_folder("/", "Destination")
    file = store.add_file("/", "résumé.txt", 1, 1)
    store.add_file(_full_path(target), "RÉSUMÉ.txt", 2, 1)
    revision = store.revision
    assert store.move(_full_path(file), _full_path(target), conflict="skip") is None
    assert store.revision == revision
    moved = store.move(_full_path(file), _full_path(target))
    assert moved["name"] == "résumé (1).txt"
    assert moved["id"] == file["id"]


def test_share_expiry_rotation_revocation_and_trash_are_durable(
    tmp_path, monkeypatch
) -> None:
    import tgdrive.storage.store as module

    store = DriveStore.create(tmp_path / "shares.sqlite3")
    folder = store.create_folder("/", "Shared")
    file = store.add_file(_full_path(folder), "secret.bin", 5, 6)
    folder_path, file_path = _full_path(folder), _full_path(file)
    token = store.create_share(folder_path)
    expiry = store.list_shares()[0]["expires_at"]
    assert datetime.fromisoformat(expiry.replace("Z", "+00:00")) > datetime.now(
        UTC
    ) + timedelta(days=6)
    monkeypatch.setattr(module, "_utc_now", lambda: "9999-01-01T00:00:00Z")
    assert not store.authorize_file(file_path, token)
    monkeypatch.undo()
    token = store.create_share(file_path, expires_at=None)
    assert store.list_shares(file_path)[0]["expires_at"] is None
    restored = DriveStore.load(store.storage_path)
    assert restored.authorize_file(file_path, token)
    share_id = restored.list_shares(file_path)[0]["id"]
    assert restored.revoke_share(share_id)
    assert not store.authorize_file(file_path, token)
    token = store.create_share(file_path)
    store.set_trashed(folder_path, True)
    store.set_trashed(folder_path, False)
    assert not store.authorize_file(file_path, token)
    assert store.list_shares() == []


def test_operation_id_is_idempotent_across_restart_and_permanent_deletion(
    tmp_path,
) -> None:
    database = tmp_path / "idempotent.sqlite3"
    store = DriveStore.create(database)
    file = store.add_file("/", "one.bin", 21, 1, operation_id="one-operation")
    assert store.add_file("/", "one.bin", 21, 1, operation_id="one-operation") == file
    revision = store.revision
    with pytest.raises(ValidationError, match="different media"):
        store.add_file("/", "other.bin", 22, 1, operation_id="one-operation")
    assert store.revision == revision
    path = _full_path(file)
    store.set_trashed(path, True)
    intent = store.begin_deletion(path)
    store.confirm_deletion_messages(intent["id"], [21])
    store.finish_deletion(intent["id"])
    recovered = DriveStore.load(database)
    with pytest.raises(ValidationError, match="file was deleted"):
        recovered.add_file("/", "one.bin", 21, 1, operation_id="one-operation")
    assert recovered.list_directory("/") == {}


def test_upload_collision_and_trash_restore_keep_both_without_losing_ids() -> None:
    store = DriveStore.create()
    first = store.add_file("/", "report.txt", 1, 1)
    second = store.add_file(
        "/", "REPORT.txt", 2, 2, keep_both=True, operation_id="duplicate-upload"
    )
    assert second["name"] == "REPORT (1).txt"
    assert (
        store.add_file(
            "/", "REPORT.txt", 2, 2, keep_both=True, operation_id="duplicate-upload"
        )
        == second
    )
    store.set_trashed(_full_path(first), True)
    replacement = store.add_file("/", "report.txt", 3, 3)
    restored = store.set_trashed(_full_path(first), False)
    assert restored["id"] == first["id"]
    assert restored["name"] == "report (2).txt"
    assert store.get_file(_full_path(replacement))["message_id"] == 3
    assert len(store.list_directory("/")) == 3
    assert DriveStore._from_payload(store.export()).export() == store.export()


def test_interrupted_snapshot_import_never_publishes_an_empty_database(
    tmp_path,
) -> None:
    source = tmp_path / "checkpoint.json"
    original = DriveStore.create()
    original.create_folder("/", "Must survive")
    source.write_bytes(original.snapshot()[1])
    destination = tmp_path / "restore.sqlite3"
    completed = subprocess.run(  # noqa: S603 - trusted fixed test code
        [
            sys.executable,
            "-c",
            "import json,os,sys; from tgdrive.storage.store import DriveStore; "
            "DriveStore._import_shares=lambda *args: os._exit(9); "
            "DriveStore._from_payload(json.load(open(sys.argv[1])),storage_path=sys.argv[2],persist=True)",
            str(source),
            str(destination),
        ],
        check=False,
    )
    assert completed.returncode == 9
    assert not destination.exists()
    assert len(list(tmp_path.glob(".restore-*.sqlite3"))) == 1


def test_database_load_rejects_inconsistent_indexes_and_overlapping_deletions(
    tmp_path,
) -> None:
    database = tmp_path / "index.sqlite3"
    store = DriveStore.create(database)
    folder = store.create_folder("/", "Folder")
    connection = sqlite3.connect(database)
    connection.execute("UPDATE nodes SET name_key='wrong' WHERE id=?", (folder["id"],))
    connection.commit()
    connection.close()
    with pytest.raises(CorruptStoreError, match="name index"):
        DriveStore.load(database)


def test_store_finalizer_closes_abandoned_connection_without_changing_committed_data(
    tmp_path,
):
    database = tmp_path / "finalized.sqlite3"
    store = DriveStore.create(database)
    item = store.add_file("/", "committed.bin", 71, 3)
    connection = store._db
    reference = weakref.ref(store)
    del store
    gc.collect()
    assert reference() is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    restored = DriveStore.load(database)
    assert restored.get_file(_full_path(item))["message_id"] == 71
    restored.close()
    restored.close()


@pytest.mark.parametrize("field", ["drive_id", "shares"])
def test_portable_catalog_requires_current_identity_and_share_records(field):
    source = DriveStore.create()
    payload = source.export()
    source.close()
    del payload[field]
    with pytest.raises(CorruptStoreError, match="missing or unknown fields"):
        DriveStore._from_payload(payload)
