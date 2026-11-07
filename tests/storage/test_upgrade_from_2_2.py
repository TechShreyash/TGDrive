"""A 2.0-2.2 catalog (schema 4) upgrades in place and keeps every record."""

import gzip
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from bson import json_util
from pydantic import SecretStr
from pymongo.errors import ConnectionFailure

from tests.support.mongo_fixtures import FakeMongoBackend
from tgdrive.core import encryption
from tgdrive.core.errors import CorruptStoreError
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.upgrade import needs_upgrade, upgrade

KEY = "e" * 64


def node(identity, parent, name, kind, **values):
    # The exact shape TGDrive 2.2's MongoStore._node wrote.
    return {
        "_id": identity,
        "id": identity,
        "parent_id": parent,
        "name": name,
        "name_key": name.casefold(),
        "kind": kind,
        "trashed": False,
        "upload_date": "2026-10-01T00:00:00Z",
        "size": 0,
        "message_id": None,
        "part_count": 1,
        "sha256": None,
        **values,
    }


def drive_2_2():
    backend = FakeMongoBackend()
    db = backend.db
    db.catalog.insert_one(
        {
            "_id": "catalog",
            "schema_version": 4,
            "drive_id": "D" * 22,
            "revision": 9,
            "backed_up_revision": 9,
            "generation": 8,
            "storage_channel_id": -1001234567890,
        }
    )
    db.nodes.insert_many(
        [
            node("root", None, "/", "folder"),
            node("A" * 22, "root", "Anime", "folder", starred=True),
            node(
                "E" * 22,
                "A" * 22,
                "[SubsPlease] Frieren - 01 (1080p).mkv",
                "file",
                size=3 * 2**30,
                message_id=102,
                part_count=2,
                parts=[
                    {
                        "index": 0,
                        "offset": 0,
                        "size": 2**31,
                        "message_id": 102,
                        "sha256": "b" * 64,
                    },
                    {
                        "index": 1,
                        "offset": 2**31,
                        "size": 2**30,
                        "message_id": 103,
                        "sha256": "c" * 64,
                    },
                ],
            ),
            node(
                "T" * 22,
                "root",
                "old draft.docx",
                "file",
                size=10,
                message_id=105,
                trashed=True,
            ),
        ]
    )
    db.shares.insert_many(
        [
            {
                "_id": "S" * 22,
                "id": "S" * 22,
                "node_id": "A" * 22,
                "token_hash": "h" * 64,
                "created_at": "2026-10-01T00:00:00Z",
                "expires_at": "2026-10-08T00:00:00Z",
            },
            {
                "_id": "P" * 22,
                "id": "P" * 22,
                "node_id": "A" * 22,
                "token_hash": "k" * 64,
                "created_at": "2026-10-01T00:00:00Z",
                "expires_at": None,
            },
        ]
    )
    db.settings.insert_one(
        {"_id": "bootstrap", "document": {"MONGODB_BACKUP_KEY": "a" * 64}}
    )
    return backend


def settings(tmp_path):
    return SimpleNamespace(resolved_data_dir=tmp_path, encryption_key=SecretStr(KEY))


def test_2_2_drive_is_refused_until_upgraded():
    with pytest.raises(CorruptStoreError):
        MongoStore(drive_2_2())


