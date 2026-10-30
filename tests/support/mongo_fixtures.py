"""Explicit, isolated MongoDB doubles for settings and application unit tests."""

from __future__ import annotations

import copy
import re
import threading
from contextlib import contextmanager
from types import SimpleNamespace

from pymongo import DeleteOne, ReplaceOne, UpdateOne
from pymongo.errors import DuplicateKeyError


def _value(document, key):
    for index, part in enumerate(key.split(".")):
        if isinstance(document, list):
            # MongoDB matches a field inside any array element.
            rest = ".".join(key.split(".")[index:])
            found = [_value(row, rest) for row in document]
            return next((value for value in found if value is not None), None)
        if not isinstance(document, dict) or part not in document:
            return None
        document = document[part]
    return document


def _matches(document, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(document, item) for item in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches(document, item) for item in expected):
                return False
            continue
        actual = _value(document, key)
        if isinstance(expected, dict):
            for operator, value in expected.items():
                if operator == "$in" and not (
                    any(item in value for item in actual)
                    if isinstance(actual, list)
                    else actual in value
                ):
                    return False
                if operator == "$nin" and actual in value:
                    return False
                if operator == "$ne" and actual == value:
                    return False
                if operator == "$exists" and (actual is not None) != value:
                    return False
                if operator == "$gt" and not (actual is not None and actual > value):
                    return False
                if operator == "$gte" and not (actual is not None and actual >= value):
                    return False
                if operator == "$lt" and not (actual is not None and actual < value):
                    return False
                if operator == "$lte" and not (actual is not None and actual <= value):
                    return False
                if operator == "$regex" and not (
                    any(re.search(value, str(item)) for item in actual)
                    if isinstance(actual, list)
                    else re.search(value, str(actual or ""))
                ):
                    return False
                if operator == "$all" and not (
                    isinstance(actual, list) and all(item in actual for item in value)
                ):
                    return False
                if operator == "$elemMatch" and not (
                    isinstance(actual, list)
                    and any(_matches({"v": item}, {"v": value}) for item in actual)
                ):
                    return False
        elif isinstance(actual, list) and not isinstance(expected, list):
            if expected not in actual:
                return False
        elif actual != expected:
            return False
    return True


def _sort_key(value):
    # MongoDB orders null before numbers before strings.
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (3, value)
    if isinstance(value, int | float):
        return (1, value)
    return (2, str(value))


def _project(row, projection):
    if not projection:
        return row
    include = {k.split(".")[0] for k, v in projection.items() if v}
    if include:
        return {k: v for k, v in row.items() if k in include or k == "_id"}
    return {k: v for k, v in row.items() if k not in projection}


def _apply(row, update):
    for field, value in update.get("$set", {}).items():
        row[field] = copy.deepcopy(value)
    for field in update.get("$unset", {}):
        row.pop(field, None)
    for field, value in update.get("$inc", {}).items():
        row[field] = row.get(field, 0) + value
    for field, value in update.get("$max", {}).items():
        row[field] = max(row.get(field, value), value)
    for field, value in update.get("$push", {}).items():
        row.setdefault(field, []).append(copy.deepcopy(value))
    for field, value in update.get("$pull", {}).items():
        values = value.get("$in", [value]) if isinstance(value, dict) else [value]
        row[field] = [item for item in row.get(field, []) if item not in values]


class Cursor(list):
    def sort(self, fields, direction=None):
        if isinstance(fields, str):
            fields = [(fields, direction or 1)]
        for key, direction in reversed(fields):
            super().sort(
                key=lambda row, key=key: _sort_key(_value(row, key)),
                reverse=direction < 0,
            )
        return self

    def skip(self, count):
        return Cursor(self[count:])

    def limit(self, count):
        return Cursor(self[:count] if count else self)

    def batch_size(self, _count):
        return self


