from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_app import FakeRuntime, login, make_settings

from tgdrive.app import create_app
from tgdrive.managed_settings import SettingsProblem
from tgdrive.store import DriveStore
from tgdrive.transfer_preferences import TransferPreferences, UploadPreferences


def preference_runtime():
    return SimpleNamespace(
        pool=SimpleNamespace(
            set_uploads_per_bot=AsyncMock(),
            status=AsyncMock(
                return_value=[
                    SimpleNamespace(kind="bot", started=True, healthy=True),
                    SimpleNamespace(kind="bot", started=True, healthy=True),
                    SimpleNamespace(kind="user", started=True, healthy=True),
                ]
            ),
        )
    )


async def test_saved_limit_survives_restart_and_failed_write_does_not_apply(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path)
    runtime = preference_runtime()
    service = TransferPreferences(settings, runtime)
    assert (await service.snapshot())["bot_upload_slots"] == 2
    assert (await service.save(UploadPreferences(uploads_per_bot=3)))[
        "bot_upload_slots"
    ] == 6
    restarted = TransferPreferences(settings, preference_runtime())
    assert (await restarted.snapshot())["uploads_per_bot"] == 3

    def failed(_value):
        raise OSError("synthetic failed write")

    monkeypatch.setattr(service, "_write", failed)
    with pytest.raises(SettingsProblem):
        await service.save(UploadPreferences(uploads_per_bot=2))
    assert (await service.snapshot())["uploads_per_bot"] == 3
    assert runtime.pool.set_uploads_per_bot.await_args.args == (3,)


def test_owner_api_rejects_invalid_limits_and_applies_without_restart(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    runtime = FakeRuntime(store)
    runtime.pool = preference_runtime().pool
    app = create_app(settings, runtime=runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            url = "/api/v1/settings/uploads"
            assert client.get(url).status_code == 401
            login(client)
            response = client.get(url)
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.json()["uploads_per_bot"] == 1
            for value in (0, 5, 1.5, True, "2"):
                assert (
                    client.patch(url, json={"uploads_per_bot": value}).status_code
                    == 422
                )
            assert (
                client.patch(url, json={"uploads_per_bot": 2, "extra": 1}).status_code
                == 422
            )
            assert (
                client.patch(url, json={"uploads_per_bot": 2}).json()[
                    "bot_upload_slots"
                ]
                == 4
            )
            assert client.get(url).json()["uploads_per_bot"] == 2
    finally:
        runtime.transfers.registry.close()
        store.close()
