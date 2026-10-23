"""Restricted demo jobs retain their write policy when recovered or resumed."""

from __future__ import annotations

import asyncio
import json

import pytest

from tgdrive.demo_files import restricted_writes, write_policy
from tgdrive.tasks import TransferRegistry


@pytest.mark.parametrize("enabled", [False, True])
async def test_durable_demo_job_restores_policy_using_current_deployment(
    tmp_path, enabled
):
    database = tmp_path / "jobs.sqlite3"
    registry = TransferRegistry(database_path=database, demo_mode=True)
    try:
        with write_policy(True):
            record = registry.create("queued")
        identity = record.id
        saved = registry._db.execute(
            "SELECT data FROM jobs WHERE id=?", (identity,)
        ).fetchone()
        assert json.loads(saved[0])["context"]["demo_read_only"] is True
    finally:
        registry.close()

    recovered = TransferRegistry(database_path=database, demo_mode=enabled)
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
    record = registry.create("queued")
    observed = []

    async def work(_record):
        observed.append(restricted_writes.get())

    with write_policy(True):
        await registry.spawn(record.id, work)
        assert restricted_writes.get() is True
    assert observed == [False]
    assert "demo_read_only" not in record.context


def test_disabled_demo_mode_does_not_mark_new_jobs():
    registry = TransferRegistry(demo_mode=False)
    with write_policy(True):
        record = registry.create("queued")
    assert "demo_read_only" not in record.context
