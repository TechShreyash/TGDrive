"""Disposable Telegram account names cached in MongoDB."""

from __future__ import annotations

import math
import time
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
        from tgdrive.storage.mongo import DatabaseUnavailable, for_directory

        self._mongo = for_directory(data_dir)
        if self._mongo is None:
            raise DatabaseUnavailable("MongoDB is required for account identities.")

    def defer(self, account: str, deadline: float) -> None:
        self._mongo.db.identities.update_one(
            {"_id": account}, {"$max": {"retry_at": deadline}}, upsert=True
        )

    def retry_at(self, account: str) -> float:
        row = self._mongo.db.identities.find_one({"_id": account}) or {}
        deadline = row.get("retry_at", 0.0)
        return (
            deadline
            if isinstance(deadline, (int, float)) and math.isfinite(deadline)
            else 0.0
        )

    def read(self, account: str) -> dict[str, str | None] | None:
        return self.read_many([account]).get(account)

    def read_many(self, accounts: list[str]) -> dict[str, dict[str, str | None]]:
        return {
            row["_id"]: account_identity(
                SimpleNamespace(
                    first_name=row.get("display_name"), username=row.get("username")
                )
            )
            for row in self._mongo.db.identities.find({"_id": {"$in": accounts}})
            if row.get("updated_at") is not None
        }

    def write(self, account: str, identity: dict[str, str | None]) -> None:
        self._mongo.db.identities.update_one(
            {"_id": account},
            {"$set": {**identity, "updated_at": time.time()}},
            upsert=True,
        )
