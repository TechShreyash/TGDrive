"""Native MongoDB catalog with atomic commits and a single-owner read cache.

The cache is rebuilt from MongoDB at startup. No local catalog file or SQL
database participates in MongoDB operation. Mutations commit changed documents
and the catalog generation together before the caller observes success.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import threading
import unicodedata
from contextlib import contextmanager
from typing import Any

from pymongo import DeleteOne, ReplaceOne

from tgdrive.core.errors import (
    CorruptStoreError,
    DuplicateNameError,
    InvalidPathError,
    InvalidRevisionError,
    NotDirectoryError,
    NotFileError,
    NotFoundError,
    RootMutationError,
    TrashedItemError,
    ValidationError,
)
from tgdrive.storage.demo_files import DEMO_FILES_ID, DEMO_FILES_NAME, check_ids
from tgdrive.storage.mongo import DatabaseUnavailable, MongoBackend
from tgdrive.storage.store import (
    _SHARE_UNSET,
    MAX_NAME_LENGTH,
    MAX_SEARCH_OFFSET,
    MAX_SEARCH_RESULTS,
    MAX_SHARE_TOKENS_PER_ITEM,
    MAX_TREE_DEPTH,
    MAX_TREE_ITEMS,
    ROOT_ID,
    SCHEMA_VERSION,
    DriveStore,
    FolderTreeConflictError,
    _folder_manifest,
    _join_path,
    _manifest_json,
    _name_key,
    _normalize_name,
    _normalize_path,
    _share_label,
    _strict_int,
    _utc_now,
    _validate_channel_id,
    _validate_file_rename,
    _validate_id,
    _validate_timestamp,
)

_TABLES = ("nodes", "shares", "operations", "deletions", "preparations")


class MongoStore(DriveStore):
    backend_name = "mongodb"

    def __init__(self, backend: MongoBackend):
        self.backend = backend
        self._lock = threading.RLock()
        self._local = threading.local()
        self._closed = False
        self._committed: dict[str, Any] = {}
        self._reload()
        self._indexes(backend)

    @property
    def _state(self):
        return getattr(
            self._local, "working", getattr(self._local, "reader", self._committed)
        )

    @property
    def _depth(self):
        return getattr(self._local, "depth", 0)

    @_depth.setter
    def _depth(self, value):
        self._local.depth = value

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
        backend.db.nodes.create_index(
            [("parent_id", 1), ("name_key", 1)],
            unique=True,
            partialFilterExpression={"trashed": False},
            name="active_sibling_names",
        )
        backend.db.nodes.create_index("parent_id")
        backend.db.shares.create_index("token_hash", unique=True)
        backend.db.shares.create_index("node_id")
        backend.db.deletions.create_index("root_id")
        backend.db.receipts.create_index("purge_at", expireAfterSeconds=0)

    @staticmethod
    def _node(identity, parent, name, kind, **values):
        return {
            "_id": identity,
            "id": identity,
            "parent_id": parent,
            "name": name,
            "name_key": _name_key(name),
            "kind": kind,
            "trashed": False,
            "upload_date": _utc_now(),
            "size": 0,
            "message_id": None,
            "part_count": 1,
            "sha256": None,
            **values,
        }

    def _reload(self):
        def read(session):
            metadata = self.backend.db.catalog.find_one(
                {"_id": "catalog"}, session=session
            )
            if metadata is None:
                raise CorruptStoreError(
                    "MongoDB has no TGDrive catalog. Open owner recovery to "
                    "restore a confirmed backup."
                )
            if metadata.get("schema_version") != SCHEMA_VERSION:
                raise CorruptStoreError("Unsupported MongoDB catalog schema")
            return {
                "metadata": metadata,
                **{
                    name: {
                        row["_id"]: row
                        for row in self.backend.db[name].find({}, session=session)
                    }
                    for name in _TABLES
                },
            }

        state = self.backend.transaction(read)
        self._committed = state
        self._statistics_cache = None
        if hasattr(self._local, "working"):
            self._local.working = state
        self._validate_state()

    def _validate_state(self):
        for key, row in self._state["nodes"].items():
            if (
                row.get("id") != key
                or row.get("name_key") != _name_key(row["name"])
                or type(row.get("starred", False)) is not bool
                or type(row.get("part_count")) is not int
                or row["part_count"] != len(row.get("parts") or [None])
            ):
                raise CorruptStoreError("Catalog node identity or index is invalid")
        for name, identity in (
            ("shares", "id"),
            ("operations", "operation_id"),
            ("deletions", "id"),
            ("preparations", "request_id"),
        ):
            if any(row.get(identity) != key for key, row in self._state[name].items()):
                raise CorruptStoreError("Catalog recovery record identity is invalid")
        if ROOT_ID not in self._state["nodes"]:
            raise CorruptStoreError("MongoDB catalog root is missing")
        roots = [
            row["id"]
            for row in self._state["nodes"].values()
            if row["parent_id"] is None
        ]
        if roots != [ROOT_ID] or len(self._subtree(ROOT_ID)) != len(
            self._state["nodes"]
        ):
            raise CorruptStoreError(
                "Catalog contains unreachable nodes or invalid roots"
            )
        # Reuse portable domain validation without creating a SQLite database.
        self._validate_payload(self._export())

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
            or type(metadata.get("backed_up_revision")) is not int
            or not 0 <= metadata["backed_up_revision"] <= metadata["revision"]
        ):
            raise CorruptStoreError("Archive catalog schema or revision is invalid")
        view = object.__new__(cls)
        view._local = threading.local()
        view._committed = {
            "metadata": metadata,
            **{name: {row["_id"]: row for row in records[name]} for name in _TABLES},
        }
        if any(row["id"] != row["_id"] for row in records["nodes"]):
            raise CorruptStoreError("Archive node identity is invalid")
        view._validate_state()
        return view._export()

    @property
    def storage_path(self):
        return None

    @property
    def database_path(self):
        return None

    def close(self):
        self._closed = True

    @contextmanager
    def _transaction(self, *, write=False):
        if self._closed:
            raise DatabaseUnavailable("The catalog is closed")
        if not write:
            previous = getattr(self._local, "reader", None)
            self._local.reader = self._state
            try:
                yield self._state
            finally:
                if previous is None:
                    del self._local.reader
                else:
                    self._local.reader = previous
            return
        with self._lock:
            self.backend.check_owner()
            outer = self._depth == 0
            before = self._committed if outer else copy.deepcopy(self._state)
            if outer:
                self._local.working = copy.deepcopy(before)
            self._depth += 1
            try:
                yield self._state
                if outer and self._state != before:
                    self._commit(before)
                    self._committed = self._state
                    self._statistics_cache = None
            except BaseException:
                if not outer:
                    self._local.working = before
                raise
            finally:
                self._depth -= 1
                if outer:
                    del self._local.working

    @contextmanager
    def metadata_batch(self):
        with self._transaction(write=True):
            yield

    def _commit(self, before):
        after = self._state
        generation = before["metadata"]["generation"]
        metadata = dict(after["metadata"], generation=generation + 1)
        metadata["backed_up_revision"] = metadata["revision"]
        changes = {}
        for name in _TABLES:
            old, new = before[name], after[name]
            changes[name] = [
                *[DeleteOne({"_id": key}) for key in old.keys() - new.keys()],
                *[
                    ReplaceOne({"_id": key}, value, upsert=True)
                    for key, value in new.items()
                    if old.get(key) != value
                ],
            ]

        def commit(session):
            self.backend.fence(session)
            result = self.backend.db.catalog.replace_one(
                {"_id": "catalog", "generation": generation},
                metadata,
                session=session,
            )
            if result.matched_count != 1:
                raise DatabaseUnavailable(
                    "The MongoDB catalog changed elsewhere. Restart to reload it."
                )
            renamed = [
                key
                for key in before["nodes"].keys() & after["nodes"].keys()
                if any(
                    before["nodes"][key].get(field) != after["nodes"][key].get(field)
                    for field in ("name_key", "parent_id", "trashed")
                )
            ]
            if renamed:
                # Free changed uniqueness slots inside the transaction so a valid
                # multi-rename/name swap is not rejected by intermediate ordering.
                self.backend.db.nodes.update_many(
                    {"_id": {"$in": renamed}},
                    {"$set": {"trashed": True}},
                    session=session,
                )
            for name, operations in changes.items():
                if operations:
                    self.backend.db[name].bulk_write(
                        operations, ordered=True, session=session
                    )

        try:
            self.backend.transaction(commit)
        except Exception:
            # A lost commit acknowledgement is not proof of rollback. Rebuild
            # authority from MongoDB before admitting another operation.
            try:
                self._reload()
            except Exception:
                self._closed = True
            raise
        after["metadata"] = metadata

    def _bump(self):
        self._state["metadata"]["revision"] += 1

    @property
    def drive_id(self):
        return self._state["metadata"]["drive_id"]

    @property
    def revision(self):
        return self._state["metadata"]["revision"]

    @property
    def storage_channel_id(self):
        return self._state["metadata"].get("storage_channel_id")

    @property
    def dirty(self):
        return False

    def bind_storage_channel(self, channel_id):
        channel_id = _validate_channel_id(channel_id)
        with self._transaction(write=True):
            if self.storage_channel_id == channel_id:
                return
            if self.storage_channel_id is not None:
                raise ValidationError(
                    "The drive belongs to a different storage channel"
                )
            self._state["metadata"]["storage_channel_id"] = channel_id
            self._bump()

    def has_file_operation(self, operation_id, *, message_id=None):
        _validate_id(operation_id)
        if message_id is not None:
            message_id = _strict_int(message_id, "message_id", 1)
        with self._transaction():
            operation = self._state["operations"].get(operation_id)
            if operation is None:
                return False
            return message_id is None or (
                self._state["nodes"].get(operation["node_id"], {}).get("message_id")
                == message_id
            )

    def _resolve(self, path):
        parts, path = _normalize_path(path)
        node = self._state["nodes"][ROOT_ID]
        chain = [node]
        for identity in parts:
            if node["kind"] != "folder":
                raise NotDirectoryError("A path component is not a folder")
            child = self._state["nodes"].get(identity)
            if child is None or child["parent_id"] != node["id"]:
                raise NotFoundError("Item not found")
            node = child
            chain.append(node)
        return node, path, chain

    def _path(self, identity):
        parts, visited = [], set()
        while identity != ROOT_ID:
            if identity in visited or len(parts) > MAX_TREE_DEPTH:
                raise CorruptStoreError("Invalid catalog ancestry")
            visited.add(identity)
            node = self._state["nodes"].get(identity)
            if node is None:
                raise NotFoundError("Item does not exist")
            parts.append(identity)
            identity = node["parent_id"]
        return "/" + "/".join(reversed(parts))

    def resolve_id(self, item_id):
        _validate_id(item_id, allow_root=True)
        with self._transaction():
            return self._path(item_id)

    def _subtree(self, identity):
        children = {}
        for row in self._state["nodes"].values():
            children.setdefault(row["parent_id"], []).append(row["id"])
        result, pending, seen = [], [identity], set()
        while pending:
            current = pending.pop()
            if current in seen:
                raise CorruptStoreError("Catalog contains a cycle")
            seen.add(current)
            result.append(current)
            pending.extend(children.get(current, []))
        return result

    def _mutable(self, chain, *, descendants=False):
        self._demo_mutable(chain, descendants=descendants)
        roots = {
            row["root_id"]
            for row in self._state["deletions"].values()
            if row["state"] != "completed"
        }
        if roots.intersection(row["id"] for row in chain) or (
            descendants and roots.intersection(self._subtree(chain[-1]["id"]))
        ):
            raise ValidationError("Item is pending permanent deletion")

    def _demo_mutable(self, chain, *, descendants=False):
        from tgdrive.storage.demo_files import restricted_writes

        if not restricted_writes.get():
            return
        check_ids(row["id"] for row in chain)
        if descendants:
            check_ids(self._subtree(chain[-1]["id"]))

    def ensure_demo_files(self):
        with self._transaction(write=True):
            node = self._state["nodes"].get(DEMO_FILES_ID)
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
            if len(self._state["nodes"]) >= MAX_TREE_ITEMS:
                raise ValidationError("Maximum catalog size exceeded")
            node = self._node(DEMO_FILES_ID, ROOT_ID, DEMO_FILES_NAME, "folder")
            self._state["nodes"][DEMO_FILES_ID] = node
            self._bump()
            return self._record(node, "/" + DEMO_FILES_ID)

    def _unique(self, parent, name, exclude=""):
        return not any(
            row["parent_id"] == parent
            and row["name_key"] == _name_key(name)
            and row["id"] != exclude
            and not row["trashed"]
            for row in self._state["nodes"].values()
        )

    def _available(self, parent, name, kind, exclude=""):
        if self._unique(parent, name, exclude):
            return name
        from pathlib import Path

        suffix = Path(name).suffix if kind == "file" else ""
        stem = name[: -len(suffix)] if suffix else name
        index = 1
        while True:
            label = f" ({index})"
            ending = suffix[: MAX_NAME_LENGTH - len(label) - 1]
            value = stem[: MAX_NAME_LENGTH - len(label) - len(ending)] + label + ending
            if self._unique(parent, value, exclude):
                return value
            index += 1

    def _file(self, node, path):
        result = self._record(node, path, message=True)
        if node.get("parts"):
            result["parts"] = copy.deepcopy(node["parts"])
        if node.get("poster"):
            result["poster"] = copy.deepcopy(node["poster"])
        return result

    def transfer_location(self, operation_id, destination="/", *, completed=False):
        """Resolve committed upload identities against the current read snapshot."""
        if operation_id is not None:
            _validate_id(operation_id)
        with self._transaction():
            operation = self._state["operations"].get(operation_id)
            if operation is not None:
                path = self._path(operation["node_id"])
            elif completed:
                raise NotFoundError("Uploaded item is unavailable")
            else:
                parts, _ = _normalize_path(destination)
                path = self._path(parts[-1] if parts else ROOT_ID)
            node, path, chain = self._resolve(path)
            self._visible(chain)
            from tgdrive.transfers.transfer_locations import location_record

            return location_record(node, path, chain, stored=operation is not None)

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
            return {"id": node["id"], "path": path}

    def get_directory(self, path, *, include_trashed=False):
        with self._transaction():
            node, path, chain = self._resolve(path)
            if node["kind"] != "folder":
                raise NotDirectoryError("Not a directory")
            self._visible(chain, include_trashed)
            result = self._record(node, path)
            result["contents"] = {
                row["id"]: self._record(row, _join_path(path, row["id"]))
                for row in self._state["nodes"].values()
                if row["parent_id"] == node["id"]
                and (include_trashed or not row["trashed"])
            }
            return result

    def list_directory(self, path, *, include_trashed=False):
        return self.get_directory(path, include_trashed=include_trashed)["contents"]

    def get_file(self, path, *, include_trashed=False):
        with self._transaction():
            node, path, chain = self._resolve(path)
            if node["kind"] != "file":
                raise NotFileError("Not a file")
            self._visible(chain, include_trashed)
            return self._file(node, path)

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
            if operation_id in self._state["operations"]:
                previous = self._state["operations"][operation_id]
                node = self._state["nodes"].get(previous["node_id"])
                if node is None:
                    raise ValidationError("This operation's file was already deleted")
                result = self._file(node, self._path(node["id"]))
                if (
                    node["message_id"] != message_id
                    or node["size"] != size
                    or result.get("parts") != parts
                    or result.get("sha256") != sha256
                    or result.get("poster") != poster
                ):
                    raise ValidationError("Operation already committed different media")
                return result
            parent, parent_path, chain = self._resolve(parent_path)
            if parent["kind"] != "folder":
                raise NotDirectoryError("Destination must be a folder")
            self._visible(chain)
            self._mutable(chain)
            if (
                len(chain) > MAX_TREE_DEPTH
                or len(self._state["nodes"]) >= MAX_TREE_ITEMS
            ):
                raise ValidationError("Maximum catalog depth or item count exceeded")
            kind = "folder" if message_id is None else "file"
            if keep_both:
                name = self._available(parent["id"], name, kind)
            elif not self._unique(parent["id"], name):
                raise DuplicateNameError("An item with this name already exists")
            identity = secrets.token_urlsafe(16)
            row = self._node(
                identity,
                parent["id"],
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
            self._state["nodes"][identity] = row
            if operation_id is not None:
                self._state["operations"][operation_id] = {
                    "_id": operation_id,
                    "operation_id": operation_id,
                    "node_id": identity,
                }
            self._bump()
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
            prior = self._state["preparations"].get(request_id)
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
            chain = self._resolve(parent["path"])[2]
            depth = 1 + max((len(parts) for parts in manifest["folders"]), default=0)
            if len(chain) - 1 + depth > MAX_TREE_DEPTH:
                raise ValidationError("Maximum tree depth exceeded")
            if (
                len(self._state["nodes"]) + 1 + len(manifest["folders"])
                > MAX_TREE_ITEMS
            ):
                raise ValidationError("Maximum catalog size exceeded")
            root_name = self._available(parent["id"], manifest["root_name"], "folder")
            mapping = {}
            root = None
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
                self._state["nodes"][identity] = node
                mapping["/".join(segments)] = _join_path(parent_path, identity)
                if not segments:
                    root = self._record(node, mapping[""])
            result = {"root": root, "folders": mapping}
            self._bump()
            self._state["preparations"][request_id] = {
                "_id": request_id,
                "request_id": request_id,
                "fingerprint": fingerprint,
                "manifest": manifest,
                "result": result,
                "created_at": _utc_now(),
            }
            return copy.deepcopy(result)

    def rename(self, path, new_name):
        name = _normalize_name(new_name)
        with self._transaction(write=True):
            node, path, chain = self._resolve(path)
            if node["id"] == ROOT_ID:
                raise RootMutationError("The root folder cannot be renamed")
            self._demo_mutable(chain, descendants=True)
            self._mutable(chain)
            if node["kind"] == "file":
                _validate_file_rename(node["name"], name)
            if not self._unique(node["parent_id"], name, node["id"]):
                raise DuplicateNameError("An item with this name already exists")
            if node["name"] != name:
                node.update(name=name, name_key=_name_key(name))
                self._bump()
            return self._record(node, path)

    def _revoke(self, identities):
        ids = set(identities)
        for key, row in list(self._state["shares"].items()):
            if row["node_id"] in ids:
                del self._state["shares"][key]

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
        if node["id"] == ROOT_ID:
            raise RootMutationError("The root folder cannot be trashed")
        self._mutable(chain, descendants=True)
        if not trashed:
            self._visible(chain[:-1])
        if node["trashed"] != trashed:
            name = (
                node["name"]
                if trashed
                else self._available(
                    node["parent_id"], node["name"], node["kind"], node["id"]
                )
            )
            node.update(trashed=trashed, name=name, name_key=_name_key(name))
            if trashed:
                self._revoke(self._subtree(node["id"]))
            self._bump()
        return self._record(node, path)

    def list_trash(self, query=""):
        with self._transaction():
            result = {}
            deleting = {
                row["root_id"]
                for row in self._state["deletions"].values()
                if row["state"] == "pending"
            }
            for node in self._state["nodes"].values():
                if (
                    node["trashed"]
                    and node["id"] not in deleting
                    and query.casefold() in node["name"].casefold()
                ):
                    path = self._path(node["id"])
                    chain = self._resolve(path)[2]
                    if not any(row["trashed"] for row in chain[:-1]):
                        result[node["id"]] = self._record(node, path) | {
                            "parent_names": [item["name"] for item in chain[1:-1]]
                        }
            return result

    def set_starred(self, path, starred):
        if type(starred) is not bool:
            raise ValidationError("starred must be a boolean")
        with self._transaction(write=True):
            node, path, chain = self._resolve(path)
            if node["id"] == ROOT_ID:
                raise RootMutationError("the drive root cannot be starred")
            self._mutable(chain)
            self._visible(chain)
            if bool(node.get("starred", False)) != starred:
                node["starred"] = starred
                self._bump()
            return self._record(node, path)

    def list_starred(self, query=""):
        if not isinstance(query, str) or len(query) > MAX_NAME_LENGTH:
            raise ValidationError("invalid starred search")
        with self._transaction():
            results = {}
            for node in self._state["nodes"].values():
                if (
                    not node.get("starred")
                    or node["id"] == ROOT_ID
                    or _name_key(query.strip()) not in node["name_key"]
                ):
                    continue
                path = self._path(node["id"])
                chain = self._resolve(path)[2]
                if any(item["trashed"] for item in chain):
                    continue
                results[node["id"]] = self._record(node, path) | {
                    "parent_names": [row["name"] for row in chain[1:-1]]
                }
            return results

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
            raise ValidationError("Invalid search pagination or sort")
        direction = order or ("asc" if sort == "name" else "desc")
        with self._transaction():
            found = []
            for node in self._state["nodes"].values():
                if node["id"] == ROOT_ID or query.casefold() not in node["name_key"]:
                    continue
                path = self._path(node["id"])
                chain = self._resolve(path)[2]
                if not include_trashed and any(row["trashed"] for row in chain):
                    continue
                row = self._record(node, path)
                row["parent_names"] = [item["name"] for item in chain[1:-1]]
                found.append(row)
            found.sort(key=lambda r: (_name_key(r["name"]), r["id"]))
            found.sort(
                key=lambda r: (
                    _name_key(r["name"])
                    if sort == "name"
                    else r["size"]
                    if sort == "size"
                    else r["upload_date"]
                ),
                reverse=direction == "desc",
            )
            found.sort(key=lambda r: r["type"] != "folder")
            items = found[offset : offset + limit]
            more = len(found) > offset + limit
            return {
                "items": items,
                "query": query,
                "has_more": more,
                "next_offset": offset + len(items) if more else None,
            }

    def _messages(self, identity):
        result = set()
        for key in self._subtree(identity):
            row = self._state["nodes"][key]
            if row["kind"] == "file":
                result.add(row["message_id"])
                result.update(part["message_id"] for part in row.get("parts", []))
                if row.get("poster"):
                    result.add(row["poster"]["message_id"])
        return sorted(result)

    def message_ids(self, path, *, include_trashed=False):
        with self._transaction():
            node, _, chain = self._resolve(path)
            self._visible(chain, include_trashed)
            return self._messages(node["id"])

    def _remove_subtree(self, identity):
        ids = self._subtree(identity)
        self._revoke(ids)
        for key in ids:
            del self._state["nodes"][key]

    def delete(self, path):
        with self._transaction(write=True):
            node, _, chain = self._resolve(path)
            if node["id"] == ROOT_ID:
                raise RootMutationError("The root folder cannot be deleted")
            self._mutable(chain, descendants=True)
            if self._messages(node["id"]):
                raise ValidationError("Files require a permanent deletion intent")
            self._remove_subtree(node["id"])
            self._bump()
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
        if node["id"] == ROOT_ID:
            raise RootMutationError("The root folder cannot be moved")
        if parent["kind"] != "folder":
            raise NotDirectoryError("Destination must be a folder")
        self._visible(chain)
        self._visible(parent_chain)
        self._mutable(chain, descendants=True)
        self._mutable(parent_chain)
        if node["id"] in {row["id"] for row in parent_chain}:
            raise InvalidPathError("A folder cannot move into its subtree")
        if node["parent_id"] == parent["id"]:
            return self._record(node, path)
        for key in self._subtree(node["id"]):
            depth = len(self._path(key).split("/")) - len(path.split("/"))
            if len(parent_chain) + depth > MAX_TREE_DEPTH:
                raise ValidationError("Move would exceed maximum tree depth")
        name = node["name"]
        if not self._unique(parent["id"], name):
            if conflict == "skip":
                return None
            name = self._available(parent["id"], name, node["kind"])
        node.update(parent_id=parent["id"], name=name, name_key=_name_key(name))
        self._bump()
        return self._record(node, _join_path(destination, node["id"]))

    def create_share(
        self,
        path,
        *,
        expires_at=DriveStore._DEFAULT_EXPIRY,
        token_factory=None,
        label="",
        replace_existing=False,
    ):
        expiry = (
            self._default_share_expiry()
            if expires_at is DriveStore._DEFAULT_EXPIRY
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
                self._revoke([node["id"]])
            if (
                sum(
                    row["node_id"] == node["id"]
                    for row in self._state["shares"].values()
                )
                >= MAX_SHARE_TOKENS_PER_ITEM
            ):
                raise ValidationError(
                    "This item has 100 links. Remove an old link first."
                )
            identity = secrets.token_urlsafe(16)
            token = (
                token_factory(identity) if token_factory else secrets.token_urlsafe(32)
            )
            self._state["shares"][identity] = {
                "_id": identity,
                "id": identity,
                "node_id": node["id"],
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "created_at": created,
                "expires_at": expiry,
                **({"label": label} if label else {}),
            }
            self._bump()
            return token

    def list_shares(self, path=None, *, include_expired=False, token_factory=None):
        with self._transaction():
            identity = self._resolve(path)[0]["id"] if path is not None else None
            return [
                {
                    "id": row["id"],
                    "label": row.get("label", ""),
                    "node_id": row["node_id"],
                    "path": self._path(row["node_id"]),
                    **self._share_details(row, token_factory),
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                }
                for row in self._state["shares"].values()
                if (identity is None or row["node_id"] == identity)
                and (
                    include_expired
                    or row["expires_at"] is None
                    or row["expires_at"] > _utc_now()
                )
            ]

    def _share_details(self, row, factory):
        node, path, chain = self._resolve(self._path(row["node_id"]))
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
            row = self._state["shares"].get(share_id)
            if row is None:
                raise NotFoundError("Link not found")
            _, _, chain = self._resolve(self._path(row["node_id"]))
            self._visible(chain)
            self._demo_mutable(chain, descendants=True)
            self._mutable(chain)
            if label is _SHARE_UNSET and expiry is _SHARE_UNSET:
                return
            if expiry is not _SHARE_UNSET:
                row["expires_at"] = expiry
            if label is not _SHARE_UNSET:
                if label:
                    row["label"] = label
                else:
                    row.pop("label", None)
            self._bump()

    def revoke_share(self, share_id):
        with self._transaction(write=True):
            if share_id.startswith("/"):
                self._demo_mutable(self._resolve(share_id)[2], descendants=True)
            elif share_id in self._state["shares"]:
                node_id = self._state["shares"][share_id]["node_id"]
                self._demo_mutable(
                    self._resolve(self._path(node_id))[2], descendants=True
                )
            keys = (
                [
                    key
                    for key, row in self._state["shares"].items()
                    if row["node_id"] == self._resolve(share_id)[0]["id"]
                ]
                if share_id.startswith("/")
                else [share_id]
            )
            removed = False
            for key in keys:
                removed |= self._state["shares"].pop(key, None) is not None
            if removed:
                self._bump()
            return removed

    def _authorized(self, chain, token):
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 1024
            or any(row["trashed"] for row in chain)
        ):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        ids = {row["id"] for row in chain}
        for row in self._state["shares"].values():
            if (
                row["node_id"] in ids
                and (row["expires_at"] is None or row["expires_at"] > _utc_now())
                and hmac.compare_digest(row["token_hash"], digest)
            ):
                return self._path(row["node_id"])
        return None

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

    def _pending_for(self, identity):
        return next(
            (
                row
                for row in self._state["deletions"].values()
                if row["root_id"] == identity and row["state"] != "completed"
            ),
            None,
        )

    def validate_deletion(self, path):
        with self._transaction():
            node, path, chain = self._resolve(path)
            if node["id"] == ROOT_ID:
                raise RootMutationError("The root folder cannot be deleted")
            self._demo_mutable(chain, descendants=True)
            if not self._pending_for(node["id"]):
                self._mutable(chain, descendants=True)
                if not node["trashed"] or any(row["trashed"] for row in chain[:-1]):
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
            prior = self._pending_for(node["id"])
            if prior:
                if channel_id is not None and prior["channel_id"] != channel_id:
                    raise ValidationError("Deletion channel does not match")
                return self._deletion(prior)
            self._mutable(chain, descendants=True)
            identity = secrets.token_urlsafe(16)
            messages = self._messages(node["id"])
            row = {
                "_id": identity,
                "id": identity,
                "root_id": node["id"],
                "path": path,
                "channel_id": channel_id,
                "state": "pending",
                "created_at": _utc_now(),
                "error": None,
                "message_ids": messages,
                "pending_message_ids": messages.copy(),
            }
            self._state["deletions"][identity] = row
            self._revoke(self._subtree(node["id"]))
            self._bump()
            return self._deletion(row)

    @staticmethod
    def _deletion(row):
        return copy.deepcopy({key: value for key, value in row.items() if key != "_id"})

    def pending_deletions(self):
        with self._transaction():
            return [
                self._deletion(row)
                for row in self._state["deletions"].values()
                if row["state"] != "completed"
            ]

    def confirm_deletion_messages(self, intent_id, message_ids):
        validated = {_strict_int(value, "message_id", 1) for value in message_ids}
        with self._transaction(write=True):
            row = self._state["deletions"].get(intent_id)
            if row is None:
                raise NotFoundError("Deletion intent not found")
            if not validated <= set(row["message_ids"]):
                raise ValidationError("Confirmation contains unrelated messages")
            if row["state"] == "completed":
                return
            row.update(
                pending_message_ids=sorted(set(row["pending_message_ids"]) - validated),
                state="pending",
                error=None,
            )
            self._bump()

    def fail_deletion(self, intent_id, error):
        with self._transaction(write=True):
            row = self._state["deletions"].get(intent_id)
            if row is None or row["state"] == "completed":
                raise NotFoundError("Active deletion intent not found")
            row.update(state="failed", error=str(error)[:500])
            self._bump()

    def finish_deletion(self, intent_id):
        """Atomically forget a trashed subtree; retain its Telegram documents."""
        with self._transaction(write=True):
            row = self._state["deletions"].get(intent_id)
            if row is None:
                raise NotFoundError("Deletion intent not found")
            messages = row["message_ids"].copy()
            if row["state"] != "completed":
                self._demo_mutable(
                    self._resolve(self._path(row["root_id"]))[2], descendants=True
                )
                self._remove_subtree(row["root_id"])
                row.update(
                    state="completed",
                    error=None,
                    message_ids=[],
                    pending_message_ids=[],
                )
                self._bump()
            return messages

    def _export(self):
        nodes = {}
        for key, row in self._state["nodes"].items():
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
            nodes[key] = value
        for row in self._state["shares"].values():
            nodes[row["node_id"]]["share_hashes"].append(row["token_hash"])
        for key, row in self._state["nodes"].items():
            if row["parent_id"] is not None:
                parent = nodes.get(row["parent_id"])
                if parent is None or parent["type"] != "folder":
                    raise CorruptStoreError("Invalid catalog parent")
                parent["children"][key] = nodes[key]
        metadata = self._state["metadata"]
        result = {
            "schema_version": SCHEMA_VERSION,
            "drive_id": metadata["drive_id"],
            "revision": metadata["revision"],
            "backed_up_revision": metadata["revision"],
            "root": nodes[ROOT_ID],
            "shares": [
                {k: v for k, v in row.items() if k != "_id"}
                for row in self._state["shares"].values()
            ],
            "operations": [
                {k: v for k, v in row.items() if k != "_id"}
                for row in self._state["operations"].values()
            ],
            "deletions": [
                self._deletion(row) for row in self._state["deletions"].values()
            ],
        }
        if metadata.get("storage_channel_id") is not None:
            result["storage_channel_id"] = metadata["storage_channel_id"]
        if self._state["preparations"]:
            result["folder_preparations"] = [
                {k: copy.deepcopy(v) for k, v in row.items() if k != "_id"}
                for row in self._state["preparations"].values()
            ]
        return result

    def export(self):
        with self._transaction():
            return self._export()

    def mark_backed_up(self, revision):
        if type(revision) is not int or not 0 <= revision <= self.revision:
            raise InvalidRevisionError("Invalid catalog revision")
        return False

    def statistics(self):
        with self._transaction():
            state = self._state
            cached = getattr(self, "_statistics_cache", None)
            if cached is not None and cached[0] is state:
                return dict(cached[1])
            result = {
                "files": 0,
                "folders": 0,
                "total_bytes": 0,
                "trashed_files": 0,
                "trashed_bytes": 0,
            }
            for row in self._state["nodes"].values():
                if row["id"] == ROOT_ID:
                    continue
                hidden = any(
                    item["trashed"] for item in self._resolve(self._path(row["id"]))[2]
                )
                if row["kind"] == "file":
                    result["trashed_files" if hidden else "files"] += 1
                    result["trashed_bytes" if hidden else "total_bytes"] += row["size"]
                elif not hidden:
                    result["folders"] += 1
            # Working batches can change again before committing or rolling back.
            if state is self._committed:
                self._statistics_cache = (state, result)
            return dict(result)
