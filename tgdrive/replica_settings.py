"""Bounded saved-domain settings carried inside encrypted recovery capsules."""

from __future__ import annotations

import json

from tgdrive.managed_settings import (
    MAX_SETTINGS_BYTES,
    ManagedDocument,
    initial_clients,
    read_document,
)
from tgdrive.performance_preferences import PerformancePreferences, PerformanceSettings
from tgdrive.replica_state import read_private
from tgdrive.transfer_preferences import TransferPreferences, UploadPreferences

POLICIES = {
    "performance-preferences.json": PerformancePreferences,
    "transfer-preferences.json": UploadPreferences,
}
MAX_SETTINGS_TOTAL = 256 * 1024


def collect_credentials(settings, runtime):
    """Capture saved application policies, never deployment paths or DB URI.

    This synchronous snapshot should run in a worker thread. Schema validation
    and byte bounds apply even to local files; a malformed saved policy cannot
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
    credentials["managed_document"] = json.loads(document.private_bytes())
    backend = getattr(runtime, "mongo", None)
    preferences = {}
    for filename, model in POLICIES.items():
        if backend is not None:
            row = backend.db.settings.find_one({"_id": filename.removesuffix(".json")})
            raw = row["document"] if row else None
        else:
            path = settings.resolved_data_dir / filename
            raw = (
                json.loads(read_private(path, maximum=MAX_SETTINGS_BYTES))
                if path.exists()
                else None
            )
        if raw is None:
            if model is PerformancePreferences:
                raw = PerformanceSettings(settings, runtime).value.model_dump()
            else:
                raw = {"uploads_per_bot": TransferPreferences(settings, runtime)._value}
        if len(json.dumps(raw, allow_nan=False).encode()) > MAX_SETTINGS_BYTES:
            raise ValueError("Saved application policy exceeds the recovery size limit")
        if model is PerformancePreferences:
            raw = PerformanceSettings(settings, runtime).value.model_dump() | raw
        preferences[filename] = model.model_validate(raw).model_dump()
    credentials["preferences"] = preferences
    key = getattr(settings, "mongodb_backup_key", None)
    if key is not None:
        credentials["mongodb_backup_key"] = key.get_secret_value()
    if len(json.dumps(credentials, allow_nan=False).encode()) > MAX_SETTINGS_TOTAL:
        raise ValueError("Saved recovery settings exceed the bounded size limit")
    return credentials


def validate_optional_credentials(credentials):
    """Validate optional newer fields while retaining older four-field capsules."""
    if "managed_document" in credentials:
        raw = credentials["managed_document"]
        if len(json.dumps(raw, allow_nan=False).encode()) > MAX_SETTINGS_BYTES:
            raise ValueError
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
        if not isinstance(raw, dict) or set(raw) - set(POLICIES):
            raise ValueError
        for filename, value in raw.items():
            if len(json.dumps(value, allow_nan=False).encode()) > MAX_SETTINGS_BYTES:
                raise ValueError
            POLICIES[filename].model_validate(value)
    if "mongodb_archive" in credentials:
        import re

        from tgdrive.store import _validate_timestamp

        archive = credentials["mongodb_archive"]
        if not isinstance(archive, dict) or set(archive) != {
            "format",
            "id",
            "revision",
            "source_channel",
            "source_message_id",
            "replica_channel",
            "replica_message_id",
            "size",
            "sha256",
            "created_at",
        }:
            raise ValueError
        if archive["format"] != "mongodb-encrypted-v1":
            raise ValueError
        if (
            not isinstance(archive["id"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", archive["id"]) is None
            or type(archive["revision"]) is not int
            or not 0 < archive["revision"] < 2**63
            or type(archive["size"]) is not int
            or not 0 < archive["size"] <= 1024**3
            or not isinstance(archive["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", archive["sha256"]) is None
        ):
            raise ValueError
        for name in ("source_channel", "replica_channel"):
            if type(archive[name]) is not int or not -(1 << 63) <= archive[name] < 0:
                raise ValueError
        for name in ("source_message_id", "replica_message_id"):
            if type(archive[name]) is not int or not 0 < archive[name] < 2**31:
                raise ValueError
        if archive["source_channel"] == archive["replica_channel"]:
            raise ValueError
        _validate_timestamp(archive["created_at"])
        if "mongodb_backup_key" not in credentials:
            raise ValueError
    if "mongodb_backup_key" in credentials:
        import re

        key = credentials["mongodb_backup_key"]
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-fA-F]{64}", key) is None:
            raise ValueError
    if len(json.dumps(credentials, allow_nan=False).encode()) > MAX_SETTINGS_TOTAL:
        raise ValueError
