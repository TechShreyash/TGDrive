from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from pymongo.errors import ConnectionFailure

from tests.support.mongo_fixtures import register_test_backend
from tests.support.recovery_fixtures import settings
from tgdrive.core.governor import MANAGED
from tgdrive.replication.replica_settings import (
    collect_credentials,
    credentials_from_snapshot,
    validate_recovery_settings,
)
from tgdrive.settings.managed_settings import (
    ManagedDocument,
    apply_document,
    initial_clients,
    save_document,
)
from tgdrive.settings.performance_preferences import PerformancePreferences


@pytest.fixture
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, settings(tmp_path))


@pytest.mark.parametrize("capture", ["live", "snapshot"])
@pytest.mark.parametrize(
    "saved",
    [
        None,
        {"timezone": "UTC", "auto_preload": False},
        PerformancePreferences(timezone="UTC", cache_memory_bytes=0).model_dump(),
    ],
)
def test_recovery_credentials_ignore_governor_only_changes(
    tmp_path, mongo_backend, capture, saved
):
    configured = settings(tmp_path)
    records = {"settings": []}
    if saved is not None:
        row = {"_id": "performance-preferences", "document": saved}
        mongo_backend.db.settings.insert_one(row)
        records["settings"].append(row)
    runtime = SimpleNamespace(mongo=mongo_backend)

    def collect():
        return (
            collect_credentials(configured, runtime)
            if capture == "live"
            else credentials_from_snapshot(configured, records)
        )

    original = collect()
    for field in MANAGED.intersection(PerformancePreferences.model_fields):
        setattr(configured, field, (getattr(configured, field, None) or 1) + 1000)
    assert collect() == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("timezone", "UTC"),
        ("auto_preload", False),
        ("upload_split_mib", 128),
        ("remote_download_concurrency", 7),
        ("cache_memory_bytes", 0),
    ],
)
def test_saved_recovery_policies_still_change_credentials(
    tmp_path, mongo_backend, field, value
):
    configured = settings(tmp_path)
    runtime = SimpleNamespace(mongo=mongo_backend)
    original = collect_credentials(configured, runtime)
    row = {"_id": "performance-preferences", "document": {field: value}}
    mongo_backend.db.settings.insert_one(row)
    current = collect_credentials(configured, runtime)
    assert current != original
    assert current["preferences"]["performance-preferences.json"][field] == value
    assert credentials_from_snapshot(configured, {"settings": [row]}) == current
    validate_recovery_settings(current)


def test_saved_authentication_changes_credentials_and_old_capsules_remain_valid(
    tmp_path, mongo_backend
):
    configured = settings(tmp_path)
    runtime = SimpleNamespace(mongo=mongo_backend)
    original = collect_credentials(configured, runtime)
    old = copy.deepcopy(original)
    old["preferences"]["performance-preferences.json"]["cache_disk_bytes"] += 1000
    validate_recovery_settings(old)
    document = ManagedDocument(
        revision=3,
        api_id=configured.api_id,
        api_hash=configured.api_hash,
        clients=initial_clients(configured),
    )
    document.clients[0].label = "Updated storage identity"
    document.clients[0].credential = SecretStr("765432:" + "z" * 24)
    save_document(configured.resolved_data_dir, document, 0)
    apply_document(configured, document)
    current = collect_credentials(configured, runtime)
    assert current != original
    assert current["bot_tokens"] == ["765432:" + "z" * 24]
    row = {"_id": "telegram", "document": json.loads(document.private_bytes())}
    assert credentials_from_snapshot(configured, {"settings": [row]}) == current


def test_captured_recovery_policies_ignore_later_live_policy_changes(
    tmp_path, mongo_backend
):
    configured = settings(tmp_path)
    runtime = SimpleNamespace(mongo=mongo_backend)
    captured = {"_id": "performance-preferences", "document": {"timezone": "UTC"}}
    mongo_backend.db.settings.insert_one(captured)
    original = credentials_from_snapshot(configured, {"settings": [captured]})
    mongo_backend.db.settings.replace_one(
        {"_id": "performance-preferences"},
        {"_id": "performance-preferences", "document": {"timezone": "Europe/Paris"}},
    )
    assert credentials_from_snapshot(configured, {"settings": [captured]}) == original
    assert collect_credentials(configured, runtime) != original


@pytest.mark.parametrize("raw", [{"cache_memory_bytes": -1}, {"unknown": 1}])
def test_canonical_defaults_do_not_mask_invalid_saved_resource_values(
    tmp_path, mongo_backend, raw
):
    configured = settings(tmp_path)
    row = {"_id": "performance-preferences", "document": raw}
    mongo_backend.db.settings.insert_one(row)
    with pytest.raises(ValueError):
        collect_credentials(configured, SimpleNamespace(mongo=mongo_backend))
    with pytest.raises(ValueError):
        credentials_from_snapshot(configured, {"settings": [row]})


def test_settings_snapshot_keeps_labels_and_preferences_without_account_sessions(
    tmp_path, mongo_backend
):
    configured = settings(tmp_path)
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
            {"_id": "bot-preferences", "document": {"encrypt": False}},
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
    assert credentials["preferences"]["bot-preferences.json"]["encrypt"] is False
    assert "mongodb_backup_key" not in credentials
    assert "mongodb_uri" not in credentials and "DATA_DIR" not in credentials


def test_credentials_collector_validates_private_policy_files(
    tmp_path, mongo_backend, monkeypatch
):
    configured = settings(tmp_path)
    collection = mongo_backend.db.settings
    collection.insert_one({"_id": "bot-preferences", "document": {"encrypt": "x"}})
    runtime = SimpleNamespace(mongo=mongo_backend)
    with pytest.raises(ValueError):
        collect_credentials(configured, runtime)
    collection.replace_one(
        {"_id": "bot-preferences"},
        {"_id": "bot-preferences", "document": {"unknown": "x"}},
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
        value["preferences"]["bot-preferences.json"] = {"encrypt": "yes"}
    elif damage == "coerced-api":
        value["managed_document"]["api_id"] = True
    else:
        value["managed_document"]["clients"][0]["enabled"] = "yes"
    with pytest.raises(ValueError):
        validate_recovery_settings(value)
