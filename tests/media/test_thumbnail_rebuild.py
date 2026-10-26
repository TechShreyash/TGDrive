"""Synthetic, offline regeneration: bounded reads, isolation and owner controls."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.core.errors import TrashedItemError
from tgdrive.media import thumbnail_rebuild as module
from tgdrive.media.generated_thumbnails import read, save
from tgdrive.media.thumbnails import Thumbnail, ThumbnailCache
from tgdrive.storage.store import DriveStore
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus
from tgdrive.web.app import create_app

COVER = b"\xff\xd8\xffnew cover\xff\xd9"


@pytest.fixture
async def rebuild(tmp_path):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "store.json")
    registry = TransferRegistry()
    runtime = SimpleNamespace(store=store, pool=object(), registry=registry)
    manager = module.ThumbnailRebuild(settings, lambda: runtime, ThumbnailCache())
    yield manager, store, runtime
    await manager.close()
    await manager.thumbnails.close()
    store.close()
    registry.close()


async def test_force_replaces_existing_covers_counts_skips_and_ignores_trash(
    rebuild, monkeypatch
):
    manager, store, _ = rebuild
    folder = store.create_folder("/", "Nested")
    photo = store.add_file("/" + folder["id"], "photo.jpg", 2, 10)
    store.add_file("/", "notes.txt", 3, 10)
    hidden = store.create_folder("/", "Hidden")
    store.add_file("/" + hidden["id"], "hidden.jpg", 4, 10)
    store.set_trashed("/" + hidden["id"], True)
    save(manager.directory, manager.settings.storage_channel, 2, b"\xff\xd8\xffold")
    calls = []

    async def render(source):
        calls.append(source.data["id"])
        return COVER

    monkeypatch.setattr(manager, "render_file", render)
    await manager.start()
    await manager.task
    assert calls == [photo["id"]]
    assert manager.state["status"] == "completed"
    assert manager.state["total"] == manager.state["processed"] == 2
    assert manager.state["generated"] == manager.state["skipped"] == 1
    assert read(manager.directory, manager.settings.storage_channel, 2) == COVER
    assert store.get_file(store.resolve_id(photo["id"]))["message_id"] == 2
    assert json.loads(manager.path.read_text())["status"] == "completed"
    if os.name == "posix":
        assert manager.path.stat().st_mode & 0o077 == 0


async def test_single_run_yields_to_active_work_and_can_be_cancelled(
    rebuild, monkeypatch
):
    manager, store, runtime = rebuild
    store.add_file("/", "photo.jpg", 2, 10)
    active = await runtime.registry.create("uploading")
    active.status = TransferStatus.RUNNING
    calls = []

    async def render(source):
        calls.append(source)
        return COVER

    monkeypatch.setattr(manager, "render_file", render)
    waiting = asyncio.Event()
    persist_progress = manager.persist_progress

    async def observe_wait():
        if manager.state["phase"] == "Waiting for active transfers or playback":
            waiting.set()
        await persist_progress()

    monkeypatch.setattr(manager, "persist_progress", observe_wait)
    first, second = await asyncio.gather(manager.start(), manager.start())
    assert first["id"] == second["id"]
    await asyncio.wait_for(waiting.wait(), 2)
    assert not calls
    stopped = await manager.cancel()
    assert stopped["status"] == "cancelled"
    assert stopped["processed"] == 0
    assert manager.task.done()


async def test_sparse_reader_bounds_origin_work_and_reuses_hot_blocks(
    rebuild, monkeypatch
):
    manager, store, _ = rebuild
    item = store.add_file("/", "movie.mov", 9, 5 * 1024**3)
    manager.drive_id = store.drive_id
    manager.state["source_bytes"] = 0
    source = module.RebuildSource(manager, item)
    source.budget = module.BLOCK
    calls = []

    async def fetch(pool, channel, data, start, length, authorize, *, background):
        assert background is True
        await authorize()
        calls.append((start, length))
        return b"x" * length

    monkeypatch.setattr(module, "read_range", fetch)
    monkeypatch.setattr(module, "RATE", 10**12)
    offset = 3 * 1024**3
    assert await source.read(offset, 16) == b"x" * 16
    assert await source.read(offset + 10, 16) == b"x" * 16
    with pytest.raises(module.SourceLimit):
        await source.read(offset + module.BLOCK, 1)
    assert calls == [(offset, module.BLOCK)]
    assert manager.state["source_bytes"] == module.BLOCK
    store.set_trashed(store.resolve_id(item["id"]), True)
    with pytest.raises(TrashedItemError):
        await source.read(offset, 1)


async def test_loopback_decoder_source_checks_token_and_exact_http_ranges(
    rebuild, monkeypatch
):
    manager, store, _ = rebuild
    item = store.add_file("/", "picture.png", 10, 6)
    manager.drive_id = store.drive_id

    async def fetch(*args, **kwargs):
        return b"abcdef"

    monkeypatch.setattr(module, "read_range", fetch)
    monkeypatch.setattr(module, "RATE", 10**12)
    source = module.RebuildSource(manager, item)
    url = await source.open()
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(url, headers={"Range": "bytes=2-4"}) as response:
                assert response.status == 206
                assert response.headers["Content-Range"] == "bytes 2-4/6"
                assert await response.read() == b"cde"
            async with client.head(url) as response:
                assert response.headers["Content-Length"] == "6"
                assert await response.read() == b""
            async with client.get(url + "wrong") as response:
                assert response.status == 404
            async with client.get(url, headers={"Range": "bytes=99-"}) as response:
                assert response.status == 416
    finally:
        await source.close()


@pytest.mark.parametrize("name", ["photo.png", "movie.mov", "manga.epub", "comic.cbz"])
async def test_native_rebuild_uses_selected_bytes_and_preserves_originals(
    rebuild, monkeypatch, name
):
    from tests.support.book_samples import cbz, epub, png

    if not shutil.which("ffmpeg"):
        pytest.skip("native decoder unavailable")
    manager, store, _ = rebuild
    original = (
        png()
        if name.endswith("png")
        else (
            Path(__file__).resolve().parents[1] / "fixtures/movtext-subtitles.mp4"
        ).read_bytes()
        if name.endswith("mov")
        else epub(padding=48 * module.BLOCK)
        if name.endswith("epub")
        else cbz(padding=48 * module.BLOCK)
    )
    item = store.add_file("/", name, 12, len(original))
    calls = []

    async def fetch(pool, channel, data, start, count, authorize, *, background):
        assert background is True
        await authorize()
        calls.append((start, count))
        return original[start : start + count]

    monkeypatch.setattr(module, "read_range", fetch)
    monkeypatch.setattr(module, "RATE", 10**12)
    await manager.start()
    await manager.task
    assert manager.state["generated"] == 1, manager.snapshot()
    assert manager.state["failed"] == manager.state["skipped"] == 0
    assert read(manager.directory, manager.settings.storage_channel, item["message_id"])
    assert sum(count for _, count in calls) < 4 * module.BLOCK
    assert original.startswith(
        b"\x89PNG"
        if name.endswith("png")
        else b"PK"
        if name.endswith(("epub", "cbz"))
        else b"\x00"
    )
    assert not list(manager.directory.glob(".repair-*.source"))


async def test_failed_optional_render_retains_existing_thumbnail(rebuild, monkeypatch):
    manager, store, _ = rebuild
    store.add_file("/", "broken.jpg", 3, 1)
    save(manager.directory, manager.settings.storage_channel, 3, COVER)

    async def render(source):
        return None

    monkeypatch.setattr(manager, "render_file", render)
    await manager.start()
    await manager.task
    assert manager.state["skipped"] == 1
    assert manager.state["status"] == "completed"
    assert read(manager.directory, manager.settings.storage_channel, 3) == COVER


async def test_restart_retains_progress_but_does_not_silently_restart_bandwidth_work(
    rebuild,
):
    manager, _, _ = rebuild
    manager.state.update(
        status="running", phase="Regenerating thumbnail", total=10, processed=3
    )
    await manager.persist()
    restored = module.ThumbnailRebuild(
        manager.settings, manager.runtime, manager.thumbnails
    )
    await restored.restore()
    assert restored.snapshot()["status"] == "interrupted"
    assert restored.snapshot()["processed"] == 3
    assert restored.task is None


async def test_force_invalidates_the_small_cover_cache():
    cache = ThumbnailCache()
    key = (-100, 2, 100, ())

    async def old():
        return Thumbnail(b"old", "image/jpeg")

    async def new():
        return Thumbnail(b"new", "image/jpeg")

    await cache.get(key, old)
    await cache.invalidate_file(-100, 2)
    assert (await cache.get(key, new)).content == b"new"
    assert cache.bytes == 3
    await cache.close()


def test_rebuild_routes_require_owner_confirmation_and_offer_progress(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path)
    store = DriveStore.create(tmp_path / "store.json")
    runtime = FakeRuntime(store)
    runtime.mongo = register_test_backend(monkeypatch, settings)
    app = create_app(settings, runtime=runtime)
    url = "/api/v1/settings/thumbnails/rebuild"
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get(url).status_code == 401
        assert client.post(url, json={"confirmed": True}).status_code == 401
        login(client)
        assert client.post(url, json={"confirmed": False}).status_code == 422
        assert client.post(url, json={}).status_code == 422
        assert client.get(url).json()["status"] == "idle"
        response = client.post(url, json={"confirmed": True})
        assert response.status_code == 202
        assert response.json()["id"]
        assert client.post(url + "/cancel", json={}).status_code == 200
    store.close()
