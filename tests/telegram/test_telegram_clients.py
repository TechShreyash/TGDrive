from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import tgdrive.telegram.clients as clients
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.core.config import Settings
from tgdrive.telegram.connection_errors import TelegramConnectionError


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, settings(tmp_path))


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "MONGODB_URI": "mongodb://synthetic.invalid",
        "API_ID": 12345,
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30 + ",654321:" + "y" * 30,
        "STORAGE_CHANNEL": -1001234567890,
        "ADMIN_PASSWORD": "correct horse battery staple",
        "SESSION_SECRET": "s" * 48,
        "STRING_SESSIONS": "session-A,session-B",
        "DATA_DIR": tmp_path / "data",
        "CACHE_DIR": tmp_path / "cache",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class NativeClient:
    instances: ClassVar[list[NativeClient]] = []

    def __init__(self, session: Any, api_id: int, api_hash: str, **options: Any):
        self.options = options
        self.connected = False
        self.authorized = True
        self.sign_ins: list[dict[str, str]] = []
        if isinstance(session, str):
            account_id = int(Path(session).name.rsplit("-", 1)[1])
            path = Path(session + ".session")
            path.write_bytes(b"synthetic session")
            path.chmod(0o644)
            self.session = SimpleNamespace(filename=str(path))
            self.me = SimpleNamespace(id=account_id, bot=True, premium=False)
        else:
            self.session = session
            self.me = SimpleNamespace(id=session.account_id, bot=False, premium=True)
        self.instances.append(self)

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> Any:
        return self.me

    async def sign_in(self, **kwargs: str) -> None:
        assert set(kwargs) == {"bot_token"}
        self.sign_ins.append(kwargs)
        self.authorized = True

    async def start(self, **_kwargs: Any) -> None:
        raise AssertionError("Interactive TelegramClient.start must never be called")


@pytest.fixture(autouse=True)
def native_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    NativeClient.instances.clear()
    monkeypatch.setattr(clients, "TelegramClient", NativeClient)
    monkeypatch.setattr(
        clients,
        "StringSession",
        lambda value: SimpleNamespace(
            account_id={"session-A": 101, "session-B": 202}[value]
        ),
    )


@pytest.mark.asyncio
async def test_pool_identity_survives_credential_reordering(tmp_path: Path) -> None:
    original = settings(tmp_path)
    pool = clients.build_client_pool(original)
    await pool.start()
    names = {item.name for item in await pool.status()}
    paths = {
        item.session.filename
        for item in NativeClient.instances
        if hasattr(item.session, "filename")
    }
    assert names == {
        "storage-bot-123456",
        "storage-bot-654321",
        "premium-user-101",
        "premium-user-202",
    }
    assert all(
        item.options["receive_updates"] is False for item in NativeClient.instances
    )
    assert all(
        item.options["flood_sleep_threshold"] == 0 for item in NativeClient.instances
    )
    assert all(Path(path).stat().st_mode & 0o777 == 0o600 for path in paths)
    await pool.stop()

    NativeClient.instances.clear()
    reordered = settings(
        tmp_path,
        BOT_TOKENS=",".join(reversed(original.bot_tokens)),
        STRING_SESSIONS="session-B,session-A",
    )
    pool = clients.build_client_pool(reordered)
    await pool.start()
    assert {item.name for item in await pool.status()} == names
    assert {
        item.session.filename
        for item in NativeClient.instances
        if hasattr(item.session, "filename")
    } == paths
    await pool.stop()


@pytest.mark.asyncio
async def test_rotated_bot_token_retains_the_account_session_path(
    tmp_path: Path,
) -> None:
    first = clients.Client(
        name="old-position", settings=settings(tmp_path), bot_token="123456:" + "x" * 30
    )
    second = clients.Client(
        name="new-position", settings=settings(tmp_path), bot_token="123456:" + "z" * 30
    )
    assert first.name == second.name == "storage-bot-123456"
    assert first.native.session.filename == second.native.session.filename
    assert "x" * 30 not in first.native.session.filename


