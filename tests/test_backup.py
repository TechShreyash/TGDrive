from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tgdrive.backup import BackupError, TelegramBackupService
from tgdrive.client_pool import ClientPool
from tgdrive.config import Settings
from tgdrive.store import DriveStore


class FakeBackupClient:
    def __init__(self, content: bytes = b"", file_name: str = "drive.json") -> None:
        self.content = content
        self.file_name = file_name
        self.name = "backup-test"
        self.is_connected = False
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()
        self.wait_during_send = False
        self.lose_next_response = False
        self.sent: dict[int, tuple[int, bytes]] = {}
        self.requests: list[int] = []
        self.download_calls = 0
        self.pinned = None
        self.captions = {}

    async def start(self) -> None:
        self.is_connected = True

    async def stop(self) -> None:
        self.is_connected = False

    async def get_messages(self, _channel: int, *, message_ids: int):
        assert message_ids > 0
        content = next(
            (body for identity, body in self.sent.values() if identity == message_ids),
            self.content,
        )
        return SimpleNamespace(
            id=message_ids,
            text=self.captions.get(message_ids, ""),
            _content=content,
            document=SimpleNamespace(file_name=self.file_name, file_size=len(content)),
        )

    async def get_pinned_message_id(self, _channel: int):
        return self.pinned

    async def pin_message(self, _channel: int, message_id: int, *, silent=True):
        self.pinned = message_id

    async def download_media(self, message: object, *, file_name: str) -> str:
        self.download_calls += 1
        Path(file_name).write_bytes(getattr(message, "_content", self.content))
        return file_name

    async def send_document(self, _channel: int, path: str, **options: object):
        random_id = options["random_id"]
        self.requests.append(random_id)
        content = Path(path).read_bytes()
        if random_id not in self.sent:
            self.sent[random_id] = (100 + len(self.sent), content)
        else:
            assert self.sent[random_id][1] == content
        self.captions[self.sent[random_id][0]] = options.get("caption", "")
        self.send_started.set()
        if self.wait_during_send:
            await self.release_send.wait()
        if self.lose_next_response:
            self.lose_next_response = False
            raise TimeoutError("response lost after acceptance")
        return SimpleNamespace(id=self.sent[random_id][0])


def settings(tmp_path: Path, *, initialize: bool = False) -> Settings:
    return Settings(
        _env_file=None,
        API_ID=12345,
        API_HASH="a" * 32,
        BOT_TOKENS="123456:" + "x" * 30,
        STORAGE_CHANNEL=-1001234567890,
        DATABASE_BACKUP_MSG_ID=42,
        ADMIN_PASSWORD="correct horse battery staple",
        SESSION_SECRET="s" * 48,
        DATA_DIR=tmp_path / "data",
        CACHE_DIR=tmp_path / "cache",
        INITIALIZE_EMPTY_DRIVE=initialize,
        COOKIE_SECURE=False,
    )


def store_bytes() -> bytes:
    store = DriveStore.create()
    store.create_folder("/", "Remote folder")
    return store.backup_snapshot()[1]


async def service_for(
    tmp_path: Path, *, initialize: bool = False, content: bytes | None = None
):
    client = FakeBackupClient(store_bytes() if content is None else content)
    pool = ClientPool([client])
    await pool.start()
    service = TelegramBackupService(settings(tmp_path, initialize=initialize), pool)
    if initialize:
        DriveStore.create(service.store_path).close()
    return service, pool, client


async def test_explicit_restore_creates_sqlite_and_never_overwrites(
    tmp_path: Path,
) -> None:
    service, pool, _ = await service_for(tmp_path)
    restored = await service.restore_from_telegram(42)
    assert next(iter(restored.list_directory("/").values()))["name"] == "Remote folder"
    assert service.store_path.name == "drive.sqlite3"
    assert service.store_path.read_bytes().startswith(b"SQLite format 3")
    with pytest.raises(FileExistsError):
        await service.restore_from_telegram(42)
    await pool.stop()


