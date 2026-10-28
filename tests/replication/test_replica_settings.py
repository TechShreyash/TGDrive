from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from pymongo.errors import ConnectionFailure

from tests.support.mongo_fixtures import register_test_backend
from tests.support.recovery_fixtures import settings
from tgdrive.replication.replica_settings import (
    collect_credentials,
    validate_recovery_settings,
)
from tgdrive.settings.managed_settings import (
    ManagedDocument,
    initial_clients,
    save_document,
)
from tgdrive.settings.performance_preferences import PerformancePreferences


@pytest.fixture
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, settings(tmp_path))


def test_settings_snapshot_keeps_labels_and_preferences_without_account_sessions(
    tmp_path, mongo_backend
):
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
    save_document(configured.resolved_data_dir, document, 0)
    preferences = PerformancePreferences(
        timezone="UTC", cache_memory_bytes=0
    ).model_dump()
    mongo_backend.db.settings.insert_many(
        [
            {"_id": "performance-preferences", "document": preferences},
            {"_id": "transfer-preferences", "document": {"uploads_per_bot": 2}},
        ]
    )
    credentials = collect_credentials(configured, SimpleNamespace(mongo=mongo_backend))
    validate_recovery_settings(credentials)
    assert credentials["managed_document"]["clients"][0]["label"] == "Primary storage"
    assert all(
        client["kind"] == "bot" for client in credentials["managed_document"]["clients"]
    )
    assert "syntheticPremiumSession" not in str(credentials)
    assert (
        credentials["preferences"]["performance-preferences.json"]["timezone"] == "UTC"
    )
    assert credentials["preferences"]["transfer-preferences.json"] == {
        "uploads_per_bot": 2
    }
    assert credentials["mongodb_backup_key"] == "a1" * 32
    assert "mongodb_uri" not in credentials and "DATA_DIR" not in credentials


def test_credentials_collector_bounds_and_validates_private_policy_files(
    tmp_path, mongo_backend, monkeypatch
):
    configured = settings(tmp_path)
    collection = mongo_backend.db.settings
    collection.insert_one(
        {"_id": "transfer-preferences", "document": {"uploads_per_bot": 99}}
    )
    runtime = SimpleNamespace(mongo=mongo_backend)
    with pytest.raises(ValueError):
        collect_credentials(configured, runtime)
    collection.replace_one(
        {"_id": "transfer-preferences"},
        {"_id": "transfer-preferences", "document": {"oversized": "x" * 65536}},
    )
    with pytest.raises(ValueError):
        collect_credentials(configured, runtime)

    def unavailable(_query):
        raise ConnectionFailure("synthetic database access failure")

    monkeypatch.setattr(collection, "find_one", unavailable)
    with pytest.raises(ConnectionFailure):
        collect_credentials(configured, runtime)


@pytest.mark.parametrize(
    "damage",
    [
        "traversal",
        "unknown-policy",
        "missing-policy",
        "invalid-value",
        "coerced-api",
        "coerced-enabled",
    ],
)
def test_imported_settings_are_complete_allowlisted_and_strongly_typed(
    tmp_path, damage, mongo_backend
):
    value = collect_credentials(
        settings(tmp_path), SimpleNamespace(mongo=mongo_backend)
    )
    value = copy.deepcopy(value)
    if damage == "traversal":
        value["preferences"]["../secrets"] = {}
    elif damage == "unknown-policy":
        value["preferences"]["arbitrary.json"] = {}
    elif damage == "missing-policy":
        value["preferences"].pop("bot-preferences.json")
    elif damage == "invalid-value":
        value["preferences"]["transfer-preferences.json"] = {"uploads_per_bot": True}
    elif damage == "coerced-api":
        value["managed_document"]["api_id"] = True
    else:
        value["managed_document"]["clients"][0]["enabled"] = "yes"
    with pytest.raises(ValueError):
        validate_recovery_settings(value)
