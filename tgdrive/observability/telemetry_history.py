"""Disposable, bounded telemetry history; never a source of drive metadata.

Three-second observations are reduced to time-weighted minute aggregates. The
configured MongoDB stores history.
No credentials, filenames, account names or request identifiers are persisted.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path

RETENTION_SECONDS = 31 * 86400
MAX_POINTS = 360
METRICS = (
    "cpu_percent",
    "memory_bytes",
    "received_bytes_per_second",
    "sent_bytes_per_second",
)
TOTALS = (
    "network_received_bytes",
    "network_sent_bytes",
    "http_upload_bytes",
    "http_download_bytes",
    "http_requests",
    "http_errors",
    "telegram_upload_bytes",
    "telegram_download_bytes",
)


def number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def validate_range(start, end, *, now=None):
    now = time.time() if now is None else now
    if (
        not number(start)
        or not number(end)
        or start >= end
        or start >= now
        or end > now + 60
        or end - start > RETENTION_SECONDS
        or start < now - RETENTION_SECONDS - 60
    ):
        raise ValueError("Choose a past interval within the last 31 days.")
    return float(start), min(float(end), now)


class HistoryStorage:
    """All methods run off the event loop, serialized by TelemetryHistory."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.mongo = None
        self.ready = False

    def initialize(self):
        if self.ready:
            return
        from tgdrive.storage.mongo import DatabaseUnavailable, for_directory

        self.mongo = for_directory(self.directory)
        if self.mongo is None:
            raise DatabaseUnavailable("MongoDB is required for telemetry history.")
        self.mongo.db.telemetry_minutes.create_index("at")
        self.ready = True

    def load(self, minute):
        self.initialize()
        state = self.mongo.db.telemetry_state.find_one({"_id": "lifetime"})
        current = self.mongo.db.telemetry_minutes.find_one({"_id": minute})
        return state, current

    def write(self, rows, state, cutoff):
        self.initialize()
        # Absolute replacements make a retry idempotent, including an unknown
        # write outcome. The lifetime checkpoint is never incremented twice.
        from pymongo import ReplaceOne

        if rows:
            self.mongo.db.telemetry_minutes.bulk_write(
                [ReplaceOne({"_id": r["at"]}, r, upsert=True) for r in rows],
                ordered=False,
            )
        self.mongo.db.telemetry_state.replace_one(
            {"_id": "lifetime"}, state, upsert=True
        )
        self.mongo.db.telemetry_minutes.delete_many({"at": {"$lt": cutoff}})

    def read(self, start, end):
        self.initialize()
        return list(
            self.mongo.db.telemetry_minutes.find(
                {"at": {"$gte": math.floor(start / 60) * 60, "$lt": end}}
            )
            .sort("at", 1)
            .limit(31 * 1440 + 2)
        )

    def earliest(self):
        self.initialize()
        row = self.mongo.db.telemetry_minutes.find_one({}, sort=[("at", 1)])
        return row["first"] if row else None