def test_upgrade_keeps_every_record_and_opens_in_2_3(tmp_path):
    backend = drive_2_2()
    before = {name: len(backend.db[name].rows) for name in ("nodes", "shares")}
    assert needs_upgrade(backend)
    copy_path = upgrade(backend, settings(tmp_path))
    with gzip.open(copy_path) as copy:
        saved = [json_util.loads(line) for line in copy]
    assert {row["collection"] for row in saved} >= {"catalog", "nodes", "shares"}
    assert (tmp_path / copy_path.rsplit("/", 1)[-1]).stat().st_mode & 0o777 == 0o600
    assert {name: len(backend.db[name].rows) for name in before} == before
    encryption.check_drive_key(backend, SecretStr(KEY))
    store = MongoStore(backend)
    assert store.statistics() == {
        "files": 1,
        "folders": 1,
        "total_bytes": 3 * 2**30,
        "trashed_files": 1,
        "trashed_bytes": 10,
    }
    assert [row["name"] for row in store.search_page("frieren 1")["items"]] == [
        "[SubsPlease] Frieren - 01 (1080p).mkv"
    ]
    assert [row["name"] for row in store.list_starred().values()] == ["Anime"]
    assert [row["name"] for row in store.list_trash().values()] == ["old draft.docx"]
    episode = store.get_file("/" + "A" * 22 + "/" + "E" * 22)
    assert [part["message_id"] for part in episode["parts"]] == [102, 103]
    assert all("nonce" not in part for part in episode["parts"])  # Plain bytes.
    assert store.list_page("/" + "A" * 22, group="media")["total"] == 1
    shares = {row["_id"]: row for row in backend.db.shares.rows}
    assert "expire_at" in shares["S" * 22] and "expire_at" not in shares["P" * 22]
    bootstrap = backend.db.settings.find_one({"_id": "bootstrap"})["document"]
    assert "MONGODB_BACKUP_KEY" not in bootstrap
    store.create_folder("/", "Made in 2.3")


def test_upgrade_runs_once_and_resumes_after_interruption(tmp_path, monkeypatch):
    backend = drive_2_2()
    from tgdrive.storage import upgrade as module

    def stop(_backend):
        raise RuntimeError("power cut")

    monkeypatch.setattr(module, "_totals", stop)
    with pytest.raises(RuntimeError):
        upgrade(backend, settings(tmp_path))
    assert needs_upgrade(backend)  # Version unchanged: the next start retries.
    copies = list(tmp_path.glob("upgrade-2.2-*.json.gz"))
    assert len(copies) == 1
    original_copy = copies[0].read_bytes()
    monkeypatch.undo()
    assert upgrade(backend, settings(tmp_path)) is not None
    assert upgrade(backend, settings(tmp_path)) is None
    assert MongoStore(backend).statistics()["files"] == 1
    assert list(tmp_path.glob("upgrade-2.2-*.json.gz")) == copies
    assert copies[0].read_bytes() == original_copy


def test_upgrade_drops_the_2_2_backup_channel_so_it_can_be_set_up_again(tmp_path):
    from tgdrive.replication.replica_state import ReplicaJournal

    backend = drive_2_2()
    backend.db.replica_state.insert_one({"_id": "config", "length": 2})
    backend.db.replica_chunks.insert_one({"_id": "c", "key": "config", "data": "{}"})
    backend.db.replica_copies.insert_one({"_id": "part", "message_id": 9})
    (tmp_path / "channel-backup.sqlite3").write_bytes(b"2.2 journal")
    (tmp_path / "replica-bootstrap.json").write_text("{}")
    upgrade(backend, settings(tmp_path))
    for name in ("replica_state", "replica_chunks", "replica_copies"):
        assert backend.db[name].rows == []
    assert not (tmp_path / "channel-backup.sqlite3").exists()
    assert (tmp_path / "channel-backup.sqlite3.2.2-unused").read_bytes() == (
        b"2.2 journal"
    )
    assert (tmp_path / "replica-bootstrap.json.2.2-unused").exists()
    assert ReplicaJournal(tmp_path, backend).get("config") is None


@pytest.mark.parametrize("prior", [0, [], {}, "../outside.json.gz"])
def test_invalid_saved_copy_identity_stops_before_migration(tmp_path, prior):
    backend = drive_2_2()
    backend.db.catalog.update_one({"_id": "catalog"}, {"$set": {"upgrade_copy": prior}})
    with pytest.raises(ValueError, match="original migration copy"):
        upgrade(backend, settings(tmp_path))
    assert needs_upgrade(backend)
    assert "tokens" not in backend.db.nodes.find_one({"_id": "E" * 22})
    assert not list(tmp_path.iterdir())


