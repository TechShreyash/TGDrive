"""Deployment defaults must preserve credentials and keep proxy trust narrow."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher

from tgdrive.config import Settings
from tgdrive.deployment import installation_secrets
from tgdrive.performance_preferences import PerformancePreferences
from tgdrive.server import main, trusted_proxy_ips, wait_for_initialization


def test_generated_keys_survive_restarts_and_concurrent_startup(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(
            workers.map(
                lambda _: installation_secrets(tmp_path, {}, existing_drive=False),
                range(4),
            )
        )
    assert all(result == results[0] for result in results)
    saved = tmp_path / "deployment-secrets.json"
    assert saved.stat().st_mode & 0o777 == 0o600
    assert len(bytes.fromhex(results[0]["MONGODB_BACKUP_KEY"])) == 32
    assert len(results[0]["SESSION_SECRET"]) >= 32
    assert installation_secrets(tmp_path, {}, existing_drive=False) == results[0]


def test_existing_drive_never_gets_replacement_keys(tmp_path):
    with pytest.raises(ValueError, match=r"original.*SESSION_SECRET"):
        installation_secrets(tmp_path, {}, existing_drive=True)
    assert not (tmp_path / "deployment-secrets.json").exists()


@pytest.mark.parametrize("damage", ["invalid-json", "permissions", "symlink"])
def test_saved_keys_are_never_silently_replaced(tmp_path, damage):
    original = installation_secrets(tmp_path, {}, existing_drive=False)
    saved = tmp_path / "deployment-secrets.json"
    if damage == "invalid-json":
        saved.write_text("damaged private file")
    elif damage == "permissions":
        saved.chmod(0o644)
    else:
        saved.rename(tmp_path / "preserved.json")
        saved.symlink_to(tmp_path / "preserved.json")
    before = saved.read_bytes()
    with pytest.raises((OSError, ValueError)):
        installation_secrets(tmp_path, {}, existing_drive=False)
    assert saved.read_bytes() == before
    if damage == "symlink":
        assert json.loads((tmp_path / "preserved.json").read_text()) == original


def test_existing_mongodb_bootstrap_reopens_with_only_database_configuration(
    tmp_path, monkeypatch
):
    """A new server must reuse the owner's hash and both original keys."""
    from tgdrive import mongo

    owner_hash = PasswordHasher().hash("original synthetic owner password")
    bootstrap = {
        "API_ID": 12345,
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30,
        "STORAGE_CHANNEL": -1001234567890,
        "ADMIN_PASSWORD_HASH": owner_hash,
        "SESSION_SECRET": "s" * 48,
        "MONGODB_BACKUP_KEY": "b" * 64,
    }
    fake = SimpleNamespace(
        db=SimpleNamespace(
            settings=SimpleNamespace(find_one=lambda _: {"document": bootstrap})
        )
    )
    monkeypatch.setenv("TGDRIVE_AUTO_SECRETS", "true")
    monkeypatch.setenv("MONGODB_URI", "mongodb://synthetic.invalid")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    for key in ("SESSION_SECRET", "MONGODB_BACKUP_KEY", "ADMIN_PASSWORD_HASH"):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(mongo, "configure", lambda *_args: fake)
    monkeypatch.setattr("tgdrive.managed_settings.read_document", lambda _: None)
    settings = Settings(_env_file=None)
    assert settings.admin_password_hash.get_secret_value() == owner_hash
    assert settings.session_secret.get_secret_value() == bootstrap["SESSION_SECRET"]
    assert settings.mongodb_backup_key.get_secret_value() == "b" * 64
    assert not (tmp_path / "deployment-secrets.json").exists()


