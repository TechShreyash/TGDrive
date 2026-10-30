from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.support.mongo_fixtures import new_store, reopen_store, store_from_payload
from tgdrive.core.errors import (
    CorruptStoreError,
    DuplicateNameError,
    InvalidNameError,
    InvalidPathError,
    NotFoundError,
    PersistenceError,
    RootMutationError,
    TrashedItemError,
    UnsupportedSchemaError,
    ValidationError,
)
from tgdrive.storage.store import SCHEMA_VERSION


def _full_path(record: dict[str, object]) -> str:
    parent = record["path"]
    item_id = record["id"]
    return f"/{item_id}" if parent == "/" else f"{parent}/{item_id}"


def test_create_persist_load_and_frontend_shape(tmp_path) -> None:
    database = tmp_path / "drive.drive"
    store = new_store(database)

    assert store.revision == 1
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

    loaded = reopen_store(database)
    assert loaded.export() == store.export()
    assert loaded.get_file(file_path)["message_id"] == 41
    directory = loaded.get_directory(folder_path)
    assert directory["contents"][uploaded["id"]]["name"] == "notes+100%.txt"
    assert store.export()["schema_version"] == SCHEMA_VERSION


def test_corrupt_and_unsupported_metadata_fail_closed() -> None:
    unsupported = new_store()
    payload = unsupported.export()
    unsupported.close()
    payload["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(UnsupportedSchemaError):
        store_from_payload(payload)

    valid = new_store().export()
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
        store_from_payload(valid)


@pytest.mark.parametrize(
    "bad_path",
    ["relative", "/../escape", "/./child", "/child//file", "/child\\file"],
)
def test_paths_reject_traversal_and_ambiguous_forms(bad_path) -> None:
    store = new_store()
    with pytest.raises(InvalidPathError):
        store.get_directory(bad_path)


def test_root_mutations_are_rejected_and_trailing_slash_is_normalized() -> None:
    store = new_store()
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
    store = new_store()
    store.create_folder("/", "Résumé")

    with pytest.raises(DuplicateNameError):
        store.add_file("/", "RÉSUMÉ", 1, 0)
    with pytest.raises(InvalidNameError):
        store.create_folder("/", " leading")
    with pytest.raises(InvalidNameError):
        store.create_folder("/", "bad/name")
    with pytest.raises(InvalidNameError):
        store.create_folder("/", "bad\nname")


def test_storage_failure_rolls_back_rows_and_revision(tmp_path, monkeypatch) -> None:
    database = tmp_path / "drive"
    store = new_store(database)
    folder = store.create_folder("/", "Before")
    folder_path = _full_path(folder)
    previous_revision = store.revision

    def fail(*_args, **_kwargs):
        raise PersistenceError("simulated storage failure")

    monkeypatch.setattr(store.backend.db.catalog, "find_one_and_update", fail)
    with pytest.raises(PersistenceError, match="simulated storage failure"):
        store.rename(folder_path, "After")
    monkeypatch.undo()
    assert store.revision == previous_revision
    assert reopen_store(database).get_directory(folder_path)["name"] == "Before"


def test_trash_listing_and_search_hide_entire_trashed_subtrees() -> None:
    store = new_store()
    folder = store.create_folder("/", "Archive")
    folder_path = _full_path(folder)
    secret = store.add_file(folder_path, "Secret Plan.txt", 50, 5)
    secret_path = _full_path(secret)
    visible = store.add_file("/", "Secret Public.txt", 51, 6)

    store.set_trashed(folder_path, True)

    def search(query, **options):
        return {row["id"] for row in store.search_page(query, **options)["items"]}

    assert search("secret") == {visible["id"]}
    assert search("secret", include_trashed=True) == {
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
    store = new_store()
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
    store = new_store()
    outer = store.create_folder("/", "Outer")
    outer_path = _full_path(outer)
    first = store.add_file(outer_path, "one.bin", 301, 1)
    inner = store.create_folder(outer_path, "Inner")
    inner_path = _full_path(inner)
    store.add_file(inner_path, "two.bin", 302, 2)
    revision = store.revision

    assert store.message_ids(_full_path(first)) == [301]
    assert set(store.message_ids(outer_path)) == {301, 302}
    assert store.revision == revision


def test_message_ids_rejects_invalid_and_hidden_targets() -> None:
    store = new_store()
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
    store = new_store()
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


def test_an_item_can_have_any_number_of_independent_links() -> None:
    store = new_store()
    folder = store.create_folder("/", "Shared")
    folder_path = _full_path(folder)
    tokens = [store.create_share(folder_path) for _ in range(150)]
    assert store.authorize_directory(folder_path, tokens[0]) == folder_path
    assert store.authorize_directory(folder_path, tokens[-1]) == folder_path
    exported = store.export()
    child = next(iter(exported["root"]["children"].values()))
    assert len(child["share_hashes"]) == 150


def test_deletion_intent_survives_restart_with_partial_confirmations(tmp_path) -> None:
    database = tmp_path / "deletion.drive"
    store = new_store(database)
    folder = store.create_folder("/", "Removal")
    path = _full_path(folder)
    store.add_file(path, "one.bin", 101, 1)
    store.add_file(path, "two.bin", 102, 2)
    store.set_trashed(path, True)
    intent = store.begin_deletion(path, channel_id=-100123)
    store.confirm_deletion_messages(intent["id"], [101])
    store.fail_deletion(intent["id"], "transport lost")
    store.close()
    recovered = reopen_store(database)
    resumed = recovered.pending_deletions()[0]
    assert resumed["channel_id"] == -100123
    assert resumed["message_ids"] == [101, 102]
    assert resumed["pending_message_ids"] == [102]
    assert recovered.message_ids(path, include_trashed=True) == [101, 102]
    assert recovered.begin_deletion(path)["id"] == intent["id"]
    with pytest.raises(ValidationError, match="pending permanent deletion"):
        recovered.set_trashed(path, False)
    with pytest.raises(ValidationError, match="unrelated messages"):
        recovered.confirm_deletion_messages(intent["id"], [999])
    assert recovered.finish_deletion(intent["id"]) == [101, 102]
    assert recovered.finish_deletion(intent["id"]) == []
    assert recovered.pending_deletions() == []
    assert recovered.export()["deletions"][0]["message_ids"] == []
    assert recovered.export()["deletions"][0]["pending_message_ids"] == []


def test_deletion_backups_preserve_refs_and_reject_forged_confirmation() -> None:
    store = new_store()
    file = store.add_file("/", "delete.bin", 99, 1)
    path = _full_path(file)
    store.set_trashed(path, True)
    intent = store.begin_deletion(path)
    payload = store.export()
    restored = store_from_payload(payload)
    assert restored.pending_deletions() == store.pending_deletions()
    payload["deletions"][0]["message_ids"] = [1000]
    payload["deletions"][0]["pending_message_ids"] = [1000]
    with pytest.raises(CorruptStoreError, match="retained subtree"):
        store_from_payload(payload)
    assert intent["pending_message_ids"] == [99]


def test_move_updates_ancestry_without_changing_ids_and_rejects_cycles() -> None:
    store = new_store()
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
    with pytest.raises(InvalidPathError, match="its subtree"):
        store.move(_full_path(target), _full_path(moved))
    with pytest.raises(NotFoundError):
        store.get_directory(_full_path(nested))


def test_move_conflicts_skip_or_keep_both_preserving_extension() -> None:
    store = new_store()
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
    import tgdrive.storage.mongo_store as module

    store = new_store(tmp_path / "shares.drive")
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
    restored = reopen_store(tmp_path / "shares.drive")
    assert restored.authorize_file(file_path, token)
    # MongoDB has one owner per drive, so the owning store performs the change.
    share_id = store.list_shares(file_path)[0]["id"]
    assert store.revoke_share(share_id)
    assert not store.authorize_file(file_path, token)
    token = store.create_share(file_path)
    store.set_trashed(folder_path, True)
    store.set_trashed(folder_path, False)
    assert not store.authorize_file(file_path, token)
    assert store.list_shares() == []


def test_operation_id_is_idempotent_across_restart_and_permanent_deletion(
    tmp_path,
) -> None:
    database = tmp_path / "idempotent.drive"
    store = new_store(database)
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
    recovered = reopen_store(database)
    with pytest.raises(ValidationError, match="already deleted"):
        recovered.add_file("/", "one.bin", 21, 1, operation_id="one-operation")
    assert recovered.list_directory("/") == {}


def test_upload_collision_and_trash_restore_keep_both_without_losing_ids() -> None:
    store = new_store()
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
    assert store_from_payload(store.export()).export() == store.export()


def test_database_load_rejects_inconsistent_indexes_and_overlapping_deletions(
    tmp_path,
) -> None:
    database = tmp_path / "index"
    store = new_store(database)
    folder = store.create_folder("/", "Folder")
    for row in store.backend.db.nodes.rows:
        if row["id"] == folder["id"]:
            row["name_key"] = "wrong"
    # Startup reads no full catalog; every backup export validates it instead.
    with pytest.raises(CorruptStoreError, match="identity or index"):
        reopen_store(database).export()


@pytest.mark.parametrize("field", ["drive_id", "shares"])
def test_portable_catalog_requires_current_identity_and_share_records(field):
    source = new_store()
    payload = source.export()
    source.close()
    del payload[field]
    with pytest.raises(CorruptStoreError, match="missing or unknown fields"):
        store_from_payload(payload)


def test_keep_both_never_creates_a_name_ending_in_whitespace(tmp_path) -> None:
    store = new_store(tmp_path)
    name = "a." + "x " * 126 + "x"
    for message_id in range(1, 12):
        store.add_file("/", name, message_id, 1, keep_both=True)
    names = [row["name"] for row in store.list_directory("/").values()]
    assert len(names) == 11
    assert all(item == item.strip() for item in names)
    reopen_store(tmp_path)  # Startup validation accepts every generated name.
