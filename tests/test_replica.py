"""Coordinator fault injection against isolated synthetic Telegram channels."""

from __future__ import annotations

import asyncio
import inspect
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from test_backup import settings

from tgdrive.replica import ChannelReplica
from tgdrive.replica_state import decode_bundle, files_in, parts_in
from tgdrive.replica_transport import ReplicaTransportError
from tgdrive.store import DriveStore

BACKUP = -1002234567890
REPLACEMENT = -1003234567890
TOKEN = "234567:" + "b" * 24


class Telegram:
    """Independent message names, with provider-side operation-ID deduplication."""

    def __init__(self):
        self.messages = {}
        self.pins = {}
        self.receipts = {}
        self.calls = []
        self.instances = []
        self.next_id = 1001
        self.lose_response = False
        self.copy_error = None
        self.publish_error_channel = None
        self.lose_publish_response_channel = None
        self.after_copy = None
        self.unavailable = set()

    def factory(self, configured, token):
        result = Transport(self, configured, token)
        self.instances.append(result)
        return result

    def new_message(self, channel, body):
        identity = self.next_id
        self.next_id += 1
        self.messages[channel, identity] = body
        return identity


class Transport:
    def __init__(self, remote, configured, token):
        self.remote, self.settings, self.token = remote, configured, token
        self.closed = False

    async def start(self):
        # Exercise the same Settings property used by real transport startup.
        assert self.token.split(":")[0] not in {
            token.split(":")[0] for token in self.settings.bot_tokens
        }

    async def close(self):
        self.closed = True

    async def inspect(self, channel, *, writable=True):
        if channel in self.remote.unavailable:
            raise ReplicaTransportError("Channel access was lost.", "REPLICA_ACCESS")
        return {"id": channel, "title": "Dedicated private channel", "private": True}

    async def copy(self, source, message, destination, *, random_id, expected_size):
        self.remote.calls.append(("copy", source, message, destination, random_id))
        if self.remote.copy_error:
            raise self.remote.copy_error
        await self.inspect(source)
        await self.inspect(destination)
        content = self.remote.messages.get((source, message))
        if content is None:
            raise ReplicaTransportError(
                "Source part is missing.", "REPLICA_MESSAGE_MISSING"
            )
        assert len(content) == expected_size
        identity = (self.token.split(":")[0], destination, random_id)
        if identity not in self.remote.receipts:
            self.remote.receipts[identity] = self.remote.new_message(
                destination, content
            )
        result = self.remote.receipts[identity]
        if self.remote.after_copy:
            hook, self.remote.after_copy = self.remote.after_copy, None
            value = hook()
            if inspect.isawaitable(value):
                await value
        if self.remote.lose_response:
            self.remote.lose_response = False
            raise ReplicaTransportError(
                "Response lost.", "REPLICA_UNAVAILABLE", retryable=True
            )
        return result

    async def publish(self, channel, path, *, random_id, caption, pin):
        self.remote.calls.append(("publish", channel, random_id))
        if channel == self.remote.publish_error_channel:
            raise ReplicaTransportError("Primary unavailable.", "REPLICA_ACCESS")
        await self.inspect(channel)
        identity = (self.token.split(":")[0], channel, random_id)
        if identity not in self.remote.receipts:
            self.remote.receipts[identity] = self.remote.new_message(
                channel, path.read_bytes()
            )
        result = self.remote.receipts[identity]
        if pin:
            self.remote.pins[channel] = result
        if self.remote.lose_publish_response_channel == channel:
            self.remote.lose_publish_response_channel = None
            raise ReplicaTransportError(
                "Publication response lost.", "REPLICA_UNAVAILABLE", retryable=True
            )
        return result

    async def pinned_message(self, channel):
        await self.inspect(channel, writable=False)
        return self.remote.pins.get(channel)

    async def verify(self, channel, message_id, expected_size):
        await self.inspect(channel)
        self.remote.calls.append(("verify", channel, message_id))
        content = self.remote.messages.get((channel, message_id))
        if content is None or len(content) != expected_size:
            raise ReplicaTransportError(
                "Remote verification failed.", "REPLICA_VERIFY_FAILED"
            )

    async def publish_catalog(self, channel, path, *, random_id, caption, pin=True):
        assert caption.startswith("TGDrive checkpoint v1\nDrive: ")
        return await self.publish(
            channel, path, random_id=random_id, caption=caption, pin=pin
        )

    async def download(self, channel, message_id, destination):
        identity = message_id or self.remote.pins[channel]
        content = self.remote.messages[channel, identity]
        if content.startswith(b"{"):
            raise ReplicaTransportError("Different filename.", "REPLICA_DOCUMENT_TYPE")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(0o600)
        return identity

    async def download_catalog(self, channel, destination, message_id=None):
        identity = message_id or self.remote.pins[channel]
        content = self.remote.messages[channel, identity]
        if not content.startswith(b"{"):
            raise ReplicaTransportError("Different filename.", "REPLICA_DOCUMENT_TYPE")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(0o600)
        return identity


def setup(tmp_path, *, multipart=False, empty=False):
    configured = settings(tmp_path)
    configured.resolved_data_dir.mkdir(parents=True, exist_ok=True)
    store = DriveStore.create(configured.resolved_data_dir / "catalog.sqlite3")
    store.bind_storage_channel(configured.storage_channel)
    remote = Telegram()
    if not empty:
        folder = store.create_folder("/", "Important")
        folder_path = "/" + folder["id"]
        store.set_starred(folder_path, True)
        if multipart:
            parts = [
                {
                    "index": 0,
                    "offset": 0,
                    "size": 3,
                    "message_id": 77,
                    "sha256": "a" * 64,
                    "document_id": 7001,
                },
                {
                    "index": 1,
                    "offset": 3,
                    "size": 4,
                    "message_id": 78,
                    "sha256": "b" * 64,
                    "document_id": 7002,
                },
            ]
            node = store.add_file(
                folder_path,
                "split.bin",
                77,
                7,
                parts=parts,
                sha256="c" * 64,
                operation_id="old-upload",
            )
            remote.messages[configured.storage_channel, 77] = b"abc"
            remote.messages[configured.storage_channel, 78] = b"defg"
        else:
            node = store.add_file(folder_path, "record.bin", 77, 3)
            remote.messages[configured.storage_channel, 77] = b"abc"
        path = folder_path + "/" + node["id"]
        store.set_starred(path, True)
        store.create_share(path, label="Research team")
    runtime = SimpleNamespace(store=store)
    replica = ChannelReplica(configured, runtime, transport_factory=remote.factory)
    config = {"channel": BACKUP, "token": TOKEN, "enabled": True}
    replica.journal.set("config", config)
    return configured, store, remote, replica, config


