"""Persistent telemetry must remain measured, bounded and separate from file state."""

from __future__ import annotations

import math
import time

import pytest
from fastapi.testclient import TestClient

from tests.support.app_fixtures import FakeRuntime, login, make_settings
from tests.support.mongo_fixtures import register_test_backend
from tgdrive.observability.telemetry_history import (
    RETENTION_SECONDS,
    HistoryStorage,
    TelemetryHistory,
    validate_range,
)
from tgdrive.storage.store import DriveStore
from tgdrive.web.app import create_app

pytestmark = pytest.mark.usefixtures("mongo_backend")


@pytest.fixture
def mongo_backend(tmp_path, monkeypatch):
    backend = register_test_backend(monkeypatch, tmp_path)
    register_test_backend(monkeypatch, tmp_path / "data", backend)
    return backend


def sample(
    at,
    *,
    cpu=20,
    memory=1024,
    incoming=100,
    outgoing=50,
    interface="eth0",
    scope="cgroup",
    limit=8192,
):
    return dict(
        at=at,
        cpu_percent=cpu,
        cpu_scope=scope,
        memory_bytes=memory,
        memory_limit_bytes=limit,
        received_bytes_per_second=incoming,
        sent_bytes_per_second=outgoing,
        network_interface=interface,
    )


def counters(value=0):
    return dict(
        network=dict(received_bytes=value * 10, sent_bytes=value * 2),
        upload_bytes=value * 3,
        download_bytes=value * 4,
        requests=value,
        errors=value // 2,
    )


def test_weighted_minute_averages_use_actual_seconds_and_preserve_null_gaps(tmp_path):
    history = TelemetryHistory(tmp_path)
    history.observe(sample(960), counters())
    history.observe(sample(963, cpu=20, incoming=100), counters(1))
    history.observe(sample(972, cpu=40, incoming=300), counters(2))
    result = history.query(900, 1200, now=1200)
    measured = result["samples"][1]
    assert measured["at"] == 960
    assert measured["cpu_percent"] == 35
    assert measured["received_bytes_per_second"] == 250
    assert measured["coverage_seconds"]["cpu_percent"] == 12
    assert result["samples"][0]["cpu_percent"] is None
    assert result["samples"][2]["received_bytes_per_second"] is None
    assert result["totals"]["http_download_bytes"] == 8
    assert result["totals"]["telegram_download_bytes"] is None


def test_cross_minute_interval_is_split_without_losing_weight(tmp_path):
    history = TelemetryHistory(tmp_path)
    history.observe(sample(1019), counters())
    history.observe(sample(1022), counters())
    result = history.query(960, 1080, now=1100)
    assert [s["coverage_seconds"]["memory_bytes"] for s in result["samples"]] == [1, 2]
    assert all(s["memory_bytes"] == 1024 for s in result["samples"])


def test_downsampling_weights_covered_seconds_instead_of_averaging_minutes(tmp_path):
    history = TelemetryHistory(tmp_path)
    start = 60000
    for at, cpu, incoming in (
        (start, 0, 0),
        (start + 3, 20, 100),
        (start + 60, 0, 0),
        (start + 69, 40, 400),
    ):
        history.observe(sample(at, cpu=cpu, incoming=incoming), counters())
    result = history.query(start, start + 86400, now=start + 86400)
    assert result["bucket_seconds"] == 240
    assert len(result["samples"]) == 360
    assert result["samples"][0]["cpu_percent"] == 35
    assert result["samples"][0]["received_bytes_per_second"] == 325
    assert result["samples"][0]["coverage_seconds"]["cpu_percent"] == 12
    assert result["samples"][1]["cpu_percent"] is None


def test_unavailable_network_is_unknown_in_lifetime_totals(tmp_path):
    history = TelemetryHistory(tmp_path)
    history.observe(
        sample(960, incoming=None, outgoing=None, interface=None),
        counters()
        | {"network": {"available": False, "received_bytes": 0, "sent_bytes": 0}},
    )
    result = history.query(900, 1020, now=1100)
    assert result["totals"]["network_received_bytes"] is None
    assert result["totals"]["http_requests"] == 0


