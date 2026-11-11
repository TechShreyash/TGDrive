import io
from types import SimpleNamespace

import pytest

from tests.recovery.test_snapshot_stream import KEY, _validate_fixture
from tests.recovery.test_snapshot_stream import source as source
from tests.storage.test_mongodb_unit import snapshot_source as snapshot_source
from tgdrive.recovery import snapshot_stream
from tgdrive.recovery.backup import BackupError
from tgdrive.recovery.mongo_backup import MongoProtection
from tgdrive.recovery.staged_snapshot import stage_snapshot


def test_cache_backup_probe_failure_preserves_old_publication(source, tmp_path):
    settings, backend, _store, _session = source
    settings.cache_backing_capacity_dir = tmp_path / "missing-probe"
    settings.data_backing_capacity_dir = tmp_path / "data-probe"
    settings.data_backing_capacity_dir.mkdir()
    path = settings.resolved_cache_dir / "mongo-backups" / "old.tgdb"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old-confirmed-checkpoint")
    with pytest.raises(BackupError, match=r"capacity|space"):
        snapshot_stream.write_recovery_snapshot(
            backend, path, KEY, settings, validate_snapshot=_validate_fixture
        )
    assert path.read_bytes() == b"old-confirmed-checkpoint"
    assert list(path.parent.iterdir()) == [path]


def test_snapshot_index_probe_refusal_precedes_creation(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}")
    destination = tmp_path / "index.sqlite"
    with pytest.raises(OSError):
        stage_snapshot(source, destination, probe_directory=tmp_path / "missing")
    assert source.read_text() == "{}"
    assert not destination.exists()


def test_stream_writer_rechecks_backing_before_each_bounded_flush(
    tmp_path, monkeypatch
):
    probe = tmp_path / "probe"
    probe.mkdir()
    free = snapshot_stream.IO_CHUNK_BYTES * 2
    monkeypatch.setattr(
        "tgdrive.storage.capacity.shutil.disk_usage",
        lambda _: SimpleNamespace(total=free + 100, used=100, free=free),
    )
    stream = io.BytesIO()
    writer = snapshot_stream._Writer(stream, tmp_path, 0, None, probe_directory=probe)
    writer.write(b"a" * snapshot_stream.IO_CHUNK_BYTES)
    free = 0
    with pytest.raises(BackupError, match=r"capacity|space"):
        writer.write(b"b" * snapshot_stream.IO_CHUNK_BYTES)
    assert stream.getvalue() == b"a" * snapshot_stream.IO_CHUNK_BYTES


async def test_live_backup_captures_promised_upload_bytes_on_loop_before_worker(
    snapshot_source, monkeypatch
):
    import asyncio
    import threading

    previous, store, backend = snapshot_source
    previous.settings.min_free_bytes = 0
    loop_thread = threading.get_ident()
    captured = []

    def remaining():
        assert threading.get_ident() == loop_thread
        assert asyncio.current_task() is not None
        captured.append(True)
        return 1024**2

    service = MongoProtection(
        previous.settings, previous.pool, backend, remaining_staging_bytes=remaining
    )
    directory = service.settings.resolved_cache_dir / "mongo-backups"
    directory.mkdir(exist_ok=True)
    protected = directory / "confirmed.tgdb"
    protected.write_bytes(b"old-protected")
    monkeypatch.setattr(
        "tgdrive.storage.capacity.shutil.disk_usage",
        lambda _: SimpleNamespace(total=2 * 1024**2, used=1024**2, free=1024**2),
    )
    with pytest.raises(BackupError, match="temporary space"):
        await service.snapshot_now(store)
    assert captured == [True]
    assert protected.read_bytes() == b"old-protected"
    assert list(directory.iterdir()) == [protected]
    assert backend.db.backups.find_one({}) is None


def test_snapshot_index_counts_future_upload_commitments(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text("{}")
    monkeypatch.setattr(
        "tgdrive.storage.capacity.shutil.disk_usage",
        lambda _: SimpleNamespace(total=1024**2, used=0, free=1024**2),
    )
    with pytest.raises(OSError, match="temporary space"):
        stage_snapshot(source, tmp_path / "index.sqlite", outstanding_bytes=1024**2)
    assert source.read_text() == "{}"
    assert not (tmp_path / "index.sqlite").exists()
