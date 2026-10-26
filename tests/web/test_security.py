from __future__ import annotations

import pytest

from tgdrive.core.security import (
    InvalidSession,
    LoginLimiter,
    SessionManager,
    safe_name,
    verify_password,
)


def test_session_round_trip_and_expiry() -> None:
    manager = SessionManager(b"k" * 32, ttl_seconds=300)
    token = manager.create(now=1_000)

    payload = manager.verify(token, now=1_299)
    assert payload["iat"] == 1_000

    with pytest.raises(InvalidSession, match="expired"):
        manager.verify(token, now=1_300)
    with pytest.raises(InvalidSession, match="expired"):
        manager.verify(token, now=1_301)


def test_session_rejects_tampering_and_malformed_data() -> None:
    manager = SessionManager(b"k" * 32, ttl_seconds=300)
    token = manager.create(now=1_000)
    payload, signature = token.split(".")

    with pytest.raises(InvalidSession):
        manager.verify(f"{payload}x.{signature}", now=1_001)
    with pytest.raises(InvalidSession):
        manager.verify("not-a-session", now=1_001)


def test_login_limiter_is_a_sliding_window() -> None:
    limiter = LoginLimiter(attempts=2, window_seconds=10)

    limiter.failure("client", now=1.0)
    assert limiter.check("client", now=2.0).allowed
    limiter.failure("client", now=2.0)
    decision = limiter.check("client", now=3.0)
    assert not decision.allowed
    assert decision.retry_after == 8
    assert limiter.check("client", now=12.1).allowed


def test_success_clears_login_failures() -> None:
    limiter = LoginLimiter(attempts=1, window_seconds=10)
    limiter.failure("client", now=1.0)
    limiter.success("client")
    assert limiter.check("client", now=2.0).allowed


def test_login_limiter_bounds_unique_client_state() -> None:
    limiter = LoginLimiter(attempts=1, window_seconds=10, max_clients=2)
    limiter.failure("first", now=1.0)
    limiter.failure("second", now=1.0)
    limiter.failure("third", now=1.0)

    assert limiter.check("first", now=2.0).allowed


def test_safe_name_removes_paths_controls_and_dangerous_empty_names() -> None:
    assert safe_name("../../folder\\report.txt") == "report.txt"
    assert safe_name("\r\n") == "file"
    assert safe_name("..") == "file"
    assert safe_name(" report + 100%.txt ") == "report + 100%.txt"


def test_password_comparison() -> None:
    assert verify_password("secret", "secret")
    assert not verify_password("secret", "other")


def test_mongodb_sessions_support_concurrent_range_checks_and_logins():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, RLock
    from types import SimpleNamespace

    class Sessions:
        """Thread-safe MongoDB collection double; no external database is used."""

        def __init__(self):
            self.rows = {}
            self.lock = RLock()

        def find(self, query):
            with self.lock:
                return [
                    dict(row)
                    for row in self.rows.values()
                    if row["expires"] > query["expires"]["$gt"]
                ]

        def insert_one(self, row):
            with self.lock:
                self.rows[row["_id"]] = dict(row)

        def delete_one(self, query):
            with self.lock:
                self.rows.pop(query["_id"], None)

        def delete_many(self, query):
            with self.lock:
                for identity in [
                    key
                    for key, row in self.rows.items()
                    if row["expires"] <= query["expires"]["$lte"]
                ]:
                    del self.rows[identity]

    collection = Sessions()
    backend = SimpleNamespace(
        db=SimpleNamespace(sessions=collection),
        write=lambda target, method, *args: getattr(target, method)(*args),
    )

    manager = SessionManager(b"k" * 32, ttl_seconds=300, mongo_backend=backend)
    existing = manager.create(now=1_000)
    start = Barrier(16)

    def check_and_login(_worker):
        start.wait(timeout=10)
        for _ in range(40):
            assert manager.verify(existing, now=1_001)["iat"] == 1_000
            token = manager.create(now=1_001)
            assert manager.verify(token, now=1_002)["iat"] == 1_001
            manager.revoke(token)
            with pytest.raises(InvalidSession, match="revoked"):
                manager.verify(token, now=1_002)

    try:
        with ThreadPoolExecutor(max_workers=16) as workers:
            list(workers.map(check_and_login, range(16)))
        manager.revoke(existing)
        with pytest.raises(InvalidSession, match="revoked"):
            manager.verify(existing, now=1_002)
    finally:
        manager.close()
