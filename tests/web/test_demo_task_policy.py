"""Restricted demo jobs retain their write policy when recovered or resumed."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.mongo_fixtures import FakeMongoBackend
from tgdrive.storage.demo_files import restricted_writes, write_policy
from tgdrive.transfers.tasks import TransferRegistry


@pytest.mark.parametrize("enabled", [False, True])
async def test_durable_demo_job_restores_policy_using_current_deployment(enabled):
    backend = FakeMongoBackend()
    registry = TransferRegistry(
        mongo=backend.db.jobs, mongo_backend=backend, demo_mode=True
    )
    try:
        with write_policy(True):
            record = await registry.create("queued")
        identity = record.id
        saved = backend.db.jobs.find_one({"_id": identity})["data"]
        assert saved["context"]["demo_read_only"] is True
    finally:
        registry.close()

    recovered = TransferRegistry(
        mongo=backend.db.jobs, mongo_backend=backend, demo_mode=enabled
    )
    observed = []

    async def work(_record):
        observed.append(restricted_writes.get())
        observed.append(await asyncio.to_thread(restricted_writes.get))

    try:
        assert restricted_writes.get() is False
        await recovered.spawn(identity, work)
        assert observed == [enabled, enabled]
        assert restricted_writes.get() is False
    finally:
        recovered.close()


async def test_trusted_job_does_not_inherit_an_unrelated_visitor_policy():
    registry = TransferRegistry(demo_mode=True)
    record = await registry.create("queued")
    observed = []

    async def work(_record):
        observed.append(restricted_writes.get())

    with write_policy(True):
        await registry.spawn(record.id, work)
        assert restricted_writes.get() is True
    assert observed == [False]
    assert "demo_read_only" not in record.context


async def test_disabled_demo_mode_does_not_mark_new_jobs():
    registry = TransferRegistry(demo_mode=False)
    with write_policy(True):
        record = await registry.create("queued")
    assert "demo_read_only" not in record.context