async def test_startup_requires_explicit_initialization_or_recovery(
    tmp_path: Path,
) -> None:
    service, pool, client = await service_for(tmp_path)
    with pytest.raises(BackupError, match="recover"):
        await service.restore()
    assert client.download_calls == 0
    with pytest.raises(BackupError, match="recover"):
        await TelegramBackupService(settings(tmp_path, initialize=True), pool).restore()
    initialized = DriveStore.create(service.store_path)
    assert initialized.list_directory("/") == {}
    assert initialized.dirty
    initialized.close()
    assert client.download_calls == 0
    await pool.stop()


async def test_existing_local_database_is_authoritative_without_telegram(
    tmp_path: Path,
) -> None:
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    store.create_folder("/", "Local")
    local_revision = store.revision
    client.content = b"invalid remote backup"
    restored = await service.restore()
    assert restored.revision == local_revision
    assert next(iter(restored.list_directory("/").values()))["name"] == "Local"
    assert client.download_calls == 0
    await pool.stop()


async def test_corrupt_local_database_is_not_reinitialized(tmp_path: Path) -> None:
    service, pool, client = await service_for(tmp_path, initialize=True)
    service.store_path.parent.mkdir(parents=True, exist_ok=True)
    service.store_path.write_bytes(b"preserve corrupt operator data")
    with pytest.raises(BackupError, match="preserve"):
        await service.restore()
    assert service.store_path.read_bytes() == b"preserve corrupt operator data"
    assert client.download_calls == 0
    await pool.stop()


async def test_mutation_during_checkpoint_remains_dirty(tmp_path: Path) -> None:
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    store.create_folder("/", "Before backup")
    client.wait_during_send = True
    task = asyncio.create_task(service.backup(store))
    await client.send_started.wait()
    store.create_folder("/", "Raced backup")
    client.release_send.set()
    await task
    assert store.dirty
    assert len(store.list_directory("/")) == 2
    assert len(client.sent) == 1
    assert service.history()[0]["revision"] < store.revision
    await pool.stop()


async def test_lost_checkpoint_response_reuses_durable_id_after_restart(
    tmp_path: Path,
) -> None:
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    client.lose_next_response = True
    with pytest.raises(BackupError, match="TimeoutError"):
        await service.backup(store)
    pending = service.history()[0]
    assert pending["status"] == "pending"
    assert store.dirty
    restarted = TelegramBackupService(settings(tmp_path), pool)
    recovered = DriveStore.load(service.store_path)
    assert await restarted.backup(recovered)
    assert client.requests == [pending["random_id"], pending["random_id"]]
    assert len(client.sent) == 1
    assert restarted.history()[0]["message_id"] == 100
    assert not recovered.dirty
    assert (tmp_path / "data" / "backups" / f"{pending['id']}.json").exists()
    await pool.stop()


async def test_checkpoints_append_history_and_restore_clean(tmp_path: Path) -> None:
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    assert await service.backup(store)
    first = service.history()[0]
    first_content = client.sent[first["random_id"]][1]
    assert not await service.backup(store)
    store.create_folder("/", "New")
    assert await service.backup(store)
    assert len(service.history()) == len(client.sent) == 2
    assert client.sent[first["random_id"]][1] == first_content
    client.content = first_content
    restored = await service.restore_from_telegram(
        first["message_id"], destination=tmp_path / "old.sqlite3"
    )
    assert restored.list_directory("/") == {}
    assert not restored.dirty
    assert len(store.list_directory("/")) == 1
    await pool.stop()


async def test_restore_rejects_pickle_invalid_json_and_duplicate_keys(
    tmp_path: Path,
) -> None:
    service, pool, client = await service_for(tmp_path)
    for name, content in [
        ("drive.data", b"pickle"),
        ("drive.json", b"invalid"),
        ("drive.json", b'{"revision":1,"revision":2}'),
    ]:
        client.file_name, client.content = name, content
        with pytest.raises(BackupError):
            await service.restore_from_telegram(42)
        assert not service.store_path.exists()
    await pool.stop()


