"""ZIP security and real transfer orchestration against an in-memory Telegram."""

import asyncio
import hashlib
import io
import stat
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZIP_DEFLATED, ZIP_LZMA, ZIP_STORED, ZipFile, ZipInfo

import pytest

from tests.support.mongo_fixtures import register_test_backend
from tests.transfers.test_multipart_transfers import Account, Ledger, registry_for
from tgdrive.storage.demo_files import DemoFilesReadOnly
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.transfers import archives
from tgdrive.transfers.archives import ArchiveError, archive_members, zip_manifest
from tgdrive.transfers.manager import TransferManager
from tgdrive.transfers.staging import staging_usage
from tgdrive.transfers.tasks import TransferStatus
from tgdrive.transfers.transfer_locations import locate_transfer


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path)


def zip_bytes(entries, *, compression=ZIP_DEFLATED):
    target = io.BytesIO()
    with ZipFile(target, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return target.getvalue()


def write_zip(tmp_path, entries, **kwargs):
    path = tmp_path / "source.zip"
    path.write_bytes(zip_bytes(entries, **kwargs))
    return path


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "C:/drive",
        "a\\escape",
        "a//b",
        "a/./b",
        "a/../b",
        " a ",
        "a\x00b",
    ],
)
def test_reject_unsafe_zip_paths(tmp_path, name):
    path = write_zip(tmp_path, [(name.replace("\x00", "_"), b"payload")])
    if "\x00" in name:
        path.write_bytes(path.read_bytes().replace(b"a_b", b"a\x00b"))
    with pytest.raises(ArchiveError):
        zip_manifest(path, 1024, 1024**2)


@pytest.mark.parametrize(
    "entries",
    [
        [("one.txt", b"a"), ("ONE.txt", b"b")],
        [("a", b"a"), ("a/b", b"b")],
        [("A/b", b"a"), ("a/c", b"b")],
        [("cafe\u0301", b"a"), ("caf\u00e9", b"b")],
        [("empty.txt", b"")],
        [("folder/", b"hidden bytes")],
    ],
)
def test_reject_conflicting_or_unrepresentable_entries(tmp_path, entries):
    path = write_zip(tmp_path, entries)
    with pytest.raises(ArchiveError):
        zip_manifest(path, 1024, 1024**2)


def test_reject_symlinks_and_unsupported_compression(tmp_path):
    entry = ZipInfo("link")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = write_zip(tmp_path, [(entry, b"target")])
    with pytest.raises(ArchiveError, match="symlinks"):
        zip_manifest(path, 1024, 1024**2)
    path = write_zip(tmp_path, [("file", b"content")], compression=ZIP_LZMA)
    assert zip_manifest(path, 1024, 1024**2)["size"] == 7
    data = bytearray(path.read_bytes())
    struct.pack_into("<H", data, data.index(b"PK\x01\x02") + 10, 99)
    path.write_bytes(data)
    with pytest.raises(ArchiveError, match="Deflate"):
        zip_manifest(path, 1024, 1024**2)


def test_manifest_limits_precede_zipfile_allocation(tmp_path, monkeypatch):
    path = write_zip(tmp_path, [(f"{index}.txt", b"a") for index in range(1001)])
    data = bytearray(path.read_bytes())
    footer = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, footer + 8, 1, 1)
    path.write_bytes(data)
    monkeypatch.setattr(
        archives, "ZipFile", lambda *_: pytest.fail("allocated unbounded ZIP entries")
    )
    with pytest.raises(ArchiveError, match="directory"):
        zip_manifest(path, 1024, 1024**2)


