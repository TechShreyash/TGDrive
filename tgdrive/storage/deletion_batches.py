"""Bounded companion rows for large deletion intents."""

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from tgdrive.core.errors import ValidationError
from tgdrive.storage.store import _strict_int, _validate_id

# Each bucket can hold at most this many distinct Telegram message IDs.
BUCKET_SIZE = 512


def node_messages(row):
    if row["kind"] != "file":
        return set()
    messages = {row["message_id"]}
    messages.update(part["message_id"] for part in row.get("parts") or ())
    if row.get("poster"):
        messages.add(row["poster"]["message_id"])
    return messages


@contextmanager
def deletion_plan(descendants, batch_size):
    """Disk-backed leaf-first plan that counts shared references."""
    with (
        TemporaryDirectory(prefix="tgdrive-deletion-") as directory,
        closing(sqlite3.connect(Path(directory) / "plan.sqlite")) as database,
    ):
        database.executescript(
            """
            PRAGMA journal_mode=OFF;
            CREATE TABLE nodes (
                position INTEGER PRIMARY KEY, node TEXT NOT NULL,
                is_file INTEGER NOT NULL, size INTEGER NOT NULL,
                messages TEXT NOT NULL
            );
            CREATE TABLE refs (
                message INTEGER PRIMARY KEY, remaining INTEGER NOT NULL
            );
            """
        )
        for position, row in enumerate(descendants):
            messages = node_messages(row)
            is_file = row["kind"] == "file"
            database.execute(
                "INSERT INTO nodes VALUES(?,?,?,?,?)",
                (
                    position,
                    row["_id"],
                    is_file,
                    row["size"],
                    json.dumps(sorted(messages)),
                ),
            )
            database.executemany(
                "INSERT INTO refs VALUES(?,1) "
                "ON CONFLICT(message) DO UPDATE SET remaining=remaining+1",
                ((message,) for message in messages),
            )
        database.execute("CREATE INDEX refs_remaining ON refs(remaining)")
        yield _prune_batches(database, batch_size)


def _prune_batches(database, batch_size):
    # Catalog traversal was breadth-first, so reversing it removes children
    # before their parents and keeps every committed snapshot connected.
    cursor = database.execute(
        "SELECT node, is_file, size, messages FROM nodes ORDER BY position DESC"
    )
    while rows := cursor.fetchmany(batch_size):
        references = Counter(
            message for _, _, _, ids in rows for message in json.loads(ids)
        )
        database.executemany(
            "UPDATE refs SET remaining=remaining-? WHERE message=?",
            ((count, message) for message, count in references.items()),
        )
        removed = [
            message
            for (message,) in database.execute(
                "SELECT message FROM refs WHERE remaining=0"
            )
        ]
        database.execute("DELETE FROM refs WHERE remaining=0")
        yield (
            [node for node, _, _, _ in rows],
            sum(is_file for _, is_file, _, _ in rows),
            sum(size for _, is_file, size, _ in rows if is_file),
            removed,
        )


def bucket_messages(messages):
    buckets = defaultdict(set)
    for message in messages:
        buckets[(message - 1) // BUCKET_SIZE].add(message)
    return buckets


def batch_id(intent_id, bucket):
    return f"{intent_id}:{bucket}"


def validate_batch(row):
    if set(row) != {"_id", "intent_id", "bucket", "message_ids", "pending_message_ids"}:
        raise ValidationError("Invalid deletion batch")
    intent_id = _validate_id(row["intent_id"])
    bucket = _strict_int(row["bucket"], "deletion bucket")
    if row["_id"] != batch_id(intent_id, bucket):
        raise ValidationError("Invalid deletion batch identity")
    messages, pending = row["message_ids"], row["pending_message_ids"]
    if (
        not isinstance(messages, list)
        or not isinstance(pending, list)
        or not 1 <= len(messages) <= BUCKET_SIZE
        or len(pending) > len(messages)
    ):
        raise ValidationError("Invalid deletion batch size")
    known = {_strict_int(value, "message_id", 1) for value in messages}
    remaining = {_strict_int(value, "message_id", 1) for value in pending}
    if (
        len(known) != len(messages)
        or len(remaining) != len(pending)
        or not remaining <= known
        or any((value - 1) // BUCKET_SIZE != bucket for value in known)
    ):
        raise ValidationError("Invalid deletion batch messages")
    return row


def batched_count(header):
    if "message_count" not in header:
        return None
    count = _strict_int(header["message_count"], "deletion message count", 1)
    if (
        header["state"] == "completed"
        or header["message_ids"] != []
        or header["pending_message_ids"] != []
    ):
        raise ValidationError("Invalid batched deletion header")
    return count
