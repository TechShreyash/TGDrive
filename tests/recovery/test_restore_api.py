"""Archive restore guards and owner HTTP contract with isolated Mongo doubles."""

import asyncio
import copy
import zlib
from threading import Event, RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from bson import json_util
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import tgdrive.recovery.mongo_backup as archive
import tgdrive.recovery.restore_api as api
from tgdrive.core.errors import CorruptStoreError
from tgdrive.core.security import SESSION_COOKIE, InvalidSession, SessionManager
from tgdrive.recovery.backup import BackupError
from tgdrive.storage.store import ROOT_ID, SCHEMA_VERSION, DriveStore
from tgdrive.web.http_guard import ApiBodyGuardMiddleware


class Collection:
    def __init__(self):
        self.rows = []
        self.fail_insert = False

    def find(self, query, **_kwargs):
        if "expires" in query:
            return [
                dict(row)
                for row in self.rows
                if row["expires"] > query["expires"]["$gt"]
            ]
        return copy.deepcopy(self.rows)

    def find_one(self, _query, **_kwargs):
        return copy.deepcopy(self.rows[0]) if self.rows else None

    def delete_many(self, query, **_kwargs):
        if "expires" in query:
            self.rows = [
                row for row in self.rows if row["expires"] > query["expires"]["$lte"]
            ]
        else:
            self.rows.clear()

    def insert_one(self, row, **_kwargs):
        self.rows.append(copy.deepcopy(row))

    def insert_many(self, rows, **_kwargs):
        if self.fail_insert:
            raise RuntimeError("Synthetic transaction failure")
        self.rows.extend(copy.deepcopy(rows))


class Collections(dict):
    def __getattr__(self, name):
        return self[name]


class Backend:
    def __init__(self):
        self.db = Collections({name: Collection() for name in archive.COLLECTIONS})
        self.ping, self.fence = Mock(), Mock()
        self.pause_writes = Mock()
        self.acquire, self.release = Mock(), Mock()
        self._settings_lock = RLock()
        self._settings_cached = True
        self._settings_document = object()

    def transaction(self, operation, *, allow_paused=False):
        before = {name: copy.deepcopy(value.rows) for name, value in self.db.items()}
        try:
            return operation(object())
        except BaseException:
            for name, rows in before.items():
                self.db[name].rows = rows
            raise

    def write(self, collection, method, *args, **kwargs):
        return getattr(collection, method)(*args, **kwargs)


def pristine(backend):
    backend.db.catalog.rows = [
        {
            "_id": "catalog",
            "schema_version": SCHEMA_VERSION,
            "drive_id": "new-drive",
            "revision": 2,
            "generation": 1,
        }
    ]
    backend.db.nodes.rows = [
        {
            "_id": ROOT_ID,
            "parent_id": None,
            "kind": "folder",
            "name": "/",
            "trashed": False,
        }
    ]
    backend.db.settings.rows = [
        {"_id": "bootstrap", "document": {}},
        {"_id": "telegram", "revision": 1, "document": {}},
    ]


def recovered_data():
    result = {name: [] for name in archive.COLLECTIONS}
    result["catalog"] = [
        {
            "_id": "catalog",
            "schema_version": SCHEMA_VERSION,
            "drive_id": "recovered-drive",
            "revision": 7,
            "backed_up_revision": 7,
            "generation": 6,
        }
    ]
    result["nodes"] = [archive.MongoStore._node(ROOT_ID, None, "/", "folder")]
    result["sessions"] = [{"_id": "old-session", "expires": 9999999999}]
    return result


def current_archive_records():
    store = DriveStore.create()
    try:
        folder = store.create_folder("/", "Folder")
        file = store.add_file("/", "Shared.bin", 41, 3, operation_id="saved-operation")
        store.create_share("/" + file["id"])
        trash = store.add_file("/", "Removed.bin", 42, 4)
        store.set_trashed("/" + trash["id"], True)
        store.begin_deletion("/" + trash["id"], channel_id=-1001234567890)
        store.prepare_folder_tree("/", "Prepared", [["Child"]], "prepared-request-1")
        payload = store.export()
    finally:
        store.close()
    data = recovered_data()
    data["catalog"][0].update(
        drive_id=payload["drive_id"],
        revision=payload["revision"],
        backed_up_revision=payload["backed_up_revision"],
    )
    data["nodes"] = []
    stack = [(payload["root"], None)]
    while stack:
        node, parent = stack.pop()
        values = {
            key: value
            for key, value in node.items()
            if key not in {"id", "name", "type", "children", "share_hashes"}
        }
        values["part_count"] = len(node.get("parts") or [None])
        data["nodes"].append(
            archive.MongoStore._node(
                node["id"], parent, node["name"], node["type"], **values
            )
        )
        if node["type"] == "folder":
            stack.extend((child, node["id"]) for child in node["children"].values())
    for name, portable, identity in (
        ("shares", "shares", "id"),
        ("operations", "operations", "operation_id"),
        ("deletions", "deletions", "id"),
        ("preparations", "folder_preparations", "request_id"),
    ):
        data[name] = [dict(row, _id=row[identity]) for row in payload[portable]]
    return data, folder["id"]


