"""A URL job can restart after atomic local publication but before context commit."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from tests.transfers.test_multipart_transfers import (
    Account,
    dispose,
    manager_for,
    registry_for,
)
from tests.transfers.test_multipart_transfers import mongo_backend as mongo_backend
from tests.transfers.test_remote import (
    PUBLIC_DNS,
    FakeResolver,
    FakeResponse,
    FakeSession,
)
from tests.transfers.test_upload_destinations import folder
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers.manager import TransferManager
from tgdrive.transfers.remote import RemoteClient, RemoteDestinationExists
from tgdrive.transfers.tasks import TransferStatus


def remote(content):
    return RemoteClient(
        max_bytes=100,
        session=FakeSession(
            [
                FakeResponse(
                    200,
                    headers={"Content-Length": str(len(content))},
                    chunks=[content],
                )
            ]
        ),
        resolver=FakeResolver(PUBLIC_DNS),
    )


@pytest.mark.parametrize("mode", ["retry", "restart_retry", "restart_recover"])
async def test_url_retry_replaces_only_own_unpublished_stage_after_publication_crash(
    tmp_path, monkeypatch, mode
):
    manager, ledger = await manager_for(tmp_path)
    old, new = b"old bytes", b"new verified bytes"
    client = remote(old)
    manager.remote = client
    original = client._publish_temp

    def crash_after_publication(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic crash after durable local publication")

    monkeypatch.setattr(client, "_publish_temp", crash_after_publication)
    target = folder(manager.store, "Target")
    record = await manager.start_remote(
        url="https://files.example/source.bin", destination=target, filename=None
    )
    await record.task
    assert record.status == TransferStatus.ERROR
    assert record.context["kind"] == "remote"
    own_stage = manager.cache_dir / "remote" / record.id / "source.bin"
    assert own_stage.read_bytes() == old
    assert ledger.messages == {}
    unrelated = manager.cache_dir / "remote" / "unrelated.bin"
    unrelated.write_bytes(b"keep this file")
    fresh = remote(new)
    manager.store.move(target, folder(manager.store, "Other"))
    expected_destination = manager.store.resolve_id(target[1:])
    if mode != "retry":
        # A true process crash leaves an unfinished row rather than the
        # synthetic ERROR above; the saved folder identity remains unchanged.
        if mode == "restart_recover":
            record.status = TransferStatus.QUEUED
            record.phase = "interrupted"
        (await manager.registry.persist(record))
        settings = manager.settings
        await dispose(manager)
        pool = ClientPool([Account("bot-one", ledger), Account("bot-two", ledger)])
        await pool.start()
        manager = TransferManager(
            settings=settings,
            pool=pool,
            store=DriveStore.load(tmp_path / "drive.sqlite3"),
            registry=registry_for(tmp_path),
            remote=fresh,
        )
    else:
        await client.close()
        manager.remote = fresh
    try:
        if mode == "restart_recover":
            await manager.recover()
            retried = manager.registry.get(record.id)
        else:
            retried = await manager.retry_job(record.id)
        await retried.task
        assert retried.status == TransferStatus.COMPLETED
        assert retried.context["destination"] == expected_destination
        items = manager.store.list_directory(expected_destination)
        item = manager.store.get_file(expected_destination + "/" + next(iter(items)))
        actual = b"".join(
            ledger.messages[p["message_id"]].content for p in item["parts"]
        )
        assert actual == new
        assert item["sha256"] == hashlib.sha256(new).hexdigest()
        assert unrelated.read_bytes() == b"keep this file"
        assert not own_stage.exists()
    finally:
        await dispose(manager)


async def test_remote_default_still_refuses_existing_arbitrary_destination(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"existing caller data")
    client = remote(b"replacement")
    try:
        with pytest.raises(RemoteDestinationExists):
            await client.download("https://files.example/source.bin", tmp_path)
        assert source.read_bytes() == b"existing caller data"
    finally:
        await client.close()


async def test_url_destination_move_while_retrieval_waits_preserves_folder_identity(
    tmp_path,
):
    manager, ledger = await manager_for(tmp_path)
    target = folder(manager.store, "Target")
    entered, release = asyncio.Event(), asyncio.Event()
    response = FakeResponse(200, headers={"Content-Length": "3"}, chunks=[b"one"])
    original = response.content.read

    async def blocked(limit):
        entered.set()
        await release.wait()
        return await original(limit)

    response.content.read = blocked
    manager.remote = RemoteClient(
        max_bytes=100,
        session=FakeSession([response]),
        resolver=FakeResolver(PUBLIC_DNS),
    )
    record = await manager.start_remote(
        url="https://files.example/source.bin", destination=target, filename=None
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        manager.store.move(target, folder(manager.store, "Other"))
        expected = manager.store.resolve_id(target[1:])
        release.set()
        await record.task
        assert record.status == TransferStatus.COMPLETED
        assert record.context["destination"] == expected
        items = manager.store.list_directory(expected)
        assert len(items) == 1
        assert next(iter(ledger.messages.values())).content == b"one"
    finally:
        release.set()
        await dispose(manager)
