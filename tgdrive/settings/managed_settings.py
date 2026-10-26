"""Private, atomic Telegram connection overrides; never drive metadata."""

from __future__ import annotations

import hashlib
import json
import re
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


def read_document(data_dir: Path) -> ManagedDocument | None:
    from pymongo.errors import PyMongoError

    from tgdrive.storage.mongo import DatabaseUnavailable, for_directory

    backend = for_directory(data_dir)
    if backend is None:
        raise SettingsProblem(
            "Connection settings require a configured MongoDB database.", status=503
        )
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
    except PyMongoError, DatabaseUnavailable:
        raise DatabaseUnavailable(
            "MongoDB is unavailable. Restore database access before starting TGDrive."
        ) from None
    except Exception:
        raise SettingsProblem(
            "Could not read MongoDB connection settings.", status=503
        ) from None


def save_document(
    data_dir: Path, document: ManagedDocument, expected_revision: int
) -> None:
    from tgdrive.storage.mongo import for_directory

    backend = for_directory(data_dir)
    raw = document.private_bytes()
    if backend is None:
        raise SettingsProblem(
            "Connection settings require a configured MongoDB database.", status=503
        )
    from pymongo.errors import DuplicateKeyError

    value = json.loads(raw)
    try:
        result = backend.write(
            backend.db.settings,
            "replace_one",
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