def test_current_archive_records_validate_without_sqlite(monkeypatch):
    import sqlite3

    data, _folder = current_archive_records()
    connect = Mock(side_effect=AssertionError("Archive validation opened SQLite"))
    monkeypatch.setattr(sqlite3, "connect", connect)
    archive.MongoStore.validate_snapshot_records(data)
    connect.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    [
        "share_expiry",
        "share_created",
        "share_label",
        "share_identity",
        "operation_kind",
        "operation_identity",
        "deletion_state",
        "deletion_messages",
        "deletion_path",
        "preparation_fingerprint",
        "preparation_mapping",
        "name_key",
        "part_count",
        "starred",
    ],
)
def test_invalid_archive_records_fail_before_database_work(fault):
    data, folder = current_archive_records()
    if fault.startswith("share_"):
        field = fault.removeprefix("share_")
        field = {"expiry": "expires_at", "created": "created_at", "identity": "id"}.get(
            field, field
        )
        data["shares"][0][field] = "invalid" if field in {"created_at", "id"} else 123
    elif fault == "operation_kind":
        data["operations"][0]["node_id"] = folder
    elif fault == "operation_identity":
        data["operations"][0]["operation_id"] = "different-operation"
    elif fault == "deletion_state":
        data["deletions"][0]["state"] = "invalid"
    elif fault == "deletion_messages":
        data["deletions"][0]["pending_message_ids"] = [999]
    elif fault == "deletion_path":
        data["deletions"][0]["path"] = "/" + folder
    elif fault == "preparation_fingerprint":
        data["preparations"][0]["fingerprint"] = "0" * 64
    elif fault == "preparation_mapping":
        data["preparations"][0]["result"]["folders"]["Child"] = "/" + folder
    else:
        data["nodes"][0][fault] = "incorrect"
    backend = Backend()
    with pytest.raises(CorruptStoreError):
        archive.restore_decoded_snapshot(backend, data)
    backend.ping.assert_not_called()
    backend.fence.assert_not_called()
    assert all(not collection.rows for collection in backend.db.values())


def test_pristine_guard_refuses_saved_work_and_settings_changes():
    backend = Backend()
    pristine(backend)
    archive.check_restore_destination(backend, allow_pristine=True)
    backend.db.jobs.rows = [{"_id": "unfinished-job"}]
    with pytest.raises(FileExistsError):
        archive.check_restore_destination(backend, allow_pristine=True)
    backend.db.jobs.rows.clear()
    backend.db.settings.rows[1]["revision"] = 2
    with pytest.raises(FileExistsError):
        archive.check_restore_destination(backend, allow_pristine=True)


def test_import_rolls_back_placeholder_and_revokes_archived_sessions(monkeypatch):
    backend = Backend()
    pristine(backend)
    before = copy.deepcopy(backend.db.catalog.rows)
    backend.db.nodes.fail_insert = True
    with pytest.raises(RuntimeError, match="Synthetic transaction"):
        archive.restore_decoded_snapshot(backend, recovered_data(), allow_pristine=True)
    assert backend.db.catalog.rows == before
    backend.db.nodes.fail_insert = False
    monkeypatch.setattr(archive.MongoStore, "_indexes", Mock())
    archive.restore_decoded_snapshot(backend, recovered_data(), allow_pristine=True)
    assert backend.db.catalog.rows[0]["drive_id"] == "recovered-drive"
    assert backend.db.sessions.rows == []
    assert backend._settings_cached is False


@pytest.mark.parametrize("fault", ["missing_parent", "share_expiry", "name_key"])
def test_authenticated_archive_with_invalid_catalog_is_rejected_before_database_work(
    tmp_path, fault
):
    data, _folder = current_archive_records()
    if fault == "missing_parent":
        data["nodes"][0]["parent_id"] = "missing-parent"
    elif fault == "share_expiry":
        data["shares"][0]["expires_at"] = 123
    else:
        data["nodes"][0]["name_key"] = "incorrect"
    records = [{"format": "tgdrive-mongodb", "version": 1}]
    records.extend(
        {"collection": name, "document": row}
        for name, rows in data.items()
        for row in rows
    )
    records.append(
        {"complete": True, "counts": {name: len(rows) for name, rows in data.items()}}
    )
    content = "\n".join(json_util.dumps(row) for row in records).encode()
    key, nonce = b"k" * 32, b"n" * 12
    encrypted = AESGCM(key).encrypt(nonce, zlib.compress(content), archive.MAGIC)
    path = tmp_path / "invalid.tgdb"
    path.write_bytes(archive.MAGIC + nonce + encrypted)
    with pytest.raises(BackupError, match="authentication or validation"):
        archive.decode_snapshot(path, key)


