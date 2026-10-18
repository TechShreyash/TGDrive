"""Safe first-run orchestration without touching real Docker or credentials."""

import json
import socket
import stat
import warnings
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from argon2 import PasswordHasher
from dotenv import dotenv_values

from scripts import setup as wizard
from tgdrive.config import Settings


@pytest.fixture(scope="module")
def owner_hash():
    return PasswordHasher().hash("fixture owner password")


@pytest.fixture
def credentials():
    return {
        "API_ID": "12345",
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30,
        "STORAGE_CHANNEL": "-1001234567890",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-1001234567890", -1001234567890),
        (" https://t.me/c/1234567890/1 ", -1001234567890),
        ("https://t.me/c/1234567890/12/34", -1001234567890),
    ],
)
def test_channel_input_is_converted_without_network(value, expected):
    assert wizard.channel_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "123",
        "0",
        "-0",
        "-9223372036854775809",
        "https://example.com/c/123/1",
        "https://t.me@evil.example/c/123/1",
        "https://t.me/+invite",
        "https://t.me/c/../1",
        "https://t.me/c/123/1/2/3",
        "https://t.me/c/123/0",
    ],
)
def test_ambiguous_channel_input_is_rejected(value):
    with pytest.raises(wizard.SetupError):
        wizard.channel_id(value)


def test_configuration_is_private_validated_and_contains_no_plain_password(
    tmp_path, owner_hash, credentials, capsys
):
    wizard.create_configuration(tmp_path, credentials, owner_hash, 8765)
    path = tmp_path / ".env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    values = dotenv_values(path)
    assert values["ADMIN_PASSWORD"] == ""
    assert values["ADMIN_PASSWORD_HASH"] == owner_hash
    assert len(values["SESSION_SECRET"]) >= 48
    assert "fixture owner password" not in path.read_text()
    configured = Settings(_env_file=None, **values)
    assert configured.cookie_secure is False
    assert configured.initialize_empty_drive is False
    assert values["BIND_ADDRESS"] == "127.0.0.1"
    assert values["PORT"] == "8765"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_configuration_or_symlink_is_never_overwritten(
    tmp_path, owner_hash, credentials, symlink
):
    original = tmp_path / "kept"
    original.write_text("operator-owned content")
    target = tmp_path / ".env"
    if symlink:
        target.symlink_to(original)
    else:
        target.write_bytes(original.read_bytes())
    with pytest.raises(FileExistsError):
        wizard.create_configuration(tmp_path, credentials, owner_hash, 8000)
    assert original.read_text() == "operator-owned content"
    assert target.read_text() == "operator-owned content"


def test_secret_input_is_stdin_and_never_command_arguments(tmp_path, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(wizard.subprocess, "run", run)
    commands = wizard.Commands(tmp_path, "tgdrive-fixture")
    payload = json.dumps({"password": "fixture-private-password"})
    commands.run(["docker", "run", "fixed-image"], private_input=payload)
    args, options = calls[0]
    assert "fixture-private-password" not in " ".join(args)
    assert options["input"] == payload
    assert "shell" not in options
    assert options["capture_output"]


def test_command_errors_do_not_echo_raw_stderr_or_secret_values(tmp_path, monkeypatch):
    monkeypatch.setattr(
        wizard.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="unexpected fixture-private-password"
        ),
    )
    with pytest.raises(wizard.SetupError) as error:
        wizard.Commands(tmp_path, "fixture").run(["docker", "info"])
    assert "fixture-private-password" not in str(error.value)


class FakeCommands(wizard.Commands):
    def __init__(self, root, owner_hash, *, status=None, confirmed=True):
        super().__init__(root, "tgdrive-fixture")
        self.owner_hash = owner_hash
        self.calls = []
        self.status = status or {"local_state": "missing", "has_pin": False}
        self.confirmed = confirmed

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:3] == ["docker", "build", "--tag"]:
            return ""
        if args[-1] == "password-hash":
            assert (
                json.loads(kwargs["private_input"])["password"]
                == "fixture owner password"
            )
            return json.dumps({"hash": self.owner_hash})
        raise AssertionError(args)

    def helper(self, *args):
        self.calls.append((list(args), {}))
        if args == ("inspect",):
            return self.status
        assert args[:2] == ("prepare", "--mode")
        return {"state": "protected" if self.confirmed else "pending"}

    def compose(self, *args, **kwargs):
        self.calls.append((list(args), kwargs))
        assert args == ("up", "-d", "--no-build")


