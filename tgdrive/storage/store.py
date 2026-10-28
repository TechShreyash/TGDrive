"""SQLite catalog and shared validation for portable drive snapshots.

MongoStore reuses the snapshot schemas and catalog helpers while persisting its
own catalog in MongoDB. SQLite transactions protect this catalog implementation;
public reads return independent records.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import unicodedata
import weakref
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tgdrive.core.errors import (
    CorruptStoreError,
    DuplicateNameError,
    InvalidNameError,
    InvalidPathError,
    InvalidRevisionError,
    NotDirectoryError,
    NotFileError,
    NotFoundError,
    PersistenceError,
    RootMutationError,
    TrashedItemError,
    UnsupportedSchemaError,
    ValidationError,
)
from tgdrive.storage.demo_files import DEMO_FILES_ID, DEMO_FILES_NAME, check_ids
from tgdrive.storage.file_parts import validate_parts, validate_poster, validate_sha256

SCHEMA_VERSION = 4
ROOT_ID = "root"
MAX_NAME_LENGTH = 255
MAX_STORE_BYTES = 64 * 1024 * 1024
MAX_TREE_DEPTH = 128
MAX_TREE_ITEMS = 250_000
MAX_FOLDER_TREE_ITEMS = 1000
MAX_FOLDER_TREE_DEPTH = 64
MAX_SEARCH_RESULTS = 500
MAX_SEARCH_OFFSET = MAX_TREE_ITEMS
# Bound independent capabilities without evicting somebody else's link.
MAX_SHARE_TOKENS_PER_ITEM = 100
_SHARE_UNSET = object()

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("upload_date must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("upload_date must be valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError("upload_date must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _share_label(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 80
        or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)
    ):
        raise ValidationError(
            "Link label must be at most 80 characters without controls"
        )
    return unicodedata.normalize("NFC", value).strip()


def _validate_id(value: object, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValidationError("item id must contain only URL-safe opaque characters")
    if value == ROOT_ID and not allow_root:
        raise ValidationError("the reserved root id cannot be used by a child")
    return value


def _validate_channel_id(value: object) -> int:
    if type(value) is not int or value == 0 or not -(2**63) <= value < 2**63:
        raise ValidationError("storage_channel_id must be a non-zero 64-bit integer")
    return value


def _normalize_name(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidNameError("name must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized in {".", ".."}:
        raise InvalidNameError("name cannot be empty, '.' or '..'")
    if normalized != normalized.strip():
        raise InvalidNameError("name cannot start or end with whitespace")
    if len(normalized) > MAX_NAME_LENGTH:
        raise InvalidNameError(f"name cannot exceed {MAX_NAME_LENGTH} characters")
    if "/" in normalized or "\\" in normalized or "\x00" in normalized:
        raise InvalidNameError("name cannot contain path separators or NUL")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
        raise InvalidNameError("name cannot contain control characters or surrogates")
    return normalized


def _name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _validate_file_rename(current_name: str, new_name: str) -> None:
    """Keep an existing file suffix when changing its display name."""
    separator = current_name.rfind(".")
    if 0 < separator < len(current_name) - 1:
        extension = current_name[separator:]
        new_separator = new_name.rfind(".")
        new_extension = (
            new_name[new_separator:] if 0 < new_separator < len(new_name) - 1 else ""
        )
        if extension.lower() != new_extension.lower():
            raise InvalidNameError(
                f"The file extension cannot be changed. Keep the {extension} extension."
            )


def _validate_share_hashes(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError("share_hashes must be a list")
    hashes: list[str] = []
    for token_hash in value:
        if not isinstance(token_hash, str) or not _HASH_RE.fullmatch(token_hash):
            raise ValidationError("share token hashes must be lowercase SHA-256 hex")
        if token_hash in hashes:
            raise ValidationError("duplicate share token hash on item")
        hashes.append(token_hash)
    return hashes


def _strict_int(value: object, field_name: str, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= (1 << 63) - 1
    ):
        raise ValidationError(
            f"{field_name} must be a signed 64-bit integer >= {minimum}"
        )
    return value


@dataclass(slots=True)
class _FileNode:
    id: str
    name: str
    message_id: int
    size: int
    upload_date: str
    trashed: bool = False
    starred: bool = False
    share_hashes: list[str] = field(default_factory=list)
    parts: list[dict[str, Any]] | None = None
    sha256: str | None = None
    poster: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.id = _validate_id(self.id)
        self.name = _normalize_name(self.name)
        self.message_id = _strict_int(self.message_id, "message_id", 1)
        self.size = _strict_int(self.size, "size", 0)
        self.upload_date = _validate_timestamp(self.upload_date)
        if not isinstance(self.trashed, bool):
            raise ValidationError("trashed must be a boolean")
        if type(self.starred) is not bool:
            raise ValidationError("starred must be a boolean")
        self.share_hashes = _validate_share_hashes(self.share_hashes)
        self.sha256 = validate_sha256(self.sha256)
        if self.parts is not None:
            self.parts = validate_parts(
                self.parts, size=self.size, message_id=self.message_id
            )
        self.poster = validate_poster(
            self.poster, message_id=self.message_id, parts=self.parts
        )


@dataclass(slots=True)
class _FolderNode:
    id: str
    name: str
    upload_date: str
    trashed: bool = False
    starred: bool = False
    share_hashes: list[str] = field(default_factory=list)
    children: dict[str, _Node] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = _validate_id(self.id, allow_root=self.id == ROOT_ID)
        if self.id == ROOT_ID:
            if self.name != "/":
                raise ValidationError("root folder name must be '/'")
        else:
            self.name = _normalize_name(self.name)
        self.upload_date = _validate_timestamp(self.upload_date)
        if not isinstance(self.trashed, bool):
            raise ValidationError("trashed must be a boolean")
        if type(self.starred) is not bool:
            raise ValidationError("starred must be a boolean")
        self.share_hashes = _validate_share_hashes(self.share_hashes)
        if not isinstance(self.children, dict):
            raise ValidationError("folder children must be an object")


_Node = _FolderNode | _FileNode


def _parse_node(data: object, *, depth: int = 0) -> _Node:
    if depth > MAX_TREE_DEPTH:
        raise ValidationError(f"tree exceeds maximum depth of {MAX_TREE_DEPTH}")
    if not isinstance(data, dict):
        raise ValidationError("each tree node must be an object")
    item_type = data.get("type")
    common_keys = {
        "id",
        "name",
        "share_hashes",
        "trashed",
        "type",
        "upload_date",
    }
    if item_type == "file":
        expected = common_keys | {"message_id", "size"}
        optional = {"parts", "sha256", "starred", "poster"}
        if not expected <= set(data) or set(data) - expected - optional:
            raise ValidationError("file node has missing or unknown fields")
        if "parts" in data and not isinstance(data["parts"], list):
            raise ValidationError("file parts must be an array")
        if "sha256" in data:
            validate_sha256(data["sha256"], optional=False)
        return _FileNode(
            id=data["id"],
            name=data["name"],
            message_id=data["message_id"],
            size=data["size"],
            upload_date=data["upload_date"],
            trashed=data["trashed"],
            starred=data.get("starred", False),
            share_hashes=data["share_hashes"],
            parts=data.get("parts"),
            sha256=data.get("sha256"),
            poster=data.get("poster"),
        )
    if item_type == "folder":
        expected = common_keys | {"children"}
        optional = {"starred"}
        if not expected <= set(data) or set(data) - expected - optional:
            raise ValidationError("folder node has missing or unknown fields")
        raw_children = data["children"]
        if not isinstance(raw_children, dict):
            raise ValidationError("folder children must be an object")
        children: dict[str, _Node] = {}
        for child_id, raw_child in raw_children.items():
            if not isinstance(child_id, str):
                raise ValidationError("child map keys must be strings")
            child = _parse_node(raw_child, depth=depth + 1)
            if child.id != child_id:
                raise ValidationError("child map key does not match child id")
            children[child_id] = child
        return _FolderNode(
            id=data["id"],
            name=data["name"],
            upload_date=data["upload_date"],
            trashed=data["trashed"],
            starred=data.get("starred", False),
            share_hashes=data["share_hashes"],
            children=children,
        )
    raise ValidationError("node type must be 'file' or 'folder'")


def _validate_tree(root: _FolderNode) -> None:
    if root.id != ROOT_ID or root.name != "/" or root.trashed:
        raise ValidationError(
            "root must be an active folder with id 'root' and name '/'"
        )

    seen_ids: set[str] = {ROOT_ID}
    seen_hashes: set[str] = set()
    item_count = 1

    def visit(folder: _FolderNode, depth: int) -> None:
        nonlocal item_count
        if depth > MAX_TREE_DEPTH:
            raise ValidationError(f"tree exceeds maximum depth of {MAX_TREE_DEPTH}")
        sibling_names: set[str] = set()
        for child_id, child in folder.children.items():
            item_count += 1
            if item_count > MAX_TREE_ITEMS:
                raise ValidationError(
                    f"tree exceeds maximum item count of {MAX_TREE_ITEMS}"
                )
            if child_id != child.id:
                raise ValidationError("child map key does not match child id")
            if child.id in seen_ids:
                raise ValidationError(f"duplicate item id: {child.id}")
            seen_ids.add(child.id)
            key = _name_key(child.name)
            if not child.trashed and key in sibling_names:
                raise DuplicateNameError(
                    f"duplicate case-insensitive sibling name: {child.name}"
                )
            if not child.trashed:
                sibling_names.add(key)
            for token_hash in child.share_hashes:
                if token_hash in seen_hashes:
                    raise ValidationError(
                        "share token hash is assigned to multiple items"
                    )
                seen_hashes.add(token_hash)
            if isinstance(child, _FolderNode):
                visit(child, depth + 1)

    for token_hash in root.share_hashes:
        if token_hash in seen_hashes:
            raise ValidationError("duplicate root share token hash")
        seen_hashes.add(token_hash)
    visit(root, 0)


def _normalize_path(path: object) -> tuple[tuple[str, ...], str]:
    if not isinstance(path, str) or not path:
        raise InvalidPathError("path must be a non-empty string")
    if not path.startswith("/"):
        raise InvalidPathError("path must be absolute")
    if "\\" in path or "\x00" in path or "//" in path:
        raise InvalidPathError("path contains a forbidden separator or NUL")
    if path == "/":
        return (), "/"
    normalized = path[:-1] if path.endswith("/") else path
    parts = tuple(normalized[1:].split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise InvalidPathError("path contains an empty or traversal component")
    for part in parts:
        if not _ID_RE.fullmatch(part):
            raise InvalidPathError("path components must be item ids")
        if part == ROOT_ID:
            raise InvalidPathError("root is represented by '/', not '/root'")
    return parts, "/" + "/".join(parts)


def _join_path(parent_path: str, child_id: str) -> str:
    return f"/{child_id}" if parent_path == "/" else f"{parent_path}/{child_id}"


def _parent_path(full_path: str) -> str:
    if full_path == "/":
        return "/"
    parent = full_path.rsplit("/", 1)[0]
    return parent or "/"


class FolderTreeConflictError(ValidationError):
    """An idempotent folder preparation cannot safely be reused."""


def _folder_manifest(
    destination: object, root_name: object, folders: object
) -> dict[str, Any]:
    _, destination = _normalize_path(destination)
    root_name = _normalize_name(root_name)
    if not isinstance(folders, list) or len(folders) >= MAX_FOLDER_TREE_ITEMS:
        raise ValidationError(
            "folder tree supports at most 1000 folders including root"
        )
    paths: set[tuple[str, ...]] = set()
    siblings: set[tuple[tuple[str, ...], str]] = set()
    for segments in folders:
        if not isinstance(segments, list) or not segments:
            raise ValidationError(
                "relative folder paths must be non-empty segment arrays"
            )
        if len(segments) >= MAX_FOLDER_TREE_DEPTH:
            raise ValidationError("folder tree cannot exceed 64 levels including root")
        path = tuple(_normalize_name(segment) for segment in segments)
        key = (path[:-1], _name_key(path[-1]))
        if path in paths or key in siblings:
            raise DuplicateNameError(
                "folder tree has duplicate normalized sibling names"
            )
        paths.add(path)
        siblings.add(key)
    if any(path[:-1] and path[:-1] not in paths for path in paths):
        raise ValidationError("folder tree must include every ancestor folder")
    return {
        "destination": destination,
        "root_name": root_name,
        "folders": [
            list(path) for path in sorted(paths, key=lambda path: (len(path), path))
        ],
    }


def _manifest_json(manifest: dict[str, Any]) -> str:
    return json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


class DriveStore:
    """SQLite metadata whose paths are presentations of stable parent IDs."""

    DATABASE_VERSION = 7
    APPLICATION_ID = 0x54474456
    _DEFAULT_EXPIRY = object()

    def __init__(self, connection: sqlite3.Connection, path: Path | None) -> None:
        self._db = connection
        self._storage_path = path
        self._lock = threading.RLock()
        self._transaction_writes: list[bool] = []
        self._savepoint_serial = 0
        self._finalizer = weakref.finalize(self, connection.close)

    @staticmethod
    def _connect(path: Path | None) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(path) if path is not None else ":memory:",
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            if path is not None:
                connection.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            connection.close()
            raise
        return connection

    @classmethod
    def create(
        cls, path: str | os.PathLike[str] | None = None, *, overwrite: bool = False
    ) -> DriveStore:
        """Create a database; never unlink an existing database or its WAL."""
        target = Path(path) if path is not None else None
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(target)
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        connection = cls._connect(target)
        store = cls(connection, target)
        try:
            store._initialize_schema()
            with store._transaction(write=True) as db:
                db.execute(
                    "INSERT INTO nodes(id,parent_id,kind,name,name_key,upload_date) "
                    "VALUES(?,NULL,'folder','/','/',?)",
                    (ROOT_ID, _utc_now()),
                )
                db.execute(
                    "INSERT INTO metadata VALUES(1,1,0,?)", (secrets.token_urlsafe(16),)
                )
            if target is not None:
                store._fsync_directory(target.parent)
            return store
        except BaseException:
            connection.close()
            if target is not None:
                for candidate in (
                    target,
                    Path(str(target) + "-wal"),
                    Path(str(target) + "-shm"),
                ):
                    candidate.unlink(missing_ok=True)
            raise

    def _initialize_schema(self) -> None:
        self._db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE metadata (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                revision INTEGER NOT NULL CHECK(revision>=1),
                backed_up_revision INTEGER NOT NULL
                    CHECK(backed_up_revision>=0 AND backed_up_revision<=revision),
                drive_id TEXT NOT NULL
            );
            CREATE TABLE drive_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY,
                parent_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('file','folder')),
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                upload_date TEXT NOT NULL,
                trashed INTEGER NOT NULL DEFAULT 0 CHECK(trashed IN (0,1)),
                starred INTEGER NOT NULL DEFAULT 0 CHECK(starred IN (0,1)),
                message_id INTEGER,
                size INTEGER NOT NULL DEFAULT 0 CHECK(size>=0),
                part_count INTEGER NOT NULL DEFAULT 1
                    CHECK(part_count BETWEEN 1 AND 4096),
                sha256 TEXT CHECK(sha256 IS NULL OR
                    (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
                poster TEXT CHECK(poster IS NULL OR json_valid(poster)),
                CHECK((id='root' AND parent_id IS NULL AND kind='folder'
                       AND name='/' AND trashed=0)
                      OR (id!='root' AND parent_id IS NOT NULL)),
                CHECK((kind='file' AND message_id>0)
                      OR (kind='folder' AND message_id IS NULL AND size=0))
            );
            CREATE INDEX nodes_parent ON nodes(parent_id);
            CREATE UNIQUE INDEX visible_sibling_name ON nodes(parent_id,name_key)
                WHERE trashed=0;
            CREATE TABLE file_parts (
                node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                part_index INTEGER NOT NULL
                        CHECK(part_index>=0 AND part_index<4096),
                offset INTEGER NOT NULL CHECK(offset>=0),
                size INTEGER NOT NULL CHECK(size>0),
                message_id INTEGER NOT NULL CHECK(message_id>0),
                sha256 TEXT NOT NULL CHECK(length(sha256)=64
                    AND sha256 NOT GLOB '*[^0-9a-f]*'),
                document_id INTEGER CHECK(document_id IS NULL OR document_id!=0),
                PRIMARY KEY(node_id,part_index), UNIQUE(node_id,message_id)
            );
            CREATE TABLE file_operations (
                operation_id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL UNIQUE
            );
            CREATE TABLE folder_preparations (
                request_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                manifest TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE shares (
                id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                label TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX shares_node ON shares(node_id);
            CREATE TABLE deletion_intents (
                id TEXT PRIMARY KEY,
                root_id TEXT NOT NULL,
                path TEXT NOT NULL,
                channel_id INTEGER,
                state TEXT NOT NULL CHECK(state IN ('pending','failed','completed')),
                created_at TEXT NOT NULL,
                error TEXT
            );
            CREATE UNIQUE INDEX active_deletion_root ON deletion_intents(root_id)
                WHERE state!='completed';
            CREATE TABLE deletion_messages (
                intent_id TEXT NOT NULL REFERENCES deletion_intents(id)
                    ON DELETE CASCADE,
                message_id INTEGER NOT NULL CHECK(message_id>0),
                confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
                PRIMARY KEY(intent_id,message_id)
            );
            CREATE TRIGGER node_parent_is_folder BEFORE INSERT ON nodes
                WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS(
                    SELECT 1 FROM nodes WHERE id=NEW.parent_id AND kind='folder')
                BEGIN SELECT RAISE(ABORT,'parent must be a folder'); END;
            PRAGMA application_id=1413956694;
            PRAGMA user_version=7;
            COMMIT;
        """)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> DriveStore:
        """Open the catalog, adding optional poster storage to a valid v6 database."""
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(target)
        with target.open("rb") as handle:
            header = handle.read(16)
        if header != b"SQLite format 3\x00":
            raise CorruptStoreError("metadata is not a TGDrive SQLite database")
        connection = None
        try:
            connection = cls._connect(target)
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != cls.APPLICATION_ID
            ):
                raise CorruptStoreError("database is not a TGDrive database")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {6, cls.DATABASE_VERSION}:
                raise UnsupportedSchemaError(f"unsupported database schema {version}")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CorruptStoreError("SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CorruptStoreError("SQLite foreign key validation failed")
            store = cls(connection, target)
            for row in connection.execute("SELECT name,name_key FROM nodes"):
                if row["name_key"] != _name_key(row["name"]):
                    raise CorruptStoreError("node name index is inconsistent")
            validated = cls._from_payload(store.export())
            validated.close()
            if version == 6:
                with store._transaction(write=True) as db:
                    if db.execute("PRAGMA user_version").fetchone()[0] == 6:
                        db.execute(
                            "ALTER TABLE nodes ADD COLUMN poster TEXT "
                            "CHECK(poster IS NULL OR json_valid(poster))"
                        )
                        db.execute("PRAGMA user_version=7")
            return store
        except (
            sqlite3.Error,
            json.JSONDecodeError,
            ValidationError,
            KeyError,
            TypeError,
            RecursionError,
        ) as exc:
            if connection is not None:
                connection.close()
            raise CorruptStoreError(f"metadata validation failed: {exc}") from exc
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    @classmethod
    def _validate_payload(cls, payload: object) -> tuple[_FolderNode, dict[str, Any]]:
        try:
            if not isinstance(payload, dict):
                raise ValidationError("top-level metadata must be an object")
            required = {
                "backed_up_revision",
                "revision",
                "root",
                "schema_version",
                "drive_id",
                "shares",
            }
            optional = {
                "deletions",
                "operations",
                "folder_preparations",
                "storage_channel_id",
            }
            if not required <= set(payload) or set(payload) - required - optional:
                raise ValidationError(
                    "top-level metadata has missing or unknown fields"
                )
            version = payload["schema_version"]
            if type(version) is not int or version != SCHEMA_VERSION:
                raise UnsupportedSchemaError(f"unsupported metadata schema {version}")
            _validate_id(payload["drive_id"])
            if "storage_channel_id" in payload:
                _validate_channel_id(payload["storage_channel_id"])
            revision = _strict_int(payload["revision"], "revision", 1)
            backed_up = _strict_int(payload["backed_up_revision"], "backed_up_revision")
            if backed_up > revision:
                raise ValidationError("backed_up_revision cannot exceed revision")
            root = _parse_node(payload["root"])
            if not isinstance(root, _FolderNode):
                raise ValidationError("root node must be a folder")
            _validate_tree(root)
            cls._validate_records(root, payload)
            return root, payload
        except UnsupportedSchemaError:
            raise
        except (ValidationError, TypeError, RecursionError) as exc:
            raise CorruptStoreError(f"metadata validation failed: {exc}") from exc

    @classmethod
    def _validate_records(cls, root: _FolderNode, payload: dict[str, Any]) -> None:
        """Check recovery records against the tree without opening a database."""
        nodes, parents, expected_hashes = {}, {}, {}
        stack = [(root, None)]
        while stack:
            node, parent = stack.pop()
            nodes[node.id], parents[node.id] = node, parent
            expected_hashes.update((value, node.id) for value in node.share_hashes)
            if isinstance(node, _FolderNode):
                stack.extend((child, node.id) for child in node.children.values())

        shares = payload["shares"]
        if not isinstance(shares, list):
            raise ValidationError("shares must be a list")
        actual_hashes, share_ids = {}, set()
        for raw in shares:
            share = cls._validate_share_record(raw)
            if share["id"] in share_ids or share["token_hash"] in actual_hashes:
                raise ValidationError("duplicate share identity or credential")
            share_ids.add(share["id"])
            actual_hashes[share["token_hash"]] = share["node_id"]
        if actual_hashes != expected_hashes:
            raise ValidationError("share records do not match node credentials")

        operations = payload.get("operations", [])
        if not isinstance(operations, list):
            raise ValidationError("operations must be a list")
        operation_ids, operation_nodes = set(), set()
        for operation in operations:
            if not isinstance(operation, dict) or set(operation) != {
                "operation_id",
                "node_id",
            }:
                raise ValidationError("invalid file operation")
            identity = _validate_id(operation["operation_id"])
            node_id = _validate_id(operation["node_id"])
            if identity in operation_ids or node_id in operation_nodes:
                raise ValidationError("duplicate file operation")
            operation_ids.add(identity)
            operation_nodes.add(node_id)
            if node_id in nodes and not isinstance(nodes[node_id], _FileNode):
                raise ValidationError("file operation must reference a file")

        deletions = payload.get("deletions", [])
        if not isinstance(deletions, list):
            raise ValidationError("deletions must be a list")
        deletion_ids, active_roots = set(), set()
        for raw in deletions:
            intent = cls._validate_deletion_record(raw)
            if intent["id"] in deletion_ids:
                raise ValidationError("duplicate deletion identity")
            deletion_ids.add(intent["id"])
            node = nodes.get(intent["root_id"])
            if intent["state"] == "completed":
                if node is not None:
                    raise ValidationError("completed deletion still has a live root")
                continue
            if node is None or not node.trashed or node.id in active_roots:
                raise ValidationError("invalid active deletion root")
            active_roots.add(node.id)
            ancestry, parent = [node.id], parents[node.id]
            while parent != ROOT_ID:
                if parent is None or nodes[parent].trashed:
                    raise ValidationError("deletion root has invalid ancestry")
                ancestry.append(parent)
                parent = parents[parent]
            if "/" + "/".join(reversed(ancestry)) != intent["path"]:
                raise ValidationError("deletion root has invalid ancestry")
            messages, pending = set(), [node]
            while pending:
                current = pending.pop()
                if isinstance(current, _FolderNode):
                    pending.extend(current.children.values())
                else:
                    messages.add(current.message_id)
                    messages.update(part["message_id"] for part in current.parts or [])
                    if current.poster:
                        messages.add(current.poster["message_id"])
            if messages != set(intent["message_ids"]):
                raise ValidationError(
                    "deletion references do not match retained subtree"
                )
        for identity in active_roots:
            parent = parents[identity]
            while parent is not None:
                if parent in active_roots:
                    raise ValidationError("overlapping active deletion roots")
                parent = parents[parent]

        preparations = payload.get("folder_preparations", [])
        if not isinstance(preparations, list):
            raise ValidationError("folder_preparations must be a list")
        preparation_ids = set()
        for preparation in preparations:
            cls._validate_folder_preparation(preparation)
            if preparation["request_id"] in preparation_ids:
                raise ValidationError("duplicate folder preparation")
            preparation_ids.add(preparation["request_id"])

    @classmethod
    def _from_payload(
        cls,
        payload: object,
        *,
        storage_path: str | os.PathLike[str] | Path | None = None,
        persist: bool = False,
        overwrite: bool = False,
        revoke_shares: bool = False,
    ) -> DriveStore:
        root, data = cls._validate_payload(payload)
        if persist and storage_path is None:
            raise ValueError("storage_path is required when persist=True")
        target = Path(storage_path) if persist and storage_path is not None else None
        staged = None
        if target is not None:
            if target.exists():
                raise FileExistsError(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.parent / f".restore-{secrets.token_urlsafe(16)}.sqlite3"
        store = cls.create(staged)
        try:
            with store._transaction(write=True) as db:
                db.execute("DELETE FROM nodes")
                expected_hashes: dict[str, str] = {}
                stack: list[tuple[_Node, str | None]] = [(root, None)]
                while stack:
                    node, parent_id = stack.pop()
                    db.execute(
                        "INSERT INTO nodes(id,parent_id,kind,name,name_key,upload_date,"
                        "trashed,message_id,size,part_count,sha256,poster) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            node.id,
                            parent_id,
                            "file" if isinstance(node, _FileNode) else "folder",
                            node.name,
                            _name_key(node.name),
                            node.upload_date,
                            int(node.trashed),
                            node.message_id if isinstance(node, _FileNode) else None,
                            node.size if isinstance(node, _FileNode) else 0,
                            len(node.parts)
                            if isinstance(node, _FileNode) and node.parts
                            else 1,
                            node.sha256 if isinstance(node, _FileNode) else None,
                            json.dumps(node.poster)
                            if isinstance(node, _FileNode) and node.poster
                            else None,
                        ),
                    )
                    if node.starred:
                        db.execute("UPDATE nodes SET starred=1 WHERE id=?", (node.id,))
                    if isinstance(node, _FileNode) and node.parts:
                        store._insert_parts(db, node.id, node.parts)
                    for token_hash in node.share_hashes:
                        expected_hashes[token_hash] = node.id
                    if isinstance(node, _FolderNode):
                        stack.extend(
                            (child, node.id)
                            for child in reversed(list(node.children.values()))
                        )
                store._import_shares(db, data["shares"], expected_hashes)
                intents = data.get("deletions", [])
                if not isinstance(intents, list):
                    raise ValidationError("deletions must be a list")
                for intent in intents:
                    store._import_deletion_locked(db, intent)
                operations = data.get("operations", [])
                if not isinstance(operations, list):
                    raise ValidationError("operations must be a list")
                for operation in operations:
                    if not isinstance(operation, dict) or set(operation) != {
                        "operation_id",
                        "node_id",
                    }:
                        raise ValidationError("invalid file operation")
                    _validate_id(operation["operation_id"])
                    node = db.execute(
                        "SELECT kind FROM nodes WHERE id=?", (operation["node_id"],)
                    ).fetchone()
                    _validate_id(operation["node_id"])
                    if node is not None and node["kind"] != "file":
                        raise ValidationError("file operation must reference a file")
                    db.execute(
                        "INSERT INTO file_operations VALUES(?,?)",
                        (operation["operation_id"], operation["node_id"]),
                    )
                preparations = data.get("folder_preparations", [])
                if not isinstance(preparations, list):
                    raise ValidationError("folder_preparations must be a list")
                for preparation in preparations:
                    store._import_folder_preparation(db, preparation)
                if "storage_channel_id" in data:
                    db.execute(
                        "INSERT INTO drive_settings VALUES('storage_channel_id',?)",
                        (str(data["storage_channel_id"]),),
                    )
                revision = data["revision"]
                if revoke_shares:
                    # Recovery must not resurrect credentials revoked since the
                    # checkpoint. Validate their records first, then remove them
                    # inside the import transaction before publishing this file.
                    removed = db.execute("DELETE FROM shares").rowcount
                    if removed:
                        revision = _strict_int(revision + 1, "revision", 1)
                db.execute(
                    "UPDATE metadata SET revision=?,backed_up_revision=?,drive_id=?",
                    (
                        revision,
                        data["backed_up_revision"],
                        data["drive_id"],
                    ),
                )
            if target is not None and staged is not None:
                # Close checkpoints the WAL before publication. A crash while
                # importing leaves no valid-looking empty destination database.
                store.close()
                descriptor = os.open(staged, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.link(staged, target)
                cls._fsync_directory(target.parent)
                staged.unlink()
                return cls.load(target)
            return store
        except BaseException as exc:
            store.close()
            if staged is not None:
                for candidate in (
                    staged,
                    Path(str(staged) + "-wal"),
                    Path(str(staged) + "-shm"),
                ):
                    candidate.unlink(missing_ok=True)
            if isinstance(exc, (ValidationError, PersistenceError)):
                raise CorruptStoreError(f"metadata validation failed: {exc}") from exc
            raise

    @staticmethod
    def _validate_share_record(share: object) -> dict[str, Any]:
        if not isinstance(share, dict) or set(share) - {"label"} != {
            "id",
            "node_id",
            "token_hash",
            "created_at",
            "expires_at",
        }:
            raise ValidationError("invalid share record")
        _validate_id(share["id"])
        _validate_id(share["node_id"], allow_root=True)
        _validate_share_hashes([share["token_hash"]])
        created = _validate_timestamp(share["created_at"])
        expiry = (
            _validate_timestamp(share["expires_at"])
            if share["expires_at"] is not None
            else None
        )
        if expiry is not None and expiry <= created:
            raise ValidationError("invalid share expiry")
        return dict(
            share,
            created_at=created,
            expires_at=expiry,
            label=_share_label(share.get("label", "")),
        )

    def _import_shares(
        self, db: sqlite3.Connection, shares: object, expected_hashes: dict[str, str]
    ) -> None:
        if not isinstance(shares, list):
            raise ValidationError("shares must be a list")
        actual_hashes = {}
        for raw in shares:
            share = self._validate_share_record(raw)
            if share["token_hash"] in actual_hashes:
                raise ValidationError("duplicate share hash")
            actual_hashes[share["token_hash"]] = share["node_id"]
            db.execute(
                "INSERT INTO shares VALUES(?,?,?,?,?,?)",
                (
                    share["id"],
                    share["node_id"],
                    share["token_hash"],
                    share["created_at"],
                    share["expires_at"],
                    share["label"],
                ),
            )
        if actual_hashes != expected_hashes:
            raise ValidationError("share records do not match node credentials")

    @property
    def storage_path(self) -> Path | None:
        return self._storage_path

    @property
    def database_path(self) -> Path | None:
        return self._storage_path

    def close(self) -> None:
        with self._lock:
            self._finalizer()

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            nested = bool(self._transaction_writes)
            if nested and write and not self._transaction_writes[0]:
                raise PersistenceError("A read transaction cannot become writable")
            savepoint = None
            if nested:
                self._savepoint_serial += 1
                savepoint = f"tgdrive_{self._savepoint_serial}"
            entered = False
            try:
                self._db.execute(
                    f"SAVEPOINT {savepoint}"
                    if nested
                    else "BEGIN IMMEDIATE"
                    if write
                    else "BEGIN"
                )
                self._transaction_writes.append(write)
                entered = True
                yield self._db
                if nested:
                    self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._db.commit()
            except BaseException as exc:
                with suppress(sqlite3.Error):
                    if nested and entered:
                        self._db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
                    elif not nested:
                        self._db.rollback()
                if isinstance(exc, sqlite3.Error):
                    raise PersistenceError(f"SQLite transaction failed: {exc}") from exc
                raise
            finally:
                if entered:
                    self._transaction_writes.pop()

    @contextmanager
    def metadata_batch(self) -> Iterator[None]:
        """Group catalog mutations atomically; never await or do remote effects here."""
        with self._transaction(write=True):
            yield

    @staticmethod
    def _bump(db: sqlite3.Connection) -> None:
        db.execute("UPDATE metadata SET revision=revision+1 WHERE singleton=1")

    @property
    def drive_id(self) -> str:
        with self._transaction() as db:
            return _validate_id(
                db.execute(
                    "SELECT drive_id FROM metadata WHERE singleton=1"
                ).fetchone()[0]
            )

    @property
    def storage_channel_id(self) -> int | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT value FROM drive_settings WHERE key='storage_channel_id'"
            ).fetchone()
            if row is None:
                return None
            return _validate_channel_id(json.loads(row[0]))

    def bind_storage_channel(self, channel_id: int) -> None:
        """Bind message IDs to their channel once; never silently rebind them."""
        channel_id = _validate_channel_id(channel_id)
        with self._transaction(write=True) as db:
            row = db.execute(
                "SELECT value FROM drive_settings WHERE key='storage_channel_id'"
            ).fetchone()
            if row is not None:
                if _validate_channel_id(json.loads(row[0])) != channel_id:
                    raise ValidationError("drive is bound to another storage channel")
                return
            db.execute(
                "INSERT INTO drive_settings VALUES('storage_channel_id',?)",
                (str(channel_id),),
            )
            self._bump(db)

    def has_file_operation(
        self, operation_id: str, *, message_id: int | None = None
    ) -> bool:
        """Include tombstones by default; a message filter matches existing files."""
        _validate_id(operation_id)
        if message_id is not None:
            message_id = _strict_int(message_id, "message_id", 1)
        with self._transaction() as db:
            return (
                db.execute(
                    "SELECT 1 FROM file_operations LEFT JOIN nodes ON "
                    "nodes.id=file_operations.node_id WHERE operation_id=? "
                    "AND (? IS NULL OR nodes.message_id=?)",
                    (operation_id, message_id, message_id),
                ).fetchone()
                is not None
            )

    @property
    def revision(self) -> int:
        with self._transaction() as db:
            return db.execute(
                "SELECT revision FROM metadata WHERE singleton=1"
            ).fetchone()[0]

    @property
    def dirty(self) -> bool:
        with self._transaction() as db:
            row = db.execute(
                "SELECT revision,backed_up_revision FROM metadata WHERE singleton=1"
            ).fetchone()
            return row[1] < row[0]

    def _payload_locked(self, db: sqlite3.Connection) -> dict[str, Any]:
        rows = db.execute("SELECT * FROM nodes ORDER BY rowid").fetchall()
        if len(rows) > MAX_TREE_ITEMS:
            raise CorruptStoreError("metadata contains too many nodes")
        nodes: dict[str, dict[str, Any]] = {}
        shares = [
            {key: value for key, value in dict(row).items() if key != "label" or value}
            for row in db.execute("SELECT * FROM shares ORDER BY rowid")
        ]
        parts_by_node: dict[str, list[dict[str, Any]]] = {}
        for part in db.execute("SELECT * FROM file_parts ORDER BY node_id,part_index"):
            parts_by_node.setdefault(part["node_id"], []).append(
                {
                    "index": part["part_index"],
                    "offset": part["offset"],
                    "size": part["size"],
                    "message_id": part["message_id"],
                    "sha256": part["sha256"],
                }
                | (
                    {"document_id": part["document_id"]}
                    if part["document_id"] is not None
                    else {}
                )
            )
        for row in rows:
            node = dict(
                id=row["id"],
                name=row["name"],
                upload_date=row["upload_date"],
                trashed=bool(row["trashed"]),
                share_hashes=[],
                type=row["kind"],
            )
            if row["starred"]:
                node["starred"] = True
            if row["kind"] == "file":
                node.update(message_id=row["message_id"], size=row["size"])
                parts = parts_by_node.pop(row["id"], None)
                if row["part_count"] != (len(parts) if parts else 1):
                    raise CorruptStoreError("file part count is inconsistent")
                if parts:
                    node["parts"] = parts
                if row["sha256"] is not None:
                    node["sha256"] = row["sha256"]
                # v6 catalogs are validated before adding the nullable column.
                if dict(row).get("poster") is not None:
                    node["poster"] = json.loads(row["poster"])
            else:
                node["children"] = {}
            nodes[row["id"]] = node
        if parts_by_node:
            raise CorruptStoreError("file parts reference a folder or missing file")
        for share in shares:
            nodes[share["node_id"]]["share_hashes"].append(share["token_hash"])
        roots = []
        for row in rows:
            if row["parent_id"] is None:
                roots.append(row["id"])
            else:
                nodes[row["parent_id"]]["children"][row["id"]] = nodes[row["id"]]
        if roots != [ROOT_ID]:
            raise CorruptStoreError("database must have exactly one root")
        if len(self._subtree_ids(db, ROOT_ID)) != len(rows):
            raise CorruptStoreError("database contains unreachable or cyclic nodes")
        metadata = db.execute("SELECT * FROM metadata WHERE singleton=1").fetchone()
        if metadata is None:
            raise CorruptStoreError("database metadata is missing")
        payload = dict(
            schema_version=SCHEMA_VERSION,
            drive_id=_validate_id(metadata["drive_id"]),
            revision=metadata["revision"],
            backed_up_revision=metadata["backed_up_revision"],
            root=nodes[ROOT_ID],
            shares=shares,
            operations=[
                dict(row)
                for row in db.execute("SELECT * FROM file_operations ORDER BY rowid")
            ],
            deletions=[
                self._deletion_record(db, row)
                for row in db.execute("SELECT * FROM deletion_intents ORDER BY rowid")
            ],
        )

        binding = db.execute(
            "SELECT value FROM drive_settings WHERE key='storage_channel_id'"
        ).fetchone()
        if binding is not None:
            payload["storage_channel_id"] = _validate_channel_id(json.loads(binding[0]))
        preparations = [
            {
                "request_id": row["request_id"],
                "fingerprint": row["fingerprint"],
                "manifest": json.loads(row["manifest"]),
                "result": json.loads(row["result"]),
                "created_at": row["created_at"],
            }
            for row in db.execute("SELECT * FROM folder_preparations ORDER BY rowid")
        ]
        if preparations:
            payload["folder_preparations"] = preparations
        return payload

    def statistics(self) -> dict[str, int]:
        with self._transaction() as db:
            row = db.execute(
                "WITH RECURSIVE tree(id,kind,size,hidden) AS ("
                "SELECT id,kind,size,trashed FROM nodes WHERE id='root' UNION ALL "
                "SELECT n.id,n.kind,n.size,(n.trashed OR t.hidden) "
                "FROM nodes n JOIN tree t ON n.parent_id=t.id) "
                "SELECT COALESCE(SUM(kind='file' AND NOT hidden),0), "
                "COALESCE(SUM(kind='folder' AND id!='root' AND NOT hidden),0), "
                "COALESCE(SUM(CASE WHEN kind='file' AND NOT hidden "
                "THEN size ELSE 0 END),0), "
                "COALESCE(SUM(kind='file' AND hidden),0), "
                "COALESCE(SUM(CASE WHEN kind='file' AND hidden "
                "THEN size ELSE 0 END),0) "
                "FROM tree"
            ).fetchone()
            shares = db.execute("SELECT COUNT(*) FROM shares").fetchone()[0]
            return dict(
                zip(
                    (
                        "files",
                        "folders",
                        "total_bytes",
                        "trashed_files",
                        "trashed_bytes",
                    ),
                    row,
                    strict=True,
                )
            ) | {"shares": shares}

    def export(self) -> dict[str, Any]:
        with self._transaction() as db:
            return self._payload_locked(db)

    @staticmethod
    def _json_bytes(payload: dict[str, Any]) -> bytes:
        content = (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if len(content) > MAX_STORE_BYTES:
            raise PersistenceError("metadata snapshot exceeds the safe size limit")
        return content

    def snapshot(self) -> tuple[int, bytes]:
        payload = self.export()
        return payload["revision"], self._json_bytes(payload)

    def backup_snapshot(self) -> tuple[int, bytes]:
        payload = self.export()
        payload["backed_up_revision"] = payload["revision"]
        return payload["revision"], self._json_bytes(payload)

    def mark_backed_up(self, revision: int) -> bool:
        with self._transaction(write=True) as db:
            row = db.execute("SELECT * FROM metadata WHERE singleton=1").fetchone()
            if type(revision) is not int or not 0 <= revision <= row["revision"]:
                raise InvalidRevisionError(
                    f"backup revision must be between 0 and {row['revision']}"
                )
            db.execute(
                "UPDATE metadata SET backed_up_revision=max(backed_up_revision,?)",
                (revision,),
            )
            return max(revision, row["backed_up_revision"]) < row["revision"]

    @staticmethod
    def _resolve(
        db: sqlite3.Connection, path: str
    ) -> tuple[sqlite3.Row, str, list[sqlite3.Row]]:
        parts, normalized = _normalize_path(path)
        node = db.execute("SELECT * FROM nodes WHERE id=?", (ROOT_ID,)).fetchone()
        if node is None:
            raise CorruptStoreError("root is missing")
        chain = [node]
        for part in parts:
            if node["kind"] != "folder":
                raise NotDirectoryError(f"path component is not a folder: {node['id']}")
            node = db.execute(
                "SELECT * FROM nodes WHERE id=? AND parent_id=?", (part, node["id"])
            ).fetchone()
            if node is None:
                raise NotFoundError(f"item not found: {normalized}")
            chain.append(node)
        return node, normalized, chain

    @staticmethod
    def _visible(chain: list[sqlite3.Row], include_trashed: bool = False) -> None:
        if not include_trashed and any(row["trashed"] for row in chain):
            raise TrashedItemError("item or one of its ancestors is trashed")

    @staticmethod
    def _mutable(
        db: sqlite3.Connection, chain: list[sqlite3.Row], *, descendants: bool = False
    ) -> None:
        DriveStore._demo_mutable(db, chain, descendants=descendants)
        roots = {
            row[0]
            for row in db.execute(
                "SELECT root_id FROM deletion_intents WHERE state!='completed'"
            )
        }
        if roots.intersection(row["id"] for row in chain) or (
            descendants
            and roots.intersection(DriveStore._subtree_ids(db, chain[-1]["id"]))
        ):
            raise ValidationError("item is pending permanent deletion")

    @staticmethod
    def _demo_mutable(db, chain, *, descendants=False):
        from tgdrive.storage.demo_files import restricted_writes

        if not restricted_writes.get():
            return
        check_ids(row["id"] for row in chain)
        if descendants:
            check_ids(DriveStore._subtree_ids(db, chain[-1]["id"]))

    @staticmethod
    def _record(
        row: sqlite3.Row, full_path: str, *, message: bool = False
    ) -> dict[str, Any]:
        result = dict(
            id=row["id"],
            name=row["name"],
            path=_parent_path(full_path),
            size=row["size"],
            type=row["kind"],
            upload_date=row["upload_date"],
        )
        if "starred" in row.keys() and row["starred"]:  # noqa: SIM118 - sqlite Row membership checks values
            result["starred"] = True
        if row["kind"] == "file":
            result["part_count"] = row["part_count"]
        if message:
            result["message_id"] = row["message_id"]
            if row["sha256"] is not None:
                result["sha256"] = row["sha256"]
        return result

    @classmethod
    def _file_record(
        cls, db: sqlite3.Connection, row: sqlite3.Row, path: str
    ) -> dict[str, Any]:
        result = cls._record(row, path, message=True)
        if row["poster"] is not None:
            result["poster"] = json.loads(row["poster"])
        parts = [
            {
                "index": item["part_index"],
                "offset": item["offset"],
                "size": item["size"],
                "message_id": item["message_id"],
                "sha256": item["sha256"],
            }
            | (
                {"document_id": item["document_id"]}
                if item["document_id"] is not None
                else {}
            )
            for item in db.execute(
                "SELECT * FROM file_parts WHERE node_id=? ORDER BY part_index",
                (row["id"],),
            )
        ]
        if parts:
            result["parts"] = parts
        return result

    @staticmethod
    def _insert_parts(
        db: sqlite3.Connection, item_id: str, parts: list[dict[str, Any]]
    ) -> None:
        db.executemany(
            "INSERT INTO file_parts(node_id,part_index,offset,size,message_id,"
            "sha256,document_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                (
                    item_id,
                    part["index"],
                    part["offset"],
                    part["size"],
                    part["message_id"],
                    part["sha256"],
                    part.get("document_id"),
                )
                for part in parts
            ),
        )

    @staticmethod
    def _path_for_id(db: sqlite3.Connection, item_id: str) -> str:
        parts = []
        visited = set()
        while item_id != ROOT_ID:
            if item_id in visited or len(parts) > MAX_TREE_DEPTH:
                raise CorruptStoreError("invalid node ancestry")
            visited.add(item_id)
            row = db.execute(
                "SELECT id,parent_id FROM nodes WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("item does not exist")
            parts.append(row["id"])
            item_id = row["parent_id"]
        return "/" + "/".join(reversed(parts))

    def resolve_id(self, item_id: str) -> str:
        _validate_id(item_id, allow_root=True)
        with self._transaction() as db:
            return self._path_for_id(db, item_id)

    def transfer_location(self, operation_id, destination="/", *, completed=False):
        """Resolve an upload's exact current item, never a same-named sibling."""
        if operation_id is not None:
            _validate_id(operation_id)
        with self._transaction() as db:
            operation = (
                db.execute(
                    "SELECT node_id FROM file_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if operation_id is not None
                else None
            )
            if operation is not None:
                path = self._path_for_id(db, operation["node_id"])
            elif completed:
                raise NotFoundError("Uploaded item is unavailable")
            else:
                parts, _ = _normalize_path(destination)
                path = self._path_for_id(db, parts[-1] if parts else ROOT_ID)
            node, path, chain = self._resolve(db, path)
            self._visible(chain)
            from tgdrive.transfers.transfer_locations import location_record

            return location_record(node, path, chain, stored=operation is not None)

    def upload_destination(
        self,
        path: str,
        *,
        directory_id: str | None = None,
    ) -> dict[str, str]:
        """Bind a writable folder by its current path or a persisted identity."""
        with self._transaction() as db:
            if directory_id is not None:
                _validate_id(directory_id, allow_root=True)
                path = self._path_for_id(db, directory_id)
            node, path, chain = self._resolve(db, path)
            if node["kind"] != "folder":
                raise NotDirectoryError("upload destination must be a folder")
            self._visible(chain)
            self._mutable(db, chain)
            return {"id": node["id"], "path": path}

    @staticmethod
    def _subtree_ids(db: sqlite3.Connection, root_id: str) -> list[str]:
        return [
            row[0]
            for row in db.execute(
                "WITH RECURSIVE subtree(id) AS (SELECT id FROM nodes WHERE id=? "
                "UNION SELECT nodes.id FROM nodes JOIN subtree ON "
                "nodes.parent_id=subtree.id) "
                "SELECT id FROM subtree",
                (root_id,),
            )
        ]

    @staticmethod
    def _unique(
        db: sqlite3.Connection, parent_id: str, name: str, exclude_id: str = ""
    ) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM nodes WHERE parent_id=? AND name_key=? AND id!=? AND "
                "trashed=0",
                (parent_id, _name_key(name), exclude_id),
            ).fetchone()
            is None
        )

    def _available_name(
        self,
        db: sqlite3.Connection,
        parent_id: str,
        name: str,
        *,
        kind: str,
        exclude_id: str = "",
    ) -> str:
        if self._unique(db, parent_id, name, exclude_id):
            return name
        suffix = Path(name).suffix if kind == "file" else ""
        stem = name[: -len(suffix)] if suffix else name
        index = 1
        while True:
            label = f" ({index})"
            ending = suffix[: MAX_NAME_LENGTH - len(label) - 1]
            candidate = (
                stem[: MAX_NAME_LENGTH - len(label) - len(ending)] + label + ending
            )
            if self._unique(db, parent_id, candidate, exclude_id):
                return candidate
            index += 1

    def _ensure_unique(
        self, db: sqlite3.Connection, parent_id: str, name: str, exclude_id: str = ""
    ) -> None:
        if not self._unique(db, parent_id, name, exclude_id):
            raise DuplicateNameError(
                f"an item named '{name}' already exists in this folder"
            )

    def _children(
        self, db: sqlite3.Connection, node_id: str, path: str, include_trashed: bool
    ) -> dict[str, dict[str, Any]]:
        return {
            row["id"]: self._record(row, _join_path(path, row["id"]))
            for row in db.execute(
                "SELECT * FROM nodes WHERE parent_id=? ORDER BY rowid", (node_id,)
            )
            if include_trashed or not row["trashed"]
        }

    def get_directory(
        self, path: str, *, include_trashed: bool = False
    ) -> dict[str, Any]:
        with self._transaction() as db:
            node, normalized, chain = self._resolve(db, path)
            if node["kind"] != "folder":
                raise NotDirectoryError(f"not a directory: {normalized}")
            self._visible(chain, include_trashed)
            result = self._record(node, normalized)
            result["contents"] = self._children(
                db, node["id"], normalized, include_trashed
            )
            return result

    def list_directory(
        self, path: str, *, include_trashed: bool = False
    ) -> dict[str, dict[str, Any]]:
        return self.get_directory(path, include_trashed=include_trashed)["contents"]

    def get_file(self, path: str, *, include_trashed: bool = False) -> dict[str, Any]:
        with self._transaction() as db:
            node, normalized, chain = self._resolve(db, path)
            if node["kind"] != "file":
                raise NotFileError(f"not a file: {normalized}")
            self._visible(chain, include_trashed)
            return self._file_record(db, node, normalized)

    def _add(
        self,
        parent_path: str,
        name: str,
        message_id: int | None,
        size: int = 0,
        operation_id: str | None = None,
        keep_both: bool = False,
        parts: list[dict[str, Any]] | None = None,
        sha256: str | None = None,
        poster: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = _normalize_name(name)
        if operation_id is not None:
            _validate_id(operation_id)
        with self._transaction(write=True) as db:
            if operation_id is not None:
                committed = db.execute(
                    "SELECT node_id FROM file_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if (
                    committed is not None
                    and db.execute(
                        "SELECT 1 FROM nodes WHERE id=?", (committed["node_id"],)
                    ).fetchone()
                    is None
                ):
                    raise ValidationError(
                        "operation_id was completed and its file was deleted"
                    )
                existing = db.execute(
                    "SELECT nodes.* FROM nodes JOIN file_operations ON "
                    "nodes.id=file_operations.node_id WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if existing is not None:
                    if existing["message_id"] != message_id or existing["size"] != size:
                        raise ValidationError(
                            "operation_id already committed different media"
                        )
                    result = self._file_record(
                        db, existing, self._path_for_id(db, existing["id"])
                    )
                    if (
                        result.get("parts") != parts
                        or result.get("sha256") != sha256
                        or result.get("poster") != poster
                    ):
                        raise ValidationError(
                            "operation_id already committed different file parts"
                        )
                    return result
            parent, parent_path, chain = self._resolve(db, parent_path)
            if parent["kind"] != "folder":
                raise NotDirectoryError("destination must be a folder")
            self._visible(chain)
            self._mutable(db, chain)
            if len(chain) > MAX_TREE_DEPTH:
                raise ValidationError("maximum tree depth exceeded")
            if db.execute("SELECT count(*) FROM nodes").fetchone()[0] >= MAX_TREE_ITEMS:
                raise ValidationError("maximum item count exceeded")
            if keep_both:
                name = self._available_name(
                    db,
                    parent["id"],
                    name,
                    kind="file" if message_id is not None else "folder",
                )
            else:
                self._ensure_unique(db, parent["id"], name)
            item_id = secrets.token_urlsafe(16)
            db.execute(
                "INSERT INTO "
                "nodes(id,parent_id,kind,name,name_key,upload_date,message_id,"
                "size,part_count,sha256,poster) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    parent["id"],
                    "folder" if message_id is None else "file",
                    name,
                    _name_key(name),
                    _utc_now(),
                    message_id,
                    size,
                    len(parts) if parts else 1,
                    sha256,
                    json.dumps(poster) if poster else None,
                ),
            )
            if parts:
                self._insert_parts(db, item_id, parts)
            if operation_id is not None:
                db.execute(
                    "INSERT INTO file_operations VALUES(?,?)", (operation_id, item_id)
                )
            self._bump(db)
            row = db.execute("SELECT * FROM nodes WHERE id=?", (item_id,)).fetchone()
            full_path = _join_path(parent_path, item_id)
            return (
                self._file_record(db, row, full_path)
                if message_id is not None
                else self._record(row, full_path)
            )

    @staticmethod
    def _validate_folder_preparation(record: object) -> dict[str, Any]:
        if not isinstance(record, dict) or set(record) != {
            "request_id",
            "fingerprint",
            "manifest",
            "result",
            "created_at",
        }:
            raise ValidationError("invalid folder preparation")
        request_id = _validate_id(record["request_id"])
        if len(request_id) < 16:
            raise ValidationError(
                "folder request id must contain at least 16 characters"
            )
        manifest = record["manifest"]
        if not isinstance(manifest, dict) or set(manifest) != {
            "destination",
            "root_name",
            "folders",
        }:
            raise ValidationError("invalid folder preparation manifest")
        normalized = _folder_manifest(**manifest)
        if normalized != manifest:
            raise ValidationError("folder preparation manifest is not canonical")
        encoded = _manifest_json(normalized)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        if record["fingerprint"] != fingerprint:
            raise ValidationError("folder preparation fingerprint does not match")
        result = record["result"]
        if not isinstance(result, dict) or set(result) != {"root", "folders"}:
            raise ValidationError("invalid folder preparation result")
        root = result["root"]
        if not isinstance(root, dict) or set(root) != {
            "id",
            "name",
            "path",
            "size",
            "type",
            "upload_date",
        }:
            raise ValidationError("invalid prepared root")
        _validate_id(root["id"])
        if (
            _normalize_name(root["name"]) != root["name"]
            or root["path"] != normalized["destination"]
            or type(root["size"]) is not int
            or root["size"] != 0
            or root["type"] != "folder"
        ):
            raise ValidationError("invalid prepared root fields")
        _validate_timestamp(root["upload_date"])
        mapping = result["folders"]
        expected = {"", *("/".join(path) for path in normalized["folders"])}
        if not isinstance(mapping, dict) or set(mapping) != expected:
            raise ValidationError("prepared folder map does not match manifest")
        if mapping[""] != _join_path(root["path"], root["id"]):
            raise ValidationError("prepared root path does not match")
        ids = {root["id"]}
        for segments in normalized["folders"]:
            relative = "/".join(segments)
            parts, full_path = _normalize_path(mapping[relative])
            parent = mapping["/".join(segments[:-1])]
            if (
                not parts
                or full_path != mapping[relative]
                or full_path != _join_path(parent, parts[-1])
                or parts[-1] in ids
            ):
                raise ValidationError("invalid prepared folder ancestry")
            ids.add(parts[-1])
        _validate_timestamp(record["created_at"])
        return record

    @staticmethod
    def _import_folder_preparation(db: sqlite3.Connection, record: object) -> None:
        record = DriveStore._validate_folder_preparation(record)
        db.execute(
            "INSERT INTO folder_preparations VALUES(?,?,?,?,?)",
            (
                record["request_id"],
                record["fingerprint"],
                _manifest_json(record["manifest"]),
                _manifest_json(record["result"]),
                _validate_timestamp(record["created_at"]),
            ),
        )

    def prepare_folder_tree(
        self,
        destination: str,
        root_name: str,
        folders: list[list[str]],
        client_request_id: str,
    ) -> dict[str, Any]:
        """Prepare a fresh tree atomically and retain its safe retry result."""
        manifest = _folder_manifest(destination, root_name, folders)
        request_id = _validate_id(client_request_id)
        if len(request_id) < 16:
            raise ValidationError(
                "folder request id must contain at least 16 characters"
            )
        encoded = _manifest_json(manifest)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        destination = manifest["destination"]
        folders = manifest["folders"]
        with self._transaction(write=True) as db:
            prior = db.execute(
                "SELECT fingerprint,result FROM folder_preparations WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if prior is not None:
                if not hmac.compare_digest(prior["fingerprint"], fingerprint):
                    raise FolderTreeConflictError(
                        "This folder request id already prepared a different tree"
                    )
                result = json.loads(prior["result"])
                try:
                    for relative, path in result["folders"].items():
                        node, _, chain = self._resolve(db, path)
                        self._visible(chain)
                        self._mutable(db, chain)
                        name = (
                            relative.rsplit("/", 1)[-1]
                            if relative
                            else result["root"]["name"]
                        )
                        if node["kind"] != "folder" or node["name"] != name:
                            raise ValidationError("prepared folder changed")
                except (
                    NotFoundError,
                    NotDirectoryError,
                    ValidationError,
                    TrashedItemError,
                ) as exc:
                    raise FolderTreeConflictError(
                        "The prepared folder tree changed. "
                        "Review it before starting again"
                    ) from exc
                return result
            parent, destination, chain = self._resolve(db, destination)
            if parent["kind"] != "folder":
                raise NotDirectoryError("destination must be a folder")
            self._visible(chain)
            self._mutable(db, chain)
            count = 1 + len(folders)
            depth = 1 + max((len(path) for path in folders), default=0)
            if len(chain) - 1 + depth > MAX_TREE_DEPTH:
                raise ValidationError("maximum tree depth exceeded")
            if (
                db.execute("SELECT count(*) FROM nodes").fetchone()[0] + count
                > MAX_TREE_ITEMS
            ):
                raise ValidationError("maximum item count exceeded")
            root_name = self._available_name(
                db, parent["id"], manifest["root_name"], kind="folder"
            )
            created = _utc_now()
            mapping: dict[str, str] = {}
            root: dict[str, Any] | None = None
            for segments in [[], *folders]:
                relative = "/".join(segments)
                parent_path = (
                    mapping["/".join(segments[:-1])] if segments else destination
                )
                parent_id = (
                    parent_path.rsplit("/", 1)[-1] if parent_path != "/" else ROOT_ID
                )
                name = segments[-1] if segments else root_name
                item_id = secrets.token_urlsafe(16)
                db.execute(
                    "INSERT INTO nodes(id,parent_id,kind,name,name_key,upload_date) "
                    "VALUES(?,?,'folder',?,?,?)",
                    (item_id, parent_id, name, _name_key(name), created),
                )
                path = _join_path(parent_path, item_id)
                mapping[relative] = path
                if not segments:
                    node = db.execute(
                        "SELECT * FROM nodes WHERE id=?", (item_id,)
                    ).fetchone()
                    root = self._record(node, path)
            result = {"root": root, "folders": mapping}
            self._import_folder_preparation(
                db,
                {
                    "request_id": request_id,
                    "fingerprint": fingerprint,
                    "manifest": manifest,
                    "result": result,
                    "created_at": created,
                },
            )
            self._bump(db)
            return result

    def create_folder(self, parent_path: str, name: str) -> dict[str, Any]:
        return self._add(parent_path, name, None)

    def ensure_demo_files(self) -> dict[str, Any]:
        """Create the reserved demo folder once; preserve its stable identity."""
        with self._transaction(write=True) as db:
            node = db.execute(
                "SELECT * FROM nodes WHERE id=?", (DEMO_FILES_ID,)
            ).fetchone()
            if node is not None:
                if node["kind"] != "folder":
                    raise ValidationError(
                        "The reserved Demo Files identity is not a folder"
                    )
                return self._record(node, self._path_for_id(db, DEMO_FILES_ID))
            if not self._unique(db, ROOT_ID, DEMO_FILES_NAME):
                raise DuplicateNameError(
                    "A different item named Demo Files already exists. "
                    "Disable demo mode and rename that item before enabling it again."
                )
            self._mutable(db, self._resolve(db, "/")[2])
            if db.execute("SELECT count(*) FROM nodes").fetchone()[0] >= MAX_TREE_ITEMS:
                raise ValidationError("maximum item count exceeded")
            db.execute(
                "INSERT INTO nodes(id,parent_id,kind,name,name_key,upload_date) "
                "VALUES(?,?,?,?,?,?)",
                (
                    DEMO_FILES_ID,
                    ROOT_ID,
                    "folder",
                    DEMO_FILES_NAME,
                    _name_key(DEMO_FILES_NAME),
                    _utc_now(),
                ),
            )
            self._bump(db)
            return self._record(
                db.execute(
                    "SELECT * FROM nodes WHERE id=?", (DEMO_FILES_ID,)
                ).fetchone(),
                "/" + DEMO_FILES_ID,
            )

    def add_file(
        self,
        parent_path: str,
        name: str,
        message_id: int,
        size: int,
        *,
        operation_id: str | None = None,
        keep_both: bool = False,
        parts: list[dict[str, Any]] | None = None,
        sha256: str | None = None,
        poster: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if type(keep_both) is not bool:
            raise ValidationError("keep_both must be a boolean")
        message_id = _strict_int(message_id, "message_id", 1)
        size = _strict_int(size, "size")
        sha256 = validate_sha256(sha256)
        if parts is not None:
            parts = validate_parts(parts, size=size, message_id=message_id)
        poster = validate_poster(poster, message_id=message_id, parts=parts)
        return self._add(
            parent_path,
            name,
            message_id,
            size,
            operation_id,
            keep_both,
            parts,
            sha256,
            poster,
        )

    def rename(self, path: str, new_name: str) -> dict[str, Any]:
        name = _normalize_name(new_name)
        with self._transaction(write=True) as db:
            node, path, chain = self._resolve(db, path)
            if node["id"] == ROOT_ID:
                raise RootMutationError("the root folder cannot be renamed")
            self._demo_mutable(db, chain, descendants=True)
            self._mutable(db, chain)
            if node["kind"] == "file":
                _validate_file_rename(node["name"], name)
            self._ensure_unique(db, node["parent_id"], name, node["id"])
            if node["name"] != name:
                db.execute(
                    "UPDATE nodes SET name=?,name_key=? WHERE id=?",
                    (name, _name_key(name), node["id"]),
                )
                self._bump(db)
            return self._record(
                db.execute("SELECT * FROM nodes WHERE id=?", (node["id"],)).fetchone(),
                path,
            )

    def set_trashed_many(self, paths: list[str], trashed: bool) -> list[dict[str, Any]]:
        with self.metadata_batch():
            return [self.set_trashed(path, trashed) for path in paths]

    def set_trashed(self, path: str, trashed: bool) -> dict[str, Any]:
        if type(trashed) is not bool:
            raise ValidationError("trashed must be a boolean")
        with self._transaction(write=True) as db:
            node, path, chain = self._resolve(db, path)
            if node["id"] == ROOT_ID:
                raise RootMutationError("the root folder cannot be trashed")
            self._mutable(db, chain, descendants=True)
            if not trashed:
                self._visible(chain[:-1])
            if bool(node["trashed"]) != trashed:
                name = (
                    node["name"]
                    if trashed
                    else self._available_name(
                        db,
                        node["parent_id"],
                        node["name"],
                        kind=node["kind"],
                        exclude_id=node["id"],
                    )
                )
                db.execute(
                    "UPDATE nodes SET trashed=?,name=?,name_key=? WHERE id=?",
                    (int(trashed), name, _name_key(name), node["id"]),
                )
                if trashed:
                    self._revoke_subtree(db, node["id"])
                self._bump(db)
            updated = db.execute(
                "SELECT * FROM nodes WHERE id=?", (node["id"],)
            ).fetchone()
            return self._record(updated, path)

    def list_trash(self, query: str = "") -> dict[str, dict[str, Any]]:
        with self._transaction() as db:
            result = {}
            for row in db.execute(
                "SELECT * FROM nodes WHERE trashed=1 AND id NOT IN "
                "(SELECT root_id FROM deletion_intents WHERE state='pending') "
                "ORDER BY rowid"
            ):
                if query.casefold() not in row["name"].casefold():
                    continue
                path = self._path_for_id(db, row["id"])
                _, _, chain = self._resolve(db, path)
                if not any(item["trashed"] for item in chain[:-1]):
                    result[row["id"]] = self._record(row, path) | {
                        "parent_names": [item["name"] for item in chain[1:-1]]
                    }
            return result

    def set_starred(self, path: str, starred: bool) -> dict[str, Any]:
        if type(starred) is not bool:
            raise ValidationError("starred must be a boolean")
        with self._transaction(write=True) as db:
            node, path, chain = self._resolve(db, path)
            if node["id"] == ROOT_ID:
                raise RootMutationError("the drive root cannot be starred")
            self._mutable(db, chain)
            self._visible(chain)
            if bool(node["starred"]) != starred:
                db.execute(
                    "UPDATE nodes SET starred=? WHERE id=?", (int(starred), node["id"])
                )
                self._bump(db)
            row = db.execute("SELECT * FROM nodes WHERE id=?", (node["id"],)).fetchone()
            return self._record(row, path)

    def list_starred(self, query: str = "") -> dict[str, dict[str, Any]]:
        if not isinstance(query, str) or len(query) > MAX_NAME_LENGTH:
            raise ValidationError("invalid starred search")
        with self._transaction() as db:
            rows = db.execute(
                "WITH RECURSIVE visible(id,full_path,parent_names) AS ("
                "SELECT id,'','[]' FROM nodes WHERE id='root' UNION ALL "
                "SELECT n.id,v.full_path||'/'||n.id,CASE WHEN v.id='root' THEN '[]' "
                "ELSE json_insert(v.parent_names,'$[#]',p.name) END "
                "FROM nodes n JOIN visible v ON n.parent_id=v.id "
                "JOIN nodes p ON p.id=v.id WHERE n.trashed=0) "
                "SELECT n.*,v.full_path,v.parent_names FROM visible v "
                "JOIN nodes n ON n.id=v.id WHERE n.id!='root' AND n.starred=1 "
                "AND instr(n.name_key,?)>0 ORDER BY n.name_key,n.id",
                (_name_key(query.strip()),),
            ).fetchall()
            return {
                row["id"]: self._record(row, row["full_path"])
                | {"parent_names": json.loads(row["parent_names"])}
                for row in rows
            }

    def search(
        self, query: str, *, include_trashed: bool = False
    ) -> dict[str, dict[str, Any]]:
        """Compatibility lookup: at most 500 literal filename matches."""
        page = self.search_page(
            query, include_trashed=include_trashed, limit=MAX_SEARCH_RESULTS
        )
        return {row["id"]: row for row in page["items"]}

    def search_page(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = 100,
        sort: str = "name",
        order: str | None = None,
        include_trashed: bool = False,
    ) -> dict[str, Any]:
        """Search one consistent snapshot without per-result ancestry queries.

        ``instr`` treats SQL wildcard and quote characters literally. Names and
        the query use the same NFC/casefold key. The recursive walk removes an
        entire trashed subtree before matching or pagination, and builds human
        parent names alongside the opaque ID path in the same query.
        """
        if not isinstance(query, str):
            raise ValidationError("search query must be a string")
        query = unicodedata.normalize("NFC", query).strip()
        if (
            not query
            or len(query) > MAX_NAME_LENGTH
            or any(unicodedata.category(c) in {"Cc", "Cs"} for c in query)
        ):
            raise ValidationError(
                "search query is empty, too long, or contains forbidden characters"
            )
        if (
            type(offset) is not int
            or not 0 <= offset <= MAX_SEARCH_OFFSET
            or type(limit) is not int
            or not 1 <= limit <= MAX_SEARCH_RESULTS
            or type(include_trashed) is not bool
            or sort not in {"name", "date", "size"}
            or (
                order is not None
                and (not isinstance(order, str) or order not in {"asc", "desc"})
            )
        ):
            raise ValidationError("invalid search pagination or sort")
        # Secondary name/ID keys remain ascending so equal dates and sizes
        # paginate deterministically in either primary direction.
        direction = order or ("asc" if sort == "name" else "desc")
        with self._transaction() as db:
            rows = db.execute(
                "WITH RECURSIVE visible(id,name,full_path,parent_names) AS ("
                "SELECT id,name,'','[]' FROM nodes WHERE id='root' UNION ALL "
                "SELECT n.id,n.name,v.full_path||'/'||n.id,"
                "CASE WHEN v.id='root' THEN '[]' ELSE "
                "json_insert(v.parent_names,'$[#]',v.name) END "
                "FROM nodes n JOIN visible v ON n.parent_id=v.id "
                "WHERE (? OR n.trashed=0)) "
                "SELECT n.*,v.full_path,v.parent_names FROM visible v "
                "JOIN nodes n ON n.id=v.id WHERE n.id!='root' "
                "AND instr(n.name_key,?)>0 ORDER BY (n.kind='folder') DESC,"
                "CASE WHEN ?='asc' THEN CASE ? "
                "WHEN 'name' THEN n.name_key WHEN 'date' THEN n.upload_date "
                "ELSE n.size END END ASC,"
                "CASE WHEN ?='desc' THEN CASE ? "
                "WHEN 'name' THEN n.name_key WHEN 'date' THEN n.upload_date "
                "ELSE n.size END END DESC,"
                "n.name_key,n.id LIMIT ? OFFSET ?",
                (
                    int(include_trashed),
                    query.casefold(),
                    direction,
                    sort,
                    direction,
                    sort,
                    limit + 1,
                    offset,
                ),
            ).fetchall()
            items = [
                self._record(row, row["full_path"])
                | {"parent_names": json.loads(row["parent_names"])}
                for row in rows[:limit]
            ]
        has_more = len(rows) > limit
        return {
            "items": items,
            "query": query,
            "has_more": has_more,
            "next_offset": offset + len(items) if has_more else None,
        }

    def message_ids(self, path: str, *, include_trashed: bool = False) -> list[int]:
        with self._transaction() as db:
            node, _, chain = self._resolve(db, path)
            self._visible(chain, include_trashed)
            return self._messages(db, node["id"])

    @staticmethod
    def _messages(db: sqlite3.Connection, node_id: str) -> list[int]:
        return [
            row[0]
            for row in db.execute(
                "WITH RECURSIVE subtree(id) AS (SELECT id FROM nodes WHERE id=? "
                "UNION SELECT nodes.id FROM nodes JOIN subtree ON "
                "nodes.parent_id=subtree.id) "
                "SELECT message_id FROM nodes JOIN subtree USING(id) WHERE kind='file' "
                "UNION SELECT p.message_id FROM file_parts p "
                "JOIN subtree s ON p.node_id=s.id "
                "UNION SELECT json_extract(n.poster,'$.message_id') FROM nodes n "
                "JOIN subtree s ON n.id=s.id WHERE n.poster IS NOT NULL "
                "ORDER BY message_id",
                (node_id,),
            )
        ]

    def delete(self, path: str) -> list[int]:
        """Remove empty metadata only; files require a durable deletion intent."""
        with self._transaction(write=True) as db:
            node, _, chain = self._resolve(db, path)
            if node["id"] == ROOT_ID:
                raise RootMutationError("the root folder cannot be deleted")
            self._mutable(db, chain, descendants=True)
            if self._messages(db, node["id"]):
                raise ValidationError("files require a permanent deletion intent")
            db.execute("DELETE FROM nodes WHERE id=?", (node["id"],))
            self._bump(db)
            return []

    def move_many(
        self, paths: list[str], destination: str, *, conflict: str = "keep_both"
    ) -> list[dict[str, Any] | None]:
        with self.metadata_batch():
            return [self.move(path, destination, conflict=conflict) for path in paths]

    def move(
        self, path: str, destination: str, *, conflict: str = "keep_both"
    ) -> dict[str, Any] | None:
        if conflict not in {"keep_both", "skip"}:
            raise ValidationError("conflict must be 'keep_both' or 'skip'")
        with self._transaction(write=True) as db:
            node, path, chain = self._resolve(db, path)
            parent, destination, parent_chain = self._resolve(db, destination)
            if node["id"] == ROOT_ID:
                raise RootMutationError("the root folder cannot be moved")
            if parent["kind"] != "folder":
                raise NotDirectoryError("destination must be a folder")
            self._visible(chain)
            self._visible(parent_chain)
            self._mutable(db, chain, descendants=True)
            self._mutable(db, parent_chain)
            if node["id"] in {item["id"] for item in parent_chain}:
                raise InvalidPathError("a folder cannot be moved into its own subtree")
            if node["parent_id"] == parent["id"]:
                return self._record(node, path)
            descendants = self._subtree_ids(db, node["id"])
            for item_id in descendants:
                relative_depth = len(self._path_for_id(db, item_id).split("/")) - len(
                    path.split("/")
                )
                if len(parent_chain) + relative_depth > MAX_TREE_DEPTH:
                    raise ValidationError("move would exceed maximum tree depth")
            name = node["name"]
            if not self._unique(db, parent["id"], name):
                if conflict == "skip":
                    return None
                name = self._available_name(db, parent["id"], name, kind=node["kind"])
            db.execute(
                "UPDATE nodes SET parent_id=?,name=?,name_key=? WHERE id=?",
                (parent["id"], name, _name_key(name), node["id"]),
            )
            self._bump(db)
            moved = db.execute(
                "SELECT * FROM nodes WHERE id=?", (node["id"],)
            ).fetchone()
            return self._record(moved, _join_path(destination, node["id"]))

    @staticmethod
    def _default_share_expiry() -> str:
        return (
            (datetime.now(UTC) + timedelta(days=7))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    def create_share(
        self,
        path: str,
        *,
        expires_at: object = _DEFAULT_EXPIRY,
        token_factory=None,
        label: str = "",
        replace_existing: bool = False,
    ) -> str:
        if expires_at is self._DEFAULT_EXPIRY:
            expiry = self._default_share_expiry()
        else:
            expiry = _validate_timestamp(expires_at) if expires_at is not None else None
        label = _share_label(label)
        created = _utc_now()
        if expiry is not None and expiry <= created:
            raise ValidationError("share expiry must be in the future")
        with self._transaction(write=True) as db:
            node, _, chain = self._resolve(db, path)
            self._visible(chain)
            self._demo_mutable(db, chain, descendants=True)
            self._mutable(db, chain)
            identity = secrets.token_urlsafe(16)
            token = (
                token_factory(identity) if token_factory else secrets.token_urlsafe(32)
            )
            if replace_existing:
                db.execute("DELETE FROM shares WHERE node_id=?", (node["id"],))
            if (
                db.execute(
                    "SELECT COUNT(*) FROM shares WHERE node_id=?", (node["id"],)
                ).fetchone()[0]
                >= MAX_SHARE_TOKENS_PER_ITEM
            ):
                raise ValidationError(
                    "This item has 100 links. Remove an old link first."
                )
            db.execute(
                "INSERT INTO shares VALUES(?,?,?,?,?,?)",
                (
                    identity,
                    node["id"],
                    hashlib.sha256(token.encode()).hexdigest(),
                    created,
                    expiry,
                    label,
                ),
            )
            self._bump(db)
            return token

    def list_shares(
        self, path: str | None = None, *, include_expired=False, token_factory=None
    ) -> list[dict[str, Any]]:
        with self._transaction() as db:
            node_id = self._resolve(db, path)[0]["id"] if path is not None else None
            result = []
            for row in db.execute("SELECT * FROM shares ORDER BY rowid"):
                if node_id is not None and row["node_id"] != node_id:
                    continue
                if (
                    not include_expired
                    and row["expires_at"] is not None
                    and row["expires_at"] <= _utc_now()
                ):
                    continue
                current_path = self._path_for_id(db, row["node_id"])
                node, _, chain = self._resolve(db, current_path)
                result.append(
                    dict(
                        id=row["id"],
                        label=row["label"],
                        node_id=row["node_id"],
                        path=current_path,
                        created_at=row["created_at"],
                        expires_at=row["expires_at"],
                        name=node["name"],
                        type=node["kind"],
                        size=node["size"],
                        folder_path=current_path.rsplit("/", 1)[0] or "/",
                        folder_label=" / ".join(
                            ["My Files", *(n["name"] for n in chain[1:-1])]
                        ),
                        copyable=bool(
                            token_factory
                            and hmac.compare_digest(
                                row["token_hash"],
                                hashlib.sha256(
                                    token_factory(row["id"]).encode()
                                ).hexdigest(),
                            )
                        ),
                    )
                )
            return result

    def update_share(
        self, share_id: str, *, label=_SHARE_UNSET, expires_at=_SHARE_UNSET
    ):
        # Omission preserves the current value; explicit None removes expiry.
        if label is not _SHARE_UNSET:
            label = _share_label(label)
        expiry = expires_at
        if expires_at is not _SHARE_UNSET and expires_at is not None:
            expiry = _validate_timestamp(expires_at)
        if expiry is not _SHARE_UNSET and expiry is not None and expiry <= _utc_now():
            raise ValidationError("Link expiry must be in the future")
        with self._transaction(write=True) as db:
            row = db.execute("SELECT * FROM shares WHERE id=?", (share_id,)).fetchone()
            if row is None:
                raise NotFoundError("Link not found")
            _, _, chain = self._resolve(db, self._path_for_id(db, row["node_id"]))
            self._visible(chain)
            self._demo_mutable(db, chain, descendants=True)
            self._mutable(db, chain)
            if label is _SHARE_UNSET and expiry is _SHARE_UNSET:
                return
            db.execute(
                "UPDATE shares SET label=?,expires_at=? WHERE id=?",
                (
                    row["label"] if label is _SHARE_UNSET else label,
                    row["expires_at"] if expiry is _SHARE_UNSET else expiry,
                    share_id,
                ),
            )
            self._bump(db)

    def revoke_share(self, share_id: str) -> bool:
        with self._transaction(write=True) as db:
            if share_id.startswith("/"):
                node, _, chain = self._resolve(db, share_id)
                self._demo_mutable(db, chain, descendants=True)
                cursor = db.execute("DELETE FROM shares WHERE node_id=?", (node["id"],))
            else:
                share = db.execute(
                    "SELECT node_id FROM shares WHERE id=?", (share_id,)
                ).fetchone()
                if share is not None:
                    _, _, chain = self._resolve(
                        db, self._path_for_id(db, share["node_id"])
                    )
                    self._demo_mutable(db, chain, descendants=True)
                cursor = db.execute("DELETE FROM shares WHERE id=?", (share_id,))
            if cursor.rowcount:
                self._bump(db)
            return bool(cursor.rowcount)

    @staticmethod
    def _revoke_subtree(db: sqlite3.Connection, node_id: str) -> None:
        db.execute(
            "WITH RECURSIVE subtree(id) AS (SELECT id FROM nodes WHERE id=? UNION "
            "SELECT nodes.id FROM nodes JOIN subtree ON nodes.parent_id=subtree.id) "
            "DELETE FROM shares WHERE node_id IN subtree",
            (node_id,),
        )

    def _authorized(
        self, db: sqlite3.Connection, chain: list[sqlite3.Row], token: object
    ) -> str | None:
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 1024
            or any(row["trashed"] for row in chain)
        ):
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        for share in db.execute(
            "SELECT * FROM shares WHERE expires_at IS NULL OR expires_at>?",
            (_utc_now(),),
        ):
            if hmac.compare_digest(share["token_hash"], token_hash) and (
                share["node_id"] in {row["id"] for row in chain}
            ):
                return self._path_for_id(db, share["node_id"])
        return None

    def authorize_directory(self, path: str, token: str) -> str | None:
        with self._transaction() as db:
            node, path, chain = self._resolve(db, path)
            if node["kind"] != "folder":
                raise NotDirectoryError(f"not a directory: {path}")
            return self._authorized(db, chain, token)

    def authorize_file(self, path: str, token: str) -> bool:
        with self._transaction() as db:
            node, path, chain = self._resolve(db, path)
            if node["kind"] != "file":
                raise NotFileError(f"not a file: {path}")
            return self._authorized(db, chain, token) is not None

    def validate_deletion(self, path: str) -> dict[str, Any]:
        """Read-only preflight for a top-level trash item, including pending retries."""
        with self._transaction() as db:
            node, path, chain = self._resolve(db, path)
            if node["id"] == ROOT_ID:
                raise RootMutationError("the root folder cannot be deleted")
            self._demo_mutable(db, chain, descendants=True)
            existing = db.execute(
                "SELECT 1 FROM deletion_intents WHERE root_id=? AND state!='completed'",
                (node["id"],),
            ).fetchone()
            if not existing:
                self._mutable(db, chain, descendants=True)
                if not node["trashed"] or any(row["trashed"] for row in chain[:-1]):
                    raise TrashedItemError(
                        "permanent deletion must target a top-level trashed item"
                    )
            return self._record(node, path)

    def begin_deletion(
        self, path: str, *, channel_id: int | None = None
    ) -> dict[str, Any]:
        if channel_id is not None and (type(channel_id) is not int or channel_id == 0):
            raise ValidationError("channel_id must be a non-zero integer")
        with self._transaction(write=True) as db:
            node, path, chain = self._resolve(db, path)
            if node["id"] == ROOT_ID:
                raise RootMutationError("the root folder cannot be deleted")
            self._demo_mutable(db, chain, descendants=True)
            existing = db.execute(
                "SELECT * FROM deletion_intents WHERE root_id=? AND state!='completed'",
                (node["id"],),
            ).fetchone()
            if existing:
                if channel_id is not None and existing["channel_id"] != channel_id:
                    raise ValidationError(
                        "deletion channel does not match existing intent"
                    )
                return self._deletion_record(db, existing)
            self._mutable(db, chain)
            if not node["trashed"] or any(row["trashed"] for row in chain[:-1]):
                raise TrashedItemError(
                    "permanent deletion must target a top-level trashed item"
                )
            descendants = set(self._subtree_ids(db, node["id"]))
            if descendants.intersection(
                row[0]
                for row in db.execute(
                    "SELECT root_id FROM deletion_intents WHERE state!='completed'"
                )
            ):
                raise ValidationError("a descendant has a pending deletion")
            intent_id = secrets.token_urlsafe(16)
            db.execute(
                "INSERT INTO deletion_intents VALUES(?,?,?,?,?,?,NULL)",
                (intent_id, node["id"], path, channel_id, "pending", _utc_now()),
            )
            db.executemany(
                "INSERT INTO deletion_messages(intent_id,message_id) VALUES(?,?)",
                (
                    (intent_id, message_id)
                    for message_id in self._messages(db, node["id"])
                ),
            )
            self._revoke_subtree(db, node["id"])
            self._bump(db)
            return self._deletion_record(
                db,
                db.execute(
                    "SELECT * FROM deletion_intents WHERE id=?", (intent_id,)
                ).fetchone(),
            )

    @staticmethod
    def _deletion_record(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        messages = db.execute(
            "SELECT * FROM deletion_messages WHERE intent_id=? ORDER BY message_id",
            (row["id"],),
        ).fetchall()
        result["message_ids"] = [item["message_id"] for item in messages]
        result["pending_message_ids"] = [
            item["message_id"] for item in messages if not item["confirmed"]
        ]
        return result

    def pending_deletions(self) -> list[dict[str, Any]]:
        with self._transaction() as db:
            return [
                self._deletion_record(db, row)
                for row in db.execute(
                    "SELECT * FROM deletion_intents WHERE state!='completed' ORDER BY "
                    "rowid"
                )
            ]

    def confirm_deletion_messages(self, intent_id: str, message_ids: list[int]) -> None:
        validated = {
            _strict_int(message_id, "message_id", 1) for message_id in message_ids
        }
        with self._transaction(write=True) as db:
            intent = db.execute(
                "SELECT * FROM deletion_intents WHERE id=?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise NotFoundError("deletion intent not found")
            known = {
                row[0]
                for row in db.execute(
                    "SELECT message_id FROM deletion_messages WHERE intent_id=?",
                    (intent_id,),
                )
            }
            if not validated <= known:
                raise ValidationError(
                    "confirmation contains messages outside the deletion intent"
                )
            if intent["state"] == "completed":
                return
            db.executemany(
                "UPDATE deletion_messages SET confirmed=1 WHERE intent_id=? AND "
                "message_id=?",
                ((intent_id, message_id) for message_id in validated),
            )
            db.execute(
                "UPDATE deletion_intents SET state='pending',error=NULL WHERE id=?",
                (intent_id,),
            )
            self._bump(db)

    def fail_deletion(self, intent_id: str, error: str) -> None:
        with self._transaction(write=True) as db:
            cursor = db.execute(
                "UPDATE deletion_intents SET state='failed',error=? WHERE id=? AND "
                "state!='completed'",
                (str(error)[:500], intent_id),
            )
            if not cursor.rowcount:
                raise NotFoundError("active deletion intent not found")
            self._bump(db)

    def finish_deletion(self, intent_id: str) -> list[int]:
        """Atomically forget a trashed subtree; retain its Telegram documents."""
        with self._transaction(write=True) as db:
            intent = db.execute(
                "SELECT * FROM deletion_intents WHERE id=?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise NotFoundError("deletion intent not found")
            record = self._deletion_record(db, intent)
            if intent["state"] != "completed":
                _, _, chain = self._resolve(
                    db, self._path_for_id(db, intent["root_id"])
                )
                self._demo_mutable(db, chain, descendants=True)
                db.execute("DELETE FROM nodes WHERE id=?", (intent["root_id"],))
                db.execute(
                    "DELETE FROM deletion_messages WHERE intent_id=?", (intent_id,)
                )
                db.execute(
                    "UPDATE deletion_intents SET state='completed',error=NULL WHERE "
                    "id=?",
                    (intent_id,),
                )
                self._bump(db)
            return record["message_ids"]

    @staticmethod
    def _validate_deletion_record(intent: object) -> dict[str, Any]:
        keys = {
            "id",
            "root_id",
            "path",
            "channel_id",
            "state",
            "created_at",
            "error",
            "message_ids",
            "pending_message_ids",
        }
        if not isinstance(intent, dict) or set(intent) != keys:
            raise ValidationError("invalid deletion intent")
        _validate_id(intent["id"])
        _validate_id(intent["root_id"])
        _normalize_path(intent["path"])
        created = _validate_timestamp(intent["created_at"])
        if intent["state"] not in {"pending", "failed", "completed"}:
            raise ValidationError("invalid deletion state")
        if intent["channel_id"] is not None and (
            type(intent["channel_id"]) is not int or intent["channel_id"] == 0
        ):
            raise ValidationError("invalid deletion channel")
        if intent["error"] is not None and (
            not isinstance(intent["error"], str) or len(intent["error"]) > 500
        ):
            raise ValidationError("invalid deletion error")
        if not isinstance(intent["message_ids"], list) or not isinstance(
            intent["pending_message_ids"], list
        ):
            raise ValidationError("invalid deletion message list")
        messages = {
            _strict_int(value, "message_id", 1) for value in intent["message_ids"]
        }
        pending = {
            _strict_int(value, "message_id", 1)
            for value in intent["pending_message_ids"]
        }
        if not pending <= messages or len(messages) != len(intent["message_ids"]):
            raise ValidationError("invalid deletion confirmations")
        if intent["state"] == "completed" and pending:
            raise ValidationError("completed deletion has pending messages")
        return dict(intent, created_at=created)

    def _import_deletion_locked(self, db: sqlite3.Connection, intent: object) -> None:
        intent = self._validate_deletion_record(intent)
        messages = set(intent["message_ids"])
        pending = set(intent["pending_message_ids"])
        if intent["state"] != "completed":
            node = db.execute(
                "SELECT * FROM nodes WHERE id=?", (intent["root_id"],)
            ).fetchone()
            if (
                node is None
                or not node["trashed"]
                or set(self._messages(db, node["id"])) != messages
            ):
                raise ValidationError(
                    "deletion references do not match retained subtree"
                )
            actual_path = self._path_for_id(db, node["id"])
            _, _, chain = self._resolve(db, actual_path)
            if actual_path != intent["path"] or any(
                item["trashed"] for item in chain[:-1]
            ):
                raise ValidationError("deletion root has invalid ancestry")
            self._mutable(db, chain, descendants=True)
        elif db.execute(
            "SELECT 1 FROM nodes WHERE id=?", (intent["root_id"],)
        ).fetchone():
            raise ValidationError("completed deletion still has a live root")
        db.execute(
            "INSERT INTO deletion_intents VALUES(?,?,?,?,?,?,?)",
            (
                intent["id"],
                intent["root_id"],
                intent["path"],
                intent["channel_id"],
                intent["state"],
                intent["created_at"],
                intent["error"],
            ),
        )
        db.executemany(
            "INSERT INTO deletion_messages VALUES(?,?,?)",
            (
                (intent["id"], message_id, int(message_id not in pending))
                for message_id in messages
            ),
        )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