@pytest.fixture
def owner_api(tmp_path, monkeypatch):
    backend = Backend()
    pristine(backend)
    runtime = SimpleNamespace(
        mongo=backend,
        stop=AsyncMock(return_value=[]),
        replica=SimpleNamespace(blocked=False, status=lambda: {"configured": False}),
    )
    app = FastAPI()
    app.state.sessions = SessionManager(b"s" * 32, 3600, mongo_backend=backend)

    async def require_admin(request: Request):
        try:
            app.state.sessions.verify(request.cookies.get(SESSION_COOKIE, ""))
        except InvalidSession:
            raise HTTPException(401, "Not authenticated") from None

    api.register_restore_routes(
        app,
        SimpleNamespace(
            demo_mode=False,
            resolved_cache_dir=tmp_path / "cache",
            resolved_data_dir=tmp_path / "data",
        ),
        require_admin=require_admin,
        get_runtime=lambda _request: runtime,
    )
    app.add_middleware(ApiBodyGuardMiddleware, sessions=app.state.sessions)
    monkeypatch.setattr(api, "decode_snapshot", Mock(return_value=recovered_data()))
    monkeypatch.setattr(archive.MongoStore, "_indexes", Mock())
    with TestClient(app) as client:
        yield client, app, runtime, backend


def sign_in(client, app):
    token = app.state.sessions.create()
    client.cookies.set(SESSION_COOKIE, token)
    return token


def headers():
    return {
        "Content-Type": "application/octet-stream",
        "X-TGDrive-Recovery-Key": "a" * 64,
        "X-TGDrive-Confirm": "RESTORE",
    }


def test_restore_authenticates_before_reading_oversized_body(owner_api):
    client, _app, runtime, _backend = owner_api
    response = client.post(
        api.RESTORE_PATH,
        content=b"unused",
        headers=headers() | {"Content-Length": str(archive.MAX_EXPANDED + 1)},
    )
    assert response.status_code == 401
    runtime.stop.assert_not_awaited()


def test_restore_rejects_oversized_archive_before_stopping_runtime(owner_api):
    client, app, runtime, _backend = owner_api
    sign_in(client, app)
    response = client.post(
        api.RESTORE_PATH,
        content=b"unused",
        headers=headers() | {"Content-Length": str(archive.MAX_EXPANDED + 1)},
    )
    assert response.status_code == 413
    runtime.stop.assert_not_awaited()


def test_invalid_archive_preserves_running_drive(owner_api, monkeypatch):
    client, app, runtime, backend = owner_api
    sign_in(client, app)
    monkeypatch.setattr(
        api, "decode_snapshot", Mock(side_effect=BackupError("Wrong key"))
    )
    response = client.post(api.RESTORE_PATH, content=b"encrypted", headers=headers())
    assert response.status_code == 422
    assert response.json()["code"] == "ARCHIVE_INVALID"
    runtime.stop.assert_not_awaited()
    assert backend.db.catalog.rows[0]["drive_id"] == "new-drive"


def test_unified_recovery_file_requires_verified_recovery_flow(owner_api):
    from tgdrive.recovery.recovery_file import MAGIC

    client, app, runtime, backend = owner_api
    sign_in(client, app)
    response = client.post(
        api.RESTORE_PATH, content=MAGIC + b"ciphertext", headers=headers()
    )
    assert response.status_code == 422
    assert response.json()["code"] == "RECOVERY_FILE_REQUIRES_UNIFIED_RESTORE"
    assert "Restore backup" in response.json()["detail"]
    api.decode_snapshot.assert_not_called()
    runtime.stop.assert_not_awaited()
    assert backend.db.catalog.rows[0]["drive_id"] == "new-drive"


def test_owner_restore_imports_archive_and_requires_restart(owner_api):
    client, app, runtime, backend = owner_api
    token = sign_in(client, app)
    response = client.post(api.RESTORE_PATH, content=b"encrypted", headers=headers())
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert runtime.recovery_only is True
    runtime.stop.assert_awaited_once()
    assert backend.db.catalog.rows[0]["drive_id"] == "recovered-drive"
    assert backend.db.sessions.rows == []
    with pytest.raises(InvalidSession):
        app.state.sessions.verify(token)


def test_failed_commit_reports_uncertain_result_and_restart(owner_api):
    client, app, _runtime, backend = owner_api
    sign_in(client, app)
    backend.db.nodes.fail_insert = True
    response = client.post(api.RESTORE_PATH, content=b"encrypted", headers=headers())
    assert response.status_code == 503
    assert response.json()["code"] == "DATABASE_RESTORE_UNCONFIRMED"
    assert response.json()["restart_required"] is True
    assert backend.db.catalog.rows[0]["drive_id"] == "new-drive"
    backend.release.assert_called_once()


def test_failed_shutdown_never_starts_database_import(owner_api):
    client, app, runtime, backend = owner_api
    sign_in(client, app)
    runtime.stop.return_value = [RuntimeError("Synthetic worker drain failure")]
    response = client.post(api.RESTORE_PATH, content=b"encrypted", headers=headers())
    assert response.status_code == 503
    assert response.json()["restart_required"] is True
    backend.acquire.assert_not_called()
    assert backend.db.catalog.rows[0]["drive_id"] == "new-drive"


async def test_repeated_disconnect_cancellation_drains_file_or_database_worker():
    entered, release = Event(), Event()

    def write():
        entered.set()
        assert release.wait(3)

    operation = asyncio.create_task(api._finish(write))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    finally:
        release.set()