@pytest.mark.parametrize(
    ("status", "answer", "mode"),
    [
        ({"local_state": "missing", "has_pin": False}, "NEW", "new"),
        (
            {"local_state": "missing", "has_pin": True, "checkpoint_candidate": True},
            "RECOVER",
            "recover",
        ),
        ({"local_state": "healthy", "has_pin": True}, "REUSE", "existing"),
    ],
)
def test_complete_isolated_wizard_prepares_explicit_mode_before_starting(
    tmp_path, monkeypatch, owner_hash, credentials, capsys, status, answer, mode
):
    commands = FakeCommands(tmp_path, owner_hash, status=status)
    monkeypatch.setattr(wizard, "choose_port", lambda: 8765)
    monkeypatch.setattr(
        wizard, "collect_credentials", lambda: (credentials, "fixture owner password")
    )
    monkeypatch.setattr("builtins.input", lambda _: answer)
    monkeypatch.setattr(wizard, "wait_ready", lambda _: True)
    assert wizard.setup(commands) == "http://127.0.0.1:8765"
    actions = [args for args, _ in commands.calls]
    assert actions[-2:] == [["prepare", "--mode", mode], ["up", "-d", "--no-build"]]
    assert (tmp_path / ".env").is_file()
    output = capsys.readouterr().out
    for secret in (*credentials.values(), "fixture owner password", owner_hash):
        assert secret not in output


def test_unconfirmed_checkpoint_never_starts_app(
    tmp_path, monkeypatch, owner_hash, credentials
):
    commands = FakeCommands(tmp_path, owner_hash, confirmed=False)
    monkeypatch.setattr(wizard, "choose_port", lambda: 8765)
    monkeypatch.setattr(
        wizard, "collect_credentials", lambda: (credentials, "fixture owner password")
    )
    monkeypatch.setattr("builtins.input", lambda _: "NEW")
    with pytest.raises(wizard.SetupError, match="not confirmed"):
        wizard.setup(commands)
    assert not any(args and args[0] == "up" for args, _ in commands.calls)
    assert (tmp_path / ".env").exists()


def test_existing_env_stops_before_prompts_build_or_data_actions(
    tmp_path, owner_hash, monkeypatch
):
    target = tmp_path / ".env"
    target.write_text("existing private configuration")
    commands = FakeCommands(tmp_path, owner_hash)
    prompt = Mock(side_effect=AssertionError("No prompt permitted"))
    monkeypatch.setattr(wizard, "choose_port", prompt)
    with pytest.raises(wizard.SetupError, match="preserved"):
        wizard.setup(commands)
    assert target.read_text() == "existing private configuration"
    assert commands.calls == []


def test_no_pin_is_not_sufficient_to_automatically_initialize(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    with pytest.raises(wizard.SetupError, match="No drive was initialized"):
        wizard.choose_mode({"local_state": "missing", "has_pin": False})


def test_unrelated_pin_never_offers_new_initialization(monkeypatch):
    prompt = Mock(side_effect=AssertionError("Must refuse before confirmation"))
    monkeypatch.setattr("builtins.input", prompt)
    with pytest.raises(wizard.SetupError, match="not a TGDrive"):
        wizard.choose_mode({"local_state": "missing", "has_pin": True})


def test_busy_port_is_diagnosed_before_configuration_write(monkeypatch, capsys):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        busy = occupied.getsockname()[1]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        answers = iter([str(busy), str(free)])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))
        assert wizard.choose_port() == free
    assert "already in use" in capsys.readouterr().out


def test_doctor_reports_missing_docker_without_mutating_files(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(wizard.shutil, "which", lambda _: None)
    with pytest.raises(wizard.SetupError, match="not installed"):
        wizard.preflight(wizard.Commands(tmp_path, "fixture"))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["compose", "buildx", "daemon", "remote"])
def test_doctor_diagnoses_tool_and_daemon_failures(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(wizard.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(wizard.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    commands = wizard.Commands(tmp_path, "fixture")

    def run(args, **kwargs):
        if failure in {"compose", "buildx"} and args[1] == failure:
            raise wizard.SetupError("not installed")
        if args[1] == "info":
            if failure == "daemon":
                raise wizard.SetupError("daemon not running")
            return "linux"
        if args[1] == "context":
            return (
                "ssh://remote" if failure == "remote" else "unix:///var/run/docker.sock"
            )
        return "ok"

    monkeypatch.setattr(commands, "run", run)
    with pytest.raises(
        wizard.SetupError,
        match={
            "compose": "Compose plugin",
            "buildx": "Buildx",
            "daemon": "daemon",
            "remote": "Remote Docker",
        }[failure],
    ):
        wizard.preflight(commands)


def test_readiness_rejects_external_targets():
    with pytest.raises(wizard.SetupError, match="loopback"):
        wizard.wait_ready("https://external.example")


def test_hidden_prompt_refuses_getpass_echo_fallback(monkeypatch):
    def unsafe_prompt(prompt):
        warnings.warn("Cannot hide input", wizard.getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("Must stop before echo fallback")

    monkeypatch.setattr(wizard.getpass, "getpass", unsafe_prompt)
    with pytest.raises(wizard.SetupError, match="cannot hide input"):
        wizard.private_prompt("Credential: ")
