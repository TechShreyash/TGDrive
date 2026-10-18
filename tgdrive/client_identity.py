"""Disposable local Telegram names; never credentials or catalog authority."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def account_identity(account: Any) -> dict[str, str | None]:
    parts = (getattr(account, "first_name", None), getattr(account, "last_name", None))
    display_name = " ".join(
        "".join(c for c in " ".join(part.split()) if c.isprintable())
        for part in parts
        if isinstance(part, str) and part.strip()
    )[:160].strip()
    username = getattr(account, "username", None)
    if not (
        isinstance(username, str)
        and 1 <= len(username) <= 64
        and all(c.isascii() and (c.isalnum() or c == "_") for c in username)
    ):
        username = None
    return {"display_name": display_name or None, "username": username}


class IdentityCache:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "connection-identities.sqlite3"
        from tgdrive.mongo import for_directory

        self._mongo = for_directory(data_dir)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise OSError("Identity cache cannot be a symbolic link")
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS identities("
                "account TEXT PRIMARY KEY, display_name TEXT, username TEXT, "
                "updated_at REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS retries("
                "account TEXT PRIMARY KEY, deadline REAL NOT NULL)"
            )
        except BaseException:
            db.close()
            raise
        return db

    def defer(self, account: str, deadline: float) -> None:
        if self._mongo is not None:
            self._mongo.db.identities.update_one(
                {"_id": account}, {"$max": {"retry_at": deadline}}, upsert=True
            )
            return
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO retries VALUES(?,?) ON CONFLICT(account) DO UPDATE "
                "SET deadline=max(deadline,excluded.deadline)",
                (account, deadline),
            )
            db.commit()

    def retry_at(self, account: str) -> float:
        if self._mongo is not None:
            row = self._mongo.db.identities.find_one({"_id": account}) or {}
            deadline = row.get("retry_at", 0.0)
            return (
                deadline
                if isinstance(deadline, (int, float)) and math.isfinite(deadline)
                else 0.0
            )
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT deadline FROM retries WHERE account=?", (account,)
            ).fetchone()
        deadline = float(row[0]) if row else 0.0
        return deadline if math.isfinite(deadline) and deadline > 0 else 0.0

    def read(self, account: str) -> dict[str, str | None] | None:
        if self._mongo is not None:
            return self.read_many([account]).get(account)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT display_name,username FROM identities WHERE account=?",
                (account,),
            ).fetchone()
        if row is None:
            return None
        return account_identity(SimpleNamespace(first_name=row[0], username=row[1]))

    def read_many(self, accounts: list[str]) -> dict[str, dict[str, str | None]]:
        if self._mongo is not None:
            return {
                row["_id"]: account_identity(
                    SimpleNamespace(
                        first_name=row.get("display_name"), username=row.get("username")
                    )
                )
                for row in self._mongo.db.identities.find({"_id": {"$in": accounts}})
                if row.get("updated_at") is not None
            }
        selected = list(set(accounts))
        if not selected:
            return {}
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT account,display_name,username FROM identities "
                "WHERE account IN (SELECT value FROM json_each(?))",
                (json.dumps(selected),),
            ).fetchall()
        return {
            row[0]: account_identity(
                SimpleNamespace(first_name=row[1], username=row[2])
            )
            for row in rows
        }

    def write(self, account: str, identity: dict[str, str | None]) -> None:
        if self._mongo is not None:
            self._mongo.db.identities.update_one(
                {"_id": account},
                {"$set": {**identity, "updated_at": time.time()}},
                upsert=True,
            )
            return
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO identities VALUES(?,?,?,?) ON CONFLICT(account) DO UPDATE "
                "SET display_name=excluded.display_name,username=excluded.username,"
                "updated_at=excluded.updated_at",
                (account, identity["display_name"], identity["username"], time.time()),
            )
            db.commit()
