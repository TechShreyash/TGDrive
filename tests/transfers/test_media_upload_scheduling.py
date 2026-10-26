"""Bounded account leasing, shared channel pacing and staged window publication."""

import asyncio
import hashlib
import itertools
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types
from telethon.errors import FloodWaitError

from tgdrive.telegram.client_pool import ClientPool, ClientPoolNotRunning
from tgdrive.telegram.clients import Client
from tgdrive.transfers.file_window import FileWindow


class Account:
    def __init__(self, name):
        self.name = name
        self.is_connected = False

    async def start(self):
        self.is_connected = True

    async def stop(self):
        self.is_connected = False


async def test_media_limit_is_per_account_reads_and_second_bot_remain_available():
    pool = ClientPool([Account("one"), Account("two")])
    await pool.start()
    waiting = asyncio.Event()
    acquired = asyncio.Event()

    async def fixed_second():
        waiting.set()
        async with pool.lease(client_name="one", media=True):
            acquired.set()

    try:
        async with pool.lease(client_name="one", media=True):
            task = asyncio.create_task(fixed_second())
            await waiting.wait()
            await asyncio.sleep(0)
            assert not acquired.is_set()
            async with pool.lease(client_name="one") as read:
                assert read.name == "one"
            async with pool.lease(media=True) as other:
                assert other.name == "two"
        await task
        assert acquired.is_set()
        assert (await pool.health()).active_leases == 0
    finally:
        await pool.stop()


async def test_stop_wakes_waiting_media_leases_without_deadlock():
    pool = ClientPool([Account("one")])
    await pool.start()
    waiting = asyncio.Event()

    async def another():
        waiting.set()
        with pytest.raises(ClientPoolNotRunning):
            async with pool.lease(media=True):
                pytest.fail("stopping pool accepted media")

    async with pool.lease(media=True):
        waiter = asyncio.create_task(another())
        await waiting.wait()
        stopping = asyncio.create_task(pool.stop())
        await waiter
    await stopping


async def test_account_flood_wait_and_channel_flood_wait_have_different_scope():
    pool = ClientPool([Account("one"), Account("two")])
    await pool.start()
    try:
        await pool.defer_client("one", time.time() + 0.04)
        async with pool.lease(media=True) as client:
            assert client.name == "two"
        await pool.defer_channel(time.time() + 0.04)
        start = time.monotonic()
        async with pool.lease(media=True):
            assert time.monotonic() - start >= 0.025
    finally:
        await pool.stop()


async def test_send_gate_paces_backups_and_media_on_same_channel():
    pool = ClientPool(media_send_interval=0.03)
    times = []

    async def publish():
        await pool.pace_send(-100123)
        times.append(time.monotonic())

    await asyncio.gather(publish(), publish(), publish())
    assert len(times) == 3
    assert all(b - a >= 0.02 for a, b in itertools.pairwise(times))


def test_file_window_cannot_read_or_seek_into_neighbor_parts(tmp_path):
    path = tmp_path / "staged"
    path.write_bytes(b"prefixBODYsuffix")
    with FileWindow(path, 6, 4) as source:
        assert source.read(512 * 1024) == b"BODY"
        assert source.read() == b""
        source.verify(hashlib.sha256(b"BODY").hexdigest())
        with pytest.raises(ValueError, match="outside"):
            source.seek(5)
        source.seek(0)
        assert source.read(2) == b"BO"
        assert source.read() == b"DY"


class Native:
    def __init__(self):
        self.uploaded = []
        self.requests = []
        self.fail_publish = False
        self.fail_blob = False
        self.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(1, 2))

    async def upload_parts(self, source, *, file_name, length, progress):
        if self.fail_blob:
            raise FloodWaitError(request=None, capture=0)
        data = bytearray()
        while chunk := source.read(512 * 1024):
            data.extend(chunk)
            if progress:
                await progress(len(data), length)
        assert len(data) == length
        self.uploaded.append(bytes(data))
        return types.InputFileBig(123, 1, file_name)

    async def __call__(self, request):
        self.requests.append(request)
        if self.fail_publish:
            raise FloodWaitError(request=None, capture=0)
        return SimpleNamespace()


def client_for(native):
    client = Client.__new__(Client)
    client.native = native
    client._upload_parts = AsyncMock(side_effect=native.upload_parts)
    client._sent = AsyncMock(return_value=SimpleNamespace(id=100))
    return client


