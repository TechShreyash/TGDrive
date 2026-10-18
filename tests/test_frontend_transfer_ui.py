"""Run the real TypeScript transfer behavior suite when frontend tools are installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_resumable_frontend_behavior() -> None:
    node = shutil.which("node")
    if not node or not (ROOT / "website/node_modules/vitest/vitest.mjs").exists():
        pytest.skip(
            "Install the locked frontend dependencies to run TypeScript behavior tests"
        )
    result = subprocess.run(  # noqa: S603 - fixed, repository-owned test entry
        [node, str(ROOT / "tests/frontend_transfer_behavior.cjs")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
