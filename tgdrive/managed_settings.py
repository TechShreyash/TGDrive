"""Private, atomic Telegram connection overrides; never drive metadata."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

MAX_CLIENTS = 50
MAX_SETTINGS_BYTES = 64 * 1024
_BOT = re.compile(r"[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}")
_ID = re.compile(r"[a-z0-9-]{1,80}")


class SettingsProblem(RuntimeError):
    def __init__(self, detail: str, *, code="SETTINGS_INVALID", status=422):
        super().__init__(detail)
        self.code = code
        self.status = status


class PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ManagedClient(PrivateModel):
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["bot", "user"]
    enabled: bool = True
    credential: SecretStr
    runtime_name: str | None = None

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("Invalid connection identifier")
        return value

    @field_validator("label")
    @classmethod
    def valid_label(cls, value: str) -> str:
        value = value.strip()
        if not value or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("Connection labels must contain visible text")
        return value

    @field_validator("credential")
    @classmethod
    def valid_credential(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if not 1 <= len(raw) <= 8192 or any(character.isspace() for character in raw):
            raise ValueError("Connection credential is invalid")
        return SecretStr(raw)


class ManagedDocument(PrivateModel):
    schema_version: Literal[1] = 1
    revision: int = Field(ge=1)
    api_id: int = Field(gt=0, le=2147483647)
    api_hash: SecretStr
    clients: list[ManagedClient] = Field(min_length=1, max_length=MAX_CLIENTS)

    @field_validator("api_hash")
    @classmethod
    def valid_hash(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if not re.fullmatch(r"[a-fA-F0-9]{32}", raw):
            raise ValueError("API hash must contain exactly 32 hexadecimal characters")
        return SecretStr(raw)

    @field_validator("clients")
    @classmethod
    def valid_clients(cls, values: list[ManagedClient]) -> list[ManagedClient]:
        if len({row.id for row in values}) != len(values):
            raise ValueError("Connection identifiers must be unique")
        if not any(row.kind == "bot" and row.enabled for row in values):
            raise ValueError("Keep at least one enabled storage bot")
        for row in values:
            if row.kind == "bot" and not _BOT.fullmatch(
                row.credential.get_secret_value()
            ):
                raise ValueError("A storage bot token is invalid")
        return values

    def private_bytes(self) -> bytes:
        result = self.model_dump(exclude={"api_hash", "clients"})
        result["api_hash"] = self.api_hash.get_secret_value()
        result["clients"] = [
            row.model_dump(exclude={"credential"})
            | {"credential": row.credential.get_secret_value()}
            for row in self.clients
        ]
        raw = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > MAX_SETTINGS_BYTES:
            raise SettingsProblem("Connection settings exceed the 64 KiB limit")
        return raw


def settings_path(data_dir: Path) -> Path:
    return data_dir / "telegram-settings.json"


def read_document(data_dir: Path) -> ManagedDocument | None:
    from tgdrive.mongo import for_directory

    backend = for_directory(data_dir)
    if backend is not None:
        try:
            with backend._settings_lock:
                if not backend._settings_cached:
                    row = backend.db.settings.find_one({"_id": "telegram"})
                    backend._settings_document = (
                        ManagedDocument.model_validate(row["document"]) if row else None
                    )
                    backend._settings_cached = True
                document = backend._settings_document
                return document.model_copy(deep=True) if document is not None else None
        except Exception:
            raise SettingsProblem(
                "Could not read MongoDB connection settings.", status=503
            ) from None
    path = settings_path(data_dir)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise SettingsProblem(
            "Cannot safely open private Telegram settings. Preserve the file and "
            "check DATA_DIR permissions; do not silently replace it.",
            status=503,
        ) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SETTINGS_BYTES:
            raise ValueError
        if info.st_mode & 0o077:
            raise SettingsProblem(
                "Private Telegram settings must have mode 0600. Correct file "
                "permissions before restarting.",
                status=503,
            )
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(MAX_SETTINGS_BYTES + 1)
        document = ManagedDocument.model_validate_json(raw)
        return document
    except SettingsProblem:
        raise
    except Exception:
        raise SettingsProblem(
            "Private Telegram settings are invalid. Preserve telegram-settings.json "
            "and repair it from a trusted local copy; environment credentials were "
            "not substituted.",
            status=503,
        ) from None
    finally:
        os.close(descriptor)


@contextmanager
def settings_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        data_dir / ".telegram-settings.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def save_document(
    data_dir: Path, document: ManagedDocument, expected_revision: int
) -> None:
    from tgdrive.mongo import for_directory

    backend = for_directory(data_dir)
    raw = document.private_bytes()
    if backend is not None:
        from pymongo.errors import DuplicateKeyError

        value = json.loads(raw)
        try:
            result = backend.db.settings.replace_one(
                {"_id": "telegram", "revision": expected_revision},
                {"_id": "telegram", "revision": document.revision, "document": value},
                upsert=expected_revision == 0,
            )
            if expected_revision and result.matched_count != 1:
                raise SettingsProblem(
                    "Connection settings changed. Refresh and retry.",
                    code="SETTINGS_CONFLICT",
                    status=409,
                )
        except DuplicateKeyError:
            raise SettingsProblem(
                "Connection settings changed. Refresh and retry.",
                code="SETTINGS_CONFLICT",
                status=409,
            ) from None
        with backend._settings_lock:
            backend._settings_document = document.model_copy(deep=True)
            backend._settings_cached = True
        return
    with settings_lock(data_dir):
        current = read_document(data_dir)
        if (current.revision if current else 0) != expected_revision:
            raise SettingsProblem(
                "Connection settings changed. Refresh and review your changes.",
                code="SETTINGS_CONFLICT",
                status=409,
            )
        temporary = data_dir / (".telegram-settings-" + secrets.token_hex(16))
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, settings_path(data_dir))
            parent = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            temporary.unlink(missing_ok=True)


def credential_id(kind: str, credential: str) -> str:
    # Opaque source identity; raw credentials never appear in response IDs.
    return kind + "-" + hashlib.sha256(credential.encode()).hexdigest()[:24]


def initial_clients(settings) -> list[ManagedClient]:
    rows = []
    identities = iter(settings.managed_client_ids)
    labels = iter(settings.managed_client_labels)
    for kind, values in (
        ("bot", settings.bot_tokens),
        ("user", settings.string_sessions),
    ):
        for index, value in enumerate(values, 1):
            rows.append(
                ManagedClient(
                    id=next(identities, credential_id(kind, value)),
                    label=next(
                        labels,
                        f"Storage bot {index}"
                        if kind == "bot"
                        else f"Premium account {index}",
                    ),
                    kind=kind,
                    credential=SecretStr(value),
                )
            )
    return rows