def test_manifest_preserves_empty_directories_and_bounds_output(tmp_path):
    path = write_zip(
        tmp_path, [("folder/", b""), ("folder/deep/data", b"hello"), ("empty/", b"")]
    )
    plan = zip_manifest(path, 1024, 1024**2)
    assert plan["size"] == 5
    assert plan["folders"] == [("empty",), ("folder",), ("folder", "deep")]
    with pytest.raises(ArchiveError, match="upload limit"):
        zip_manifest(path, 4, 1024**2)
    with pytest.raises(ArchiveError, match="staging budget"):
        zip_manifest(path, 1024, path.stat().st_size + 4)


class MemoryTelegram(Account):
    def __init__(self, ledger):
        super().__init__("archive-test", ledger)
        self.read_started = asyncio.Event()
        self.read_gate = None

    async def stream_media(self, message, *, offset, limit):
        self.read_started.set()
        if self.read_gate is not None:
            await self.read_gate.wait()
        for index in range(offset, offset + limit):
            data = message.content[index * 1024**2 : (index + 1) * 1024**2]
            if not data:
                break
            yield data


async def manager_for(tmp_path, *, store=None, ledger=None, registry=None):
    ledger = ledger or Ledger()
    client = MemoryTelegram(ledger)
    pool = ClientPool([client])
    await pool.start()
    settings = SimpleNamespace(
        resolved_cache_dir=tmp_path / "cache",
        max_concurrent_transfers=1,
        storage_channel=-100123,
        bot_upload_limit=4 * 1024**2,
        upload_limit=8 * 1024**2,
        staging_budget_bytes=16 * 1024**2,
        min_free_bytes=0,
    )
    manager = TransferManager(
        settings=settings,
        pool=pool,
        store=store or DriveStore.create(tmp_path / "drive.sqlite3"),
        registry=registry or registry_for(tmp_path),
        remote=SimpleNamespace(close=AsyncMock()),
    )
    return manager, ledger, client


def source(manager, ledger, name, content, parent="/", *, checksum=False):
    ledger.next_id += 1
    identity = ledger.next_id
    ledger.messages[identity] = SimpleNamespace(
        id=identity,
        document=SimpleNamespace(file_size=len(content)),
        content=content,
    )
    return manager.store.add_file(
        parent,
        name,
        identity,
        len(content),
        sha256=hashlib.sha256(content).hexdigest() if checksum else None,
    )


async def finish(job):
    await asyncio.wait_for(asyncio.shield(job.task), 10)


async def dispose(manager):
    await manager.close()
    await manager.pool.stop()
    manager.store.close()


async def test_create_zip_preserves_tree_and_reduces_overlapping_selection(tmp_path):
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        folder = manager.store.create_folder("/", "Project")
        folder_path = manager.store.resolve_id(folder["id"])
        manager.store.create_folder(folder_path, "Empty")
        file = source(
            manager, ledger, "notes.txt", b"hello", folder_path, checksum=True
        )
        extra = source(manager, ledger, "other.bin", b"world")
        paths = [
            folder_path,
            manager.store.resolve_id(file["id"]),
            manager.store.resolve_id(extra["id"]),
        ]
        assert len(archive_members(manager.store, paths)) == 4
        job = await manager.start_archive(paths=paths, destination="/", name="Bundle")
        await finish(job)
        assert job.status is TransferStatus.COMPLETED, job.error
        output = next(
            row
            for row in manager.store.list_directory("/").values()
            if row["name"] == "Bundle.zip"
        )
        output = manager.store.get_file(manager.store.resolve_id(output["id"]))
        with ZipFile(
            io.BytesIO(ledger.messages[output["message_id"]].content)
        ) as archive:
            assert set(archive.namelist()) == {
                "Project/",
                "Project/Empty/",
                "Project/notes.txt",
                "other.bin",
            }
            assert archive.read("Project/notes.txt") == b"hello"
            assert all(row.compress_type == ZIP_STORED for row in archive.infolist())
        assert manager.store.get_file(manager.store.resolve_id(file["id"]))["size"] == 5
        location = locate_transfer(
            manager.store, {"id": job.id, "kind": "archive", "status": "completed"}
        )
        assert location["item_id"] == output["id"]
    finally:
        await dispose(manager)


