"""One-time, resumable upgrade of a TGDrive 2.0-2.2 database to the 2.3 catalog.

2.2 drives use catalog schema 4. Their nodes, shares and file locations are
kept as they are; the upgrade only adds what 2.3 reads (search words, type
groups, drive totals, record expiry and the encryption-key check) and then
raises the schema version. Every step can run again, so an interrupted
upgrade simply continues on the next start. Files already in Telegram are
never touched: parts without a nonce stay readable as plain bytes.

Before changing anything it writes a copy of every collection to
``DATA_DIR/upgrade-2.2-<time>.json.gz`` (owner-only permissions).
"""

from __future__ import annotations

import gzip
import logging
import os
from datetime import UTC, datetime
from typing import Any

from bson import json_util
from pymongo import UpdateOne

logger = logging.getLogger(__name__)

OLD_SCHEMA = 4
_BATCH = 1000


def needs_upgrade(backend) -> bool:
    row = backend.db.catalog.find_one({"_id": "catalog"}, {"schema_version": 1})
    return row is not None and row.get("schema_version") == OLD_SCHEMA


def _save_copy(backend, directory) -> str:
    from tgdrive.recovery.mongo_backup import COLLECTIONS

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"upgrade-2.2-{stamp}.json.gz"
    temporary = path.with_name("." + path.name + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with (
        os.fdopen(descriptor, "wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb") as target,
    ):
        for name in (
            *COLLECTIONS,
            "runtime",
            "replica_state",
            "replica_chunks",
            "replica_copies",
        ):
            for row in backend.db[name].find({}):
                target.write(
                    json_util.dumps({"collection": name, "document": row}).encode()
                    + b"\n"
                )
    os.replace(temporary, path)
    return str(path)


def _bulk(backend, collection, updates) -> None:
    batch = []
    for update in updates:
        batch.append(update)
        if len(batch) == _BATCH:
            backend.check_owner()
            collection.bulk_write(batch, ordered=False)
            batch = []
    if batch:
        backend.check_owner()
        collection.bulk_write(batch, ordered=False)


def _totals(backend) -> dict[str, int]:
    from tgdrive.storage.store import ROOT_ID

    children: dict[Any, list[dict]] = {}
    for row in backend.db.nodes.find(
        {}, {"_id": 1, "parent_id": 1, "kind": 1, "size": 1, "trashed": 1}
    ):
        children.setdefault(row["parent_id"], []).append(row)
    totals = dict.fromkeys(
        ("files", "folders", "total_bytes", "trashed_files", "trashed_bytes"), 0
    )
    stack = [(ROOT_ID, False)]
    while stack:
        parent, hidden = stack.pop()
        for row in children.get(parent, []):
            inside = hidden or bool(row.get("trashed"))
            if row["kind"] == "file":
                totals["trashed_files" if inside else "files"] += 1
                totals["trashed_bytes" if inside else "total_bytes"] += row["size"]
            else:
                if not inside:
                    totals["folders"] += 1
                stack.append((row["_id"], inside))
    return totals


def upgrade(backend, settings) -> str | None:
    """Upgrade a schema-4 drive in place; returns the safety copy's path."""
    from tgdrive.core import encryption
    from tgdrive.storage.mongo_store import (
        _EXPIRED_SHARE_RETENTION,
        _RECORD_RETENTION,
        MongoStore,
        _expiry_date,
    )
    from tgdrive.storage.store import SCHEMA_VERSION

    if not needs_upgrade(backend):
        return None
    backend.check_owner()
    copy_path = _save_copy(backend, settings.resolved_data_dir)
    logger.warning(
        "Upgrading the TGDrive 2.2 catalog to %s; a copy of the database was "
        "saved to %s",
        SCHEMA_VERSION,
        copy_path,
    )
    nodes = backend.db.nodes
    _bulk(
        backend,
        nodes,
        (
            UpdateOne(
                {"_id": row["_id"]},
                {"$set": MongoStore._names(row["name"], row["kind"])},
            )
            for row in nodes.find({"parent_id": {"$ne": None}}, {"name": 1, "kind": 1})
        ),
    )
    nodes.update_one(
        {"parent_id": None},
        {"$set": {"fold": "", "tokens": [], "group": "folder"}},
    )
    later = datetime.now(UTC) + _RECORD_RETENTION
    _bulk(
        backend,
        backend.db.shares,
        (
            UpdateOne(
                {"_id": row["_id"]},
                {
                    "$set": {
                        "expire_at": _expiry_date(row["expires_at"])
                        + _EXPIRED_SHARE_RETENTION
                    }
                },
            )
            for row in backend.db.shares.find(
                {"expires_at": {"$ne": None}, "expire_at": {"$exists": False}},
                {"expires_at": 1},
            )
        ),
    )
    backend.db.deletions.update_many(
        {"state": "completed", "expire_at": {"$exists": False}},
        {"$set": {"expire_at": later}},
    )
    backend.db.preparations.update_many(
        {"expire_at": {"$exists": False}}, {"$set": {"expire_at": later}}
    )
    backend.check_owner()
    if backend.db.settings.find_one({"_id": "encryption"}) is None:
        # 2.2 stored no encrypted parts, so this key protects new uploads only.
        backend.db.settings.insert_one(encryption.key_record(settings.encryption_key))
    backend.db.settings.update_one(
        {"_id": "bootstrap"}, {"$unset": {"document.MONGODB_BACKUP_KEY": ""}}
    )
    # 2.2 kept its independent backup channel's progress in DATA_DIR SQLite,
    # which 2.3 cannot read. Start that channel from scratch: the owner sets it
    # up again in Storage & Backups. The old channel's messages are not touched.
    for name in ("replica_state", "replica_chunks", "replica_copies"):
        backend.check_owner()
        backend.db[name].delete_many({})
    old_journal = settings.resolved_data_dir / "channel-backup.sqlite3"
    if old_journal.is_file() and not old_journal.is_symlink():
        old_journal.rename(old_journal.with_name(old_journal.name + ".2.2-unused"))
        logger.warning(
            "The 2.2 independent backup channel is not carried over; set it up "
            "again in Storage & Backups. Its old messages stay in Telegram."
        )
    bootstrap_file = settings.resolved_data_dir / "replica-bootstrap.json"
    if bootstrap_file.is_file() and not bootstrap_file.is_symlink():
        bootstrap_file.rename(
            bootstrap_file.with_name(bootstrap_file.name + ".2.2-unused")
        )
    if "parent_id_1" in nodes.index_information():
        # 2.3's listing indexes all start with parent_id, so this one is waste.
        nodes.drop_index("parent_id_1")
    backend.check_owner()
    # Last step: once the version changes, 2.3 opens the drive normally.
    backend.db.catalog.update_one(
        {"_id": "catalog", "schema_version": OLD_SCHEMA},
        {
            "$set": {
                **_totals(backend),
                "schema_version": SCHEMA_VERSION,
                "upgraded_from": OLD_SCHEMA,
                "upgraded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        },
    )
    logger.warning("TGDrive 2.2 catalog upgrade finished")
    return copy_path