def test_new_container_can_generate_keys_before_explicit_initialization(
    tmp_path, monkeypatch
):
    from tgdrive import mongo

    for key, value in {
        "TGDRIVE_AUTO_SECRETS": "true",
        "MONGODB_URI": "mongodb://synthetic.invalid",
        "DATA_DIR": str(tmp_path),
        "API_ID": "12345",
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30,
        "STORAGE_CHANNEL": "-1001234567890",
        "ADMIN_PASSWORD": "new synthetic owner password",
        "ADMIN_PASSWORD_HASH": "",
        "SESSION_SECRET": "",
        "MONGODB_BACKUP_KEY": "",
    }.items():
        monkeypatch.setenv(key, value)
    fake = SimpleNamespace(
        db=SimpleNamespace(
            settings=SimpleNamespace(find_one=lambda _: None),
            catalog=SimpleNamespace(find_one=lambda _: None),
        )
    )
    monkeypatch.setattr(mongo, "configure", lambda *_args: fake)
    monkeypatch.setattr("tgdrive.managed_settings.read_document", lambda _: None)
    first = Settings(_env_file=None)
    second = Settings(_env_file=None)
    assert first.session_secret == second.session_secret
    assert first.mongodb_backup_key == second.mongodb_backup_key
    assert first.admin_password_hash is None


def recovery_configuration(tmp_path, monkeypatch, **overrides):
    values = {
        "API_ID": 12345,
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30,
        "STORAGE_CHANNEL": -1001234567890,
        "ADMIN_PASSWORD_HASH": PasswordHasher().hash("original owner password"),
        "SESSION_SECRET": "s" * 48,
    }
    environment = {
        "MONGODB_URI": "mongodb://synthetic.invalid",
        "DATA_DIR": str(tmp_path),
        "ADMIN_PASSWORD": "",
        "ADMIN_PASSWORD_HASH": "",
        "SESSION_SECRET": "",
        "MONGODB_BACKUP_KEY": "",
        **overrides,
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr("tgdrive.replica_bootstrap.load_bootstrap", lambda _: values)
    return values


def test_explicit_rotation_is_not_hidden_by_local_recovery_copy(tmp_path, monkeypatch):
    bootstrap = recovery_configuration(
        tmp_path,
        monkeypatch,
        ADMIN_PASSWORD="replacement owner password",
        SESSION_SECRET="replacement-signing-secret-" * 3,
        STORAGE_CHANNEL=-1009876543210,
    )
    backend = SimpleNamespace(
        db=SimpleNamespace(
            settings=SimpleNamespace(find_one=lambda _: {"document": bootstrap})
        )
    )
    monkeypatch.setattr("tgdrive.mongo.configure", lambda *_args: backend)
    monkeypatch.setattr("tgdrive.managed_settings.read_document", lambda _: None)
    settings = Settings(_env_file=None)
    assert settings.storage_channel == -1009876543210
    assert (
        settings.session_secret.get_secret_value() == "replacement-signing-secret-" * 3
    )
    assert settings.admin_password.get_secret_value() == "replacement owner password"
    assert settings.admin_password_hash is None


def test_database_outage_reuses_saved_keys_for_recovery_shell(tmp_path, monkeypatch):
    bootstrap = recovery_configuration(
        tmp_path, monkeypatch, TGDRIVE_AUTO_SECRETS="true"
    )
    keys = installation_secrets(tmp_path, {}, existing_drive=False)
    bootstrap["SESSION_SECRET"] = keys["SESSION_SECRET"]

    def unavailable(_query):
        raise ConnectionError("synthetic database outage")

    backend = SimpleNamespace(
        db=SimpleNamespace(settings=SimpleNamespace(find_one=unavailable))
    )
    monkeypatch.setattr("tgdrive.mongo.configure", lambda *_args: backend)
    monkeypatch.setattr(
        "tgdrive.managed_settings.read_document",
        lambda _: pytest.fail("Do not query unavailable MongoDB a second time"),
    )
    settings = Settings(_env_file=None)
    assert settings.session_secret.get_secret_value() == keys["SESSION_SECRET"]
    assert settings.mongodb_backup_key.get_secret_value() == keys["MONGODB_BACKUP_KEY"]
    assert (
        settings.admin_password_hash.get_secret_value()
        == bootstrap["ADMIN_PASSWORD_HASH"]
    )


def test_database_outage_does_not_generate_replacement_recovery_keys(
    tmp_path, monkeypatch
):
    recovery_configuration(tmp_path, monkeypatch, TGDRIVE_AUTO_SECRETS="true")

    def unavailable(_query):
        raise ConnectionError("synthetic database outage")

    monkeypatch.setattr(
        "tgdrive.mongo.configure",
        lambda *_args: SimpleNamespace(
            db=SimpleNamespace(settings=SimpleNamespace(find_one=unavailable))
        ),
    )
    with pytest.raises(ValueError, match=r"original.*MONGODB_BACKUP_KEY"):
        Settings(_env_file=None)
    assert not (tmp_path / "deployment-secrets.json").exists()


def test_proxy_discovery_trusts_only_resolved_private_addresses(monkeypatch):
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setenv("TGDRIVE_PROXY_HOSTS", "coolify-proxy")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            for address in ("10.0.2.2", "10.0.2.2", "8.8.8.8", "0.0.0.0")  # noqa: S104
            if host == "coolify-proxy"
        ],
    )
    assert set(trusted_proxy_ips().split(",")) == {"127.0.0.1", "::1", "10.0.2.2"}


