"""Guided local Docker setup. Run with Linux/WSL Python 3.10 or newer."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

IMAGE = "tgdrive:local"
DOCKER_HELP = "https://docs.docker.com/engine/install/ubuntu/"
COMPOSE_HELP = "https://docs.docker.com/compose/install/linux/"
ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r"[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}")


class SetupError(RuntimeError):
    pass


class Commands:
    def __init__(self, root: Path, project: str):
        self.root = root
        self.project = project

    def run(self, args: list[str], *, private_input=None, stream=False, timeout=120):
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, never a shell
                args,
                cwd=self.root,
                input=private_input,
                text=True,
                capture_output=not stream,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SetupError(
                "Docker was not found. Install it first: " + DOCKER_HELP
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SetupError(
                "The Docker operation timed out. State is preserved; check "
                "Docker/network and retry."
            ) from exc
        if result.returncode:
            if not stream:
                try:
                    message = json.loads(result.stdout)
                except (ValueError, TypeError):
                    message = None
                if isinstance(message, dict) and isinstance(message.get("error"), str):
                    raise SetupError(message["error"])
                if "permission denied" in (result.stderr or "").lower():
                    raise SetupError(
                        "Docker permission was denied. Use Docker's documented "
                        "non-root/rootless setup; do not run this wizard with sudo."
                    )
            raise SetupError(
                "Docker could not complete this step. Check that its Linux daemon "
                "is running and reachable. Existing configuration/data are preserved."
            )
        return result.stdout or ""

    def compose(self, *args: str, **kwargs):
        return self.run(
            ["docker", "compose", "--project-name", self.project, *args], **kwargs
        )

    def helper(self, *args: str) -> dict[str, Any]:
        output = self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "app",
            "python",
            "-m",
            "tgdrive.setup",
            *args,
            timeout=180,
        )
        try:
            result = json.loads(output)
        except ValueError as exc:
            raise SetupError(
                "The setup helper returned an unexpected response; preserve state "
                "and check the image build."
            ) from exc
        if not isinstance(result, dict):
            raise SetupError("The setup helper returned an unexpected response.")
        return result


def preflight(commands: Commands) -> None:
    if sys.platform != "linux":
        raise SetupError(
            "Run this wizard in a Linux terminal or WSL Ubuntu, using Linux Python "
            "and Docker."
        )
    if sys.version_info < (3, 10):  # noqa: UP036 - host wizard supports 3.10
        raise SetupError(
            "The setup wizard needs host Python 3.10 or newer. Docker supplies the "
            "application runtime."
        )
    if os.geteuid() == 0:
        raise SetupError(
            "Run as your normal user, with Docker access configured; do not use sudo "
            "for this wizard."
        )
    if shutil.which("docker") is None:
        raise SetupError(
            "Docker is not installed or on PATH. Install it first: " + DOCKER_HELP
        )
    commands.run(["docker", "--version"])
    try:
        commands.run(["docker", "compose", "version", "--short"])
    except SetupError as exc:
        raise SetupError(
            "The Docker Compose plugin is unavailable. Install it first: "
            + COMPOSE_HELP
        ) from exc
    try:
        commands.run(["docker", "buildx", "version"])
    except SetupError as exc:
        raise SetupError(
            "Docker Buildx is unavailable. Install Docker's buildx plugin with its "
            "official Engine packages."
        ) from exc
    operating_system = commands.run(["docker", "info", "--format", "{{.OSType}}"])
    if operating_system.strip() != "linux":
        raise SetupError(
            "Select a running Linux Docker engine; Windows containers are unsupported."
        )
    endpoint = commands.run(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
    ).strip()
    override = os.environ.get("DOCKER_HOST", endpoint)
    if not override.startswith("unix://"):
        raise SetupError(
            "Run setup on the Docker host itself using its local Unix socket. Remote "
            "Docker contexts need manual setup."
        )
    for name in (
        "Dockerfile",
        "compose.yaml",
        "compose.override.yaml",
        ".dockerignore",
        "tgdrive",
    ):
        if not (commands.root / name).exists():
            raise SetupError(
                "Run the wizard from a complete TGDrive source checkout/release."
            )


def channel_id(value: str) -> int:
    value = value.strip()
    if value.startswith("https://"):
        parsed = urlsplit(value)
        match = re.fullmatch(
            r"/c/([1-9][0-9]*)/[1-9][0-9]*(?:/[1-9][0-9]*)?", parsed.path
        )
        if parsed.netloc != "t.me" or parsed.query or parsed.fragment or match is None:
            raise SetupError(
                "Paste a private channel message link like "
                "https://t.me/c/1234567890/1, or its negative numeric channel ID."
            )
        value = "-100" + match.group(1)
    if not re.fullmatch(r"-[1-9][0-9]*", value):
        raise SetupError(
            "Use a negative numeric channel ID, or a copied private channel message "
            "link."
        )
    result = int(value)
    if not -(2**63) <= result < 0:
        raise SetupError("The channel ID is outside the supported range.")
    return result


def private_prompt(prompt: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning as exc:
        raise SetupError(
            "This terminal cannot hide input. Use an interactive Linux/SSH "
            "terminal; credentials were not requested with echo enabled."
        ) from exc


def ask_valid(prompt: str, validate, *, hidden=False):
    while True:
        raw = private_prompt(prompt) if hidden else input(prompt)
        try:
            return validate(raw)
        except (ValueError, SetupError) as exc:
            print(str(exc))


def choose_port() -> int:
    def available(raw):
        if not re.fullmatch(r"[0-9]{1,5}", raw or "8000"):
            raise SetupError("Enter a port number from 1024 to 65535.")
        value = int(raw or "8000")
        if not 1024 <= value <= 65535:
            raise SetupError("Choose a port from 1024 to 65535.")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", value))
        except OSError as exc:
            raise SetupError(
                "That local port is already in use. Choose another port; an existing "
                "TGDrive may already be running."
            ) from exc
        return value

    return ask_valid("Local web port [8000]: ", available)


def collect_credentials() -> tuple[dict[str, str], str]:
    print(
        "Get your API ID/hash at https://my.telegram.org/apps and a bot token from "
        "@BotFather."
    )
    print(
        "Add the bot to a NEW dedicated private channel, or the existing channel you "
        "are recovering."
    )
    print("Grant channel administrator rights to post and delete messages.")
    print(
        "Copy a message link inside that channel (https://t.me/c/…/…) to identify it "
        "without forwarding to another bot."
    )

    def api_id(raw):
        if not raw.isdecimal() or not 0 < int(raw) < 2**31:
            raise SetupError(
                "API ID must be a positive integer from Telegram's API page."
            )
        return str(int(raw))

    def api_hash(raw):
        if re.fullmatch(r"[A-Fa-f0-9]{32}", raw.strip()) is None:
            raise SetupError("API hash must contain 32 hexadecimal characters.")
        return raw.strip()

    def bot_token(raw):
        if TOKEN.fullmatch(raw.strip()) is None:
            raise SetupError("Paste the complete bot token provided by @BotFather.")
        return raw.strip()

    def mongodb_uri(raw):
        raw = raw.strip()
        if not raw.startswith(("mongodb+srv://", "mongodb://")):
            raise SetupError("Paste a MongoDB connection URI from Atlas.")
        return raw

    values = {
        "MONGODB_URI": ask_valid(
            "MongoDB connection URI (hidden): ", mongodb_uri, hidden=True
        ),
        "MONGODB_DATABASE": "tgdrive",
        "API_ID": ask_valid("Telegram API ID (hidden): ", api_id, hidden=True),
        "API_HASH": ask_valid("Telegram API hash (hidden): ", api_hash, hidden=True),
        "BOT_TOKENS": ask_valid("Storage bot token (hidden): ", bot_token, hidden=True),
        "STORAGE_CHANNEL": str(
            ask_valid(
                "Channel ID or private message link (hidden): ", channel_id, hidden=True
            )
        ),
    }
    while True:
        password = private_prompt(
            "Choose your web login password (12-256 characters): "
        )
        if not 12 <= len(password) <= 256:
            print("Use a password with 12-256 characters.")
            continue
        if password != private_prompt("Repeat your web login password: "):
            print("The passwords did not match; try again.")
            continue
        return values, password


def create_configuration(
    root: Path, values: dict[str, str], password_hash: str, port: int
) -> None:
    if (
        not password_hash.startswith("$argon2id$")
        or "\n" in password_hash
        or "'" in password_hash
    ):
        raise SetupError(
            "The container did not return a valid password hash; no configuration "
            "was written."
        )
    configuration = dict(values)
    configuration.update(
        ADMIN_PASSWORD="",
        ADMIN_PASSWORD_HASH=password_hash,
        SESSION_SECRET=secrets.token_urlsafe(48),
        MONGODB_BACKUP_KEY=secrets.token_hex(32),
        COOKIE_SECURE="false",
        INITIALIZE_EMPTY_DRIVE="false",
        BIND_ADDRESS="127.0.0.1",
        PORT=str(port),
        DATA_DIR="data",
        CACHE_DIR="cache",
    )
    for value in configuration.values():
        if any(character in value for character in ("'", "\n", "\r", "\x00")):
            raise SetupError(
                "Configuration contained an unsupported character; nothing was written."
            )
    content = "# Generated locally by scripts/setup.py; keep this file private.\n"
    content += (
        "\n".join(f"{key}='{value}'" for key, value in configuration.items()) + "\n"
    )
    target = root / ".env"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def choose_mode(status: dict[str, Any]) -> str:
    if status.get("backend") == "mongodb":
        if status.get("local_state") == "healthy":
            print("Your existing MongoDB drive will be reused.")
            return "existing"
        print("The MongoDB database is empty.")
        if input("Type NEW to create a new drive: ").strip() != "NEW":
            raise SetupError("Stopped. No drive was initialized.")
        return "new"
    if status.get("local_state") == "healthy":
        print("An existing local Docker drive was found and preserved.")
        if input("Type REUSE to open that existing drive: ").strip() != "REUSE":
            raise SetupError(
                "Stopped. Existing data/configuration are preserved; no drive was "
                "initialized."
            )
        return "existing"
    if status.get("has_pin"):
        print(
            "This channel already has a pinned message. A new drive will not be "
            "created here."
        )
        if not status.get("checkpoint_candidate"):
            raise SetupError(
                "The current pin is not a TGDrive recovery checkpoint. Use a known "
                "drive.json message ID via the recovery guide, or a new dedicated "
                "channel."
            )
        if (
            input(
                "Type RECOVER to restore the verified checkpoint into the missing "
                "local database: "
            ).strip()
            != "RECOVER"
        ):
            raise SetupError("Stopped. No drive was created or restored.")
        return "recover"
    print(
        "No current pin was found. This does NOT prove the channel is empty or has "
        "no previous drive."
    )
    print(
        "If this is an older drive, stop and recover a known drive.json message ID "
        "using docs/operations.md."
    )
    print(
        "Continue only if you created this dedicated channel for a genuinely NEW drive."
    )
    if (
        input("Type NEW to confirm this is your new dedicated channel: ").strip()
        != "NEW"
    ):
        raise SetupError(
            "Stopped. No drive was initialized. See the recovery guide for existing "
            "channels."
        )
    return "new"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def wait_ready(url: str, *, timeout=90) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise SetupError("Readiness probes must use the local loopback address.")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(url + "/healthz", timeout=3) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    return False


def setup(commands: Commands) -> str:
    target = commands.root / ".env"
    if target.exists() or target.is_symlink():
        raise SetupError(
            "Existing .env was preserved. For a configured installation use "
            "docker compose up -d. If local data is missing, use the explicit "
            "recover procedure in docs/operations.md; do not rerun init."
        )
    port = choose_port()
    values, password = collect_credentials()
    print("Building TGDrive's Docker image. The first build can take several minutes.")
    commands.run(["docker", "build", "--tag", IMAGE, "."], stream=True, timeout=3600)
    output = commands.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            IMAGE,
            "python",
            "-m",
            "tgdrive.setup",
            "password-hash",
        ],
        private_input=json.dumps({"password": password}),
    )
    del password
    try:
        hashed = json.loads(output)["hash"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SetupError(
            "The password helper returned an invalid response. No configuration was "
            "written."
        ) from exc
    create_configuration(commands.root, values, hashed, port)
    print(
        "Private .env saved with mode 0600. Password/hash/session secret were not "
        "printed."
    )
    status = commands.helper("inspect")
    mode = choose_mode(status)
    print("Preparing the database and checking Telegram access…")
    confirmed = commands.helper("prepare", "--mode", mode)
    if confirmed.get("state") != "protected":
        raise SetupError(
            "Recovery protection was not confirmed. State is preserved; the app was "
            "not started."
        )
    commands.compose("up", "-d", "--no-build", timeout=180)
    url = f"http://127.0.0.1:{port}"
    if not wait_ready(url):
        raise SetupError(
            "The app did not become ready within 90 seconds. State is preserved. "
            "Inspect docker compose ps/logs locally and see docs/operations.md; "
            "do not paste unreviewed logs or your .env into support chats."
        )
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check Docker prerequisites without changing configuration or data",
    )
    parser.add_argument(
        "--project-name",
        default="tgdrive",
        help="Compose project/volume namespace (default: tgdrive)",
    )
    args = parser.parse_args()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.project_name) is None:
        parser.error("Use a lowercase Docker Compose project name.")
    try:
        commands = Commands(ROOT, args.project_name)
        preflight(commands)
        if args.check:
            print(
                "Docker Engine, Compose, and Buildx are available. No uv, Node, or "
                "host application Python environment is needed."
            )
            return 0
        if not sys.stdin.isatty():
            raise SetupError(
                "Run in an interactive Linux terminal so credential/password prompts "
                "stay private."
            )
        url = setup(commands)
        print("TGDrive is ready. Sign in with the web password you chose.")
        print("Open " + url)
        print(
            "On a remote server, use an SSH tunnel first: ssh -L PORT:127.0.0.1:PORT "
            "user@server (replace PORT with the chosen port)."
        )
        print(
            "Keep .env and both Docker volumes. Add HTTPS before exposing the "
            "service publicly; see docs/setup.md."
        )
        return 0
    except (SetupError, PermissionError, FileExistsError) as exc:
        print("Setup stopped: " + str(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(
            "Setup stopped. Existing configuration and data were preserved.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
