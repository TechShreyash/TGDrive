from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from test_backup import settings

from tgdrive.managed_settings import ManagedDocument, initial_clients
from tgdrive.performance_preferences import PerformancePreferences
from tgdrive.replica_settings import collect_credentials, validate_optional_credentials
from tgdrive.replica_state import atomic_private


def test_settings_snapshot_keeps_managed_labels_sessions_and_safe_preferences(tmp_path):
    configured = settings(tmp_path)
    configured.mongodb_backup_key = SecretStr("a1" * 32)
    configured.string_sessions_csv = SecretStr("syntheticPremiumSession")
    document = ManagedDocument(
        revision=3,
        api_id=configured.api_id,
        api_hash=configured.api_hash,
        clients=initial_clients(configured),
    )
    document.clients[0].label = "Primary storage"
    atomic_private(
        configured.resolved_data_dir / "telegram-settings.json",
        document.private_bytes(),
    )
    preferences = PerformancePreferences(
        timezone="UTC", cache_memory_bytes=0
    ).model_dump()
    atomic_private(
        configured.resolved_data_dir / "performance-preferences.json",
        json.dumps(preferences).encode(),
    )
    atomic_private(
        configured.resolved_data_dir / "transfer-preferences.json",
        b'{"uploads_per_bot":2}',
    )
    credentials = collect_credentials(configured, SimpleNamespace())
    validate_optional_credentials(credentials)
    assert credentials["managed_document"]["clients"][0]["label"] == "Primary storage"
    assert (
        credentials["managed_document"]["clients"][1]["credential"]
        == "syntheticPremiumSession"
    )
    assert (
        credentials["preferences"]["performance-preferences.json"]["timezone"] == "UTC"
    )
    assert credentials["preferences"]["transfer-preferences.json"] == {
        "uploads_per_bot": 2
    }
    assert credentials["mongodb_backup_key"] == "a1" * 32
    assert "mongodb_uri" not in credentials and "DATA_DIR" not in credentials


def test_credentials_collector_bounds_and_validates_private_policy_files(tmp_path):
    configured = settings(tmp_path)
    path = configured.resolved_data_dir / "transfer-preferences.json"
    atomic_private(path, b'{"uploads_per_bot":99}')
    with pytest.raises(ValueError):
        collect_credentials(configured, SimpleNamespace())
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        collect_credentials(configured, SimpleNamespace())
    path.chmod(0o600)
    atomic_private(path, b" " * (65536 + 1))
    with pytest.raises(ValueError):
        collect_credentials(configured, SimpleNamespace())


@pytest.mark.parametrize(
    "damage",
    ["traversal", "unknown-policy", "invalid-value", "coerced-api", "coerced-enabled"],
)
def test_imported_optional_settings_are_allowlisted_and_strongly_typed(
    tmp_path, damage
):
    value = collect_credentials(settings(tmp_path), SimpleNamespace())
    value = copy.deepcopy(value)
    if damage == "traversal":
        value["preferences"]["../secrets"] = {}
    elif damage == "unknown-policy":
        value["preferences"]["arbitrary.json"] = {}
    elif damage == "invalid-value":
        value["preferences"]["transfer-preferences.json"] = {"uploads_per_bot": True}
    elif damage == "coerced-api":
        value["managed_document"]["api_id"] = True
    else:
        value["managed_document"]["clients"][0]["enabled"] = "yes"
    with pytest.raises(ValueError):
        validate_optional_credentials(value)
