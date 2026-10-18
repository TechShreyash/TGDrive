"""Authentication, request throttling, and untrusted-name helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import unicodedata
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

SESSION_COOKIE: Final = "tgdrive_session"
_TOKEN_VERSION: Final = 1


class InvalidSession(ValueError):
    """Raised when a session token is invalid or expired."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class SessionManager:
    """Create and verify compact HMAC-authenticated admin sessions."""

    def __init__(
        self, key: bytes, ttl_seconds: int, database_path: Path | None = None
    ) -> None:
        if len(key) < 32:
            raise ValueError("session key must contain at least 32 bytes")
        self._key = key
        self._ttl_seconds = ttl_seconds
        self._db = None
        from tgdrive.mongo import for_directory

        backend = for_directory(database_path.parent) if database_path else None
        self._mongo_backend = backend
        self._mongo = backend.db.sessions if backend else None
        self._mongo_sessions = (
            {
                row["_id"]: row["expires"]
                for row in self._mongo.find({"expires": {"$gt": int(time.time())}})
            }
            if self._mongo is not None
            else {}
        )
        self._finalizer = None
        if database_path is not None and self._mongo is None:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(database_path, check_same_thread=False)
            database_path.chmod(0o600)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS sessions "
                "(hash TEXT PRIMARY KEY, expires INTEGER NOT NULL)"
            )
            self._db.commit()
            self._finalizer = weakref.finalize(self, self._db.close)

    def close(self) -> None:
        if self._finalizer is not None:
            self._finalizer()

    def revoke(self, token: str) -> None:
        if self._mongo is not None:
            digest = hashlib.sha256(token.encode()).hexdigest()
            self._mongo_backend.write(self._mongo, "delete_one", {"_id": digest})
            self._mongo_sessions.pop(digest, None)
            return
        if self._db is not None:
            with self._db:
                self._db.execute(
                    "DELETE FROM sessions WHERE hash=?",
                    (hashlib.sha256(token.encode()).hexdigest(),),
                )

    def create(self, *, now: int | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        payload = {
            "v": _TOKEN_VERSION,
            "iat": issued_at,
            "exp": issued_at + self._ttl_seconds,
            "nonce": secrets.token_urlsafe(18),
        }
        encoded = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        signature = _b64encode(
            hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        token = f"{encoded}.{signature}"
        if self._db is not None:
            with self._db:
                self._db.execute("DELETE FROM sessions WHERE expires<=?", (issued_at,))
                self._db.execute(
                    "INSERT INTO sessions VALUES (?, ?)",
                    (hashlib.sha256(token.encode()).hexdigest(), payload["exp"]),
                )
        if self._mongo is not None:
            self._mongo_backend.write(
                self._mongo, "delete_many", {"expires": {"$lte": issued_at}}
            )
            self._mongo_backend.write(
                self._mongo,
                "insert_one",
                {
                    "_id": hashlib.sha256(token.encode()).hexdigest(),
                    "expires": payload["exp"],
                },
            )
            self._mongo_sessions[hashlib.sha256(token.encode()).hexdigest()] = payload[
                "exp"
            ]
        return token

    def verify(self, token: str, *, now: int | None = None) -> dict[str, int | str]:
        try:
            encoded, supplied_signature = token.split(".", 1)
            expected_signature = _b64encode(
                hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise InvalidSession("invalid session signature")
            payload = json.loads(_b64decode(encoded))
            current_time = int(time.time() if now is None else now)
            if payload.get("v") != _TOKEN_VERSION:
                raise InvalidSession("unsupported session version")
            if not isinstance(payload.get("exp"), int) or payload["exp"] < current_time:
                raise InvalidSession("session expired")
            if (
                not isinstance(payload.get("iat"), int)
                or payload["iat"] > current_time + 60
            ):
                raise InvalidSession("invalid session timestamp")
            if not isinstance(payload.get("nonce"), str):
                raise InvalidSession("invalid session nonce")
            if (
                self._db is not None
                and self._db.execute(
                    "SELECT 1 FROM sessions WHERE hash=? AND expires>?",
                    (hashlib.sha256(token.encode()).hexdigest(), current_time),
                ).fetchone()
                is None
            ):
                raise InvalidSession("Session was revoked")
            if (
                self._mongo is not None
                and self._mongo_sessions.get(
                    hashlib.sha256(token.encode()).hexdigest(), 0
                )
                <= current_time
            ):
                raise InvalidSession("Session was revoked")
            return payload
        except InvalidSession:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise InvalidSession("malformed session") from exc


@dataclass(frozen=True, slots=True)
class LoginDecision:
    allowed: bool
    retry_after: int = 0


class LoginLimiter:
    """Small in-memory sliding-window limiter for password attempts."""

    def __init__(
        self,
        attempts: int = 5,
        window_seconds: int = 300,
        max_clients: int = 10_000,
    ) -> None:
        if attempts <= 0 or window_seconds <= 0 or max_clients <= 0:
            raise ValueError("login limiter settings must be positive")
        self._attempts = attempts
        self._window = window_seconds
        self._max_clients = max_clients
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str, *, now: float | None = None) -> LoginDecision:
        current = time.monotonic() if now is None else now
        failures = self._failures.get(key)
        if failures is None:
            if len(self._failures) >= self._max_clients:
                self._failures.popitem(last=False)
            failures = deque()
            self._failures[key] = failures
        else:
            self._failures.move_to_end(key)
        while failures and failures[0] <= current - self._window:
            failures.popleft()
        if len(failures) < self._attempts:
            return LoginDecision(True)
        retry_after = max(1, int(self._window - (current - failures[0])))
        return LoginDecision(False, retry_after)

    def failure(self, key: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self.check(key, now=current)
        self._failures[key].append(current)

    def success(self, key: str) -> None:
        self._failures.pop(key, None)


def verify_password(candidate: str, expected: str) -> bool:
    """Compare passwords without content-dependent early exits."""

    if expected.startswith("$argon2id$"):
        try:
            return PasswordHasher().verify(expected, candidate)
        except VerificationError:
            return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def safe_name(value: str | None, *, fallback: str = "file") -> str:
    """Normalize an untrusted display/file name without interpreting it as a path."""

    raw = (value or "").replace("\\", "/").rsplit("/", 1)[-1]
    normalized = unicodedata.normalize("NFC", raw).strip()
    normalized = "".join(
        char for char in normalized if unicodedata.category(char) != "Cc"
    )
    normalized = normalized.strip(". ")
    if normalized in {"", ".", ".."}:
        normalized = fallback
    # Telegram and common filesystems become unreliable with very long names.
    return normalized[:240]
