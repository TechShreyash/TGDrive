"""Validate authenticated V2 plaintext without building a catalog in memory.

SQLite is a private, disposable validation index, never the drive's catalog.
Callers must authenticate the complete encrypted file before passing plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from itertools import islice
from pathlib import Path

import ijson
from bson import BSON, Decimal128, json_util
from bson.codec_options import CodecOptions
from bson.errors import InvalidDocument

from tgdrive.storage.capacity import measure_capacity, require_headroom
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.storage.store import (
    ROOT_ID,
    SCHEMA_VERSION,
    _FileNode,
    _name_key,
    _parse_one,
    _strict_int,
    _validate_id,
    _validate_timestamp,
)

# MongoDB already limits individual BSON documents to 16 MiB. Extended JSON
# may expand binary data, so its per-record allowance is larger. Neither limit
# restricts the number of records or total catalog size.
MAX_BSON_BYTES = 16 * 1024**2
MAX_VALUE_BYTES = 32 * 1024**2
MAX_TOKEN_BYTES = MAX_VALUE_BYTES
MAX_VALUE_ITEMS = 65_536
MAX_NESTING = 128
READ_BYTES = 64 * 1024
_JSON_OPTIONS = json_util.JSONOptions(tz_aware=True)
_BSON_OPTIONS = CodecOptions(tz_aware=True)
_REQUIRED = {
    "format",
    "version",
    "complete",
    "created_at",
    "database",
    "replica",
    "credentials",
}
_SCHEMA = """
CREATE TABLE documents (
    sequence INTEGER PRIMARY KEY, collection TEXT NOT NULL,
    identity BLOB NOT NULL, document BLOB NOT NULL,
    UNIQUE(collection, identity)
);
CREATE INDEX collection_rows ON documents(collection, sequence);
CREATE TABLE nodes (
    id TEXT PRIMARY KEY, parent TEXT, kind TEXT NOT NULL, name_key TEXT NOT NULL,
    trashed INTEGER NOT NULL, size INTEGER NOT NULL, reachable INTEGER DEFAULT 0,
    hidden INTEGER DEFAULT 0
);
CREATE INDEX node_parents ON nodes(parent);
CREATE UNIQUE INDEX sibling_names ON nodes(parent, name_key) WHERE trashed = 0;
CREATE TABLE messages (node TEXT NOT NULL, message INTEGER NOT NULL,
    PRIMARY KEY(node, message));
CREATE INDEX source_messages ON messages(message);
CREATE TABLE mappings (source INTEGER PRIMARY KEY, destination INTEGER UNIQUE);
CREATE TABLE shares (id TEXT PRIMARY KEY, node TEXT NOT NULL, hash TEXT UNIQUE);
CREATE TABLE operations (id TEXT PRIMARY KEY, node TEXT UNIQUE);
CREATE TABLE deletions (id TEXT PRIMARY KEY, root TEXT, active INTEGER);
CREATE UNIQUE INDEX deletion_roots ON deletions(root) WHERE active = 1;
CREATE TABLE deletion_messages (deletion TEXT, message INTEGER,
    PRIMARY KEY(deletion, message));
