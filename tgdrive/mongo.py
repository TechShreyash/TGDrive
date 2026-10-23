"""MongoDB connection ownership, bounded requests and installation fencing.

Only explicit MongoDB configuration enables this backend. No connection string
is included in exceptions or application status.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bson.codec_options import CodecOptions, TypeDecoder, TypeRegistry
from bson.int64 import Int64
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern


class _IntegerDecoder(TypeDecoder):
    bson_type = Int64

    def transform_bson(self, value):
        return int(value)


class DatabaseUnavailable(RuntimeError):
    pass


_backends: dict[tuple[str, str], MongoBackend] = {}
_directories: dict[Path, MongoBackend] = {}
_guard = threading.RLock()


def configure(data_dir: Path, uri: str, database: str = "tgdrive") -> MongoBackend:
    with _guard:
        key = (uri, database)
        if key not in _backends:
            _backends[key] = MongoBackend(uri, database)
        backend = _backends[key]
        _directories[data_dir.resolve()] = backend
        return backend


def for_directory(data_dir: Path) -> MongoBackend | None:
    return _directories.get(data_dir.resolve())


def for_settings(settings: Any) -> MongoBackend | None:
    uri = getattr(settings, "mongodb_uri", None)
    if not uri:
        return None
    return configure(
        settings.resolved_data_dir, uri.get_secret_value(), settings.mongodb_database
    )


class MongoBackend:
    def __init__(self, uri: str, database: str):
        self.client = MongoClient(
            uri,
            appname="TGDrive",
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            timeoutMS=15000,
            maxPoolSize=12,
            minPoolSize=0,
            retryWrites=True,
            retryReads=True,
        )
        self.db = self.client.get_database(
            database,
            write_concern=WriteConcern("majority", wtimeout=10000),
            read_concern=ReadConcern("majority"),
            codec_options=CodecOptions(
                tz_aware=True, type_registry=TypeRegistry([_IntegerDecoder()])
            ),
        )
        self._settings_lock = threading.RLock()
        self._settings_cached = False
        self._settings_document = None
        self.token: str | None = None
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None
        self._healthy_until = 0.0
        self._lost = False

    def ping(self) -> None:
        try:
            self.db.command("ping")
        except PyMongoError:
            raise DatabaseUnavailable(
                "MongoDB is unavailable. Check database access and network settings."
            ) from None

    def acquire(self) -> None:
        if self.token is not None:
            raise DatabaseUnavailable(
                "This process already has an active MongoDB owner."
            )
        token = uuid.uuid4().hex
        try:
            # The server's clock governs expiration. Updating this same document
            # also serializes a takeover against every catalog transaction.
            self.db.runtime.update_one(
                {"_id": "owner"},
                {
                    "$setOnInsert": {
                        "expires_at": datetime(1970, 1, 1, tzinfo=UTC),
                        "epoch": 0,
                    }
                },
                upsert=True,
            )
            result = self.db.runtime.find_one_and_update(
                {
                    "_id": "owner",
                    "$expr": {
                        "$lte": [
                            {
                                "$ifNull": [
                                    "$expires_at",
                                    datetime(1970, 1, 1, tzinfo=UTC),
                                ]
                            },
                            "$$NOW",
                        ]
                    },
                },
                [
                    {
                        "$set": {
                            "token": token,
                            "expires_at": {
                                "$dateAdd": {
                                    "startDate": "$$NOW",
                                    "unit": "second",
                                    "amount": 60,
                                }
                            },
                            "epoch": {"$add": [{"$ifNull": ["$epoch", 0]}, 1]},
                        }
                    }
                ],
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            raise DatabaseUnavailable(
                "Another TGDrive installation is using this MongoDB database. "
                "Stop the old installation before starting this one; do not run init."
            ) from None
        except PyMongoError:
            raise DatabaseUnavailable("Could not acquire MongoDB ownership.") from None
        if not result or result.get("token") != token:
            raise DatabaseUnavailable(
                "Another TGDrive installation owns this MongoDB database. "
                "Stop the old installation before starting this one; do not run init."
            )
        self.token = token
        self._lost = False
        self._healthy_until = time.monotonic() + 45
        self._stop.clear()
        self._heartbeat = threading.Thread(
            target=self._renew_loop, name="mongodb-owner", daemon=True
        )
        self._heartbeat.start()

    def _renew_loop(self) -> None:
        while not self._stop.wait(15):
            try:
                result = self.db.runtime.update_one(
                    {
                        "_id": "owner",
                        "token": self.token,
                        "$expr": {"$gt": ["$expires_at", "$$NOW"]},
                    },
                    [
                        {
                            "$set": {
                                "expires_at": {
                                    "$dateAdd": {
                                        "startDate": "$$NOW",
                                        "unit": "second",
                                        "amount": 60,
                                    }
                                }
                            }
                        }
                    ],
                )
                if result.matched_count != 1:
                    self._lost = True
                    return
                self._healthy_until = time.monotonic() + 45
            except PyMongoError:
                # Keep read-only cache available briefly; no remote side effect
                # is admitted after the conservative local ownership deadline.
                continue

    def check_owner(self) -> None:
        if not self.token or self._lost or time.monotonic() >= self._healthy_until:
            raise DatabaseUnavailable(
                "MongoDB ownership is unavailable. Transfers are paused; restart "
                "after restoring database connectivity."
            )

    def fence(self, session: Any) -> None:
        self.check_owner()
        result = self.db.runtime.update_one(
            {
                "_id": "owner",
                "token": self.token,
                "$expr": {"$gt": ["$expires_at", "$$NOW"]},
            },
            {"$inc": {"commits": 1}},
            session=session,
        )
        if result.matched_count != 1:
            self._lost = True
            raise DatabaseUnavailable("MongoDB ownership changed; writes are stopped.")

    def release(self) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=16)
            self._heartbeat = None
        token, self.token = self.token, None
        if token:
            with suppress(PyMongoError):
                self.db.runtime.delete_one({"_id": "owner", "token": token})

    def write(self, collection, method, *args, **kwargs):
        def apply(session):
            self.fence(session)
            return getattr(collection, method)(*args, session=session, **kwargs)

        return self.transaction(apply)

    def transaction(self, callback):
        try:
            with self.client.start_session() as session:
                return session.with_transaction(
                    callback,
                    read_concern=ReadConcern("snapshot"),
                    write_concern=WriteConcern("majority", wtimeout=10000),
                    max_commit_time_ms=10000,
                )
        except PyMongoError:
            raise DatabaseUnavailable(
                "MongoDB could not confirm this operation. Refresh before retrying."
            ) from None


def save_bootstrap(settings: Any, backend: MongoBackend) -> None:
    """Store deployment preferences and owner auth, excluding DB credentials/paths."""
    from argon2 import PasswordHasher
    from pydantic import SecretStr

    values = {}
    for key, value in settings.model_dump(by_alias=True).items():
        if key in {
            "MONGODB_URI",
            "MONGODB_DATABASE",
            "DATA_DIR",
            "CACHE_DIR",
            "DEMO_MODE",
            "DEMO_ADMIN_PASSWORD",
            "DEMO_ADMIN_PASSWORD_HASH",
            "ADMIN_PASSWORD",
            "managed_revision",
            "managed_client_ids",
            "managed_client_labels",
        }:
            continue
        values[key] = (
            value.get_secret_value() if isinstance(value, SecretStr) else value
        )
    if not values.get("ADMIN_PASSWORD_HASH"):
        values["ADMIN_PASSWORD_HASH"] = PasswordHasher().hash(
            settings.admin_password.get_secret_value()
        )
    backend.db.settings.update_one(
        {"_id": "bootstrap"}, {"$setOnInsert": {"document": values}}, upsert=True
    )