@pytest.mark.parametrize("folder_name", [None, "Unpacked"])
async def test_extract_keeps_both_and_hides_child_jobs(tmp_path, folder_name):
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        original = source(manager, ledger, "one.txt", b"original")
        manager.store.create_folder("/", "folder")
        payload = zip_bytes(
            [("one.txt", b"new"), ("folder/two.txt", b"two"), ("folder/empty/", b"")]
        )
        zip_file = source(manager, ledger, "source.zip", payload, checksum=True)
        job = await manager.start_extraction(
            path=manager.store.resolve_id(zip_file["id"]),
            destination="/",
            folder_name=folder_name,
        )
        await finish(job)
        assert job.status is TransferStatus.COMPLETED, job.error
        destination = job.context["destination"]
        rows = manager.store.list_directory(destination)
        assert {row["name"] for row in rows.values()} >= (
            {"one (1).txt", "folder (1)"}
            if folder_name is None
            else {"one.txt", "folder"}
        )
        expected_folder = "folder (1)" if folder_name is None else "folder"
        folder = next(row for row in rows.values() if row["name"] == expected_folder)
        assert {
            row["name"]
            for row in manager.store.list_directory(
                manager.store.resolve_id(folder["id"])
            ).values()
        } == {"two.txt", "empty"}
        assert (
            manager.store.get_file(manager.store.resolve_id(original["id"]))["size"]
            == 8
        )
        children = [
            row
            for row in manager.registry.list()
            if row.context.get("archive_parent_id") == job.id
        ]
        assert len(children) == 2 and all(child.history_hidden for child in children)
        location = locate_transfer(
            manager.store,
            {
                "id": job.id,
                "kind": "extract",
                "status": "completed",
                "destination": destination,
            },
        )
        assert location["available"] and location["folder_path"] == destination
    finally:
        await dispose(manager)


async def test_corrupt_zip_import_changes_no_catalog_items(tmp_path):
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        content = bytearray(
            zip_bytes([("folder/file", b"original")], compression=ZIP_STORED)
        )
        content[content.index(b"original")] ^= 1
        item = source(manager, ledger, "broken.zip", bytes(content))
        before = manager.store.list_directory("/")
        job = await manager.start_extraction(
            path=manager.store.resolve_id(item["id"]),
            destination="/",
            folder_name="Output",
        )
        await finish(job)
        assert job.status is TransferStatus.ERROR
        # Some libarchive versions hit the size guard before reporting the CRC.
        assert "CRC" in job.error or job.error == (
            "An archive entry exceeded its declared size or upload limit."
        )
        assert manager.store.list_directory("/") == before
        assert not ledger.calls
    finally:
        await dispose(manager)


async def test_cancel_drains_source_read_and_removes_only_workspace(tmp_path):
    manager, ledger, client = await manager_for(tmp_path)
    try:
        item = source(manager, ledger, "source.zip", zip_bytes([("file", b"data")]))
        client.read_gate = asyncio.Event()
        job = await manager.start_extraction(
            path=manager.store.resolve_id(item["id"]), destination="/", folder_name=None
        )
        await asyncio.wait_for(client.read_started.wait(), 3)
        await manager.cancel_job(job.id)
        client.read_gate.set()
        await finish(job)
        if manager.registry._finalizers:
            await asyncio.gather(*tuple(manager.registry._finalizers))
        assert job.status is TransferStatus.CANCELLED
        assert not (manager.cache_dir / "archives" / job.id).exists()
        assert manager.store.get_file(manager.store.resolve_id(item["id"]))
    finally:
        await dispose(manager)


