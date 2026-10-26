from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo.errors import ConnectionFailure
from telethon import types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

import tgdrive.settings.management as management_module
from tests.support.app_fixtures import FakeRuntime, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.core.config import Settings
from tgdrive.settings.managed_settings import (
    ManagedClient,
    ManagedDocument,
    SettingsProblem,
    read_document,
    save_document,
)
from tgdrive.settings.management import (
    ConnectionManager,
    SettingsChange,
    verify_connection,
)
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.telegram.client_identity import IdentityCache
from tgdrive.transfers.tasks import TransferStatus
from tgdrive.transfers.uploads import ResumableUploads
from tgdrive.web.app import create_app

TOKEN = "123456:" + "x" * 30
SECOND_TOKEN = "654321:" + "y" * 30


@pytest.fixture
def managed(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    settings.resolved_cache_dir.mkdir(parents=True)
    backend = register_test_backend(monkeypatch, settings)
    store = MongoStore.create(backend)
    runtime = FakeRuntime(store)
    runtime.mongo = backend

    async def verifier(_api_id, _api_hash, row, _channel):
        return (
            "storage-bot-" + row.credential.get_secret_value().split(":", 1)[0]
            if row.kind == "bot"
            else "premium-user-987654"
        )

    manager = ConnectionManager(settings, runtime, verifier=verifier)
    runtime.connection_manager = manager
    yield manager, runtime, settings, store
    runtime.transfers.registry.close()
    store.close()


@pytest.mark.asyncio
async def test_atomic_private_save_cas_and_secret_safe_snapshot(managed):
    manager, _runtime, settings, _ = managed
    snapshot = await manager.snapshot()
    client = snapshot["clients"][0]
    result = await manager.save(
        SettingsChange.model_validate(
            {
                "expected_revision": 0,
                "api_id": 99999,
                "api_hash": "b" * 32,
                "clients": [
                    {
                        "id": client["id"],
                        "label": "Main bot",
                        "kind": "bot",
                        "enabled": True,
                    }
                ],
            }
        )
    )
    assert result["revision"] == 1
    assert result["applied_revision"] == 0
    assert result["restart_required"] and result["admission_paused"]
    assert result["api_id"] == 99999
    assert settings.api_id == 12345  # Running configuration never changes.
    assert result["active_clients"][0]["label"] == "Storage bot 1"
    text = json.dumps(result)
    for secret in (TOKEN, "b" * 32, settings.session_secret.get_secret_value()):
        assert secret not in text
    assert _runtime.mongo.db.settings.find_one({"_id": "telegram"})["revision"] == 1
    document = read_document(settings.resolved_data_dir)
    assert document.api_hash.get_secret_value() == "b" * 32
    assert document.clients[0].credential.get_secret_value() == TOKEN
    with pytest.raises(SettingsProblem) as conflict:
        await manager.save(SettingsChange(expected_revision=0, api_id=1000))
    assert conflict.value.code == "SETTINGS_CONFLICT"
    assert read_document(settings.resolved_data_dir).revision == 1
    with pytest.raises(SettingsProblem) as pending:
        manager.check_upload_allowed()
    assert pending.value.code == "SETTINGS_RESTART_REQUIRED"


@pytest.mark.asyncio
async def test_failed_verification_preserves_previous_configuration(managed):
    manager, _, settings, _ = managed
    manager.verifier = AsyncMock(side_effect=RuntimeError(TOKEN))
    with pytest.raises(SettingsProblem) as captured:
        await manager.save(SettingsChange(expected_revision=0, api_id=12))
    assert captured.value.code == "SETTINGS_PREFLIGHT_FAILED"
    assert TOKEN not in str(captured.value)
    assert read_document(settings.resolved_data_dir) is None


@pytest.mark.asyncio
async def test_no_enabled_bot_no_duplicate_accounts_no_unknown_identity(managed):
    manager, _, settings, _ = managed
    row = (await manager.snapshot())["clients"][0]
    for clients in (
        [{"id": row["id"], "label": "Bot", "kind": "bot", "enabled": False}],
        [
            {"label": "One", "kind": "bot", "credential": TOKEN},
            {"label": "Two", "kind": "bot", "credential": TOKEN},
        ],
        [{"id": "not-saved", "label": "One", "kind": "bot", "credential": TOKEN}],
    ):
        with pytest.raises(SettingsProblem):
            await manager.save(
                SettingsChange.model_validate(
                    {"expected_revision": 0, "clients": clients}
                )
            )
    assert read_document(settings.resolved_data_dir) is None


@pytest.mark.asyncio
async def test_premium_private_import_preserves_existing_client_ids(managed):
    manager, _, settings, _ = managed
    initial = (await manager.snapshot())["clients"][0]["id"]
    result = await manager.add_authenticated_session(0, "Premium", "synthetic-session")
    assert [row["kind"] for row in result["clients"]] == ["bot", "user"]
    assert result["clients"][0]["id"] == initial
    assert "synthetic-session" not in json.dumps(result)
    assert (
        read_document(settings.resolved_data_dir)
        .clients[1]
        .credential.get_secret_value()
        == "synthetic-session"
    )
    api_id, api_hash, revision = manager.login_api()
    assert api_id == 12345 and api_hash.get_secret_value() == "a" * 32 and revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("same_account", [True, False])
async def test_pending_upload_allows_only_same_account_credential_repair(
    managed, same_account
):
    manager, runtime, settings, _ = managed
    row = (await manager.snapshot())["clients"][0]
    record = await runtime.transfers.registry.create("failed")
    record.status = TransferStatus.ERROR
    record.context.update(sender="storage-bot-123456", random_id=123, send_started=True)
    token = "123456:" + "z" * 30 if same_account else SECOND_TOKEN
    change = SettingsChange.model_validate(
        {
            "expected_revision": 0,
            "clients": [
                {
                    "id": row["id"],
                    "label": row["label"],
                    "kind": "bot",
                    "credential": token,
                }
            ],
        }
    )
    if same_account:
        result = await manager.save(change)
        assert result["restart_required"]
        assert token not in json.dumps(result)
        saved = read_document(settings.resolved_data_dir).clients[0]
        assert saved.runtime_name == record.context["sender"]
        assert saved.credential.get_secret_value() == token
    else:
        with pytest.raises(SettingsProblem, match="same Telegram account"):
            await manager.save(change)
        assert read_document(settings.resolved_data_dir) is None
    assert record.context["random_id"] == 123
    assert record.context["send_started"]


@pytest.mark.asyncio
async def test_refresh_restores_verified_live_channel_access(managed):
    from telethon.errors import ChannelPrivateError

    from tests.telegram.test_client_pool import FakeClient
    from tgdrive.telegram.client_pool import ClientPool

    manager, runtime, _, _ = managed
    client = FakeClient("storage-bot-123456")
    runtime.pool = ClientPool([client])
    await runtime.pool.start()
    try:
        await runtime.pool.report_failure(client, ChannelPrivateError(request=None))
        before = await manager.snapshot()
        assert before["clients"][0]["error_code"] == "TELEGRAM_CHANNEL_ACCESS"
        await manager.refresh_connections()
        await asyncio.gather(*manager._check_tasks)
        after = await manager.snapshot()
        assert after["clients"][0]["healthy"]
        assert after["clients"][0]["error"] is None
    finally:
        await manager.close()
        await runtime.pool.stop()


@pytest.mark.asyncio
async def test_busy_rechecked_after_network_verification(managed):
    manager, runtime, settings, _ = managed
    entered = asyncio.Event()
    resume = asyncio.Event()
    original = manager.verifier

    async def verifier(*arguments):
        entered.set()
        await resume.wait()
        return await original(*arguments)

    manager.verifier = verifier
    task = asyncio.create_task(
        manager.save(SettingsChange(expected_revision=0, api_id=123))
    )
    await entered.wait()
    await runtime.transfers.registry.create("uploading")
    resume.set()
    with pytest.raises(SettingsProblem) as captured:
        await task
    assert captured.value.code == "SETTINGS_BUSY"
    assert read_document(settings.resolved_data_dir) is None


@pytest.mark.asyncio
async def test_failed_ambiguous_job_also_blocks_removing_its_sender(managed):
    manager, runtime, _, _ = managed
    job = await runtime.transfers.registry.create("uploading")
    job.status = TransferStatus.ERROR
    job.context.update(sender="storage-bot-123456", send_started=True)
    with pytest.raises(SettingsProblem) as captured:
        await manager.save(SettingsChange(expected_revision=0, api_id=1))
    assert captured.value.code == "SETTINGS_BUSY"


@pytest.mark.asyncio
async def test_catalog_stats_include_inherited_trash_and_live_client_health(managed):
    manager, runtime, _, store = managed
    folder = store.create_folder("/", "Folder")
    store.add_file("/", "visible.bin", 101, 7)
    folder_path = "/" + folder["id"]
    store.add_file(folder_path, "trashed.bin", 102, 13)
    store.set_trashed(folder_path, True)
    runtime.pool = SimpleNamespace(
        status=AsyncMock(
            return_value=(
                SimpleNamespace(
                    name="storage-bot-123456",
                    started=True,
                    connected=True,
                    healthy=True,
                    active_leases=2,
                    last_error_type=None,
                ),
            )
        )
    )
    snapshot = await manager.snapshot()
    assert snapshot["stats"] | {} == {
        **snapshot["stats"],
        "files": 1,
        "folders": 0,
        "total_bytes": 7,
        "trashed_files": 1,
        "trashed_bytes": 13,
    }
    assert snapshot["clients"][0]["connected"] is True
    assert snapshot["clients"][0]["active_leases"] == 2
    assert snapshot["clients"][0]["premium"] is False


def document(revision=1):
    return ManagedDocument(
        revision=revision,
        api_id=333,
        api_hash=SecretStr("b" * 32),
        clients=[
            ManagedClient(
                id="saved-bot",
                label="Saved bot",
                kind="bot",
                credential=SecretStr(SECOND_TOKEN),
            )
        ],
    )


def test_restart_loads_managed_overrides_before_invalid_environment(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "private"
    register_test_backend(monkeypatch, data)
    save_document(data, document(), 0)
    monkeypatch.setenv("MONGODB_URI", "mongodb://synthetic.invalid")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("API_ID", "invalid-old-env")
    monkeypatch.setenv("API_HASH", "invalid-old-hash")
    monkeypatch.setenv("BOT_TOKENS", "invalid-old-token")
    monkeypatch.setenv("STORAGE_CHANNEL", "-1001234567890")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)
    settings = Settings(_env_file=None)
    assert settings.api_id == 333 and settings.bot_tokens == (SECOND_TOKEN,)
    assert settings.managed_revision == 1
    assert settings.managed_client_ids == ("saved-bot",)
    assert settings.managed_client_labels == ("Saved bot",)


@pytest.mark.parametrize("saved_main_state", [None, "disabled", "removed", "rotated"])
def test_main_token_seeds_only_before_connections_are_managed(
    tmp_path, monkeypatch, saved_main_state
):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "private"
    register_test_backend(monkeypatch, data)
    expected_token = TOKEN
    if saved_main_state is not None:
        saved = document()
        expected_token = SECOND_TOKEN
        if saved_main_state == "disabled":
            saved.clients.append(
                ManagedClient(
                    id="main-bot",
                    label="Main bot",
                    kind="bot",
                    enabled=False,
                    credential=SecretStr(TOKEN),
                )
            )
        elif saved_main_state == "rotated":
            expected_token = "123456:" + "z" * 30
            saved.clients[0].id = "main-bot"
            saved.clients[0].credential = SecretStr(expected_token)
        save_document(data, saved, 0)
    monkeypatch.delenv("BOT_TOKENS", raising=False)
    for name, value in {
        "MONGODB_URI": "mongodb://synthetic.invalid",
        "DATA_DIR": str(data),
        "API_ID": "12345",
        "API_HASH": "a" * 32,
        "MAIN_BOT_TOKEN": TOKEN,
        "TELEGRAM_ADMIN_IDS": "123",
        "STORAGE_CHANNEL": "-1001234567890",
        "ADMIN_PASSWORD": "correct horse battery staple",
        "SESSION_SECRET": "s" * 48,
    }.items():
        monkeypatch.setenv(name, value)

    applied = Settings(_env_file=None)
    assert applied.bot_tokens == (expected_token,)
    assert applied.bot_tokens_csv.get_secret_value() == expected_token
    assert applied.main_bot_token_value == TOKEN
    assert applied.managed_revision == (0 if saved_main_state is None else 1)


@pytest.mark.parametrize("damage", ["document", "unavailable"])
def test_invalid_or_unavailable_mongo_settings_never_fall_back_to_environment(
    tmp_path, monkeypatch, damage
):
    data = tmp_path / "data"
    backend = register_test_backend(monkeypatch, data)
    save_document(data, document(), 0)
    backend._settings_cached = False
    collection = backend.db.settings
    if damage == "document":
        collection.replace_one(
            {"_id": "telegram"},
            {
                "_id": "telegram",
                "revision": 1,
                "document": {"api_hash": "synthetic-secret"},
            },
        )
    else:

        def unavailable(_query):
            raise ConnectionFailure("synthetic-secret")

        monkeypatch.setattr(collection, "find_one", unavailable)
    expected = SettingsProblem if damage == "document" else DatabaseUnavailable
    with pytest.raises(expected) as captured:
        read_document(data)
    assert "synthetic-secret" not in str(captured.value)
    assert collection.rows  # Rejected configuration is preserved.


def test_store_cas_atomic_write_and_no_catalog_secret_export(managed):
    _, _, settings, store = managed
    save_document(settings.resolved_data_dir, document(), 0)
    with pytest.raises(SettingsProblem):
        save_document(settings.resolved_data_dir, document(2), 0)
    assert read_document(settings.resolved_data_dir).revision == 1
    assert SECOND_TOKEN not in json.dumps(store.export())


def test_owner_only_api_omits_secret_inputs_and_preserves_unsaved_on_invalid(managed):
    manager, runtime, settings, _ = managed
    app = create_app(settings, runtime=runtime)
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/v1/settings").status_code == 401
        assert (
            client.patch("/api/v1/settings", json={"expected_revision": 0}).status_code
            == 401
        )
        assert (
            client.post(
                "/api/checkPassword", json={"pass": "correct horse battery staple"}
            ).status_code
            == 200
        )
        result = client.get("/api/v1/settings")
        assert result.status_code == 200 and TOKEN not in result.text
        bad = client.patch(
            "/api/v1/settings",
            json={
                "expected_revision": 0,
                "api_hash": "wrong-secret",
                "storage_channel": -1009,
            },
        )
        assert bad.status_code == 422 and "wrong-secret" not in bad.text
        result = client.patch(
            "/api/v1/settings", json={"expected_revision": 0, "api_id": 67890}
        )
        assert result.status_code == 200 and result.json()["restart_required"]
        assert TOKEN not in result.text
        assert manager.restart_required


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unprotected", "conflict", "receipt"])
async def test_pending_work_and_recovery_conflict_block_settings(managed, case):
    manager, runtime, settings, store = managed
    protection = {
        "state": "protected",
        "metadata_revision": store.revision,
        "protected_revision": store.revision,
    }
    runtime.backup = SimpleNamespace(protection_status=lambda _: protection)
    uploads = None
    if case == "unprotected":
        protection.update(state="pending", protected_revision=0)
    elif case == "conflict":
        protection.update(state="error", recovery_required=True)
    else:
        uploads = ResumableUploads(settings, runtime.transfers)
        uploads.create("pending.bin", "/", 3)
    try:
        with pytest.raises(SettingsProblem) as captured:
            await manager.save(SettingsChange(expected_revision=0, api_id=1))
        assert captured.value.code == "SETTINGS_BUSY"
        assert read_document(settings.resolved_data_dir) is None
    finally:
        if uploads is not None:
            uploads.close()


@pytest.mark.asyncio
async def test_clean_confirmed_watermark_can_repair_broken_old_credentials(managed):
    manager, runtime, _, store = managed
    runtime.backup = SimpleNamespace(
        protection_status=lambda _: {
            "state": "error",
            "metadata_revision": store.revision,
            "protected_revision": store.revision,
            "error": "Old connection unavailable",
        }
    )
    result = await manager.save(SettingsChange(expected_revision=0, api_id=123))
    assert result["restart_required"]
    assert result["recovery"]["state"] == "error"  # No false protection claim.


@pytest.mark.asyncio
async def test_replaced_account_is_not_shown_connected_before_restart(managed):
    manager, runtime, _, _ = managed
    runtime.pool = SimpleNamespace(
        status=AsyncMock(
            return_value=(
                SimpleNamespace(
                    name="storage-bot-123456",
                    started=True,
                    connected=True,
                    healthy=True,
                    active_leases=0,
                    last_error_type=None,
                ),
            )
        )
    )
    row = (await manager.snapshot())["clients"][0]
    result = await manager.save(
        SettingsChange.model_validate(
            {
                "expected_revision": 0,
                "clients": [
                    {
                        "id": row["id"],
                        "label": "Replacement",
                        "kind": "bot",
                        "credential": SECOND_TOKEN,
                    }
                ],
            }
        )
    )
    assert result["clients"][0]["connected"] is None
    assert not result["clients"][0]["healthy"]
    assert result["active_clients"][0]["connected"] is True


class NativeProbe:
    def __init__(
        self, *, bot=True, premium=False, authorized=True, rights=True, identity=123456
    ):
        self.account = SimpleNamespace(id=identity, bot=bot, premium=premium)
        self.authorized = authorized
        self.rights = rights
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.sign_in = AsyncMock()
        self.get_me = AsyncMock(return_value=self.account)
        self.is_user_authorized = AsyncMock(return_value=authorized)
        self.get_input_entity = AsyncMock(
            return_value=types.InputPeerChannel(1234567890, 123)
        )
        self.calls = []

    async def __call__(self, request):
        self.calls.append(request)
        return SimpleNamespace(
            chats=[
                types.Channel(
                    id=1234567890,
                    title="Private test",
                    photo=types.ChatPhotoEmpty(),
                    date=datetime.now(UTC),
                    broadcast=True,
                    admin_rights=types.ChatAdminRights(
                        post_messages=self.rights,
                        edit_messages=self.rights,
                        delete_messages=self.rights,
                    ),
                )
            ]
        )


def fake_session():
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(data=b"x" * 256)
    return session.save()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bot", "user"])
async def test_real_preflight_contract_is_memory_only_checks_getme_and_channel(
    monkeypatch, kind, tmp_path
):
    native = NativeProbe(bot=kind == "bot", premium=kind == "user")
    native.account.first_name = "Telegram Name"
    native.account.username = "telegram_name"
    constructors = []

    def constructor(session, api_id, api_hash, **options):
        constructors.append((session, api_id, api_hash, options))
        return native

    monkeypatch.setattr(management_module, "TelegramClient", constructor)
    row = ManagedClient(
        id="test-account",
        label="Test",
        kind=kind,
        credential=SecretStr(TOKEN if kind == "bot" else fake_session()),
    )
    register_test_backend(monkeypatch, tmp_path)
    cache = IdentityCache(tmp_path)
    result = await verify_connection(
        12345, "a" * 32, row, -1001234567890, identity_cache=cache
    )
    assert result == ("storage-bot-123456" if kind == "bot" else "premium-user-123456")
    assert cache.read(result) == {
        "display_name": "Telegram Name",
        "username": "telegram_name",
    }
    assert isinstance(constructors[0][0], StringSession)
    assert constructors[0][3]["receive_updates"] is False
    assert constructors[0][3]["flood_sleep_threshold"] == 0
    assert constructors[0][3]["base_logger"].propagate is False
    native.disconnect.assert_awaited_once()
    native.get_me.assert_awaited_once()
    assert (
        len(native.calls) == 1
        and native.calls[0].__class__.__name__ == "GetFullChannelRequest"
    )
    if kind == "bot":
        native.sign_in.assert_awaited_once_with(bot_token=TOKEN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["nonpremium", "expired", "wrong-bot", "rights", "network"]
)
async def test_preflight_failure_never_echoes_secret_and_always_disconnects(
    monkeypatch, case
):
    user = case in {"nonpremium", "expired"}
    native = NativeProbe(
        bot=not user,
        premium=False,
        authorized=case != "expired",
        rights=case != "rights",
        identity=654321 if case == "wrong-bot" else 123456,
    )
    if case == "network":
        native.connect.side_effect = RuntimeError(TOKEN)
    monkeypatch.setattr(
        management_module, "TelegramClient", lambda *_args, **_kwargs: native
    )
    row = ManagedClient(
        id="test-account",
        label="Test",
        kind="user" if user else "bot",
        credential=SecretStr(fake_session() if user else TOKEN),
    )
    with pytest.raises(SettingsProblem) as captured:
        await verify_connection(12345, "a" * 32, row, -1001234567890)
    assert TOKEN not in str(captured.value)
    native.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_applies_enabled_rows_and_removal_cannot_resurrect_environment(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    backend = register_test_backend(monkeypatch, data)
    candidate = document()
    candidate.clients.append(
        ManagedClient(
            id="disabled-bot",
            label="Disabled",
            kind="bot",
            enabled=False,
            credential=SecretStr(TOKEN),
        )
    )
    save_document(data, candidate, 0)
    monkeypatch.setenv("MONGODB_URI", "mongodb://synthetic.invalid")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("BOT_TOKENS", TOKEN)
    monkeypatch.setenv("STORAGE_CHANNEL", "-1001234567890")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)
    applied = Settings(_env_file=None)
    assert applied.bot_tokens == (SECOND_TOKEN,)
    runtime = SimpleNamespace(store=None, pool=None, mongo=backend)
    manager = ConnectionManager(applied, runtime)
    result = await manager.snapshot()
    assert result["applied_revision"] == result["revision"] == 1
    assert not result["restart_required"]
    assert len(result["clients"]) == 2
    assert len(result["active_clients"]) == 1


@pytest.mark.asyncio
async def test_disabling_expired_bot_does_not_reverify_disabled_credentials(managed):
    manager, _, _, _ = managed
    row = (await manager.snapshot())["clients"][0]
    verified = []

    async def verifier(_api_id, _api_hash, candidate, _channel):
        verified.append(candidate.id)
        assert candidate.credential.get_secret_value() == SECOND_TOKEN
        return "storage-bot-654321"

    manager.verifier = verifier
    result = await manager.save(
        SettingsChange.model_validate(
            {
                "expected_revision": 0,
                "clients": [
                    {
                        "id": row["id"],
                        "label": "Expired",
                        "kind": "bot",
                        "enabled": False,
                    },
                    {"label": "Replacement", "kind": "bot", "credential": SECOND_TOKEN},
                ],
            }
        )
    )
    assert len(verified) == 1
    assert result["clients"][0]["enabled"] is False
    assert result["clients"][1]["enabled"] is True


@pytest.mark.asyncio
async def test_concurrent_managers_cas_after_candidate_verification(managed):
    first, runtime, settings, _ = managed
    entered = 0
    both = asyncio.Event()

    async def verifier(*_args):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await both.wait()
        return "storage-bot-123456"

    first.verifier = verifier
    second = ConnectionManager(settings, runtime, verifier=verifier)
    results = await asyncio.gather(
        first.save(SettingsChange(expected_revision=0, api_id=1)),
        second.save(SettingsChange(expected_revision=0, api_id=2)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    failures = [result for result in results if isinstance(result, SettingsProblem)]
    assert len(failures) == 1 and failures[0].code == "SETTINGS_CONFLICT"
    assert read_document(settings.resolved_data_dir).revision == 1


@pytest.mark.asyncio
async def test_cancelled_preflight_drains_all_probe_tasks_and_never_saves(managed):
    manager, _, settings, _ = managed
    entered = 0
    closed = 0
    both = asyncio.Event()

    async def verifier(*_args):
        nonlocal entered, closed
        entered += 1
        if entered == 2:
            both.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed += 1

    manager.verifier = verifier
    task = asyncio.create_task(
        manager.save(
            SettingsChange.model_validate(
                {
                    "expected_revision": 0,
                    "clients": [
                        {"label": "One", "kind": "bot", "credential": TOKEN},
                        {"label": "Two", "kind": "bot", "credential": SECOND_TOKEN},
                    ],
                }
            )
        )
    )
    await both.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == 2
    assert read_document(settings.resolved_data_dir) is None


@pytest.mark.asyncio
async def test_inflight_bot_import_blocks_credentials_commit(managed):
    manager, runtime, settings, _ = managed
    runtime.bot_mode = SimpleNamespace(active_imports=1)
    with pytest.raises(SettingsProblem) as captured:
        await manager.save(SettingsChange(expected_revision=0, api_id=222))
    assert captured.value.code == "SETTINGS_BUSY"
    assert read_document(settings.resolved_data_dir) is None


@pytest.mark.asyncio
async def test_removing_premium_preserves_bot_multipart_logical_limit(managed):
    manager, _, settings, _ = managed
    manager.settings = settings.model_copy(
        update={
            "max_file_size": 3000 * 1024**2,
            "string_sessions_csv": SecretStr("synthetic-premium"),
        }
    )
    result = await manager.save(SettingsChange(expected_revision=0, api_id=123))
    assert result["restart_required"]
    assert manager.settings.upload_limit == 3000 * 1024**2
    assert read_document(settings.resolved_data_dir) is not None


async def test_login_api_rejects_busy_before_phone_code_request(managed):
    manager, runtime, _, _ = managed
    await runtime.transfers.registry.create("uploading")
    with pytest.raises(SettingsProblem) as captured:
        manager.login_api()
    assert captured.value.code == "SETTINGS_BUSY"


@pytest.mark.asyncio
async def test_stale_completed_receipt_does_not_block_connection_changes(managed):
    manager, runtime, settings, _ = managed
    uploads = ResumableUploads(settings, runtime.transfers)
    try:
        receipt = uploads.create("finished.bin", "/", 3)
        record = await runtime.transfers.registry.create(
            "uploading", transfer_id=receipt["id"]
        )
        record.status = TransferStatus.COMPLETED
        result = await manager.save(SettingsChange(expected_revision=0, api_id=123))
        assert result["restart_required"]
        assert result["stats"]["staging_reserved_bytes"] == 0
    finally:
        uploads.close()


@pytest.mark.asyncio
async def test_cancellation_while_collecting_health_happens_before_session_commit(
    managed,
):
    from tests.telegram.test_premium_login import LoginClient
    from tgdrive.settings.premium_login import PremiumLoginService

    manager, runtime, settings, _ = managed
    entered = asyncio.Event()

    async def blocked_status():
        entered.set()
        await asyncio.Event().wait()

    runtime.pool = SimpleNamespace(status=blocked_status)
    client = LoginClient()
    service = PremiumLoginService(
        manager.login_api,
        manager.add_authenticated_session,
        client_factory=lambda *_: client,
    )
    task = None
    try:
        identity = (await service.begin("owner", 0, "+919876543210", "Premium"))[
            "challenge_id"
        ]
        task = asyncio.create_task(service.verify("owner", identity, code="12345"))
        await entered.wait()
        assert read_document(settings.resolved_data_dir) is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.logged_out  # Not committed: the disposable login is revoked.
        assert read_document(settings.resolved_data_dir) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_post_commit_mongodb_reply_loss_is_resolved_by_private_probe(
    managed, monkeypatch
):
    manager, runtime, _, _ = managed
    backend = runtime.mongo
    collection = backend.db.settings
    original = collection.replace_one

    def lose_reply(*args, **kwargs):
        original(*args, **kwargs)
        backend._settings_cached = False
        raise ConnectionFailure("synthetic lost commit response")

    monkeypatch.setattr(collection, "replace_one", lose_reply)
    with pytest.raises(ConnectionFailure):
        await manager.add_authenticated_session(
            0, "Premium", "committed-private-session"
        )
    assert manager.session_is_saved("committed-private-session") is True
    assert manager.session_is_saved("not-committed") is False

    def unavailable(_query):
        raise ConnectionFailure("synthetic unavailable settings")

    backend._settings_cached = False
    monkeypatch.setattr(collection, "find_one", unavailable)
    assert manager.session_is_saved("committed-private-session") is None


@pytest.mark.asyncio
async def test_cancellation_after_commit_never_revokes_saved_session(
    managed, monkeypatch
):
    from tests.telegram.test_premium_login import SESSION, LoginClient
    from tgdrive.settings.premium_login import PremiumLoginService

    manager, _, _, _ = managed
    original = management_module.save_document
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def cancel_after_commit(*arguments):
        original(*arguments)
        loop.call_soon_threadsafe(task.cancel)

    monkeypatch.setattr(management_module, "save_document", cancel_after_commit)
    client = LoginClient()

    async def disconnect():
        await asyncio.sleep(0)
        client.disconnected = True

    client.disconnect = disconnect
    service = PremiumLoginService(
        manager.login_api,
        manager.add_authenticated_session,
        client_factory=lambda *_: client,
        is_session_saved=manager.session_is_saved,
    )
    try:
        identity = (await service.begin("owner", 0, "+919876543210", "Premium"))[
            "challenge_id"
        ]
        with pytest.raises(asyncio.CancelledError):
            await service.verify("owner", identity, code="12345")
        assert manager.session_is_saved(SESSION) is True
        assert not client.logged_out
    finally:
        await service.close()


async def test_cached_names_are_read_only_and_hidden_for_replacement_account(managed):
    manager, runtime, _, _ = managed
    old_name = "storage-bot-123456"
    manager.identity_cache.write(
        old_name, {"display_name": "Actual Bot", "username": "actual_bot"}
    )
    runtime.pool = SimpleNamespace(
        status=AsyncMock(
            return_value=(
                SimpleNamespace(
                    name=old_name, started=True, healthy=True, connected=True
                ),
            )
        ),
    )
    before = await manager.snapshot()
    assert before["clients"][0]["display_name"] == "Actual Bot"
    assert before["clients"][0]["username"] == "actual_bot"
    assert before["clients"][0]["label"] == "Storage bot 1"
    result = await manager.save(
        SettingsChange.model_validate(
            {
                "expected_revision": 0,
                "clients": [
                    {
                        "id": before["clients"][0]["id"],
                        "label": "Replacement",
                        "kind": "bot",
                        "credential": SECOND_TOKEN,
                    }
                ],
            }
        )
    )
    assert result["clients"][0]["display_name"] is None
    assert result["clients"][0]["username"] is None


def test_refresh_names_requires_owner_and_same_origin_without_saving_configuration(
    managed,
):
    from tests.support.app_fixtures import login

    manager, runtime, settings, _ = managed
    manager.verifier = AsyncMock(return_value="storage-bot-123456")
    runtime.pool = SimpleNamespace(status=AsyncMock(return_value=()))
    with TestClient(
        create_app(settings, runtime=runtime), base_url="https://testserver"
    ) as client:
        route = "/api/v1/settings/refresh"
        assert client.post(route, json={}).status_code == 401
        login(client)
        assert (
            client.post(
                route, json={}, headers={"Origin": "https://other.example"}
            ).status_code
            == 403
        )
        assert manager.verifier.call_count == 0
        response = client.post(route, json={})
        assert response.status_code == 202
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["revision"] == 0
        assert response.json()["restart_required"] is False
        assert TOKEN not in response.text

        async def completed_checks():
            await asyncio.gather(*manager._check_tasks)

        # The endpoint accepts background work; a GET need not complete it.
        client.portal.call(completed_checks)
        client.get("/api/v1/settings")
        assert manager.verifier.call_count == 1
        assert read_document(settings.resolved_data_dir) is None


@pytest.mark.asyncio
async def test_refresh_checks_all_enabled_saved_clients_bounded_and_isolates_failure(
    managed,
):
    manager, runtime, _, _ = managed
    rows = []
    for index in range(5):
        rows.append(
            ManagedClient(
                id=f"bot-{index}",
                label=f"Bot {index}",
                kind="bot",
                enabled=index != 4,
                credential=SecretStr(f"{123450 + index}:" + "x" * 30),
            )
        )
    manager.active = rows
    runtime.pool = SimpleNamespace(status=AsyncMock(return_value=()))
    release = asyncio.Event()
    both_entered = asyncio.Event()
    entered = 0
    peak = 0
    calls = []

    async def verify(_id, _hash, row, _channel):
        nonlocal entered, peak
        calls.append(row.id)
        entered += 1
        peak = max(peak, entered)
        if entered == 2:
            both_entered.set()
        try:
            await release.wait()
            if row.id == "bot-1":
                raise SettingsProblem("Storage Channel permission is missing")
            return "storage-bot-" + row.credential.get_secret_value().split(":")[0]
        finally:
            entered -= 1

    manager.verifier = verify
    first = await manager.refresh_connections()
    assert any(
        row.get("connection_check") in {"queued", "checking"}
        for row in first["clients"]
    )
    await manager.refresh_connections()
    await asyncio.wait_for(both_entered.wait(), 2)
    assert peak == 2
    release.set()
    await asyncio.gather(*manager._check_tasks)
    result = await manager.snapshot()
    assert sorted(calls) == ["bot-0", "bot-1", "bot-2", "bot-3"]
    assert peak == 2
    assert result["clients"][1]["connection_check"] == "failed"
    assert (
        result["clients"][1]["connection_check_error"]
        == "Storage Channel permission is missing"
    )
    assert result["clients"][0]["connection_check"] == "passed"
    assert "connection_check" not in result["clients"][4]
    assert result["revision"] == 0
    assert not result["restart_required"]


@pytest.mark.asyncio
async def test_refresh_cancels_on_close_and_hides_results_after_configuration_change(
    managed,
):
    manager, _, _, _ = managed
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def verify(*_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    manager.verifier = verify
    await manager.refresh_connections()
    await entered.wait()
    await manager.close()
    assert cancelled.is_set()
    assert not manager._check_tasks
    manager._checks_revision = 99
    assert "connection_check" not in (await manager.snapshot())["clients"][0]


@pytest.mark.asyncio
async def test_refresh_preserves_flood_wait_and_never_exposes_raw_error(managed):
    manager, runtime, _, _ = managed
    runtime.pool = SimpleNamespace(
        status=AsyncMock(return_value=()), defer_client=AsyncMock()
    )
    problem = SettingsProblem("Telegram temporarily limited verification")
    problem.retry_after = 120
    manager.verifier = AsyncMock(side_effect=problem)
    await manager.refresh_connections()
    for _ in range(100):
        if runtime.pool.defer_client.await_count:
            break
        await asyncio.sleep(0.01)
    row = (await manager.snapshot())["clients"][0]
    assert row["connection_check"] == "waiting"
    deadline = row["connection_retry_at"]
    assert manager.identity_cache.retry_at("storage-bot-123456") == deadline
    runtime.pool.defer_client.assert_awaited_once_with("storage-bot-123456", deadline)
    assert manager.verifier.await_count == 1
    await manager.close()
    manager._refresh_not_before = 0
    manager.verifier = AsyncMock(side_effect=RuntimeError("secret-token"))
    # Do not sleep through a persisted wait in this separate error-sanitization check.
    manager.identity_cache.retry_at = lambda _name: 0
    await manager.refresh_connections()
    await asyncio.gather(*manager._check_tasks)
    result = await manager.snapshot()
    assert result["clients"][0]["connection_check"] == "failed"
    assert "secret-token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_refresh_obeys_existing_transfer_pool_wait_before_network_probe(managed):
    import time

    manager, runtime, _, _ = managed
    deadline = time.time() + 300
    runtime.pool = SimpleNamespace(
        status=AsyncMock(return_value=()),
        account_retry_at=AsyncMock(return_value=deadline),
    )
    manager.verifier = AsyncMock()
    await manager.refresh_connections()
    for _ in range(100):
        if runtime.pool.account_retry_at.await_count:
            break
        await asyncio.sleep(0.001)
    result = await manager.snapshot()
    assert result["clients"][0]["connection_check"] == "waiting"
    assert result["clients"][0]["connection_retry_at"] == deadline
    manager.verifier.assert_not_awaited()
    await manager.close()
