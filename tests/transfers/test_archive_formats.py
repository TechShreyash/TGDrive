"""Real isolated decoders and durable publication for small archive fixtures."""

import asyncio
import bz2
import gzip
import io
import logging
import lzma
import tarfile
from compression import zstd
from contextlib import contextmanager
from zipfile import ZIP_BZIP2, ZIP_LZMA

import libarchive
import pytest

from tests.support.book_samples import cbr, png
from tests.support.mongo_fixtures import register_test_backend
from tests.transfers.test_archives import (
    dispose,
    finish,
    manager_for,
    source,
    zip_bytes,
)
from tgdrive.transfers import archive_worker, archives
from tgdrive.transfers.archive_worker import FORMATS, Manifest, WorkerError, rar_header
from tgdrive.transfers.tasks import TransferStatus


@pytest.fixture(autouse=True)
def mongo_backend(tmp_path, monkeypatch):
    return register_test_backend(monkeypatch, tmp_path)


def tar_bytes(entries=None):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w") as archive:
        root = tarfile.TarInfo("./")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, data in entries or [
            ("./nested/file.txt", b"hello"),
            ("./empty/", None),
        ]:
            entry = tarfile.TarInfo(name)
            if data is None:
                entry.type = tarfile.DIRTYPE
            else:
                entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data) if data is not None else None)
    return target.getvalue()


def seven_zip(tmp_path):
    path = tmp_path / "fixture.7z"
    member = tmp_path / "file.txt"
    member.write_bytes(b"hello")
    with libarchive.file_writer(str(path), "7zip") as archive:
        archive.add_files(str(member), pathname="nested/file.txt")
    return path.read_bytes()


# Tiny functional RAR5 fixture from libarchive's BSD-2-Clause tests:
# libarchive/test/test_read_format_rar5_stored.rar.uu (helloworld.txt).
RAR5 = bytes.fromhex(
    "526172211a0701003392b5e50a01050600050101808000383006632c0203"
    "0b9d00049d00a48302b443a0958000010e68656c6c6f776f726c642e7478"
    "740a03137e0eab5b56e90e1a68656c6c6f206c6962617263686976652074"
    "657374207375697465210a1d77565103050400"
)


@pytest.mark.parametrize(
    "suffix", [s for s in FORMATS if s not in {".zip", ".7z", ".rar"}]
)
async def test_tar_and_single_stream_formats(tmp_path, suffix):
    container, compression = FORMATS[suffix]
    data = tar_bytes() if container == "tar" else b"hello"
    data = {
        None: lambda value: value,
        "gzip": gzip.compress,
        "bzip2": bz2.compress,
        "xz": lzma.compress,
        "zstd": zstd.compress,
    }[compression](data)
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        file = source(manager, ledger, "result.txt" + suffix, data)
        job = await manager.start_extraction(
            path=manager.store.resolve_id(file["id"]),
            destination="/",
            folder_name="Unpacked",
        )
        await finish(job)
        assert job.status is TransferStatus.COMPLETED, job.error
        output = manager.store.list_directory(job.context["destination"])
        if container == "tar":
            assert {row["name"] for row in output.values()} == {"nested", "empty"}
            nested = next(row for row in output.values() if row["name"] == "nested")
            output = manager.store.list_directory(
                manager.store.resolve_id(nested["id"])
            )
        assert len(output) == 1
        result = manager.store.get_file(
            manager.store.resolve_id(next(iter(output.values()))["id"])
        )
        assert ledger.messages[result["message_id"]].content == b"hello"
        if container == "raw":
            assert result["name"] == "result.txt"
    finally:
        await dispose(manager)