async def test_archive_retry_reuses_dispatched_upload_after_restart(tmp_path):
    manager, ledger, _ = await manager_for(tmp_path)
    file = source(manager, ledger, "file.txt", b"content")
    ledger.lose_once = True
    job = await manager.start_archive(
        paths=[manager.store.resolve_id(file["id"])],
        destination="/",
        name="archive.zip",
    )
    await finish(job)
    assert job.status is TransferStatus.ERROR
    first_requests = set(ledger.requests)
    await manager.close()
    await manager.pool.stop()
    store = manager.store
    recovered, _, _ = await manager_for(
        tmp_path, store=store, ledger=ledger, registry=registry_for(tmp_path)
    )
    try:
        await recovered.recover()
        job = recovered.registry.get(job.id)
        await finish(job)
        assert job.status is TransferStatus.COMPLETED, job.error
        assert set(ledger.requests) == first_requests
        assert (
            len(
                [
                    row
                    for row in store.list_directory("/").values()
                    if row["name"] == "archive.zip"
                ]
            )
            == 1
        )
    finally:
        await dispose(recovered)


async def test_extract_parent_owns_child_staging_once(tmp_path):
    manager, _, _ = await manager_for(tmp_path)
    try:
        directory = manager.cache_dir / "archives" / "parent"
        directory.mkdir(parents=True)
        file = directory / "0.file"
        file.write_bytes(b"abc")
        parent = await manager.registry.create(
            "extracting",
            context=dict(
                kind="extract",
                staging_size=10,
                staging_written=3,
                staging_directory=str(directory),
            ),
        )
        await manager.registry.create(
            "queued",
            context=dict(
                kind="upload",
                archive_parent_id=parent.id,
                local_path=str(file),
                staging_size=3,
            ),
        )
        assert staging_usage(manager) == (10, 7)
    finally:
        await dispose(manager)


