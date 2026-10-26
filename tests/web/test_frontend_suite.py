"""Run the complete Vitest behavior suite when frontend tools are installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_frontend_behavior_suite() -> None:
    node = shutil.which("node")
    if not node or not (ROOT / "website/node_modules/vitest/vitest.mjs").exists():
        pytest.skip(
            "Install the locked frontend dependencies to run the complete Vitest suite"
        )
    result = subprocess.run(  # noqa: S603 - fixed, repository-owned test entry
        [node, str(ROOT / "website/node_modules/vitest/vitest.mjs"), "run"],
        cwd=ROOT / "website",
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
