"""Archive admission, owner access and explicit public-destination consent."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login
from tests.web.test_demo_admin_auth import VISITOR, auth_client, sign_in
from tgdrive.storage.demo_files import DEMO_FILES_ID
from tgdrive.transfers.archives import ArchiveError
from tgdrive.transfers.staging import StorageUnavailable


def capture_starts(runtime, monkeypatch):
    calls = []

    def starter(kind):
        async def start(**payload):
            calls.append((kind, payload))
            return await runtime.transfers.registry.create(
                "archiving" if kind == "archive" else "downloading",
                context={
                    "kind": kind,
                    "source_kind": kind,
                    "name": payload.get("name", "Source.zip"),
                    "destination": payload["destination"],
                },
            )

        return start

    monkeypatch.setattr(
        runtime.transfers, "start_archive", starter("archive"), raising=False
    )
    monkeypatch.setattr(
        runtime.transfers, "start_extraction", starter("extract"), raising=False
    )
    return calls


@pytest.mark.parametrize("extract", [False, True])
def test_archive_routes_require_owner_even_with_a_valid_share(
    app_client, monkeypatch, extract
):
    client, store, runtime, _ = app_client
    source = store.add_file("/", "Source.zip", 123, 20)
    path = "/" + source["id"]
    token = store.create_share(path)
    calls = capture_starts(runtime, monkeypatch)
    route = "/api/v1/archives" + ("/extract" if extract else "")
    payload = {"path": path} if extract else {"paths": [path], "name": "Bundle.zip"}
    response = client.post(route, json={**payload, "auth": token})
    assert response.status_code == 401
    assert calls == []


@pytest.mark.parametrize("folder_name", [None, "Unpacked"])
def test_extraction_returns_identifiable_job_and_preserves_destination_choice(
    app_client, monkeypatch, folder_name
):
    client, _, runtime, _ = app_client
    calls = capture_starts(runtime, monkeypatch)
    login(client)
    response = client.post(
        "/api/v1/archives/extract",
        json={"path": "/source", "folder_name": folder_name},
    )
    assert response.status_code == 202
    result = response.json()
    assert result["id"] == result["job"]["id"]
    assert result["job"]["kind"] == "extract"
    assert result["job"]["destination"] == "/"
    assert calls == [
        (
            "extract",
            {
                "path": "/source",
                "destination": "/",
                "folder_name": folder_name,
                "allow_public_exposure": False,
            },
        )
    ]


@pytest.mark.parametrize("extract", [False, True])
def test_archive_routes_confirm_inherited_public_folder_access_before_start(
    app_client, monkeypatch, extract
):
    client, store, runtime, _ = app_client
    shared = store.create_folder("/", "Shared")
    shared_path = "/" + shared["id"]
    nested = store.create_folder(shared_path, "Nested")
    destination = shared_path + "/" + nested["id"]
    store.create_share(shared_path)
    calls = capture_starts(runtime, monkeypatch)
    login(client)
    route = "/api/v1/archives" + ("/extract" if extract else "")
    payload = (
        {"path": "/source"} if extract else {"paths": ["/source"], "name": "Bundle.zip"}
    )
    payload["destination"] = destination
    denied = client.post(route, json=payload)
    assert denied.status_code == 409
    assert denied.json()["code"] == "PUBLIC_EXPOSURE_CONFIRMATION_REQUIRED"
    assert calls == []

    allowed = client.post(route, json={**payload, "allow_public_exposure": True})
    assert allowed.status_code == 202
    assert allowed.json()["job"]["destination"] == destination
    assert calls[0][1]["allow_public_exposure"] is True
    assert allowed.json()["job"]["kind"] == ("extract" if extract else "archive")


@pytest.mark.parametrize("extract", [False, True])
@pytest.mark.parametrize("problem", ["missing", "file", "trashed"])
def test_invalid_archive_destinations_never_start_work(
    app_client, monkeypatch, extract, problem
):
    client, store, runtime, _ = app_client
    destination = "/missing"
    if problem == "file":
        destination = "/" + store.add_file("/", "Target.txt", 124, 10)["id"]
    elif problem == "trashed":
        destination = "/" + store.create_folder("/", "Trashed")["id"]
        store.set_trashed(destination, True)
    calls = capture_starts(runtime, monkeypatch)
    login(client)
    route = "/api/v1/archives" + ("/extract" if extract else "")
    payload = (
        {"path": "/source"} if extract else {"paths": ["/source"], "name": "Bundle.zip"}
    )
    response = client.post(route, json={**payload, "destination": destination})
    assert response.status_code == 404
    assert calls == []


@pytest.mark.parametrize("extract", [False, True])
def test_archive_admission_obeys_restart_pause(app_client, monkeypatch, extract):
    client, _, runtime, _ = app_client
    calls = capture_starts(runtime, monkeypatch)
    runtime.connection_manager = SimpleNamespace(
        restart_required=True, close=AsyncMock()
    )
    login(client)
    route = "/api/v1/archives" + ("/extract" if extract else "")
    payload = (
        {"path": "/source"} if extract else {"paths": ["/source"], "name": "Bundle.zip"}
    )
    response = client.post(route, json=payload)
    assert response.status_code == 409
    assert response.json()["code"] == "SETTINGS_RESTART_REQUIRED"
    assert calls == []


@pytest.mark.parametrize("extract", [False, True])
def test_demo_archive_destinations_remain_read_only(tmp_path, monkeypatch, extract):
    with auth_client(tmp_path) as (client, _, runtime):
        runtime.store.ensure_demo_files()
        calls = capture_starts(runtime, monkeypatch)
        assert sign_in(client, VISITOR).status_code == 200
        route = "/api/v1/archives" + ("/extract" if extract else "")
        payload = (
            {"path": "/source"}
            if extract
            else {"paths": ["/source"], "name": "Bundle.zip"}
        )
        response = client.post(
            route, json={**payload, "destination": "/" + DEMO_FILES_ID}
        )
        assert response.status_code == 403
        assert response.json()["code"] == "DEMO_FILES_READ_ONLY"
        assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"paths": [], "name": "Bundle.zip"},
        {"paths": ["/source"] * 1001, "name": "Bundle.zip"},
        {"paths": [""], "name": "Bundle.zip"},
        {"paths": ["/source"], "name": ""},
        {"paths": ["/source"], "name": "x" * 256},
        {
            "paths": ["/source"],
            "name": "Bundle.zip",
            "allow_public_exposure": "true",
        },
    ],
)
def test_archive_request_bounds_reject_before_work(app_client, monkeypatch, payload):
    client, _, runtime, _ = app_client
    calls = capture_starts(runtime, monkeypatch)
    login(client)
    assert client.post("/api/v1/archives", json=payload).status_code == 422
    assert calls == []


@pytest.mark.parametrize(
    "error,status",
    [
        (ArchiveError("Unsupported ZIP"), 400),
        (StorageUnavailable("Free temporary storage and retry."), 507),
    ],
)
def test_archive_admission_errors_remain_actionable(
    app_client, monkeypatch, error, status
):
    client, _, runtime, _ = app_client

    async def fail(**_payload):
        raise error

    monkeypatch.setattr(runtime.transfers, "start_archive", fail, raising=False)
    login(client)
    response = client.post(
        "/api/v1/archives", json={"paths": ["/source"], "name": "Bundle.zip"}
    )
    assert response.status_code == status
    assert response.json()["detail"] == str(error)