async def test_offline_restore_preserves_deletion_and_share_metadata(
    tmp_path: Path,
) -> None:
    service, pool, _ = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    file = store.add_file("/", "delete.bin", 12, 1, operation_id="upload-operation")
    store.set_trashed("/" + file["id"], True)
    store.begin_deletion("/" + file["id"], channel_id=-1001)
    source = tmp_path / "checkpoint.json"
    source.write_bytes(store.backup_snapshot()[1])
    restored = service.restore_from_file(
        source, destination=tmp_path / "recovery.sqlite3"
    )
    assert restored.pending_deletions() == store.pending_deletions()
    assert restored.export()["operations"] == store.export()["operations"]
    payload = json.loads(source.read_bytes())
    payload["deletions"][0]["pending_message_ids"] = [999]
    source.write_text(json.dumps(payload))
    with pytest.raises(Exception, match="confirmation"):
        service.restore_from_file(source, destination=tmp_path / "invalid.sqlite3")
    assert not (tmp_path / "invalid.sqlite3").exists()
    await pool.stop()


async def test_stop_flushes_dirty_store(tmp_path: Path) -> None:
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    service.start(store)
    await service.stop(store)
    assert not store.dirty
    assert len(client.sent) == 1
    await pool.stop()


async def test_failed_forced_checkpoint_retries_even_when_store_is_clean(
    tmp_path: Path,
) -> None:
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    store.bind_storage_channel(service._settings.storage_channel)
    store.mark_backed_up(store.revision)
    client.lose_next_response = True
    with pytest.raises(BackupError):
        await service.backup(store, force=True)
    assert not store.dirty
    assert await service.backup(store)
    assert len(client.sent) == 1
    assert service.history()[0]["status"] == "completed"
    await pool.stop()


@pytest.mark.parametrize("origin", ["file", "telegram"])
async def test_explicit_restore_revokes_every_share_before_publication(
    tmp_path, origin
):
    source = DriveStore.create()
    folder = source.create_folder("/", "Shared")
    folder_path = "/" + folder["id"]
    file = source.add_file(folder_path, "secret.bin", 9, 4)
    file_path = folder_path + "/" + file["id"]
    tokens = [
        source.create_share("/", expires_at=None),
        source.create_share(folder_path),
        source.create_share(file_path),
    ]
    snapshot = source.backup_snapshot()[1]
    service, pool, _ = await service_for(tmp_path, content=snapshot)
    destination = tmp_path / "restored.sqlite3"
    if origin == "telegram":
        restored = await service.restore_from_telegram(42, destination=destination)
    else:
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_bytes(snapshot)
        restored = service.restore_from_file(checkpoint, destination=destination)
    assert restored.list_shares() == []
    assert all(not restored.authorize_file(file_path, token) for token in tokens)
    assert restored.get_file(file_path)["message_id"] == 9
    assert restored.revision == source.revision + 1
    assert restored.dirty
    opened = DriveStore.load(destination)
    assert opened.list_shares() == []
    assert all(not opened.authorize_file(file_path, token) for token in tokens)
    assert all(source.authorize_file(file_path, token) for token in tokens)
    await pool.stop()


