"""Native MongoDB catalog: every read and write is a MongoDB query.

No catalog copy lives in process memory; MongoDB caches what is used. Folder
listings, names, search and shares use indexes, ancestry is one ``$graphLookup``
and drive totals are counters updated inside each write transaction. TGDrive
imposes no item-count, depth or snapshot-size limit; MongoDB's storage is the
limit. Writes run in majority transactions fenced by the ownership lease.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import re
import secrets
import threading
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ASCENDING, DESCENDING

from tgdrive.core.errors import (
    CorruptStoreError,
    DuplicateNameError,
    InvalidPathError,
    NotDirectoryError,
    NotFileError,
    NotFoundError,
    RootMutationError,
    TrashedItemError,
    UnsupportedSchemaError,
    ValidationError,
)
from tgdrive.storage.demo_files import DEMO_FILES_ID, DEMO_FILES_NAME, check_ids
from tgdrive.storage.file_parts import validate_parts, validate_poster, validate_sha256
from tgdrive.storage.mongo import DatabaseUnavailable, MongoBackend
from tgdrive.storage.store import (
    _SHARE_UNSET,
    MAX_NAME_LENGTH,
    MAX_SEARCH_RESULTS,
    ROOT_ID,
    SCHEMA_VERSION,
    FolderTreeConflictError,
    _FileNode,
    _fold,
    _folder_manifest,
    _FolderNode,
    _grams,
    _join_path,
    _manifest_json,
    _name_key,
    _normalize_name,
    _normalize_path,
    _parent_path,
    _parse_node,
    _share_label,
    _strict_int,
    _tokens,
    _utc_now,
    _validate_channel_id,
    _validate_file_rename,
    _validate_id,
    _validate_share_hashes,
    _validate_timestamp,
    _validate_tree,
)

_TABLES = ("nodes", "shares", "operations", "deletions", "preparations")
# Finished bookkeeping and expired links are removed by MongoDB TTL indexes.
_RECORD_RETENTION = timedelta(days=7)
_EXPIRED_SHARE_RETENTION = timedelta(days=30)
_COUNTERS = ("files", "folders", "total_bytes", "trashed_files", "trashed_bytes")
_BATCH = 1000
_LIGHT = {"parts": 0, "poster": 0, "tokens": 0, "grams": 0, "fold": 0}


def _expiry_date(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _clean(row):
    return {k: v for k, v in row.items() if k != "_id"}


def mongo_nodes(catalog):
    """MongoDB node documents (with search keys) for a portable catalog."""
    stack = [(catalog["root"], None)]
    rows = []
    while stack:
        node, parent = stack.pop()
        values = {
            key: copy.deepcopy(node[key])
            for key in (
                "upload_date",
                "trashed",
                "starred",
                "message_id",
                "size",
                "parts",
                "sha256",
                "poster",
            )
            if key in node
        }
        values["part_count"] = len(node.get("parts") or [None])
        rows.append(
            MongoStore._node(node["id"], parent, node["name"], node["type"], **values)
        )
        if node["type"] == "folder":
            stack.extend((child, node["id"]) for child in node["children"].values())
    return rows


def catalog_counters(catalog):
    """Drive totals for a portable catalog: visible versus inside Trash."""
    totals = dict.fromkeys(_COUNTERS, 0)
    stack = [(catalog["root"], False)]
    while stack:
        node, hidden = stack.pop()
        hidden = hidden or node["trashed"]
        if node["type"] == "file":
            totals["trashed_files" if hidden else "files"] += 1
            totals["trashed_bytes" if hidden else "total_bytes"] += node["size"]
        else:
            if node["id"] != ROOT_ID and not hidden:
                totals["folders"] += 1
            stack.extend((child, hidden) for child in node["children"].values())
    return totals


class MongoStore:
    backend_name = "mongodb"
    _DEFAULT_EXPIRY = object()

    def __init__(self, backend: MongoBackend):
        self.backend = backend
        self._lock = threading.RLock()
        self._local = threading.local()
        self._closed = False
        metadata = backend.db.catalog.find_one({"_id": "catalog"})
        if metadata is None:
            raise CorruptStoreError(
                "MongoDB has no TGDrive catalog. Open owner recovery to "
                "restore a confirmed backup."
            )
        if metadata.get("schema_version") != SCHEMA_VERSION:
            raise CorruptStoreError(
                "This database belongs to an older TGDrive version. "
                "Start TGDrive 2.3 with a new MongoDB database and storage channel."
            )
        self._metadata = metadata
        try:
            root = backend.db.nodes.find_one({"_id": ROOT_ID}, _LIGHT)
            if (
                root is None
                or root.get("parent_id") is not None
                or root.get("kind") != "folder"
            ):
                raise CorruptStoreError("MongoDB catalog root is missing")
        except (KeyError, TypeError, AttributeError) as error:
            raise CorruptStoreError("Catalog contains a malformed record") from error
        self._indexes(backend)

    # ----------------------------------------------------------- lifecycle

    @classmethod
    def create(
        cls,
        backend: MongoBackend,
        *,
        storage_channel: int | None = None,
        initial_settings: list[dict[str, Any]] | None = None,
    ) -> MongoStore:
        backend.ping()
        if storage_channel is not None:
            storage_channel = _validate_channel_id(storage_channel)
        bound = storage_channel is not None
        metadata = {
            "_id": "catalog",
            "schema_version": SCHEMA_VERSION,
            "drive_id": secrets.token_urlsafe(16),
            "revision": 2 if bound else 1,
            "backed_up_revision": 2 if bound else 1,
            "generation": 1 if bound else 0,
            "storage_channel_id": storage_channel,
            **dict.fromkeys(_COUNTERS, 0),
        }
        root = cls._node(ROOT_ID, None, "/", "folder")

        def initialize(session):
            collections = _TABLES
            if initial_settings is not None:
                from tgdrive.recovery.mongo_backup import COLLECTIONS

                collections = COLLECTIONS
                backend.fence(session)
            if backend.db.catalog.find_one({"_id": "catalog"}, session=session):
                raise FileExistsError("MongoDB already contains a TGDrive catalog")
            if any(
                backend.db[name].find_one({}, session=session) for name in collections
            ):
                raise CorruptStoreError("MongoDB contains incomplete catalog state")
            backend.db.catalog.insert_one(metadata, session=session)
            backend.db.nodes.insert_one(root, session=session)
            if initial_settings:
                backend.db.settings.insert_many(
                    copy.deepcopy(initial_settings), session=session
                )

        backend.transaction(initialize)
        if initial_settings is not None:
            with backend._settings_lock:
                backend._settings_cached = False
                backend._settings_document = None
        cls._indexes(backend)
        return cls(backend)

    @staticmethod
    def _indexes(backend):
        nodes = backend.db.nodes
        nodes.create_index(
            [("parent_id", 1), ("name_key", 1)],
            unique=True,
            partialFilterExpression={"trashed": False},
            name="active_sibling_names",
        )
        # Listing and every sort order of a folder page; children of a parent.
        nodes.create_index([("parent_id", 1), ("trashed", 1), ("name_key", 1)])
        nodes.create_index([("parent_id", 1), ("trashed", 1), ("size", 1)])
        nodes.create_index([("parent_id", 1), ("trashed", 1), ("upload_date", 1)])
        nodes.create_index([("parent_id", 1), ("kind", 1)])
        nodes.create_index("tokens")
        nodes.create_index("grams")
        nodes.create_index(
            [("trashed", 1), ("name_key", 1)],
            partialFilterExpression={"trashed": True},
            name="trash_roots",
        )
        nodes.create_index(
            [("starred", 1), ("name_key", 1)],
            partialFilterExpression={"starred": True},
            name="starred_items",
        )
        backend.db.shares.create_index("token_hash", unique=True)
        backend.db.shares.create_index("node_id")
        backend.db.shares.create_index("expire_at", expireAfterSeconds=0)
        backend.db.operations.create_index("node_id")
        backend.db.operations.create_index("expire_at", expireAfterSeconds=0)
        backend.db.deletions.create_index("root_id")
        backend.db.deletions.create_index("state")
        backend.db.deletions.create_index("expire_at", expireAfterSeconds=0)
        backend.db.preparations.create_index("expire_at", expireAfterSeconds=0)
        backend.db.receipts.create_index("purge_at", expireAfterSeconds=0)

    @staticmethod
    def _node(identity, parent, name, kind, **values):
        fold = _fold(name) if parent is not None else ""
        return {
            "_id": identity,
            "id": identity,
            "parent_id": parent,
            "name": name,
            "name_key": _name_key(name),
            "fold": fold,
            "tokens": _tokens(name) if parent is not None else [],
            "grams": _grams(fold),
            "kind": kind,
            "trashed": False,
            "upload_date": _utc_now(),
            "size": 0,
            "message_id": None,
            "part_count": 1,
            "sha256": None,
            **values,
        }

    @staticmethod
    def _names(name):
        fold = _fold(name)
        return {
            "name": name,
            "name_key": _name_key(name),
            "fold": fold,
            "tokens": _tokens(name),
            "grams": _grams(fold),
        }

    def close(self):
        self._closed = True

    # --------------------------------------------------------- transactions

    @property
    def _session(self):
        return getattr(self._local, "session", None)

    @contextmanager
    def _transaction(self, *, write=False):
        if self._closed:
            raise DatabaseUnavailable("The catalog is closed")
        if self._session is not None:
            # Nested work joins the open transaction; a failure aborts all of it.
            yield self._session
            return
        if not write:
            yield None
            return
        with self._lock:
            self.backend.check_owner()
            with self.backend.session_transaction() as session:
                self._local.session = session
                self._local.changed = False
                try:
                    yield session
                    if self._local.changed:
                        self.backend.fence(session)
                        self._local.metadata = self.db.catalog.find_one_and_update(
                            {"_id": "catalog"},
                            {"$inc": {"generation": 1}},
                            session=session,
                            return_document=True,
                        )
                except BaseException:
                    self._local.metadata = None
                    raise
                finally:
                    self._local.session = None
            # Publish the committed counters only after MongoDB acknowledged them.
            if getattr(self._local, "metadata", None):
                self._metadata = self._local.metadata
            self._local.metadata = None

    @contextmanager
    def metadata_batch(self):
        with self._transaction(write=True):
            yield

    @property
    def db(self):
        return self.backend.db

    def _bump(self, **counters):
        changes = {"revision": 1}
        for name, value in counters.items():
            if value:
                changes[name] = value
        changes["backed_up_revision"] = 1
        self.db.catalog.update_one(
            {"_id": "catalog"}, {"$inc": changes}, session=self._session
        )
        self._local.changed = True

    def _find(self, collection, query, projection=None, **options):
        return self.db[collection].find(
            query, projection, session=self._session, **options
        )

    def _one(self, collection, query, projection=None):
        return self.db[collection].find_one(query, projection, session=self._session)

    # --------------------------------------------------------------- reads

    def _current(self):
        if self._session is not None:
            return self._one("catalog", {"_id": "catalog"})
        return self._metadata

    @property
    def drive_id(self):
        return self._current()["drive_id"]

    @property
    def revision(self):
        return self._current()["revision"]

    @property
    def storage_channel_id(self):
        return self._current().get("storage_channel_id")

    def bind_storage_channel(self, channel_id):
        channel_id = _validate_channel_id(channel_id)
        with self._transaction(write=True):
            current = self.storage_channel_id
            if current == channel_id:
                return
            if current is not None:
                raise ValidationError(
                    "The drive belongs to a different storage channel"
                )
            self.db.catalog.update_one(
                {"_id": "catalog"},
                {"$set": {"storage_channel_id": channel_id}},
                session=self._session,
            )
            self._bump()

    def has_file_operation(self, operation_id, *, message_id=None):
        _validate_id(operation_id)
        if message_id is not None:
            message_id = _strict_int(message_id, "message_id", 1)
        with self._transaction():
            operation = self._one("operations", {"_id": operation_id})
            if operation is None:
                return False
            if message_id is None:
                return True
            node = self._one("nodes", {"_id": operation["node_id"]}, {"message_id": 1})
            return node is not None and node.get("message_id") == message_id

    def _get(self, identity, *, full=False):
        return self._one("nodes", {"_id": identity}, None if full else _LIGHT)

    def _ancestors(self, identity, *, full=False):
        """Root-first chain ending at ``identity`` in one MongoDB round trip."""
        rows = list(
            self.db.nodes.aggregate(
                [
                    {"$match": {"_id": identity}},
                    {
                        "$graphLookup": {
                            "from": "nodes",
                            "startWith": "$parent_id",
                            "connectFromField": "parent_id",
                            "connectToField": "_id",
                            "as": "up",
                            "depthField": "depth",
                        }
                    },
                    {
                        "$project": {
                            "grams": 0,
                            "tokens": 0,
                            "up.grams": 0,
                            "up.tokens": 0,
                            "up.parts": 0,
                            "up.poster": 0,
                            **({} if full else {"parts": 0, "poster": 0}),
                        }
                    },
                ],
                session=self._session,
            )
        )
        if not rows:
            raise NotFoundError("Item not found")
        node = rows[0]
        node["_full"] = full
        up = sorted(node.pop("up"), key=lambda row: -row["depth"])
        chain = [*up, node]
        if chain[0]["_id"] != ROOT_ID or any(
            chain[index]["parent_id"] != chain[index - 1]["_id"]
            for index in range(1, len(chain))
        ):
            raise CorruptStoreError("Invalid catalog ancestry")
        return chain

    @staticmethod
    def _chain_path(chain):
        return "/" + "/".join(row["_id"] for row in chain[1:])

    def _resolve(self, path, *, full=False):
        parts, path = _normalize_path(path)
        if not parts:
            return self._get(ROOT_ID, full=full), "/", [self._get(ROOT_ID)]
        chain = self._ancestors(parts[-1], full=full)
        if tuple(row["_id"] for row in chain[1:]) != parts:
            # A path is the item's own ID chain; a stale prefix means it moved.
            if any(row["kind"] != "folder" for row in chain[:-1]):
                raise NotDirectoryError("A path component is not a folder")
            raise NotFoundError("Item not found")
        return chain[-1], path, chain

    def _path(self, identity):
        if identity == ROOT_ID:
            return "/"
        return self._chain_path(self._ancestors(identity))

    def resolve_id(self, item_id):
        _validate_id(item_id, allow_root=True)
        with self._transaction():
            return self._path(item_id)

    def _children(self, parent_ids, projection=None):
        for start in range(0, len(parent_ids), _BATCH):
            yield from self._find(
                "nodes",
                {"parent_id": {"$in": parent_ids[start : start + _BATCH]}},
                projection or {"_id": 1, "kind": 1},
            )

    def _walk(self, identity, projection=None):
        """Every descendant in bounded batches; memory holds one level of folders."""
        level = [identity]
        while level:
            following = []
            for row in self._children(level, projection):
                yield row
                if row["kind"] == "folder":
                    following.append(row["_id"])
            level = following

    def _contains(self, ancestor, identity):
        """True when ``identity`` is ``ancestor`` or inside it (one lookup)."""
        if ancestor == identity:
            return True
        try:
            return any(row["_id"] == ancestor for row in self._ancestors(identity))
        except NotFoundError:
            return False

    def _active_deletion_roots(self):
        return [
            row["root_id"]
            for row in self._find(
                "deletions", {"state": {"$ne": "completed"}}, {"root_id": 1}
            )
        ]

    def _mutable(self, chain, *, descendants=False):
        self._demo_mutable(chain, descendants=descendants)
        roots = set(self._active_deletion_roots())
        if not roots:
            return
        if roots.intersection(row["_id"] for row in chain) or (
            descendants
            and any(self._contains(chain[-1]["_id"], root) for root in roots)
        ):
            raise ValidationError("Item is pending permanent deletion")

    def _demo_mutable(self, chain, *, descendants=False):
        from tgdrive.storage.demo_files import restricted_writes

        if not restricted_writes.get():
            return
        check_ids(row["_id"] for row in chain)
        if descendants and self._contains(chain[-1]["_id"], DEMO_FILES_ID):
            check_ids([DEMO_FILES_ID])

    def ensure_demo_files(self):
        with self._transaction(write=True):
            node = self._get(DEMO_FILES_ID)
            if node is not None:
                if node["kind"] != "folder":
                    raise ValidationError(
                        "The reserved Demo Files identity is not a folder"
                    )
                return self._record(node, self._path(DEMO_FILES_ID))
            if not self._unique(ROOT_ID, DEMO_FILES_NAME):
                raise DuplicateNameError(
                    "A different item named Demo Files already exists. "
                    "Disable demo mode and rename that item before enabling it again."
                )
            self._mutable(self._resolve("/")[2])
            node = self._node(DEMO_FILES_ID, ROOT_ID, DEMO_FILES_NAME, "folder")
            self.db.nodes.insert_one(node, session=self._session)
            self._bump(folders=1)
            return self._record(node, "/" + DEMO_FILES_ID)

    def _unique(self, parent, name, exclude=""):
        query = {"parent_id": parent, "name_key": _name_key(name), "trashed": False}
        if exclude:
            query["_id"] = {"$ne": exclude}
        return self._one("nodes", query, {"_id": 1}) is None

    def _available(self, parent, name, kind, exclude=""):
        if self._unique(parent, name, exclude):
            return name
        from pathlib import Path

        suffix = Path(name).suffix if kind == "file" else ""
        stem = name[: -len(suffix)] if suffix else name
        # One indexed query for every "stem (n)" sibling instead of a probe per n.
        prefix = _name_key(stem[: MAX_NAME_LENGTH - 12])
        query = {
            "parent_id": parent,
            "trashed": False,
            "name_key": {"$regex": "^" + re.escape(prefix)},
        }
        if exclude:
            query["_id"] = {"$ne": exclude}
        taken = {row["name_key"] for row in self._find("nodes", query, {"name_key": 1})}
        index = 1
        while True:
            label = f" ({index})"
            # A cut extension may end in whitespace, which names cannot.
            ending = suffix[: MAX_NAME_LENGTH - len(label) - 1].rstrip()
            value = stem[: MAX_NAME_LENGTH - len(label) - len(ending)] + label + ending
            if _name_key(value) not in taken:
                return value
            index += 1

    def _file(self, node, path):
        # Light reads omit parts/poster; load them only when a file is returned.
        if "grams" not in node and not node.get("_full"):
            node = self._get(node["_id"], full=True) or node
        result = self._record(node, path, message=True)
        if node.get("parts"):
            result["parts"] = copy.deepcopy(node["parts"])
        if node.get("poster"):
            result["poster"] = copy.deepcopy(node["poster"])
        return result

    def transfer_location(self, operation_id, destination="/", *, completed=False):
        """Resolve committed upload identities against current MongoDB state."""
        if operation_id is not None:
            _validate_id(operation_id)
        with self._transaction():
            operation = (
                self._one("operations", {"_id": operation_id})
                if operation_id is not None
                else None
            )
            if operation is not None:
                chain = self._ancestors(operation["node_id"])
            elif completed:
                raise NotFoundError("Uploaded item is unavailable")
            else:
                parts, _ = _normalize_path(destination)
                chain = self._ancestors(parts[-1]) if parts else [self._get(ROOT_ID)]
            self._visible(chain)
            from tgdrive.transfers.transfer_locations import location_record

            return location_record(
                chain[-1], self._chain_path(chain), chain, stored=operation is not None
            )

    def upload_destination(self, path, *, directory_id=None):
        with self._transaction():
            if directory_id is not None:
                _validate_id(directory_id, allow_root=True)
                path = self._path(directory_id)
            node, path, chain = self._resolve(path)
            if node["kind"] != "folder":
                raise NotDirectoryError("Upload destination must be a folder")
            self._visible(chain)
            self._mutable(chain)
            return {"id": node["_id"], "path": path}

    def get_directory(self, path, *, include_trashed=False):
        with self._transaction():
            node, path, chain = self._resolve(path)
            if node["kind"] != "folder":
                raise NotDirectoryError("Not a directory")
            self._visible(chain, include_trashed)
            result = self._record(node, path)
            result["breadcrumbs"] = [
                {
                    "id": row["_id"],
                    "name": row["name"],
                    "path": self._chain_path(chain[: i + 1]),
                }
                for i, row in enumerate(chain)
            ]
            query = {"parent_id": node["_id"]}
            if not include_trashed:
                query["trashed"] = False
            result["contents"] = {
                row["_id"]: self._record(row, _join_path(path, row["_id"]))
                for row in self._find("nodes", query, _LIGHT).sort("name_key", 1)
            }
            return result

    def list_directory(self, path, *, include_trashed=False):
        return self.get_directory(path, include_trashed=include_trashed)["contents"]

    def list_page(self, path, *, offset=0, limit=100, sort="name", order="asc"):
        """One indexed page of a folder; folders first, then the sort field."""
        fields = {
            "name": "name_key",
            "size": "size",
            "date": "upload_date",
            "type": "name_key",
        }
        if sort not in fields or order not in {"asc", "desc"}:
            raise ValidationError("Invalid sort")
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValidationError("Invalid pagination")
        with self._transaction():
            node, path, chain = self._resolve(path)
            if node["kind"] != "folder":
                raise NotDirectoryError("Not a directory")
            self._visible(chain)
            direction = ASCENDING if order == "asc" else DESCENDING
            query = {"parent_id": node["_id"], "trashed": False}
            folders = self.db.nodes.count_documents(
                dict(query, kind="folder"), session=self._session
            )
            total = self.db.nodes.count_documents(query, session=self._session)
            items = []
            for kind, skip in (("folder", offset), ("file", max(0, offset - folders))):
                if len(items) >= limit or (kind == "folder" and offset >= folders):
                    continue
                cursor = (
                    self._find("nodes", dict(query, kind=kind), _LIGHT)
                    .sort([(fields[sort], direction), ("_id", direction)])
                    .skip(skip)
                    .limit(limit - len(items))
                )
                items.extend(
                    self._record(row, _join_path(path, row["_id"])) for row in cursor
                )
            return {"items": items, "total": total, "offset": offset}

    def get_file(self, path, *, include_trashed=False):
        with self._transaction():
            node, path, chain = self._resolve(path, full=True)
            if node["kind"] != "file":
                raise NotFileError("Not a file")
            self._visible(chain, include_trashed)
            return self._file(node, path)

    # -------------------------------------------------------------- writes

    def _subtree_totals(self, identity):
        """Files, folders and bytes under a node, including the node itself."""
        node = self._get(identity)
        files = int(node["kind"] == "file")
        folders = int(node["kind"] == "folder")
        size = node["size"] if files else 0
        if folders:
            for row in self._walk(identity, {"_id": 1, "kind": 1, "size": 1}):
                if row["kind"] == "file":
                    files += 1
                    size += row["size"]
                else:
                    folders += 1
        return files, folders, size

    def _hidden(self, chain):
        return any(row["trashed"] for row in chain)

    def _add(
        self,
        parent_path,
        name,
        message_id,
        size=0,
        operation_id=None,
        keep_both=False,
        parts=None,
        sha256=None,
        poster=None,
    ):
        name = _normalize_name(name)
        if operation_id is not None:
            _validate_id(operation_id)
        with self._transaction(write=True):
            if operation_id is not None:
                previous = self._one("operations", {"_id": operation_id})
                if previous is not None:
                    node = self._get(previous["node_id"], full=True)
                    if node is None:
                        raise ValidationError(
                            "This operation's file was already deleted"
                        )
                    result = self._file(node, self._path(node["_id"]))
                    if (
                        node["message_id"] != message_id
                        or node["size"] != size
                        or result.get("parts") != parts
                        or result.get("sha256") != sha256
                        or result.get("poster") != poster
                    ):
                        raise ValidationError(
                            "Operation already committed different media"
                        )
                    return result
            parent, parent_path, chain = self._resolve(parent_path)
            if parent["kind"] != "folder":
                raise NotDirectoryError("Destination must be a folder")
            self._visible(chain)
            self._mutable(chain)
            kind = "folder" if message_id is None else "file"
            if keep_both:
                name = self._available(parent["_id"], name, kind)
            elif not self._unique(parent["_id"], name):
                raise DuplicateNameError("An item with this name already exists")
            identity = secrets.token_urlsafe(16)
            row = self._node(
                identity,
                parent["_id"],
                name,
                kind,
                message_id=message_id,
                size=size,
                sha256=sha256,
                part_count=len(parts) if parts else 1,
            )
            if parts:
                row["parts"] = copy.deepcopy(parts)
            if poster:
                row["poster"] = copy.deepcopy(poster)
            self.db.nodes.insert_one(row, session=self._session)
            if operation_id is not None:
                self.db.operations.insert_one(
                    {
                        "_id": operation_id,
                        "operation_id": operation_id,
                        "node_id": identity,
                    },
                    session=self._session,
                )
            if kind == "file":
                self._bump(files=1, total_bytes=size)
            else:
                self._bump(folders=1)
            path = _join_path(parent_path, identity)
            return (
                self._file(row, path)
                if message_id is not None
                else self._record(row, path)
            )

    def prepare_folder_tree(self, destination, root_name, folders, client_request_id):
        manifest = _folder_manifest(destination, root_name, folders)
        request_id = _validate_id(client_request_id)
        if len(request_id) < 16:
            raise ValidationError(
                "Folder request id must contain at least 16 characters"
            )
        fingerprint = hashlib.sha256(_manifest_json(manifest).encode()).hexdigest()
        with self._transaction(write=True):
            prior = self._one("preparations", {"_id": request_id})
            if prior:
                if not hmac.compare_digest(prior["fingerprint"], fingerprint):
                    raise FolderTreeConflictError("Folder request id was already used")
                result = prior["result"]
                try:
                    for relative, path in result["folders"].items():
                        node, _, chain = self._resolve(path)
                        self._visible(chain)
                        self._mutable(chain)
                        expected = (
                            relative.rsplit("/", 1)[-1]
                            if relative
                            else result["root"]["name"]
                        )
                        if node["kind"] != "folder" or node["name"] != expected:
                            raise ValidationError("Prepared folder changed")
                except (
                    NotFoundError,
                    NotDirectoryError,
                    ValidationError,
                    TrashedItemError,
                ):
                    raise FolderTreeConflictError(
                        "The prepared folder tree changed"
                    ) from None
                return copy.deepcopy(result)
            parent = self.upload_destination(manifest["destination"])
            root_name = self._available(parent["id"], manifest["root_name"], "folder")
            mapping, rows, root = {}, [], None
            for segments in [[], *manifest["folders"]]:
                parent_path = (
                    mapping["/".join(segments[:-1])] if segments else parent["path"]
                )
                parent_id = (
                    parent_path.rsplit("/", 1)[-1] if parent_path != "/" else ROOT_ID
                )
                identity = secrets.token_urlsafe(16)
                node = self._node(
                    identity,
                    parent_id,
                    segments[-1] if segments else root_name,
                    "folder",
                )
                rows.append(node)
                mapping["/".join(segments)] = _join_path(parent_path, identity)
                if not segments:
                    root = self._record(node, mapping[""])
            for start in range(0, len(rows), _BATCH):
                self.db.nodes.insert_many(
                    rows[start : start + _BATCH], session=self._session
                )
            result = {"root": root, "folders": mapping}
            self.db.preparations.insert_one(
                {
                    "_id": request_id,
                    "request_id": request_id,
                    "fingerprint": fingerprint,
                    "manifest": manifest,
                    "result": result,
                    "created_at": _utc_now(),
                    "expire_at": datetime.now(UTC) + _RECORD_RETENTION,
                },
                session=self._session,
            )
            self._bump(folders=len(rows))
            return copy.deepcopy(result)

    def rename(self, path, new_name):
        name = _normalize_name(new_name)
        with self._transaction(write=True):
            node, path, chain = self._resolve(path)
            if node["_id"] == ROOT_ID:
                raise RootMutationError("The root folder cannot be renamed")
            self._demo_mutable(chain, descendants=True)
            self._mutable(chain)
            if node["kind"] == "file":
                _validate_file_rename(node["name"], name)
            if not node["trashed"] and not self._unique(
                node["parent_id"], name, node["_id"]
            ):
                raise DuplicateNameError("An item with this name already exists")
            if node["name"] != name:
                self.db.nodes.update_one(
                    {"_id": node["_id"]},
                    {"$set": self._names(name)},
                    session=self._session,
                )
                node.update(self._names(name))
                self._bump()
            return self._record(node, path)

    def _revoke_under(self, identity):
        """Revoke links on a node or anything inside it, checking only shared items."""
        removed = []
        for row in self._find("shares", {}, {"node_id": 1}):
            if self._contains(identity, row["node_id"]):
                removed.append(row["_id"])
        if removed:
            self.db.shares.delete_many({"_id": {"$in": removed}}, session=self._session)

    def set_trashed(self, path, trashed):
        with self._transaction(write=True):
            return self._set_trashed(path, trashed)

    def set_trashed_many(self, paths, trashed):
        with self._transaction(write=True):
            return [self._set_trashed(path, trashed) for path in paths]

    def _set_trashed(self, path, trashed):
        if type(trashed) is not bool:
            raise ValidationError("trashed must be a boolean")
        node, path, chain = self._resolve(path)
        if node["_id"] == ROOT_ID:
            raise RootMutationError("The root folder cannot be trashed")
        self._mutable(chain, descendants=True)
        if not trashed:
            self._visible(chain[:-1])
        if node["trashed"] != trashed:
            name = (
                node["name"]
                if trashed
                else self._available(
                    node["parent_id"], node["name"], node["kind"], node["_id"]
                )
            )
            # Only the selected item changes; descendants are hidden through it.
            self.db.nodes.update_one(
                {"_id": node["_id"]},
                {"$set": {"trashed": trashed, **self._names(name)}},
                session=self._session,
            )
            node.update(trashed=trashed, **self._names(name))
            counters = {}
            if not self._hidden(chain[:-1]):
                files, folders, size = self._subtree_totals(node["_id"])
                sign = 1 if trashed else -1
                counters = {
                    "files": -sign * files,
                    "total_bytes": -sign * size,
                    "folders": -sign * folders,
                    "trashed_files": sign * files,
                    "trashed_bytes": sign * size,
                }
            if trashed:
                self._revoke_under(node["_id"])
            self._bump(**counters)
        return self._record(node, path)

    def _listed(self, node, chain):
        return self._record(node, self._chain_path(chain)) | {
            "parent_names": [item["name"] for item in chain[1:-1]]
        }

    def list_trash(self, query=""):
        with self._transaction():
            deleting = {
                row["root_id"]
                for row in self._find("deletions", {"state": "pending"}, {"root_id": 1})
            }
            needle = _fold(query)
            result = {}
            for node in self._find("nodes", {"trashed": True}, _LIGHT).sort(
                "name_key", 1
            ):
                if node["_id"] in deleting or needle not in _fold(node["name"]):
                    continue
                chain = self._ancestors(node["_id"])
                if not self._hidden(chain[:-1]):
                    result[node["_id"]] = self._listed(node, chain)
            return result

    def set_starred(self, path, starred):
        if type(starred) is not bool:
            raise ValidationError("starred must be a boolean")
        with self._transaction(write=True):
            node, path, chain = self._resolve(path)
            if node["_id"] == ROOT_ID:
                raise RootMutationError("the drive root cannot be starred")
            self._mutable(chain)
            self._visible(chain)
            if bool(node.get("starred", False)) != starred:
                change = (
                    {"$set": {"starred": True}}
                    if starred
                    else {"$unset": {"starred": ""}}
                )
                self.db.nodes.update_one(
                    {"_id": node["_id"]}, change, session=self._session
                )
                if starred:
                    node["starred"] = True
                else:
                    node.pop("starred", None)
                self._bump()
            return self._record(node, path)

    def list_starred(self, query=""):
        if not isinstance(query, str) or len(query) > MAX_NAME_LENGTH:
            raise ValidationError("invalid starred search")
        with self._transaction():
            needle = _fold(query.strip())
            results = {}
            for node in self._find("nodes", {"starred": True}, _LIGHT).sort(
                "name_key", 1
            ):
                if node["_id"] == ROOT_ID or needle not in _fold(node["name"]):
                    continue
                chain = self._ancestors(node["_id"])
                if not self._hidden(chain):
                    results[node["_id"]] = self._listed(node, chain)
            return results

    # -------------------------------------------------------------- search

    def search_page(
        self,
        query,
        *,
        offset=0,
        limit=100,
        sort="name",
        order=None,
        include_trashed=False,
    ):
        if not isinstance(query, str):
            raise ValidationError("Search query must be a string")
        query = unicodedata.normalize("NFC", query).strip()
        if (
            not query
            or len(query) > MAX_NAME_LENGTH
            or any(unicodedata.category(c) in {"Cc", "Cs"} for c in query)
        ):
            raise ValidationError("Search query is empty, too long or invalid")
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= MAX_SEARCH_RESULTS
            or type(include_trashed) is not bool
            or sort not in {"name", "date", "size"}
            or (
                order is not None
                and (not isinstance(order, str) or order not in {"asc", "desc"})
            )
        ):
            raise ValidationError("Invalid search pagination or sort")
        folded = _fold(query)
        words = [word for word in re.split(r"[\W_]+", folded) if word]
        clauses = []
        if not words:
            # Only punctuation (for example "%"): a literal substring scan.
            words = [folded]
            clauses.append({"fold": {"$regex": re.escape(folded)}})
        for word in words if not clauses else []:
            # Word starts use the token index; three or more characters can also
            # match inside a word through the trigram index.
            options = [{"tokens": {"$regex": "^" + re.escape(word)}}]
            if len(word) >= 3:
                options.append({"grams": {"$all": _grams(word)}})
            clauses.append({"$or": options})
        direction = order or ("asc" if sort == "name" else "desc")
        with self._transaction():
            visible = {}

            def placement(parent):
                if parent not in visible:
                    chain = self._ancestors(parent)
                    visible[parent] = (
                        self._hidden(chain),
                        [row["name"] for row in chain[1:]],
                        self._chain_path(chain),
                    )
                return visible[parent]

            # shortcut: matches are ranked in memory, so a query matching millions
            # of names reads all of them; page with an indexed sort key if that
            # becomes a real workload.
            found = []
            projection = {
                "_id": 1,
                "id": 1,
                "name": 1,
                "kind": 1,
                "size": 1,
                "upload_date": 1,
                "starred": 1,
                "parent_id": 1,
                "part_count": 1,
                "fold": 1,
                "trashed": 1,
                "tokens": 1,
            }
            for node in self._find("nodes", {"$and": clauses}, projection):
                if node["_id"] == ROOT_ID or not all(
                    word in node["fold"]
                    or any(token.startswith(word) for token in node["tokens"])
                    for word in words
                ):
                    continue
                hidden, names, parent_path = placement(node["parent_id"])
                if not include_trashed and (hidden or node["trashed"]):
                    continue
                row = self._record(node, _join_path(parent_path, node["_id"]))
                row["parent_names"] = names
                fold = node["fold"]
                rank = (
                    0
                    if fold == folded
                    else 1
                    if fold.startswith(folded)
                    else 2
                    if all(any(t.startswith(w) for t in node["tokens"]) for w in words)
                    else 3
                )
                found.append((rank, row))
            key = {
                "name": lambda item: _name_key(item[1]["name"]),
                "size": lambda item: item[1]["size"],
                "date": lambda item: item[1]["upload_date"],
            }[sort]
            # Ties keep name then ID order in both directions (sorts are stable).
            found.sort(key=lambda item: (_name_key(item[1]["name"]), item[1]["id"]))
            found.sort(key=key, reverse=direction == "desc")
            if sort == "name" and order is None:
                found.sort(key=lambda item: item[0])  # Best matches first.
            found.sort(key=lambda item: item[1]["type"] != "folder")
            items = [row for _, row in found[offset : offset + limit]]
            more = len(found) > offset + limit
            return {
                "items": items,
                "query": query,
                "has_more": more,
                "next_offset": offset + len(items) if more else None,
            }

    # ------------------------------------------------------ messages/delete

    def _messages(self, identity):
        result = set()
        root = self._get(identity, full=True)
        rows = [root] if root["kind"] == "file" else []
        rows = [
            *rows,
            *self._walk(
                identity,
                {"_id": 1, "kind": 1, "message_id": 1, "parts": 1, "poster": 1},
            ),
        ]
        for row in rows:
            if row["kind"] == "file":
                result.add(row["message_id"])
                result.update(part["message_id"] for part in row.get("parts") or [])
                if row.get("poster"):
                    result.add(row["poster"]["message_id"])
        return sorted(result)

    def message_ids(self, path, *, include_trashed=False):
        with self._transaction():
            node, _, chain = self._resolve(path)
            self._visible(chain, include_trashed)
            return self._messages(node["_id"])

    def _remove_subtree(self, identity):
        """Delete a subtree bottom-up in batches; returns every removed id."""
        levels, removed = [[identity]], [identity]
        while levels[-1]:
            following = []
            for row in self._children(levels[-1]):
                removed.append(row["_id"])
                if row["kind"] == "folder":
                    following.append(row["_id"])
            levels.append(following)
        for level in reversed(levels):
            for start in range(0, len(level), _BATCH):
                batch = level[start : start + _BATCH]
                self.db.nodes.delete_many(
                    {"parent_id": {"$in": batch}}, session=self._session
                )
        self.db.nodes.delete_many({"_id": identity}, session=self._session)
        for start in range(0, len(removed), _BATCH):
            batch = removed[start : start + _BATCH]
            self.db.shares.delete_many(
                {"node_id": {"$in": batch}}, session=self._session
            )
            # Keep upload receipts as tombstones so a replayed upload cannot
            # recreate deleted media; MongoDB expires them later.
            self.db.operations.update_many(
                {"node_id": {"$in": batch}},
                {"$set": {"expire_at": datetime.now(UTC) + _RECORD_RETENTION}},
                session=self._session,
            )
        return removed

    def delete(self, path):
        with self._transaction(write=True):
            node, _, chain = self._resolve(path)
            if node["_id"] == ROOT_ID:
                raise RootMutationError("The root folder cannot be deleted")
            self._mutable(chain, descendants=True)
            if self._messages(node["_id"]):
                raise ValidationError("Files require a permanent deletion intent")
            folders = self._subtree_totals(node["_id"])[1]
            self._remove_subtree(node["_id"])
            self._bump(folders=0 if self._hidden(chain) else -folders)
            return []

    def move(self, path, destination, *, conflict="keep_both"):
        with self._transaction(write=True):
            return self._move(path, destination, conflict=conflict)

    def move_many(self, paths, destination, *, conflict="keep_both"):
        with self._transaction(write=True):
            return [self._move(path, destination, conflict=conflict) for path in paths]

    def _move(self, path, destination, *, conflict="keep_both"):
        if conflict not in {"keep_both", "skip"}:
            raise ValidationError("Invalid conflict policy")
        node, path, chain = self._resolve(path)
        parent, destination, parent_chain = self._resolve(destination)
        if node["_id"] == ROOT_ID:
            raise RootMutationError("The root folder cannot be moved")
        if parent["kind"] != "folder":
            raise NotDirectoryError("Destination must be a folder")
        self._visible(chain)
        self._visible(parent_chain)
        self._mutable(chain, descendants=True)
        self._mutable(parent_chain)
        if node["_id"] in {row["_id"] for row in parent_chain}:
            raise InvalidPathError("A folder cannot move into its subtree")
        if node["parent_id"] == parent["_id"]:
            return self._record(node, path)
        name = node["name"]
        if not self._unique(parent["_id"], name):
            if conflict == "skip":
                return None
            name = self._available(parent["_id"], name, node["kind"])
        self.db.nodes.update_one(
            {"_id": node["_id"]},
            {"$set": {"parent_id": parent["_id"], **self._names(name)}},
            session=self._session,
        )
        node.update(parent_id=parent["_id"], **self._names(name))
        self._bump()
        return self._record(node, _join_path(destination, node["_id"]))

    # -------------------------------------------------------------- shares

    def create_share(
        self,
        path,
        *,
        expires_at=_DEFAULT_EXPIRY,
        token_factory=None,
        label="",
        replace_existing=False,
    ):
        expiry = (
            self._default_share_expiry()
            if expires_at is self._DEFAULT_EXPIRY
            else _validate_timestamp(expires_at)
            if expires_at is not None
            else None
        )
        label = _share_label(label)
        created = _utc_now()
        if expiry is not None and expiry <= created:
            raise ValidationError("Share expiry must be in the future")
        with self._transaction(write=True):
            node, _, chain = self._resolve(path)
            self._visible(chain)
            self._demo_mutable(chain, descendants=True)
            self._mutable(chain)
            if replace_existing:
                self.db.shares.delete_many(
                    {"node_id": node["_id"]}, session=self._session
                )
            identity = secrets.token_urlsafe(16)
            token = (
                token_factory(identity) if token_factory else secrets.token_urlsafe(32)
            )
            row = {
                "_id": identity,
                "id": identity,
                "node_id": node["_id"],
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "created_at": created,
                "expires_at": expiry,
                **({"label": label} if label else {}),
            }
            if expiry is not None:
                row["expire_at"] = _expiry_date(expiry) + _EXPIRED_SHARE_RETENTION
            self.db.shares.insert_one(row, session=self._session)
            self._bump()
            return token

    def list_shares(self, path=None, *, include_expired=False, token_factory=None):
        with self._transaction():
            query = {}
            if path is not None:
                query["node_id"] = self._resolve(path)[0]["_id"]
            now = _utc_now()
            result = []
            for row in self._find("shares", query).sort("created_at", 1):
                if not include_expired and (
                    row["expires_at"] is not None and row["expires_at"] <= now
                ):
                    continue
                chain = self._ancestors(row["node_id"])
                result.append(
                    {
                        "id": row["id"],
                        "label": row.get("label", ""),
                        "node_id": row["node_id"],
                        "path": self._chain_path(chain),
                        **self._share_details(row, chain, token_factory),
                        "created_at": row["created_at"],
                        "expires_at": row["expires_at"],
                    }
                )
            return result

    def _share_details(self, row, chain, factory):
        node, path = chain[-1], self._chain_path(chain)
        return {
            "name": node["name"],
            "type": node["kind"],
            "size": node.get("size"),
            "folder_path": path.rsplit("/", 1)[0] or "/",
            "folder_label": " / ".join(["My Files", *(n["name"] for n in chain[1:-1])]),
            "copyable": bool(
                factory
                and hmac.compare_digest(
                    row["token_hash"],
                    hashlib.sha256(factory(row["id"]).encode()).hexdigest(),
                )
            ),
        }

    def update_share(self, share_id, *, label=_SHARE_UNSET, expires_at=_SHARE_UNSET):
        if label is not _SHARE_UNSET:
            label = _share_label(label)
        expiry = expires_at
        if expires_at is not _SHARE_UNSET and expires_at is not None:
            expiry = _validate_timestamp(expires_at)
        if expiry is not _SHARE_UNSET and expiry is not None and expiry <= _utc_now():
            raise ValidationError("Link expiry must be in the future")
        with self._transaction(write=True):
            row = self._one("shares", {"_id": share_id})
            if row is None:
                raise NotFoundError("Link not found")
            chain = self._ancestors(row["node_id"])
            self._visible(chain)
            self._demo_mutable(chain, descendants=True)
            self._mutable(chain)
            if label is _SHARE_UNSET and expiry is _SHARE_UNSET:
                return
            change, unset = {}, {}
            if expiry is not _SHARE_UNSET:
                change["expires_at"] = expiry
                if expiry is None:
                    unset["expire_at"] = ""
                else:
                    change["expire_at"] = (
                        _expiry_date(expiry) + _EXPIRED_SHARE_RETENTION
                    )
            if label is not _SHARE_UNSET:
                if label:
                    change["label"] = label
                else:
                    unset["label"] = ""
            update = {"$set": change} if change else {}
            if unset:
                update["$unset"] = unset
            self.db.shares.update_one({"_id": share_id}, update, session=self._session)
            self._bump()

    def revoke_share(self, share_id):
        with self._transaction(write=True):
            if share_id.startswith("/"):
                node, _, chain = self._resolve(share_id)
                self._demo_mutable(chain, descendants=True)
                query = {"node_id": node["_id"]}
            else:
                row = self._one("shares", {"_id": share_id})
                if row is None:
                    return False
                self._demo_mutable(self._ancestors(row["node_id"]), descendants=True)
                query = {"_id": share_id}
            removed = self.db.shares.delete_many(
                query, session=self._session
            ).deleted_count
            if removed:
                self._bump()
            return bool(removed)

    def _authorized(self, chain, token):
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 1024
            or self._hidden(chain)
        ):
            return None
        # The token's SHA-256 is the indexed lookup key; the token never is.
        row = self._one(
            "shares", {"token_hash": hashlib.sha256(token.encode()).hexdigest()}
        )
        ids = [item["_id"] for item in chain]
        if (
            row is None
            or row["node_id"] not in ids
            or (row["expires_at"] is not None and row["expires_at"] <= _utc_now())
        ):
            return None
        return self._chain_path(chain[: ids.index(row["node_id"]) + 1])

    def authorize_directory(self, path, token):
        with self._transaction():
            node, _, chain = self._resolve(path)
            if node["kind"] != "folder":
                raise NotDirectoryError("Not a directory")
            return self._authorized(chain, token)

    def authorize_file(self, path, token):
        with self._transaction():
            node, _, chain = self._resolve(path)
            if node["kind"] != "file":
                raise NotFileError("Not a file")
            return self._authorized(chain, token) is not None

    # ----------------------------------------------------------- deletions

    def _pending_for(self, identity):
        return self._one(
            "deletions", {"root_id": identity, "state": {"$ne": "completed"}}
        )

    def validate_deletion(self, path):
        with self._transaction():
            node, path, chain = self._resolve(path)
            if node["_id"] == ROOT_ID:
                raise RootMutationError("The root folder cannot be deleted")
            self._demo_mutable(chain, descendants=True)
            if not self._pending_for(node["_id"]):
                self._mutable(chain, descendants=True)
                if not node["trashed"] or self._hidden(chain[:-1]):
                    raise TrashedItemError(
                        "Permanent deletion requires a top-level trash item"
                    )
            return self._record(node, path)

    def begin_deletion(self, path, *, channel_id=None):
        if channel_id is not None:
            _validate_channel_id(channel_id)
        with self._transaction(write=True):
            node, path, chain = self._resolve(path)
            self.validate_deletion(path)
            prior = self._pending_for(node["_id"])
            if prior:
                if channel_id is not None and prior["channel_id"] != channel_id:
                    raise ValidationError("Deletion channel does not match")
                return self._deletion(prior)
            self._mutable(chain, descendants=True)
            identity = secrets.token_urlsafe(16)
            # shortcut: one document lists every message, so a single deletion is
            # bound by MongoDB's 16 MB document size (~1.5M messages); store the
            # ids in their own collection if a drive ever needs a larger one.
            messages = self._messages(node["_id"])
            row = {
                "_id": identity,
                "id": identity,
                "root_id": node["_id"],
                "path": path,
                "channel_id": channel_id,
                "state": "pending",
                "created_at": _utc_now(),
                "error": None,
                "message_ids": messages,
                "pending_message_ids": messages.copy(),
            }
            self.db.deletions.insert_one(row, session=self._session)
            self._revoke_under(node["_id"])
            self._bump()
            return self._deletion(row)

    @staticmethod
    def _deletion(row):
        return copy.deepcopy(
            {k: v for k, v in row.items() if k not in {"_id", "expire_at"}}
        )

    def pending_deletions(self):
        with self._transaction():
            return [
                self._deletion(row)
                for row in self._find("deletions", {"state": {"$ne": "completed"}})
            ]

    def confirm_deletion_messages(self, intent_id, message_ids):
        validated = {_strict_int(value, "message_id", 1) for value in message_ids}
        with self._transaction(write=True):
            row = self._one("deletions", {"_id": intent_id})
            if row is None:
                raise NotFoundError("Deletion intent not found")
            if row["state"] == "completed":
                return
            if not validated <= set(row["message_ids"]):
                raise ValidationError("Confirmation contains unrelated messages")
            self.db.deletions.update_one(
                {"_id": intent_id},
                {
                    "$set": {
                        "pending_message_ids": sorted(
                            set(row["pending_message_ids"]) - validated
                        ),
                        "state": "pending",
                        "error": None,
                    }
                },
                session=self._session,
            )
            self._bump()

    def fail_deletion(self, intent_id, error):
        with self._transaction(write=True):
            row = self._one("deletions", {"_id": intent_id})
            if row is None or row["state"] == "completed":
                raise NotFoundError("Active deletion intent not found")
            self.db.deletions.update_one(
                {"_id": intent_id},
                {"$set": {"state": "failed", "error": str(error)[:500]}},
                session=self._session,
            )
            self._bump()

    def finish_deletion(self, intent_id):
        """Atomically forget a trashed subtree; retain its Telegram documents."""
        with self._transaction(write=True):
            row = self._one("deletions", {"_id": intent_id})
            if row is None:
                raise NotFoundError("Deletion intent not found")
            messages = row["message_ids"].copy()
            if row["state"] != "completed":
                chain = self._ancestors(row["root_id"])
                self._demo_mutable(chain, descendants=True)
                files, _folders, size = self._subtree_totals(row["root_id"])
                self._remove_subtree(row["root_id"])
                self.db.deletions.update_one(
                    {"_id": intent_id},
                    {
                        "$set": {
                            "state": "completed",
                            "error": None,
                            "message_ids": [],
                            "pending_message_ids": [],
                            "expire_at": datetime.now(UTC) + _RECORD_RETENTION,
                        }
                    },
                    session=self._session,
                )
                self._bump(trashed_files=-files, trashed_bytes=-size)
            return messages

    # ------------------------------------------------------ export / stats

    @classmethod
    def _portable(cls, metadata, nodes, shares, operations, deletions, preparations):
        """Portable schema-5 catalog from raw rows; also validates their shape."""
        try:
            portable, parents = {}, {}
            for row in nodes:
                key = row["_id"]
                if row.get("id") != key or row.get("name_key") != _name_key(
                    row["name"]
                ):
                    raise CorruptStoreError("Catalog node identity or index is invalid")
                value = {
                    "id": key,
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
                    if row.get("parts"):
                        value["parts"] = copy.deepcopy(row["parts"])
                    if row.get("sha256"):
                        value["sha256"] = row["sha256"]
                    if row.get("poster") is not None:
                        value["poster"] = copy.deepcopy(row["poster"])
                else:
                    value["children"] = {}
                portable[key] = value
                parents[key] = row["parent_id"]
            if ROOT_ID not in portable or parents[ROOT_ID] is not None:
                raise CorruptStoreError("MongoDB catalog root is missing")
            for row in shares:
                portable[row["node_id"]]["share_hashes"].append(row["token_hash"])
            for key, parent in parents.items():
                if parent is not None:
                    folder = portable.get(parent)
                    if folder is None or folder["type"] != "folder":
                        raise CorruptStoreError("Invalid catalog parent")
                    folder["children"][key] = portable[key]
            result = {
                "schema_version": SCHEMA_VERSION,
                "drive_id": metadata["drive_id"],
                "revision": metadata["revision"],
                "backed_up_revision": metadata["revision"],
                "root": portable[ROOT_ID],
                "shares": [
                    {k: v for k, v in row.items() if k not in {"_id", "expire_at"}}
                    for row in shares
                ],
                "operations": [
                    {k: v for k, v in row.items() if k not in {"_id", "expire_at"}}
                    for row in operations
                ],
                "deletions": [cls._deletion(row) for row in deletions],
            }
            if metadata.get("storage_channel_id") is not None:
                result["storage_channel_id"] = metadata["storage_channel_id"]
            if preparations:
                result["folder_preparations"] = [
                    {
                        k: copy.deepcopy(v)
                        for k, v in row.items()
                        if k not in {"_id", "expire_at"}
                    }
                    for row in preparations
                ]
        except (KeyError, TypeError, AttributeError) as error:
            raise CorruptStoreError("Catalog contains a malformed record") from error
        cls._validate_payload(result)
        if cls._count(result["root"]) != len(portable):
            raise CorruptStoreError(
                "Catalog contains unreachable nodes or invalid roots"
            )
        return result

    @staticmethod
    def _count(root):
        total, pending = 0, [root]
        while pending:
            node = pending.pop()
            total += 1
            if node["type"] == "folder":
                pending.extend(node["children"].values())
        return total

    @classmethod
    def validate_snapshot_records(cls, records):
        """Validate archived records and return their portable catalog without I/O."""
        catalogs = records["catalog"]
        if len(catalogs) != 1 or catalogs[0].get("_id") != "catalog":
            raise CorruptStoreError("Archive catalog metadata is invalid")
        metadata = catalogs[0]
        if (
            metadata.get("schema_version") != SCHEMA_VERSION
            or type(metadata.get("generation")) is not int
            or metadata["generation"] < 0
            or type(metadata.get("revision")) is not int
            or metadata["revision"] < 1
        ):
            raise CorruptStoreError("Archive catalog schema or revision is invalid")
        return cls._portable(metadata, *(records[name] for name in _TABLES))

    def export(self):
        """Whole portable catalog, read in one snapshot transaction."""

        def read(session):
            return self._portable(
                self.db.catalog.find_one({"_id": "catalog"}, session=session),
                *(list(self.db[name].find({}, session=session)) for name in _TABLES),
            )

        if self._session is not None:
            return read(self._session)
        return self.backend.transaction(read)

    def statistics(self):
        metadata = (
            self._one("catalog", {"_id": "catalog"})
            if self._session is not None
            else self.db.catalog.find_one({"_id": "catalog"})
        )
        return {name: metadata.get(name, 0) for name in _COUNTERS}

    @staticmethod
    def _default_share_expiry() -> str:
        return (
            (datetime.now(UTC) + timedelta(days=7))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

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
        except (ValidationError, TypeError) as exc:
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

    def create_folder(self, parent_path: str, name: str) -> dict[str, Any]:
        return self._add(parent_path, name, None)

    @staticmethod
    def _record(
        row: dict[str, Any], full_path: str, *, message: bool = False
    ) -> dict[str, Any]:
        result = dict(
            id=row["_id"],
            name=row["name"],
            path=_parent_path(full_path),
            size=row["size"],
            type=row["kind"],
            upload_date=row["upload_date"],
        )
        if row.get("starred"):
            result["starred"] = True
        if row["kind"] == "file":
            result["part_count"] = row["part_count"]
        if message:
            result["message_id"] = row["message_id"]
            if row.get("sha256") is not None:
                result["sha256"] = row["sha256"]
        return result

    @staticmethod
    def _visible(chain: list[dict[str, Any]], include_trashed: bool = False) -> None:
        if not include_trashed and any(row["trashed"] for row in chain):
            raise TrashedItemError("item or one of its ancestors is trashed")
