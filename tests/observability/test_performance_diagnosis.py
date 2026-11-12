from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.responses import Response

from tests.support.app_fixtures import app_client as app_client
from tests.support.app_fixtures import login
from tgdrive.observability.performance_diagnosis import (
    WORKLOAD_KINDS,
    PerformanceDiagnosis,
    _difference,
)
from tgdrive.observability.traffic import TrafficCounters, TrafficMiddleware


@pytest.fixture
def observation(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(
        "tgdrive.observability.performance_diagnosis.time.time", lambda: now[0]
    )
    workload = dict.fromkeys(WORKLOAD_KINDS, 0)
    diagnosis = PerformanceDiagnosis(workload)
    traffic = SimpleNamespace(download_bytes=0, requests=0)
    activity = {
        "hits": 0,
        "misses": 0,
        "scratch": {
            "bytes": 4,
            "max_bytes": 4,
            "waiting_heads": 0,
            "active_streams": 0,
        },
    }
    cache = SimpleNamespace(activity_snapshot=lambda: activity)
    account = {
        "name": "private-account",
        "combined_limit": 3,
        "cooldown_seconds": 0,
        "requests": {"queued": 0, "download_bytes": 0, "cooldown_seconds": 0},
    }
    telegram = {
        "active": 0,
        "queued": 0,
        "accounts": [account],
        "queue": [{"request_id": "private-token", "client_name": "private-path"}],
    }
    sources = {account["name"]: 1}

    def sample():
        diagnosis.observe(traffic, telegram, cache, sources)
        return diagnosis.snapshot()

    return SimpleNamespace(**locals())


def test_window_states_and_separate_http_origin_and_cache_deltas(observation):
    o = observation
    assert o.diagnosis.snapshot()["state"] == "unavailable"
    assert o.sample()["state"] == "observing"
    assert o.diagnosis.snapshot()["http_delivered_bytes_per_second"] is None
    o.now[0] += 6
    assert o.sample()["state"] == "idle"
    o.workload["download_requests"] = 1
    o.traffic.download_bytes = 1200
    o.account["requests"]["download_bytes"] = 300
    o.activity["hits"], o.activity["misses"] = 5, 2
    o.now[0] += 6
    result = o.sample()
    assert result["state"] == "active"
    assert result["observed_seconds"] == 12
    assert result["http_delivered_bytes_per_second"] == 100
    assert result["telegram_inbound_bytes_per_second"] == 25
    assert (result["cache_hits"], result["cache_misses"]) == (5, 2)
    assert result["workload"]["download_requests"] == 1
    assert not result["constraints"]


@pytest.mark.parametrize("change", ["reset", "replaced", "missing"])
def test_origin_discontinuities_remain_unknown(observation, change):
    o = observation
    o.account["requests"]["download_bytes"] = 300
    o.sample()
    o.now[0] += 6
    o.traffic.download_bytes = 60
    if change == "reset":
        o.account["requests"]["download_bytes"] = 1
    elif change == "replaced":
        o.sources["private-account"] = 2
    else:
        o.account["requests"] = None
    result = o.sample()
    assert result["telegram_inbound_bytes_per_second"] is None
    assert result["http_delivered_bytes_per_second"] == 10


def test_cache_replacement_and_counter_reset_are_not_zero_hits(observation):
    o = observation
    o.activity["hits"] = 2
    o.sample()
    o.now[0] += 6
    o.activity["hits"] = 0
    assert o.sample()["cache_hits"] is None
    o.diagnosis.observe(o.traffic, o.telegram, None, o.sources)
    assert o.diagnosis.snapshot()["cache_hits"] is None


@pytest.mark.parametrize("values", [(1, None, 3), (None, 2, 3), (3, 1, 4)])
def test_counter_gaps_and_interior_resets_remain_unknown(values):
    assert _difference(values) is None


def test_non_dictionary_cache_activity_remains_unavailable(observation):
    o = observation
    o.cache.activity_snapshot = lambda: object()
    o.sample()
    o.now[0] += 6
    o.traffic.download_bytes = 60
    result = o.sample()
    assert result["cache_hits"] is result["cache_misses"] is None
    assert result["http_delivered_bytes_per_second"] == 10


def test_multiple_observed_waits_have_units_without_private_values(observation):
    o = observation
    o.telegram["queued"] = 2
    o.account["requests"].update(queued=1, cooldown_seconds=4)
    o.activity["scratch"]["waiting_heads"] = 3
    result = o.sample()
    assert {row["code"] for row in result["constraints"]} == {
        "telegram_queue",
        "telegram_cooldown",
        "scratch_wait",
    }
    assert all(row["certainty"] == "observed" for row in result["constraints"])
    assert all(
        item["unit"] and isinstance(item["value"], (int, float))
        for row in result["constraints"]
        for item in row["evidence"]
    )
    assert "private-" not in json.dumps(result)
    assert "mongo" not in json.dumps(result)
    o.telegram["queued"] = 0
    o.account["requests"]["queued"] = 0
    o.activity["scratch"]["waiting_heads"] = 0
    o.now[0] += 3
    assert o.sample()["constraints"] == []  # A cooling idle account is not a wait.


def test_missing_account_metrics_do_not_become_zero_evidence(observation):
    o = observation
    o.telegram["accounts"] = []
    o.telegram["queued"] = 2
    result = o.sample()
    evidence = result["constraints"][0]["evidence"]
    assert evidence == [
        {"name": "pool_queued_operations", "value": 2, "unit": "operations"}
    ]
    assert result["telegram_inbound_bytes_per_second"] is None


def test_stale_clock_jumps_and_long_windows_are_bounded(observation):
    o = observation
    for _ in range(100):
        o.now[0] += 3
        o.sample()
    assert len(o.diagnosis.samples) == 11
    assert o.diagnosis.snapshot()["observed_seconds"] == 30
    o.now[0] += 16
    result = o.diagnosis.snapshot()
    assert result["state"] == "unavailable"
    assert result["http_delivered_bytes_per_second"] is None
    assert result["constraints"] == []
    assert o.sample()["observed_seconds"] == 0
    o.now[0] -= 100
    assert o.diagnosis.snapshot()["state"] == "unavailable"
    assert o.sample()["state"] == "observing"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", WORKLOAD_KINDS[:3])
async def test_classified_requests_release_counts_on_cancellation(kind):
    counters = TrafficCounters()
    entered = asyncio.Event()

    async def app(scope, _receive, _send):
        counters.classify(scope, kind)
        counters.classify(scope, kind)
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        TrafficMiddleware(app, counters)({"type": "http", "path": "/file"}, None, None)
    )
    await entered.wait()
    assert counters.workload[kind] == 1
    assert sum(counters.workload.values()) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(value == 0 for value in counters.workload.values())