def test_proxy_discovery_failure_does_not_trust_the_network(monkeypatch, caplog):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "auto")
    monkeypatch.setenv("TGDRIVE_PROXY_HOSTS", "coolify-proxy")

    def unavailable(*_args, **_kwargs):
        raise socket.gaierror("synthetic DNS failure")

    monkeypatch.setattr(socket, "getaddrinfo", unavailable)
    assert set(trusted_proxy_ips().split(",")) == {"127.0.0.1", "::1"}
    assert "could not be resolved" in caplog.text


def test_explicit_proxy_configuration_is_preserved(monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1,10.0.2.0/24")
    assert trusted_proxy_ips() == "127.0.0.1,10.0.2.0/24"


def test_container_launcher_keeps_one_worker_and_proxy_configuration(monkeypatch):
    monkeypatch.delenv("TGDRIVE_AUTO_SECRETS", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    called = {}
    monkeypatch.setattr(
        "tgdrive.server.uvicorn.run",
        lambda app, **kwargs: called.update(app=app, **kwargs),
    )
    main()
    assert called["app"] == "main:app"
    assert called["host"] == "0.0.0.0"  # noqa: S104
    assert called["workers"] == 1
    assert called["proxy_headers"] is True
    assert called["forwarded_allow_ips"] == "127.0.0.1"
    assert called["timeout_graceful_shutdown"] == 45


def test_new_container_waits_for_explicit_init_without_writing_database(monkeypatch):
    catalog_reads = iter([None, None, {"_id": "catalog"}])
    backend = SimpleNamespace(
        db=SimpleNamespace(
            catalog=SimpleNamespace(find_one=lambda _: next(catalog_reads)),
            runtime=SimpleNamespace(find_one=lambda _: None),
        )
    )
    monkeypatch.setenv("TGDRIVE_AUTO_SECRETS", "true")
    monkeypatch.setenv("MONGODB_URI", "mongodb://synthetic.invalid")
    monkeypatch.setattr("tgdrive.mongo.configure", lambda *_args: backend)
    waits = []
    monkeypatch.setattr("tgdrive.server.time.sleep", waits.append)
    wait_for_initialization()
    assert waits == [5]


def test_existing_channel_recovery_bootstrap_does_not_wait_for_missing_mongo(
    tmp_path, monkeypatch
):
    from tgdrive.replica_state import atomic_private

    atomic_private(tmp_path / "replica-bootstrap.json", b"{}")
    monkeypatch.setenv("TGDRIVE_AUTO_SECRETS", "true")
    monkeypatch.setenv("MONGODB_URI", "mongodb://synthetic.invalid")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def must_not_connect(*_args):
        raise AssertionError("Recovery bootstrap must reach owner HTTP startup")

    monkeypatch.setattr("tgdrive.mongo.configure", must_not_connect)
    wait_for_initialization()


def test_existing_mongo_settings_without_catalog_reach_recovery_shell(
    tmp_path, monkeypatch
):
    backend = SimpleNamespace(
        db=SimpleNamespace(
            catalog=SimpleNamespace(find_one=lambda _: None),
            settings=SimpleNamespace(find_one=lambda _: {"_id": "bootstrap"}),
        )
    )
    monkeypatch.setenv("TGDRIVE_AUTO_SECRETS", "true")
    monkeypatch.setenv("MONGODB_URI", "mongodb://synthetic.invalid")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("tgdrive.mongo.configure", lambda *_args: backend)
    monkeypatch.setattr(
        "tgdrive.server.time.sleep", lambda _: pytest.fail("Must not wait for init")
    )
    wait_for_initialization()


def test_hosted_and_local_compose_resolve_without_real_credentials(tmp_path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose is needed for deployment configuration validation")
    root = Path(__file__).resolve().parents[1]
    for name in (
        "compose.yaml",
        "compose.override.yaml",
        "compose.server.yaml",
    ):
        shutil.copyfile(root / name, tmp_path / name)
    fixture = {
        "MONGODB_URI": "mongodb://synthetic.invalid",
        "API_ID": "12345",
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30,
        "STORAGE_CHANNEL": "-1001234567890",
        "ADMIN_PASSWORD": "synthetic owner password",
        "SESSION_SECRET": "s" * 48,
        "MONGODB_BACKUP_KEY": "b" * 64,
    }
    (tmp_path / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in fixture.items())
    )
    environment = {**os.environ, **fixture}
    environment.pop("COMPOSE_FILE", None)
    environment.pop("BIND_ADDRESS", None)
    environment.pop("PORT", None)
    environment.pop("TGDRIVE_RESTART_POLICY", None)
    environment.pop("COOKIE_SECURE", None)
    policy_defaults = {
        key.upper(): str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in PerformancePreferences().model_dump().items()
    }
    # MAX_FILE_SIZE is an optional override; absent means use staging capacity.
    policy_defaults.pop("MAX_FILE_SIZE")
    policy_defaults["TELEGRAM_UPLOADS_PER_BOT"] = "1"
    policy_defaults["DEMO_MODE"] = "false"
    for key in policy_defaults:
        environment.pop(key, None)

    def config(*files):
        arguments = [docker, "compose"]
        for name in files:
            arguments.extend(["-f", name])
        result = subprocess.run(  # noqa: S603 - fixed CLI and synthetic config only
            [*arguments, "config", "--format", "json"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if "is not a docker command" in result.stderr:
            pytest.skip("Docker Compose plugin is not installed")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["services"]["app"]

    hosted = config("compose.yaml")
    assert not hosted.get("ports")
    assert hosted["environment"]["TGDRIVE_PROXY_HOSTS"] == "coolify-proxy"
    assert hosted["environment"]["SESSION_SECRET"] == fixture["SESSION_SECRET"]
    assert hosted["environment"]["COOKIE_SECURE"] == "true"
    assert hosted["restart"] == "unless-stopped"
    local = config()
    assert local["ports"][0]["host_ip"] == "127.0.0.1"
    assert str(local["ports"][0]["published"]) == "8000"
    assert local["environment"]["SESSION_SECRET"] == fixture["SESSION_SECRET"]
    assert local["environment"]["TGDRIVE_PROXY_HOSTS"] == ""
    assert local["environment"]["COOKIE_SECURE"] == "false"
    assert local["restart"] == "no"
    server = config("compose.yaml", "compose.server.yaml")
    assert server["ports"][0]["host_ip"] == "127.0.0.1"
    assert server["environment"]["COOKIE_SECURE"] == "true"
    assert server["environment"]["TGDRIVE_PROXY_HOSTS"] == ""
    assert server["restart"] == "unless-stopped"
    for deployment in (hosted, local, server):
        for key, value in policy_defaults.items():
            assert deployment["environment"][key] == value

    # Optional settings supplied by an operator must survive Compose rendering,
    # including zero and false, rather than being replaced by app fallbacks.
    overrides = {
        "DEMO_MODE": "true",
        "REMOTE_DOWNLOAD_CONCURRENCY": "3",
        "CACHE_MEMORY_BYTES": "536870912",
        "CACHE_DISK_BYTES": "0",
        "AUTO_PRELOAD": "false",
        "UPLOAD_SPLIT_MIB": "0",
        "TIMEZONE": "UTC",
    }
    environment.update(overrides)
    for files in (("compose.yaml",), (), ("compose.yaml", "compose.server.yaml")):
        rendered = config(*files)["environment"]
        assert {key: rendered[key] for key in overrides} == overrides