@pytest.mark.parametrize("kind", ["7z", "rar4", "rar5", "zip-bzip2", "zip-lzma"])
async def test_native_archive_formats(tmp_path, kind):
    if kind == "7z":
        data, suffix = seven_zip(tmp_path), ".7z"
    elif kind.startswith("rar"):
        data, suffix = (cbr() if kind == "rar4" else RAR5), ".rar"
    else:
        data = zip_bytes(
            [("nested/file.txt", b"hello")],
            compression=ZIP_BZIP2 if kind == "zip-bzip2" else ZIP_LZMA,
        )
        suffix = ".zip"
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        file = source(manager, ledger, "source" + suffix, data)
        job = await manager.start_extraction(
            path=manager.store.resolve_id(file["id"]),
            destination="/",
            folder_name="Unpacked",
        )
        await finish(job)
        assert job.status is TransferStatus.COMPLETED, job.error
        uploaded = {}
        for entry in job.context["archive_plan"]["files"]:
            relative, _, name = entry["name"].rpartition("/")
            folder = job.context["archive_folders"][relative]
            output = next(
                row
                for row in manager.store.list_directory(folder).values()
                if row["name"] == name
            )
            file = manager.store.get_file(manager.store.resolve_id(output["id"]))
            uploaded[entry["name"]] = ledger.messages[file["message_id"]].content
        expected = {"nested/file.txt": b"hello"}
        if kind == "rar4":
            expected = {"1.png": png(1), "2.png": png(2)}
        elif kind == "rar5":
            expected = {"helloworld.txt": b"hello libarchive test suite!\n"}
        assert uploaded == expected
    finally:
        await dispose(manager)


@pytest.mark.parametrize(
    "kind",
    [
        "crc",
        "truncated",
        "trailing",
        "traversal",
        "symlink",
        "hardlink",
        "special",
        "empty",
        "duplicate",
    ],
)
async def test_failed_archives_never_publish_folders_or_files(tmp_path, kind):
    if kind in {"crc", "truncated", "trailing"}:
        data = bytearray(
            gzip.compress(
                tar_bytes() + (b"0" * (30 * 1024**2) if kind == "trailing" else b"")
            )
        )
        if kind == "crc":
            data[-8] ^= 1
        elif kind == "truncated":
            del data[-4:]
        data, suffix = bytes(data), ".tar.gz"
    elif kind in {"traversal", "empty", "duplicate"}:
        entries = {
            "traversal": [("../escape", b"hello")],
            "empty": [("empty", b"")],
            "duplicate": [("one", b"a"), ("ONE", b"b")],
        }[kind]
        data, suffix = tar_bytes(entries), ".tar"
    else:
        target = io.BytesIO()
        with tarfile.open(fileobj=target, mode="w") as archive:
            entry = tarfile.TarInfo("unsafe")
            entry.type = {
                "symlink": tarfile.SYMTYPE,
                "hardlink": tarfile.LNKTYPE,
                "special": tarfile.FIFOTYPE,
            }[kind]
            entry.linkname = "target"
            archive.addfile(entry)
        data, suffix = target.getvalue(), ".tar"
    manager, ledger, _ = await manager_for(tmp_path)
    try:
        file = source(manager, ledger, "source" + suffix, data)
        job = await manager.start_extraction(
            path=manager.store.resolve_id(file["id"]),
            destination="/",
            folder_name="Unpacked",
        )
        await finish(job)
        assert job.status is TransferStatus.ERROR, job.error
        assert {row["name"] for row in manager.store.list_directory("/").values()} == {
            "source" + suffix
        }
        assert not job.context.get("archive_extracted")
    finally:
        await dispose(manager)


async def test_unknown_expanded_size_obeys_shared_staging_budget(tmp_path):
    manager, ledger, _ = await manager_for(tmp_path)
    manager.settings.staging_budget_bytes = 1024
    try:
        file = source(manager, ledger, "large.txt.gz", gzip.compress(b"a" * 2048))
        job = await manager.start_extraction(
            path=manager.store.resolve_id(file["id"]), destination="/", folder_name=None
        )
        await finish(job)
        assert job.status is TransferStatus.ERROR
        assert "budget" in job.error
        assert len(manager.store.list_directory("/")) == 1
    finally:
        await dispose(manager)


