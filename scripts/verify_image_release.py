"""Bind privileged image publication to successful, same-repository release CI."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def verify_release(event, repository: str, *, root=Path(".")):
    run = event["workflow_run"]
    if (
        run.get("event") != "push"
        or run.get("conclusion") != "success"
        or run.get("pull_requests")
        or event["repository"]["full_name"] != repository
        or run.get("head_repository", {}).get("id") != event["repository"]["id"]
        or run.get("repository", {}).get("id") != event["repository"]["id"]
    ):
        raise ValueError(
            "Publication requires successful tag-push CI from this repository"
        )
    tag, sha = run.get("head_branch", ""), run.get("head_sha", "")
    match = TAG.fullmatch(tag)
    if match is None or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError(
            "Publication requires an exact application version tag and SHA"
        )

    def git(*args):
        # Fixed git executable/operations; tag and SHA have strict allowlists.
        executable = shutil.which("git")
        if executable is None:
            raise RuntimeError("Git is required for release verification")
        return subprocess.check_output(  # noqa: S603 # nosec B603
            [executable, *args], cwd=root, text=True
        ).strip()

    ref = "refs/tags/" + tag
    if (
        git("cat-file", "-t", ref) != "tag"
        or git("rev-parse", ref + "^{commit}") != sha
    ):
        raise ValueError("The annotated release tag does not identify the tested SHA")
    if git("merge-base", sha, "refs/remotes/origin/main") != sha:
        raise ValueError("The tested release is not in trusted main history")
    version = git("show", sha + ":pyproject.toml")
    if tomllib.loads(version)["project"]["version"] != tag[1:]:
        raise ValueError("The application version and release tag disagree")
    versions = [
        tuple(map(int, parsed.groups()))
        for name in git("tag", "--merged", "refs/remotes/origin/main").splitlines()
        if (parsed := TAG.fullmatch(name)) is not None
    ]
    current = tuple(map(int, match.groups()))
    return {
        "sha": sha,
        "version": tag[1:],
        "image": "ghcr.io/" + repository.split("/", 1)[0].lower() + "/tgdrive",
        "promote_latest": str(current == max(versions)).lower(),
        "promote_minor": str(
            current == max(v for v in versions if v[:2] == current[:2])
        ).lower(),
    }


if __name__ == "__main__":
    outputs = verify_release(
        json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
        os.environ["GITHUB_REPOSITORY"],
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as target:
        for name, value in outputs.items():
            target.write(f"{name}={value}\n")
