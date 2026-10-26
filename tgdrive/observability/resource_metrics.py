"""Sample Linux CPU and interface counters without optional monitoring services."""

from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path

from tgdrive.observability.traffic import HISTORY_SECONDS, MAX_SAMPLE_GAP_SECONDS


class ResourceMetrics:
    def __init__(self, *, proc=Path("/proc"), cgroup=Path("/sys/fs/cgroup")):
        self.proc, self.cgroup = proc, cgroup
        self.started_at = time.time()
        self._cpu_previous = None
        self._network_previous = None
        self._received = self._sent = 0
        self.history = deque(maxlen=120)
        self.value = {"cpu": {"percent": None}, "network": {"available": False}}

    def _cpu_counter(self):
        try:
            capacity = float(os.cpu_count() or 1)
            if hasattr(os, "sched_getaffinity"):
                capacity = min(capacity, len(os.sched_getaffinity(0)))
            quota, period = (self.cgroup / "cpu.max").read_text().split()
            if quota != "max":
                capacity = min(capacity, int(quota) / int(period))
            stats = dict(
                line.split()
                for line in (self.cgroup / "cpu.stat").read_text().splitlines()
            )
            return "cgroup", int(stats["usage_usec"]) / 1_000_000, capacity
        except OSError, ValueError, KeyError, ZeroDivisionError:
            pass
        try:
            counters = [
                int(value)
                for value in (self.proc / "stat")
                .read_text()
                .splitlines()[0]
                .split()[1:9]
            ]
            if len(counters) < 4:
                return None
            idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
            return "host", sum(counters) - idle, sum(counters)
        except OSError, ValueError, IndexError:
            return None

    def _network_counter(self):
        try:
            interfaces = {}
            for line in (self.proc / "net/dev").read_text().splitlines():
                if ":" not in line:
                    continue
                name, values = line.split(":", 1)
                fields = values.split()
                if name.strip() != "lo" and len(fields) >= 9:
                    interfaces[name.strip()] = (int(fields[0]), int(fields[8]))
            routes = []
            try:
                for line in (self.proc / "net/route").read_text().splitlines()[1:]:
                    fields = line.split()
                    if (
                        len(fields) >= 8
                        and fields[1] == "00000000"
                        and int(fields[3], 16) & 1
                    ):
                        routes.append((int(fields[6]), fields[0]))
            except OSError:
                pass
            try:
                for line in (self.proc / "net/ipv6_route").read_text().splitlines():
                    fields = line.split()
                    if (
                        len(fields) >= 10
                        and fields[0] == "0" * 32
                        and fields[1] == "00"
                    ):
                        routes.append((int(fields[5], 16), fields[-1]))
            except OSError:
                pass
            eligible = sorted(route for route in routes if route[1] in interfaces)
            interface = (
                eligible[0][1]
                if eligible
                else next(iter(interfaces))
                if len(interfaces) == 1
                else None
            )
            return (interface, *interfaces[interface]) if interface else None
        except OSError, ValueError, IndexError:
            return None

    def sample(self):
        now = time.monotonic()
        cpu = self._cpu_counter()
        percent = None
        if cpu is not None and self._cpu_previous is not None:
            previous_at, previous = self._cpu_previous
            elapsed = now - previous_at
            if cpu[0] == previous[0] and 0 < elapsed <= MAX_SAMPLE_GAP_SECONDS:
                denominator = (
                    elapsed * cpu[2] if cpu[0] == "cgroup" else cpu[2] - previous[2]
                )
                delta = cpu[1] - previous[1]
                if denominator > 0 and delta >= 0:
                    percent = min(100.0, 100 * delta / denominator)
        self._cpu_previous = (now, cpu) if cpu is not None else None
        cpu_value = {"percent": percent, "scope": cpu[0] if cpu else None}
        if cpu is not None and cpu[0] == "cgroup":
            cpu_value["capacity_cores"] = cpu[2]
        network = self._network_counter()
        incoming = outgoing = None
        if network is not None and self._network_previous is not None:
            previous_at, previous = self._network_previous
            elapsed = now - previous_at
            if network[0] == previous[0]:
                received, sent = network[1] - previous[1], network[2] - previous[2]
                if received >= 0 and sent >= 0:
                    self._received += received
                    self._sent += sent
                    if 0 < elapsed <= MAX_SAMPLE_GAP_SECONDS:
                        incoming, outgoing = received / elapsed, sent / elapsed
        self._network_previous = (now, network) if network is not None else None
        self.value = {
            "cpu": cpu_value,
            "network": {
                "available": network is not None,
                "interface": network[0] if network else None,
                "started_at": self.started_at,
                "received_bytes": self._received,
                "sent_bytes": self._sent,
                "received_bytes_per_second": incoming,
                "sent_bytes_per_second": outgoing,
                "scope": (
                    "Default network interface; includes URL imports, "
                    "Telegram and HTTP traffic."
                ),
            },
        }
        # Keep measured history even when nobody has the dashboard open. Reading
        # the cgroup files here avoids polling the whole system/disk each tick.
        memory = {}
        for key, filename in (
            ("memory_bytes", "memory.current"),
            ("memory_limit_bytes", "memory.max"),
        ):
            try:
                raw = (self.cgroup / filename).read_text().strip()
                memory[key] = int(raw) if raw != "max" else None
            except OSError, ValueError:
                memory[key] = None
        at = time.time()
        if (
            self.history
            and not 0 <= at - self.history[-1]["at"] <= MAX_SAMPLE_GAP_SECONDS
        ):
            self.history.clear()
        while self.history and self.history[0]["at"] < at - HISTORY_SECONDS:
            self.history.popleft()
        self.history.append(
            {
                "at": at,
                "cpu_percent": percent,
                "cpu_scope": cpu_value["scope"],
                **memory,
                "received_bytes_per_second": incoming,
                "sent_bytes_per_second": outgoing,
                "network_interface": network[0] if network else None,
            }
        )
        self.value = self.value | {"history": list(self.history)}
        return self.value