@pytest.mark.asyncio
async def test_bot_authorization_never_enters_interactive_login(tmp_path: Path) -> None:
    token = "123456:" + "x" * 30
    client = clients.Client(name="unused", settings=settings(tmp_path), bot_token=token)
    client.native.authorized = False
    await client.start()
    assert client.native.sign_ins == [{"bot_token": token}]
    assert client.is_connected
    await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_id,is_bot", [(123, True), (123456, False)])
async def test_wrong_authorized_account_fails_closed(
    tmp_path: Path,
    wrong_id: int,
    is_bot: bool,
) -> None:
    client = clients.Client(
        name="unused", settings=settings(tmp_path), bot_token="123456:" + "x" * 30
    )
    client.native.me = SimpleNamespace(id=wrong_id, bot=is_bot, premium=True)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        await client.start()
    assert not client.is_connected


@pytest.mark.asyncio
async def test_user_session_must_already_be_authorized(tmp_path: Path) -> None:
    client = clients.Client(
        name="unused", settings=settings(tmp_path), session_string="session-A"
    )
    client.native.authorized = False
    with pytest.raises(
        TelegramConnectionError, match="no longer authorized"
    ) as captured:
        await client.start()
    assert captured.value.code == "TELEGRAM_CREDENTIALS_REVOKED"
    assert not client.native.sign_ins
    assert not client.is_connected


@pytest.mark.asyncio
@pytest.mark.parametrize("is_bot,premium", [(True, True), (False, False)])
async def test_premium_session_capability_is_checked(
    tmp_path: Path,
    is_bot: bool,
    premium: bool,
) -> None:
    client = clients.Client(
        name="unused", settings=settings(tmp_path), session_string="session-A"
    )
    client.native.me = SimpleNamespace(id=101, bot=is_bot, premium=premium)
    with pytest.raises(RuntimeError, match="not a Premium"):
        await client.start()
    assert not client.is_connected


def test_optional_bot_mode_has_separate_update_session(tmp_path: Path) -> None:
    assert clients.build_bot_mode_client(settings(tmp_path)) is None
    value = settings(
        tmp_path, MAIN_BOT_TOKEN="123456:" + "x" * 30, TELEGRAM_ADMIN_IDS="1"
    )
    client = clients.build_bot_mode_client(value)
    assert client is not None
    assert client.native.options["receive_updates"] is True
    assert client.name == "bot-mode-123456"
    assert Path(client.native.session.filename).name == "bot-mode-123456.session"


@pytest.mark.asyncio
async def test_main_bot_bootstraps_storage_with_a_separate_bot_mode_session(
    tmp_path: Path,
) -> None:
    value = settings(
        tmp_path,
        BOT_TOKENS="",
        STRING_SESSIONS="",
        MAIN_BOT_TOKEN="123456:" + "x" * 30,
        TELEGRAM_ADMIN_IDS="1",
    )
    pool = clients.build_client_pool(value)
    bot = clients.build_bot_mode_client(value)
    assert bot is not None
    try:
        await pool.start()
        await bot.start()
        assert {row.name for row in await pool.status()} == {"storage-bot-123456"}
        assert bot.name == "bot-mode-123456"
        assert {item.options["receive_updates"] for item in NativeClient.instances} == {
            False,
            True,
        }
        assert len({item.session.filename for item in NativeClient.instances}) == 2
    finally:
        await bot.stop()
        await pool.stop()


@pytest.mark.asyncio
async def test_optional_session_failure_does_not_disable_working_storage_bots(
    tmp_path: Path,
) -> None:
    pool = clients.build_client_pool(
        settings(tmp_path, STRING_SESSIONS="synthetic-invalid-session")
    )
    await pool.start()
    assert (await pool.health()).ready
    assert (await pool.health()).healthy_clients == 2
    async with pool.lease(prefer_premium=True) as client:
        assert pool.kind_of(client) == "bot"
    await pool.stop()