async def test_archive_reads_multipart_sources_and_checks_their_hashes(tmp_path):
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        parts, offset = [], 0
        for index, payload in enumerate([b"first", b"second"]):
            row = source(manager, ledger, f"part-{index}", payload)
            parts.append(
                dict(
                    index=index,
                    offset=offset,
                    size=len(payload),
                    message_id=row["message_id"],
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
            offset += len(payload)
        logical = manager.store.add_file(
            "/",
            "logical.bin",
            parts[0]["message_id"],
            offset,
            parts=parts,
            sha256=hashlib.sha256(b"firstsecond").hexdigest(),
        )
        job = await manager.start_archive(
            paths=[manager.store.resolve_id(logical["id"])],
            destination="/",
            name="joined.zip",
        )
        await finish(job)
        assert job.status is TransferStatus.COMPLETED, job.error
        result = manager.store.transfer_location(job.id, completed=True)
        output = manager.store.get_file(manager.store.resolve_id(result["item_id"]))
        with ZipFile(
            io.BytesIO(ledger.messages[output["message_id"]].content)
        ) as archive:
            assert archive.read("logical.bin") == b"firstsecond"
    finally:
        await dispose(manager)


async def test_source_move_follows_identity_and_wrong_checksum_cannot_upload(tmp_path):
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        folder = manager.store.create_folder("/", "Folder")
        item = source(manager, ledger, "source.txt", b"source", checksum=True)
        members = archive_members(manager.store, [manager.store.resolve_id(item["id"])])
        manager.store.move_many(
            [manager.store.resolve_id(item["id"])],
            manager.store.resolve_id(folder["id"]),
        )
        assert archives._source(manager.store, members[0])["id"] == item["id"]
        ledger.messages[item["message_id"]].content = b"change"
        job = await manager.start_archive(
            paths=[manager.store.resolve_id(item["id"])],
            destination="/",
            name="bad.zip",
        )
        await finish(job)
        assert job.status is TransferStatus.ERROR and "checksum" in job.error
        assert not ledger.calls
    finally:
        await dispose(manager)


async def test_cancel_after_ambiguous_zip_send_cleans_uncommitted_telegram_parts(
    tmp_path,
):
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        item = source(manager, ledger, "source.txt", b"payload")
        ledger.lose_once = True
        job = await manager.start_archive(
            paths=[manager.store.resolve_id(item["id"])],
            destination="/",
            name="cancel.zip",
        )
        await finish(job)
        assert job.status is TransferStatus.ERROR and job.context["send_started"]
        assert len(ledger.messages) == 2
        await manager.cancel_job(job.id)
        if job.task is not None:
            await finish(job)
        if manager.registry._finalizers:
            await asyncio.gather(*tuple(manager.registry._finalizers))
        assert job.status is TransferStatus.CANCELLED
        assert len(ledger.messages) == 1
        assert manager.store.get_file(manager.store.resolve_id(item["id"]))
        assert not (manager.cache_dir / "archives" / job.id).exists()
    finally:
        await dispose(manager)


async def test_extract_retry_reuses_completed_child_without_staged_bytes(tmp_path):
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        item = source(manager, ledger, "source.zip", zip_bytes([("file", b"data")]))
        job = await manager.start_extraction(
            path=manager.store.resolve_id(item["id"]),
            destination="/",
            folder_name="Output",
        )
        await finish(job)
        assert job.status is TransferStatus.COMPLETED, job.error
        # Model interruption after child publication, before parent completion.
        job.context["archive_completed"] = []
        job.status = TransferStatus.ERROR
        job.phase = "interrupted"
        await manager.registry.persist(job)
        calls = len(ledger.calls)
        await manager.retry_job(job.id)
        await finish(job)
        assert job.status is TransferStatus.COMPLETED, job.error
        assert len(ledger.calls) == calls
        assert len(manager.store.list_directory(job.context["destination"])) == 1
    finally:
        await dispose(manager)


async def test_resource_open_cancel_closes_handle_after_worker_drains():
    import threading

    acquired, release = threading.Event(), threading.Event()
    resource = io.BytesIO(b"data")

    def opening():
        acquired.set()
        release.wait(3)
        return resource

    task = asyncio.create_task(archives._open(opening))
    await asyncio.to_thread(acquired.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert resource.closed


async def test_create_rejects_oversized_directory_before_dispatch(
    tmp_path, monkeypatch
):
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        item = source(manager, ledger, "source.txt", b"data")
        monkeypatch.setattr(archives, "MAX_DIRECTORY_BYTES", 80)
        with pytest.raises(ArchiveError, match="directory limit"):
            await manager.start_archive(
                paths=[manager.store.resolve_id(item["id"])],
                destination="/",
                name="out.zip",
            )
        assert not manager.registry.list()
        assert not ledger.calls
    finally:
        await dispose(manager)


def test_expanded_manifest_is_bounded_before_catalog_writes(tmp_path, monkeypatch):
    path = write_zip(tmp_path, [("folder/deep/\u00e9.txt", b"payload")])
    plan = zip_manifest(path, 1024, 1024**2)
    encoded = archives.json.dumps(
        plan, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    monkeypatch.setattr(archives, "MAX_MANIFEST_BYTES", len(encoded) - 1)
    with pytest.raises(ArchiveError, match="4 MiB job metadata"):
        zip_manifest(path, 1024, 1024**2)


async def test_folder_worker_restores_demo_write_policy(tmp_path):
    manager, _, _ = await manager_for(tmp_path)
    try:
        protected = manager.store.ensure_demo_files()
        destination = manager.store.resolve_id(protected["id"])
        before = manager.store.get_directory(destination)
        revision = manager.store.revision
        record = SimpleNamespace(
            id="restricted_archive_job",
            context={
                "demo_read_only": True,
                "folder_name": "Extracted",
                "archive_plan": {"folders": []},
            },
        )
        with pytest.raises(DemoFilesReadOnly):
            await archives._worker(
                archives._prepare_folders, manager, record, destination
            )
        assert manager.store.get_directory(destination) == before
        assert manager.store.revision == revision
    finally:
        await dispose(manager)
