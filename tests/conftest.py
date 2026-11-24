"""Test-only coverage for isolated Python workers with file-size limits."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from coverage import Coverage

_WORKERS = frozenset(
    {
        "tgdrive.media.archive_preview_worker",
        "tgdrive.media.compatibility_worker",
        "tgdrive.media.thumbnail_worker",
        "tgdrive.transfers.archive_worker",
        "tgdrive.transfers.torrent_worker",
    }
)
_LAUNCHER = Path(__file__).parent / "support" / "native_worker_coverage.py"


@pytest.fixture(autouse=True)
def native_worker_traces(monkeypatch):
    trace = Coverage.current()
    if trace is None or os.name != "posix":
        yield
        return

    original = asyncio.create_subprocess_exec
    readers = []
    traces = []
    mode = "branch" if trace.get_data().has_arcs() else "line"

    def collect(descriptor):
        with os.fdopen(descriptor, "rb") as stream:
            traces.append(stream.read())

    async def spawn(*command: Any, **kwargs: Any):
        if (
            len(command) < 3
            or command[:2] != (sys.executable, "-m")
            or command[2] not in _WORKERS
        ):
            return await original(*command, **kwargs)
        read_end, write_end = os.pipe()
        reader = threading.Thread(target=collect, args=(read_end,), daemon=True)
        reader.start()
        readers.append(reader)
        kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), write_end)
        try:
            return await original(
                sys.executable,
                str(_LAUNCHER),
                str(write_end),
                mode,
                *command[2:],
                **kwargs,
            )
        finally:
            os.close(write_end)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    yield
    for reader in readers:
        reader.join(timeout=5)
        assert not reader.is_alive(), "Native worker coverage pipe stayed open"
    for raw in traces:
        if raw:  # A killed process cannot flush a trace.
            data = json.loads(raw)
            if mode == "branch":
                trace.get_data().add_arcs(
                    {
                        filename: [tuple(arc) for arc in arcs]
                        for filename, arcs in data.items()
                    }
                )
            else:
                trace.get_data().add_lines(data)
