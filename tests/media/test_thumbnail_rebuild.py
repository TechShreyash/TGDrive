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
from tests.support.mongo_fixtures import new_store, register_test_backend
from tgdrive.core.errors import TrashedItemError
from tgdrive.media import thumbnail_rebuild as module
from tgdrive.media.generated_thumbnails import read, save
from tgdrive.media.thumbnails import Thumbnail, ThumbnailCache
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus
from tgdrive.web.app import create_app

COVER = b"\xff\xd8\xffnew cover\xff\xd9"


@pytest.fixture
async def rebuild(tmp_path):
    settings = make_settings(tmp_path)
    store = new_store(tmp_path / "store.json")
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
    assert manager.task is not None
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
    assert manager.task is not None
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
    assert manager.task is not None
    await manager.task
    assert manager.state["skipped"] == 1
    assert manager.state["status"] == "completed"
    assert read(manager.directory, manager.settings.storage_channel, 3) == COVER


@pytest.mark.parametrize("cancel", [False, True])
async def test_foreground_after_native_admission_defers_without_skipping(
    rebuild, monkeypatch, cancel
):
    from tests.support.book_samples import png

    if not shutil.which("ffmpeg"):
        pytest.skip("native decoder unavailable")
    manager, store, runtime = rebuild
    payload = png()
    item = store.add_file("/", "picture.png", 10, len(payload))
    foreground = False
    runtime.pool = SimpleNamespace(
        performance_snapshot=lambda: {"active": int(foreground)}
    )
    monkeypatch.setattr(module, "RATE", 10**12)
    fetches = []

    async def fetch(pool, channel, data, start, count, authorize, *, background):
        await authorize()
        fetches.append((start, count))
        return payload[start : start + count]

    monkeypatch.setattr(module, "read_range", fetch)
    generate = module.covers.generate
    attempts = 0

    def foreground_started():
        nonlocal foreground
        foreground = True

    async def render(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return await generate(
            *args, **kwargs, on_start=foreground_started if attempts == 1 else None
        )

    monkeypatch.setattr(module.covers, "generate", render)
    sources = []
    render_file = manager.render_file

    async def track_source(source):
        sources.append(source)
        return await render_file(source)

    monkeypatch.setattr(manager, "render_file", track_source)
    waiting = asyncio.Event()
    persist_progress = manager.persist_progress

    async def observe_wait():
        if manager.state["phase"] == "Waiting for active transfers or playback":
            waiting.set()
        await persist_progress()

    monkeypatch.setattr(manager, "persist_progress", observe_wait)
    await manager.start()
    assert manager.task is not None
    await asyncio.wait_for(waiting.wait(), 5)
    assert sources[0].runner is None
    assert not module.covers._background_slots[asyncio.get_running_loop()].locked()
    assert not list(manager.directory.glob(".repair-*.source"))
    assert not fetches
    assert manager.state["processed"] == manager.state["skipped"] == 0
    if cancel:
        await asyncio.wait_for(manager.cancel(), 2)
        assert manager.state["status"] == "cancelled"
        assert len(sources) == 1
    else:
        foreground = False
        await asyncio.wait_for(manager.task, 5)
        assert attempts == 2 and sources[0] is sources[1]
        assert sources[0].admitted_bytes == len(payload)
        assert manager.state["generated"] == 1
        assert manager.state["processed"] == 1
        assert manager.state["source_bytes"] == len(payload)
        assert read(
            manager.directory, manager.settings.storage_channel, item["message_id"]
        )
    assert manager.state["skipped"] == manager.state["failed"] == 0


async def test_interrupted_source_read_does_not_refund_its_budget(rebuild, monkeypatch):
    manager, store, _ = rebuild
    item = store.add_file("/", "picture.png", 10, module.BLOCK)
    manager.drive_id = store.drive_id
    source = module.RebuildSource(manager, item)
    source.budget = module.BLOCK
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def fetch(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()

    monkeypatch.setattr(module, "read_range", fetch)
    task = asyncio.create_task(source.read(0, 1))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    with pytest.raises(module.SourceLimit, match="Source read budget reached"):
        await source.read(0, 1)
    assert source.admitted_bytes == module.BLOCK
    assert source.used == manager.state["source_bytes"] == 0


async def test_pdf_deferral_closes_staging_before_wait_and_reuses_source(
    rebuild, monkeypatch
):
    manager, store, runtime = rebuild
    item = store.add_file("/", "document.pdf", 10, 2 * module.BLOCK)
    manager.drive_id = store.drive_id
    source = module.RebuildSource(manager, item)
    source.budget = 2 * module.BLOCK
    foreground = False
    runtime.pool = SimpleNamespace(
        performance_snapshot=lambda: {"active": int(foreground)}
    )
    monkeypatch.setattr(module, "RATE", 10**12)
    opened = []
    open_source = manager.staging._open_source

    def track_source(*args):
        result = open_source(*args)
        opened.append(result)
        return result

    monkeypatch.setattr(manager.staging, "_open_source", track_source)
    fetches = []

    async def fetch(pool, channel, data, start, count, authorize, *, background):
        nonlocal foreground
        fetches.append(start)
        foreground = start == 0
        return b"x" * count

    monkeypatch.setattr(module, "read_range", fetch)

    async def wait_turn():
        nonlocal foreground
        assert foreground
        assert all(output.closed and not path.exists() for output, path in opened)
        foreground = False

    monkeypatch.setattr(manager, "wait_turn", wait_turn)

    async def generate(*args, **kwargs):
        return COVER

    monkeypatch.setattr(module.covers, "generate", generate)
    assert await manager.render_when_runnable(source) == COVER
    assert fetches == [0, module.BLOCK]
    assert len(opened) == 2
    assert all(output.closed and not path.exists() for output, path in opened)
    assert source.used == source.admitted_bytes == manager.state["source_bytes"]
    assert source.used == 2 * module.BLOCK


async def test_loopback_cleanup_drains_before_repeated_cancellation_returns(
    rebuild, monkeypatch
):
    manager, store, _ = rebuild
    item = store.add_file("/", "picture.png", 10, 10)
    source = module.RebuildSource(manager, item)
    cleaning, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def cleanup():
        cleaning.set()
        await release.wait()
        cleaned.set()

    monkeypatch.setattr(source, "runner", SimpleNamespace(cleanup=cleanup))
    pending = asyncio.create_task(source.close())
    await asyncio.wait_for(cleaning.wait(), 2)
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    assert not pending.done() and not cleaned.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 2)
    assert cleaned.is_set()


async def test_retries_reuse_hot_blocks_identity_and_cumulative_budget(
    rebuild, monkeypatch
):
    manager, store, _ = rebuild
    item = store.add_file("/", "movie.mkv", 10, 3 * module.BLOCK)
    manager.drive_id = store.drive_id
    source = module.RebuildSource(manager, item)
    source.budget = 2 * module.BLOCK
    monkeypatch.setattr(module, "RATE", 10**12)
    fetches = []

    async def fetch(pool, channel, data, start, count, authorize, *, background):
        fetches.append(start)
        return b"x" * count

    monkeypatch.setattr(module, "read_range", fetch)
    attempts = 0

    async def render(current):
        nonlocal attempts
        attempts += 1
        assert current is source
        assert await current.read(0, 1) == b"x"
        if attempts == 1:
            current.deferred = True
            raise module.SourceDeferred
        assert await current.read(module.BLOCK, 1) == b"x"
        await current.read(2 * module.BLOCK, 1)

    monkeypatch.setattr(manager, "render_file", render)
    with pytest.raises(module.SourceLimit, match="Source read budget reached"):
        await manager.render_when_runnable(source)
    assert attempts == 2
    assert fetches == [0, module.BLOCK]
    assert source.used == source.admitted_bytes == 2 * module.BLOCK


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("limit", ["budget", "identity"])
async def test_http_source_limit_takes_precedence_over_transient_deferral(
    rebuild, monkeypatch, timeout, limit
):
    manager, store, runtime = rebuild
    item = store.add_file("/", "picture.png", 10, 10)
    manager.drive_id = store.drive_id
    source = module.RebuildSource(manager, item)
    source.budget = 0
    if limit == "identity":
        manager.drive_id = "changed"
    runtime.pool = SimpleNamespace(performance_snapshot=lambda: {"active": 1})

    async def render(current):
        url = await current.open()
        try:
            async with (
                aiohttp.ClientSession() as client,
                client.get(url) as response,
            ):
                if limit == "identity":
                    assert response.status == 409
                    await response.read()
                else:
                    with pytest.raises(aiohttp.ClientPayloadError):
                        await response.read()
            current.deferred = True
            if timeout:
                raise TimeoutError
        finally:
            await current.close()

    monkeypatch.setattr(manager, "render_file", render)
    reason = (
        "File removed or changed"
        if limit == "identity"
        else "Source read budget reached"
    )
    with pytest.raises(module.SourceLimit, match=reason):
        await manager.render_when_runnable(source)
    assert source.admitted_bytes == source.used == 0
    assert source.runner is None


async def test_active_render_budget_is_cumulative_and_excludes_only_policy_wait(
    rebuild, monkeypatch
):
    manager, store, _ = rebuild
    item = store.add_file("/", "movie.mkv", 10, 10)
    manager.drive_id = store.drive_id
    source = module.RebuildSource(manager, item)
    clock = 0.0
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock))
    attempts = 0
    waits = 0

    async def render(current):
        nonlocal attempts, clock
        attempts += 1
        clock += 500
        current.deferred = True
        raise module.SourceDeferred

    async def wait_turn():
        nonlocal clock, waits
        waits += 1
        clock += 2000

    monkeypatch.setattr(manager, "render_file", render)
    monkeypatch.setattr(manager, "wait_turn", wait_turn)
    with pytest.raises(
        module.SourceLimit, match="Thumbnail rendering time budget reached"
    ):
        await manager.render_when_runnable(source)
    assert attempts == 2 and waits == 1


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
    thumbnail = await cache.get(key, new)
    assert thumbnail is not None and thumbnail.content == b"new"
    assert cache.bytes == 3
    await cache.close()


def test_rebuild_routes_require_owner_confirmation_and_offer_progress(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path)
    store = new_store(tmp_path / "store.json")
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
