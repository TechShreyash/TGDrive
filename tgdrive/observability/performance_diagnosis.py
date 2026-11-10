"""Short-lived aggregate observations, never a second resource controller."""

from __future__ import annotations

import time
from collections import deque
from itertools import pairwise

WINDOW_SECONDS = 30
STALE_AFTER_SECONDS = 15
WORKLOAD_KINDS = (
    "playback_requests",
    "download_requests",
    "unknown_file_requests",
    "upload_requests",
)


def _difference(values):
    if len(values) < 2 or any(value is None for value in values):
        return None
    if any(after < before for before, after in pairwise(values)):
        return None
    return values[-1] - values[0]


class PerformanceDiagnosis:
    def __init__(self, workload):
        self.workload = workload
        self.samples = deque(maxlen=11)

    def observe(self, traffic, telegram, cache, sources):
        now = time.time()
        if self.samples and not 0 < now - self.samples[-1]["at"] <= STALE_AFTER_SECONDS:
            self.samples.clear()
        while self.samples and self.samples[0]["at"] < now - WINDOW_SECONDS:
            self.samples.popleft()
        telegram = telegram or {}
        accounts = telegram.get("accounts", [])
        origin = {}
        for account in accounts:
            requests = account.get("requests")
            identity = sources.get(account["name"])
            if requests is None or identity is None:
                origin = None
                break
            origin[identity] = requests["download_bytes"]
        if not accounts:
            origin = None
        read_activity = getattr(cache, "activity_snapshot", None)
        activity = read_activity() if callable(read_activity) else None
        constraints = []

        def constraint(code, *evidence):
            constraints.append(
                {
                    "code": code,
                    "certainty": "observed",
                    "evidence": [
                        {"name": name, "value": value, "unit": unit}
                        for name, value, unit in evidence
                    ],
                }
            )

        queued = telegram.get("queued")
        request_metrics = [
            account["requests"]
            for account in accounts
            if account.get("requests") is not None
        ]
        account_queued = (
            sum(row["queued"] for row in request_metrics) if request_metrics else None
        )
        if queued or account_queued:
            evidence = []
            if queued is not None:
                evidence.append(("pool_queued_operations", queued, "operations"))
            if account_queued is not None:
                evidence.append(
                    ("observed_account_queued_requests", account_queued, "requests")
                )
            limits = [
                account["combined_limit"]
                for account in accounts
                if account.get("combined_limit") is not None
            ]
            if limits:
                evidence.append(
                    ("configured_operations_per_account", max(limits), "operations")
                )
            constraint("telegram_queue", *evidence)
        cooling = [
            max(
                account.get("cooldown_seconds", 0),
                (account.get("requests") or {}).get("cooldown_seconds", 0),
            )
            for account in accounts
            if queued or (account.get("requests") or {}).get("queued", 0)
        ]
        cooling = [seconds for seconds in cooling if seconds > 0]
        if cooling:
            constraint(
                "telegram_cooldown",
                ("cooling_accounts_during_queue", len(cooling), "accounts"),
                ("longest_cooldown_seconds", max(cooling), "seconds"),
            )
        scratch = activity["scratch"] if activity is not None else {}
        if scratch.get("waiting_heads", 0):
            constraint(
                "scratch_wait",
                ("waiting_heads", scratch["waiting_heads"], "streams"),
                ("reserved_bytes", scratch["bytes"], "bytes"),
                ("effective_limit_bytes", scratch["max_bytes"], "bytes"),
            )
        self.samples.append(
            {
                "at": now,
                "workload": dict(self.workload),
                "http": traffic.download_bytes,
                "requests": traffic.requests,
                "origin": origin,
                "cache": (id(cache), activity["hits"], activity["misses"])
                if activity is not None
                else None,
                "busy": bool(
                    any(self.workload.values())
                    or telegram.get("active", 0)
                    or queued
                    or account_queued
                    or scratch.get("active_streams", 0)
                ),
                "constraints": constraints,
            }
        )

    def snapshot(self):
        now = time.time()
        latest = self.samples[-1] if self.samples else None
        result = {
            "sampled_at": latest["at"] if latest else None,
            "window_seconds": WINDOW_SECONDS,
            "observed_seconds": 0,
            "stale_after_seconds": STALE_AFTER_SECONDS,
            "state": "unavailable",
            "workload": dict(self.workload),
            "telegram_inbound_bytes_per_second": None,
            "http_delivered_bytes_per_second": None,
            "cache_hits": None,
            "cache_misses": None,
            "constraints": [],
        }
        if latest is None or not 0 <= now - latest["at"] <= STALE_AFTER_SECONDS:
            return result
        rows = list(self.samples)
        seconds = latest["at"] - rows[0]["at"]
        http = _difference([row["http"] for row in rows])
        requests = _difference([row["requests"] for row in rows])
        origins = [row["origin"] for row in rows]
        inbound = None
        if all(
            origin is not None and origin.keys() == origins[0].keys()
            for origin in origins
        ):
            deltas = [
                _difference([origin[key] for origin in origins]) for key in origins[0]
            ]
            if all(delta is not None for delta in deltas):
                inbound = sum(deltas)
        caches = [row["cache"] for row in rows]
        hits = misses = None
        if all(cache is not None and cache[0] == caches[0][0] for cache in caches):
            hits = _difference([cache[1] for cache in caches])
            misses = _difference([cache[2] for cache in caches])
        busy = (
            any(row["busy"] for row in rows)
            or bool(http)
            or bool(inbound)
            or bool(requests)
        )
        return result | {
            "observed_seconds": seconds,
            "state": "observing" if seconds < 6 else "active" if busy else "idle",
            "workload": latest["workload"],
            "telegram_inbound_bytes_per_second": inbound / seconds
            if inbound is not None and seconds
            else None,
            "http_delivered_bytes_per_second": http / seconds
            if http is not None and seconds
            else None,
            "cache_hits": hits,
            "cache_misses": misses,
            "constraints": latest["constraints"][:8],
        }
