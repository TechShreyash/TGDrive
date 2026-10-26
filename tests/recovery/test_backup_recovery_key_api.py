"""Recovery-kit key downloads stay private and are never browser-cacheable."""

from pydantic import SecretStr

from tests.recovery.test_backup_api import mongo_backend as mongo_backend
from tests.recovery.test_backup_api import owner as owner_fixture
from tests.support.app_fixtures import login

owner = owner_fixture


def test_recovery_key_download_requires_owner_and_marks_secret_no_store(owner):
    client, _, runtime, settings = owner
    settings.mongodb_backup_key = SecretStr("b" * 64)
    runtime.mongo = object()
    response = client.get("/api/v1/backup/recovery-key")
    assert response.status_code == 401
    assert "b" * 64 not in response.text
    login(client)
    response = client.get("/api/v1/backup/recovery-key")
    assert response.status_code == 200
    assert response.text == "MONGODB_BACKUP_KEY=" + "b" * 64 + "\n"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == (
        'attachment; filename="tgdrive-recovery-key.env"'
    )


def test_recovery_key_is_unavailable_without_matching_backend_or_key(owner):
    client, _, runtime, settings = owner
    login(client)
    settings.mongodb_backup_key = SecretStr("b" * 64)
    runtime.mongo = None
    assert client.get("/api/v1/backup/recovery-key").status_code == 404
    runtime.mongo = object()
    settings.mongodb_backup_key = None
    assert client.get("/api/v1/backup/recovery-key").status_code == 404