async def test_raw_reservations_grow_in_chunks_and_release_unused_bytes(
    tmp_path, monkeypatch
):
    manager, ledger, _ = await manager_for(tmp_path)
    manager.settings.upload_limit = manager.settings.bot_upload_limit = 10 * 1024**2
    data = b"hello" * (2 * 1024**2 - 1)
    compressed = gzip.compress(data)
    reservations = []
    persist = manager.registry.persist

    async def remember(record):
        if record.phase == "extracting":
            reservations.append(record.context["staging_size"])
        return await persist(record)

    monkeypatch.setattr(manager.registry, "persist", remember)
    try:
        file = source(manager, ledger, "large.txt.gz", compressed)
        job = await manager.start_extraction(
            path=manager.store.resolve_id(file["id"]), destination="/", folder_name=None
        )
        await finish(job)
        assert job.status is TransferStatus.COMPLETED, job.error
        assert len(compressed) + 8 * 1024**2 in reservations
        assert manager.settings.staging_budget_bytes in reservations
        assert len(compressed) + len(data) in reservations
        assert len(set(reservations)) == 3  # Two growth chunks and final exact size.
        output = next(
            row
            for row in manager.store.list_directory("/").values()
            if row["name"] == "large.txt"
        )
        result = manager.store.get_file(manager.store.resolve_id(output["id"]))
        assert ledger.messages[result["message_id"]].content == data
        assert job.context["staging_size"] == len(compressed)
    finally:
        await dispose(manager)


async def test_worker_resource_failure_is_reaped_without_publishing(
    tmp_path, monkeypatch
):
    manager, ledger, _ = await manager_for(tmp_path)
    real_spawn = asyncio.create_subprocess_exec
    process = None

    async def limited(*args, **kwargs):
        nonlocal process
        # Exercise abrupt worker exit without allocating a large dictionary.
        script = (
            "import resource,sys; sys.stdin.buffer.readline(); "
            "resource.setrlimit(resource.RLIMIT_AS,(32*1024**2,32*1024**2)); "
            "bytearray(64*1024**2)"
        )
        process = await real_spawn(args[0], "-c", script, **kwargs)
        return process

    monkeypatch.setattr(archives.asyncio, "create_subprocess_exec", limited)
    try:
        file = source(manager, ledger, "source.tar", tar_bytes())
        job = await manager.start_extraction(
            path=manager.store.resolve_id(file["id"]),
            destination="/",
            folder_name="Unpacked",
        )
        await finish(job)
        assert job.status is TransferStatus.ERROR
        assert "resource limits" in job.error
        assert process is not None and process.returncode != 0
        assert len(manager.store.list_directory("/")) == 1
    finally:
        await dispose(manager)


def test_rar_rejects_volume_and_encryption_flags(tmp_path):
    path = tmp_path / "source.rar"
    for data in [
        b"Rar!\x1a\x07\x00\x00\x00\x73\x01\x00\x00\x00",
        b"Rar!\x1a\x07\x00\x00\x00\x73\x80\x00\x00\x00",
        RAR5[:16] + b"\x01" + RAR5[17:],
    ]:
        path.write_bytes(data)
        with pytest.raises(WorkerError, match=r"split-volume|Encrypted|Split-volume"):
            rar_header(path)


def test_manifest_bounds_depth_and_implicit_folders():
    with pytest.raises(WorkerError, match="64 components"):
        Manifest(1024, 1024).add(
            "/".join(["a"] * 65), directory=False, size=1, index=0, container="tar"
        )
    plan = Manifest(1024, 1024)
    with pytest.raises(WorkerError, match="1000"):
        for index in range(501):
            plan.add(
                f"folder{index}/data",
                directory=False,
                size=1,
                index=index,
                container="7zip",
            )