def test_failed_copy_removes_only_its_incomplete_temporary_file(tmp_path, monkeypatch):
    backend = drive_2_2()

    def failed(*_args, **_kwargs):
        raise OSError("Snapshot cursor failed")

    monkeypatch.setattr(backend.db.nodes, "find", failed)
    with pytest.raises(OSError, match="Snapshot cursor"):
        upgrade(backend, settings(tmp_path))
    assert list(tmp_path.iterdir()) == []
    assert backend.db.catalog.find_one({"_id": "catalog"})["schema_version"] == 4
    assert "upgrade_copy" not in backend.db.catalog.find_one({"_id": "catalog"})


def test_durable_copy_identity_is_saved_before_the_first_migration_write(
    tmp_path, monkeypatch
):
    backend = drive_2_2()
    bulk = backend.db.nodes.bulk_write
    copies = []

    def checked(updates, **kwargs):
        name = backend.db.catalog.find_one({"_id": "catalog"})["upgrade_copy"]
        path = tmp_path / name
        assert path.stat().st_mode & 0o777 == 0o600
        with gzip.open(path) as original:
            rows = [json_util.loads(line) for line in original]
        original_catalog = next(
            row["document"] for row in rows if row["collection"] == "catalog"
        )
        assert original_catalog["schema_version"] == 4
        assert "upgrade_copy" not in original_catalog
        assert all(
            "tokens" not in row["document"]
            for row in rows
            if row["collection"] == "nodes"
        )
        copies.append(path)
        return bulk(updates, **kwargs)

    monkeypatch.setattr(backend.db.nodes, "bulk_write", checked)
    copy_path = upgrade(backend, settings(tmp_path))
    assert copies == [Path(copy_path)]


def test_copy_name_collision_cannot_overwrite_existing_rollback_bytes(
    tmp_path, monkeypatch
):
    from tgdrive.storage import upgrade as module

    backend = drive_2_2()
    frozen = datetime(2000, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda _zone: frozen))
    monkeypatch.setattr(module.secrets, "token_hex", lambda _count: "a" * 16)
    original = tmp_path / "upgrade-2.2-20000101T000000Z-aaaaaaaaaaaaaaaa.json.gz"
    original.write_bytes(b"preserved rollback copy")
    original.chmod(0o600)
    with pytest.raises(FileExistsError):
        upgrade(backend, settings(tmp_path))
    assert original.read_bytes() == b"preserved rollback copy"
    assert list(tmp_path.iterdir()) == [original]
    assert needs_upgrade(backend)
    assert "upgrade_copy" not in backend.db.catalog.find_one({"_id": "catalog"})


def test_lost_copy_identity_acknowledgement_reuses_the_original_on_retry(
    tmp_path, monkeypatch
):
    backend = drive_2_2()
    update = backend.db.catalog.update_one

    def lost_ack(query, changes, **kwargs):
        result = update(query, changes, **kwargs)
        if "upgrade_copy" in changes.get("$set", {}):
            raise ConnectionFailure("Copy identity acknowledgement lost")
        return result

    monkeypatch.setattr(backend.db.catalog, "update_one", lost_ack)
    with pytest.raises(ConnectionFailure, match="acknowledgement lost"):
        upgrade(backend, settings(tmp_path))
    name = backend.db.catalog.find_one({"_id": "catalog"})["upgrade_copy"]
    original = tmp_path / name
    original_bytes = original.read_bytes()
    assert "tokens" not in backend.db.nodes.find_one({"_id": "E" * 22})
    monkeypatch.setattr(backend.db.catalog, "update_one", update)
    assert upgrade(backend, settings(tmp_path)) == str(original)
    assert original.read_bytes() == original_bytes
    assert list(tmp_path.iterdir()) == [original]
