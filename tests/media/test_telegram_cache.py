from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon import types, utils

from tgdrive.telegram.clients import Client, TelegramMessage, _BlockCache

MIB = 1024**2


def document(identity, size):
    return types.Document(
        id=identity,
        access_hash=2,
        file_reference=b"current",
        date=datetime.now(UTC),
        mime_type="video/mp4",
        size=size,
        dc_id=2,
        attributes=[types.DocumentAttributeFilename(file_name="original.mp4")],
    )


def message(identity, size, alternates=()):
    return types.Message(
        id=77,
        peer_id=types.PeerChannel(123),
        date=datetime.now(UTC),
        message="",
        media=types.MessageMediaDocument(
            document=document(identity, size), alt_documents=list(alternates)
        ),
    )


class Iterator:
    def __init__(self, owner, identity, offset, limit):
        self.owner, self.identity, self.offset, self.limit = (
            owner,
            identity,
            offset,
            limit,
        )
        self.count = 0
        self.closed = False
        owner.active += 1
        owner.peak = max(owner.peak, owner.active)
        owner.iterators.append(self)

    def __aiter__(self):
        return self

    async def __anext__(self):
        data = self.owner.payloads[self.identity]
        if self.count >= self.limit or self.offset >= len(data):
            raise StopAsyncIteration
        self.owner.requests[(self.identity, self.offset)] += 1
        self.count += 1
        if self.owner.gate is not None:
            await self.owner.gate.wait()
        await asyncio.sleep(0)
        result = data[self.offset : self.offset + MIB // 2]
        self.offset += len(result)
        return result

    async def close(self):
        if not self.closed:
            self.closed = True
            self.owner.active -= 1


class Native:
    def __init__(self, payloads, gate=None):
        self.payloads, self.gate = payloads, gate
        self.requests = Counter()
        self.active = self.peak = 0
        self.iterators = []

    def iter_download(self, target, *, offset, request_size, limit):
        dc, location = utils.get_input_location(target)
        assert dc == 2 and request_size == MIB // 2
        return Iterator(self, location.id, offset, limit)


def transport(native, name="account-one"):
    value = Client.__new__(Client)
    value.native, value.name = native, name
    return value


async def read(client, source, limit=1, offset=0):
    return b"".join(
        [part async for part in client.stream_media(source, limit=limit, offset=offset)]
    )


async def wait_for(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("state did not settle")


@pytest.mark.asyncio
async def test_repeated_overlapping_and_final_window_reads_reuse_confirmed_bytes():
    payload = b"a" * MIB + b"b" * MIB + b"c" * MIB + b"tail"
    native = Native({1: payload})
    client = transport(native)
    source = TelegramMessage(client, message(1, len(payload)))
    assert await read(client, source) == payload[:MIB]
    assert await read(client, source) == payload[:MIB]
    assert await read(client, source, limit=2) == payload[: 2 * MIB]
    assert await read(client, source, offset=3) == b"tail"
    assert await read(client, source, offset=3) == b"tail"
    assert sum(native.requests.values()) == 5  # two complete windows + one tail
    assert all(count == 1 for count in native.requests.values())
    assert client._download_block_cache.bytes_used == 2 * MIB + 4
    assert native.active == 0


@pytest.mark.asyncio
async def test_variants_replacements_and_accounts_never_share_wrong_bytes():
    payloads = {1: b"o" * MIB, 2: b"v" * MIB, 3: b"n" * MIB}
    native = Native(payloads)
    client = transport(native)
    source = TelegramMessage(client, message(1, MIB, [document(2, MIB)]))
    variant = client.select_media_variant(
        source, client.describe_media(source)["variants"][0]["id"]
    )
    for _ in range(2):
        assert await read(client, source) == payloads[1]
        assert await read(client, variant) == payloads[2]
    replacement = TelegramMessage(client, message(3, MIB))
    assert await read(client, replacement) == payloads[3]
    assert sum(native.requests.values()) == 6
    other_native = Native(payloads)
    other = transport(other_native, "account-two")
    assert await read(other, TelegramMessage(other, message(1, MIB))) == payloads[1]
    assert sum(other_native.requests.values()) == 2
    assert other._download_block_cache is client._download_block_cache


@pytest.mark.asyncio
async def test_partial_windows_are_not_cached_as_confirmed_bytes():
    native = Native({1: b"x" * (MIB // 2)})
    client = transport(native)
    source = TelegramMessage(client, message(1, MIB))
    assert len(await read(client, source)) == MIB // 2
    assert len(await read(client, source)) == MIB // 2
    assert sum(native.requests.values()) == 2
    assert client._download_block_cache.bytes_used == 0


@pytest.mark.asyncio
async def test_one_cancelled_waiter_does_not_cancel_other_shared_readers():
    gate = asyncio.Event()
    native = Native({1: b"x" * MIB}, gate)
    client = transport(native)
    source = TelegramMessage(client, message(1, MIB))
    readers = [asyncio.create_task(read(client, source)) for _ in range(3)]
    await wait_for(
        lambda: (
            getattr(client, "_download_block_cache", None) is not None
            and any(
                f.waiters == 3 for f in client._download_block_cache.flights.values()
            )
        )
    )
    readers[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await readers[0]
    assert native.active == 1 and not native.iterators[0].closed
    gate.set()
    assert await readers[1] == await readers[2] == b"x" * MIB
    assert sum(native.requests.values()) == 2
    assert native.active == 0 and not client._download_block_cache.flights


@pytest.mark.asyncio
async def test_last_waiter_cancel_drains_read_and_a_new_request_starts_cleanly():
    gate = asyncio.Event()
    native = Native({1: b"x" * MIB}, gate)
    client = transport(native)
    source = TelegramMessage(client, message(1, MIB))
    first = asyncio.create_task(read(client, source))
    await wait_for(lambda: native.active == 1)
    first.cancel()
    second = asyncio.create_task(read(client, source))
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    assert await second == b"x" * MIB
    assert native.peak == 1
    assert all(iterator.closed for iterator in native.iterators)
    assert not client._download_block_cache.flights


@pytest.mark.asyncio
async def test_cache_lru_byte_entry_and_expiry_bounds():
    clock = SimpleNamespace(now=0.0)
    cache = _BlockCache(max_bytes=4, max_entries=2, ttl=30, clock=lambda: clock.now)
    loads = []

    async def load(value):
        loads.append(value)
        return value

    async def get(key, value):
        return await cache.get((key,), len(value), lambda: load(value))

    await get("a", b"aa")
    await get("b", b"bb")
    await get("a", b"aa")
    await get("c", b"cc")
    assert list(cache.entries) == [("a",), ("c",)]
    assert cache.bytes_used == 4 and len(loads) == 3
    clock.now = 29
    assert await get("a", b"aa") == b"aa"
    clock.now = 30
    assert await get("a", b"aa") == b"aa"
    assert len(loads) == 4 and cache.bytes_used == 2
    # Tiny payloads are also bounded by entry count, independent of byte budget.
    await get("d", b"d")
    await get("e", b"e")
    assert len(cache.entries) == 2 and cache.bytes_used == 2


@pytest.mark.asyncio
async def test_global_unique_read_bound_and_cancelled_admission_leave_no_tasks():
    cache = _BlockCache(read_slots=2)
    gate = asyncio.Event()
    state = SimpleNamespace(active=0, peak=0)

    async def load():
        state.active += 1
        state.peak = max(state.peak, state.active)
        try:
            await gate.wait()
            return b"x"
        finally:
            state.active -= 1

    readers = [asyncio.create_task(cache.get((index,), 1, load)) for index in range(5)]
    await wait_for(lambda: state.active == 2)
    assert len(cache.flights) == 2
    for reader in readers:
        reader.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    assert state.peak == 2 and state.active == 0
    assert not cache.flights and cache.bytes_used == 0
    assert await cache.get((99,), 1, lambda: asyncio.sleep(0, result=b"y")) == b"y"


@pytest.mark.asyncio
async def test_failed_reads_release_capacity_and_are_not_cached():
    cache = _BlockCache(read_slots=1)

    async def fail():
        raise RuntimeError("synthetic provider failure")

    with pytest.raises(RuntimeError, match="provider failure"):
        await cache.get((1,), 1, fail)
    assert not cache.entries and not cache.flights
    assert await cache.get((1,), 1, lambda: asyncio.sleep(0, result=b"z")) == b"z"


@pytest.mark.asyncio
async def test_controlled_player_seek_pattern_halves_native_part_requests(monkeypatch):
    import tgdrive.telegram.clients as module

    class Uncached:
        async def get(self, key, expected, load):
            return await load()

    payload = b"a" * MIB + b"b" * MIB + b"c" * MIB + b"tail"
    pattern = [(0, 1), (0, 1), (0, 2), (3, 1), (3, 1), (1, 2)]
    observed = []
    for cached in (False, True):
        native = Native({1: payload})
        client = transport(native)
        source = TelegramMessage(client, message(1, len(payload)))
        with monkeypatch.context() as patch:
            if not cached:
                patch.setattr(module, "_client_block_cache", lambda _: Uncached())
            for offset, limit in pattern:
                assert (
                    await read(client, source, limit=limit, offset=offset)
                    == payload[offset * MIB : (offset + limit) * MIB]
                )
        observed.append(sum(native.requests.values()))
        assert native.active == 0
    assert observed == [14, 7]


def test_cached_payloads_are_not_reused_across_event_loops():
    native = Native({1: b"x" * MIB})
    client = transport(native)
    source = TelegramMessage(client, message(1, MIB))
    assert asyncio.run(read(client, source)) == b"x" * MIB
    first = client._download_block_cache
    assert asyncio.run(read(client, source)) == b"x" * MIB
    assert client._download_block_cache is not first
    assert sum(native.requests.values()) == 4