@pytest.mark.parametrize(
    "params,headers,expected",
    [
        ({}, {}, "unknown_file_requests"),
        ({"preview": "true"}, {}, "playback_requests"),
        ({}, {"sec-fetch-dest": "video"}, "playback_requests"),
        ({"download": "true"}, {"sec-fetch-dest": "video"}, "download_requests"),
    ],
)
def test_file_route_reuses_playback_classifier(
    app_client, monkeypatch, params, headers, expected
):
    client, store, _runtime, _settings = app_client
    item = store.add_file("/", "movie.mp4", 7, 4)
    login(client)
    seen = []

    async def stream(**kwargs):
        seen.append(dict(kwargs["request"].app.state.traffic.workload))
        return Response(b"data")

    monkeypatch.setattr("tgdrive.web.app.stream_telegram_file", stream)
    assert (
        client.get(
            "/file", params={"path": "/" + item["id"], **params}, headers=headers
        ).status_code
        == 200
    )
    assert seen == [{kind: int(kind == expected) for kind in WORKLOAD_KINDS}]
    assert not any(client.app.state.traffic.workload.values())


def test_diagnosis_remains_on_the_authenticated_performance_route(app_client):
    client, _store, _runtime, _settings = app_client
    assert client.get("/api/v1/performance").status_code == 401
    login(client)
    response = client.get("/api/v1/performance")
    assert response.status_code == 200
    diagnosis = response.json()["diagnosis"]
    assert diagnosis["window_seconds"] == 30
    assert diagnosis["stale_after_seconds"] == 15
    assert diagnosis["state"] in {"idle", "observing", "active", "unavailable"}
    assert diagnosis["telegram_inbound_bytes_per_second"] is None


@pytest.mark.parametrize(
    "platform,expected", [("darwin", 5000), ("linux", 5000 * 1024)]
)
def test_peak_memory_units_follow_the_native_platform(
    tmp_path, monkeypatch, platform, expected
):
    from tgdrive.observability import performance_api

    monkeypatch.setattr(performance_api.sys, "platform", platform)
    monkeypatch.setattr(
        performance_api.resource,
        "getrusage",
        lambda _who: SimpleNamespace(ru_maxrss=5000),
    )
    result = performance_api.system_snapshot(
        SimpleNamespace(
            resolved_data_dir=tmp_path,
            resolved_cache_dir=tmp_path,
            staging_budget_bytes=0,
        )
    )
    assert result["process_peak_rss_bytes"] == expected
