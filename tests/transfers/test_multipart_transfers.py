"""Small-threshold integration probes for durable multi-message logical files."""

import asyncio
import hashlib
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError

from tests.support.mongo_fixtures import register_test_backend
from tgdrive.storage.mongo import for_directory
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool, NoClientAvailable
from tgdrive.transfers.manager import TransferError, TransferManager
from tgdrive.transfers.multipart_transfer import fingerprint
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path)


def registry_for(directory):
    backend = for_directory(directory)
    return TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)


@pytest.mark.parametrize("payload", [b"", b"a", b"abcd", b"abcde", b"abcdefgh"])
def test_fingerprint_preserves_whole_and_part_hashes_at_split_boundaries(
    tmp_path, payload
):
    source = tmp_path / "fingerprint.bin"
    source.write_bytes(payload)
    checksum, parts = fingerprint(source, len(payload), 4)
    assert checksum == hashlib.sha256(payload).hexdigest()
    assert len(parts) == (len(payload) + 3) // 4
    for index, part in enumerate(parts):
        content = payload[index * 4 : (index + 1) * 4]
        assert part["index"] == index
        assert part["offset"] == index * 4
        assert part["size"] == len(content)
        assert part["sha256"] == hashlib.sha256(content).hexdigest()
    assert source.read_bytes() == payload


class Ledger:
    def __init__(self):
        self.messages = {}
        self.requests = {}
        self.calls = []
        self.next_id = 100
        self.lose_once = False
        self.flood_seconds = 0
        self.rpc_delay = 0
        self.fail_cleanup = False
        self.flood_cleanup = False
        self.hold = False
        self.dispatched = asyncio.Event()
        self.both_dispatched = asyncio.Event()
        self.three_dispatched = asyncio.Event()
        self.flooded = asyncio.Event()
        self.release = asyncio.Event()
        self.active = {}
        self.max_active = {}


class Account:
    def __init__(self, name, ledger, *, flood=False):
        self.name = name
        self.ledger = ledger
        self.flood = flood
        self.is_connected = False

    async def start(self):
        self.is_connected = True

    async def stop(self):
        self.is_connected = False

    async def send_staged(
        self,
        channel,
        path,
        *,
        file_name,
        random_id,
        progress,
        offset,
        size,
        expected_sha256,
        before_dispatch,
    ):
        state = self.ledger
        state.active[self.name] = state.active.get(self.name, 0) + 1
        state.max_active[self.name] = max(
            state.max_active.get(self.name, 0), state.active[self.name]
        )
        try:
            state.calls.append((self.name, random_id, offset, size))
            if self.flood:
                self.flood = False
                state.flooded.set()
                raise FloodWaitError(request=None, capture=state.flood_seconds)
            content = path.read_bytes()[offset : offset + size]
            assert len(content) == size
            assert hashlib.sha256(content).hexdigest() == expected_sha256
            await progress(size, size)
            (await before_dispatch())
            if state.rpc_delay:
                await asyncio.sleep(state.rpc_delay)
            key = (self.name, random_id)
            if key not in state.requests:
                state.next_id += 1
                message = SimpleNamespace(
                    id=state.next_id,
                    document=SimpleNamespace(file_size=size),
                    content=content,
                )
                message.native = SimpleNamespace(
                    document=SimpleNamespace(id=10_000 + message.id),
                )
                state.messages[message.id] = message
                state.requests[key] = message.id
            state.dispatched.set()
            if len(state.messages) >= 2:
                state.both_dispatched.set()
            if len(state.messages) >= 3:
                state.three_dispatched.set()
            if state.hold:
                await state.release.wait()
            if state.lose_once:
                state.lose_once = False
                raise TimeoutError("synthetic lost response after remote publication")
            return state.messages[state.requests[key]]
        finally:
            state.active[self.name] -= 1

    async def get_messages(self, channel, *, message_ids):
        return self.ledger.messages.get(message_ids)

    async def delete_messages(self, channel, message_ids):
        if self.ledger.fail_cleanup:
            raise ConnectionError("synthetic unavailable deletion")
        if self.ledger.flood_cleanup:
            self.ledger.flood_cleanup = False
            raise FloodWaitError(request=None, capture=0)
        for identity in message_ids if isinstance(message_ids, list) else [message_ids]:
            self.ledger.messages.pop(identity, None)


