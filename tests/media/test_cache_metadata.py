"""Display-only cache labels reuse authoritative catalog data without media reads."""

import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from tests.support.mongo_fixtures import new_store
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import MediaReadCache

CHANNEL = -1001234567890


@pytest.fixture(autouse=True)
def unpaced(monkeypatch):
    async def wait(operations=1):
        await asyncio.sleep(0)

    monkeypatch.setattr("tgdrive.media.media_reader.pacing.background.wait", wait)


async def cached(cache, message_id, payload=b"bytes", *, channel=CHANNEL):
    identity = cache.register_file(
        channel_id=channel, message_id=message_id, size=len(payload)
    )
    key = (channel, message_id, "document", message_id * 10, len(payload), 0)

    async def fetch():
        return payload

    await cache.get(key, len(payload), fetch)
    return identity, key


async def finish(cache):
    assert cache._metadata_task is not None
    await asyncio.wait_for(asyncio.shield(cache._metadata_task), 3)


def test_metadata_page_bounds_projection_and_detects_conflicting_owners(
    monkeypatch,
):
    store = new_store()
    try:
        for index in range(66):
            store.create_folder("/", f"folder{index}")
        store.add_file("/", "one.mkv", 77, 5)
        store.add_file("/", "two.mkv", 77, 5)
        store.add_file(
            "/",
            "poster-owner.mkv",
            88,
            8,
            poster=dict(message_id=99, size=5, sha256="a" * 64),
        )
        parts = [
            dict(index=0, offset=0, message_id=111, size=2, sha256="a" * 64),
            dict(index=1, offset=2, message_id=112, size=3, sha256="b" * 64),
        ]
        store.add_file("/", "multipart.mkv", 111, 5, parts=parts)
        find = store._find
        queries = []

        def observed(collection, query, projection=None, **kwargs):
            queries.append((query, projection, kwargs))
            return find(collection, query, projection, **kwargs)

        monkeypatch.setattr(store, "_find", observed)
        after, seen, boundaries = None, {}, []
        while True:
            last, matches = store.cache_metadata_page(
                after, {(77, 5), (99, 5), (77, 999), (111, 2), (111, 5), (112, 3)}
            )
            if last is None:
                break
            assert after is None or last > after
            for key, label in matches.items():
                seen[key] = label if key not in seen or seen[key] == label else None
            after = last
            boundaries.append(last)
        assert len(queries) == 3
        assert seen == {
            (77, 5): None,
            (99, 5): ("poster-owner.mkv", "poster"),
            (111, 2): ("multipart.mkv", "original"),
            (111, 5): ("multipart.mkv", "original"),
            (112, 3): ("multipart.mkv", "original"),
        }
        assert queries[0][0] == {}
        assert queries[1][0] == {"_id": {"$gt": boundaries[0]}}
        projection = queries[0][1]
        assert projection["parts"]["$map"]["input"]["$filter"]["cond"] == {
            "$in": ["$$part.message_id", [77, 99, 111, 112]]
        }
        assert queries[0][2] == {"max_time_ms": 2000}
    finally:
        store.close()


async def test_late_conflict_stays_unknown_and_identical_owner_duplicate_is_safe():
    cache = MediaReadCache()
    first, _ = await cached(cache, 77)
    second, _ = await cached(cache, 88)
    requested = []

    def page(after, wanted):
        requested.append(after)
        assert wanted == {(77, 5), (88, 5)}
        if after is None:
            return "a", {
                (77, 5): ("one.mkv", "original"),
                (88, 5): ("owner.mkv", "poster"),
            }
        if after == "a":
            return "b", {
                (77, 5): ("conflict.mkv", "original"),
                (88, 5): ("owner.mkv", "poster"),
            }
        return None, {}

    before = copy.deepcopy(cache._blocks)
    store = SimpleNamespace(cache_metadata_page=page)
    cache.backfill_metadata(store, CHANNEL)
    task = cache._metadata_task
    cache.backfill_metadata(store, CHANNEL)
    assert cache._metadata_task is task
    await finish(cache)
    assert (
        cache._files[first]["name"] == ""
        and cache._files[first]["purpose"] == "unknown"
    )
    assert (
        cache._files[second]["name"] == "owner.mkv"
        and cache._files[second]["purpose"] == "poster"
    )
    assert cache._blocks == before and requested == [None, "a", "b"]
    cache.backfill_metadata(store, CHANNEL)
    assert cache._metadata_task is task
    await cache.close()


async def test_backfill_preserves_bytes_ranges_and_persists_labels(tmp_path):
    policy = MediaCachePolicy(
        memory_max_bytes=0, disk_max_bytes=4 * 1024**2, min_free_bytes=0
    )
    cache = MediaReadCache(tmp_path, policy)
    assert cache.disk is not None
    identity, key = await cached(cache, 99)
    foreign, _ = await cached(cache, 100, channel=CHANNEL - 1)
    before = await cache.snapshot()
    blocks = copy.deepcopy(cache._blocks)

    def page(after, wanted):
        if after is None:
            return "a", {
                (99, 5): ("Dr Stone.mkv", "poster"),
                (99, 6): ("wrong-size", "original"),
            }
        return None, {}

    cache.backfill_metadata(SimpleNamespace(cache_metadata_page=page), CHANNEL)
    await finish(cache)
    after = await cache.snapshot()
    assert cache._blocks == blocks and cache._files[foreign]["purpose"] == "unknown"
    assert after["files"][0]["id"] in {row["id"] for row in before["files"]}
    assert cache._files[identity]["name"] == "Dr Stone.mkv"
    assert cache.disk.read(key, 5) == b"bytes"
    await cache.close()
    restored = MediaReadCache(tmp_path, policy)
    assert restored.disk is not None
    row = restored._files[identity]
    assert (row["name"], row["purpose"]) == ("Dr Stone.mkv", "poster")
    assert restored._blocks == blocks
    assert restored.disk.read(key, 5) == b"bytes"
    await restored.close()


