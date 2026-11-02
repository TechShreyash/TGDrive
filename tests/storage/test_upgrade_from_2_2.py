"""A 2.0-2.2 catalog (schema 4) upgrades in place and keeps every record."""

import gzip
from types import SimpleNamespace

import pytest
from bson import json_util
from pydantic import SecretStr

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
    monkeypatch.undo()
    assert upgrade(backend, settings(tmp_path)) is not None
    assert upgrade(backend, settings(tmp_path)) is None
    assert MongoStore(backend).statistics()["files"] == 1
