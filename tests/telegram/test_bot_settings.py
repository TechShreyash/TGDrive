import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import new_store, register_test_backend
from tests.telegram.test_bot_mode import FakeBotClient, FakeMessage, settings
from tgdrive.settings.bot_settings import (
    DEFAULT_FOLDER_NAME,
    SETTINGS_LOCATION,
    BotSettings,
    BotUploadPreferences,
    get_bot_settings,
)
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.telegram.bot_mode import BotMode
from tgdrive.telegram.clients import Client
from tgdrive.web.app import create_app


@pytest.fixture
def bot_setup(tmp_path, monkeypatch):
    configured = settings(tmp_path)
    store = new_store(tmp_path / "drive")
    backend = register_test_backend(monkeypatch, configured)
    runtime = SimpleNamespace(store=store, mongo=backend)
    service = get_bot_settings(configured, runtime)
    mode = BotMode(
        client=cast(Client, FakeBotClient()),
        settings=configured,
        store=store,
        folder_settings=service,
    )
    try:
        yield service, mode, store
    finally:
        mode.close()
        store.close()


def file_message(user_id=1, identity=100):
    return FakeMessage(
        user_id,
        copied=SimpleNamespace(
            id=identity,
            document=SimpleNamespace(file_name="forwarded.txt", file_size=3),
        ),
    )


async def test_forwarding_creates_dedicated_default_once_for_all_admins(bot_setup):
    service, bot, store = bot_setup
    snapshot = await service.snapshot()
    assert snapshot["folder_path"] is None and snapshot["available"]
    assert snapshot["folder_label"] == "My Files / TGDriveBotModeFiles"
    assert store.list_directory("/") == {}  # Viewing settings is read-only.
    await asyncio.gather(
        *(
            bot._file(bot.client, file_message(user_id, identity=100 + user_id))
            for user_id in (1, 2)
        )
    )
    folders = list(store.list_directory("/").values())
    assert len(folders) == 1 and folders[0]["name"] == DEFAULT_FOLDER_NAME
    path = f"/{folders[0]['id']}"
    assert len(store.list_directory(path)) == 2
    assert (await service.snapshot())["folder_path"] == path
    restarted = BotSettings(service.settings, service.runtime)
    assert (await restarted.snapshot())["folder_path"] == path


async def test_website_change_applies_to_next_import_without_restart(bot_setup):
    service, bot, store = bot_setup
    folder = store.create_folder("/", "Incoming")
    path = f"/{folder['id']}"
    await service.save(
        BotUploadPreferences(folder_path=path, site_url="https://drive.example")
    )
    message = file_message()
    await bot._file(bot.client, message)
    assert len(store.list_directory(path)) == 1
    assert "Incoming" in message.status.edit_text.calls[-1][0][0]
    link_message = FakeMessage(2, text="/set_folder")
    await bot._set_folder(bot.client, link_message)
    text, buttons = link_message.replies[-1]
    assert "https://drive.example" + SETTINGS_LOCATION in text
    assert buttons is not None
    assert buttons[0][0].type.url == "https://drive.example" + SETTINGS_LOCATION
    await service.save(BotUploadPreferences(folder_path="/"))
    message = file_message(2, identity=102)
    assert (await service.snapshot())[
        "settings_url"
    ] == "https://drive.example" + SETTINGS_LOCATION
    await bot._file(bot.client, message)
    assert any(item["type"] == "file" for item in store.list_directory("/").values())


async def test_missing_destination_blocks_import_and_save_rejects_files_and_trash(
    bot_setup,
):
    service, bot, store = bot_setup
    folder = store.create_folder("/", "Inbox")
    path = f"/{folder['id']}"
    await service.save(BotUploadPreferences(folder_path=path))
    store.set_trashed(path, True)
    message = file_message()
    await bot._file(bot.client, message)
    assert "/set_folder" in message.replies[-1][0]
    assert store.list_directory("/") == {}
    assert not bot.client.deleted
    assert not (await service.snapshot())["available"]
    file = store.add_file("/", "not-a-folder", 888, 1)
    for invalid in (path, f"/{file['id']}", "/missing"):
        with pytest.raises(SettingsProblem):
            await service.save(BotUploadPreferences(folder_path=invalid))
    assert service.value.folder_path == path


