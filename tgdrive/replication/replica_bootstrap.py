"""Portable recovery credentials validated against the configured MongoDB backend."""

from __future__ import annotations

import json

from argon2 import PasswordHasher

from tgdrive.replication.replica_state import atomic_private, read_private

FIELDS = {
    "API_ID",
    "API_HASH",
    "BOT_TOKENS",
    "STORAGE_CHANNEL",
    "SESSION_SECRET",
    "ADMIN_PASSWORD_HASH",
}


def save_bootstrap(settings):
    password = settings.admin_password_hash
    password_hash = (
        password.get_secret_value()
        if password
        else PasswordHasher().hash(settings.admin_password.get_secret_value())
    )
    values = {
        "API_ID": settings.api_id,
        "API_HASH": settings.api_hash.get_secret_value(),
        "BOT_TOKENS": settings.bot_tokens_csv.get_secret_value(),
        "STORAGE_CHANNEL": settings.storage_channel,
        "SESSION_SECRET": settings.session_secret.get_secret_value(),
        "ADMIN_PASSWORD_HASH": password_hash,
    }
    atomic_private(
        settings.resolved_data_dir / "replica-bootstrap.json",
        json.dumps({"version": 1, "settings": values}).encode(),
    )


def load_bootstrap(directory, *, mongodb_uri, mongodb_database="tgdrive"):
    path = directory / "replica-bootstrap.json"
    if not path.exists():
        return None
    payload = json.loads(read_private(path, maximum=64 * 1024))
    if not isinstance(payload, dict) or set(payload) != {"version", "settings"}:
        raise ValueError("Invalid private recovery bootstrap")
    values = payload["settings"]
    if payload["version"] != 1 or not isinstance(values, dict) or set(values) != FIELDS:
        raise ValueError("Invalid private recovery bootstrap")
    if type(values["API_ID"]) is not int or type(values["STORAGE_CHANNEL"]) is not int:
        raise ValueError("Invalid private recovery bootstrap")
    from tgdrive.core.config import Settings

    # Explicit injected API credentials bypass managed-source lookup; validation
    # never connects to MongoDB or Telegram, and never echoes rejected secrets.
    Settings(
        _env_file=None,
        **values,
        MONGODB_URI=mongodb_uri,
        MONGODB_DATABASE=mongodb_database,
        DATA_DIR=directory,
        CACHE_DIR=directory.parent / "recovery-validation-cache",
    )
    return values