def test_source_changes_do_not_mix_cpu_or_interface_measurements(tmp_path):
    history = TelemetryHistory(tmp_path)
    for at, interface, scope in (
        (960, "eth0", "cgroup"),
        (963, "eth0", "cgroup"),
        (966, "eth1", "host"),
        (969, "eth1", "host"),
    ):
        history.observe(sample(at, interface=interface, scope=scope), counters())
    point = history.query(960, 1020, now=1100)["samples"][0]
    assert point["cpu_percent"] is point["received_bytes_per_second"] is None
    assert point["cpu_scope"] is point["network_interface"] is None
    assert point["memory_bytes"] == 1024


def test_suspend_and_clock_reversal_do_not_backfill_or_double_count_coverage(tmp_path):
    history = TelemetryHistory(tmp_path)
    for at in (960, 963, 900, 903, 962, 965, 968, 1100):
        history.observe(sample(at), counters())
    result = history.query(900, 1140, now=1200)
    assert result["samples"][0]["memory_bytes"] is None
    assert result["samples"][1]["coverage_seconds"]["memory_bytes"] == 6
    assert result["samples"][2]["memory_bytes"] is None
    assert result["samples"][3]["memory_bytes"] is None


def test_restart_retains_minutes_and_lifetime_counters_without_replay(tmp_path):
    history = TelemetryHistory(tmp_path)
    history.observe(sample(960), counters(10))
    history.observe(sample(963), counters(20))
    history.flush(force=True, now=964)
    history.flush(force=True, now=964)  # Unknown write outcome/retry is idempotent.
    restarted = TelemetryHistory(tmp_path)
    restarted.observe(sample(966), counters(2))
    restarted.observe(sample(969), counters(3))
    restarted.flush(force=True, now=970)
    result = TelemetryHistory(tmp_path).query(960, 1020, now=1100)
    assert result["totals"]["network_received_bytes"] == 230
    assert result["totals"]["http_requests"] == 23
    assert result["samples"][0]["coverage_seconds"]["memory_bytes"] == 6
    assert result["tracked_since"] == 960
    assert result["last_updated"] == 969


def test_telegram_account_replacement_and_counter_resets_are_not_negative(tmp_path):
    history = TelemetryHistory(tmp_path)

    def observe(at, value, identity):
        history.observe(
            sample(at),
            counters(),
            {
                "accounts": [
                    {
                        "name": "bot",
                        "requests": {
                            "upload_bytes": value,
                            "download_bytes": value * 2,
                        },
                    }
                ]
            },
            sources={"bot": identity},
        )

    observe(960, 100, "first")
    observe(963, 150, "first")
    observe(966, 200, "replacement")
    observe(969, 10, "replacement")
    result = history.query(960, 1020, now=1100)
    assert result["totals"]["telegram_upload_bytes"] == 360
    assert result["totals"]["telegram_download_bytes"] == 720
    assert "bot" not in repr(history.storage.read(960, 1020))


def test_connection_churn_does_not_evict_active_counter_baselines(tmp_path):
    history = TelemetryHistory(tmp_path)
    for index in range(600):
        history.observe(
            sample(960 + index * 3),
            counters(),
            {
                "accounts": [
                    {"name": "stable", "requests": {"upload_bytes": 100 + index}},
                    {"name": "replacement", "requests": {"upload_bytes": 1}},
                ]
            },
            sources={"stable": "stable", "replacement": index},
        )
    assert history._totals["telegram_upload_bytes"] == 699 + 600
    assert len(history._baselines) <= 512


