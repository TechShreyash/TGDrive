"""Authentication, request throttling, and untrusted-name helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from pymongo.errors import PyMongoError

SESSION_COOKIE: Final = "tgdrive_session"
_TOKEN_VERSION: Final = 1


def credential_session_key(key: bytes, credential: str) -> bytes:
    """Invalidate browser sessions when the configured owner credential changes.

    Derive from the configured value, rather than a freshly salted login hash,
    so ordinary restarts preserve sessions. Share-link keys stay independent.
    """

    return hmac.new(
        key, b"tgdrive/owner-session/v1\0" + credential.encode(), hashlib.sha256
    ).digest()


def demo_admin_session_key(key: bytes, credential: str) -> bytes:
    """Keep demo administrator sessions independent of ordinary owner sessions."""

    return hmac.new(
        key, b"tgdrive/demo-admin-session/v1\0" + credential.encode(), hashlib.sha256
    ).digest()


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
        self,
        key: bytes,
        ttl_seconds: int,
        *,
        mongo_backend=None,
        demo_admin_key: bytes | None = None,
    ) -> None:
        if len(key) < 32:
            raise ValueError("session key must contain at least 32 bytes")
        if demo_admin_key is not None and len(demo_admin_key) < 32:
            raise ValueError(
                "demo administrator session key must contain at least 32 bytes"
            )
        self._key = key
        self._demo_admin_key = demo_admin_key
        self._ttl_seconds = ttl_seconds
        from tgdrive.storage.mongo import DatabaseUnavailable

        self._mongo_backend = mongo_backend
        self._mongo = mongo_backend.db.sessions if mongo_backend is not None else None
        try:
            self._mongo_sessions = (
                {
                    row["_id"]: row["expires"]
                    for row in self._mongo.find({"expires": {"$gt": int(time.time())}})
                }
                if self._mongo is not None
                else {}
            )
        except PyMongoError:
            raise DatabaseUnavailable(
                "MongoDB is unavailable. Restore database access, then restart "
                "TGDrive. Owner sign-in requires the configured database."
            ) from None

    def close(self) -> None:
        """The shared MongoDB backend owns its connection lifecycle."""

    def revoke(self, token: str) -> None:
        if self._mongo is not None:
            digest = hashlib.sha256(token.encode()).hexdigest()
            self._mongo_backend.write(self._mongo, "delete_one", {"_id": digest})
            self._mongo_sessions.pop(digest, None)
            return

    def create(self, *, now: int | None = None, demo_admin: bool = False) -> str:
        if demo_admin and self._demo_admin_key is None:
            raise ValueError("Demo administrator sign-in is not configured")
        issued_at = int(time.time() if now is None else now)
        payload = {
            "v": _TOKEN_VERSION,
            "iat": issued_at,
            "exp": issued_at + self._ttl_seconds,
            "nonce": secrets.token_urlsafe(18),
        }
        if demo_admin:
            payload["demo_admin"] = True
        encoded = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        signature = _b64encode(
            hmac.new(
                self._demo_admin_key if demo_admin else self._key,
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        token = f"{encoded}.{signature}"
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
            ordinary_signature = hmac.compare_digest(
                supplied_signature, expected_signature
            )
            administrator_signature = False
            if self._demo_admin_key is not None:
                administrator_signature = hmac.compare_digest(
                    supplied_signature,
                    _b64encode(
                        hmac.new(
                            self._demo_admin_key,
                            encoded.encode("ascii"),
                            hashlib.sha256,
                        ).digest()
                    ),
                )
            if not (ordinary_signature or administrator_signature):
                raise InvalidSession("invalid session signature")
            payload = json.loads(_b64decode(encoded))
            if not isinstance(payload, dict):
                raise InvalidSession("malformed session")
            elevated = payload.get("demo_admin") is True
            if not (administrator_signature if elevated else ordinary_signature):
                raise InvalidSession("invalid session capability signature")
            current_time = int(time.time() if now is None else now)
            if payload.get("v") != _TOKEN_VERSION:
                raise InvalidSession("unsupported session version")
            if (
                not isinstance(payload.get("exp"), int)
                or payload["exp"] <= current_time
            ):
                raise InvalidSession("session expired")
            if (
                not isinstance(payload.get("iat"), int)
                or payload["iat"] > current_time + 60
            ):
                raise InvalidSession("invalid session timestamp")
            if not isinstance(payload.get("nonce"), str):
                raise InvalidSession("invalid session nonce")
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