def test_native_warning_prevents_returning_a_publishable_manifest(
    tmp_path, monkeypatch
):
    path = tmp_path / "source.zip"
    path.write_bytes(zip_bytes([("file.txt", b"hello")]))
    reader = libarchive.file_reader

    @contextmanager
    def warned(*args, **kwargs):
        with reader(*args, **kwargs) as archive:
            yield archive
        # Native readers may only report a warning while closing the archive.
        logging.getLogger("libarchive").warning("damaged archive checksum")

    monkeypatch.setattr(libarchive, "file_reader", warned)
    monkeypatch.setattr(archive_worker, "send", lambda *args, **kwargs: None)
    with pytest.raises(WorkerError, match="warning: damaged archive checksum"):
        archive_worker.extract(
            {
                "source": str(path),
                "destination": str(tmp_path),
                "name": "source.zip",
                "limit": 1024,
                "budget": 1024**2,
            }
        )


async def test_decoder_killed_and_reaped_on_cancellation(tmp_path, monkeypatch):
    manager, ledger, _ = await manager_for(tmp_path)
    process = None
    real_spawn = asyncio.create_subprocess_exec
    waiting = asyncio.Event()

    async def spawn(*args, **kwargs):
        nonlocal process
        assert set(kwargs["env"]) <= {"PATH", "LANG", "PYTHONPATH", "LIBARCHIVE"}
        process = await real_spawn(*args, **kwargs)
        read = process.stdout.readline

        async def pause():
            line = await read()
            waiting.set()
            await asyncio.Event().wait()
            return line

        process.stdout.readline = pause
        return process

    monkeypatch.setattr(archives.asyncio, "create_subprocess_exec", spawn)
    try:
        file = source(manager, ledger, "source.tar", tar_bytes())
        job = await manager.start_extraction(
            path=manager.store.resolve_id(file["id"]),
            destination="/",
            folder_name="Unpacked",
        )
        await asyncio.wait_for(waiting.wait(), 5)
        await manager.cancel_job(job.id)
        await finish(job)
        assert process is not None and process.returncode is not None
        assert job.status is TransferStatus.CANCELLED
        assert len(manager.store.list_directory("/")) == 1
    finally:
        await dispose(manager)


async def test_double_cancellation_during_launch_reaps_the_acquired_worker(
    tmp_path, monkeypatch
):
    manager, ledger, _ = await manager_for(tmp_path)
    real_spawn = asyncio.create_subprocess_exec
    acquired, release = asyncio.Event(), asyncio.Event()
    process = None

    async def delayed(*args, **kwargs):
        nonlocal process
        process = await real_spawn(*args, **kwargs)
        acquired.set()
        await release.wait()
        return process

    monkeypatch.setattr(archives.asyncio, "create_subprocess_exec", delayed)
    try:
        file = source(manager, ledger, "source.tar", tar_bytes())
        job = await manager.start_extraction(
            path=manager.store.resolve_id(file["id"]),
            destination="/",
            folder_name="Unpacked",
        )
        await asyncio.wait_for(acquired.wait(), 5)
        job.cancel_event.set()
        job.task.cancel()
        await asyncio.sleep(0)
        job.task.cancel()
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(job.task, return_exceptions=True), 5)
        assert process is not None and process.returncode is not None
        assert len(manager.store.list_directory("/")) == 1
    finally:
        release.set()
        await dispose(manager)


async def test_double_cancellation_cannot_interrupt_decoder_cleanup():
    waiting, release = asyncio.Event(), asyncio.Event()
    sentinel = asyncio.create_task(asyncio.Event().wait())

    class Process:
        returncode = None
        killed = waited = 0

        def kill(self):
            self.killed += 1
            self.returncode = -9

        async def wait(self):
            waiting.set()
            await release.wait()
            self.waited += 1

    process = Process()
    cleanup = asyncio.create_task(archives._stop_decoder(process, sentinel))
    await waiting.wait()
    cleanup.cancel()
    await asyncio.sleep(0)
    cleanup.cancel()
    await asyncio.sleep(0)
    release.set()
    result = await asyncio.gather(cleanup, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert process.killed == process.waited == 1
    assert sentinel.cancelled()
