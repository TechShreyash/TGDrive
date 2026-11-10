"""Coordinator fault injection against isolated synthetic Telegram channels."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from collections.abc import Callable
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from tests.support.mongo_fixtures import new_store, register_test_backend
from tests.support.recovery_fixtures import settings
from tgdrive.replication.replica import ChannelReplica
from tgdrive.replication.replica_state import (
    ReplicaJournal,
    decode_bundle,
    files_in,
    parts_in,
)
from tgdrive.replication.replica_transport import ReplicaTransportError
from tgdrive.storage.mongo import for_directory

BACKUP = -1002234567890
REPLACEMENT = -1003234567890
TOKEN = "234567:" + "b" * 24

pytestmark = pytest.mark.usefixtures("replica_backend")


@pytest.fixture
def replica_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, settings(tmp_path))


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
        self.copy_error: ReplicaTransportError | None = None
        self.publish_error_channel: int | None = None
        self.lose_publish_response_channel: int | None = None
        self.after_copy: Callable[[], object] | None = None
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

    async def download(self, channel, message_id, destination):
        identity = message_id or self.remote.pins[channel]
        content = self.remote.messages[channel, identity]
        if content.startswith(b"{"):
            raise ReplicaTransportError("Different filename.", "REPLICA_DOCUMENT_TYPE")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(0o600)
        return identity


def setup(tmp_path, *, multipart=False, empty=False):
    configured = settings(tmp_path)
    configured.resolved_data_dir.mkdir(parents=True, exist_ok=True)
    store = new_store(configured.resolved_data_dir / "catalog")
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
    runtime = SimpleNamespace(
        store=store, mongo=for_directory(configured.resolved_data_dir)
    )
    replica = ChannelReplica(configured, runtime, transport_factory=remote.factory)
    replica._capture = capture_for(replica, store)
    config = {"channel": BACKUP, "token": TOKEN, "enabled": True}
    replica.journal.set("config", config)
    return configured, store, remote, replica, config


async def dispose(replica, store):
    await replica.close()
    store.close()


def test_local_archive_selection_keeps_unverified_and_older_retained_files(tmp_path):
    configured, store, _remote, replica, _ = setup(tmp_path)
    directory = configured.resolved_cache_dir / "mongo-backups"
    directory.mkdir(parents=True)
    path = directory / "retained.tgdb"
    path.write_bytes(b"retained encrypted archive")
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = [
        {"id": "pending", "status": "pending", "created_at": "2026-11-07T12:00:00Z"},
        {"id": "missing", "status": "confirmed", "created_at": "2026-11-07T11:00:00Z"},
        {
            "id": "retained",
            "status": "unverified",
            "created_at": "2026-11-07T10:00:00Z",
            "sha256": digest,
        },
    ]
    replica.runtime.backup = SimpleNamespace(
        latest_history=lambda: rows[:1], local_archives=lambda: iter(rows)
    )
    try:
        assert replica.backup_file() == path
        assert replica.status()["backup_file_available"]
        assert replica.recovery_file() == path
        path.write_bytes(b"changed archive")
        from tgdrive.settings.managed_settings import SettingsProblem

        with pytest.raises(SettingsProblem, match="changed") as error:
            replica.recovery_file()
        assert error.value.status == 409
        path.unlink()
        assert replica.backup_file() is None
        assert not replica.status()["backup_file_available"]
    finally:
        store.close()


@pytest.mark.parametrize(
    "kind", ["missing", "permissions", "symlink", "directory", "fifo"]
)
def test_local_archive_open_failures_are_structured(tmp_path, monkeypatch, kind):
    from tgdrive.settings.managed_settings import SettingsProblem

    _configured, store, _remote, replica, _ = setup(tmp_path)
    path = tmp_path / "saved.tgdb"
    path.write_bytes(b"saved archive")
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if kind == "permissions":
        path.chmod(0o644)
    elif kind == "symlink":
        target = tmp_path / "target.tgdb"
        path.rename(target)
        path.symlink_to(target)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "fifo":
        import os

        path.unlink()
        os.mkfifo(path)

    def selection():
        if kind == "missing":
            path.unlink()
        return path, digest

    monkeypatch.setattr(replica, "_backup_selection", selection)
    try:
        with pytest.raises(SettingsProblem) as error:
            replica.recovery_file()
        assert error.value.status == (404 if kind == "missing" else 409)
    finally:
        store.close()


def test_verified_archive_descriptor_survives_retention_unlink(tmp_path, monkeypatch):
    _configured, store, _remote, replica, _ = setup(tmp_path)
    path = tmp_path / "retained.tgdb"
    path.write_bytes(b"verified encrypted archive")
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(replica, "_backup_selection", lambda: (path, digest))
    try:
        selected, source = replica.open_recovery_file()
        try:
            assert selected == path and source.tell() == 0
            path.unlink()
            assert source.read() == b"verified encrypted archive"
        finally:
            source.close()
        assert source.closed
    finally:
        store.close()


@pytest.mark.asyncio
async def test_file_inspection_keeps_saved_provider_wait_before_new_requests(tmp_path):
    from tgdrive.settings.managed_settings import SettingsProblem

    _, store, remote, replica, _ = setup(tmp_path)
    deadline = time.time() + 120
    replica.update(phase="waiting", error={"retry_at": deadline})
    try:
        with pytest.raises(SettingsProblem, match="retry time"):
            await replica.inspect_file(tmp_path / "not-read.tgdb", "a1" * 32)
        assert not remote.instances
        assert replica.status()["error"]["retry_at"] == deadline
    finally:
        await dispose(replica, store)


def journal_record(journal: ReplicaJournal, key: str):
    record = journal.get(key)
    assert isinstance(record, dict)
    return record


def remote_bundle(remote, replica, channel=BACKUP):
    return decode_bundle(remote.messages[channel, remote.pins[channel]], replica.key())


def capture_for(replica, store):
    from tgdrive.recovery.mongo_backup import COLLECTIONS
    from tgdrive.recovery.recovery_file import snapshot_catalog
    from tgdrive.replication.replica_activation import _mongo_nodes
    from tgdrive.replication.replica_settings import credentials_from_snapshot

    def capture():
        from tgdrive.core.encryption import key_record

        catalog = store.export()
        records = {name: [] for name in COLLECTIONS}
        records["settings"] = [key_record(replica.settings.encryption_key)]
        records["catalog"] = [
            {
                "_id": "catalog",
                "schema_version": catalog["schema_version"],
                "drive_id": catalog["drive_id"],
                "revision": catalog["revision"],
                "backed_up_revision": catalog["backed_up_revision"],
                "storage_channel_id": catalog["storage_channel_id"],
                "generation": 1,
            }
        ]
        records["nodes"] = _mongo_nodes(catalog)
        records["shares"] = [
            dict(row, _id=row["id"]) for row in catalog.get("shares", [])
        ]
        for name, portable, identity in (
            ("operations", "operations", "operation_id"),
            ("deletions", "deletions", "id"),
            ("preparations", "folder_preparations", "request_id"),
        ):
            records[name] = [
                dict(row, _id=row[identity]) for row in catalog.get(portable, [])
            ]
        return {
            "catalog": snapshot_catalog(records),
            "database": records,
            "credentials": credentials_from_snapshot(replica.settings, records),
        }

    return capture


def enable_staged_capture(replica, store, *, large=False):
    """Seed an explicit snapshot adapter, then exercise production capture."""
    records = capture_for(replica, store)()["database"]
    backend = replica.runtime.mongo
    assert backend is not None
    backend.client = SimpleNamespace(start_session=lambda **_: nullcontext(object()))
    for name, rows in records.items():
        backend.db[name].delete_many({})
        if rows:
            backend.db[name].insert_many(rows)
    if large:
        # Exceed both prior catalog-count and expanded-metadata ceilings without
        # requiring thousands of synthetic Telegram side effects.
        backend.db.identities.insert_many(
            {"_id": f"identity-{index}", "name": "x" * 2000} for index in range(10_001)
        )
    del replica._capture


@pytest.mark.asyncio
async def test_staged_replica_crosses_old_limits_without_catalog_materialization(
    tmp_path, monkeypatch
):
    from tgdrive.replication import replica as replica_module

    configured, store, remote, replica, config = setup(tmp_path)
    enable_staged_capture(replica, store, large=True)
    original_export = store.export
    before = original_export()

    def reject_materialization(*_args, **_kwargs):
        raise AssertionError("The staged path must not materialize a full catalog")

    monkeypatch.setattr(store, "export", reject_materialization)
    monkeypatch.setattr(replica_module, "decode_bundle", reject_materialization)
    monkeypatch.setattr(replica, "start", lambda: None)
    try:
        remote.lose_response = True
        with pytest.raises(ReplicaTransportError, match="Response lost"):
            await replica._sync(config)
        job = replica.journal.get("job")
        assert isinstance(job, dict)
        assert "staged" in job and "database" not in job
        assert len(str(job)) < 10_000
        index = replica._open_staged(job).path
        assert index.stat().st_size > 16 * 1024**2
        await replica._sync(config)
        assert not index.exists()
        copies = [call for call in remote.calls if call[0] == "copy"]
        assert len(copies) == 2 and copies[0][-1] == copies[1][-1]
        await replica.inspect_file(replica.backup_file(), replica.key(), source=BACKUP)
        candidate = replica.journal.get("candidate")
        assert isinstance(candidate, dict)
        assert "staged" in candidate and "database" not in candidate
        assert "root" not in candidate["catalog"]
        assert len(str(candidate)) < 10_000
        await replica.recover_to(REPLACEMENT, "345678:" + "z" * 30, str(REPLACEMENT))
        await replica._recover_copy(replica.journal.get("recovery"))
        recovered = replica.journal.get("candidate")
        assert isinstance(recovered, dict)
        assert recovered["complete"] and "staged" in recovered
        assert recovered["catalog"]["storage_channel_id"] == REPLACEMENT
        staged = replica._open_staged(recovered)
        for node in staged.nodes():
            if node["kind"] != "file":
                continue
            for part in parts_in(node):
                identity = (
                    f"{store.drive_id}:{REPLACEMENT}:{BACKUP}:"
                    f"{part['message_id']}:{part['size']}"
                )
                assert replica.journal.intent(identity, part["size"])[
                    1
                ] == staged.mapping(part["message_id"])
        replica.activate()
        assert replica.status()["phase"] == "restart_required"
        assert original_export() == before
        assert not list(configured.resolved_data_dir.glob(".replica-mapping-*"))
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_cancelled_staged_worker_drains_and_removes_unadmitted_index(tmp_path):
    import threading

    _configured, store, _remote, replica, config = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    try:
        await replica._sync(config)
        bundle = await asyncio.to_thread(
            replica._read_bundle, replica.backup_file(), replica.key()
        )
        index = replica._open_staged(bundle).path

        def stage():
            entered.set()
            release.wait()
            return bundle

        operation = asyncio.create_task(replica._staged_worker(stage))
        await asyncio.to_thread(entered.wait)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        assert index.exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert not index.exists()
    finally:
        release.set()
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_cancelled_mapping_creation_drains_and_closes_created_index(
    tmp_path, monkeypatch
):
    import threading

    from tgdrive.replication import replica as replica_module

    configured, store, remote, replica, config = setup(tmp_path)
    enable_staged_capture(replica, store)
    entered, release = threading.Event(), threading.Event()
    operation = None
    allocate = replica_module._CopyMapping

    def delayed_mapping(*args):
        mapping = allocate(*args)
        entered.set()
        release.wait()
        return mapping

    monkeypatch.setattr(replica_module, "_CopyMapping", delayed_mapping)
    try:
        operation = asyncio.create_task(replica._sync(config))
        assert await asyncio.to_thread(entered.wait, 5)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        assert list(configured.resolved_data_dir.glob(".replica-mapping-*"))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert not list(configured.resolved_data_dir.glob(".replica-mapping-*"))
        assert not remote.calls
        assert journal_record(replica.journal, "job")["staged"]
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_missing_normal_job_index_recaptures_without_losing_confirmed_state(
    tmp_path, monkeypatch
):
    configured, store, remote, replica, config = setup(tmp_path)
    enable_staged_capture(replica, store)
    try:
        await replica._sync(config)
        confirmed = replica.journal.get("restore_point")
        assert isinstance(confirmed, dict)
        protected = replica.status()["protected_revision"]
        job = await asyncio.to_thread(replica._capture)
        replica.journal.set("job", job)
        replica._open_staged(job).path.unlink()
        random_id, _ = replica.journal.intent("pending-original-part", 3)
        capture = replica._capture
        captured = []

        def recapture():
            assert replica.journal.get("restore_point") == confirmed
            assert replica.status()["protected_revision"] == protected
            assert replica.journal.intent("pending-original-part", 3) == (
                random_id,
                None,
            )
            captured.append(True)
            return capture()

        monkeypatch.setattr(replica, "_capture", recapture)
        await replica._sync(config, force_snapshot=True)
        assert captured == [True]
        assert replica.journal.get("job") is None
        assert replica.status()["phase"] == "protected"
        assert replica.journal.intent("pending-original-part", 3) == (random_id, None)
        assert len([call for call in remote.calls if call[0] == "copy"]) == 1
        assert not list(configured.resolved_data_dir.glob(".replica-mapping-*"))
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_key", ["candidate", "recovery", "activation"])
async def test_missing_job_cannot_discard_recovery_or_activation(
    tmp_path, protected_key
):
    from tgdrive.settings.managed_settings import SettingsProblem

    _configured, store, _remote, replica, _config = setup(tmp_path)
    enable_staged_capture(replica, store)
    try:
        job = await asyncio.to_thread(replica._capture)
        replica.journal.set("job", job)
        replica._open_staged(job).path.unlink()
        preserved = {"preserve": protected_key}
        replica.journal.set(protected_key, preserved)
        with pytest.raises(SettingsProblem, match="Preserve recovery state"):
            await asyncio.to_thread(replica._restart_missing_job, job)
        assert replica.journal.get("job") == job
        assert replica.journal.get(protected_key) == preserved
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_completed_recovery_commit_survives_cleanup_failure_without_recopy(
    tmp_path, monkeypatch
):
    configured, store, remote, replica, config = setup(tmp_path)
    enable_staged_capture(replica, store)
    monkeypatch.setattr(replica, "start", lambda: None)
    try:
        await replica._sync(config)
        await replica.inspect_file(replica.backup_file(), replica.key(), source=BACKUP)
        await replica.recover_to(REPLACEMENT, "345678:" + "z" * 30, str(REPLACEMENT))
        recovery = replica.journal.get("recovery")
        assert isinstance(recovery, dict)
        previous = replica.journal.get("candidate")
        assert isinstance(previous, dict)
        discard = replica._discard_staged
        failed = []

        def fail_after_commit(value):
            saved = replica.journal.get_many({"candidate": None, "status": {}})
            assert isinstance(saved["candidate"], dict)
            assert isinstance(saved["status"], dict)
            if value == previous and saved["candidate"].get("complete") and not failed:
                assert saved["status"]["phase"] == "ready_to_activate"
                failed.append(True)
                raise OSError("Injected cleanup failure after readiness commit")
            discard(value)

        monkeypatch.setattr(replica, "_discard_staged", fail_after_commit)
        with pytest.raises(OSError, match="after readiness commit") as error:
            await replica._recover_copy(recovery)
        assert failed == [True]
        replica._record_failure(error.value)
        completed = replica.journal.get("candidate")
        assert isinstance(completed, dict)
        assert completed["complete"]
        assert completed["catalog"]["storage_channel_id"] == REPLACEMENT
        assert replica._open_staged(completed).path.exists()
        calls = list(remote.calls)
        await replica.close()
        replica = ChannelReplica(
            configured, replica.runtime, transport_factory=remote.factory
        )
        replica.start = lambda: None
        await replica.action("retry")
        await replica._recover_copy(recovery)
        assert replica.journal.get("candidate") == completed
        assert replica.status()["phase"] == "ready_to_activate"
        assert remote.calls == calls
        assert not list(configured.resolved_data_dir.glob(".replica-mapping-*"))
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["paused", "disabled"])
async def test_recovery_completion_preserves_control_stop(tmp_path, phase):
    _configured, store, remote, replica, _config = setup(tmp_path)
    recovery = {"target": REPLACEMENT}
    previous = {"complete": False}
    replica.journal.set_many({"candidate": previous, "recovery": recovery})
    replica.update(phase=phase)
    try:
        completed = {"complete": True, "discovered": True}
        assert await replica._complete_recovery(completed, recovery) is False
        assert replica.journal.get("candidate") == previous
        assert journal_record(replica.journal, "status")["phase"] == phase
        assert not remote.calls
        assert replica.journal.get("activation") is None
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_discovered_completion_with_busy_control_stays_ready_without_deadlock(
    tmp_path, monkeypatch
):
    _configured, store, _remote, replica, _config = setup(tmp_path)
    recovery = {"target": REPLACEMENT}
    completed = {"complete": True, "discovered": True}
    replica.journal.set("recovery", recovery)

    def unexpected_activation():
        raise AssertionError("Busy controls require explicit activation later")

    monkeypatch.setattr(replica, "activate", unexpected_activation)
    try:
        async with replica.control, replica._operation:
            assert await asyncio.wait_for(
                replica._complete_recovery(completed, recovery), timeout=2
            )
        assert replica.journal.get("candidate") == completed
        assert journal_record(replica.journal, "status")["phase"] == "ready_to_activate"
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_pause_after_readiness_commit_prevents_automatic_activation(
    tmp_path, monkeypatch
):
    import threading

    _configured, store, _remote, replica, _config = setup(tmp_path)
    recovery = {"target": REPLACEMENT}
    completed = {"complete": True, "discovered": True}
    replica.journal.set("recovery", recovery)
    committed, release = threading.Event(), threading.Event()
    save = replica._save_candidate
    operation = None

    def save_then_wait(*args, **kwargs):
        result = save(*args, **kwargs)
        committed.set()
        release.wait()
        return result

    def unexpected_activation():
        raise AssertionError("Pause must win over automatic activation")

    monkeypatch.setattr(replica, "_save_candidate", save_then_wait)
    monkeypatch.setattr(replica, "activate", unexpected_activation)
    try:
        operation = asyncio.create_task(replica._complete_recovery(completed, recovery))
        assert await asyncio.to_thread(committed.wait, 5)
        async with replica.control:
            replica.update(phase="paused")
            release.set()
            assert await asyncio.wait_for(operation, timeout=2)
        assert replica.journal.get("candidate") == completed
        assert journal_record(replica.journal, "status")["phase"] == "paused"
        assert replica.journal.get("activation") is None
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_resource_rejection_status_and_controls_preserve_unreadable_state(
    tmp_path, monkeypatch
):
    from tgdrive.settings.managed_settings import SettingsProblem

    _configured, store, remote, replica, _config = setup(tmp_path)
    problem = {
        "key": "candidate",
        "code": "ACTIVATION_RESOURCE_LIMIT",
        "detail": "Preserve the saved recovery state and original archive.",
    }
    get = replica.journal.get
    reads = []

    def reject_saved_values(key, default=None):
        reads.append(key)
        assert key == "backup_file", "Rejected state must not be decoded again"
        return get(key, default)

    def reject_writes(*_args, **_kwargs):
        raise AssertionError("Resource rejection must not overwrite saved state")

    monkeypatch.setattr(replica.journal, "resource_error", lambda: problem)
    monkeypatch.setattr(replica.journal, "get", reject_saved_values)
    monkeypatch.setattr(replica.journal, "set_many", reject_writes)
    try:
        for _ in range(2):
            state = replica.status()
            assert state["phase"] == "error"
            assert state["error"]["code"] == problem["code"]
            assert state["error"]["detail"] == problem["detail"]
            assert "preserve" in state["error"]["action"]
            assert state["error"]["retry_at"] == 0
            assert not state["file_restore_allowed"]
        for action in ("disable", "retry"):
            with pytest.raises(SettingsProblem) as error:
                await replica.action(action)
            assert error.value.status == 409
            assert error.value.code == problem["code"]
        assert replica.blocked is True
        failure = OSError("Synthetic secondary failure")
        assert replica._record_failure(failure) is failure
        await replica._loop()
        assert reads == ["backup_file", "backup_file"]
        assert not remote.instances
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_manifest_resource_error_survives_controls_and_keeps_safe_download(
    tmp_path, monkeypatch
):
    from tgdrive.replication.replica_activation import ActivationError, _resource_error

    configured, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        archive = replica.backup_file()
        assert archive is not None
        content = archive.read_bytes()
        before = replica.journal.get_many({"config": None, "status": None})
        error = _resource_error(
            "Pending activation exceeds provisional decoding limits"
        )
        replica.runtime._activation_unavailable = error
        get = replica.journal.get
        reads = []

        def bounded_selection_only(key, default=None):
            reads.append(key)
            assert key == "backup_file"
            return get(key, default)

        monkeypatch.setattr(replica.journal, "get", bounded_selection_only)
        for _ in range(2):
            state = replica.status()
            assert state["error"]["code"] == "ACTIVATION_RESOURCE_LIMIT"
            assert "preserve" in state["error"]["action"]
            assert state["backup_file_available"]
        path, source = replica.open_recovery_file()
        with source:
            assert path == archive and source.read() == content
        for action in ("retry", "resume", "disable"):
            with pytest.raises(ActivationError) as refused:
                await replica.action(action)
            assert refused.value.code == "ACTIVATION_RESOURCE_LIMIT"
        assert replica.blocked is True
        await replica._loop()
        assert reads == ["backup_file"] * 3
        monkeypatch.setattr(replica.journal, "get", get)
        assert replica.journal.get_many({"config": None, "status": None}) == before
        assert archive.read_bytes() == content
        assert configured.storage_channel == store.storage_channel_id
        assert len(remote.instances) == 1
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("apply_first", [False, True])
async def test_manifest_without_journal_flag_keeps_staged_index_across_disable(
    tmp_path, monkeypatch, apply_first
):
    from tgdrive.replication.replica_activation import _load, apply_activation

    configured, store, remote, replica, config = setup(tmp_path)
    enable_staged_capture(replica, store)
    monkeypatch.setattr(replica, "start", lambda: None)
    try:
        await replica._sync(config)
        await replica.inspect_file(replica.backup_file(), replica.key(), source=BACKUP)
        await replica.recover_to(REPLACEMENT, "345678:" + "z" * 30, str(REPLACEMENT))
        await replica._recover_copy(replica.journal.get("recovery"))
        candidate = replica.journal.get("candidate")
        assert isinstance(candidate, dict)
        index = replica._open_staged(candidate).path
        set_value = replica.journal.set

        def lose_activation_flag(key, value):
            if key == "activation":
                raise OSError("Injected activation flag transaction failure")
            return set_value(key, value)

        with monkeypatch.context() as context:
            context.setattr(replica.journal, "set", lose_activation_flag)
            with pytest.raises(OSError, match="flag transaction failure"):
                await asyncio.to_thread(replica.activate)
        assert replica.journal.get("activation") is None
        manifest = _load(configured)
        assert manifest is not None
        assert manifest["phase"] == "prepared"
        if apply_first:
            assert await asyncio.to_thread(
                apply_activation, configured, replica.runtime.mongo
            )
            runtime = replica.runtime
            await replica.close()
            replica = ChannelReplica(
                configured, runtime, transport_factory=remote.factory
            )
            replica.start = lambda: None
        await replica.action("disable")
        assert replica.journal.get("candidate") is None
        assert replica.journal.get("recovery") is None
        assert index.exists()
        await asyncio.to_thread(replica._discard_staged, candidate)
        assert index.exists()
        manifest = _load(configured)
        assert manifest is not None
        assert manifest["candidate"]["staged"] == candidate["staged"]
        if not apply_first:
            assert await asyncio.to_thread(
                apply_activation, configured, replica.runtime.mongo
            )
        manifest = _load(configured)
        assert manifest is not None and manifest["phase"] == "applied"
        await asyncio.to_thread(replica._discard_staged, candidate)
        assert index.exists()
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", [b"not JSON", b"[" + b"{}," * 65_537 + b"{}]"])
async def test_unreadable_active_manifest_prevents_staged_cleanup(tmp_path, unreadable):
    from tgdrive.replication.replica_activation import MANIFEST

    configured, store, _remote, replica, _config = setup(tmp_path)
    enable_staged_capture(replica, store)
    try:
        job = await asyncio.to_thread(replica._capture)
        index = replica._open_staged(job).path
        manifest = configured.resolved_data_dir / MANIFEST
        manifest.write_bytes(unreadable)
        manifest.chmod(0o600)
        await asyncio.to_thread(replica._discard_staged, job)
        assert index.exists()
        assert manifest.read_bytes() == unreadable
        manifest.unlink()
        await asyncio.to_thread(replica._discard_staged, job)
        assert not index.exists()
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_first", [False, True])
async def test_first_resource_rejection_blocks_mutations_and_keeps_status_readable(
    tmp_path, monkeypatch, guard_first
):
    import json

    _configured, store, _remote, replica, _config = setup(tmp_path)
    assert replica.journal.backend is not None
    database = replica.journal.backend.db
    raw = json.dumps([{}] * 65_537)
    database.replica_state.insert_one({"_id": "candidate", "length": len(raw)})
    database.replica_chunks.insert_one(
        {"_id": "candidate\x000", "key": "candidate", "index": 0, "data": raw}
    )
    find, reads = database.replica_chunks.find, []

    def counted_read(query, *args, **kwargs):
        if query.get("key") == "candidate":
            reads.append(query)
        return find(query, *args, **kwargs)

    monkeypatch.setattr(database.replica_chunks, "find", counted_read)
    try:
        if guard_first:
            assert replica.blocked is True
        for _ in range(2):
            state = replica.status()
            assert state["phase"] == "error"
            assert state["error"]["code"] == "ACTIVATION_RESOURCE_LIMIT"
            assert "preserve" in state["error"]["action"]
        assert replica.blocked is True
        assert len(reads) == 1
        chunk = database.replica_chunks.find_one({"key": "candidate"})
        assert chunk is not None and chunk["data"] == raw
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["publish", "pause"])
async def test_staged_resume_preserves_acknowledgements_and_publication_identity(
    tmp_path, fault
):
    configured, store, remote, replica, config = setup(tmp_path)
    enable_staged_capture(replica, store)
    try:
        if fault == "publish":
            remote.lose_publish_response_channel = BACKUP
            with pytest.raises(ReplicaTransportError, match="response lost"):
                await replica._sync(config)
        else:
            remote.after_copy = lambda: replica.update(phase="paused")
            await replica._sync(config)
            assert not remote.pins
        job = replica.journal.get("job")
        assert isinstance(job, dict)
        index = replica._open_staged(job).path
        await replica.close()
        replica = ChannelReplica(
            configured, replica.runtime, transport_factory=remote.factory
        )
        replica.update(phase="syncing")
        await replica._sync(config)
        copies = [call for call in remote.calls if call[0] == "copy"]
        assert len(copies) == 1
        assert any(call[0] == "verify" for call in remote.calls)
        publications = [
            call for call in remote.calls if call[:2] == ("publish", BACKUP)
        ]
        if fault == "publish":
            assert len(publications) == 2
            assert publications[0][-1] == publications[1][-1]
        assert replica.status()["protected_revision"] == store.revision
        assert replica.journal.get("job") is None
        assert not index.exists()
        assert not list(configured.resolved_data_dir.glob(".replica-mapping-*"))
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_staged_inspection_preserves_file_key_refusal_before_remote_probes(
    tmp_path,
):
    from tgdrive.core.encryption import key_record
    from tgdrive.replication.replica_activation import ActivationError
    from tgdrive.replication.replica_state import encode_bundle

    _configured, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        archive = replica.backup_file()
        assert archive is not None
        payload = decode_bundle(archive.read_bytes(), replica.key())
        payload["database"]["settings"] = [key_record("f" * 64)]
        path = tmp_path / "different-file-key.tgdb"
        path.write_bytes(encode_bundle(payload, replica.key()))
        path.chmod(0o600)
        calls = len(remote.instances)
        with pytest.raises(ActivationError, match="ENCRYPTION_KEY") as error:
            await replica.inspect_file(path, replica.key())
        assert error.value.status == 409
        assert len(remote.instances) == calls
        assert replica.journal.get("candidate") is None
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_v2_restore_point_publishes_one_self_contained_file_per_channel(
    tmp_path,
    monkeypatch,
):
    from tgdrive.recovery.recovery_file import snapshot_catalog

    configured, store, remote, replica, config = setup(tmp_path)
    before = store.export()
    try:
        await replica._sync(config)
        published = [call for call in remote.calls if call[0] == "publish"]
        assert len(published) == 2
        assert {call[1] for call in published} == {configured.storage_channel, BACKUP}
        point = replica.journal.get("restore_point")
        assert isinstance(point, dict)
        assert point["format_version"] == 2
        original = remote.messages[
            configured.storage_channel, point["source_message_id"]
        ]
        assert original == remote.messages[BACKUP, point["backup_message_id"]]
        decoded = decode_bundle(original, replica.key())
        assert decoded["version"] == 2
        assert snapshot_catalog(decoded["database"]) == (
            before | {"backed_up_revision": before["revision"]}
        )
        assert decoded["recovery_token"] == TOKEN
        archive = replica.backup_file()
        assert archive is not None and archive.read_bytes() == original
        assert store.export() == before
        assert replica.status()["phase"] == "protected"
        store.create_folder("/", "Next restore point")
        await replica._sync(config)
        assert len([call for call in remote.calls if call[0] == "publish"]) == 4
        assert len([call for call in remote.calls if call[0] == "copy"]) == 1
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_v2_local_retention_waits_for_confirmation_and_preserves_pending_files(
    tmp_path,
    monkeypatch,
):
    from tgdrive.replication.replica_state import atomic_private

    configured, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        previous = replica.backup_file()
        assert previous is not None
        point = replica.journal.get("restore_point")
        assert isinstance(point, dict)
        remote_content = remote.messages[BACKUP, point["backup_message_id"]]
        pending = previous.parent / (("f" * 64) + ".tgdb")
        atomic_private(pending, b"unconfirmed recovery bytes")
        store.create_folder("/", "Next generation")
        remote.publish_error_channel = configured.storage_channel
        with pytest.raises(ReplicaTransportError):
            await replica._sync(config)
        assert previous.is_file()
        assert replica.backup_file() == previous
        assert pending.read_bytes() == b"unconfirmed recovery bytes"
        remote.publish_error_channel = None
        await replica._sync(config)
        assert replica.backup_file() != previous
        archive = replica.backup_file()
        assert archive is not None and archive.is_file()
        assert not previous.exists()
        assert pending.read_bytes() == b"unconfirmed recovery bytes"
        assert remote.messages[BACKUP, point["backup_message_id"]] == remote_content
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_v2_file_rebuilds_main_from_surviving_backup_without_changing_live_drive(
    tmp_path,
    monkeypatch,
):
    configured, store, remote, replica, config = setup(tmp_path)
    monkeypatch.setattr(replica, "start", lambda: None)
    before = store.export()
    try:
        await replica._sync(config)
        saved = replica.backup_file()
        assert saved is not None
        protected = {
            key: value for key, value in remote.messages.items() if key[0] == BACKUP
        }
        remote.unavailable.add(configured.storage_channel)
        inspect_channel = Transport.inspect

        async def independent_access(self, channel, **kwargs):
            if self.token in configured.bot_tokens and channel == BACKUP:
                raise ReplicaTransportError(
                    "Main bot cannot access backup", "REPLICA_ACCESS"
                )
            return await inspect_channel(self, channel, **kwargs)

        monkeypatch.setattr(Transport, "inspect", independent_access)
        await replica.inspect_file(saved, replica.key())
        inspected = replica.journal.get("candidate")
        assert isinstance(inspected, dict)
        assert inspected["source_is_backup"]
        assert inspected["catalog"]["storage_channel_id"] == BACKUP
        assert "staged" in inspected and "database" not in inspected
        assert not inspected["complete"]
        target_token = "345678:" + "z" * 30
        await replica.recover_to(REPLACEMENT, target_token, str(REPLACEMENT))
        await replica._recover_copy(replica.journal.get("recovery"))
        recovered = replica.journal.get("candidate")
        assert isinstance(recovered, dict)
        assert recovered["complete"]
        assert recovered["catalog"]["storage_channel_id"] == REPLACEMENT
        assert recovered["backup_config"] == {"channel": BACKUP, "token": TOKEN}
        assert replica.status()["phase"] == "ready_to_activate"
        for key, value in protected.items():
            assert remote.messages[key] == value
        assert store.export() == before
        archive = replica.backup_file()
        assert archive is not None
        updated = decode_bundle(archive.read_bytes(), replica.key())
        assert all(
            updated["catalog"][key] == value
            for key, value in recovered["catalog"].items()
        )
        assert all(
            updated["replica_catalog"][key] == value
            for key, value in inspected["catalog"].items()
        )
        assert updated["credentials"]["bot_tokens"] == [target_token]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_v2_import_accepts_fresh_access_when_saved_bot_tokens_are_revoked(
    tmp_path,
    monkeypatch,
):
    from tgdrive.settings.managed_settings import SettingsProblem

    configured, store, _remote, replica, config = setup(tmp_path)
    fresh_token = "456789:" + "q" * 30
    try:
        await replica._sync(config)
        saved = replica.backup_file()
        assert saved is not None
        start = Transport.start

        async def reject_revoked(self):
            if self.token in {TOKEN, *configured.bot_tokens}:
                raise ReplicaTransportError("Token revoked", "REPLICA_CREDENTIALS")
            await start(self)

        monkeypatch.setattr(Transport, "start", reject_revoked)
        with pytest.raises(SettingsProblem) as error:
            await replica.inspect_file(saved, replica.key(), source=BACKUP)
        assert error.value.code == "RECOVERY_ACCESS_REQUIRED"
        assert replica.journal.get("candidate") is None
        assert not list(
            (configured.resolved_data_dir / "recovery-staging").glob("*.sqlite")
        )
        await replica.inspect_file(
            saved, replica.key(), source=BACKUP, token=fresh_token
        )
        candidate = replica.journal.get("candidate")
        assert isinstance(candidate, dict)
        assert candidate["token"] == fresh_token
        assert candidate["catalog"]["storage_channel_id"] == BACKUP
        assert not candidate["complete"]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_v2_database_only_restore_reuses_verified_main_bot_without_extra_token(
    tmp_path,
    monkeypatch,
):
    configured, store, remote, replica, config = setup(tmp_path)
    monkeypatch.setattr(replica, "start", lambda: None)
    try:
        await replica._sync(config)
        await replica.inspect_file(
            replica.backup_file(), replica.key(), source=configured.storage_channel
        )
        copies = len([call for call in remote.calls if call[0] == "copy"])
        await replica.recover_to(
            configured.storage_channel,
            "",
            str(configured.storage_channel),
            catalog_only=True,
        )
        await replica._recover_copy(replica.journal.get("recovery"))
        candidate = replica.journal.get("candidate")
        assert isinstance(candidate, dict)
        assert candidate["complete"] and "staged" in candidate
        assert candidate["credentials"]["bot_tokens"] == list(configured.bot_tokens)
        assert candidate["backup_config"] == {"channel": BACKUP, "token": TOKEN}
        assert len([call for call in remote.calls if call[0] == "copy"]) == copies
        assert replica.status()["phase"] == "ready_to_activate"
    finally:
        await dispose(replica, store)


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
        # The same encrypted backup is pinned in main, so restore needs only env.
        main = configured.storage_channel
        assert (
            remote.messages[main, remote.pins[main]]
            == remote.messages[BACKUP, remote.pins[BACKUP]]
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
            configured,
            SimpleNamespace(
                store=store, mongo=for_directory(configured.resolved_data_dir)
            ),
            transport_factory=remote.factory,
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
            configured,
            SimpleNamespace(
                store=store, mongo=for_directory(configured.resolved_data_dir)
            ),
            transport_factory=remote.factory,
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
    from tgdrive.replication.replica_transport import MAX_VERIFY_BATCH

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
        assert isinstance(old, dict)
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
            configured,
            SimpleNamespace(
                store=store, mongo=for_directory(configured.resolved_data_dir)
            ),
            transport_factory=remote.factory,
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
    from tgdrive.replication.replica import HEALTH_CHECK_INTERVAL_SECONDS

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        assert isinstance(point, dict)
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
    tmp_path,
    monkeypatch,
):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        assert isinstance(point, dict)
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
async def test_new_catalog_revision_is_not_delayed_until_the_next_health_check(
    tmp_path,
):
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
    tmp_path,
    monkeypatch,
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
    tmp_path,
    monkeypatch,
):
    _, store, remote, replica, config = setup(tmp_path)
    original_start = Transport.start

    async def limited_start(self):
        error = ReplicaTransportError(
            "Telegram requested a pause. Backup will resume after the wait.",
            "REPLICA_FLOOD_WAIT",
            retry_after=120,
            retryable=True,
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
        assert replica._task is not None
        replica._task.cancel()
        await asyncio.gather(replica._task, return_exceptions=True)
        monkeypatch.setattr(Transport, "start", original_start)
        connections = len(remote.instances)
        await replica.action("sync")
        assert replica._task is not None
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
async def test_expired_health_wait_is_checked_on_manual_retry(
    tmp_path, monkeypatch, action
):
    _, store, remote, replica, config = setup(tmp_path)
    monkeypatch.setattr(replica, "start", lambda: None)
    try:
        await replica._sync(config)
        connections = len(remote.instances)
        replica.update(
            phase="waiting",
            checked_at=time.time(),
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
async def test_deletion_barrier_rechecks_recent_checkpoint_before_mutation(
    tmp_path, failure
):
    from tgdrive.settings.managed_settings import SettingsProblem

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        assert isinstance(point, dict)
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
    _, store, _remote, replica, config = setup(tmp_path)
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
    tmp_path,
    monkeypatch,
    caller,
    action,
    code,
):
    _, store, remote, replica, config = setup(tmp_path)
    entered, release, recorded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    mutation = None
    report = replica.report

    async def failed_start(self):
        entered.set()
        await release.wait()
        error = ReplicaTransportError(
            "Provider requested a wait.",
            code,
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
    tmp_path,
    action,
):
    from tgdrive.settings.managed_settings import SettingsProblem

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
    tmp_path,
    monkeypatch,
):
    from tgdrive.settings.managed_settings import SettingsProblem

    _, store, _remote, replica, config = setup(tmp_path)
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
    tmp_path,
    monkeypatch,
    stage,
):
    _, store, remote, replica, config = setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original_thread = asyncio.to_thread
    from tgdrive.replication import replica as replica_module

    original_worker = replica_module._worker
    selected = replica._credentials if stage == "credentials" else replica._capture

    async def blocked_snapshot(function, *args, **kwargs):
        if function == selected:
            entered.set()
            await release.wait()
        return await original_thread(function, *args, **kwargs)

    monkeypatch.setattr(
        "tgdrive.replication.replica.asyncio.to_thread", blocked_snapshot
    )

    async def blocked_worker(function, *args, **kwargs):
        if function == selected:
            entered.set()
            await release.wait()
        return await original_worker(function, *args, **kwargs)

    monkeypatch.setattr(replica_module, "_worker", blocked_worker)
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
    tmp_path,
    monkeypatch,
    action,
    stage,
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
        assert not [call for call in remote.calls if call[0] in {"copy", "publish"}]
        if action == "disable":
            assert replica.journal.get("recovery") is None
            assert replica.journal.get("candidate") is None
        else:
            assert not journal_record(replica.journal, "candidate")["complete"]
    finally:
        release.set()
        if operation is not None and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_deletion_poll_does_not_extend_a_short_saved_telegram_wait(
    tmp_path,
    monkeypatch,
):
    from tgdrive.recovery.backup import BackupError

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        timestamp = [1000.0]
        monkeypatch.setattr(
            "tgdrive.replication.replica.time.time", lambda: timestamp[0]
        )
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
    tmp_path,
    monkeypatch,
):
    from tgdrive.recovery.backup import BackupError

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        error = {"code": "REPLICA_FLOOD_WAIT", "retry_at": 1003.0}
        replica.update(phase="waiting", error=error)
        connections = len(remote.instances)
        timestamps = iter([1002.999999])
        monkeypatch.setattr(
            "tgdrive.replication.replica.time.time", lambda: next(timestamps, 1003.0)
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
        assert isinstance(candidate, dict)
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
        assert isinstance(restored, dict)
        assert restored["complete"]
        assert restored["catalog"]["storage_channel_id"] == REPLACEMENT
        assert replica.status()["phase"] == "ready_to_activate"
        assert restored["credentials"]["bot_tokens"] == ["345678:" + "z" * 30]
        for node in replica._open_staged(restored).nodes():
            if node["kind"] != "file":
                continue
            for part in parts_in(node):
                assert (REPLACEMENT, part["message_id"]) in remote.messages
        assert (
            decode_bundle(
                remote.messages[REPLACEMENT, remote.pins[REPLACEMENT]], replica.key()
            )["catalog"]["storage_channel_id"]
            == REPLACEMENT
        )
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
            message_id=journal_record(replica.journal, "restore_point")[
                "source_message_id"
            ],
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
        assert journal_record(replica.journal, "candidate")["complete"]
        assert len([call for call in remote.calls if call[0] == "copy"]) == before
        verified = [call[2] for call in remote.calls if call[0] == "verify"]
        assert verified == [77, 78]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_wrong_key_or_foreign_channel_does_not_replace_live_metadata(tmp_path):
    from tgdrive.settings.managed_settings import SettingsProblem

    configured, store, remote, replica, config = setup(tmp_path)
    try:
        before = store.export()
        await replica._sync(config)
        with pytest.raises(SettingsProblem, match="nothing was changed") as error:
            await replica.inspect_recovery(BACKUP, TOKEN, "c2" * 32)
        assert error.value.code == "BACKUP_KEY_MISMATCH"
        assert error.value.status == 409
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
    from tgdrive.recovery.backup import BackupError
    from tgdrive.settings.managed_settings import SettingsProblem

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
    from tgdrive.settings.managed_settings import SettingsProblem

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
            configured,
            SimpleNamespace(
                store=store, mongo=for_directory(configured.resolved_data_dir)
            ),
            transport_factory=remote.factory,
        )
        await replica._sync(config)
        second_random_id = [
            call[2] for call in remote.calls if call[:2] == ("publish", BACKUP)
        ][-1]
        assert (second_random_id == first_random_id) is not lost_stage
        assert (remote.pins[BACKUP] == original_pin) is not lost_stage
        assert replica.status()["protected_revision"] == store.revision
        assert (
            journal_record(replica.journal, "restore_point")["backup_message_id"]
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
    from tgdrive.settings.managed_settings import SettingsProblem

    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        point = replica.journal.get("restore_point")
        assert isinstance(point, dict)
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
async def test_recovery_refuses_unrelated_pin_introduced_during_pause(tmp_path):
    _, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        unrelated = remote.messages[BACKUP, remote.pins[BACKUP]]
        await replica.inspect_recovery(BACKUP, TOKEN, replica.key())
        recovery = {"target": REPLACEMENT, "target_token": "345678:" + "z" * 30}
        replica.journal.set("recovery", recovery)
        replica.update(phase="recovering")
        remote.after_copy = lambda: replica.update(phase="paused")
        await replica._recover_copy(recovery)
        assert not journal_record(replica.journal, "candidate")["complete"]
        foreign_pin = remote.new_message(REPLACEMENT, unrelated)
        remote.pins[REPLACEMENT] = foreign_pin
        publications = len([call for call in remote.calls if call[0] == "publish"])
        replica.update(phase="recovering")
        with pytest.raises(ValueError, match="pin changed"):
            await replica._recover_copy(recovery)
        assert not journal_record(replica.journal, "candidate")["complete"]
        assert remote.pins[REPLACEMENT] == foreign_pin
        assert (
            len([call for call in remote.calls if call[0] == "publish"]) == publications
        )
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_provider_inflight_reply_preserves_deadline_and_identity_after_restart(
    tmp_path,
):
    from telethon.errors import RandomIdDuplicateError

    from tgdrive.recovery.backup import BackupError
    from tgdrive.replication.replica_transport import _provider_error

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
            configured,
            SimpleNamespace(
                store=store, mongo=for_directory(configured.resolved_data_dir)
            ),
            transport_factory=remote.factory,
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


class DiscoveryTransport(Transport):
    async def start(self):
        """The storage bot itself reads the pin; the backup-bot guard is skipped."""


@pytest.mark.asyncio
async def test_empty_database_finds_pinned_backup_and_restores_in_one_click(
    tmp_path, monkeypatch
):
    configured, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        pinned = remote.pins[configured.storage_channel]
        fresh_store = new_store(tmp_path / "fresh")
        runtime = SimpleNamespace(
            store=fresh_store, mongo=replica.runtime.mongo, recovery_only=True
        )
        found = ChannelReplica(
            configured,
            runtime,
            transport_factory=lambda settings, token: DiscoveryTransport(
                remote, settings, token
            ),
        )
        monkeypatch.setattr(found, "start", lambda: None)
        found.journal.set("candidate", None)
        assert await found.discover_backup() is True
        candidate = found.status()["recovery_candidate"]
        assert isinstance(candidate, dict)
        assert candidate["discovered"] and not candidate["complete"]
        assert candidate["channel"] == configured.storage_channel
        await found.restore_discovered()
        await found._recover_copy(found.journal.get("recovery"))
        assert journal_record(found.journal, "candidate")["complete"]
        assert found.status()["phase"] == "restart_required"
        assert remote.pins[configured.storage_channel] == pinned
        assert not [c for c in remote.calls if c[0] == "copy" and c[3] != BACKUP]
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_discovery_without_a_pin_starts_a_new_drive(tmp_path):
    _, store, remote, replica, _ = setup(tmp_path)
    replica.factory = lambda settings, token: DiscoveryTransport(
        remote, settings, token
    )
    try:
        assert await replica.discover_backup() is False
        assert replica.journal.get("candidate") is None
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_discovery_refuses_a_backup_the_key_cannot_open(tmp_path):
    from tgdrive.settings.managed_settings import SettingsProblem

    configured, store, remote, replica, _ = setup(tmp_path)
    channel = configured.storage_channel
    remote.pins[channel] = remote.new_message(
        channel, b"TGDRIVE-BACKUP-2\n" + b"x" * 64
    )
    replica.factory = lambda settings, token: DiscoveryTransport(
        remote, settings, token
    )
    try:
        with pytest.raises(SettingsProblem) as caught:
            await replica.discover_backup()
        assert caught.value.code == "BACKUP_KEY_MISMATCH"
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
async def test_discovery_stops_at_real_legacy_drive_json_marker(tmp_path):
    from telethon import types

    from tgdrive.replication.replica_transport import ReplicaTransport

    configured, store, remote, replica, _config = setup(tmp_path)

    class LegacyPinned(DiscoveryTransport):
        async def download(self, channel, message_id, destination):
            ReplicaTransport._capsule(
                SimpleNamespace(
                    size=123,
                    attributes=[types.DocumentAttributeFilename("drive.json")],
                )
            )

    replica.factory = lambda settings, token: LegacyPinned(remote, settings, token)
    remote.pins[configured.storage_channel] = 901
    before = dict(remote.messages)
    try:
        with pytest.raises(ReplicaTransportError) as error:
            await replica.discover_backup()
        assert error.value.code == "LEGACY_BACKUP_REQUIRES_UPGRADE"
        assert replica.status()["error"]["code"] == error.value.code
        assert replica.journal.get("candidate") is None
        assert remote.messages == before
        assert not list(configured.resolved_data_dir.glob(".replica-discover-*"))
    finally:
        await dispose(replica, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["file", "remote", "discovery"])
@pytest.mark.parametrize(
    ("failure", "code", "status"),
    [
        ("resource", "ACTIVATION_RESOURCE_LIMIT", 409),
        ("malformed", "RECOVERY_FILE_INVALID", 422),
        ("truncated", "RECOVERY_FILE_INVALID", 422),
        ("wrong_key", "BACKUP_KEY_MISMATCH", 409),
    ],
)
async def test_recovery_admission_keeps_authenticated_resource_and_key_errors_distinct(
    tmp_path, entrypoint, failure, code, status
):
    import secrets
    import zlib

    from bson import json_util
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from tgdrive.recovery.recovery_file import MAGIC
    from tgdrive.recovery.snapshot_stream import decrypt_snapshot
    from tgdrive.replication.replica_state import atomic_private
    from tgdrive.settings.managed_settings import SettingsProblem

    configured, store, remote, replica, config = setup(tmp_path)
    try:
        await replica._sync(config)
        before = store.export()
        confirmed = replica.journal.get("restore_point")
        assert isinstance(confirmed, dict)
        plaintext = tmp_path / "fixture.json"
        decrypt_snapshot(
            replica.backup_file(),
            plaintext,
            replica.key(),
            configured,
            max_expanded_bytes=None,
        )
        payload = json_util.loads(plaintext.read_text())
        if failure == "resource":
            payload["database"]["identities"] = [
                {"_id": "too-many-items", "values": [{}] * 65_537}
            ]
        elif failure == "malformed":
            payload["version"] = 99
        nonce = secrets.token_bytes(12)
        key = bytes.fromhex("f" * 64 if failure == "wrong_key" else replica.key())
        content = (
            MAGIC
            + nonce
            + AESGCM(key).encrypt(
                nonce, zlib.compress(json_util.dumps(payload).encode()), MAGIC
            )
        )
        if failure == "truncated":
            content = MAGIC + b"incomplete"
        archive = tmp_path / "refused.tgdb"
        atomic_private(archive, content)
        instances = len(remote.instances)
        with pytest.raises(SettingsProblem) as error:
            if entrypoint == "file":
                await replica.inspect_file(archive, replica.key())
            elif entrypoint == "remote":
                remote.messages[BACKUP, remote.pins[BACKUP]] = content
                await replica.inspect_recovery(BACKUP, TOKEN, replica.key())
            else:
                remote.messages[
                    configured.storage_channel, remote.pins[configured.storage_channel]
                ] = content
                replica.factory = lambda settings, token: DiscoveryTransport(
                    remote, settings, token
                )
                await replica.discover_backup()
        assert error.value.code == code
        assert error.value.status == status
        if entrypoint == "file":
            assert len(remote.instances) == instances
        replica._record_failure(error.value)
        assert replica.status()["error"]["code"] == code
        assert replica.journal.get("restore_point") == confirmed
        assert replica.journal.get("candidate") is None
        assert store.export() == before
        assert archive.read_bytes() == content
        staging = configured.resolved_data_dir / "recovery-staging"
        assert not list(staging.glob("*"))
    finally:
        await dispose(replica, store)
