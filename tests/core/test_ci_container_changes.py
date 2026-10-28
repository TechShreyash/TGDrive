from __future__ import annotations

import os
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.mark.parametrize(
    ("changed_file", "required"),
    [
        ("Dockerfile", True),
        ("uv.lock", True),
        ("website/package-lock.json", True),
        ("website/vendor/player.tgz", True),
        ("README.md", False),
        ("tgdrive/web/app.py", False),
    ],
)
def test_container_detection_compares_the_whole_pr(
    tmp_path: Path, changed_file: str, required: bool
) -> None:
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
    ).read_text(encoding="utf-8")
    step = workflow.split("id: container_changes\n", 1)[1]
    script = dedent(step.split("run: |\n", 1)[1].split("\n            - name:", 1)[0])

    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603 - fixed Git commands in an isolated repository
            [  # noqa: S607 - standard Git executable in an isolated repository
                "git",
                "-c",
                "user.name=CI test",
                "-c",
                "user.email=ci@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "--initial-branch=main")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Base")
    git("switch", "-c", "dependency")
    changed = tmp_path / changed_file
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("updated\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Update dependency")
    (tmp_path / "followup.txt").write_text("unrelated\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Unrelated follow-up")
    git("switch", "main")
    git("merge", "--no-ff", "dependency", "-m", "Synthetic PR merge")

    output = tmp_path / "output.txt"
    subprocess.run(  # noqa: S603 - trusted workflow script and synthetic repository
        ["bash", "-e", "-c", script],  # noqa: S607 - standard Bash executable
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        check=True,
        capture_output=True,
    )

    assert output.read_text(encoding="utf-8") == f"required={str(required).lower()}\n"
