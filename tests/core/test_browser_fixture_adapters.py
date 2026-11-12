"""Isolated fixture transaction regressions; no browser/service dependency."""

import pytest

from tests.support.memory_mongo import FixtureMongoBackend


def test_fixture_transaction_commits_and_fences_only_its_current_token():
    backend = FixtureMongoBackend()
    tokens = []

    def write(session):
        tokens.append(session)
        backend.fence(session)
        backend.db.telemetry_state.replace_one(
            {"_id": "lifetime"},
            {"_id": "lifetime", "bytes": 3},
            upsert=True,
            session=session,
        )

    backend.transaction(write)
    row = backend.db.telemetry_state.find_one({"_id": "lifetime"})
    assert row is not None and row["bytes"] == 3
    with pytest.raises(AssertionError, match="active transaction"):
        backend.fence(tokens[0])


def test_fixture_transaction_rolls_back_existing_and_new_collections():
    backend = FixtureMongoBackend()
    backend.db.telemetry_state.insert_one({"_id": "lifetime", "bytes": 1})

    def fail(session):
        backend.fence(session)
        backend.db.telemetry_state.replace_one(
            {"_id": "lifetime"},
            {"_id": "lifetime", "bytes": 2},
            session=session,
        )
        backend.db.telemetry_minutes.insert_one(
            {"_id": 60, "bytes": 2}, session=session
        )
        raise ValueError("Checkpoint failed")

    with pytest.raises(ValueError, match="Checkpoint failed"):
        backend.transaction(fail)
    row = backend.db.telemetry_state.find_one({"_id": "lifetime"})
    assert row is not None and row["bytes"] == 1
    assert list(backend.db.telemetry_minutes.find({})) == []
    backend.transaction(lambda session: backend.fence(session))