class Collection:
    def __init__(self):
        self.rows = []

    def find(self, query=None, projection=None, **_kwargs):
        return Cursor(
            copy.deepcopy(
                [_project(r, projection) for r in self.rows if _matches(r, query or {})]
            )
        )

    def find_one(self, query=None, projection=None, *, sort=None, **_kwargs):
        rows = self.find(query)
        if sort:
            rows.sort(sort)
        row = next(iter(rows), None)
        return None if row is None else _project(row, projection)

    def aggregate(self, pipeline, **_kwargs):
        """Only the ancestor lookup shape the catalog uses: $match + $graphLookup."""
        rows = list(self.rows)
        for stage in pipeline:
            if "$match" in stage:
                rows = [row for row in rows if _matches(row, stage["$match"])]
            elif "$graphLookup" in stage:
                spec = stage["$graphLookup"]
                by_key = {}
                source = self.database[spec["from"]].rows
                for row in source:
                    by_key.setdefault(row.get(spec["connectToField"]), []).append(row)
                result = []
                for row in rows:
                    found, seen, depth = [], set(), 0
                    frontier = [row.get(spec["startWith"].removeprefix("$"))]
                    while frontier:
                        following = []
                        for key in frontier:
                            for match in by_key.get(key, []):
                                if match["_id"] in seen:
                                    continue
                                seen.add(match["_id"])
                                item = copy.deepcopy(match)
                                if "depthField" in spec:
                                    item[spec["depthField"]] = depth
                                found.append(item)
                                following.append(match.get(spec["connectFromField"]))
                        frontier, depth = following, depth + 1
                    result.append(dict(copy.deepcopy(row), **{spec["as"]: found}))
                rows = result
            elif "$project" in stage:
                rows = [_project(row, stage["$project"]) for row in rows]
            else:
                raise AssertionError(f"Unsupported synthetic stage: {stage}")
        return iter(copy.deepcopy(rows))

    def count_documents(self, query, **_kwargs):
        return len(self.find(query))

    def create_index(self, *_args, **_kwargs):
        return "synthetic-index"

    def insert_one(self, document, **_kwargs):
        if self.find_one({"_id": document["_id"]}) is not None:
            raise DuplicateKeyError("Synthetic duplicate document")
        self.rows.append(copy.deepcopy(document))
        return SimpleNamespace(inserted_id=document["_id"])

    def insert_many(self, documents, **kwargs):
        for document in documents:
            self.insert_one(document, **kwargs)

    def replace_one(self, query, document, *, upsert=False, **_kwargs):
        document = copy.deepcopy(document)
        if "_id" not in document and "_id" in query:
            document["_id"] = query["_id"]
        for index, current in enumerate(self.rows):
            if _matches(current, query):
                self.rows[index] = copy.deepcopy(document)
                return SimpleNamespace(matched_count=1)
        if upsert:
            self.insert_one(document)
        return SimpleNamespace(matched_count=0)

    def delete_many(self, query, **_kwargs):
        before = len(self.rows)
        self.rows[:] = [row for row in self.rows if not _matches(row, query)]
        return SimpleNamespace(deleted_count=before - len(self.rows))

    def delete_one(self, query, **_kwargs):
        row = next((r for r in self.rows if _matches(r, query)), None)
        if row is not None:
            self.rows.remove(row)
        return SimpleNamespace(deleted_count=int(row is not None))

    def update_many(self, query, update, **_kwargs):
        count = 0
        for row in self.rows:
            if _matches(row, query):
                _apply(row, update)
                count += 1
        return SimpleNamespace(matched_count=count, modified_count=count)

    def update_one(self, query, update, *, upsert=False, **_kwargs):
        for row in self.rows:
            if _matches(row, query):
                _apply(row, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if not upsert:
            return SimpleNamespace(matched_count=0, modified_count=0)
        row = {k: v for k, v in copy.deepcopy(query).items() if not k.startswith("$")}
        row.update(copy.deepcopy(update.get("$setOnInsert", {})))
        _apply(row, update)
        self.insert_one(row)
        return SimpleNamespace(matched_count=0, modified_count=0)

    def find_one_and_update(self, query, update, *, return_document=False, **_kwargs):
        for row in self.rows:
            if _matches(row, query):
                before = copy.deepcopy(row)
                _apply(row, update)
                return copy.deepcopy(row) if return_document else before
        return None

    def bulk_write(self, requests, **_kwargs):
        for request in requests:
            if isinstance(request, ReplaceOne):
                self.replace_one(request._filter, request._doc, upsert=request._upsert)
            elif isinstance(request, DeleteOne):
                self.delete_one(request._filter)
            elif isinstance(request, UpdateOne):
                self.update_one(request._filter, request._doc)
            else:
                raise AssertionError("Unsupported synthetic database operation")


class Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = Collection()
            self.collections[name].database = self
        return self.collections[name]

    def __getattr__(self, name):
        return self[name]


class FakeMongoBackend:
    def __init__(self):
        self.db = Database()
        self._settings_lock = threading.RLock()
        self._settings_cached = False
        self._settings_document = None

    def ping(self):
        pass

    def fence(self, _session):
        pass

    def check_owner(self):
        pass

    def write(self, collection, method, *args, **kwargs):
        return getattr(collection, method)(*args, **kwargs)

    @contextmanager
    def session_transaction(self):
        before = {
            name: copy.deepcopy(value.rows)
            for name, value in self.db.collections.items()
        }
        try:
            yield object()
        except BaseException:
            for name in list(self.db.collections):
                self.db.collections[name].rows = before.get(name, [])
            raise

    def transaction(self, operation):
        before = {
            name: copy.deepcopy(value.rows)
            for name, value in self.db.collections.items()
        }
        try:
            return operation(object())
        except BaseException:
            for name, collection in self.db.collections.items():
                collection.rows = before.get(name, [])
            raise


def register_test_backend(monkeypatch, settings_or_directory, backend=None):
    """Opt a fixture into a fake backend without changing production selection."""
    from tgdrive.storage import mongo

    directory = getattr(
        settings_or_directory, "resolved_data_dir", settings_or_directory
    )
    backend = backend or FakeMongoBackend()
    monkeypatch.setitem(mongo._directories, directory.resolve(), backend)

    def configured(data_dir, _uri, _database="tgdrive"):
        result = mongo.for_directory(data_dir)
        if result is None:
            raise AssertionError(
                "Register an isolated MongoDB backend for this fixture"
            )
        return result

    monkeypatch.setattr(mongo, "configure", configured)
    return backend


_STORE_BACKENDS = {}


def new_store(path=None, backend=None):
    """Create an isolated MongoStore; ``path`` only names it for reopen_store."""
    from tgdrive.storage.mongo_store import MongoStore

    backend = backend or FakeMongoBackend()
    store = MongoStore.create(backend)
    if path is not None:
        _STORE_BACKENDS[str(path)] = backend
    return store


def reopen_store(path):
    """Open a new MongoStore over the database created by ``new_store(path)``."""
    from tgdrive.storage.mongo_store import MongoStore

    return MongoStore(_STORE_BACKENDS[str(path)])


def catalog_counters(payload):
    from tgdrive.storage.mongo_store import catalog_counters as counters

    return counters(payload)


def store_from_payload(payload, *, revoke_shares=False):
    """Load a validated portable catalog into a fresh synthetic database."""
    from tgdrive.storage.mongo_store import MongoStore
    from tgdrive.storage.mongo_store import mongo_nodes as _mongo_nodes
    from tgdrive.storage.store import SCHEMA_VERSION

    MongoStore._validate_payload(payload)
    backend = FakeMongoBackend()
    db = backend.db
    revision = payload["revision"] + (1 if revoke_shares and payload["shares"] else 0)
    db.catalog.insert_one(
        {
            "_id": "catalog",
            "schema_version": SCHEMA_VERSION,
            "drive_id": payload["drive_id"],
            "revision": revision,
            "backed_up_revision": payload["backed_up_revision"],
            "generation": 0,
            "storage_channel_id": payload.get("storage_channel_id"),
            **catalog_counters(payload),
        }
    )
    db.nodes.insert_many(_mongo_nodes(payload))
    if not revoke_shares:
        db.shares.insert_many([dict(row, _id=row["id"]) for row in payload["shares"]])
    db.operations.insert_many(
        [dict(row, _id=row["operation_id"]) for row in payload.get("operations", [])]
    )
    db.deletions.insert_many(
        [dict(row, _id=row["id"]) for row in payload.get("deletions", [])]
    )
    db.preparations.insert_many(
        [
            dict(row, _id=row["request_id"])
            for row in payload.get("folder_preparations", [])
        ]
    )
    return MongoStore(backend)
