"""Bounded saved-domain settings carried inside encrypted recovery capsules."""

from __future__ import annotations

import json

from tgdrive.settings.bot_settings import BotUploadPreferences
from tgdrive.settings.managed_settings import (
    ManagedDocument,
    initial_clients,
    read_document,
)
from tgdrive.settings.performance_preferences import (
    PerformancePreferences,
    PerformanceSettings,
)
from tgdrive.settings.transfer_preferences import TransferPreferences, UploadPreferences

POLICIES = {
    "performance-preferences.json": PerformancePreferences,
    "transfer-preferences.json": UploadPreferences,
    "bot-preferences.json": BotUploadPreferences,
}


def collect_credentials(settings, runtime):
    """Capture saved application policies, never deployment paths or DB URI.

    This synchronous snapshot should run in a worker thread. Schema validation
    and byte bounds apply to saved records; a malformed saved policy cannot
    silently be omitted from a claimed complete recovery capsule.
    """
    credentials = {
        "api_id": settings.api_id,
        "api_hash": settings.api_hash.get_secret_value(),
        "session_secret": settings.session_secret.get_secret_value(),
        "bot_tokens": list(settings.bot_tokens),
    }
    document = read_document(settings.resolved_data_dir)
    if document is None:
        document = ManagedDocument(
            revision=max(1, settings.managed_revision),
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            clients=initial_clients(settings),
        )
    document = document.model_copy(
        update={
            "clients": [client for client in document.clients if client.kind == "bot"]
        }
    )
    credentials["managed_document"] = json.loads(document.private_bytes())
    backend = runtime.mongo
    preferences = {}
    for filename, model in POLICIES.items():
        row = backend.db.settings.find_one({"_id": filename.removesuffix(".json")})
        raw = row["document"] if row else None
        if raw is None:
            if model is PerformancePreferences:
                raw = PerformanceSettings(settings, runtime).value.model_dump()
            elif model is BotUploadPreferences:
                raw = BotUploadPreferences().model_dump()
            else:
                raw = {"uploads_per_bot": TransferPreferences(settings, runtime)._value}
        if model is PerformancePreferences:
            raw = PerformanceSettings(settings, runtime).value.model_dump() | raw
        preferences[filename] = model.model_validate(raw).model_dump()
    credentials["preferences"] = preferences
    return credentials


def credentials_from_snapshot(settings, records):
    """Use the same captured database generation as the recovery catalog."""
    rows = {row["_id"]: row["document"] for row in records["settings"]}
    raw = rows.get("telegram")
    document = (
        ManagedDocument.model_validate(raw)
        if raw is not None
        else ManagedDocument(
            revision=max(1, settings.managed_revision),
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            clients=initial_clients(settings),
        )
    )
    # Account sessions are live account access, not necessary drive recovery data.
    document = document.model_copy(
        update={
            "clients": [client for client in document.clients if client.kind == "bot"]
        }
    )
    document = ManagedDocument.model_validate(json.loads(document.private_bytes()))
    credentials = {
        "api_id": settings.api_id,
        "api_hash": settings.api_hash.get_secret_value(),
        "session_secret": settings.session_secret.get_secret_value(),
        "bot_tokens": [
            client.credential.get_secret_value()
            for client in document.clients
            if client.enabled
        ],
        "managed_document": json.loads(document.private_bytes()),
        "preferences": {},
    }
    defaults = {
        "performance-preferences.json": PerformanceSettings(
            settings, None
        ).value.model_dump(),
        "transfer-preferences.json": {
            "uploads_per_bot": getattr(settings, "telegram_uploads_per_bot", 1)
        },
        "bot-preferences.json": BotUploadPreferences().model_dump(),
    }
    for filename, model in POLICIES.items():
        value = defaults[filename] | rows.get(filename.removesuffix(".json"), {})
        credentials["preferences"][filename] = model.model_validate(value).model_dump()
    validate_recovery_settings(credentials)
    return credentials


def validate_recovery_settings(credentials):
    """Validate bounded application settings and optional database archive details."""
    if "managed_document" in credentials:
        raw = credentials["managed_document"]
        # PrivateModel normally allows coercion. Serialized capsules must carry
        # actual scalar types so booleans cannot impersonate API/revision IDs.
        if (
            not isinstance(raw, dict)
            or type(raw.get("api_id")) is not int
            or type(raw.get("revision")) is not int
            or type(raw.get("schema_version")) is not int
            or not isinstance(raw.get("clients"), list)
            or any(
                not isinstance(row, dict) or type(row.get("enabled", True)) is not bool
                for row in raw["clients"]
            )
        ):
            raise ValueError
        document = ManagedDocument.model_validate(raw)
        if json.loads(document.private_bytes()) != raw:
            raise ValueError
    if "preferences" in credentials:
        raw = credentials["preferences"]
        if not isinstance(raw, dict) or set(raw) != set(POLICIES):
            raise ValueError
        for filename, value in raw.items():
            POLICIES[filename].model_validate(value)
