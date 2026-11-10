"""Background catalog checks stay paced and leave the event loop responsive."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.media.test_preloading import CHANNEL, finish, manager_for
from tests.support.mongo_fixtures import FakeMongoBackend, new_store
from tgdrive.core.errors import CorruptStoreError
from tgdrive.media.media_reader import BLOCK
from tgdrive.media.thumbnail_rebuild import RebuildSource
from tgdrive.storage import pacing
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.store import ROOT_ID


def test_missing_catalog_metadata_blocks_statistics_and_mutations_without_repair():
    backend = FakeMongoBackend()
    store = new_store(backend=backend)
    try:
        store.add_file("/", "preserved.bin", 77, 3)
        nodes = list(backend.db.nodes.find({}))
        backend.db.catalog.delete_one({"_id": "catalog"})

        with pytest.raises(CorruptStoreError, match="catalog metadata is missing"):
            store.statistics()
        with pytest.raises(CorruptStoreError, match="catalog metadata is missing"):
            store.bind_storage_channel(-1001234567890)

        assert list(backend.db.catalog.find({})) == []
        assert list(backend.db.nodes.find({})) == nodes
    finally:
        store.close()


def test_missing_root_refuses_directory_listing_and_preserves_surviving_nodes():
    backend = FakeMongoBackend()
    store = new_store(backend=backend)
    try:
        folder = store.create_folder("/", "Preserved")
        store.add_file("/" + folder["id"], "preserved.bin", 77, 3)
        backend.db.nodes.delete_one({"_id": ROOT_ID})
        nodes = list(backend.db.nodes.find({}))
        metadata = list(backend.db.catalog.find({}))

        with pytest.raises(CorruptStoreError, match="catalog root is missing"):
            store.list_directory("/")

        assert len(nodes) == 2
        assert list(backend.db.nodes.find({})) == nodes
        assert list(backend.db.catalog.find({})) == metadata
    finally:
        store.close()


def test_recent_activity_refuses_a_lost_installation_owner(monkeypatch):
    store = new_store()
    try:
        item = store.add_file("/", "notes.txt", 77, 3)
        row = store.db.nodes.find_one({"_id": item["id"]})
        assert row is not None
        before = row["active_at"]

        def lost_owner():
            raise DatabaseUnavailable("Synthetic ownership loss")

        monkeypatch.setattr(store.backend, "check_owner", lost_owner)
        with pytest.raises(DatabaseUnavailable, match="ownership loss"):
            store.mark_opened(item["id"])
        row = store.db.nodes.find_one({"_id": item["id"]})
        assert row is not None and row["active_at"] == before
    finally:
        store.close()


async def test_thumbnail_authorization_does_not_block_the_event_loop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    data = {"id": "file", "name": "movie.mp4", "message_id": 77, "size": 3}
    calling_threads = []
    loop_thread = threading.get_ident()

    def get_file(path):
        calling_threads.append(threading.get_ident())
        entered.set()
        release.wait(3)
        assert path == "/file"
        return data

    store = SimpleNamespace(
        drive_id="drive", get_file=get_file, resolve_id=lambda identity: "/" + identity
    )
    owner = SimpleNamespace(
        drive_id="drive", runtime=lambda: SimpleNamespace(store=store)
    )
    monkeypatch.setattr(pacing.background, "wait", AsyncMock())
    task = asyncio.create_task(RebuildSource(owner, data).authorize())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0)
        assert not task.done(), "The event loop could not advance during the lookup"
        assert len(calling_threads) == 1
        assert calling_threads[0] != loop_thread
    finally:
        release.set()
        await task


async def test_preload_paces_every_catalog_authorization(tmp_path, monkeypatch):
    manager, pool, _ = await manager_for(tmp_path, b"x" * (4 * BLOCK))
    calls = []

    async def wait(operations=1):
        calls.append(("paced", operations))

    async def authorize():
        if not calls:
            calls.append(("admission", None))
            return
        assert calls and calls[-1] == ("paced", 2)
        calls.append(("authorized", None))

    monkeypatch.setattr(pacing.background, "wait", wait)
    try:
        job = await manager.prepare(
            channel_id=CHANNEL,
            message_id=77,
            size=4 * BLOCK,
            name="movie.mp4",
            authorize=authorize,
        )
        assert (await finish(manager, job))["state"] == "ready"
        checks = sum(kind == "authorized" for kind, _ in calls)
        assert checks >= 4
        assert len(calls) == 2 * checks + 1
    finally:
        await manager.close()
        await pool.stop()
