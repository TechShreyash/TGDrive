"""A missing derivative can reuse complete local data, never refill its source."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import threading
from types import SimpleNamespace

import pytest

from tgdrive.media.generated_thumbnails import CachedThumbnailRepair, read
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import BLOCK, MediaReadCache
from tgdrive.media.thumbnails import Thumbnail, ThumbnailBusy, ThumbnailCache

JPEG = b"\xff\xd8\xffsmall cover\xff\xd9"


def source(size, message_id=1, parts=None):
    return dict(channel_id=-100, message_id=message_id, size=size, parts=parts)


def media_cache(tmp_path, *, disk=False):
    return MediaReadCache(
        tmp_path / "media" if disk else None,
        MediaCachePolicy(
            disk_max_bytes=8 * BLOCK if disk else 0,
            memory_max_bytes=0 if disk else 8 * BLOCK,
            min_free_bytes=0,
        ),
    )


async def fill(cache, metadata, payload, *, blocks=None):
    cache.register_file(**metadata, name="clip.mov")
    keys = []
    for part in metadata["parts"] or [
        dict(message_id=metadata["message_id"], offset=0, size=metadata["size"])
    ]:
        for index in range((part["size"] + BLOCK - 1) // BLOCK):
            key = (
                metadata["channel_id"],
                part["message_id"],
                "document",
                part["message_id"] * 100,
                part["size"],
                index,
            )
            keys.append(key)
            if blocks is not None and len(keys) > blocks:
                continue
            lo = part["offset"] + index * BLOCK
            data = payload[lo : min(lo + BLOCK, part["offset"] + part["size"])]

            async def remote(data=data):
                return data

            await cache.get(key, len(data), remote)
    await cache.flush()
    return keys


def forbid_origin(monkeypatch, cache):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("A thumbnail repair must not call the origin-fill path")

    monkeypatch.setattr(cache, "get", forbidden)


async def repair(helper, cache, metadata):
    return await helper.get(
        cache,
        source=metadata,
        name="clip.mov",
        version=cache.cached_source_version(**metadata),
        authorize=lambda: None,
    )


@pytest.mark.parametrize("disk", [False, True])
async def test_complete_multipart_original_is_spooled_verified_and_saved(
    tmp_path, monkeypatch, disk
):
    payload = b"a" * (BLOCK + 7) + b"second part"
    part_sizes = [BLOCK + 7, len(b"second part")]
    parts = []
    offset = 0
    for index, size in enumerate(part_sizes):
        parts.append(
            dict(
                index=index,
                message_id=index + 1,
                offset=offset,
                size=size,
                sha256=hashlib.sha256(payload[offset : offset + size]).hexdigest(),
            )
        )
        offset += size
    metadata = source(len(payload), parts=parts)
    cache = media_cache(tmp_path, disk=disk)
    await fill(cache, metadata, payload)
    version = cache.cached_source_version(**metadata)
    forbid_origin(monkeypatch, cache)
    directory = tmp_path / "covers"
    helper = CachedThumbnailRepair(directory)
    calls = []

    async def generate(path, name, **_kwargs):
        calls.append(path)
        assert name == "clip.mov" and path.read_bytes() == payload
        assert path.stat().st_mode & 0o077 == 0
        return JPEG

    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", generate)
    assert await repair(helper, cache, metadata) == JPEG
    assert len(calls) == 1 and not calls[0].exists()
    assert read(directory, -100, 1) == JPEG
    assert not list(directory.glob(".repair-*"))
    assert cache.cached_source_version(**metadata) == version
    await cache.close()


@pytest.mark.parametrize("state", ["cold", "partial", "oversized"])
async def test_uncached_partial_and_oversized_sources_never_refill_or_render(
    tmp_path, monkeypatch, state
):
    cache = media_cache(tmp_path)
    metadata = source(129 * BLOCK if state == "oversized" else 2 * BLOCK)
    cache.register_file(**metadata, name="clip.mov")
    if state == "partial":
        await fill(cache, metadata, b"a" * metadata["size"], blocks=1)
    forbid_origin(monkeypatch, cache)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("A partial source must not be rendered")

    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", forbidden)
    helper = CachedThumbnailRepair(tmp_path / "covers")
    assert await repair(helper, cache, metadata) is None
    assert not list(tmp_path.rglob(".repair-*"))
    await cache.close()


@pytest.mark.parametrize("change", ["eviction", "clear", "checksum"])
async def test_source_changes_abort_copy_and_remove_temporary_files(
    tmp_path, monkeypatch, change
):
    payload = b"a" * BLOCK + b"b" * BLOCK
    metadata = source(len(payload))
    if change == "checksum":
        metadata["parts"] = [
            dict(index=0, message_id=1, offset=0, size=len(payload), sha256="0" * 64)
        ]
    cache = media_cache(tmp_path)
    keys = await fill(cache, metadata, payload)
    original = cache.cached_file_blocks

    async def changing(**kwargs):
        count = 0
        async for block in original(**kwargs):
            yield block
            count += 1
            if count == 1 and change == "clear":
                await cache.clear(cache.cached_source_version(**metadata).file_id)
            elif count == 1 and change == "eviction":
                _, data = cache.memory.entries.pop(keys[1])
                cache.memory.bytes_used -= len(data)

    monkeypatch.setattr(cache, "cached_file_blocks", changing)
    forbid_origin(monkeypatch, cache)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Changed/incomplete source must not be rendered")

    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", forbidden)
    helper = CachedThumbnailRepair(tmp_path / "covers")
    if change == "clear":
        with pytest.raises(ThumbnailBusy):
            await repair(helper, cache, metadata)
    else:
        assert await repair(helper, cache, metadata) is None
    assert not helper._slot.locked()
    assert not list(tmp_path.rglob(".repair-*"))
    await cache.close()


async def test_corrupt_disk_digest_is_rejected_before_render(tmp_path, monkeypatch):
    metadata = source(20)
    cache = media_cache(tmp_path, disk=True)
    keys = await fill(cache, metadata, b"a" * 20)
    block = cache.disk.directory / (cache.disk._key(keys[0]) + ".block")
    block.write_bytes(block.read_bytes()[:-1] + b"z")
    forbid_origin(monkeypatch, cache)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Corrupt source must not be rendered")

    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", forbidden)
    assert (
        await repair(CachedThumbnailRepair(tmp_path / "covers"), cache, metadata)
        is None
    )
    assert not list(tmp_path.rglob(".repair-*"))
    await cache.close()


async def test_cancelled_repair_cleans_source_and_can_retry(tmp_path, monkeypatch):
    metadata = source(20)
    cache = media_cache(tmp_path)
    await fill(cache, metadata, b"a" * 20)
    helper = CachedThumbnailRepair(tmp_path / "covers")
    entered = asyncio.Event()
    paths = []

    async def blocked(path, _name, **_kwargs):
        paths.append(path)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", blocked)
    task = asyncio.create_task(repair(helper, cache, metadata))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not paths[0].exists() and not helper._slot.locked()

    async def ready(*_args, **_kwargs):
        return JPEG

    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", ready)
    assert await repair(helper, cache, metadata) == JPEG
    await cache.close()


@pytest.mark.parametrize("stage", ["open", "write", "cleanup"])
async def test_repeated_cancellation_drains_owned_files_before_releasing_slot(
    tmp_path, monkeypatch, stage
):
    metadata = source(20)
    cache = media_cache(tmp_path)
    await fill(cache, metadata, b"a" * 20)
    helper = CachedThumbnailRepair(tmp_path / "covers")
    entered, release = threading.Event(), threading.Event()
    outputs, paths = [], []
    original_open, original_cleanup = helper._open_source, helper._cleanup_source

    def pause():
        entered.set()
        assert release.wait(5)

    class Writer:
        def __init__(self, output):
            self.output = output

        def write(self, data):
            if stage == "write":
                pause()
            return self.output.write(data)

        def close(self):
            self.output.close()

        def flush(self):
            self.output.flush()

    def opening(size, reserve):
        if stage == "open":
            pause()
        output, path = original_open(size, reserve)
        outputs.append(output)
        paths.append(path)
        return Writer(output), path

    def cleanup(output, path):
        if stage == "cleanup":
            pause()
        original_cleanup(output, path)

    async def ready(*_args, **_kwargs):
        return JPEG

    monkeypatch.setattr(helper, "_open_source", opening)
    monkeypatch.setattr(helper, "_cleanup_source", cleanup)
    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", ready)
    task = asyncio.create_task(repair(helper, cache, metadata))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and helper._slot.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not helper._slot.locked()
    assert outputs and all(output.closed for output in outputs)
    assert paths and all(not path.exists() for path in paths)
    await cache.close()


async def test_failed_render_waits_for_new_data_not_metadata_or_cache_reads(
    tmp_path, monkeypatch
):
    metadata = source(20)
    cache = media_cache(tmp_path)
    keys = await fill(cache, metadata, b"a" * 20)
    helper = CachedThumbnailRepair(tmp_path / "covers")
    calls = 0

    async def failed(*_args, **kwargs):
        nonlocal calls
        calls += 1
        kwargs["on_start"]()
        return None

    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", failed)
    version = cache.cached_source_version(**metadata)
    assert await repair(helper, cache, metadata) is None
    cache.register_file(**metadata, name="renamed.mov")

    async def forbidden():
        raise AssertionError("Existing cache read must not download")

    assert await cache.get(keys[0], 20, forbidden) == b"a" * 20
    assert cache.cached_source_version(**metadata) == version
    assert await repair(helper, cache, metadata) is None
    assert calls == 1
    await cache.clear(version.file_id)
    await fill(cache, metadata, b"a" * 20)
    assert cache.cached_source_version(**metadata).revision > version.revision
    assert await repair(helper, cache, metadata) is None
    assert calls == 2
    await cache.close()


async def test_unrelated_cache_clear_does_not_suppress_a_valid_source_retry(
    tmp_path, monkeypatch
):
    cache = media_cache(tmp_path)
    first, second = source(20), source(20, message_id=2)
    await fill(cache, first, b"a" * 20)
    await fill(cache, second, b"b" * 20)
    helper = CachedThumbnailRepair(tmp_path / "covers")
    original = cache.cached_file_blocks
    revision = cache.cached_source_version(**first).data_revision

    async def clearing(**kwargs):
        async for block in original(**kwargs):
            yield block
            await cache.clear(cache.cached_source_version(**second).file_id)

    calls = 0

    async def ready(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return JPEG

    monkeypatch.setattr(cache, "cached_file_blocks", clearing)
    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", ready)
    with pytest.raises(ThumbnailBusy):
        await repair(helper, cache, first)
    assert calls == 0
    assert cache.cached_source_version(**first).data_revision == revision
    monkeypatch.setattr(cache, "cached_file_blocks", original)
    assert await repair(helper, cache, first) == JPEG
    assert calls == 1
    assert not list(tmp_path.rglob(".repair-*"))
    await cache.close()


async def test_spool_respects_configured_floor_and_space_recovery_can_retry(
    tmp_path, monkeypatch
):
    cache = media_cache(tmp_path)
    cache.policy = MediaCachePolicy(
        memory_max_bytes=8 * BLOCK, min_free_bytes=128 * BLOCK
    )
    metadata = source(20)
    await fill(cache, metadata, b"a" * 20)
    helper = CachedThumbnailRepair(tmp_path / "covers")
    free = 64 * BLOCK + metadata["size"]
    monkeypatch.setattr(
        "tgdrive.media.generated_thumbnails.shutil.disk_usage",
        lambda _directory: SimpleNamespace(free=free),
    )
    calls = 0

    async def ready(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return JPEG

    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", ready)
    with pytest.raises(ThumbnailBusy):
        await repair(helper, cache, metadata)
    assert calls == 0
    free = 129 * BLOCK
    assert await repair(helper, cache, metadata) == JPEG
    assert calls == 1
    await cache.close()


def test_only_unlocked_owned_repair_leftovers_are_removed(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    directory = tmp_path / "covers"
    directory.mkdir()
    orphan = directory / (".repair-" + "a" * 32 + ".source")
    active = directory / (".repair-" + "b" * 32 + ".source")
    symlink = directory / (".repair-" + "c" * 32 + ".source")
    other = directory / ".repair-unowned.source"
    private = directory / "private.txt"
    for path in (orphan, active, other, private):
        path.write_bytes(b"retained")
    symlink.symlink_to(private)
    helper = CachedThumbnailRepair(directory)
    with active.open("r+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        output, path = helper._open_source(20, 64 * BLOCK)
        try:
            assert not orphan.exists()
            assert active.exists() and other.exists() and symlink.is_symlink()
            assert private.read_bytes() == b"retained"
            assert path.exists()
            helper._cleanup_stale_sources()
            assert path.exists(), "An active current source must retain its lock"
        finally:
            helper._cleanup_source(output, path)
    helper._cleanup_stale_sources()
    assert not active.exists() and not path.exists()
    assert other.exists() and private.exists() and symlink.is_symlink()


async def test_startup_removes_owned_crash_leftovers_before_any_cache_fill(tmp_path):
    directory = tmp_path / "covers"
    directory.mkdir()
    orphan = directory / (".repair-" + "d" * 32 + ".source")
    orphan.write_bytes(b"abandoned source")
    await CachedThumbnailRepair(directory).start()
    assert not orphan.exists()


@pytest.mark.parametrize("timeout", ["admission", "outer"])
async def test_busy_native_renderer_never_marks_source_as_failed(
    tmp_path, monkeypatch, timeout
):
    import tgdrive.media.generated_thumbnails as module

    cache = media_cache(tmp_path)
    metadata = source(20)
    await fill(cache, metadata, b"a" * 20)
    helper = CachedThumbnailRepair(tmp_path / "covers")
    held = asyncio.Semaphore(0)
    monkeypatch.setitem(module._slots, asyncio.get_running_loop(), held)
    monkeypatch.setattr(
        module, "NATIVE_ADMISSION_SECONDS", 0.02 if timeout == "admission" else 2
    )
    monkeypatch.setattr(module, "REPAIR_SECONDS", 8 if timeout == "admission" else 0.03)
    with pytest.raises(ThumbnailBusy):
        await repair(helper, cache, metadata)
    assert not helper._slot.locked()
    assert not list(tmp_path.rglob(".repair-*"))
    held.release()
    monkeypatch.setattr(module, "REPAIR_SECONDS", 8)

    async def ready(*_args, **_kwargs):
        return JPEG

    monkeypatch.setattr(module, "generate", ready)
    assert await repair(helper, cache, metadata) == JPEG
    await cache.close()


async def test_transient_native_process_spawn_failure_can_retry_same_source(
    tmp_path, monkeypatch
):
    import tgdrive.media.generated_thumbnails as module

    cache = media_cache(tmp_path)
    metadata = source(20)
    await fill(cache, metadata, b"a" * 20)
    helper = CachedThumbnailRepair(tmp_path / "covers")

    async def unavailable(*_args, **_kwargs):
        raise OSError(errno.EAGAIN, "Transient process admission failure")

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", unavailable)
    with pytest.raises(ThumbnailBusy):
        await repair(helper, cache, metadata)

    async def ready(*_args, **_kwargs):
        return JPEG

    monkeypatch.setattr(module, "generate", ready)
    assert await repair(helper, cache, metadata) == JPEG
    assert not list(tmp_path.rglob(".repair-*"))
    await cache.close()


async def test_repair_deadline_cleans_source_and_does_not_repeat(tmp_path, monkeypatch):
    metadata = source(20)
    cache = media_cache(tmp_path)
    await fill(cache, metadata, b"a" * 20)
    helper = CachedThumbnailRepair(tmp_path / "covers")
    monkeypatch.setattr("tgdrive.media.generated_thumbnails.REPAIR_SECONDS", 0.03)
    calls = 0

    async def blocked(*_args, **kwargs):
        nonlocal calls
        calls += 1
        kwargs["on_start"]()
        await asyncio.Event().wait()

    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", blocked)
    assert await repair(helper, cache, metadata) is None
    assert await repair(helper, cache, metadata) is None
    assert calls == 1 and not helper._slot.locked()
    assert not list(tmp_path.rglob(".repair-*"))
    await cache.close()


async def test_only_one_file_repairs_at_once_without_a_long_queue(
    tmp_path, monkeypatch
):
    cache = media_cache(tmp_path)
    first, second = source(20), source(20, message_id=2)
    await fill(cache, first, b"a" * 20)
    await fill(cache, second, b"b" * 20)
    helper = CachedThumbnailRepair(tmp_path / "covers")
    entered = asyncio.Event()

    async def blocked(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("tgdrive.media.generated_thumbnails.generate", blocked)
    task = asyncio.create_task(repair(helper, cache, first))
    await entered.wait()
    with pytest.raises(ThumbnailBusy):
        await repair(helper, cache, second)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await cache.close()


async def test_only_negative_thumbnail_entries_follow_source_revision():
    cache = ThumbnailCache()
    calls = 0

    async def missing():
        nonlocal calls
        calls += 1
        return None

    assert await cache.get((1,), missing, missing_revision=1) is None
    assert await cache.get((1,), missing, missing_revision=1) is None
    assert calls == 1
    assert await cache.get((2,), missing, missing_revision=2) is None
    assert await cache.get((1,), missing, missing_revision=1) is None
    assert calls == 2
    assert await cache.get((1,), missing, missing_revision=3) is None
    assert calls == 3

    async def image():
        return Thumbnail(JPEG, "image/jpeg")

    assert (await cache.get((3,), image, missing_revision=1)).content == JPEG
    assert (await cache.get((3,), missing, missing_revision=2)).content == JPEG
    assert calls == 3
    await cache.close()
