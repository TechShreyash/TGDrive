"""Small explicit database double for the offline HTTP/browser fixture.

This is not a MongoDB emulator or production storage backend. It preserves the
session, identity and settings interfaces used by that fixture without a client,
connection string lookup, filesystem database or external database request.
"""

from __future__ import annotations

import copy
import threading
from collections import defaultdict
from types import SimpleNamespace

from pymongo.errors import DuplicateKeyError


def _value(row, key):
    current = row
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _matches(row, query):
    for key, expected in query.items():
        value = _value(row, key)
        if not isinstance(expected, dict):
            if value != expected:
                return False
            continue
        for operator, bound in expected.items():
            if operator == "$in":
                matches = value in bound
            elif operator == "$ne":
                matches = value != bound
            elif value is None:
                matches = False
            elif operator == "$gt":
                matches = value > bound
            elif operator == "$gte":
                matches = value >= bound
            elif operator == "$lt":
                matches = value < bound
            elif operator == "$lte":
                matches = value <= bound
            else:
                raise AssertionError(f"Unsupported fixture query: {operator}")
            if not matches:
                return False
    return True


class FixtureCursor(list):
    def sort(self, key, direction=1):
        super().sort(key=lambda row: _value(row, key), reverse=direction < 0)
        return self

    def limit(self, count):
        return FixtureCursor(self[:count])


class FixtureCollection:
    def __init__(self):
        self.rows = {}
        self.lock = threading.RLock()

    def find(self, query=None, **_kwargs):
        with self.lock:
            return FixtureCursor(
                copy.deepcopy(row)
                for row in self.rows.values()
                if _matches(row, query or {})
            )

    def find_one(self, query=None, *, sort=None, **kwargs):
        rows = self.find(query, **kwargs)
        for key, direction in reversed(sort or []):
            rows.sort(key, direction)
        return rows[0] if rows else None

    def insert_one(self, document, **_kwargs):
        with self.lock:
            identity = document["_id"]
            if identity in self.rows:
                raise DuplicateKeyError("Duplicate fixture identity")
            self.rows[identity] = copy.deepcopy(document)
            return SimpleNamespace(inserted_id=identity)

    def replace_one(self, query, document, *, upsert=False, **_kwargs):
        with self.lock:
            current = self.find_one(query)
            if current is None and not upsert:
                return SimpleNamespace(matched_count=0)
            document = copy.deepcopy(document)
            document.setdefault("_id", current["_id"] if current else query["_id"])
            if current is None and document["_id"] in self.rows:
                raise DuplicateKeyError("Duplicate fixture identity")
            self.rows[document["_id"]] = copy.deepcopy(document)
            return SimpleNamespace(matched_count=int(current is not None))

    def update_one(self, query, changes, *, upsert=False, **_kwargs):
        with self.lock:
            current = self.find_one(query)
            if current is None and not upsert:
                return SimpleNamespace(matched_count=0)
            document = current or {"_id": query["_id"]}
            for operator, values in changes.items():
                if operator == "$set" or (
                    operator == "$setOnInsert" and current is None
                ):
                    document.update(copy.deepcopy(values))
                elif operator == "$max":
                    for key, value in values.items():
                        document[key] = max(document.get(key, value), value)
                elif operator != "$setOnInsert":
                    raise AssertionError(f"Unsupported fixture update: {operator}")
            self.rows[document["_id"]] = document
            return SimpleNamespace(matched_count=int(current is not None))

    def delete_many(self, query, **_kwargs):
        with self.lock:
            identities = [row["_id"] for row in self.find(query)]
            for identity in identities:
                del self.rows[identity]
            return SimpleNamespace(deleted_count=len(identities))

    def delete_one(self, query, **_kwargs):
        with self.lock:
            row = self.find_one(query)
            if row is not None:
                del self.rows[row["_id"]]
            return SimpleNamespace(deleted_count=int(row is not None))

    def create_index(self, *_args, **_kwargs):
        return "fixture-index"

    def bulk_write(self, operations, **_kwargs):
        for operation in operations:
            self.replace_one(
                operation._filter, operation._doc, upsert=operation._upsert
            )


class FixtureMongoBackend:
    def __init__(self):
        collections = defaultdict(FixtureCollection)

        class Database:
            def __getattr__(self, name):
                return collections[name]

        self.db = Database()
        self._settings_lock = threading.RLock()
        self._settings_cached = False
        self._settings_document = None

    def write(self, collection, operation, *args, **kwargs):
        return getattr(collection, operation)(*args, **kwargs)

    def assert_writable(self, **_kwargs):
        return None