async def test_window_upload_hashes_exact_bytes_and_checkpoints_before_send(tmp_path):
    path = tmp_path / "staged"
    path.write_bytes(b"beforePAYLOADafter")
    native = Native()
    client = client_for(native)
    calls = []

    async def checkpoint():
        assert native.uploaded == [b"PAYLOAD"]
        assert native.requests == []
        calls.append("durable")

    await client.send_staged(
        -100123,
        path,
        file_name="part.bin",
        random_id=123,
        offset=6,
        size=7,
        expected_sha256=hashlib.sha256(b"PAYLOAD").hexdigest(),
        before_dispatch=checkpoint,
    )
    assert calls == ["durable"]
    assert native.requests[0].random_id == 123
    assert native.requests[0].media.force_file


async def test_changed_window_or_failed_durable_checkpoint_cannot_publish(tmp_path):
    path = tmp_path / "staged"
    path.write_bytes(b"changed")
    native = Native()
    client = client_for(native)
    with pytest.raises(ValueError, match="changed"):
        await client.send_staged(
            -100123,
            path,
            file_name="part.bin",
            random_id=123,
            size=7,
            expected_sha256=hashlib.sha256(b"ORIGINAL").hexdigest(),
        )
    assert native.requests == []

    async def failed_checkpoint():
        raise OSError("synthetic journal failure")

    with pytest.raises(OSError, match="journal"):
        await client.send_staged(
            -100123,
            path,
            file_name="part.bin",
            random_id=123,
            size=7,
            expected_sha256=hashlib.sha256(b"changed").hexdigest(),
            before_dispatch=failed_checkpoint,
        )
    assert native.requests == []


async def test_actual_sendmedia_flood_defers_shared_channel_but_blob_flood_does_not(
    tmp_path,
):
    path = tmp_path / "staged"
    path.write_bytes(b"abc")
    native = Native()
    client = client_for(native)
    gate = AsyncMock()
    defer = AsyncMock()
    client.bind_send_gate(gate, defer)
    native.fail_blob = True
    with pytest.raises(FloodWaitError) as blob:
        await client.send_staged(-100123, path, file_name="x", random_id=1)
    assert not getattr(blob.value, "tgdrive_channel_flood", False)
    defer.assert_not_awaited()
    native.fail_blob = False
    native.fail_publish = True
    with pytest.raises(FloodWaitError) as message:
        await client.send_staged(-100123, path, file_name="x", random_id=1)
    assert message.value.tgdrive_channel_flood
    gate.assert_awaited_once_with(-100123)
    defer.assert_awaited_once()
    assert defer.await_args.args[0] >= time.time()


@pytest.mark.parametrize("shared_object", [False, True])
async def test_duplicate_runtime_identity_has_one_usable_media_slot(shared_object):
    one = Account("same-account")
    two = one if shared_object else Account("same-account")
    pool = ClientPool([one, two], allow_partial_start=True)
    await pool.start()
    acquired = asyncio.Event()

    async def another():
        async with pool.lease(media=True):
            acquired.set()

    try:
        states = await pool.status()
        assert sum(item.healthy for item in states) == 1
        assert any(item.last_error_type == "DuplicateClientIdentity" for item in states)
        assert (await pool.health()).ready
        assert one.is_connected
        if not shared_object:
            assert not two.is_connected
        async with pool.lease(media=True) as leased:
            assert leased is one
            waiter = asyncio.create_task(another())
            await asyncio.sleep(0)
            assert not acquired.is_set()
        await waiter
        assert acquired.is_set()
    finally:
        await pool.stop()


async def test_prestart_account_cooldown_survives_factory_identity_resolution():
    async def factory():
        return Account("actual-telegram-account")

    pool = ClientPool([factory], allow_partial_start=True)
    deadline = time.time() + 30
    await pool.defer_client("actual-telegram-account", deadline)
    await pool.start()
    try:
        assert pool._slots[0].cooldown_until == deadline
        assert (await pool.health()).ready
    finally:
        await pool.stop()


async def test_live_limit_changes_preserve_running_uploads_and_fifo():
    pool = ClientPool([Account("one")])
    await pool.start()
    entered = []
    releases = [asyncio.Event() for _ in range(4)]

    async def work(index):
        async with pool.lease(media=True):
            entered.append(index)
            await releases[index].wait()

    tasks = [asyncio.create_task(work(index)) for index in range(4)]
    try:
        await asyncio.sleep(0.01)
        assert entered == [0]
        await pool.set_uploads_per_bot(2)
        await asyncio.sleep(0.01)
        assert entered == [0, 1]
        await pool.set_uploads_per_bot(1)
        releases[0].set()
        await asyncio.sleep(0.01)
        assert entered == [0, 1]  # Existing second lease finishes; no new admission.
        releases[1].set()
        await asyncio.sleep(0.01)
        assert entered == [0, 1, 2]
        releases[2].set()
        await asyncio.sleep(0.01)
        assert entered == [0, 1, 2, 3]
    finally:
        for release in releases:
            release.set()
        await asyncio.gather(*tasks)
        await pool.stop()
