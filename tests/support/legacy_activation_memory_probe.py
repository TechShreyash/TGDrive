"""Approved-only legacy inline activation preparation and 256 MiB measurement.

Prepare outside the memory limit, then measure the exact admitted manifest in
the existing probe image with --memory=256m --memory-swap=256m --network=host.
MongoDB 8 on loopback:27028 is external to the limit; no Telegram or .env access.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import io
import json
import os
import re
import resource
import signal
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
URI = "mongodb://127.0.0.1:27028/?directConnection=true"
SOURCES = (
    "tgdrive/replication/replica_activation.py",
    "tgdrive/replication/replica_state.py",
    "tgdrive/recovery/recovery_file.py",
    "tgdrive/recovery/staged_snapshot.py",
)


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def sources():
    return {name: digest(ROOT / name) for name in SOURCES}


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def filesystem(directory):
    resolved = Path(directory).resolve()
    candidates = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mounted, backing = line.split(" - ", 1)
        fields, storage = mounted.split(), backing.split()
        mountpoint = Path(
            re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        )
        if resolved.is_relative_to(mountpoint):
            candidates.append(
                (
                    len(mountpoint.parts),
                    {
                        "type": storage[0],
                        "source": storage[1],
                        "mountpoint": str(mountpoint),
                    },
                )
            )
    result = max(candidates, key=lambda value: value[0])[1]
    if result["type"] != "ext4":
        raise RuntimeError(f"Probe scratch must be verified ext4, got {result['type']}")
    return result


def item_counts(source):
    """Count precisely what the bounded parser charges, without building values."""
    import ijson

    stack, jobs = [], []
    count, job_start = 0, None
    for prefix, event, _value in ijson.parse(source, use_float=True):
        if event == "map_key":
            count += 1
        elif event not in {"end_map", "end_array"}:
            if stack and stack[-1] == "array":
                count += 1
            if event in {"start_map", "start_array"}:
                stack.append("map" if event == "start_map" else "array")
        if event in {"end_map", "end_array"}:
            stack.pop()
        if prefix == "candidate.database.jobs.item":
            if event == "start_map":
                job_start = count
            elif event == "end_map":
                jobs.append(count - job_start)
    return {"whole_value_items": count, "job_value_items": jobs}


def jobs(count, items, shape):
    result = []
    for index in range(count):
        summary = {"name": "Many-item legacy history"}
        if shape == "mixed":
            summary.update(
                metadata={f"k{item:05d}": item for item in range(items)},
                items=[{} for _ in range(items)],
            )
        else:
            summary["items"] = [{"v": True} for _ in range(items)]
        result.append(
            {
                "_id": f"legacy-history-{index}",
                "archived": True,
                "created_at": 1.0,
                "status": "completed",
                "summary": summary,
            }
        )
    return result


def legacy_acceptance(configured, path):
    """Rehearse pre-guard validation, preserving the historical JSON semantics."""
    from bson import json_util

    from tgdrive.replication import replica_activation as activation
    from tgdrive.replication.replica_state import read_private

    value = json_util.loads(read_private(path), object_pairs_hook=activation._object)
    assert value["version"] == 1 and value["phase"] == "prepared"
    assert value["backend"] == "mongodb" and value["expected"] == {"kind": "missing"}
    assert activation._candidate(configured, value["candidate"]) == value["candidate"]
    assert activation._activation_identity(value) == value["id"]


def settings(directory, database):
    from unittest.mock import patch

    from pydantic import SecretStr

    from tests.support.recovery_fixtures import settings as synthetic

    with patch.dict(os.environ, {}, clear=True):
        return synthetic(directory).model_copy(
            update={
                "mongodb_uri": SecretStr(URI),
                "mongodb_database": database,
                "min_free_bytes": 0,
            }
        )


def prepare(args):
    from bson import BSON

    from tests.support.mongo_fixtures import FakeMongoBackend
    from tgdrive.core.encryption import key_record
    from tgdrive.recovery.mongo_backup import COLLECTIONS
    from tgdrive.recovery.recovery_file import MAX_DATABASE_BYTES, MAX_DATABASE_RECORDS
    from tgdrive.replication import replica_activation as activation
    from tgdrive.replication.replica_settings import credentials_from_snapshot
    from tgdrive.replication.replica_state import atomic_private
    from tgdrive.storage.mongo_store import MongoStore

    directory = args.prepare.resolve()
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    disk = filesystem(directory)
    database = "tgdrive_test_legacy_activation_" + uuid.uuid4().hex
    configured = settings(directory, database)
    fake = FakeMongoBackend()
    store = MongoStore.create(fake, storage_channel=configured.storage_channel)
    fake.db.nodes.insert_many(
        MongoStore._node(
            f"{index:022d}",
            "root",
            f"f{index}",
            "file",
            size=3,
            message_id=index,
            sha256=hashlib.sha256(b"abc").hexdigest(),
        )
        for index in range(1, args.files + 1)
    )
    fake.db.catalog.update_one(
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
    catalog = store.export()
    store.close()
    records = {name: list(fake.db[name].find({})) for name in COLLECTIONS}
    records["settings"] = [key_record(configured.encryption_key)]
    count = 0 if args.target_budget is not None else args.items
    records["jobs"] = jobs(args.jobs, count, args.shape)

    def manifest():
        checked = activation._candidate(
            configured,
            {
                "catalog": catalog,
                "database": records,
                "complete": True,
                "credentials": credentials_from_snapshot(configured, records),
                "token": "234567:" + "b" * 30,
                "key": "d" * 64,
            },
        )
        value = {
            "version": 1,
            "candidate": checked,
            "expected": {"kind": "missing"},
            "settings_revision": 0,
            "settings_sha256": None,
            "backend": "mongodb",
            "mongodb_key_before_sha256": None,
            "phase": "prepared",
        }
        value["id"] = activation._activation_identity(value)
        return value

    value = manifest()
    if args.target_budget is not None:
        base = item_counts(io.BytesIO(activation._raw(value)))["whole_value_items"]
        count = (args.target_budget - base) // (2 * args.jobs)
        assert count > 0
        records["jobs"] = jobs(args.jobs, count, args.shape)
        value = manifest()
    bson_bytes = sum(len(BSON.encode(row)) for rows in records.values() for row in rows)
    record_count = sum(map(len, records.values()))
    assert bson_bytes <= MAX_DATABASE_BYTES and record_count <= MAX_DATABASE_RECORDS
    path = configured.resolved_data_dir / activation.MANIFEST
    # The historical writer serialized directly. Deliberately bypass today's
    # writer preflight only to construct authentic old-format probe fixtures.
    atomic_private(path, activation._raw(value))
    canonical_bytes = path.stat().st_size
    if args.serialized_bytes is not None:
        remaining = args.serialized_bytes - canonical_bytes
        assert remaining >= 0, (
            "Requested serialized size is smaller than the JSON value"
        )
        block = b" " * (64 * 1024)
        with path.open("ab", buffering=0) as target:
            while remaining:
                chunk = block[: min(remaining, len(block))]
                if target.write(chunk) != len(chunk):
                    raise OSError("Incomplete private-manifest padding write")
                remaining -= len(chunk)
            os.fsync(target.fileno())
        activation._sync(path.parent)
        assert path.stat().st_size == args.serialized_bytes
    assert path.stat().st_size <= 64 * 1024**2
    legacy_acceptance(configured, path)
    with path.open("rb") as source:
        counts = item_counts(source)
    if args.target_budget is not None:
        assert (
            args.target_budget - 2 * args.jobs
            < counts["whole_value_items"]
            <= args.target_budget
        )
    metadata = {
        "database": database,
        "files": args.files,
        "jobs": args.jobs,
        "shape": args.shape,
        "items_per_job": count,
        **counts,
        "source_bson_bytes": bson_bytes,
        "source_records": record_count,
        "manifest_bytes": path.stat().st_size,
        "canonical_manifest_bytes": canonical_bytes,
        "padding_bytes": path.stat().st_size - canonical_bytes,
        "manifest_sha256": digest(path),
        "activation_id": value["id"],
        "accepted_uncapped": True,
        "fixture_writer": (
            "Historical atomic_private(_raw(value)); new preflight skipped"
        ),
        "acceptance_method": (
            "Original read_private/json_util.loads/_candidate/identity; "
            "new _load skipped"
        ),
        "source_sha256": sources(),
        "scratch_filesystem": disk,
        "prepare_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * 1024,
    }
    with (directory / "probe.json").open("x") as target:
        json.dump(metadata, target)
    emit({"prepared": metadata, "directory": str(directory)})


def phase(name):
    emit(
        {
            "phase": name,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        }
    )


def exercise(directory, metadata, expect_resource_limit):
    phase("before_imports")
    from tgdrive.replication import replica_activation as activation
    from tgdrive.storage.mongo import configure

    configured = settings(directory, metadata["database"])
    phase("before_load")
    try:
        value = activation._load(configured)
    except activation.ActivationError as error:
        if not expect_resource_limit:
            raise
        assert error.code == "ACTIVATION_RESOURCE_LIMIT" and error.status == 409
        unchanged = (
            digest(configured.resolved_data_dir / activation.MANIFEST)
            == metadata["manifest_sha256"]
        )
        assert unchanged
        emit(
            {
                "phase": "resource_limit",
                "code": error.code,
                "status": error.status,
                "detail": str(error),
                "manifest_unchanged": unchanged,
            }
        )
        return
    assert not expect_resource_limit, (
        "The oversized legacy value must be refused safely"
    )
    assert value["id"] == metadata["activation_id"]
    phase("after_load")
    del value
    gc.collect()
    phase("before_effective_settings")
    effective = activation.effective_settings(configured)
    assert effective.storage_channel == configured.storage_channel
    phase("after_effective_settings")
    del effective
    gc.collect()
    backend = configure(configured.resolved_data_dir, URI, metadata["database"])
    try:
        assert backend.client.server_info()["version"].startswith("8.")
        assert backend.client.admin.command("hello").get("setName")
        assert not backend.db.list_collection_names()
        backend.acquire()
        phase("before_apply_activation")
        assert activation.apply_activation(configured, backend)
        assert backend.db.nodes.count_documents({"kind": "file"}) == metadata["files"]
        assert backend.db.jobs.count_documents({}) == metadata["jobs"]
        phase("after_apply_activation")
    finally:
        backend.release()
        backend.client.close()
    phase("complete")


def measure(args):
    directory = args.measure.resolve()
    disk = filesystem(directory)
    metadata = json.loads((directory / "probe.json").read_text())
    name = metadata["database"]
    assert re.fullmatch(r"tgdrive_test_legacy_activation_[a-f0-9]{32}", name)
    assert metadata["accepted_uncapped"]
    path = directory / "data" / "channel-activation.json"
    assert digest(path) == metadata["manifest_sha256"]
    with path.open("rb") as source:
        counts = item_counts(source)
    current_sources = sources()
    assert args.allow_source_change or metadata["source_sha256"] == current_sources
    memory = Path("/sys/fs/cgroup/memory.max").read_text().strip()
    swap = Path("/sys/fs/cgroup/memory.swap.max").read_text().strip()
    assert memory == str(256 * 1024**2) and swap == "0"
    started = time.monotonic()
    sys.stdout.flush()
    pid = os.fork()
    if pid == 0:
        try:
            exercise(directory, metadata, args.expect_resource_limit)
        except BaseException as error:
            emit(
                {
                    "child_error": type(error).__name__,
                    "code": getattr(error, "code", None),
                    "detail": str(error)[:500],
                }
            )
            os._exit(1)
        os._exit(0)
    _, status, usage = os.wait4(pid, 0)
    # A tiny monitor survives a loader OOM and retains wait4's actual peak RSS.
    from pymongo import MongoClient

    with MongoClient(URI, serverSelectionTimeoutMS=8000) as client:
        database = client[name]
        state = {
            collection: database[collection].count_documents({})
            for collection in ("catalog", "nodes", "jobs")
        }
        client.drop_database(name)
    result = {
        "child_exit": os.waitstatus_to_exitcode(status),
        "child_signal": os.WTERMSIG(status) if os.WIFSIGNALED(status) else None,
        "peak_rss_bytes": usage.ru_maxrss * 1024,
        "elapsed_seconds": time.monotonic() - started,
        "memory_max": memory,
        "memory_swap_max": swap,
        "scratch_filesystem": disk,
        "database_before_cleanup": state,
        "database_cleaned": name,
        "manifest_unchanged": digest(path) == metadata["manifest_sha256"],
        "input": metadata,
        "measured_item_counts": counts,
        "expected_resource_limit": args.expect_resource_limit,
        "measured_source_sha256": current_sources,
        "memory_events": Path("/sys/fs/cgroup/memory.events").read_text(),
    }
    if args.expect_resource_limit:
        result["safe_refusal"] = (
            result["child_exit"] == 0
            and result["manifest_unchanged"]
            and not any(state.values())
        )
    emit({"measurement": result})
    if args.expect_resource_limit and not result["safe_refusal"]:
        raise SystemExit(1)
    raise SystemExit(
        137 if result["child_signal"] == signal.SIGKILL else result["child_exit"]
    )


if __name__ == "__main__":
    if os.environ.get("TGDRIVE_LEGACY_ACTIVATION_PROBE") != "1":
        raise SystemExit(
            "Owner approval and TGDRIVE_LEGACY_ACTIVATION_PROBE=1 required"
        )
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", type=Path)
    modes.add_argument("--measure", type=Path)
    parser.add_argument("--jobs", type=int, choices=(1, 8, 16, 17), default=8)
    parser.add_argument("--items", type=int, default=64000)
    parser.add_argument("--shape", choices=("cells", "mixed"), default="cells")
    parser.add_argument("--target-budget", type=int)
    parser.add_argument("--serialized-bytes", type=int)
    parser.add_argument("--files", type=int, default=500)
    parser.add_argument("--allow-source-change", action="store_true")
    parser.add_argument("--expect-resource-limit", action="store_true")
    arguments = parser.parse_args()
    assert 0 < arguments.items <= 64000 and 0 < arguments.files <= 500
    assert (
        arguments.serialized_bytes is None
        or 0 < arguments.serialized_bytes <= 64 * 1024**2
    )
    os.umask(0o077)
    prepare(arguments) if arguments.prepare else measure(arguments)