def test_retention_prunes_minutes_without_erasing_lifetime_stats(tmp_path):
    history = TelemetryHistory(tmp_path)
    history.observe(sample(960), counters(100))
    history.observe(sample(963), counters(200))
    history.flush(force=True, now=964)
    now = 964 + RETENTION_SECONDS + 120
    history.observe(sample(now), counters(300))
    history.flush(force=True, now=now)
    assert history.storage.read(0, now - RETENTION_SECONDS) == []
    result = history.query(now - 3600, now, now=now)
    assert result["totals"]["network_received_bytes"] == 3000
    assert result["tracked_since"] == 960


def test_month_query_is_bounded_and_boundary_minutes_are_explicit(tmp_path):
    history = TelemetryHistory(tmp_path)
    now = 4_000_000
    result = history.query(now - 30 * 86400 + 17, now - 7, now=now)
    assert len(result["samples"]) <= 360
    assert result["bucket_seconds"] >= 60
    assert result["start"] % 60 == 0
    assert result["end"] <= now
    assert all(point["cpu_percent"] is None for point in result["samples"])
    assert result["tracked_since"] is None


@pytest.mark.parametrize(
    "start,end",
    [
        (math.nan, 100),
        (0, math.inf),
        (1000, 900),
        (1001, 1010),
        (999, 1061),
        (-1, 1000),
        (1000 - RETENTION_SECONDS - 61, 1000),
    ],
)
def test_invalid_or_future_ranges_are_rejected(start, end):
    with pytest.raises(ValueError):
        validate_range(start, end, now=1000)


def test_flush_backoff_and_pending_memory_are_bounded_during_failure(
    tmp_path, monkeypatch
):
    history = TelemetryHistory(tmp_path)
    history.observe(sample(960), counters())
    writes = []

    def fail(*args):
        writes.append(args)
        raise OSError("temporarily unavailable")

    monkeypatch.setattr(history.storage, "write", fail)
    monkeypatch.setattr(
        "tgdrive.observability.telemetry_history.time.monotonic", lambda: 1000
    )
    with pytest.raises(OSError):
        history.flush(now=960)
    history.flush(now=963)
    assert len(writes) == 1
    for at in range(1020, 1020 + 180 * 60, 60):
        history.observe(sample(at), counters())
        history.observe(sample(at + 3), counters())
    assert len(history._minutes) == 120


def test_history_uses_mongodb_without_creating_a_local_database(
    tmp_path, mongo_backend
):
    storage = HistoryStorage(tmp_path)
    storage.load(960)
    assert storage.mongo is mongo_backend
    assert not (tmp_path / "telemetry.sqlite3").exists()
    assert mongo_backend.db.telemetry_minutes.count_documents({}) == 0


def test_history_api_requires_owner_bounds_results_and_hides_storage_errors(
    tmp_path, monkeypatch, mongo_backend
):
    store = DriveStore.create(tmp_path / "drive.sqlite3")
    runtime = FakeRuntime(store)
    runtime.mongo = mongo_backend
    app = create_app(make_settings(tmp_path), runtime=runtime)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            now = time.time()
            params = {"start": now - 3600, "end": now}
            assert (
                client.get("/api/v1/performance/history", params=params).status_code
                == 401
            )
            item = store.add_file("/", "public.txt", 123, 12)["id"]
            token = store.create_share("/" + item)
            assert (
                client.get(
                    "/api/v1/performance/history", params=params | {"auth": token}
                ).status_code
                == 401
            )
            login(client)
            response = client.get("/api/v1/performance/history", params=params)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert len(response.json()["samples"]) <= 360
            assert (
                client.get(
                    "/api/v1/performance/history", params={"start": "nan", "end": now}
                ).status_code
                == 422
            )
            assert (
                client.get(
                    "/api/v1/performance/history",
                    params={"start": now + 1, "end": now + 10},
                ).status_code
                == 422
            )

            def fail(*args):
                raise OSError("mongodb://private-secret")

            monkeypatch.setattr(app.state.telemetry_history, "query", fail)
            failed = client.get("/api/v1/performance/history", params=params)
            assert failed.status_code == 503
            assert "private-secret" not in failed.text
    finally:
        runtime.transfers.registry.close()
        store.close()
