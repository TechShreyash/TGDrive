"""Manual, owner-approved recovery probe; never contacts real Telegram or reads .env.

Run as a module with --files 1 first, then --files 10001 inside the approved
256 MiB container. MongoDB 8 on loopback:27028 is outside that memory budget.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import ijson
from bson import BSON, Int64, json_util
from pydantic import SecretStr

from tests.support.recovery_fixtures import settings
from tgdrive.core import encryption
from tgdrive.recovery.recovery_file import write_recovery_snapshot
from tgdrive.recovery.snapshot_stream import decrypt_snapshot
from tgdrive.recovery.staged_snapshot import (
    MAX_BSON_BYTES,
    MAX_VALUE_BYTES,
    MAX_VALUE_ITEMS,
)
from tgdrive.replication.replica import ChannelReplica
from tgdrive.replication.replica_activation import apply_activation
from tgdrive.storage.mongo import configure, initialize_if_empty
from tgdrive.storage.mongo_store import MongoStore

ROOT = Path(__file__).resolve().parents[2]
MAIN, BACKUP, TARGET = -1001234567890, -1002234567890, -1003234567890
SLOTS = {MAIN: 0, BACKUP: 1, TARGET: 2}
STRIDE, CHECKSUM = 1_000_000, hashlib.sha256(b"abc").hexdigest()


def name(index):
    return "界" * 250 + "".join(
        chr(0x4E00 + ((index >> shift) & 15)) for shift in range(0, 20, 4)
    )


def history(index, stamp, shape):
    summary = {
        "name": "Probe history",
        "probe_date": stamp,
        "probe_integer": Int64(2**40),
    }
    if shape == "many":
        assert MAX_VALUE_ITEMS >= 64_016
        summary["metadata"] = {f"k{item:05d}": item for item in range(32_000)}
        summary["items"] = [{} for _ in range(32_000)]
    row = {
        "_id": "probe-history" if index == 0 else f"probe-history-{index}",
        "archived": True,
        "created_at": 1.0,
        "status": "completed",
        "summary": summary,
    }
    assert len(BSON.encode(row)) < MAX_BSON_BYTES
    assert len(json_util.dumps(row).encode()) < MAX_VALUE_BYTES
    return row


class Telegram:
    """Arithmetic file receipts; only four small publication-path records persist."""

    def __init__(self, directory, count):
        self.directory, self.count = directory, count
        self.publications, self.receipts, self.pins = {}, {}, {}
        self.copies = 0

    async def start(self):
        pass

    async def close(self):
        pass

    async def inspect(self, channel, **_kwargs):
        assert channel in SLOTS
        return {"id": channel, "title": "Synthetic dedicated channel", "private": True}

    async def verify(self, channel, message, size):
        if (channel, message) in self.publications:
            assert self.publications[channel, message].stat().st_size == size
        else:
            assert (
                message // STRIDE == SLOTS[channel]
                and 0 < message % STRIDE <= self.count
                and size == 3
            )

    async def verify_many(self, channel, documents):
        for message, size in documents:
            await self.verify(channel, message, size)

    async def copy(self, source, message, target, *, random_id, expected_size):
        await self.verify(source, message, expected_size)
        assert type(random_id) is int and random_id > 0
        self.copies += 1
        return SLOTS[target] * STRIDE + message % STRIDE

    async def publish(self, channel, path, *, random_id, caption, pin):
        receipt = channel, random_id
        if receipt not in self.receipts:
            message = 10 * STRIDE + len(self.receipts) + 1
            target = self.directory / f"publication-{message}.tgdb"
            shutil.copyfile(path, target)
            self.receipts[receipt] = message
            self.publications[channel, message] = target
        message = self.receipts[receipt]
        if pin:
            self.pins[channel] = message
        return message

    async def pinned_message(self, channel):
        return self.pins.get(channel)

    async def download(self, channel, message, destination):
        message = message or self.pins[channel]
        shutil.copyfile(self.publications[channel, message], destination)
        return message


def audit(directory, phase):
    total, sql = 0, {}
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        assert not path.stat().st_mode & 0o077
        total += size
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\0":
                continue
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as db:
            sql[str(path.relative_to(directory))] = {
                "bytes": size,
                "pages_bytes": db.execute("PRAGMA page_count").fetchone()[0]
                * db.execute("PRAGMA page_size").fetchone()[0],
            }
    result = {
        "phase": phase,
        "disk_bytes": total,
        "sqlite": sql,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
    }
    print(json.dumps({"probe_phase": result}), file=sys.stderr, flush=True)
    return result


def memory_marker(phase):
    current_rss = next(
        int(row.split()[1]) * 1024
        for row in Path("/proc/self/status").read_text().splitlines()
        if row.startswith("VmRSS:")
    )
    result = {
        "phase": phase,
        "elapsed_monotonic": time.monotonic(),
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "rss_bytes": current_rss,
    }
    for metric in ("memory.current", "memory.peak", "memory.stat"):
        path = Path("/sys/fs/cgroup") / metric
        if path.exists():
            result[metric] = path.read_text().strip()
    print(json.dumps({"probe_memory": result}), file=sys.stderr, flush=True)


def scratch_filesystem():
    target = Path(tempfile.gettempdir()).resolve()
    matches = []
    for row in Path("/proc/self/mountinfo").read_text().splitlines():
        mounted, filesystem = row.split(" - ", 1)
        mountpoint = Path(mounted.split()[4])
        if target.is_relative_to(mountpoint):
            matches.append((len(mountpoint.parts), filesystem.split()[0]))
    return max(matches)[1]


def trace_publication(replica):
    from functools import wraps

    from tgdrive.recovery import snapshot_stream, staged_snapshot

    def trace(owner, attribute):
        operation = getattr(owner, attribute)

        @wraps(operation)
        def measured(*args, **kwargs):
            memory_marker(attribute + ":before")
            try:
                return operation(*args, **kwargs)
            finally:
                memory_marker(attribute + ":after")

        setattr(owner, attribute, measured)

    trace(replica, "_stage_staged_bundle")
    trace(replica, "_capture")
    trace(snapshot_stream, "write_staged_json")
    trace(staged_snapshot, "validate_snapshot")


async def probe(args, directory):
    configured = settings(directory).model_copy(
        update={
            "mongodb_uri": SecretStr(args.uri),
            "mongodb_database": args.database,
            "min_free_bytes": 0,
        }
    )
    backend = configure(configured.resolved_data_dir, args.uri, args.database)
    backend.ping()
    server = backend.client.server_info()["version"]
    replica_set = backend.client.admin.command("hello").get("setName")
    if not server.startswith("8.") or not replica_set:
        raise RuntimeError("The probe requires a MongoDB 8 replica set")
    if backend.db.list_collection_names():
        raise RuntimeError("Refusing to replace an existing probe database")
    replica = store = None
    acquired = False
    try:
        backend.acquire()
        acquired = True
        initialize_if_empty(configured, backend)
        encryption.configure(configured.encryption_key)
        for start in range(1, args.files + 1, 256):
            rows = [
                MongoStore._node(
                    f"{index:022d}",
                    "root",
                    name(index),
                    "file",
                    size=3,
                    message_id=index,
                    sha256=CHECKSUM,
                    parts=[
                        {
                            "index": 0,
                            "offset": 0,
                            "size": 3,
                            "message_id": index,
                            "sha256": CHECKSUM,
                        }
                    ],
                )
                for index in range(start, min(start + 256, args.files + 1))
            ]
            backend.write(backend.db.nodes, "insert_many", rows)
        backend.write(
            backend.db.catalog,
            "update_one",
            {"_id": "catalog"},
            {
                "$set": {
                    "revision": 3,
                    "backed_up_revision": 3,
                    "files": args.files,
                    "total_bytes": 3 * args.files,
                }
            },
        )
        stamp = datetime(2026, 1, 1, tzinfo=UTC)
        jobs = 16 if args.shape == "many" else 1
        for index in range(jobs):
            backend.write(
                backend.db.jobs, "insert_one", history(index, stamp, args.shape)
            )
        store = MongoStore(backend)
        store.export = lambda: (_ for _ in ()).throw(
            AssertionError("Catalog materialization is forbidden")
        )
        phases = [audit(directory, "seeded")]
        memory_marker("seeded")
        capsule, plaintext = directory / "normal.tgdb", directory / "expanded.json"
        key = bytes.fromhex(encryption.backup_key())
        memory_marker("normal_snapshot:before")
        revision, counts, _digest = await asyncio.to_thread(
            write_recovery_snapshot, backend, capsule, key, configured
        )
        await asyncio.to_thread(
            decrypt_snapshot,
            capsule,
            plaintext,
            key,
            configured,
            max_expanded_bytes=None,
        )
        expanded = plaintext.stat().st_size
        assert counts["nodes"] == args.files + 1 and revision == 3
        assert args.files <= 10000 or expanded > 16 * 1024**2
        phases.append(audit(directory, "normal_snapshot"))
        memory_marker("normal_snapshot:after")
        plaintext.unlink()
        remote = Telegram(directory, args.files)
        replica = ChannelReplica(
            configured,
            SimpleNamespace(store=store, mongo=backend),
            transport_factory=lambda *_: remote,
        )
        trace_publication(replica)
        replica.start = lambda: None
        config = {"channel": BACKUP, "token": "234567:" + "b" * 30, "enabled": True}
        replica.journal.set("config", config)
        await replica._sync(config)
        phases.append(audit(directory, "replica_published"))
        await replica.inspect_file(
            replica.backup_file(), replica.key(), source=BACKUP, token=config["token"]
        )
        phases.append(audit(directory, "inspected"))
        await replica.recover_to(TARGET, "345678:" + "c" * 30, str(TARGET))
        await replica._recover_copy(replica.journal.get("recovery"))
        phases.append(audit(directory, "recovered"))
        await asyncio.to_thread(replica.activate)
        phases.append(audit(directory, "prepared"))
        assert await asyncio.to_thread(apply_activation, configured, backend)
        phases.append(audit(directory, "applied"))
        encryption.check_drive_key(backend, configured.encryption_key)
        assert (
            backend.db.catalog.find_one({"_id": "catalog"})["storage_channel_id"]
            == TARGET
        )
        assert backend.db.nodes.count_documents({"kind": "file"}) == args.files
        for row in backend.db.nodes.find({"kind": "file"}).batch_size(256):
            index = int(row["_id"])
            assert (
                row["name"] == name(index) and row["message_id"] == 2 * STRIDE + index
            )
            assert row["size"] == 3 and len(row["parts"]) == 1
            assert row["parts"][0]["message_id"] == row["message_id"]
        assert backend.db.jobs.count_documents({}) == jobs
        for index in range(jobs):
            identity = "probe-history" if index == 0 else f"probe-history-{index}"
            summary = backend.db.jobs.find_one({"_id": identity})["summary"]
            assert summary["probe_date"] == stamp and summary["probe_integer"] == 2**40
            if args.shape == "many":
                assert len(summary["metadata"]) == len(summary["items"]) == 32_000
                assert summary["metadata"]["k31999"] == 31_999
                assert all(item == {} for item in summary["items"])
        rollback = next(
            configured.resolved_data_dir.glob("recovery-archive/*/mongodb-before.json")
        )
        assert rollback.stat().st_size > 0 and remote.copies == 2 * args.files
        with rollback.open("rb") as preserved:
            original = next(ijson.items(preserved, "catalog.item", use_float=True))
            assert original["storage_channel_id"] == MAIN
            assert original["files"] == args.files
        return {
            "mongodb_version": server,
            "mongodb_replica_set": replica_set,
            "files": args.files,
            "shape": args.shape,
            "archived_jobs": jobs,
            "job_map_keys": 32_000 if args.shape == "many" else 0,
            "job_array_items": 32_000 if args.shape == "many" else 0,
            "name_characters": 255,
            "snapshot_counts": counts,
            "normal_encrypted_bytes": capsule.stat().st_size,
            "normal_expanded_bytes": expanded,
            "rollback_bytes": rollback.stat().st_size,
            "phases": phases,
            "observed_phase_disk_peak_bytes": max(row["disk_bytes"] for row in phases),
        }
    finally:
        if replica:
            await replica.close()
        if store:
            store.close()
        backend.release()
        if acquired:
            backend.client.drop_database(args.database)
        backend.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=10001)
    parser.add_argument("--shape", choices=("flat", "many"), default="flat")
    parser.add_argument(
        "--uri", default="mongodb://127.0.0.1:27028/?directConnection=true"
    )
    parser.add_argument("--database", default="tgdrive_test_memory_" + uuid.uuid4().hex)
    parser.add_argument(
        "--git-revision", help="Host git rev-parse HEAD for slim containers"
    )
    parser.add_argument(
        "--git-status", help="Host git status --porcelain; empty if clean"
    )
    args = parser.parse_args()
    uri = urlsplit(args.uri)
    if (
        not args.database.startswith("tgdrive_test_")
        or uri.scheme != "mongodb"
        or uri.hostname not in {"127.0.0.1", "localhost"}
        or uri.port != 27028
        or uri.username
        or uri.password
        or uri.path not in {"", "/"}
        or not 0 < args.files < STRIDE
    ):
        parser.error("Use a disposable tgdrive_test_ database on loopback:27028")
    if (
        args.git_revision is not None
        and re.fullmatch(r"[a-f0-9]{40}", args.git_revision) is None
    ):
        parser.error("--git-revision must be a full lowercase commit SHA")
    git = shutil.which("git")
    if args.git_revision is not None:
        if args.git_status is None:
            parser.error(
                "Supply --git-status with --git-revision; use an empty value when clean"
            )
        revision, git_status = args.git_revision, args.git_status
    elif git is not None:
        revision = subprocess.check_output(  # noqa: S603
            [git, "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        git_status = subprocess.check_output(  # noqa: S603
            [git, "status", "--porcelain"], cwd=ROOT, text=True
        ).strip()
    else:
        parser.error("Git or host --git-revision/--git-status values are required")
    os.umask(0o077)
    filesystem = scratch_filesystem()
    if filesystem in {"tmpfs", "ramfs"}:
        parser.error("Use disk-backed scratch; tmpfs would consume the memory budget")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="tgdrive-recovery-probe-") as temporary:
        result = asyncio.run(probe(args, Path(temporary)))
    result.update(
        elapsed_seconds=time.monotonic() - started,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        mongodb_budget="external MongoDB 8 replica set, excluded from container limit",
        scratch_filesystem=filesystem,
    )
    result["host_meminfo"] = Path("/proc/meminfo").read_text().splitlines()[0]
    result["cgroup_memory_max"] = Path("/sys/fs/cgroup/memory.max").read_text().strip()
    result["cgroup_memory_swap_max"] = (
        Path("/sys/fs/cgroup/memory.swap.max").read_text().strip()
    )
    for metric in ("memory.peak", "memory.current", "memory.events"):
        path = Path("/sys/fs/cgroup") / metric
        if path.exists():
            result["cgroup_" + metric.replace(".", "_")] = path.read_text().strip()
    result["disk_measurement"] = (
        "Phase snapshots; transient disk peaks between phases are not sampled"
    )
    result["git_revision"], result["git_status"] = revision, git_status
    result["python"] = sys.version
    result["source_sha256"] = {}
    for path in [*ROOT.glob("tgdrive/**/*.py"), Path(__file__).resolve()]:
        with path.open("rb") as source:
            result["source_sha256"][str(path.relative_to(ROOT))] = hashlib.file_digest(
                source, "sha256"
            ).hexdigest()
    print(json.dumps(result, sort_keys=True))
