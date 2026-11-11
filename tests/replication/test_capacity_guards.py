from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.replication.test_replica_api import owner as owner
from tests.replication.test_replica_transport import (
    BACKUP,
    BACKUP_NAME,
    document,
    message,
    started,
)
from tests.support.app_fixtures import login
from tgdrive.replication import replica_activation
from tgdrive.replication.replica_state import atomic_private
from tgdrive.replication.replica_transport import ReplicaTransportError


def test_atomic_private_refusal_keeps_old_file_and_no_temporary(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_bytes(b"existing-journal")
    with pytest.raises(OSError):
        atomic_private(path, b"new-intent", probe_directory=tmp_path / "missing")
    assert path.read_bytes() == b"existing-journal"
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_private_rechecks_bounded_writes_and_preserves_old_on_failure(
    tmp_path, monkeypatch
):
    probe = tmp_path / "probe"
    probe.mkdir()
    path = tmp_path / "manifest.json"
    path.write_bytes(b"existing-journal")
    calls = 0

    def usage(directory):
        nonlocal calls
        if directory == probe:
            calls += 1
        return SimpleNamespace(total=1024**3, used=0, free=1024**3 if calls < 3 else 0)

    monkeypatch.setattr("tgdrive.storage.capacity.shutil.disk_usage", usage)
    with pytest.raises(OSError):
        atomic_private(path, b"x" * (128 * 1024), probe_directory=probe)
    assert calls >= 3
    assert path.read_bytes() == b"existing-journal"
    assert {entry.name for entry in tmp_path.iterdir()} == {"manifest.json", "probe"}


def test_mongo_preservation_probe_refusal_precedes_transaction(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    calls = []
    backend = SimpleNamespace(transaction=lambda callback: calls.append(callback))
    with pytest.raises(OSError):
        replica_activation._preserve_mongo(
            backend, archive, probe_directory=tmp_path / "missing"
        )
    assert calls == []
    assert not list(archive.iterdir())


def test_mongo_preservation_rechecks_and_removes_only_its_failed_temporary(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    marker = archive / "protected-existing"
    marker.write_bytes(b"retain")
    probe = tmp_path / "probe"
    probe.mkdir()
    calls = 0

    def usage(directory):
        nonlocal calls
        if directory == probe:
            calls += 1
        return SimpleNamespace(total=1024**3, used=0, free=1024**3 if calls < 5 else 0)

    monkeypatch.setattr("tgdrive.storage.capacity.shutil.disk_usage", usage)
    db = {
        name: SimpleNamespace(
            find=lambda *args, **kwargs: [
                {"_id": "entry", "content": "x" * (128 * 1024)}
            ]
        )
        for name in replica_activation._MONGO_ARCHIVE
    }
    backend = SimpleNamespace(
        db=db, fence=lambda _: None, transaction=lambda callback: callback(None)
    )
    with pytest.raises(OSError):
        replica_activation._preserve_mongo(backend, archive, probe_directory=probe)
    assert marker.read_bytes() == b"retain"
    assert list(archive.iterdir()) == [marker]
    assert calls >= 5


async def test_replica_download_probe_failure_preserves_existing_state(tmp_path):
    transport, native = await started()
    transport._settings.resolved_data_dir = tmp_path
    transport._settings.data_backing_capacity_dir = tmp_path / "missing"
    native.messages[BACKUP, 901] = message(
        901, BACKUP, document(len(native.body), BACKUP_NAME)
    )
    native.pins[BACKUP] = 901
    marker = tmp_path / "protected-journal"
    marker.write_bytes(b"retain")
    try:
        with pytest.raises(ReplicaTransportError) as error:
            await transport.download(BACKUP, None, tmp_path / "recovery.tgdb")
        assert error.value.code == "REPLICA_LOCAL_SPACE"
        assert marker.read_bytes() == b"retain"
        assert list(tmp_path.iterdir()) == [marker]
    finally:
        await transport.close()


def test_replica_import_probe_failure_refuses_before_inspection(owner):
    client, _runtime, replica = owner
    login(client)
    replica.settings.data_backing_capacity_dir = replica.directory / "missing-probe"
    replica.inspect_file = AsyncMock()
    before = list(replica.directory.iterdir())
    response = client.post(
        "/api/v1/replica/import",
        content=b"ciphertext",
        headers={
            "Content-Type": "application/octet-stream",
            "X-TGDrive-Recovery-Key": "a1" * 32,
        },
    )
    assert response.status_code == 503
    replica.inspect_file.assert_not_awaited()
    assert list(replica.directory.iterdir()) == before


def test_atomic_replacement_charges_new_bytes_without_double_counting_old_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "manifest.json"
    path.write_bytes(b"old" * 1000)
    monkeypatch.setattr(
        "tgdrive.storage.capacity.shutil.disk_usage",
        lambda _: SimpleNamespace(total=10000, used=9800, free=200),
    )
    atomic_private(path, b"x" * 100, min_free_bytes=50)
    assert path.read_bytes() == b"x" * 100


def test_private_replacement_preserves_promised_upload_space_and_old_archive(
    tmp_path, monkeypatch
):
    path = tmp_path / "archive.tgdb"
    path.write_bytes(b"old-protected")
    monkeypatch.setattr(
        "tgdrive.storage.capacity.shutil.disk_usage",
        lambda _: SimpleNamespace(total=2000, used=1000, free=1000),
    )
    with pytest.raises(OSError):
        atomic_private(path, b"new", outstanding_bytes=1000)
    assert path.read_bytes() == b"old-protected"
    assert list(tmp_path.iterdir()) == [path]


def test_replica_import_counts_live_future_upload_reservations(owner, monkeypatch):
    import asyncio

    client, runtime, replica = owner
    login(client)
    replica.settings.min_free_bytes = 0
    replica.inspect_file = AsyncMock()
    observed = []

    def remaining():
        assert asyncio.current_task() is not None
        observed.append(True)
        return 1000

    runtime._remaining_staging_bytes = remaining
    monkeypatch.setattr(
        "tgdrive.storage.capacity.shutil.disk_usage",
        lambda _: SimpleNamespace(total=2000, used=1000, free=1000),
    )
    response = client.post(
        "/api/v1/replica/import",
        content=b"ciphertext",
        headers={
            "Content-Type": "application/octet-stream",
            "X-TGDrive-Recovery-Key": "a1" * 32,
        },
    )
    assert response.status_code == 507
    assert observed
    replica.inspect_file.assert_not_awaited()
    assert not list(replica.directory.glob(".replica-import-*"))


def test_live_activation_intent_preserves_catalog_and_promised_upload_space(
    owner, monkeypatch
):
    from tests.replication.test_replica_activation import NEW, candidate

    client, runtime, replica = owner
    login(client)
    replica.settings.min_free_bytes = 0
    before = runtime.store.export()
    replica.journal.set("candidate", candidate(before))
    runtime._remaining_staging_bytes = lambda: 1024**2
    monkeypatch.setattr(
        "tgdrive.storage.capacity.shutil.disk_usage",
        lambda _: SimpleNamespace(total=2 * 1024**2, used=1024**2, free=1024**2),
    )
    response = client.post(
        "/api/v1/replica/recover", json={"activate": True, "confirm_channel": str(NEW)}
    )
    assert response.status_code == 507
    assert runtime.store.export() == before
    assert not (replica.directory / replica_activation.MANIFEST).exists()
    assert not replica.journal.get("activation")