class TelemetryHistory:
    def __init__(self, directory: Path, *, storage=None):
        self.storage = storage or HistoryStorage(directory)
        self._lock = threading.RLock()
        self._ready = False
        self._minutes = {}
        self._previous = None
        self._baselines = {}
        self._totals = dict.fromkeys(TOTALS, None)
        self._tracked_since = None
        self._updated_at = None
        self._last_flush = 0.0
        self._highest_at = 0

    @property
    def initialized(self):
        return self._ready

    def _initialize(self, now):
        if self._ready:
            return
        state, minute = self.storage.load(math.floor(now / 60) * 60)
        if state:
            self._tracked_since = state.get("tracked_since")
            self._updated_at = state.get("last_updated")
            self._totals.update(
                {k: v for k, v in state.get("totals", {}).items() if k in TOTALS}
            )
        if minute:
            self._minutes[minute["at"]] = minute
            self._highest_at = minute["last"]
        self._ready = True

    def _count(self, key, source, current):
        if not number(current):
            return
        identity = (key, source)
        # Refresh insertion order so replacing many connections cannot evict a
        # still-active account and count its cumulative bytes a second time.
        previous = self._baselines.pop(identity, 0)
        self._baselines[identity] = current
        delta = current - previous if current >= previous else current
        self._totals[key] = (self._totals[key] or 0) + delta
        # Replacement connections do not grow monitoring memory indefinitely.
        if len(self._baselines) > 512:
            self._baselines.pop(next(iter(self._baselines)))

    def observe(self, sample, traffic, telegram=None, *, sources=None, now=None):
        """Consume one actual sample, including counters once, on a worker thread."""
        now = sample["at"] if now is None else now
        if not number(now):
            return
        with self._lock:
            self._initialize(now)
            self._tracked_since = self._tracked_since or now
            self._updated_at = now
            network = traffic.get("network", {})
            for key, source in (
                (
                    "network_received_bytes",
                    network.get("received_bytes")
                    if network.get("available", True)
                    else None,
                ),
                (
                    "network_sent_bytes",
                    network.get("sent_bytes")
                    if network.get("available", True)
                    else None,
                ),
                ("http_upload_bytes", traffic.get("upload_bytes")),
                ("http_download_bytes", traffic.get("download_bytes")),
                ("http_requests", traffic.get("requests")),
                ("http_errors", traffic.get("errors")),
            ):
                self._count(key, "process", source)
            for account in (telegram or {}).get("accounts", []):
                account_requests = account.get("requests") or {}
                identity = (sources or {}).get(account["name"], account["name"])
                for direction in ("upload", "download"):
                    self._count(
                        f"telegram_{direction}_bytes",
                        identity,
                        account_requests.get(direction + "_bytes"),
                    )
            previous = self._previous
            if (
                previous
                and previous["at"] >= self._highest_at
                and 0 < now - previous["at"] <= 30
            ):
                self._accumulate(previous, sample)
            # A suspension, restart or clock reversal starts a fresh interval.
            self._previous = dict(sample)
            self._highest_at = max(self._highest_at, now)
            # Bound RAM when the metrics database is unavailable for hours.
            while len(self._minutes) > 120:
                del self._minutes[min(self._minutes)]

    def _accumulate(self, previous, sample):
        start, end = previous["at"], sample["at"]
        while start < end:
            minute = math.floor(start / 60) * 60
            stop = min(end, minute + 60)
            seconds = stop - start
            row = self._minutes.setdefault(
                minute,
                {
                    "at": minute,
                    "values": {},
                    "sources": {},
                    "first": start,
                    "last": stop,
                },
            )
            row["first"], row["last"] = min(row["first"], start), max(row["last"], stop)
            for key in METRICS:
                value = sample.get(key)
                source_key = (
                    "cpu_scope"
                    if key == "cpu_percent"
                    else "network_interface"
                    if key.endswith("per_second")
                    else "memory_limit_bytes"
                )
                source = sample.get(source_key)
                if key != "memory_bytes" and source != previous.get(source_key):
                    continue
                if not number(value):
                    continue
                weighted = row["values"].setdefault(key, [0.0, 0.0])
                weighted[0] += value * seconds
                weighted[1] += seconds
                if source_key not in row["sources"]:
                    row["sources"][source_key] = source
                elif row["sources"][source_key] != source:
                    row["sources"][source_key] = "mixed"
            start = stop

    def flush(self, *, force=False, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._initialize(now)
            monotonic = time.monotonic()
            if not force and monotonic - self._last_flush < 60:
                return
            # Back off even on failure; file service does not wait on diagnostics.
            self._last_flush = monotonic
            state = {
                "tracked_since": self._tracked_since,
                "last_updated": self._updated_at,
                "totals": self._totals,
            }
            self.storage.write(
                list(self._minutes.values()),
                state,
                math.floor((now - RETENTION_SECONDS) / 60) * 60,
            )
            current = math.floor(now / 60) * 60
            self._minutes = {k: v for k, v in self._minutes.items() if k >= current}

    def query(self, start, end, *, now=None):
        now = time.time() if now is None else now
        start, end = validate_range(start, end, now=now)
        with self._lock:
            self._initialize(now)
            # Historical data is deliberately minute-granular. Return the actual
            # boundary minutes rather than pretending to resolve individual seconds.
            start = math.floor(start / 60) * 60
            end = min(math.ceil(end / 60) * 60, now)
            rows = {row["at"]: row for row in self.storage.read(start, end)}
            rows.update(
                {k: v for k, v in self._minutes.items() if k < end and k + 60 > start}
            )
            bucket = max(60, math.ceil((end - start) / MAX_POINTS / 60) * 60)
            accumulators = [({}, {}) for _ in range(math.ceil((end - start) / bucket))]
            for row in rows.values():
                index = int((row["at"] - start) // bucket)
                if not 0 <= index < len(accumulators):
                    continue
                values, source_sets = accumulators[index]
                for key, (total, seconds) in row["values"].items():
                    pair = values.setdefault(key, [0.0, 0.0])
                    pair[0] += total
                    pair[1] += seconds
                for key, source in row["sources"].items():
                    source_sets.setdefault(key, set()).add(source)
            points = []
            # Anchor to the requested interval so the result never exceeds 360.
            for index in range(math.ceil((end - start) / bucket)):
                at, stop = (
                    start + index * bucket,
                    min(end, start + (index + 1) * bucket),
                )
                values, source_sets = accumulators[index]
                point = {"at": at, "end_at": stop, "coverage_seconds": {}}
                for key in ("cpu_scope", "network_interface", "memory_limit_bytes"):
                    sources = source_sets.get(key, set())
                    point[key] = (
                        next(iter(sources))
                        if len(sources) == 1 and "mixed" not in sources
                        else None
                    )
                for key in METRICS:
                    total, seconds = values.get(key, (0, 0))
                    valid = seconds > 0 and (
                        key == "memory_bytes"
                        or point[
                            "cpu_scope" if key == "cpu_percent" else "network_interface"
                        ]
                        is not None
                    )
                    point[key] = total / seconds if valid else None
                    point["coverage_seconds"][key] = seconds if valid else 0
                points.append(point)
            return {
                "available": True,
                "start": start,
                "end": end,
                "bucket_seconds": bucket,
                "retention_seconds": RETENTION_SECONDS,
                "earliest_at": min(
                    (
                        value
                        for value in [
                            self.storage.earliest(),
                            *(r["first"] for r in self._minutes.values()),
                        ]
                        if value is not None
                    ),
                    default=None,
                ),
                "tracked_since": self._tracked_since,
                "last_updated": self._updated_at,
                "totals": dict(self._totals),
                "samples": points,
            }