async def test_default_reuses_existing_folder_and_reset_restores_default(bot_setup):
    service, _, store = bot_setup
    folder = store.create_folder("/", DEFAULT_FOLDER_NAME)
    custom = store.create_folder("/", "Custom")
    await service.save(BotUploadPreferences(folder_path=f"/{custom['id']}"))
    await service.save(BotUploadPreferences())
    assert (await service.destination())[0] == f"/{folder['id']}"
    store.rename(f"/{folder['id']}", "Renamed inbox")
    assert (await service.destination())[1] == "My Files / Renamed inbox"
    assert len(store.list_directory("/")) == 2


async def test_folder_change_during_copy_keeps_inflight_file_in_its_original_folder(
    bot_setup,
):
    service, bot, store = bot_setup
    first = store.create_folder("/", "First")
    second = store.create_folder("/", "Second")
    first_path, second_path = f"/{first['id']}", f"/{second['id']}"
    await service.save(BotUploadPreferences(folder_path=first_path))
    message = file_message()
    entered, resume = asyncio.Event(), asyncio.Event()

    async def copy(*args, **kwargs):
        entered.set()
        await resume.wait()
        return message.copied

    message.copy = copy
    task = asyncio.create_task(bot._file(bot.client, message))
    try:
        await entered.wait()
        await service.save(BotUploadPreferences(folder_path=second_path))
        resume.set()
        await task
        assert len(store.list_directory(first_path)) == 1
        assert store.list_directory(second_path) == {}
        await bot._file(bot.client, file_message(2, identity=120))
        assert len(store.list_directory(second_path)) == 1
    finally:
        resume.set()
        await task


async def test_blocked_upload_does_not_create_default_folder(bot_setup):
    _, bot, store = bot_setup

    def block():
        raise SettingsProblem("Drive protection needs attention")

    bot.before_write = block
    await bot._file(bot.client, file_message())
    assert not store.list_directory("/")


async def test_failed_write_does_not_apply_new_folder(bot_setup, monkeypatch):
    service, _, _ = bot_setup
    await service.save(BotUploadPreferences(folder_path="/"))

    def fail(value):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(service, "_write", fail)
    with pytest.raises(SettingsProblem):
        await service.save(BotUploadPreferences())
    assert service.value.folder_path == "/"
    assert (await BotSettings(service.settings, service.runtime).snapshot())[
        "folder_path"
    ] == "/"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:secret@example.com",
        "https://example.com/?auth=secret",
        "https://example.com/#share",
        "not a url",
    ],
)
def test_settings_links_reject_credentials_tokens_and_non_web_urls(url):
    with pytest.raises(ValidationError):
        BotUploadPreferences(site_url=url)


async def test_configured_public_url_takes_precedence_over_a_local_browser_address(
    bot_setup,
):
    service, _, _ = bot_setup
    service.settings.public_url = "https://public.example"
    await service.save(
        BotUploadPreferences(folder_path="/", site_url="http://localhost:8000")
    )
    assert (await service.snapshot())[
        "settings_url"
    ] == "https://public.example" + SETTINGS_LOCATION


async def test_mongodb_preferences_survive_service_restart(bot_setup):
    service, _, store = bot_setup

    class Collection:
        document = None

        def find_one(self, query):
            assert query == {"_id": "bot-preferences"}
            return self.document

        def replace_one(self, query, document, **kwargs):
            self.document = document

    collection = Collection()
    backend = SimpleNamespace(db=SimpleNamespace(settings=collection))
    backend.write = lambda target, method, *args, **kwargs: getattr(target, method)(
        *args, **kwargs
    )
    runtime = SimpleNamespace(store=store, mongo=backend)
    service = BotSettings(service.settings, runtime)
    await service.save(
        BotUploadPreferences(folder_path="/", site_url="https://drive.example")
    )
    assert (await BotSettings(service.settings, runtime).snapshot())[
        "settings_url"
    ] == "https://drive.example" + SETTINGS_LOCATION