async def manager_for(
    tmp_path, *, ledger=None, users=False, callback=None, flood=False
):
    ledger = ledger or Ledger()
    bots = [Account("bot-one", ledger, flood=flood), Account("bot-two", ledger)]
    pool = ClientPool(bots, [Account("premium", ledger)] if users else [])
    await pool.start()
    settings = SimpleNamespace(
        resolved_cache_dir=tmp_path / "cache",
        max_concurrent_transfers=2,
        storage_channel=-100123,
        bot_upload_limit=4,
        upload_limit=100,
        staging_budget_bytes=16 * 1024**3,
        min_free_bytes=0,
    )
    manager = TransferManager(
        settings=settings,
        pool=pool,
        store=DriveStore.create(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path),
        remote=SimpleNamespace(close=AsyncMock()),
        ensure_backup=callback,
    )
    return manager, ledger


async def dispose(manager):
    await manager.close()
    await manager.pool.stop()
    manager.registry.close()
    manager.store.close()


def staged(manager, content):
    source = manager.cache_dir / "source.upload"
    source.write_bytes(content)
    return source


def stored(manager):
    rows = manager.store.list_directory("/")
    assert len(rows) == 1
    return manager.store.get_file("/" + next(iter(rows)))


async def test_bot_only_parts_commit_once_then_checkpoint(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def protect(revision):
        entered.set()
        await release.wait()
        return revision

    manager, ledger = await manager_for(tmp_path, callback=protect)
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        item = stored(manager)
        assert item["size"] == 10
        assert [p["size"] for p in item["parts"]] == [4, 4, 2]
        assert [p["offset"] for p in item["parts"]] == [0, 4, 8]
        assert item["sha256"] == hashlib.sha256(b"abcdefghij").hexdigest()
        assert all(p["document_id"] == 10_000 + p["message_id"] for p in item["parts"])
        assert (
            b"".join(ledger.messages[p["message_id"]].content for p in item["parts"])
            == b"abcdefghij"
        )
        assert record.phase == "backup_pending"
        assert source.exists()
        assert (await manager.pool.health()).active_leases == 0
        assert all(value == 1 for value in ledger.max_active.values())
        release.set()
        await record.task
        assert record.status == TransferStatus.COMPLETED
        assert not source.exists()
    finally:
        release.set()
        await dispose(manager)


async def test_telegram_recovery_clears_waiting_error_before_completion(
    tmp_path, monkeypatch
):
    manager, _ = await manager_for(tmp_path)
    lease = manager.pool.lease
    attempts = 0

    @asynccontextmanager
    async def recover_after_one_wait(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise NoClientAvailable("Telegram temporarily unavailable")
        async with lease(*args, **kwargs) as client:
            yield client

    monkeypatch.setattr(manager.pool, "lease", recover_after_one_wait)
    try:
        record = await manager.enqueue_upload(
            staged(manager, b"abc"), "/", "small.bin", 3
        )
        await record.task

        assert attempts == 2
        assert record.status is TransferStatus.COMPLETED
        assert record.error is None
        assert record.as_dict()["error"] is None
        assert stored(manager)["size"] == 3
    finally:
        await dispose(manager)


async def test_lost_response_replays_same_sender_random_id_skips_confirmed_parts(
    tmp_path,
):
    manager, ledger = await manager_for(tmp_path)
    ledger.lose_once = True
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    try:
        await record.task
        assert record.status == TransferStatus.ERROR
        plan = record.context["multipart_plan"]
        identities = [(p["sender"], p["random_id"]) for p in plan["parts"]]
        assert not manager.store.list_directory("/")
        before_count = len(ledger.messages)
        record = await manager.retry_job(record.id)
        await record.task
        assert record.status == TransferStatus.COMPLETED
        for before, part in zip(identities, plan["parts"], strict=True):
            assert part["random_id"] == before[1]
            if before[0] is not None:
                assert part["sender"] == before[0]
        assert len(ledger.messages) == 3
        assert before_count <= 3
        assert len(ledger.requests) == 3
        assert len(stored(manager)["parts"]) == 3
    finally:
        await dispose(manager)


async def test_process_restart_reconciles_ambiguous_part_and_reuses_plan(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold = True
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    await asyncio.wait_for(ledger.dispatched.wait(), 2)
    original = [
        (p["random_id"], p["sha256"]) for p in record.context["multipart_plan"]["parts"]
    ]
    identity = record.id
    await manager.close()
    manager.registry.close()
    manager.store.close()
    await manager.pool.stop()
    ledger.hold = False
    ledger.release.set()
    pool = ClientPool([Account("bot-one", ledger), Account("bot-two", ledger)])
    await pool.start()
    recovered = TransferManager(
        settings=manager.settings,
        pool=pool,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path),
        remote=SimpleNamespace(close=AsyncMock()),
    )
    try:
        await recovered.recover()
        row = recovered.registry.get(identity)
        await row.task
        assert row.status == TransferStatus.COMPLETED
        assert [
            (p["random_id"], p["sha256"])
            for p in row.context["multipart_plan"]["parts"]
        ] == original
        assert len(ledger.requests) == len(ledger.messages) == 3
        assert len(stored(recovered)["parts"]) == 3
    finally:
        await dispose(recovered)


async def test_cancel_reconciles_and_deletes_every_own_part_before_unlink(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold = True
    source = staged(manager, b"abcdefghijkl")
    record = await manager.enqueue_upload(source, "/", "twelve.bin", 12)
    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        (await manager.cancel_job(record.id))
        assert source.exists()
        ledger.release.set()
        await record.task
        assert record.status == TransferStatus.CANCELLED
        assert ledger.messages == {}
        assert not manager.store.list_directory("/")
        assert not source.exists()
        assert all(
            p.get("deleted")
            for p in record.context["multipart_plan"]["parts"]
            if p.get("message_id")
        )
    finally:
        ledger.release.set()
        await dispose(manager)


async def test_cancel_cleanup_failure_retains_bytes_and_retries_cleanup(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold = True
    ledger.fail_cleanup = True
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        (await manager.cancel_job(record.id))
        ledger.release.set()
        await record.task
        assert record.status != TransferStatus.CANCELLED
        assert source.exists() and ledger.messages
        assert manager.store.list_directory("/") == {}
        sent = len(ledger.requests)
        ledger.fail_cleanup = False
        record = await manager.retry_job(record.id)
        await record.task
        assert record.status == TransferStatus.CANCELLED
        assert not source.exists() and not ledger.messages
        assert len(ledger.requests) == sent
    finally:
        ledger.release.set()
        await dispose(manager)


@pytest.mark.parametrize("healthy", [True, False])
@pytest.mark.parametrize("split_mib", [0, 2000])
async def test_premium_never_raises_new_part_size(tmp_path, healthy, split_mib):
    manager, ledger = await manager_for(tmp_path, users=True)
    manager.settings.upload_split_mib = split_mib
    if not healthy:
        user = next(slot.client for slot in manager.pool._slots if slot.kind == "user")
        await manager.pool.mark_unhealthy(user)
    source = staged(manager, b"abcdef")
    record = await manager.enqueue_upload(source, "/", "six.bin", 6)
    try:
        await record.task
        assert record.status == TransferStatus.COMPLETED
        assert [part["size"] for part in stored(manager)["parts"]] == [4, 2]
        assert record.context["multipart_plan"]["kind"] == "bot"
        assert ledger.calls[0][0] == ("premium" if healthy else "bot-one")
    finally:
        await dispose(manager)


async def test_smaller_split_applies_to_premium_and_preserves_original(tmp_path):
    manager, ledger = await manager_for(tmp_path, users=True)
    manager.settings.bot_upload_limit = 4 * 1024**2
    manager.settings.upload_split_mib = 1
    payload = b"a" * (3 * 1024**2 + 7)
    record = await manager.enqueue_upload(
        staged(manager, payload), "/", "data.bin", len(payload)
    )
    try:
        await record.task
        assert record.status == TransferStatus.COMPLETED
        parts = stored(manager)["parts"]
        assert [part["size"] for part in parts] == [1024**2] * 3 + [7]
        assert ledger.calls[0][0] == "premium"
        assert (
            b"".join(ledger.messages[p["message_id"]].content for p in parts) == payload
        )
    finally:
        await dispose(manager)


async def test_obsolete_premium_sized_plan_is_rejected_without_sending(tmp_path):
    from tgdrive.transfers.multipart_transfer import fingerprint

    manager, ledger = await manager_for(tmp_path, users=True)
    payload = b"abcdef"
    source = staged(manager, payload)
    checksum, parts = fingerprint(source, len(payload), len(payload))
    parts[0]["sender"] = "premium"
    record = await manager.registry.create("queued", total=len(payload))
    record.context.update(
        sha256=checksum,
        multipart_plan=dict(version=1, kind="user", parts=parts),
    )
    (await manager.registry.persist(record))
    try:
        with pytest.raises(TransferError, match="multipart journal is invalid"):
            await manager._upload_path(record, source, "/", "old.bin", len(payload))
        assert source.read_bytes() == payload
        assert not ledger.calls and not ledger.messages
        assert manager.store.list_directory("/") == {}
        assert record.context["multipart_plan"]["kind"] == "user"
    finally:
        await dispose(manager)


async def test_upload_part_flood_wait_persists_fixed_sender_and_retries(tmp_path):
    manager, ledger = await manager_for(tmp_path, flood=True)
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    try:
        await record.task
        assert record.status == TransferStatus.COMPLETED
        first = ledger.calls[0]
        repeated = [call for call in ledger.calls if call[1] == first[1]]
        assert len(repeated) == 2
        assert {call[0] for call in repeated} == {first[0]}
        assert len(ledger.messages) == 3
    finally:
        await dispose(manager)


async def test_global_media_slot_not_held_while_fixed_sender_cooling(tmp_path):
    manager, _ledger = await manager_for(tmp_path)
    await manager.pool.defer_client("bot-one", time.time() + 0.05)
    async with manager.pool.lease(client_name="bot-one", media=True) as client:
        assert client.name == "bot-one"
    assert manager._semaphore._value == 2
    await dispose(manager)


async def test_cancel_refuses_to_delete_replaced_remote_document(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold = True
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        (await manager.cancel_job(record.id))
        ledger.release.set()
        # Replace only during cleanup, after the accepted identity was journalled.
        original = manager._verify_lineage

        async def replace_before_delete(item=None):
            if any(
                p.get("document_id") for p in record.context["multipart_plan"]["parts"]
            ):
                message = next(iter(ledger.messages.values()))
                message.native.document.id += 1
            await original(item)

        manager._verify_lineage = replace_before_delete
        await record.task
        assert record.status != TransferStatus.CANCELLED
        assert source.exists()
        assert ledger.messages
        assert manager.store.list_directory("/") == {}
    finally:
        ledger.release.set()
        await dispose(manager)


async def test_refetch_rejects_changed_native_document_identity(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    # Fail catalog publication after all piece identities have been persisted.
    original = manager.store.add_file
    manager.store.add_file = lambda *a, **kw: (_ for _ in ()).throw(
        OSError("synthetic commit error")
    )
    try:
        await record.task
        assert record.status == TransferStatus.ERROR
        part = record.context["multipart_plan"]["parts"][0]
        part["confirmed"] = False
        ledger.messages[part["message_id"]].native.document.id += 1
        (await manager.registry.persist(record))
        manager.store.add_file = original
        retried = await manager.retry_job(record.id)
        await retried.task
        assert retried.status == TransferStatus.ERROR
        assert "identity changed" in retried.error
        assert source.exists()
        assert manager.store.list_directory("/") == {}
    finally:
        await dispose(manager)


async def test_cleanup_flood_wait_is_durable_and_does_not_rotate_sender(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold = True
    ledger.flood_cleanup = True
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        (await manager.cancel_job(record.id))
        ledger.release.set()
        await record.task
        assert record.status == TransferStatus.CANCELLED
        assert record.context["channel_retry_at"] > 0
        assert not ledger.messages
        assert not source.exists()
    finally:
        ledger.release.set()
        await dispose(manager)


async def test_one_flooding_part_does_not_hide_other_active_part_progress(tmp_path):
    manager, ledger = await manager_for(tmp_path, flood=True)
    ledger.hold = True
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        assert ledger.flooded.is_set()
        assert record.phase in {"uploading", "finalizing"}
        assert record.as_dict()["retry_at"] is None
        ledger.release.set()
        await record.task
        assert record.status == TransferStatus.COMPLETED
    finally:
        ledger.release.set()
        await dispose(manager)


async def test_fully_waiting_logical_upload_exposes_provider_retry_deadline(tmp_path):
    manager, ledger = await manager_for(tmp_path, flood=True)
    second = next(slot.client for slot in manager.pool._slots if slot.name == "bot-two")
    await manager.pool.mark_unhealthy(second)
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    try:
        await asyncio.wait_for(ledger.flooded.wait(), 2)
        async with asyncio.timeout(2):
            while record.phase != "retry_wait":  # noqa: ASYNC110 - observe committed state
                await asyncio.sleep(0.001)
        assert record.phase == "retry_wait"
        assert record.as_dict()["retry_at"] > time.time()
        await record.task
        assert record.status == TransferStatus.COMPLETED
    finally:
        await dispose(manager)


@pytest.mark.parametrize("lost_acknowledgement", [False, True])
async def test_cancel_recovery_skips_deleted_unconfirmed_parts(
    tmp_path,
    monkeypatch,
    lost_acknowledgement,
):
    manager, ledger = await manager_for(tmp_path)
    ledger.hold = True
    source = staged(manager, b"abcdefghij")
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    calls = 0
    failed = False
    originals = []

    for slot in manager.pool._slots:
        client = slot.client
        original = client.delete_messages
        originals.append((client, original))

        async def interrupted_delete(channel, identities, *, original=original):
            nonlocal calls, failed
            calls += 1
            if calls == 2 and not failed:
                failed = True
                raise ConnectionError("synthetic second-part cleanup interruption")
            await original(channel, identities)

        monkeypatch.setattr(client, "delete_messages", interrupted_delete)
    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        # Let both worker dispatches reach the remote-effect boundary.
        await asyncio.wait_for(ledger.both_dispatched.wait(), 2)
        (await manager.cancel_job(record.id))
        ledger.release.set()
        await record.task
        assert record.status == TransferStatus.QUEUED
        assert source.exists()
        parts = record.context["multipart_plan"]["parts"]
        deleted = [p for p in parts if p.get("deleted")]
        assert len(deleted) == 1
        assert not deleted[0]["confirmed"]
        if lost_acknowledgement:
            deleted[0].pop("deleted")
            (await manager.registry.persist(record))
        requests = len(ledger.requests)
        for client, original in originals:
            monkeypatch.setattr(client, "delete_messages", original)
        retried = await manager.retry_job(record.id)
        await retried.task
        assert retried.status == TransferStatus.CANCELLED
        assert not ledger.messages
        assert not source.exists()
        assert len(ledger.requests) == requests
        assert manager.store.list_directory("/") == {}
    finally:
        ledger.release.set()
        await dispose(manager)


async def test_restart_restores_flood_cooldown_from_terminal_error_job(tmp_path):
    manager, ledger = await manager_for(tmp_path, flood=True)
    ledger.flood_seconds = 30
    ledger.lose_once = True
    source = staged(manager, b"abcdefghij")
    failed = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    await failed.task
    assert failed.status == TransferStatus.ERROR
    assert failed.context["multipart_plan"]["parts"][0]["retry_at"] > time.time()
    await manager.close()
    manager.registry.close()
    manager.store.close()
    await manager.pool.stop()
    pool = ClientPool([Account("bot-one", ledger), Account("bot-two", ledger)])
    await pool.start()
    recovered = TransferManager(
        settings=manager.settings,
        pool=pool,
        store=DriveStore.load(tmp_path / "drive.sqlite3"),
        registry=registry_for(tmp_path),
        remote=SimpleNamespace(close=AsyncMock()),
    )
    try:
        await recovered.recover()
        resumed = recovered.registry.get(failed.id)
        assert resumed.task is not None
        assert resumed.context["multipart_plan"]["parts"][0]["retry_at"] > time.time()
        before = len(ledger.calls)
        fresh = manager.cache_dir / "new.upload"
        fresh.write_bytes(b"x")
        job = await recovered.enqueue_upload(fresh, "/", "new.bin", 1)
        await asyncio.wait_for(job.task, 2)
        assert job.status == TransferStatus.COMPLETED
        assert ledger.calls[-1][0] == "bot-two"
        assert all(call[0] != "bot-one" for call in ledger.calls[before:])
        assert time.time() < failed.context["multipart_plan"]["parts"][0]["retry_at"]
        assert not resumed.task.done()
    finally:
        await dispose(recovered)


async def test_recover_hydrates_channel_cooldown_before_any_lineage_await(tmp_path):
    manager, _ = await manager_for(tmp_path)
    row = await manager.registry.create("failed")
    row.status = TransferStatus.ERROR
    row.context.update(kind="upload", channel_retry_at=time.time() + 30)
    (await manager.registry.persist(row))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def guard():
        entered.set()
        await release.wait()

    # This pending row makes recover await lineage after loading all cooldowns.
    (await manager.registry.create("queued"))
    manager._ensure_lineage = guard
    recovering = asyncio.create_task(manager.recover())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert manager.pool._channel_cooldown_until >= row.context["channel_retry_at"]
        assert all(slot.active == 0 for slot in manager.pool._slots)
    finally:
        release.set()
        await recovering
        await dispose(manager)


@pytest.mark.parametrize("user_cancel", [False, True])
async def test_hash_thread_is_drained_and_stage_cleanup_distinguishes_shutdown(
    tmp_path,
    monkeypatch,
    user_cancel,
):
    from tgdrive.transfers import multipart_transfer

    manager, ledger = await manager_for(tmp_path)
    source = staged(manager, b"abcdefghij")
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    original = multipart_transfer.fingerprint

    def slow_hash(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(2)
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(multipart_transfer, "fingerprint", slow_hash)
    record = await manager.enqueue_upload(source, "/", "ten.bin", 10)
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if user_cancel:
            (await manager.cancel_job(record.id))
            record.task.cancel()
        else:
            shutdown = asyncio.create_task(manager.registry.shutdown())
        await asyncio.sleep(0)
        assert source.exists()
        assert not finished.is_set()
        assert manager._semaphore._value == 1
        release.set()
        if shutdown is not None:
            await shutdown
        await asyncio.gather(record.task, return_exceptions=True)
        assert finished.is_set()
        assert manager._semaphore._value == 2
        assert ledger.messages == {}
        if user_cancel:
            assert record.status == TransferStatus.CANCELLED
            assert not source.exists()
        else:
            assert record.status == TransferStatus.QUEUED
            assert source.exists()
    finally:
        release.set()
        if shutdown is not None:
            await shutdown
        await dispose(manager)


async def test_queued_small_file_gets_media_turn_before_large_file_finishes(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    ledger.rpc_delay = 0.005
    large = staged(manager, b"x" * 40)
    big = await manager.enqueue_upload(large, "/", "large.bin", 40)
    try:
        await asyncio.wait_for(ledger.both_dispatched.wait(), 2)
        small = manager.cache_dir / "small.upload"
        small.write_bytes(b"z")
        tiny = await manager.enqueue_upload(small, "/", "small.bin", 1)
        await asyncio.gather(big.task, tiny.task)
        assert big.status == tiny.status == TransferStatus.COMPLETED
        small_turn = next(
            index for index, call in enumerate(ledger.calls) if call[3] == 1
        )
        assert small_turn < 10
        assert all(value == 1 for value in ledger.max_active.values())
    finally:
        await dispose(manager)


async def test_three_bots_upload_ready_files_without_global_two_upload_cap(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    await manager.pool.stop()
    manager.pool = ClientPool([Account(name, ledger) for name in ("a", "b", "c")])
    await manager.pool.start()
    ledger.hold = True
    records = []
    try:
        for index in range(4):
            source = manager.cache_dir / ("ready-" + str(index))
            source.write_bytes(b"ab")
            records.append(await manager.enqueue_upload(source, "/", source.name, 2))
        async with asyncio.timeout(3):
            await ledger.three_dispatched.wait()
        await asyncio.sleep(0.05)  # Let the fourth fingerprint finish and queue.
        assert len(ledger.calls) == 3
        assert sorted(ledger.max_active.values()) == [1, 1, 1]
        assert any(row.status is TransferStatus.QUEUED for row in records)
        ledger.release.set()
        async with asyncio.timeout(3):
            await asyncio.gather(*(record.task for record in records))
        assert all(row.status is TransferStatus.COMPLETED for row in records)
    finally:
        ledger.release.set()
        await dispose(manager)


@pytest.mark.parametrize("mixed_clients", [False, True])
async def test_one_large_file_uses_every_eligible_client_slot(tmp_path, mixed_clients):
    manager, ledger = await manager_for(tmp_path)
    await manager.pool.stop()
    names = ("a", "b", "c")
    manager.pool = ClientPool(
        [Account(name, ledger) for name in (names[:2] if mixed_clients else names)],
        [Account("c", ledger)] if mixed_clients else [],
    )
    await manager.pool.start()
    ledger.hold = True
    source = staged(
        manager, b"abcdefghij"
    )  # Three bot-compatible parts, > Premium limit.
    record = await manager.enqueue_upload(source, "/", "large.bin", 10)
    try:
        await asyncio.wait_for(ledger.three_dispatched.wait(), 2)
        assert len(ledger.calls) == 3
        assert set(ledger.max_active) == set(names)
        assert all(count == 1 for count in ledger.max_active.values())
        assert not manager.store.list_directory("/")
        if mixed_clients:
            (await manager.cancel_job(record.id))
        ledger.release.set()
        await asyncio.wait_for(record.task, 2)
        if mixed_clients:
            assert record.status == TransferStatus.CANCELLED
            assert ledger.messages == {}
        else:
            assert record.status == TransferStatus.COMPLETED
            assert (
                b"".join(
                    ledger.messages[p["message_id"]].content
                    for p in stored(manager)["parts"]
                )
                == b"abcdefghij"
            )
    finally:
        ledger.release.set()
        await dispose(manager)


async def test_multipart_workers_follow_live_per_bot_limit(tmp_path):
    manager, ledger = await manager_for(tmp_path)
    await manager.pool.stop()
    manager.pool = ClientPool([Account("a", ledger)])
    await manager.pool.start()
    ledger.hold = True
    record = await manager.enqueue_upload(
        staged(manager, b"x" * 12), "/", "large.bin", 12
    )
    try:
        await asyncio.wait_for(ledger.dispatched.wait(), 2)
        assert len(ledger.calls) == 1
        await manager.pool.set_uploads_per_bot(2)
        await asyncio.wait_for(ledger.both_dispatched.wait(), 3)
        assert ledger.max_active == {"a": 2}
        await manager.pool.set_uploads_per_bot(1)
        assert ledger.active == {
            "a": 2
        }  # Lowering drains; it never cancels current sends.
        ledger.release.set()
        await asyncio.wait_for(record.task, 2)
        assert record.status == TransferStatus.COMPLETED
        assert ledger.max_active == {"a": 2}
    finally:
        ledger.release.set()
        await dispose(manager)
