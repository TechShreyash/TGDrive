"""Explicit, isolated MongoDB doubles for settings and application unit tests."""

from __future__ import annotations

import copy
import re
import threading
from types import SimpleNamespace

from pymongo import DeleteOne, ReplaceOne, UpdateOne
from pymongo.errors import DuplicateKeyError


def _value(document, key):
    for part in key.split("."):
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
                if operator == "$in" and actual not in value:
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
                if operator == "$regex" and not re.search(value, str(actual or "")):
                    return False
        elif actual != expected:
            return False
    return True


class Cursor(list):
    def sort(self, fields, direction=None):
        if isinstance(fields, str):
            fields = [(fields, direction)]
        for key, direction in reversed(fields):
            super().sort(key=lambda row: _value(row, key), reverse=direction < 0)
        return self

    def limit(self, count):
        return Cursor(self[:count])

    def batch_size(self, _count):
        return self


class Collection:
    def __init__(self):
        self.rows = []

    def find(self, query=None, **_kwargs):
        return Cursor(copy.deepcopy([r for r in self.rows if _matches(r, query or {})]))

    def find_one(self, query=None, *, sort=None, **_kwargs):
        rows = self.find(query)
        if sort:
            rows.sort(sort)
        return next(iter(rows), None)

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
                row.update(copy.deepcopy(update.get("$set", {})))
                count += 1
        return SimpleNamespace(matched_count=count)

    def update_one(self, query, update, *, upsert=False, **_kwargs):
        row = self.find_one(query)
        created = row is None
        if row is None:
            if not upsert:
                return SimpleNamespace(matched_count=0)
            row = copy.deepcopy(query)
        if created:
            row.update(copy.deepcopy(update.get("$setOnInsert", {})))
        row.update(copy.deepcopy(update.get("$set", {})))
        for field, value in update.get("$max", {}).items():
            row[field] = max(row.get(field, value), value)
        return self.replace_one(query, row, upsert=upsert)

    def bulk_write(self, requests, **_kwargs):
        for request in requests:
            if isinstance(request, ReplaceOne):
                self.replace_one(request._filter, request._doc, upsert=request._upsert)
            elif isinstance(request, DeleteOne):
                self.delete_one(request._filter)
            elif isinstance(request, UpdateOne):
                row = self.find_one(request._filter)
                if row is not None:
                    row.update(request._doc.get("$set", {}))
                    self.replace_one(request._filter, row)
            else:
                raise AssertionError("Unsupported synthetic database operation")


class Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, Collection())

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