@pytest.mark.parametrize("action", ["close", "clear"])
async def test_cancelled_backfill_drains_offload_and_never_publishes_stale_labels(
    action,
):
    cache = MediaReadCache()
    identity, _ = await cached(cache, 77)
    blocks = copy.deepcopy(cache._blocks)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def page(after, wanted):
        entered.set()
        try:
            assert release.wait(3)
            return "a", {(77, 5): ("must-not-publish", "original")}
        finally:
            finished.set()

    cache.backfill_metadata(SimpleNamespace(cache_metadata_page=page), CHANNEL)
    assert await asyncio.to_thread(entered.wait, 2)
    stopping = asyncio.create_task(
        cache.close() if action == "close" else cache.clear("nonexistent")
    )
    await asyncio.sleep(0)
    assert not stopping.done() and not finished.is_set()
    release.set()
    await asyncio.wait_for(stopping, 3)
    assert finished.is_set() and cache._files[identity]["name"] == ""
    assert cache._blocks == blocks
    if action != "close":
        await cache.close()


async def test_fresh_registration_wins_and_new_representations_keep_purpose():
    cache = MediaReadCache()
    identity, _ = await cached(cache, 77)
    entered, release = threading.Event(), threading.Event()

    def page(after, wanted):
        if after is not None:
            return None, {}
        entered.set()
        assert release.wait(3)
        return "a", {(77, 5): ("old.mkv", "poster")}

    cache.backfill_metadata(SimpleNamespace(cache_metadata_page=page), CHANNEL)
    assert await asyncio.to_thread(entered.wait, 2)
    cache.register_file(
        channel_id=CHANNEL, message_id=77, size=5, name="fresh.mkv", purpose="original"
    )
    release.set()
    await finish(cache)
    assert cache._files[identity]["name"] == "fresh.mkv"
    cache.register_file(channel_id=CHANNEL, message_id=77, size=5)
    assert cache._files[identity]["purpose"] == "original"
    key = (CHANNEL, 77, "document", 99999, 5, 0)
    cache.register_block(key, 5)
    owner = cache._files[cache._blocks[key]["file_id"]]
    assert (owner["name"], owner["purpose"]) == ("fresh.mkv", "original")
    await cache.close()


@pytest.mark.parametrize("purpose", [None, {}, "invalid"])
async def test_old_or_invalid_purpose_does_not_discard_cached_bytes(tmp_path, purpose):
    policy = MediaCachePolicy(
        memory_max_bytes=0, disk_max_bytes=4 * 1024**2, min_free_bytes=0
    )
    cache = MediaReadCache(tmp_path, policy)
    identity, key = await cached(cache, 77)
    await cache.close()
    path = tmp_path / "inventory.json"
    index = json.loads(path.read_text())
    if purpose is None:
        index["files"][0].pop("purpose", None)
    else:
        index["files"][0]["purpose"] = purpose
    path.write_text(json.dumps(index))
    restored = MediaReadCache(tmp_path, policy)
    assert restored.disk is not None
    assert restored._files[identity]["purpose"] == "unknown"
    assert restored.disk.read(key, 5) == b"bytes"
    await restored.close()


async def test_clear_waits_for_metadata_inventory_writer_before_removing_entry(
    tmp_path, monkeypatch
):
    policy = MediaCachePolicy(
        memory_max_bytes=0, disk_max_bytes=4 * 1024**2, min_free_bytes=0
    )
    cache = MediaReadCache(tmp_path, policy)
    assert cache.disk is not None
    identity, key = await cached(cache, 77)
    await cache.flush()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    write = cache._write_index

    def held(value):
        entered.set()
        assert release.wait(3)
        try:
            write(value)
        finally:
            finished.set()

    monkeypatch.setattr(cache, "_write_index", held)

    def page(after, wanted):
        return (
            ("a", {(77, 5): ("named.mkv", "original")}) if after is None else (None, {})
        )

    cache.backfill_metadata(SimpleNamespace(cache_metadata_page=page), CHANNEL)
    assert await asyncio.to_thread(entered.wait, 2)
    clearing = asyncio.create_task(cache.clear(identity))
    await asyncio.sleep(0)
    assert not clearing.done() and not finished.is_set()
    release.set()
    await asyncio.wait_for(clearing, 3)
    assert finished.is_set() and identity not in cache._files
    assert cache.disk.read(key, 5) is None
    assert json.loads((tmp_path / "inventory.json").read_text())["files"] == []
    await cache.close()


async def test_failed_pass_keeps_bytes_and_does_not_repeat_on_each_poll(caplog):
    cache = MediaReadCache()
    identity, _ = await cached(cache, 77)
    before = copy.deepcopy(cache._blocks)
    calls = []

    def unavailable(after, wanted):
        calls.append(True)
        raise OSError("private query details must not be logged")

    store = SimpleNamespace(cache_metadata_page=unavailable)
    cache.backfill_metadata(store, CHANNEL)
    await finish(cache)
    cache.backfill_metadata(store, CHANNEL)
    assert len(calls) == 1 and cache._blocks == before
    assert cache._files[identity]["purpose"] == "unknown"
    assert "private query" not in caplog.text
    await cache.close()