async def dispose(replica, store):
    await replica.close()
    store.close()


def remote_bundle(remote, replica, channel=BACKUP):
    return decode_bundle(remote.messages[channel, remote.pins[channel]], replica.key())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "empty,multipart", [(True, False), (False, False), (False, True)]
)
async def test_existing_or_empty_catalog_gets_independent_complete_recovery_bundle(
    tmp_path, empty, multipart
):
    configured, store, remote, replica, config = setup(
        tmp_path, empty=empty, multipart=multipart
    )
    before = store.export()
    try:
        await replica._sync(config)
        assert replica.status()["protected_revision"] == store.revision
        bundle = remote_bundle(remote, replica)
        assert bundle["catalog"] == before
        restored = bundle["replica_catalog"]
        assert restored["storage_channel_id"] == BACKUP
        assert restored["drive_id"] == store.drive_id
        assert restored["shares"] == before["shares"]
        assert restored["operations"] == restored["deletions"] == []
        old_files, new_files = list(files_in(before)), list(files_in(restored))
        assert len(old_files) == len(new_files)
        for old, new in zip(old_files, new_files, strict=True):
            assert old["id"] == new["id"] and old["starred"] == new["starred"]
            for original, copied in zip(parts_in(old), parts_in(new), strict=True):
                assert original["message_id"] != copied["message_id"]
                assert (
                    remote.messages[configured.storage_channel, original["message_id"]]
                    == remote.messages[BACKUP, copied["message_id"]]
                )
        assert store.export() == before, (
            "Replication cannot mutate live source metadata"
        )
        assert all(instance.closed for instance in remote.instances)
        assert configured.storage_channel not in remote.pins, (
            "Primary catalog pin must be preserved"
        )
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_response_loss_preserves_intent_and_reuses_operation_after_restart(
    tmp_path,
):
    configured, store, remote, replica, config = setup(tmp_path)
    try:
        remote.lose_response = True
        with pytest.raises(ReplicaTransportError):
            await replica._sync(config)
        original = next(call for call in remote.calls if call[0] == "copy")
        assert replica.journal.get("job")
        assert not remote.pins
        await replica.close()
        replica = ChannelReplica(
            configured, SimpleNamespace(store=store), transport_factory=remote.factory
        )
        await replica._sync(config)
        attempts = [call for call in remote.calls if call[0] == "copy"]
        assert attempts == [original, original]
        assert (
            len([key for key in remote.messages if key[0] == BACKUP]) == 2
        )  # payload + capsule
        assert replica.status()["protected_revision"] == store.revision
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_pause_mid_copy_resumes_confirmed_parts_without_resending(tmp_path):
    configured, store, remote, replica, config = setup(tmp_path, multipart=True)
    try:
        remote.after_copy = lambda: replica.update(phase="paused")
        await replica._sync(config)
        assert replica.status()["phase"] == "paused"
        assert replica.status()["done_parts"] == 1 and not remote.pins
        await replica.close()
        replica = ChannelReplica(
            configured, SimpleNamespace(store=store), transport_factory=remote.factory
        )
        assert replica.status()["phase"] == "paused"
        replica.update(phase="idle")  # Explicitly resume the saved paused job.
        await replica._sync(config)
        assert [call[2] for call in remote.calls if call[0] == "copy"] == [77, 78]
        assert replica.status()["done_parts"] == 2
        assert remote_bundle(remote, replica)["complete"]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_missing_source_part_does_not_advertise_restore_point(tmp_path):
    _, store, remote, replica, config = setup(tmp_path, multipart=True)
    try:
        remote.messages.pop((replica.settings.storage_channel, 78))
        with pytest.raises(ReplicaTransportError):
            await replica._sync(config)
        assert replica.status()["protected_revision"] == 0
        assert replica.journal.get("restore_point") is None
        assert not remote.pins
        assert replica.journal.get("job")
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_confirmed_part", [False, True])
async def test_retry_batches_confirmed_parts_before_copying_remaining_files(
    tmp_path, monkeypatch, missing_confirmed_part
):
    _, store, remote, replica, config = setup(tmp_path, empty=True)
    for index in range(35):
        identity = 77 + index
        store.add_file("/", f"file-{index}.bin", identity, 1)
        remote.messages[replica.settings.storage_channel, identity] = b"x"

    async def stop_after_29():
        if len(remote.receipts) == 29:
            remote.copy_error = ReplicaTransportError(
                "Wait for Telegram.",
                "REPLICA_FLOOD_WAIT",
                retry_after=20,
                retryable=True,
            )
        else:
            remote.after_copy = stop_after_29

    remote.after_copy = stop_after_29
    verify_one = Transport.verify

    async def verify_many(self, channel, documents):
        self.remote.calls.append(("verify_batch", channel, list(documents)))
        for message, size in documents:
            await verify_one(self, channel, message, size)

    async def unexpected_single_verification(self, *_args):
        raise AssertionError("The retry must use bounded batch verification")

    monkeypatch.setattr(Transport, "verify_many", verify_many, raising=False)
    monkeypatch.setattr(Transport, "verify", unexpected_single_verification)
    try:
        with pytest.raises(ReplicaTransportError):
            await replica._sync(config)
        assert replica.status()["done_parts"] == 29
        assert not remote.pins
        if missing_confirmed_part:
            message = next(iter(remote.receipts.values()))
            remote.messages.pop((BACKUP, message))
        remote.copy_error = None
        remote.calls.clear()
        if missing_confirmed_part:
            with pytest.raises(ReplicaTransportError):
                await replica._sync(config)
            assert not remote.pins
            assert not [call for call in remote.calls if call[0] == "copy"]
        else:
            await replica._sync(config)
            copies = [call for call in remote.calls if call[0] == "copy"]
            assert len(copies) == 6
            assert replica.status()["phase"] == "protected"
            assert replica.status()["done_files"] == 35
        batches = [call for call in remote.calls if call[0] == "verify_batch"]
        assert len(batches) == 1
        assert len(batches[0][2]) == 29
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_reverification_splits_large_catalog_into_bounded_batches(
    tmp_path, monkeypatch
):
    from tgdrive.replica_transport import MAX_VERIFY_BATCH

    _, store, remote, replica, config = setup(tmp_path, empty=True)
    for index in range(MAX_VERIFY_BATCH + 3):
        identity = 77 + index
        store.add_file("/", f"file-{index}.bin", identity, 1)
        remote.messages[replica.settings.storage_channel, identity] = b"x"
    batches = []

    async def verify_many(self, channel, documents):
        batches.append(list(documents))
        for message, size in documents:
            await self.verify(channel, message, size)

    monkeypatch.setattr(Transport, "verify_many", verify_many, raising=False)
    try:
        await replica._sync(config)
        store.create_folder("/", "Next snapshot")
        remote.calls.clear()
        await replica._sync(config)
        assert [len(batch) for batch in batches] == [MAX_VERIFY_BATCH, 3]
        assert not [call for call in remote.calls if call[0] == "copy"]
        assert replica.status()["phase"] == "protected"
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_catalog_change_during_copy_reports_pending_revision_and_catches_up(
    tmp_path,
):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        revision = store.revision
        remote.after_copy = lambda: store.create_folder("/", "Created during backup")
        await replica._sync(config)
        assert replica.status()["protected_revision"] == revision
        assert replica.status()["pending_revision"] == 1
        first = remote_bundle(remote, replica)
        assert first["catalog"]["revision"] == revision
        await replica._sync(config)
        assert replica.status()["protected_revision"] == store.revision
        assert replica.status()["pending_revision"] == 0
        assert len([call for call in remote.calls if call[0] == "copy"]) == 1
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_primary_lost_at_second_publish_retains_recoverable_backup_and_old_status(
    tmp_path,
):
    configured, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        old = replica.journal.get("restore_point")
        store.create_folder("/", "After previous restore point")
        remote.publish_error_channel = configured.storage_channel
        with pytest.raises(ReplicaTransportError):
            await replica._sync(config)
        assert replica.journal.get("restore_point") == old
        assert replica.status()["protected_revision"] == old["revision"]
        assert remote_bundle(remote, replica)["catalog"]["revision"] == store.revision
        assert remote_bundle(remote, replica)["complete"]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_replacement_target_uses_new_intent_and_retains_old_backup(tmp_path):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        old_pin = remote.pins[BACKUP]
        config = dict(config, channel=REPLACEMENT)
        replica.journal.set("config", config)
        replica.journal.set("job", None)
        replica.update(protected_revision=0)
        await replica._sync(config)
        assert remote.pins[BACKUP] == old_pin
        assert REPLACEMENT in remote.pins
        attempts = [call for call in remote.calls if call[0] == "copy"]
        assert [call[3] for call in attempts] == [BACKUP, REPLACEMENT]
        assert attempts[0][4] != attempts[1][4]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_flood_wait_error_and_retry_deadline_survive_service_restart(tmp_path):
    configured, store, remote, replica, _config = setup(tmp_path)
    try:
        remote.copy_error = ReplicaTransportError(
            "Telegram requested a wait.",
            "REPLICA_FLOOD_WAIT",
            retry_after=120,
            retryable=True,
        )
        remote.copy_error.operation = "messages.sendMedia"
        replica.start()
        for _ in range(100):
            if replica.status().get("error"):
                break
            await asyncio.sleep(0.01)
        result = replica.status()
        assert result["phase"] == "waiting"
        assert result["error"]["retry_at"] > 0
        assert "messages.sendMedia" in result["error"]["detail"]
        assert "Retry in 120 seconds" in result["report"][-1]["message"]
        assert "channel permissions do not need changing" in result["error"]["action"]
        deadline = result["error"]["retry_at"]
        await replica.close()
        replica = ChannelReplica(
            configured, SimpleNamespace(store=store), transport_factory=remote.factory
        )
        assert replica.status()["error"]["retry_at"] == deadline
        count = len(remote.calls)
        replica.start()
        await asyncio.sleep(0.05)
        assert len(remote.calls) == count, "Restart must honor Telegram's wait"
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_expired_health_check_detects_lost_channel_even_without_catalog_changes(
    tmp_path,
):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        replica.update(checked_at=0)
        remote.unavailable.add(BACKUP)
        with pytest.raises(ReplicaTransportError):
            await replica._sync(config)
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_unchanged_backup_stays_idle_between_hourly_health_checks(tmp_path):
    from tgdrive.replica import HEALTH_CHECK_INTERVAL_SECONDS

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        remote.calls.clear()
        connections = len(remote.instances)
        replica.update(checked_at=time.time() - HEALTH_CHECK_INTERVAL_SECONDS + 60)
        await replica._sync(config)
        await replica._sync(config)
        assert len(remote.instances) == connections
        assert remote.calls == []
        assert replica.status()["phase"] == "protected"
        assert replica.journal.get("job") is None
        assert replica.journal.get("restore_point") == point
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_hourly_health_check_does_not_start_a_copy_job_or_republish(
    tmp_path, monkeypatch,
):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        success = replica.status()["last_success_at"]
        remote.calls.clear()
        phases = []
        start = Transport.start

        async def record_start(self):
            phases.append(replica.status()["phase"])
            assert replica.journal.get("job") is None
            await start(self)

        monkeypatch.setattr(Transport, "start", record_start)
        replica.update(checked_at=0)
        await replica._sync(config)
        assert phases == ["protected"]
        assert [call[0] for call in remote.calls] == ["verify"]
        assert replica.journal.get("restore_point") == point
        assert replica.status()["last_success_at"] == success
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_new_catalog_revision_is_not_delayed_until_the_next_health_check(tmp_path):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        previous = replica.status()["protected_revision"]
        connections = len(remote.instances)
        store.create_folder("/", "New metadata")
        await replica._sync(config)
        assert len(remote.instances) == connections + 1
        assert replica.status()["protected_revision"] > previous
        assert replica.status()["protected_revision"] == store.revision
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_manual_sync_forces_health_check_but_resume_reuses_recent_confirmation(
    tmp_path, monkeypatch,
):
    _, store, remote, replica, config = setup(tmp_path)
    monkeypatch.setattr(replica, "start", lambda: None)
    try:
        await replica._sync(config)
        connections = len(remote.instances)
        await replica.action("pause")
        await replica.action("resume")
        await replica._sync(config)
        assert len(remote.instances) == connections
        assert replica.status()["phase"] == "protected"
        await replica.action("sync")
        await replica._sync(config)
        assert len(remote.instances) == connections + 1
        assert replica.status()["phase"] == "protected"
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_idle_health_wait_reports_context_and_retries_after_saved_deadline(
    tmp_path, monkeypatch,
):
    _, store, remote, replica, config = setup(tmp_path)
    original_start = Transport.start

    async def limited_start(self):
        error = ReplicaTransportError(
            "Telegram requested a pause. Backup will resume after the wait.",
            "REPLICA_FLOOD_WAIT", retry_after=120, retryable=True,
        )
        error.operation = "sign_in"
        raise error

    try:
        await replica._sync(config)
        replica.update(checked_at=0)
        monkeypatch.setattr(Transport, "start", limited_start)
        replica.start()
        for _ in range(100):
            if replica.status().get("error"):
                break
            await asyncio.sleep(0.01)
        failure = replica.status()["error"]
        assert "Routine backup health check delayed" in failure["detail"]
        assert "sign_in" in failure["detail"]
        assert "Retry in 120 seconds" in failure["detail"]
        assert replica.journal.get("job") is None
        assert replica.journal.get("restore_point") is not None
        replica._task.cancel()
        await asyncio.gather(replica._task, return_exceptions=True)
        monkeypatch.setattr(Transport, "start", original_start)
        connections = len(remote.instances)
        await replica.action("sync")
        replica._task.cancel()
        await asyncio.gather(replica._task, return_exceptions=True)
        assert replica.status()["error"]["retry_at"] == failure["retry_at"]
        assert len(remote.instances) == connections
        replica.update(error=failure | {"retry_at": 0}, checked_at=time.time())
        await replica._sync(config)
        assert len(remote.instances) == connections + 1
        assert replica.status()["phase"] == "protected"
        assert replica.status()["error"] is None
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["retry", "resume"])
async def test_expired_health_wait_is_checked_on_manual_retry(tmp_path, monkeypatch, action):
    _, store, remote, replica, config = setup(tmp_path)
    monkeypatch.setattr(replica, "start", lambda: None)
    try:
        await replica._sync(config)
        connections = len(remote.instances)
        replica.update(
            phase="waiting", checked_at=time.time(),
            error={"code": "REPLICA_FLOOD_WAIT", "retry_at": time.time() - 1},
        )
        await replica.action(action)
        await replica._sync(config)
        assert len(remote.instances) == connections + 1
        assert replica.status()["phase"] == "protected"
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["access", "pin", "document"])
async def test_deletion_barrier_rechecks_recent_checkpoint_before_mutation(tmp_path, failure):
    from tgdrive.managed_settings import SettingsProblem

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        if failure == "access":
            remote.unavailable.add(BACKUP)
        elif failure == "pin":
            remote.pins[BACKUP] += 1
        else:
            remote.messages.pop((BACKUP, point["backup_message_id"]))
        connections = len(remote.instances)
        with pytest.raises((SettingsProblem, ReplicaTransportError)):
            await replica.before_mutation()
        assert len(remote.instances) == connections + 1
        assert replica.journal.get("restore_point") == point
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_pause_during_health_check_stays_paused(tmp_path, monkeypatch):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        replica.update(checked_at=0)
        original_pin = Transport.pinned_message

        async def pause_after_pin(self, channel):
            result = await original_pin(self, channel)
            replica.update(phase="paused")
            return result

        monkeypatch.setattr(Transport, "pinned_message", pause_after_pin)
        await replica._sync(config)
        assert replica.status()["phase"] == "paused"
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["worker", "mutation"])
@pytest.mark.parametrize("action", ["pause", "disable"])
@pytest.mark.parametrize("code", ["REPLICA_FLOOD_WAIT", "REPLICA_UNAVAILABLE"])
async def test_stop_during_failed_request_preserves_control_and_saved_wait(
    tmp_path, monkeypatch, caller, action, code,
):
    _, store, remote, replica, config = setup(tmp_path)
    entered, release, recorded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    mutation = None
    report = replica.report

    async def failed_start(self):
        entered.set()
        await release.wait()
        error = ReplicaTransportError(
            "Provider requested a wait.", code,
            retry_after=120 if code == "REPLICA_FLOOD_WAIT" else None,
            retryable=True,
        )
        error.operation = "sign_in"
        raise error

    def record_error(level, message):
        report(level, message)
        if level == "error":
            recorded.set()

    try:
        await replica._sync(config)
        replica.update(checked_at=0)
        monkeypatch.setattr(Transport, "start", failed_start)
        monkeypatch.setattr(replica, "report", record_error)
        if caller == "worker":
            replica.start()
        else:
            mutation = asyncio.create_task(replica.before_mutation())
        await asyncio.wait_for(entered.wait(), 1)
        await replica.action(action)
        release.set()
        if mutation is not None:
            with pytest.raises(ReplicaTransportError):
                await mutation
        else:
            await asyncio.wait_for(recorded.wait(), 1)
        expected_phase = "paused" if action == "pause" else "disabled"
        state = replica.status()
        assert state["phase"] == expected_phase
        assert state["error"]["retry_at"] > time.time()
        assert state["enabled"] is (action == "pause")
        deadline = state["error"]["retry_at"]
        connections = len(remote.instances)
        await replica.action("resume")
        await asyncio.sleep(0)
        assert replica.status()["phase"] == "waiting"
        assert replica.status()["error"]["retry_at"] == deadline
        assert len(remote.instances) == connections
    finally:
        release.set()
        if mutation is not None and not mutation.done():
            mutation.cancel()
            await asyncio.gather(mutation, return_exceptions=True)
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "disable"])
async def test_deletion_barrier_rechecks_control_after_waiting_for_operation_lock(
    tmp_path, action,
):
    from tgdrive.managed_settings import SettingsProblem

    _, store, remote, replica, config = setup(tmp_path)
    mutation = None
    try:
        await replica._sync(config)
        connections = len(remote.instances)
        async with replica._operation:
            mutation = asyncio.create_task(replica.before_mutation())
            await asyncio.sleep(0)
            assert not mutation.done()
            await replica.action(action)
        if action == "pause":
            with pytest.raises(SettingsProblem, match="Resume independent backup"):
                await mutation
        else:
            await mutation  # Explicitly disabling protection permits the mutation.
        assert len(remote.instances) == connections
        assert replica.status()["phase"] == (
            "paused" if action == "pause" else "disabled"
        )
    finally:
        if mutation is not None and not mutation.done():
            mutation.cancel()
            await asyncio.gather(mutation, return_exceptions=True)
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_deletion_barrier_rechecks_pause_after_successful_remote_check(
    tmp_path, monkeypatch,
):
    from tgdrive.managed_settings import SettingsProblem

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        original_pin = Transport.pinned_message

        async def pause_after_pin(self, channel):
            result = await original_pin(self, channel)
            await replica.action("pause")
            return result

        monkeypatch.setattr(Transport, "pinned_message", pause_after_pin)
        with pytest.raises(SettingsProblem, match="Resume independent backup"):
            await replica.before_mutation()
        assert replica.status()["phase"] == "paused"
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["credentials", "catalog"])
async def test_pause_during_local_snapshot_does_not_restart_telegram_work(
    tmp_path, monkeypatch, stage,
):
    _, store, remote, replica, config = setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original_thread = asyncio.to_thread
    selected = replica._credentials if stage == "credentials" else store.export

    async def blocked_snapshot(function, *args, **kwargs):
        if function == selected:
            entered.set()
            await release.wait()
        return await original_thread(function, *args, **kwargs)

    monkeypatch.setattr("tgdrive.replica.asyncio.to_thread", blocked_snapshot)
    operation = asyncio.create_task(replica._sync(config))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await replica.action("pause")
        release.set()
        await operation
        assert replica.status()["phase"] == "paused"
        assert replica.journal.get("job") is None
        assert not remote.instances
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_worker_rechecks_pause_after_waiting_for_operation_lock(tmp_path):
    _, store, remote, replica, _config = setup(tmp_path)
    try:
        async with replica._operation:
            replica.start()
            await asyncio.sleep(0)
            await replica.action("pause")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert replica.status()["phase"] == "paused"
        assert not remote.instances
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "disable"])
@pytest.mark.parametrize("stage", ["start", "inspect", "close"])
async def test_recovery_probe_does_not_undo_pause_or_disable(
    tmp_path, monkeypatch, action, stage,
):
    _, store, remote, replica, config = setup(tmp_path)
    target_token = "345678:" + "z" * 30
    entered, release = asyncio.Event(), asyncio.Event()
    original = getattr(Transport, stage)
    operation = None

    async def blocked_probe(self, *args, **kwargs):
        if self.token == target_token:
            entered.set()
            await release.wait()
        return await original(self, *args, **kwargs)

    try:
        await replica._sync(config)
        await replica.inspect_recovery(BACKUP, TOKEN, replica.key())
        recovery = {"target": REPLACEMENT, "target_token": target_token}
        replica.journal.set("recovery", recovery)
        replica.update(phase="recovering")
        remote.calls.clear()
        monkeypatch.setattr(Transport, stage, blocked_probe)
        operation = asyncio.create_task(replica._recover_copy(recovery))
        await asyncio.wait_for(entered.wait(), 1)
        await replica.action(action)
        release.set()
        await operation
        assert replica.status()["phase"] == (
            "paused" if action == "pause" else "disabled"
        )
        assert not [
            call for call in remote.calls if call[0] in {"copy", "publish"}
        ]
        if action == "disable":
            assert replica.journal.get("recovery") is None
            assert replica.journal.get("candidate") is None
        else:
            assert not replica.journal.get("candidate")["complete"]
    finally:
        release.set()
        if operation is not None and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_deletion_poll_does_not_extend_a_short_saved_telegram_wait(
    tmp_path, monkeypatch,
):
    from tgdrive.backup import BackupError

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        timestamp = [1000.0]
        monkeypatch.setattr("tgdrive.replica.time.time", lambda: timestamp[0])
        error = {
            "code": "REPLICA_FLOOD_WAIT",
            "detail": "Saved provider cooldown",
            "retry_at": 1003.0,
        }
        replica.update(phase="waiting", error=error)
        connections = len(remote.instances)
        for current in (1000.0, 1001.0, 1002.9):
            timestamp[0] = current
            with pytest.raises(BackupError):
                await replica.before_mutation()
            assert replica.status()["error"] == error
            assert len(remote.instances) == connections
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_deletion_barrier_rejects_saved_wait_at_expiry_boundary(
    tmp_path, monkeypatch,
):
    from tgdrive.backup import BackupError

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        error = {"code": "REPLICA_FLOOD_WAIT", "retry_at": 1003.0}
        replica.update(phase="waiting", error=error)
        connections = len(remote.instances)
        timestamps = iter([1002.999999])
        monkeypatch.setattr(
            "tgdrive.replica.time.time", lambda: next(timestamps, 1003.0)
        )
        with pytest.raises(BackupError):
            await replica.before_mutation()
        assert replica.status()["error"] == error
        assert len(remote.instances) == connections
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_inspected_backup_rebuilds_replacement_main_when_original_main_is_lost(
    tmp_path,
):
    configured, store, remote, replica, config = setup(tmp_path, multipart=True)
    try:
        before = store.export()
        await replica._sync(config)
        key = replica.key()
        remote.unavailable.add(configured.storage_channel)
        await replica.inspect_recovery(BACKUP, TOKEN, key)
        candidate = replica.journal.get("candidate")
        assert candidate["catalog"]["storage_channel_id"] == BACKUP
        assert not candidate["complete"]
        recovery = {
            "target": REPLACEMENT,
            "target_token": "345678:" + "z" * 30,
        }
        replica.journal.set("recovery", recovery)
        replica.update(phase="recovering")
        await replica._recover_copy(recovery)
        restored = replica.journal.get("candidate")
        assert restored["complete"]
        assert restored["catalog"]["storage_channel_id"] == REPLACEMENT
        assert replica.status()["phase"] == "ready_to_activate"
        assert restored["credentials"]["bot_tokens"] == ["345678:" + "z" * 30]
        for node in files_in(restored["catalog"]):
            for part in parts_in(node):
                assert (REPLACEMENT, part["message_id"]) in remote.messages
        assert remote.messages[REPLACEMENT, remote.pins[REPLACEMENT]].startswith(b"{")
        assert store.export() == before, "Recovery must not activate partial metadata"
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_database_only_restore_checks_existing_parts_without_payload_copies(
    tmp_path,
):
    configured, store, remote, replica, config = setup(tmp_path, multipart=True)
    try:
        await replica._sync(config)
        await replica.inspect_recovery(
            configured.storage_channel,
            TOKEN,
            replica.key(),
            message_id=replica.journal.get("restore_point")["source_message_id"],
        )
        before = len([call for call in remote.calls if call[0] == "copy"])
        recovery = {
            "target": configured.storage_channel,
            "target_token": "345678:" + "z" * 30,
            "catalog_only": True,
        }
        replica.journal.set("recovery", recovery)
        replica.update(phase="recovering")
        await replica._recover_copy(recovery)
        assert replica.journal.get("candidate")["complete"]
        assert len([call for call in remote.calls if call[0] == "copy"]) == before
        verified = [call[2] for call in remote.calls if call[0] == "verify"]
        assert verified == [77, 78]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_wrong_key_or_foreign_channel_does_not_replace_live_metadata(tmp_path):
    configured, store, remote, replica, config = setup(tmp_path)
    try:
        before = store.export()
        await replica._sync(config)
        with pytest.raises(ValueError, match="nothing changed"):
            await replica.inspect_recovery(BACKUP, TOKEN, "c2" * 32)
        assert replica.journal.get("candidate") is None
        assert store.export() == before
        encrypted = remote.messages[BACKUP, remote.pins[BACKUP]]
        remote.messages[REPLACEMENT, 9000] = encrypted
        remote.pins[REPLACEMENT] = 9000
        with pytest.raises(ValueError, match="selected channel"):
            await replica.inspect_recovery(REPLACEMENT, TOKEN, replica.key())
        assert replica.journal.get("candidate") is None
        assert store.export() == before
        assert configured.storage_channel == store.storage_channel_id
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_manual_retry_and_mutations_do_not_bypass_persisted_provider_wait(
    tmp_path,
):
    from tgdrive.backup import BackupError
    from tgdrive.managed_settings import SettingsProblem

    _, store, remote, replica, _config = setup(tmp_path)
    try:
        deadline = time.time() + 120
        replica.update(
            phase="waiting",
            error={
                "code": "REPLICA_FLOOD_WAIT",
                "detail": "Wait for Telegram",
                "retry_at": deadline,
            },
        )
        await replica.action("retry")
        assert replica.status()["error"]["retry_at"] == deadline
        with pytest.raises((BackupError, SettingsProblem, ReplicaTransportError)):
            await replica.before_mutation()
        assert not [call for call in remote.calls if call[0] == "copy"]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_pause_during_final_part_does_not_publish_or_claim_protection(tmp_path):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        remote.after_copy = lambda: replica.update(phase="paused")
        await replica._sync(config)
        assert replica.status()["phase"] == "paused"
        assert not remote.pins
        assert replica.status()["protected_revision"] == 0
        replica.update(phase="idle")
        await replica._sync(config)
        assert replica.status()["protected_revision"] == store.revision
        assert len([call for call in remote.calls if call[0] == "copy"]) == 1
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_configure_refuses_destination_with_foreign_pinned_checkpoint(tmp_path):
    from tgdrive.managed_settings import SettingsProblem

    _, store, remote, replica, _config = setup(tmp_path)
    try:
        remote.pins[BACKUP] = 700
        before = replica.journal.get("config")
        with pytest.raises(SettingsProblem):
            await replica.configure(BACKUP, TOKEN, True)
        assert replica.journal.get("config") == before
        assert remote.pins[BACKUP] == 700
        assert not [call for call in remote.calls if call[0] in {"copy", "publish"}]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["paused", "disabled"])
