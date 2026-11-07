"""Manual approval-only rehearsal using authentic v2.2.0 backup/restore code.

Uses the currently installed dependencies, not a complete historical image.
Only random tgdrive_test_ databases on disposable MongoDB 8 localhost:27028.
No Telegram requests, deployment credentials, .env, branch changes or installs.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TAG, TAG_SHA = "v2.2.0", "3c4940012528deebe5a238e96b91ec5705643d4c"
URI = "mongodb://127.0.0.1:27028/?directConnection=true"
OLD_KEY, SHARE, JOB = "a" * 64, "synthetic-permanent-share", "U" * 22


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def backend(name):
    from tgdrive.storage.mongo import MongoBackend

    assert re.fullmatch(
        r"tgdrive_test_legacy_[a-f0-9]{32}_(source|mongo|v2|rollback|probe)", name
    )
    result = MongoBackend(URI, name)
    result.ping()
    assert result.client.admin.command("buildInfo")["version"].startswith("8.")
    assert result.client.admin.command("hello").get("setName")
    result.acquire()
    return result


def release(value):
    value.release()
    value.client.close()


def current_settings(directory):
    from tests.support.recovery_fixtures import settings

    with patch.dict(os.environ, {}, clear=True):
        return settings(directory)


def old_worker(manifest):
    import tgdrive
    from tgdrive.core.config import Settings
    from tgdrive.recovery.mongo_backup import restore_snapshot, write_snapshot
    from tgdrive.recovery.recovery_file import write_recovery_snapshot
    from tgdrive.storage.mongo_store import MongoStore

    root = manifest.parent.resolve()
    state = json.loads(manifest.read_text())
    assert root.parent == Path(tempfile.gettempdir()).resolve()
    assert root.name.startswith("tgdrive-legacy-")
    assert Path(tgdrive.__file__).resolve().is_relative_to(root / TAG)
    if state.get("read_rollback"):
        restored = backend(state["databases"]["rollback"])
        try:
            catalog = MongoStore(restored).export()
            assert catalog["schema_version"] == 4
            print(
                json.dumps(
                    {"rollback_read_by_tag": TAG, "revision": catalog["revision"]}
                )
            )
        finally:
            release(restored)
        return
    source = backend(state["databases"]["source"])
    try:
        from tgdrive.transfers.multipart_transfer import _validate_plan, fingerprint
        from tgdrive.transfers.tasks import TransferRegistry

        plain = root / "legacy-upload.bin"
        plain.write_bytes(b"abcdefg")
        digest, parts = fingerprint(plain, 7, 4)
        parts[0].update(
            sender="synthetic-bot", send_started=True, message_id=501, confirmed=True
        )
        plan = {"version": 1, "kind": "bot", "parts": parts}
        _validate_plan(plan, 7, digest)
        registry = TransferRegistry(mongo=source.db.jobs, mongo_backend=source)
        asyncio.run(
            registry.create(
                "queued",
                total=7,
                transfer_id=JOB,
                context={
                    "kind": "upload",
                    "name": "pending.bin",
                    "size": 7,
                    "local_path": str(plain),
                    "destination": "/",
                    "sha256": digest,
                    "multipart_plan": plan,
                },
            )
        )
        registry.close()
        settings = Settings(
            _env_file=None,
            MONGODB_URI=URI,
            MONGODB_DATABASE=source.db.name,
            API_ID=12345,
            API_HASH="b" * 32,
            BOT_TOKENS="123456:" + "x" * 30,
            STORAGE_CHANNEL=-1001234567890,
            ADMIN_PASSWORD="synthetic password",
            SESSION_SECRET="s" * 48,
            MONGODB_BACKUP_KEY=OLD_KEY,
            DATA_DIR=root / "old-data",
            CACHE_DIR=root / "old-cache",
            COOKIE_SECURE=False,
        )
        write_snapshot(source, root / "legacy-mongo.tgdb", bytes.fromhex(OLD_KEY))
        write_recovery_snapshot(
            source, root / "legacy-v2.tgdb", bytes.fromhex(OLD_KEY), settings
        )
        for kind in ("mongo", "v2"):
            target = backend(state["databases"][kind])
            try:
                restore_snapshot(
                    target, root / f"legacy-{kind}.tgdb", bytes.fromhex(OLD_KEY)
                )
                assert MongoStore(target).export()["schema_version"] == 4
            finally:
                release(target)
        print(
            json.dumps(
                {
                    "tag": TAG,
                    "loaded_module": str(Path(tgdrive.__file__).resolve()),
                    "restored_formats": ["TGDRIVE-MONGO-1", "TGDRIVE-BACKUP-2/schema4"],
                }
            )
        )
    finally:
        release(source)


def run_old(manifest, old_root):
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(Path(__file__).resolve()), "--old-worker", str(manifest)],
        cwd=manifest.parent,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(old_root),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


async def refusal_checks(settings, database, legacy_v2):
    from telethon.tl import types

    from tgdrive.core import encryption
    from tgdrive.recovery.recovery_file import LegacyRecoveryFile, decode_recovery_file
    from tgdrive.replication.replica import ChannelReplica
    from tgdrive.replication.replica_transport import (
        ReplicaTransport,
        ReplicaTransportError,
    )

    encryption.configure(settings.encryption_key)

    class LegacyPin:
        def __init__(self, *_):
            pass

        async def start(self):
            pass

        async def close(self):
            pass

        async def inspect(self, *_args, **_kwargs):
            return {}

        async def pinned_message(self, _channel):
            return 17

        async def download(self, *_args):
            ReplicaTransport._capsule(
                SimpleNamespace(
                    size=123,
                    attributes=[types.DocumentAttributeFilename("tgdrive-legacy.tgdb")],
                )
            )
            raise AssertionError("A known legacy pin must not initialize a drive")

    replica = ChannelReplica(
        settings,
        SimpleNamespace(mongo=database, store=None),
        transport_factory=LegacyPin,
    )
    try:
        try:
            await replica.discover_backup()
        except ReplicaTransportError as error:
            assert error.code == "LEGACY_BACKUP_REQUIRES_UPGRADE"
        else:
            raise AssertionError("Legacy discovery must refuse, not return False")
        try:
            await replica.inspect_file(legacy_v2, replica.key())
        except ValueError:
            pass
        else:
            raise AssertionError("Current-key import must refuse the historical file")
        # The original key authenticates the fixture for this schema check;
        # public import still uses the derived current key, not an old-key input.
        try:
            decode_recovery_file(legacy_v2.read_bytes(), OLD_KEY)
        except LegacyRecoveryFile:
            pass
        else:
            raise AssertionError("Current code must refuse schema-4 direct activation")
        assert database.db.catalog.count_documents({}) == 0
        assert replica.journal.get("candidate") is None
        return {
            "legacy_pin": "LEGACY_BACKUP_REQUIRES_UPGRADE",
            "current_key_import": "refused",
            "authenticated_schema4": "refused",
        }
    finally:
        await replica.close()


def main():
    if os.environ.get("TGDRIVE_LEGACY_RECOVERY_REHEARSAL") != "1":
        raise SystemExit(
            "Owner approval and TGDRIVE_LEGACY_RECOVERY_REHEARSAL=1 required"
        )
    from bson import json_util

    from tests.storage.test_upgrade_from_2_2 import drive_2_2
    from tgdrive.core import encryption
    from tgdrive.storage.mongo_store import MongoStore
    from tgdrive.storage.upgrade import upgrade

    executable = shutil.which("git")
    if executable is None:
        raise SystemExit("The existing Git executable is required")

    def git(*args):
        return subprocess.check_output([executable, *args], cwd=ROOT)  # noqa: S603

    assert git("rev-parse", TAG + "^{commit}").decode().strip() == TAG_SHA
    databases = {
        kind: "tgdrive_test_legacy_" + uuid.uuid4().hex + "_" + kind
        for kind in ("source", "mongo", "v2", "rollback", "probe")
    }
    report = {
        "tag": TAG,
        "tag_sha": TAG_SHA,
        "current_head": git("rev-parse", "HEAD").decode().strip(),
        "python": sys.version.split()[0],
        "dependency_scope": "Current dependencies, not the complete v2.2 image",
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("pymongo", "cryptography", "pydantic")
        },
    }
    cleanup = backend(databases["probe"])
    try:
        with tempfile.TemporaryDirectory(prefix="tgdrive-legacy-") as temporary:
            root = Path(temporary)
            old_root = root / TAG
            old_root.mkdir()
            with tarfile.open(
                fileobj=io.BytesIO(git("archive", "--format=tar", TAG, "tgdrive"))
            ) as archive:
                archive.extractall(old_root, filter="data")
            source = backend(databases["source"])
            fixture = drive_2_2()
            try:
                for name in ("catalog", "nodes", "shares", "settings"):
                    rows = list(fixture.db[name].rows)
                    if name == "shares":
                        for row in rows:
                            row["token_hash"] = hashlib.sha256(
                                (
                                    SHARE if row["expires_at"] is None else "expired"
                                ).encode()
                            ).hexdigest()
                            row["created_at"] = (
                                datetime.now(UTC) - timedelta(days=2)
                            ).isoformat()
                            if row["expires_at"] is not None:
                                row["expires_at"] = (
                                    datetime.now(UTC) - timedelta(days=1)
                                ).isoformat()
                    source.db[name].insert_many(rows)
            finally:
                release(source)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"databases": databases}))
            manifest.chmod(0o600)
            key_file = root / "original-backup-key.txt"
            key_file.write_text(OLD_KEY)
            key_file.chmod(0o600)
            report["historical_restore"] = run_old(manifest, old_root)
            hashes = {
                kind: checksum(root / f"legacy-{kind}.tgdb") for kind in ("mongo", "v2")
            }
            report["upgrades"] = {}
            for kind in ("mongo", "v2"):
                target = backend(databases[kind])
                try:
                    job = target.db.jobs.find_one({"_id": JOB})
                    shares = list(target.db.shares.find({}).sort("_id", 1))
                    configured = current_settings(root / kind)
                    copy = Path(upgrade(target, configured))
                    assert copy.stat().st_mode & 0o077 == 0
                    assert upgrade(target, configured) is None
                    store = MongoStore(target)
                    assert store.statistics() == {
                        "files": 1,
                        "folders": 1,
                        "total_bytes": 3 * 2**30,
                        "trashed_files": 1,
                        "trashed_bytes": 10,
                    }
                    episode = store.get_file("/" + "A" * 22 + "/" + "E" * 22)
                    assert [part["message_id"] for part in episode["parts"]] == [
                        102,
                        103,
                    ]
                    assert all(part.get("nonce") is None for part in episode["parts"])
                    assert (
                        encryption.crypt(None, 3, b"legacy plaintext range")
                        == b"legacy plaintext range"
                    )
                    assert store.authorize_file("/" + "A" * 22 + "/" + "E" * 22, SHARE)
                    assert [
                        (row["_id"], row["token_hash"])
                        for row in target.db.shares.find({}).sort("_id", 1)
                    ] == [(row["_id"], row["token_hash"]) for row in shares]
                    assert target.db.jobs.find_one({"_id": JOB}) == job
                    with gzip.open(copy, "rt") as saved:
                        rows = [json_util.loads(line) for line in saved]
                    assert (
                        next(
                            row["document"]
                            for row in rows
                            if row["collection"] == "catalog"
                        )["schema_version"]
                        == 4
                    )
                    if kind == "mongo":
                        rollback = backend(databases["rollback"])
                        try:
                            for row in rows:
                                if row["collection"] != "runtime":
                                    rollback.db[row["collection"]].insert_one(
                                        row["document"]
                                    )
                        finally:
                            release(rollback)
                    report["upgrades"][kind] = {
                        "schema": 5,
                        "rollback_sha256": checksum(copy),
                        "parts_shares_plans_counters_preserved": True,
                    }
                finally:
                    release(target)
            manifest.write_text(
                json.dumps({"databases": databases, "read_rollback": True})
            )
            report["rollback"] = run_old(manifest, old_root)
            report["current_refusals"] = asyncio.run(
                refusal_checks(
                    current_settings(root / "refusal"), cleanup, root / "legacy-v2.tgdb"
                )
            )
            assert key_file.read_text() == OLD_KEY
            assert hashes == {
                kind: checksum(root / f"legacy-{kind}.tgdb") for kind in hashes
            }
            report["artifact_sha256"] = hashes
            report["limitations"] = (
                "No historical dependency image, live Telegram "
                "or full media HTTP transfer"
            )
    finally:
        cleanup.release()
        for name in databases.values():
            assert name.startswith("tgdrive_test_legacy_")
            cleanup.client.drop_database(name)
        cleanup.client.close()
    report["cleanup"] = (
        "Exact random test databases and private temporary directory removed"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--old-worker":
        old_worker(Path(sys.argv[2]))
    else:
        main()
