"""Bounded archive jobs using the normal durable Telegram upload pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import struct
import sys
from contextlib import suppress
from pathlib import Path
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_LZMA, ZIP_STORED, BadZipFile, ZipFile

from tgdrive.core.errors import InvalidNameError, NotDirectoryError, ValidationError
from tgdrive.media.logical_media import PartLease
from tgdrive.media.ranges import ByteRange
from tgdrive.storage.demo_files import write_policy
from tgdrive.storage.store import _name_key, _normalize_name, _normalize_path
from tgdrive.transfers.archive_worker import Manifest, WorkerError, archive_format
from tgdrive.transfers.manager import BackupPending, TransferCancelled, TransferError
from tgdrive.transfers.staging import check_staging
from tgdrive.transfers.tasks import TransferStatus, _journalled
from tgdrive.transfers.uploads import ResumableUploads

MAX_ENTRIES = 1000
MAX_DEPTH = 64
MAX_DIRECTORY_BYTES = 8 * 1024**2
MAX_MANIFEST_BYTES = 4 * 1024**2
BLOCK = 1024**2
_worker = ResumableUploads._owned_worker


class ArchiveError(ValidationError):
    pass


async def _open(callback, *args):
    resource = None

    def acquire():
        nonlocal resource
        resource = callback(*args)
        return resource

    try:
        return await _worker(acquire)
    except BaseException:
        if resource is not None:
            await _worker(resource.close)
        raise


def public_destination(store, path):
    return any(
        path == row["path"] or path.startswith(row["path"].rstrip("/") + "/")
        for row in store.list_shares()
    )


def _signature(file):
    media = {key: file.get(key) for key in ("message_id", "size", "sha256", "parts")}
    return hashlib.sha256(json.dumps(media, sort_keys=True).encode()).hexdigest()


def _source(store, member):
    file = store.get_file(store.resolve_id(member["id"]))
    if _signature(file) != member["signature"]:
        raise ArchiveError("An archive source changed. Start a new archive job.")
    return file


def _member(file):
    return {"id": file["id"], "size": file["size"], "signature": _signature(file)}


def _available_name(name, used, *, folder=False):
    suffix = "" if folder else Path(name).suffix
    stem = name[: -len(suffix)] if suffix else name
    candidate, index = name, 1
    while _name_key(candidate) in used:
        label = f" ({index})"
        ending = label + suffix[: 255 - len(label) - 1]
        candidate = stem[: 255 - len(ending)] + ending
        index += 1
    used.add(_name_key(candidate))
    return candidate


def archive_members(store, paths):
    selected = sorted(set(_normalize_path(path)[1] for path in paths), key=len)
    selected = [
        path
        for path in selected
        if not any(
            path.startswith(parent.rstrip("/") + "/")
            for parent in selected
            if parent != path
        )
    ]
    if "/" in selected:
        raise ArchiveError("Select files or folders inside My Files to create a ZIP.")
    result, used = [], set()

    def visit(path, prefix, root=False):
        try:
            node = store.get_directory(path)
        except NotDirectoryError:
            node = store.get_file(path)
        name = (
            _available_name(node["name"], used, folder=node["type"] == "folder")
            if root
            else node["name"]
        )
        relative = (*prefix, name)
        if len(relative) > MAX_DEPTH or len("/".join(relative)) > 4096:
            raise ArchiveError(
                "ZIP paths cannot exceed 64 components or 4096 characters."
            )
        entry = {"name": "/".join(relative), "type": node["type"], "id": node["id"]}
        if node["type"] == "file":
            entry.update(_member(node))
        result.append(entry)
        if len(result) > MAX_ENTRIES:
            raise ArchiveError("A ZIP job supports at most 1000 files and folders.")
        if node["type"] == "folder":
            for child in node["contents"].values():
                visit(store.resolve_id(child["id"]), relative)

    with store.metadata_batch():
        for path in selected:
            visit(path, (), True)
    return result


def _zip_directory(source):
    """Check the real central-directory count before ZipFile allocates entries."""
    size = source.seek(0, 2)
    tail_size = min(size, 65557)
    source.seek(size - tail_size)
    tail = source.read(tail_size)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        raise ArchiveError("This is not a readable ZIP archive.")
    _, disk, cd_disk, disk_count, count, length, offset, comment = struct.unpack_from(
        "<4s4H2LH", tail, at
    )
    if at + 22 + comment != len(tail) or disk or cd_disk or disk_count != count:
        raise ArchiveError("Split-volume ZIP archives are unsupported.")
    end = size - tail_size + at
    if count == 65535 or length == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        if end < 20:
            raise ArchiveError("Invalid ZIP64 archive.")
        source.seek(end - 20)
        signature, disk, zip64_at, disks = struct.unpack("<4sLQL", source.read(20))
        if signature != b"PK\x06\x07" or disk or disks != 1 or zip64_at + 56 > end - 20:
            raise ArchiveError("Invalid or split-volume ZIP64 archive.")
        source.seek(zip64_at)
        record = struct.unpack("<4sQ2H2L4Q", source.read(56))
        if (
            record[0] != b"PK\x06\x06"
            or record[4]
            or record[5]
            or record[6] != record[7]
        ):
            raise ArchiveError("Invalid ZIP64 directory.")
        count, length, offset = record[7:10]
        end = zip64_at
    if count > MAX_ENTRIES or length > MAX_DIRECTORY_BYTES or offset + length > end:
        raise ArchiveError(
            "ZIP archives support at most 1000 entries and an 8 MiB directory."
        )
    source.seek(offset)
    directory = source.read(length)
    position = actual = 0
    while position < len(directory):
        if directory[position : position + 4] != b"PK\x01\x02" or position + 46 > len(
            directory
        ):
            raise ArchiveError("Invalid ZIP central directory.")
        name, extra, comment = struct.unpack_from("<3H", directory, position + 28)
        position += 46 + name + extra + comment
        actual += 1
        if actual > MAX_ENTRIES or position > len(directory):
            raise ArchiveError("ZIP directory exceeds its declared limits.")
    if actual != count:
        raise ArchiveError("ZIP directory entry count is inconsistent.")


def zip_manifest(path, limit, budget):
    entries, folders, explicit, files = [], {}, set(), set()
    total = 0
    try:
        with Path(path).open("rb") as source:
            _zip_directory(source)
            with ZipFile(source) as archive:
                for index, entry in enumerate(archive.infolist()):
                    raw = entry.orig_filename
                    if (
                        raw != entry.filename
                        or "\\" in raw
                        or raw.startswith("/")
                        or re.match(r"^[A-Za-z]:", raw)
                    ):
                        raise ArchiveError("ZIP contains an unsafe entry path.")
                    parts = raw[:-1].split("/") if entry.is_dir() else raw.split("/")
                    if not parts or len(parts) > MAX_DEPTH or len(raw) > 4096:
                        raise ArchiveError(
                            "ZIP paths cannot exceed 64 components or 4096 characters."
                        )
                    try:
                        parts = tuple(_normalize_name(part) for part in parts)
                    except InvalidNameError as exc:
                        raise ArchiveError(
                            "ZIP contains an unsafe entry name."
                        ) from exc
                    key = tuple(_name_key(part) for part in parts)
                    mode = stat.S_IFMT(entry.external_attr >> 16)
                    if mode not in {
                        0,
                        stat.S_IFDIR if entry.is_dir() else stat.S_IFREG,
                    }:
                        raise ArchiveError(
                            "ZIP symlinks and special files are unsupported."
                        )
                    if entry.flag_bits & 1 or entry.compress_type not in {
                        ZIP_STORED,
                        ZIP_DEFLATED,
                        ZIP_BZIP2,
                        ZIP_LZMA,
                    }:
                        raise ArchiveError(
                            "Use an unencrypted ZIP with Stored, Deflate, "
                            "BZIP2 or LZMA compression."
                        )
                    if (
                        key in explicit
                        or (not entry.is_dir() and key in folders)
                        or key in files
                    ):
                        raise ArchiveError(
                            "ZIP contains duplicate or conflicting entry paths."
                        )
                    explicit.add(key)
                    for depth in range(1, len(parts)):
                        parent = key[:depth]
                        if parent in files or (
                            parent in folders and folders[parent] != parts[:depth]
                        ):
                            raise ArchiveError(
                                "ZIP contains duplicate or conflicting folder names."
                            )
                        folders[parent] = parts[:depth]
                    if entry.is_dir():
                        if entry.file_size or (
                            key in folders and folders[key] != parts
                        ):
                            raise ArchiveError(
                                "ZIP directory entries must be empty and unambiguous."
                            )
                        folders[key] = parts
                        continue
                    if entry.file_size <= 0:
                        raise ArchiveError(
                            "TGDrive cannot extract ZIP archives "
                            "containing empty files."
                        )
                    if entry.file_size > limit:
                        raise ArchiveError(
                            "An extracted file exceeds the configured upload limit."
                        )
                    files.add(key)
                    total += entry.file_size
                    if total + Path(path).stat().st_size > budget:
                        raise ArchiveError(
                            "ZIP and extracted files exceed the staging budget."
                        )
                    entries.append(
                        {
                            "name": "/".join(parts),
                            "size": entry.file_size,
                            "index": index,
                        }
                    )
                if (
                    len(entries) + len(folders) > MAX_ENTRIES
                    or len(folders) >= MAX_ENTRIES
                ):
                    raise ArchiveError(
                        "A ZIP job supports at most 1000 files and folders "
                        "including implicit folders."
                    )
    except (BadZipFile, EOFError, struct.error) as exc:
        raise ArchiveError("This ZIP archive is damaged or incomplete.") from exc
    plan = {
        "files": entries,
        "folders": sorted(folders.values(), key=lambda p: (len(p), p)),
        "size": total,
    }
    if (
        len(json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        > MAX_MANIFEST_BYTES
    ):
        raise ArchiveError(
            "The expanded ZIP manifest exceeds the 4 MiB job metadata limit."
        )
    return plan


def _directory(manager, record):
    directory = manager.cache_dir / "archives" / record.id
    if not directory.resolve().is_relative_to(
        (manager.cache_dir / "archives").resolve()
    ):
        raise ArchiveError("Invalid archive workspace.")
    return directory


async def _destination(manager, record):
    path = await manager._resolve_upload_destination(
        record, record.context["destination"]
    )
    if not record.context.get("allow_public_exposure") and public_destination(
        manager.store, path
    ):
        raise ArchiveError(
            "The destination is now publicly shared. "
            "Start again and confirm public access."
        )
    return path


async def _bytes(manager, record, member):
    async def authorize():
        if record.cancel_event.is_set():
            raise TransferCancelled("Transfer cancelled")
        _source(manager.store, member)

    file = _source(manager.store, member)
    digest, length = hashlib.sha256(), 0
    parts = file.get("parts") or [
        {"message_id": file["message_id"], "size": file["size"]}
    ]
    for part in parts:
        if part["size"] == 0:
            continue
        lease = await PartLease(
            manager.pool,
            manager.settings.storage_channel,
            part,
            background=True,
            authorize=authorize,
        ).open()
        stream = lease.read(
            ByteRange(0, part["size"] - 1, part["size"], is_partial=False)
        )
        try:
            async for chunk in stream:
                if record.cancel_event.is_set():
                    raise TransferCancelled("Transfer cancelled")
                digest.update(chunk)
                length += len(chunk)
                if length > file["size"]:
                    raise ArchiveError("An archive source exceeded its declared size.")
                yield chunk
        finally:
            try:
                await stream.aclose()
            finally:
                await lease.close()
    if length != file["size"] or (
        file.get("sha256") and digest.hexdigest() != file["sha256"]
    ):
        raise ArchiveError(
            "An archive source failed its size or checksum verification."
        )


def _sync(file):
    file.flush()
    os.fsync(file.fileno())


def _sync_path(path):
    with path.open("rb+") as file:
        _sync(file)


@_journalled
async def start_archive(
    manager, paths, destination, name, *, allow_public_exposure=False
):
    name = _normalize_name(name)
    if not name.lower().endswith(".zip"):
        name = _normalize_name(name + ".zip")
    if manager._check_upload_allowed is not None:
        manager._check_upload_allowed()
    binding = manager.store.upload_destination(destination)
    members = await _worker(archive_members, manager.store, paths)
    if (
        sum(80 + len(member["name"].encode("utf-8")) for member in members)
        > MAX_DIRECTORY_BYTES
    ):
        raise ArchiveError("The selected ZIP paths exceed the 8 MiB directory limit.")
    total = sum(member.get("size", 0) for member in members)
    bound = (
        total
        + sum(256 + 2 * len(member["name"].encode("utf-8")) for member in members)
        + 256
    )
    if bound > manager.settings.upload_limit:
        raise ArchiveError(
            "The selected files and ZIP headers exceed the configured upload limit."
        )
    check_staging(manager.settings, manager, bound)
    identity = secrets.token_urlsafe(18)
    context = dict(
        kind="archive",
        source_kind="archive",
        name=name,
        members=members,
        destination=binding["path"],
        destination_id=binding["id"],
        staging_size=bound,
        staging_written=0,
        allow_public_exposure=allow_public_exposure,
    )
    manager._pending_staging[identity] = bound
    try:
        record = await manager.registry.create(
            "archiving", total=total, transfer_id=identity, context=context
        )
    finally:
        manager._pending_staging.pop(identity, None)
    spawn_archive(manager, record)
    return record


@_journalled
async def start_extraction(
    manager, path, destination, folder_name, *, allow_public_exposure=False
):
    if manager._check_upload_allowed is not None:
        manager._check_upload_allowed()
    file = manager.store.get_file(path)
    try:
        archive_format(file["name"])
    except ValueError as error:
        raise ArchiveError(str(error)) from None
    if folder_name is not None:
        folder_name = _normalize_name(folder_name)
    binding = manager.store.upload_destination(destination)
    check_staging(manager.settings, manager, file["size"])
    identity = secrets.token_urlsafe(18)
    context = dict(
        kind="extract",
        source_kind="extract",
        name=file["name"],
        archive_source=_member(file),
        folder_name=folder_name,
        destination=binding["path"],
        destination_id=binding["id"],
        staging_size=file["size"],
        staging_written=0,
        allow_public_exposure=allow_public_exposure,
    )
    manager._pending_staging[identity] = file["size"]
    try:
        record = await manager.registry.create(
            "downloading", total=file["size"], transfer_id=identity, context=context
        )
    finally:
        manager._pending_staging.pop(identity, None)
    spawn_archive(manager, record)
    return record


def spawn_archive(manager, record):
    task = manager.registry.spawn(record.id, lambda item: run_archive(manager, item))
    task.add_done_callback(lambda _: discard_stage(manager, record))
    return task


async def _create_zip(manager, record, directory):
    context = record.context
    path = directory / "archive.zip"
    context.update(local_path=str(path), staging_directory=str(directory))
    await manager.registry.persist(record)
    if context.get("archive_ready") and not path.exists():
        context.pop("archive_ready")
    if not context.get("archive_ready"):
        async with manager._semaphore:
            context["staging_written"] = 0
            await manager.registry.update(
                record.id, status=TransferStatus.RUNNING, phase="archiving", current=0
            )
            archive = await _open(lambda: ZipFile(path, "w", compression=ZIP_STORED))
            current = 0
            try:
                for member in context["members"]:
                    if record.cancel_event.is_set():
                        raise TransferCancelled("Transfer cancelled")
                    if member["type"] == "folder":
                        manager.store.get_directory(
                            manager.store.resolve_id(member["id"])
                        )
                        await _worker(archive.writestr, member["name"] + "/", b"")
                        continue
                    output = await _open(
                        lambda name=member["name"]: archive.open(
                            name, "w", force_zip64=True
                        )
                    )
                    stream = _bytes(manager, record, member)
                    try:
                        async for chunk in stream:
                            check_staging(
                                manager.settings,
                                manager,
                                context["staging_size"],
                                exclude=record.id,
                                written=context["staging_written"],
                            )
                            await _worker(output.write, chunk)
                            current += len(chunk)
                            context["staging_written"] = archive.fp.tell()
                            await manager.registry.update(
                                record.id, current=current, checkpoint_progress=False
                            )
                    finally:
                        try:
                            await stream.aclose()
                        finally:
                            await _worker(output.close)
            finally:
                await _worker(archive.close)
            await _worker(_sync_path, path)
            context.update(
                archive_ready=True,
                size=path.stat().st_size,
                staging_written=path.stat().st_size,
            )
            await manager.registry.persist(record)
    if path.stat().st_size > manager.settings.upload_limit:
        raise ArchiveError("The ZIP exceeds the configured upload limit.")
    await manager._upload_path(
        record,
        path,
        await _destination(manager, record),
        context["name"],
        context["size"],
    )


async def _extract_files(manager, record, directory):
    context = record.context
    source = directory / "source.zip"
    context.update(local_path=str(source), staging_directory=str(directory))
    await manager.registry.persist(record)
    async with manager._semaphore:
        if not context.get("archive_downloaded"):
            context["staging_written"] = 0
            await manager.registry.update(
                record.id,
                status=TransferStatus.RUNNING,
                phase="downloading",
                current=0,
                total=context["archive_source"]["size"],
            )
            output = await _open(source.open, "wb")
            stream = _bytes(manager, record, context["archive_source"])
            try:
                async for chunk in stream:
                    check_staging(
                        manager.settings,
                        manager,
                        context["staging_size"],
                        exclude=record.id,
                        written=context["staging_written"],
                    )
                    await _worker(output.write, chunk)
                    context["staging_written"] = output.tell()
                    await manager.registry.update(
                        record.id, current=output.tell(), checkpoint_progress=False
                    )
                await _worker(_sync, output)
            finally:
                try:
                    await stream.aclose()
                finally:
                    await _worker(output.close)
            context["archive_downloaded"] = True
            await manager.registry.persist(record)
        if not context.get("archive_extracted"):
            if not await _worker(source.exists):
                raise ArchiveError(
                    "Temporary archive bytes are missing. Cancel this job and "
                    "start again; preserve CACHE_DIR across restarts."
                )
            await _decode_archive(manager, record, source, directory)


@_journalled
async def _stop_decoder(process, cancelled=None):
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    await process.wait()
    if cancelled is not None:
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)


async def _decoder_line(process, cancelled):
    reading = asyncio.create_task(process.stdout.readline())
    try:
        await asyncio.wait({reading, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        if cancelled.done():
            raise TransferCancelled("Transfer cancelled")
        try:
            return reading.result()
        except ValueError as error:
            raise ArchiveError(
                "The archive processor exceeded its response limit."
            ) from error
    finally:
        if not reading.done():
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)


def _validate_plan(plan, limit, budget, directory):
    if (
        not isinstance(plan, dict)
        or len(plan.get("files", [])) + len(plan.get("folders", [])) > MAX_ENTRIES
    ):
        raise ArchiveError("The archive processor returned an invalid manifest.")
    verified = Manifest(limit, budget)
    try:
        for parts in plan["folders"]:
            verified.add(
                "/".join(parts) + "/", directory=True, size=0, index=0, container="zip"
            )
        for index, entry in enumerate(plan["files"]):
            verified.add(
                entry["name"],
                directory=False,
                size=entry["size"],
                index=entry["index"],
                container="zip",
            )
            staged = (directory / f"{index}.file").lstat()
            if not stat.S_ISREG(staged.st_mode) or staged.st_size != entry["size"]:
                raise ArchiveError(
                    "Temporary extracted files failed size verification."
                )
        result = verified.result()
        if result["size"] != plan["size"]:
            raise ArchiveError("The archive processor returned an invalid size.")
        return result
    except (KeyError, TypeError, WorkerError) as error:
        raise ArchiveError(
            "The archive processor returned an invalid manifest."
        ) from error


async def _decode_archive(manager, record, source, directory):
    context = record.context
    original = await _worker(lambda: source.stat().st_size)
    total = 0
    if archive_format(context["name"])[1][0] == "zip":
        metadata = await _worker(
            zip_manifest,
            source,
            manager.settings.upload_limit,
            manager.settings.staging_budget_bytes,
        )
        total = metadata["size"]

    def discard_partial():
        for file in directory.glob("*.file"):
            if file.stem.isdecimal():
                file.unlink()

    await _worker(discard_partial)
    context.update(staging_size=original, staging_written=original)
    await manager.registry.update(
        record.id,
        status=TransferStatus.RUNNING,
        phase="extracting",
        current=0,
        total=total,
    )
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(await _worker(lambda: Path(__file__).resolve().parents[2])),
    }
    if os.environ.get("LIBARCHIVE"):
        environment["LIBARCHIVE"] = os.environ["LIBARCHIVE"]
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tgdrive.transfers.archive_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
            limit=MAX_MANIFEST_BYTES + 1024,
        )
    )
    interrupted = False
    while True:
        try:
            process = await asyncio.shield(launch)
            break
        except asyncio.CancelledError:
            if launch.cancelled():
                raise
            interrupted = True
    if interrupted:
        await _stop_decoder(process)
        raise asyncio.CancelledError
    cancelled = asyncio.create_task(record.cancel_event.wait())
    try:
        process.stdin.write(
            json.dumps(
                {
                    "source": str(source),
                    "destination": str(directory),
                    "name": context["name"],
                    "limit": manager.settings.upload_limit,
                    "budget": manager.settings.staging_budget_bytes,
                }
            ).encode()
            + b"\n"
        )
        await process.stdin.drain()
        reserved = current = 0
        while True:
            if record.cancel_event.is_set():
                raise TransferCancelled("Transfer cancelled")
            line = await _decoder_line(process, cancelled)
            if not line or len(line) > MAX_MANIFEST_BYTES + 128:
                raise ArchiveError(
                    "The archive processor stopped or exceeded its resource limits "
                    "(512 MiB memory / 600 CPU seconds). "
                    "Try a smaller archive or dictionary."
                )
            message = json.loads(line)
            if "error" in message:
                raise ArchiveError(str(message["error"])[:500])
            if "result" in message:
                await process.wait()
                if process.returncode:
                    raise ArchiveError(
                        "The archive processor did not finish successfully."
                    )
                plan = await _worker(
                    _validate_plan,
                    message["result"],
                    manager.settings.upload_limit,
                    manager.settings.staging_budget_bytes - original,
                    directory,
                )
                if plan["size"] > reserved:
                    raise ArchiveError("The archive exceeded its storage reservation.")
                context.update(
                    archive_plan=plan,
                    archive_extracted=True,
                    staging_size=original + plan["size"],
                    staging_written=original + plan["size"],
                )
                await manager.registry.persist(record)
                return
            if "reserve" in message:
                reserved = message["reserve"]
                if (
                    type(reserved) is not int
                    or not current
                    <= reserved
                    <= manager.settings.staging_budget_bytes - original
                ):
                    raise ArchiveError(
                        "The archive processor requested invalid storage."
                    )
                check_staging(
                    manager.settings,
                    manager,
                    original + reserved,
                    exclude=record.id,
                    written=context["staging_written"],
                )
                context["staging_size"] = original + reserved
                await manager.registry.persist(record)
            elif "write" in message:
                length = message["write"]
                if (
                    type(length) is not int
                    or not 0 < length <= BLOCK
                    or current + length > reserved
                ):
                    raise ArchiveError(
                        "The archive processor exceeded its storage reservation."
                    )
                check_staging(
                    manager.settings,
                    manager,
                    original + reserved,
                    exclude=record.id,
                    written=context["staging_written"],
                )
            elif "progress" in message:
                progress = message["progress"]
                if type(progress) is not int or not current <= progress <= reserved:
                    raise ArchiveError(
                        "The archive processor returned invalid progress."
                    )
                current = progress
                context["staging_written"] = original + current
                await manager.registry.update(
                    record.id, current=current, checkpoint_progress=False
                )
                continue
            else:
                raise ArchiveError(
                    "The archive processor returned an invalid response."
                )
            process.stdin.write(b"ok\n")
            await process.stdin.drain()
    finally:
        await _stop_decoder(process, cancelled)


def _prepare_folders(manager, record, destination):
    context, store = record.context, manager.store
    folders = context["archive_plan"]["folders"]
    # Executor workers must explicitly restore the job's demo write policy.
    with write_policy(context.get("demo_read_only", False)), store.metadata_batch():
        if context["folder_name"] is not None:
            tree = store.prepare_folder_tree(
                destination,
                context["folder_name"],
                [list(path) for path in folders],
                record.id + "_folders",
            )
            return tree["folders"]
        mapping = {"": destination}
        for index, path in enumerate(path for path in folders if len(path) == 1):
            nested = [
                list(child[1:])
                for child in folders
                if len(child) > 1 and child[0] == path[0]
            ]
            tree = store.prepare_folder_tree(
                destination, path[0], nested, f"{record.id}_folder_{index}"
            )
            mapping.update(
                {
                    "/".join((path[0], relative)) if relative else path[0]: value
                    for relative, value in tree["folders"].items()
                }
            )
        return mapping


async def _upload_extracted(manager, record, directory):
    context = record.context
    destination = await _destination(manager, record)
    if "archive_folders" not in context:
        folders = await _worker(_prepare_folders, manager, record, destination)
        context["archive_folders"] = folders
        if context["folder_name"] is not None:
            binding = manager.store.upload_destination(folders[""])
            context.update(destination=binding["path"], destination_id=binding["id"])
        await manager.registry.persist(record)
    files = context["archive_plan"]["files"]
    completed = context.setdefault("archive_completed", [])
    await manager.registry.update(
        record.id,
        phase="uploading",
        status=TransferStatus.RUNNING,
        total=context["archive_plan"]["size"],
        current=sum(files[i]["size"] for i in completed),
    )
    for index, file in enumerate(files):
        if index in completed:
            continue
        if record.cancel_event.is_set():
            raise TransferCancelled("Transfer cancelled")
        await _destination(manager, record)
        identity = f"{record.id}_{index}"
        try:
            child = manager.registry.get(identity)
        except KeyError:
            relative, _, name = file["name"].rpartition("/")
            target = context["archive_folders"][relative]
            target = (
                manager.store.resolve_id(target.rsplit("/", 1)[-1])
                if target != "/"
                else "/"
            )
            binding = manager.store.upload_destination(target)
            child = await manager.registry.create(
                "queued",
                total=file["size"],
                transfer_id=identity,
                context=dict(
                    kind="upload",
                    archive_parent_id=record.id,
                    archive_file_index=index,
                    local_path=str(directory / f"{index}.file"),
                    destination=binding["path"],
                    destination_id=binding["id"],
                    name=name,
                    size=file["size"],
                ),
            )
        if (
            child.status is not TransferStatus.COMPLETED
            and not manager._metadata_committed(child)
            and not await _worker(Path(child.context["local_path"]).exists)
        ):
            raise ArchiveError(
                "Temporary extracted files are missing. Cancel this extraction "
                "and start again; preserve CACHE_DIR across restarts."
            )
        if child.status is TransferStatus.ERROR:
            await manager.retry_job(child.id)
        elif not child.terminal and (child.task is None or child.task.done()):
            await manager._resume(child)
        while child.task is not None and not child.task.done():
            if record.cancel_event.is_set():
                raise TransferCancelled("Transfer cancelled")
            current = sum(files[i]["size"] for i in completed)
            if child.phase in {"uploading", "finalizing", "backup_pending"}:
                current += child.current
            await manager.registry.update(
                record.id, current=current, checkpoint_progress=False
            )
            await asyncio.wait({child.task}, timeout=0.5)
        if child.status is not TransferStatus.COMPLETED:
            raise TransferError(
                child.error
                or "An extracted file could not be uploaded. Retry the extraction."
            )
        completed.append(index)
        context["staging_written"] = max(0, context["staging_written"] - file["size"])
        context["staging_size"] = max(0, context["staging_size"] - file["size"])
        await manager.registry.persist(record)
    if manager._ensure_backup is not None:
        await manager._ensure_backup(manager.store.revision)
    await manager.registry.update(
        record.id,
        status=TransferStatus.COMPLETED,
        phase="completed",
        current=context["archive_plan"]["size"],
    )


async def run_archive(manager, record):
    directory = _directory(manager, record)
    try:
        committed = manager._metadata_committed(record)
        if committed or (
            record.context.get("source_kind") == "archive"
            and record.context.get("archive_ready")
            and record.context.get("multipart_plan")
        ):
            if (
                not committed
                and not record.cancel_event.is_set()
                and not await _worker(Path(record.context["local_path"]).exists)
            ):
                raise ArchiveError(
                    "Temporary ZIP bytes are missing. Cancel this job and "
                    "start again; preserve CACHE_DIR across restarts."
                )
            await manager._upload_path(
                record,
                Path(record.context["local_path"]),
                record.context["destination"],
                record.context["name"],
                record.context["size"],
            )
            return
        if record.cancel_event.is_set():
            raise TransferCancelled("Transfer cancelled")
        await _destination(manager, record)
        if manager._check_upload_allowed is not None:
            manager._check_upload_allowed()
        await _worker(lambda: directory.mkdir(parents=True, exist_ok=True, mode=0o700))
        if record.context["source_kind"] == "archive":
            await _create_zip(manager, record, directory)
        else:
            await _extract_files(manager, record, directory)
            if record.cancel_event.is_set():
                raise TransferCancelled("Transfer cancelled")
            await _upload_extracted(manager, record, directory)
    except TransferCancelled:
        record.cancel_event.set()
        await manager.registry.update(
            record.id,
            status=TransferStatus.CANCELLED,
            phase="cancelled",
            error="Transfer cancelled",
        )
    finally:
        if record.cancel_event.is_set():
            await cancel_children(manager, record)
        discard_stage(manager, record)


async def cancel_children(manager, record):
    pending = []
    for child in manager.registry.list():
        if child.context.get("archive_parent_id") != record.id or child.status in {
            TransferStatus.COMPLETED,
            TransferStatus.CANCELLED,
        }:
            continue
        try:
            await manager.cancel_job(child.id)
        except BackupPending:
            if child.status is TransferStatus.ERROR:
                await manager.retry_job(child.id)
        if child.task is not None and not child.task.done():
            child.task.add_done_callback(lambda _: discard_stage(manager, record))
            pending.append(child.task)
    if pending:
        await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))


def discard_stage(manager, record):
    if record.status not in {TransferStatus.COMPLETED, TransferStatus.CANCELLED}:
        return
    if any(
        child.context.get("archive_parent_id") == record.id
        and (not child.terminal or (child.task is not None and not child.task.done()))
        for child in manager.registry.list()
    ):
        return
    if (
        record.id in manager._archive_cleanup_pending
        or not _directory(manager, record).exists()
    ):
        return
    manager._archive_cleanup_pending.add(record.id)
    cleanup = asyncio.create_task(
        _worker(shutil.rmtree, _directory(manager, record), True)
    )
    manager.registry._finalizers.add(cleanup)

    def finished(done):
        manager._archive_cleanup_pending.discard(record.id)
        manager.registry._finalizer_done(done)

    cleanup.add_done_callback(finished)
