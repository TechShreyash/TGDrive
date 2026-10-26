"""Portable recovery credentials cannot bypass mandatory MongoDB access."""

import json
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher

from tests.support.app_fixtures import make_settings
from tgdrive.core.config import Settings
from tgdrive.replication.replica_bootstrap import load_bootstrap, save_bootstrap
from tgdrive.replication.replica_state import atomic_private
from tgdrive.storage.mongo import DatabaseUnavailable


def test_bootstrap_contains_valid_credentials_and_only_a_password_hash(tmp_path):
    settings = make_settings(tmp_path)
    save_bootstrap(settings)
    path = settings.resolved_data_dir / "replica-bootstrap.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert settings.admin_password.get_secret_value() not in path.read_text()
    values = load_bootstrap(
        settings.resolved_data_dir,
        mongodb_uri=settings.mongodb_uri,
        mongodb_database=settings.mongodb_database,
    )
    assert PasswordHasher().verify(
        values["ADMIN_PASSWORD_HASH"], settings.admin_password.get_secret_value()
    )
    assert values["SESSION_SECRET"] == settings.session_secret.get_secret_value()
    assert "MONGODB_URI" not in values


@pytest.mark.parametrize(
    "payload",
    [{"version": 2, "settings": {}}, {"version": 1, "settings": {}, "extra": True}],
)
def test_bootstrap_rejects_unrecognized_shapes(tmp_path, payload):
    atomic_private(tmp_path / "replica-bootstrap.json", json.dumps(payload).encode())
    with pytest.raises(ValueError, match="Invalid private recovery bootstrap"):
        load_bootstrap(tmp_path, mongodb_uri="mongodb://synthetic.invalid/tgdrive")


def test_private_bootstrap_cannot_start_without_mongodb(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    save_bootstrap(settings)

    def unavailable(_query):
        raise RuntimeError("database unavailable")

    backend = SimpleNamespace(
        db=SimpleNamespace(settings=SimpleNamespace(find_one=unavailable))
    )
    monkeypatch.setattr("tgdrive.storage.mongo.configure", lambda *_args: backend)
    for name in (
        "API_ID",
        "API_HASH",
        "BOT_TOKENS",
        "STORAGE_CHANNEL",
        "SESSION_SECRET",
        "ADMIN_PASSWORD_HASH",
        "ADMIN_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("API_HASH", "")
    monkeypatch.setenv("DATA_DIR", str(settings.resolved_data_dir))
    monkeypatch.setenv("CACHE_DIR", str(settings.resolved_cache_dir))
    monkeypatch.setenv("MONGODB_URI", "mongodb://synthetic.invalid/tgdrive")
    with pytest.raises(DatabaseUnavailable, match="MongoDB is unavailable"):
        Settings(_env_file=None)