CREATE TABLE preparations (id TEXT PRIMARY KEY);
CREATE TABLE queue (id TEXT PRIMARY KEY);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value BLOB NOT NULL);
"""


class RecoveryResourceLimit(ValueError):
    """JSON admission exceeded a bounded decoder resource allowance."""


def _pack(value):
    raw = BSON.encode(value)
    if len(raw) > MAX_BSON_BYTES:
        raise ValueError("Recovery document exceeds MongoDB's 16 MiB limit")
    return raw


def _unpack(raw):
    return BSON(raw).decode(codec_options=_BSON_OPTIONS)


def _object(value):
    """Reject representations PyMongo helpfully coerces or silently discards."""
    for field, bits in (("$numberInt", 32), ("$numberLong", 64)):
        if field in value:
            number = value[field]
            if (
                not isinstance(number, str)
                or len(number) > 20
                or re.fullmatch(r"-?(0|[1-9][0-9]*)", number) is None
                or not -(1 << (bits - 1)) <= int(number) < 1 << (bits - 1)
            ):
                raise ValueError("Invalid recovery BSON integer")
    if "$binary" in value:
        binary = value["$binary"]
        if "$type" in value:
            if set(value) != {"$binary", "$type"} or type(value["$type"]) is bool:
                raise ValueError("Invalid recovery BSON binary")
            encoded = binary
        else:
            if (
                set(value) != {"$binary"}
                or not isinstance(binary, dict)
                or set(binary) != {"base64", "subType"}
            ):
                raise ValueError("Invalid recovery BSON binary")
            encoded = binary["base64"]
        if not isinstance(encoded, str):
            raise ValueError("Invalid recovery BSON binary")
        base64.b64decode(encoded.encode("ascii"), validate=True)
    if "$undefined" in value and (
        set(value) != {"$undefined"} or value["$undefined"] is not True
    ):
        raise ValueError("Invalid recovery BSON undefined value")
    if "$date" in value and (
        isinstance(value["$date"], bool) or not isinstance(value["$date"], (str, int))
    ):
        raise ValueError("Invalid recovery BSON date")
    if "$timestamp" in value:
        timestamp = value["$timestamp"]
        if (
            not isinstance(timestamp, dict)
            or set(timestamp) != {"t", "i"}
            or any(
                isinstance(number, bool)
                or not isinstance(number, int)
                or not 0 <= number < 2**32
                for number in timestamp.values()
            )
        ):
            raise ValueError("Invalid recovery BSON timestamp")
    for field, options_field in (
        ("$regex", "$options"),
        ("$regularExpression", "options"),
    ):
        if field in value:
            expression = value if field == "$regex" else value[field]
            if not isinstance(expression, dict):
                raise ValueError("Invalid recovery BSON regular expression")
            options = expression.get(options_field, "")
            if (
                not isinstance(options, str)
                or re.fullmatch(r"[ilmsux]*", options) is None
            ):
                raise ValueError("Invalid recovery BSON regular expression options")
            if field == "$regex" and set(value) - {"$regex", "$options"}:
                raise ValueError("Invalid recovery BSON regular expression fields")
    result = json_util.object_hook(value, json_options=_JSON_OPTIONS)
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError("Non-finite recovery number")
    if isinstance(result, Decimal128) and not result.to_decimal().is_finite():
        raise ValueError("Non-finite recovery decimal")
    return result


class _DiskBudget:
    def __init__(
        self, directory, min_free_bytes, *, probe_directory=None, outstanding_bytes=0
    ):
        self.directory = directory
        self.minimum = _strict_int(min_free_bytes, "min_free_bytes")
        self.probe_directory = probe_directory
        self.outstanding_bytes = outstanding_bytes
        self.bytes = self.records = 0
        self.check(force=True)

    def check(self, added_bytes=0, *, force=False):
        self.bytes += added_bytes
        self.records += 1
        if force or self.bytes >= READ_BYTES or self.records >= 100:
            try:
                require_headroom(
                    measure_capacity(
                        self.directory, probe_directory=self.probe_directory
                    ),
                    max(added_bytes, READ_BYTES),
                    reserve_bytes=self.minimum,
                    outstanding_bytes=self.outstanding_bytes,
                )
            except OSError as error:
                raise OSError(
                    "Not enough temporary space or unavailable recovery capacity; "
                    "check the backing-capacity probe and free space, then retry"
                ) from error
            self.bytes = self.records = 0


def _write_many(db, statement, rows, budget):
    source = iter(rows)
    while batch := list(islice(source, 256)):
        budget.check(len(batch) * 256)
        db.executemany(statement, batch)


class _Reader:
    """Stop a decoder accumulating an enormous token before it emits an event."""

    def __init__(self, source):
        self.source = source
        self.position = self.checkpoint = 0

    def read(self, size=-1):
        if size == 0:
            return b""
        remaining = MAX_TOKEN_BYTES - (self.position - self.checkpoint)
        if remaining <= 0:
            raise RecoveryResourceLimit(
                "Recovery JSON token or whitespace exceeds its limit"
            )
        data = self.source.read(
            min(size if size > 0 else READ_BYTES, READ_BYTES, remaining)
        )
        self.position += len(data)
        return data


class _Parser:
    def __init__(self, source, *, object_hook=_object):
        self.object_hook = object_hook
        self.reader = _Reader(source)
        self.events = iter(
            ijson.basic_parse(self.reader, use_float=True, buf_size=READ_BYTES)
        )

    def next(self):
        event, value = next(self.events)
        self.reader.checkpoint = self.reader.position
        if event == "number" and isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite recovery number")
        return event, value

    def expect(self, expected):
        event, value = self.next()
        if event != expected:
            raise ValueError("Invalid recovery JSON structure")
        return value

    def _check_record_size(self, start):
        if self.reader.position - start > MAX_VALUE_BYTES + READ_BYTES:
            raise RecoveryResourceLimit("Recovery JSON record exceeds its limit")

    def value(self, first=None, *, depth=0, start=None, items=None):
        if depth >= MAX_NESTING:
            raise RecoveryResourceLimit("Recovery JSON nesting exceeds its limit")
        if start is None:
            start = self.reader.position
        if items is None:
            items = [0]
        event, value = first or self.next()
        self._check_record_size(start)
        if event == "start_map":
            result = {}
            while True:
                event, key = self.next()
                self._check_record_size(start)
                if event == "end_map":
                    break
                if event != "map_key" or key in result:
                    raise ValueError("Duplicate or invalid recovery field")
                items[0] += 1
                if items[0] > MAX_VALUE_ITEMS:
                    raise RecoveryResourceLimit(
                        "Recovery JSON record exceeds its item limit"
                    )
                result[key] = self.value(depth=depth + 1, start=start, items=items)
            return self.object_hook(result)
        if event == "start_array":
            result = []
            while True:
                first = self.next()
                self._check_record_size(start)
                if first[0] == "end_array":
                    return result
                items[0] += 1
                if items[0] > MAX_VALUE_ITEMS:
                    raise RecoveryResourceLimit(
                        "Recovery JSON record exceeds its item limit"
                    )
                result.append(
                    self.value(first, depth=depth + 1, start=start, items=items)
                )
        if event not in {"string", "number", "boolean", "null"}:
            raise ValueError("Invalid recovery JSON value")
        return value


def _identity(value):
    # Match Python's existing duplicate-id check, including int/float/Int64
    # aliases; arrays and objects were never accepted as snapshot identities.
    hash(value)
    if isinstance(value, (int, float)):
        numerator, denominator = value.as_integer_ratio()
        return f"number:{numerator}/{denominator}".encode()
    return _pack({"id": value})


def _portable_row(row):
    return {key: value for key, value in row.items() if key not in {"_id", "expire_at"}}


def _catalog_metadata(catalog):
    if catalog.get("schema_version") == 4:
        from tgdrive.recovery.recovery_file import LegacyRecoveryFile

        raise LegacyRecoveryFile(
            "This is a TGDrive 2.2 recovery file. Preserve it and its original "
            "key; restore with tagged v2.2 into isolated MongoDB, then upgrade."
        )
    if (
        catalog.get("_id") != "catalog"
        or type(catalog.get("schema_version")) is not int
        or catalog["schema_version"] != SCHEMA_VERSION
        or type(catalog.get("generation")) is not int
        or catalog["generation"] < 0
        or type(catalog.get("revision")) is not int
    ):
        raise ValueError("Invalid recovery catalog metadata")
    _validate_id(catalog["drive_id"])
    _strict_int(catalog["revision"], "revision", 1)
    channel = catalog.get("storage_channel_id")
    if type(channel) is not int or not -(1 << 63) <= channel < 0:
        raise ValueError("Invalid recovery source channel")


def _node(db, row, budget):
    from tgdrive.storage.pdf_progress import validate_pdf_state
    from tgdrive.storage.video_progress import validate_video_state

    validate_video_state(row)
    validate_pdf_state(row)
    identity = row["_id"]
    if row.get("id", identity) != identity or row.get("name_key") != _name_key(
        row["name"]
    ):
        raise ValueError("Invalid catalog node identity or name index")
    value = {
        "id": identity,
        "name": row["name"],
        "type": row["kind"],
        "upload_date": row["upload_date"],
        "trashed": row["trashed"],
        "share_hashes": [],
    }
    if row.get("starred"):
        value["starred"] = True
    if row["kind"] == "file":
        value.update(message_id=row["message_id"], size=row["size"])
        for key in ("parts", "sha256"):
            if row.get(key):
                value[key] = row[key]
        if row.get("poster") is not None:
            value["poster"] = row["poster"]
    else:
        value["children"] = {}
    parsed, _ = _parse_one(value)
    parent = row["parent_id"]
    if identity == ROOT_ID:
        if parent is not None or row["kind"] != "folder" or row["trashed"]:
            raise ValueError("Invalid catalog root")
    else:
        _validate_id(parent, allow_root=True)
    db.execute(
        "INSERT INTO nodes(id,parent,kind,name_key,trashed,size) VALUES(?,?,?,?,?,?)",
        (
            identity,
            parent,
            row["kind"],
            _name_key(parsed.name),
            int(parsed.trashed),
            parsed.size if isinstance(parsed, _FileNode) else 0,
        ),
    )
    if row["kind"] == "file":
        messages = [row["message_id"]]
        messages.extend(part["message_id"] for part in row.get("parts") or [])
        if row.get("poster"):
            messages.append(row["poster"]["message_id"])
        _write_many(
            db,
            "INSERT OR IGNORE INTO messages(node,message) VALUES(?,?)",
            ((identity, message) for message in messages),
            budget,
        )


def _record(db, collection, row, budget):
    from tgdrive.recovery.recovery_file import sanitize_database

    if not isinstance(row, dict) or "_id" not in row:
        raise ValueError("Invalid recovery database document")
    raw = _pack(row)
    identity = _identity(row["_id"])
    budget.check(len(raw) + 2 * len(identity))
    db.execute(
        "INSERT INTO documents(collection,identity,document) VALUES(?,?,?)",
        (collection, identity, raw),
    )
    if collection == "sessions":
        raise ValueError("Recovery snapshots must not contain browser sessions")
    if collection == "settings":
        if sanitize_database({"settings": [row], "sessions": []})["settings"] != [row]:
            raise ValueError("Recovery snapshots must not contain user sessions")
    elif collection == "catalog":
        _catalog_metadata(row)
    elif collection == "nodes":
        _node(db, row, budget)
    elif collection == "shares":
        share = MongoStore._validate_share_record(_portable_row(row))
        db.execute(
            "INSERT INTO shares VALUES(?,?,?)",
            (share["id"], share["node_id"], share["token_hash"]),
        )
    elif collection == "operations":
        operation = _portable_row(row)
        if set(operation) != {"operation_id", "node_id"}:
            raise ValueError("Invalid recovery file operation")
        db.execute(
            "INSERT INTO operations VALUES(?,?)",
            (
                _validate_id(operation["operation_id"]),
                _validate_id(operation["node_id"]),
            ),
        )
    elif collection == "deletions":
        intent = MongoStore._validate_deletion_record(_portable_row(row))
        db.execute(
            "INSERT INTO deletions VALUES(?,?,?)",
            (intent["id"], intent["root_id"], int(intent["state"] != "completed")),
        )
        _write_many(
            db,
            "INSERT INTO deletion_messages VALUES(?,?)",
            ((intent["id"], message) for message in intent["message_ids"]),
            budget,
        )
    elif collection == "preparations":
        preparation = MongoStore._validate_folder_preparation(_portable_row(row))
        db.execute("INSERT INTO preparations VALUES(?)", (preparation["request_id"],))


def _database(parser, db, budget):
    from tgdrive.recovery.mongo_backup import COLLECTIONS

    parser.expect("start_map")
    seen = set()
    while True:
        event, name = parser.next()
        if event == "end_map":
            break
        if event != "map_key" or name in seen or name not in COLLECTIONS:
            raise ValueError("Duplicate or unknown recovery collection")
        seen.add(name)
        parser.expect("start_array")
        while True:
            first = parser.next()
            if first[0] == "end_array":
                break
            _record(db, name, parser.value(first), budget)
    if seen != set(COLLECTIONS):
        raise ValueError("Missing recovery collection")


def _replica(parser, db, budget):
    first = parser.next()
    if first == ("null", None):
        return None
    if first[0] != "start_map":
        raise ValueError("Invalid recovery replica")
    seen, channel = set(), None
    while True:
        event, name = parser.next()
        if event == "end_map":
            break
        if event != "map_key" or name in seen or name not in {"channel", "messages"}:
            raise ValueError("Duplicate or invalid recovery replica field")
        seen.add(name)
        if name == "channel":
            channel = parser.value()
            if type(channel) is not int or not -(1 << 63) <= channel < 0:
                raise ValueError("Invalid recovery replica channel")
        else:
            parser.expect("start_map")
            while True:
                event, source = parser.next()
                if event == "end_map":
                    break
                if (
                    event != "map_key"
                    or len(source) > 19
                    or re.fullmatch(r"[1-9][0-9]*", source) is None
                    or int(source) > (1 << 63) - 1
                ):
                    raise ValueError("Invalid recovery source message")
                destination = parser.value()
                if type(destination) is not int or not 0 < destination < 2**31:
                    raise ValueError("Invalid recovery destination message")
                budget.check(128)
                db.execute(
                    "INSERT INTO mappings VALUES(?,?)", (int(source), destination)
                )
    if seen != {"channel", "messages"}:
        raise ValueError("Missing recovery replica field")
    return {"channel": channel}


def _validate_graph(db, catalog, budget):
    if db.execute("SELECT 1 FROM nodes WHERE id=?", (ROOT_ID,)).fetchone() is None:
        raise ValueError("Missing recovery catalog root")
    if db.execute(
        "SELECT 1 FROM nodes n LEFT JOIN nodes p ON p.id=n.parent "
        "WHERE n.id != ? AND (p.id IS NULL OR p.kind != 'folder') LIMIT 1",
        (ROOT_ID,),
    ).fetchone():
        raise ValueError("Invalid recovery catalog parent")
    # A disk-backed frontier handles wide and arbitrarily deep trees without a
    # Python catalog, recursive traversal, or an unbounded ancestry list.
    db.execute("INSERT INTO queue VALUES(?)", (ROOT_ID,))
    while batch := db.execute("SELECT id FROM queue LIMIT 256").fetchall():
        budget.check(force=True)
        for (identity,) in batch:
            db.execute("DELETE FROM queue WHERE id=?", (identity,))
            db.execute(
                "UPDATE nodes SET reachable=1, hidden=trashed OR COALESCE("
                "(SELECT parent.hidden FROM nodes parent "
                "WHERE parent.id=nodes.parent),0) "
                "WHERE id=?",
                (identity,),
            )
            children = db.execute("SELECT id FROM nodes WHERE parent=?", (identity,))
            _write_many(
                db,
                "INSERT INTO queue VALUES(?)",
                children,
                budget,
            )
    if db.execute("SELECT 1 FROM nodes WHERE reachable=0 LIMIT 1").fetchone():
        raise ValueError("Recovery catalog contains unreachable nodes or cycles")
    if db.execute(
        "SELECT 1 FROM shares s LEFT JOIN nodes n ON n.id=s.node "
        "WHERE n.id IS NULL LIMIT 1"
    ).fetchone():
        raise ValueError("Recovery share references a missing node")
    if db.execute(
        "SELECT 1 FROM operations o JOIN nodes n ON n.id=o.node "
        "WHERE n.kind!='file' LIMIT 1"
    ).fetchone():
        raise ValueError("Recovery operation references a folder")


def _validate_deletions(db, budget):
    for (raw,) in db.execute(
        "SELECT document FROM documents WHERE collection='deletions'"
    ):
        intent = _portable_row(_unpack(raw))
        budget.check(force=True)
        node = db.execute(
            "SELECT parent,trashed FROM nodes WHERE id=?", (intent["root_id"],)
        ).fetchone()
        if intent["state"] == "completed":
            if node is not None:
                raise ValueError("Completed recovery deletion still has a live root")
            continue
        if node is None or not node[1]:
            raise ValueError("Invalid active recovery deletion root")
        # Compare the recorded path backwards, avoiding a catalog-depth list.
        path = intent["path"]
        end = len(path) - int(path.endswith("/"))
        identity = intent["root_id"]
        first = True
        while identity != ROOT_ID:
            start = path.rfind("/", 0, end)
            if path[start + 1 : end] != identity:
                raise ValueError("Recovery deletion path does not match ancestry")
            parent, trashed = db.execute(
                "SELECT parent,trashed FROM nodes WHERE id=?", (identity,)
            ).fetchone()
            if not first and trashed:
                raise ValueError("Recovery deletion has a trashed ancestor")
            first, identity, end = False, parent, start
        if end != 0:
            raise ValueError("Recovery deletion path does not match ancestry")
        difference = db.execute(
            "WITH RECURSIVE subtree(id) AS (SELECT ? UNION ALL "
            "SELECT n.id FROM nodes n JOIN subtree s ON n.parent=s.id), "
            "expected(message) AS (SELECT m.message FROM messages m "
            "JOIN subtree s ON s.id=m.node), "
            "actual(message) AS (SELECT message FROM deletion_messages "
            "WHERE deletion=?) "
            "SELECT 1 FROM (SELECT message FROM expected "
            "EXCEPT SELECT message FROM actual) "
            "UNION ALL SELECT 1 FROM (SELECT message FROM actual "
            "EXCEPT SELECT message FROM expected) LIMIT 1",
            (intent["root_id"], intent["id"]),
        ).fetchone()
        if difference:
            raise ValueError("Recovery deletion messages do not match its subtree")


def _validate(db, header, budget):
    from tgdrive.recovery.recovery_file import _TOKEN, _credentials

    if (
        header["format"] != "tgdrive-backup"
        or type(header["version"]) is not int
        or header["version"] != 2
        or header["complete"] is not True
    ):
        raise ValueError("Invalid recovery envelope")
    _validate_timestamp(header["created_at"])
    _credentials(header["credentials"])
    if "recovery_token" in header:
        token = header["recovery_token"]
        if (
            not isinstance(token, str)
            or len(token) > 256
            or not _TOKEN.fullmatch(token)
        ):
            raise ValueError("Invalid recovery bot token")
    rows = db.execute(
        "SELECT document FROM documents WHERE collection='catalog' LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("Recovery snapshot requires exactly one catalog")
    catalog = _unpack(rows[0][0])
    _validate_graph(db, catalog, budget)
    _validate_deletions(db, budget)
    if header["replica"] is not None:
        if header["replica"]["channel"] == catalog["storage_channel_id"]:
            raise ValueError("Recovery replica channel must differ from its source")
        if (
            db.execute(
                "SELECT 1 FROM messages m LEFT JOIN mappings p ON p.source=m.message "
                "WHERE p.source IS NULL LIMIT 1"
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM mappings p LEFT JOIN messages m ON m.message=p.source "
                "WHERE m.message IS NULL LIMIT 1"
            ).fetchone()
        ):
            raise ValueError("Recovery mapping does not match catalog messages")
    db.execute("INSERT INTO metadata VALUES('header',?)", (_pack(header),))
    db.execute("INSERT INTO metadata VALUES('catalog',?)", (_pack(catalog),))


class StagedSnapshot:
    """Read-only validated rows; each iterator keeps only its current row in memory."""

    def __init__(self, path):
        self.path = Path(path)
        with closing(self._connect()) as db:
            self.header = _unpack(
                db.execute("SELECT value FROM metadata WHERE key='header'").fetchone()[
                    0
                ]
            )
            self.catalog = _unpack(
                db.execute("SELECT value FROM metadata WHERE key='catalog'").fetchone()[
                    0
                ]
            )
            # BSON represents large plain JSON integers as Int64. Keep the
            # validated envelope scalar types while iterators retain raw BSON.
            if self.header["replica"] is not None:
                self.header["replica"]["channel"] = int(
                    self.header["replica"]["channel"]
                )
            for key in (
                "schema_version",
                "generation",
                "revision",
                "storage_channel_id",
            ):
                self.catalog[key] = int(self.catalog[key])
            self.file_count = db.execute(
                "SELECT count(*) FROM nodes WHERE kind='file'"
            ).fetchone()[0]
            # Python summation avoids SQLite's signed-64-bit sum overflow.
            self.total_bytes = sum(
                size
                for (size,) in db.execute("SELECT size FROM nodes WHERE kind='file'")
            )
            self.counters = dict.fromkeys(
                ("files", "folders", "total_bytes", "trashed_files", "trashed_bytes"), 0
            )
            for identity, kind, size, hidden in db.execute(
                "SELECT id,kind,size,hidden FROM nodes"
            ):
                if kind == "file":
                    self.counters["trashed_files" if hidden else "files"] += 1
                    self.counters["trashed_bytes" if hidden else "total_bytes"] += size
                elif identity != ROOT_ID and not hidden:
                    self.counters["folders"] += 1
        with self.path.open("rb") as source:
            self.digest = hashlib.file_digest(source, "sha256").hexdigest()

    def _connect(self):
        return sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )

    def records(self, collection):
        from tgdrive.recovery.mongo_backup import COLLECTIONS

        if collection not in COLLECTIONS:
            raise ValueError("Unknown recovery collection")
        with (
            closing(self._connect()) as db,
            closing(
                db.execute(
                    "SELECT document FROM documents WHERE collection=? "
                    "ORDER BY sequence",
                    (collection,),
                )
            ) as cursor,
        ):
            for (raw,) in cursor:
                yield _unpack(raw)

    def mapping(self, source_id):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT destination FROM mappings WHERE source=?", (int(source_id),)
            ).fetchone()
        if row is None:
            raise KeyError(source_id)
        return row[0]

    def _check_channel(self, channel):
        if channel is not None and self.header["replica"] != {"channel": channel}:
            raise ValueError("Requested channel has no validated recovery mapping")

    @staticmethod
    def _remap_node(db, row, channel):
        if channel is not None and row["kind"] == "file":

            def remap(identity):
                return db.execute(
                    "SELECT destination FROM mappings WHERE source=?", (identity,)
                ).fetchone()[0]

            row["message_id"] = remap(row["message_id"])
            for part in row.get("parts") or []:
                part["message_id"] = remap(part["message_id"])
            if row.get("poster"):
                row["poster"]["message_id"] = remap(row["poster"]["message_id"])
        return row

    def nodes(self, channel=None):
        self._check_channel(channel)
        with closing(self._connect()) as db, closing(self.records("nodes")) as rows:
            for row in rows:
                yield self._remap_node(db, row, channel)

    def node_after(self, sequence=0, channel=None):
        """Read one node without carrying a SQLite cursor between worker threads."""
        _strict_int(sequence, "sequence")
        self._check_channel(channel)
        with closing(self._connect()) as db:
            with closing(
                db.execute(
                    "SELECT sequence,document FROM documents WHERE collection='nodes' "
                    "AND sequence>? ORDER BY sequence LIMIT 1",
                    (sequence,),
                )
            ) as cursor:
                found = cursor.fetchone()
            if found is None:
                return None
            return found[0], self._remap_node(db, _unpack(found[1]), channel)


def stage_snapshot(
    json_path,
    destination,
    *,
    min_free_bytes=0,
    probe_directory=None,
    outstanding_bytes=0,
):
    """Stage completely authenticated plaintext; delete partial indexes on failure."""
    destination = Path(destination)
    budget = _DiskBudget(
        destination.parent,
        min_free_bytes,
        probe_directory=probe_directory,
        outstanding_bytes=outstanding_bytes,
    )
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(destination)) as db:
            db.execute("PRAGMA cache_size=-2048")
            db.execute("PRAGMA temp_store=FILE")
            db.execute("PRAGMA mmap_size=0")
            db.executescript(_SCHEMA)
            with Path(json_path).open("rb") as source:
                parser = _Parser(source)
                parser.expect("start_map")
                header, seen = {}, set()
                while True:
                    event, name = parser.next()
                    if event == "end_map":
                        break
                    if (
                        event != "map_key"
                        or name in seen
                        or name not in _REQUIRED | {"recovery_token"}
                    ):
                        raise ValueError("Duplicate or unknown recovery envelope field")
                    seen.add(name)
                    if name == "database":
                        _database(parser, db, budget)
                    elif name == "replica":
                        header[name] = _replica(parser, db, budget)
                    else:
                        header[name] = parser.value()
                if not seen >= _REQUIRED:
                    raise ValueError("Missing recovery envelope field")
                if next(parser.events, None) is not None:
                    raise ValueError("Trailing recovery JSON data")
            _validate(db, header, budget)
            budget.check(force=True)
            db.commit()
            budget.check(force=True)
        with destination.open("rb") as index:
            os.fsync(index.fileno())
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return StagedSnapshot(destination)
    except (
        KeyError,
        TypeError,
        ArithmeticError,
        sqlite3.IntegrityError,
        InvalidDocument,
        ijson.JSONError,
    ) as error:
        destination.unlink(missing_ok=True)
        raise ValueError("Invalid recovery snapshot") from error
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def validate_snapshot(
    json_path, *, min_free_bytes=0, probe_directory=None, outstanding_bytes=0
):
    """Validate an exporter file with an automatically removed private index."""
    with tempfile.TemporaryDirectory(
        prefix=".snapshot-", dir=Path(json_path).parent
    ) as directory:
        stage_snapshot(
            json_path,
            Path(directory) / "index.sqlite",
            min_free_bytes=min_free_bytes,
            probe_directory=probe_directory,
            outstanding_bytes=outstanding_bytes,
        )