def test_bot_settings_owner_api_validates_and_persists(tmp_path, monkeypatch):
    configured = make_settings(tmp_path)
    store = new_store(tmp_path / "drive")
    runtime = FakeRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, configured)
    app = create_app(configured, runtime=runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            url = "/api/v1/settings/bot-mode"
            assert client.get(url).status_code == 401
            assert client.patch(url, json={"folder_path": "/"}).status_code == 401
            login(client)
            response = client.get(url)
            assert response.headers["Cache-Control"] == "no-store"
            assert response.json()["folder_label"].endswith(DEFAULT_FOLDER_NAME)
            assert store.list_directory("/") == {}
            for value in (True, 123, "/../escape"):
                assert client.patch(url, json={"folder_path": value}).status_code == 422
            assert (
                client.patch(url, json={"folder_path": "/missing"}).status_code == 409
            )
            response = client.patch(
                url, json={"folder_path": "/", "site_url": "https://testserver"}
            )
            assert response.status_code == 200
            assert response.json()["folder_label"] == "My Files"
            assert (
                client.get(url).json()["settings_url"]
                == "https://testserver" + SETTINGS_LOCATION
            )
    finally:
        runtime.transfers.registry.close()
        store.close()


@pytest.mark.parametrize("operation", ["save", "destination", "create_folder"])
async def test_cancelled_bot_mutation_keeps_lock_until_following_save_is_safe(
    bot_setup, monkeypatch, operation
):
    import threading

    service, _bot, store = bot_setup
    first_folder = store.create_folder("/", "First")
    last_folder = store.create_folder("/", "Last")
    first_path, last_path = "/" + first_folder["id"], "/" + last_folder["id"]
    await service.snapshot()
    entered, release, finished = (threading.Event() for _ in range(3))
    original_write, original_create = service._write, store.create_folder

    def held(*args):
        entered.set()
        assert release.wait(5), "Bot settings worker was not released"
        try:
            return (
                original_create if operation == "create_folder" else original_write
            )(*args)
        finally:
            finished.set()

    if operation == "create_folder":
        monkeypatch.setattr(store, "create_folder", held)
    else:

        def write(value):
            if value.folder_path != last_path:
                return held(value)
            return original_write(value)

        monkeypatch.setattr(service, "_write", write)
    first = asyncio.create_task(
        service.save(BotUploadPreferences(folder_path=first_path))
        if operation == "save"
        else service.destination()
    )
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        following = asyncio.create_task(
            service.save(BotUploadPreferences(folder_path=last_path))
        )
        await asyncio.sleep(0)
        assert not first.done() and not following.done()
        assert service._lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await following
        saved = service.runtime.mongo.db.settings.find_one({"_id": "bot-preferences"})
        assert saved is not None
        assert (
            saved["document"]["folder_path"] == service.value.folder_path == last_path
        )
    finally:
        release.set()
        await asyncio.gather(
            first, *([following] if following else []), return_exceptions=True
        )
        assert await asyncio.to_thread(finished.wait, 2)


async def test_bot_destination_validation_stays_cancellable_before_commit(
    bot_setup, monkeypatch
):
    import threading

    service, _bot, store = bot_setup
    folder = store.create_folder("/", "Selected")
    await service.snapshot()
    entered, release, finished = (threading.Event() for _ in range(3))
    original = store.get_directory

    def read(path):
        entered.set()
        assert release.wait(5)
        try:
            return original(path)
        finally:
            finished.set()

    monkeypatch.setattr(store, "get_directory", read)
    task = asyncio.create_task(
        service.save(BotUploadPreferences(folder_path="/" + folder["id"]))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not service._lock.locked()
        assert (
            service.runtime.mongo.db.settings.find_one({"_id": "bot-preferences"})
            is None
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 2)