async def test_local_checkpoint_retention_keeps_recent_daily_and_pending_files(
    tmp_path,
):
    service, pool, client = await service_for(tmp_path)
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    dates = [now - timedelta(minutes=index) for index in range(150)]
    for day in range(1, 30):
        dates.extend([now - timedelta(days=day), now - timedelta(days=day, hours=1)])
    dates.extend(now - timedelta(days=day) for day in range(31, 36))
    checkpoints = {
        f"checkpoint{index:012d}": created for index, created in enumerate(dates)
    }
    with service._history() as connection:
        for index, (identity, created) in enumerate(checkpoints.items()):
            connection.execute(
                "INSERT INTO checkpoints(id,revision,sha256,random_id,channel_id,"
                "created_at,message_id,status) VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity,
                    index + 1,
                    "a" * 64,
                    index + 1,
                    -100123,
                    created.isoformat().replace("+00:00", "Z"),
                    index + 100,
                    "completed",
                ),
            )
            (service._history_dir / f"{identity}.json").write_bytes(
                b"immutable snapshot"
            )
        pending_id = "checkpoint999999999999"
        connection.execute(
            "INSERT INTO checkpoints(id,revision,sha256,random_id,channel_id,"
            "created_at,message_id,status) VALUES(?,?,?,?,?,?,?,?)",
            (
                pending_id,
                999,
                "b" * 64,
                99999,
                -100123,
                (now - timedelta(days=50)).isoformat(),
                None,
                "pending",
            ),
        )
        pending_path = service._history_dir / f"{pending_id}.json"
        pending_path.write_bytes(b"must survive for retry")
        connection.commit()
    latest = sorted(checkpoints, key=checkpoints.get, reverse=True)[:96]
    daily = set()
    for day in range(30):
        target_day = (now - timedelta(days=day)).date()
        candidates = [
            identity
            for identity, created in checkpoints.items()
            if created.date() == target_day
        ]
        daily.add(max(candidates, key=checkpoints.get))
    retained = set(latest) | daily
    assert service.prune_local_history(now=now) == len(checkpoints) - len(retained)
    assert {path.stem for path in service._history_dir.glob("*.json")} == retained | {
        pending_id
    }
    assert len(retained) == 125
    assert pending_path.read_bytes() == b"must survive for retry"
    assert len(service.history()) == len(checkpoints) + 1
    assert client.requests == []
    assert service.prune_local_history(now=now) == 0
    await pool.stop()


async def test_history_connection_closes_on_initialization_or_query_failure(
    tmp_path, monkeypatch
):
    import tgdrive.backup as module

    service, pool, _ = await service_for(tmp_path)
    with service._history() as connection:
        connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with (
        pytest.raises(RuntimeError, match="query failed"),
        service._history() as failed,
    ):
        raise RuntimeError("query failed")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")
    original = module.sqlite3.connect
    observed = []

    class InitializationFailure(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA synchronous=FULL":
                raise sqlite3.OperationalError("initialization failed")
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        result = original(*args, **kwargs, factory=InitializationFailure)
        observed.append(result)
        return result

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    with (
        pytest.raises(sqlite3.OperationalError, match="initialization failed"),
        service._history(),
    ):
        pytest.fail("Connection initialization must fail before yielding")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observed[0].execute("SELECT 1")
    await pool.stop()


async def test_uploaded_checkpoint_contains_only_metadata_and_hashed_share_credentials(
    tmp_path,
):
    service, pool, client = await service_for(tmp_path, initialize=True)
    store = await service.restore()
    item = store.add_file(
        "/", "private-name.bin", 79, 3, operation_id="upload-identity"
    )
    token = store.create_share("/" + item["id"])
    await service.backup(store)
    content = next(iter(client.sent.values()))[1]
    payload = json.loads(content)
    assert set(payload) == {
        "schema_version",
        "drive_id",
        "storage_channel_id",
        "revision",
        "backed_up_revision",
        "root",
        "shares",
        "operations",
        "deletions",
    }
    assert set(payload["shares"][0]) == {
        "id",
        "node_id",
        "token_hash",
        "created_at",
        "expires_at",
    }
    assert len(payload["shares"][0]["token_hash"]) == 64
    assert token.encode() not in content
    configured = service._settings
    for secret in (
        configured.api_hash.get_secret_value(),
        configured.bot_tokens_csv.get_secret_value(),
        configured.admin_password.get_secret_value(),
        configured.session_secret.get_secret_value(),
    ):
        assert secret.encode() not in content
    assert b"session_cookie" not in content and b"password_hash" not in content
    assert payload["operations"] == [
        {"operation_id": "upload-identity", "node_id": item["id"]}
    ]
    assert payload["root"]["children"][item["id"]]["name"] == "private-name.bin"
    store.close()
    await pool.stop()
