from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tgdrive.observability.traffic import (
    TrafficCounters,
    TrafficMiddleware,
    sample_performance,
)


@pytest.mark.asyncio
async def test_traffic_counts_received_bytes_and_only_successful_sends():
    counters = TrafficCounters()

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ready", "more_body": True})
        await send(
            {"type": "http.response.body", "body": b"not delivered", "more_body": False}
        )

    async def receive():
        return {"type": "http.request", "body": b"received"}

    async def send(message):
        if message.get("body") == b"not delivered":
            raise OSError("disconnected client")

    middleware = TrafficMiddleware(app, counters)
    with pytest.raises(OSError, match="disconnected client"):
        await middleware(
            {"type": "http", "path": "/api/v1/uploads/example"}, receive, send
        )
    assert counters.requests == counters.errors == 1
    assert counters.active == 0
    assert counters.upload_bytes == len(b"received")
    assert counters.download_bytes == len(b"ready")


def test_traffic_history_is_bounded_and_restarts_without_fabricated_rates(
    monkeypatch,
):
    now = [1000.0]
    monkeypatch.setattr("tgdrive.observability.traffic.time.time", lambda: now[0])
    counters = TrafficCounters()
    for _index in range(180):
        now[0] += 1
        counters.download_bytes += 100
        counters.snapshot()
    snapshot = counters.snapshot()
    assert len(snapshot["history"]) == 120
    assert snapshot["history"][0]["download_bytes"] == 6100
    assert snapshot["history"][-1]["download_bytes"] == 18000
    assert len(counters.snapshot()["history"]) == 120
    restarted = TrafficCounters().snapshot()
    assert restarted["download_bytes"] == restarted["requests"] == 0
    assert len(restarted["history"]) == 1
    assert restarted["download_bytes_per_second"] is None


def test_rates_reject_old_samples_and_clock_jumps(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("tgdrive.observability.traffic.time.time", lambda: now[0])
    counters = TrafficCounters()
    counters.snapshot()
    now[0] += 3
    counters.download_bytes += 3000
    counters.upload_bytes += 600
    current = counters.snapshot()
    assert current["download_bytes_per_second"] == 1000
    assert current["upload_bytes_per_second"] == 200
    now[0] += 44683
    stale = counters.snapshot()
    assert len(stale["history"]) == 1
    assert stale["download_bytes_per_second"] is None
    now[0] -= 100
    assert len(counters.snapshot()["history"]) == 1


@pytest.mark.asyncio
async def test_sampler_records_without_dashboard_and_cancels_cleanly():
    counters = TrafficCounters()
    sampled = asyncio.Event()

    class Pool:
        def performance_snapshot(self):
            sampled.set()

    task = asyncio.create_task(sample_performance(counters, Pool(), interval=60))
    await asyncio.wait_for(sampled.wait(), 1)
    assert len(counters.history) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_system_snapshot_distinguishes_current_and_peak_memory(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tgdrive.observability.performance_api import system_snapshot

    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/status":
            return "Name:\ttgdrive\nVmRSS:\t12345 kB\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    current = system_snapshot(
        SimpleNamespace(resolved_data_dir=tmp_path, staging_budget_bytes=1024)
    )
    assert current["process_rss_bytes"] == 12345 * 1024
    assert current["process_peak_rss_bytes"] > 0
    assert current["cpu_count"] is None or current["cpu_count"] > 0