async def test_pause_during_database_archive_stops_metadata_publication(
    tmp_path, monkeypatch, phase
):
    _, store, remote, replica, config = setup(tmp_path)

    async def stopped_archive(*_args, **_kwargs):
        replica.update(phase=phase)
        return None

    monkeypatch.setattr("tgdrive.replica_mongo.mirror_mongodb", stopped_archive)
    try:
        await replica._sync(config)
        assert replica.status()["phase"] == phase
        assert replica.status()["protected_revision"] == 0
        assert not remote.pins
        assert not [call for call in remote.calls if call[0] == "publish"]
        assert replica.journal.get("job"), "Interrupted catalog must remain resumable"
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_stage", [False, True])
async def test_capsule_publication_response_loss_recovers_after_process_restart(
    tmp_path, lost_stage
):
    configured, store, remote, replica, config = setup(tmp_path)
    try:
        remote.lose_publish_response_channel = BACKUP
        with pytest.raises(ReplicaTransportError):
            await replica._sync(config)
        original_pin = remote.pins[BACKUP]
        assert replica.status()["protected_revision"] == 0
        assert replica.journal.get("restore_point") is None
        first_random_id = next(call[2] for call in remote.calls if call[0] == "publish")
        if lost_stage:
            # Disk loss after Telegram accepted a capsule must get a fresh
            # ciphertext intent, never reuse an ID for different AES-GCM bytes.
            for path in (configured.resolved_data_dir / "replica-bundles").glob(
                "*.tgdb"
            ):
                path.unlink()
        await replica.close()
        replica = ChannelReplica(
            configured, SimpleNamespace(store=store), transport_factory=remote.factory
        )
        await replica._sync(config)
        second_random_id = [
            call[2] for call in remote.calls if call[:2] == ("publish", BACKUP)
        ][-1]
        assert (second_random_id == first_random_id) is not lost_stage
        assert (remote.pins[BACKUP] == original_pin) is not lost_stage
        assert replica.status()["protected_revision"] == store.revision
        assert (
            replica.journal.get("restore_point")["backup_message_id"]
            == remote.pins[BACKUP]
        )
        assert remote_bundle(remote, replica)["catalog"] == store.export()
        assert len([call for call in remote.calls if call[0] == "copy"]) == 1
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, 9000])
async def test_health_check_detects_changed_pin_without_overwriting_it(
    tmp_path, replacement
):
    from tgdrive.managed_settings import SettingsProblem

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        count = len([call for call in remote.calls if call[0] == "publish"])
        replica.update(checked_at=0)
        if replacement is None:
            remote.pins.pop(BACKUP)
        else:
            remote.pins[BACKUP] = replacement
        with pytest.raises(SettingsProblem) as error:
            await replica._sync(config)
        assert error.value.code == "REPLICA_PIN_CHANGED"
        assert replica.journal.get("restore_point") == point
        assert remote.pins.get(BACKUP) == replacement
        assert len([call for call in remote.calls if call[0] == "publish"]) == count
        assert remote.messages[BACKUP, point["backup_message_id"]]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_key_kit_pairs_confirmed_point_with_its_original_keys_after_rotation(
    tmp_path,
):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        replica.settings.mongodb_backup_key = SecretStr("a1" * 32)
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        original_key = replica.key()
        replica.journal.set("key", "c3" * 32)
        replica.settings.mongodb_backup_key = SecretStr("b2" * 32)
        kit = replica.recovery_kit()
        assert kit["restore_point"] == point
        assert kit["key"] == original_key
        assert kit["next_backup_key"] == "c3" * 32
        assert kit["mongodb_backup_key"] == "a1" * 32
        assert kit["next_mongodb_backup_key"] == "b2" * 32
        bundle = decode_bundle(
            remote.messages[BACKUP, point["backup_message_id"]], kit["key"]
        )
        assert bundle["credentials"]["mongodb_backup_key"] == kit["mongodb_backup_key"]
        # A failed next generation must not pair its new keys with the old point.
        store.create_folder("/", "Next generation")
        remote.copy_error = ReplicaTransportError("Offline.", "REPLICA_ACCESS")
        # Force a new missing receipt through an added file, preserving original.
        folder = next(iter(store.export()["root"]["children"].values()))
        store.add_file("/" + folder["id"], "new.bin", 79, 1)
        with pytest.raises(ReplicaTransportError):
            await replica._sync(config)
        assert replica.recovery_kit() == kit
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_inspected_recovery_key_kit_uses_candidate_keys_and_checkpoint(tmp_path):
    from tgdrive.replica_state import encode_bundle

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        inspected = remote_bundle(remote, replica)
        inspected_key = "d4" * 32
        inspected["credentials"]["mongodb_backup_key"] = "e5" * 32
        message = remote.new_message(BACKUP, encode_bundle(inspected, inspected_key))
        await replica.inspect_recovery(BACKUP, TOKEN, inspected_key, message_id=message)
        kit = replica.recovery_kit()
        assert kit["key"] == inspected_key
        assert kit["mongodb_backup_key"] == "e5" * 32
        assert kit["restore_point"]["source_channel"] == BACKUP
        assert kit["restore_point"]["source_message_id"] == message
        assert decode_bundle(remote.messages[BACKUP, message], kit["key"]) == inspected
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_at", ["capsule", "primary-catalog"])
async def test_recovery_resumes_its_accepted_pin_after_lost_publication_response(
    tmp_path, monkeypatch, lost_at
):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        await replica.inspect_recovery(BACKUP, TOKEN, replica.key())
        recovery = {"target": REPLACEMENT, "target_token": "345678:" + "z" * 30}
        replica.journal.set("recovery", recovery)
        replica.update(phase="recovering")
        if lost_at == "capsule":
            remote.lose_publish_response_channel = REPLACEMENT
        else:
            publish_catalog = Transport.publish_catalog
            lose_response = True

            async def lose_catalog_response(self, *_args, **_kwargs):
                nonlocal lose_response
                result = await publish_catalog(self, *_args, **_kwargs)
                if lose_response:
                    lose_response = False
                    raise ReplicaTransportError(
                        "Catalog response lost.", "REPLICA_UNAVAILABLE", retryable=True
                    )
                return result

            monkeypatch.setattr(Transport, "publish_catalog", lose_catalog_response)
        with pytest.raises(ReplicaTransportError):
            await replica._recover_copy(recovery)
        assert remote.pins[REPLACEMENT]
        assert not replica.journal.get("candidate")["complete"]
        count = len([call for call in remote.calls if call[0] == "copy"])
        await replica._recover_copy(recovery)
        assert replica.journal.get("candidate")["complete"]
        assert replica.status()["phase"] == "ready_to_activate"
        assert remote.messages[REPLACEMENT, remote.pins[REPLACEMENT]].startswith(b"{")
        assert len([call for call in remote.calls if call[0] == "copy"]) == count
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["capsule", "catalog"])
async def test_recovery_refuses_unrelated_pin_introduced_during_pause(tmp_path, kind):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        unrelated = (
            remote.messages[BACKUP, remote.pins[BACKUP]]
            if kind == "capsule"
            else DriveStore._json_bytes(store.export())
        )
        await replica.inspect_recovery(BACKUP, TOKEN, replica.key())
        recovery = {"target": REPLACEMENT, "target_token": "345678:" + "z" * 30}
        replica.journal.set("recovery", recovery)
        replica.update(phase="recovering")
        remote.after_copy = lambda: replica.update(phase="paused")
        await replica._recover_copy(recovery)
        assert not replica.journal.get("candidate")["complete"]
        foreign_pin = remote.new_message(REPLACEMENT, unrelated)
        remote.pins[REPLACEMENT] = foreign_pin
        publications = len([call for call in remote.calls if call[0] == "publish"])
        replica.update(phase="recovering")
        with pytest.raises(ValueError, match="pin changed"):
            await replica._recover_copy(recovery)
        assert not replica.journal.get("candidate")["complete"]
        assert remote.pins[REPLACEMENT] == foreign_pin
        assert (
            len([call for call in remote.calls if call[0] == "publish"]) == publications
        )
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_pending_mongo_job_rejects_changed_key_before_any_remote_side_effect(
    tmp_path, monkeypatch
):
    from tgdrive.managed_settings import SettingsProblem

    configured, store, remote, replica, config = setup(tmp_path)
    original_mongo_key, replacement_mongo_key = "a1" * 32, "b2" * 32
    configured.mongodb_backup_key = SecretStr(original_mongo_key)
    try:
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        original_capsule = remote.messages[BACKUP, point["backup_message_id"]]
        captured = replica._credentials()
        store.create_folder("/", "A later catalog generation")
        pending = {"catalog": store.export(), "credentials": captured}
        replica.journal.set("job", pending)
        replica.runtime.mongo = object()
        configured.mongodb_backup_key = SecretStr(replacement_mongo_key)

        def current_credentials():
            current_key = configured.mongodb_backup_key.get_secret_value()
            return captured | {"mongodb_backup_key": current_key}

        monkeypatch.setattr(replica, "_credentials", current_credentials)
        archive = {
            "format": "mongodb-encrypted-v1",
            "id": "captured-full-mongo",
            "revision": store.revision,
            "source_channel": configured.storage_channel,
            "source_message_id": 900,
            "replica_channel": BACKUP,
            "replica_message_id": 901,
            "size": 128,
            "sha256": "c" * 64,
            "created_at": "2026-10-22T10:00:00Z",
        }
        mirror = AsyncMock(return_value=archive)
        monkeypatch.setattr("tgdrive.replica_mongo.mirror_mongodb", mirror)
        calls, connections = list(remote.calls), len(remote.instances)
        with pytest.raises(SettingsProblem) as error:
            await replica._sync(config)
        assert error.value.code == "REPLICA_MONGODB_KEY_CHANGED"
        assert original_mongo_key not in str(error.value)
        assert replacement_mongo_key not in str(error.value)
        mirror.assert_not_awaited()
        assert remote.calls == calls and len(remote.instances) == connections
        assert replica.journal.get("job") == pending
        assert replica.journal.get("restore_point") == point
        assert remote.pins[BACKUP] == point["backup_message_id"]
        assert replica.recovery_kit()["mongodb_backup_key"] == original_mongo_key
        # Reinstating the captured key resumes this exact generation safely.
        configured.mongodb_backup_key = SecretStr(original_mongo_key)
        await replica._sync(config)
        mirror.assert_awaited_once()
        bundle = remote_bundle(remote, replica)
        assert bundle["credentials"]["mongodb_backup_key"] == original_mongo_key
        assert bundle["credentials"]["mongodb_archive"] == archive
        assert replica.status()["protected_revision"] == pending["catalog"]["revision"]
        assert replica.journal.get("job") is None
        assert remote.messages[BACKUP, point["backup_message_id"]] == original_capsule
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_provider_inflight_reply_preserves_deadline_and_identity_after_restart(
    tmp_path,
):
    from telethon.errors import RandomIdDuplicateError

    from tgdrive.backup import BackupError
    from tgdrive.replica_transport import _provider_error

    configured, store, remote, replica, config = setup(tmp_path)

    def still_inflight():
        raise _provider_error(RandomIdDuplicateError(request=TOKEN))

    try:
        remote.after_copy = still_inflight
        with pytest.raises(ReplicaTransportError) as error:
            await replica.before_mutation()
        assert error.value.code == "REPLICA_IN_FLIGHT"
        state = replica.status()
        assert state["phase"] == "waiting"
        assert state["error"]["retry_at"] > time.time()
        assert TOKEN not in state["error"]["detail"]
        assert len(remote.receipts) == 1
        first_id = next(call[-1] for call in remote.calls if call[0] == "copy")
        await replica.close()
        replica = ChannelReplica(
            configured, SimpleNamespace(store=store), transport_factory=remote.factory
        )
        calls = list(remote.calls)
        with pytest.raises(BackupError):
            await replica._sync(config)
        assert remote.calls == calls, "Restart cannot bypass the durable wait"
        replica.update(error=state["error"] | {"retry_at": 0})
        await replica._sync(config)
        ids = [call[-1] for call in remote.calls if call[0] == "copy"]
        assert ids == [first_id, first_id]
        assert replica.status()["protected_revision"] == store.revision
        assert len([row for row in remote.messages if row[0] == BACKUP]) == 2
    finally:
        await dispose(replica, store)
