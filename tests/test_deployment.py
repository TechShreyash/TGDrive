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


def test_hosted_and_local_compose_resolve_without_real_credentials(tmp_path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose is needed for deployment configuration validation")
    root = Path(__file__).resolve().parents[1]
    for name in ("compose.yaml", "compose.override.yaml", "compose.coolify.yaml"):
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
    assert "SESSION_SECRET" not in hosted["environment"]
    assert "ADMIN_PASSWORD_HASH" not in hosted["environment"]
    local = config()
    assert local["ports"][0]["host_ip"] == "127.0.0.1"
    assert str(local["ports"][0]["published"]) == "8000"
    assert local["environment"]["SESSION_SECRET"] == fixture["SESSION_SECRET"]
    assert local["environment"]["TGDRIVE_PROXY_HOSTS"] == ""
    assert not config("compose.coolify.yaml").get("ports")
