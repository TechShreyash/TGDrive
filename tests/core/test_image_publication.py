"""Publication gates use isolated Git history, never a registry or GitHub token."""

import copy
import shutil
import subprocess

import pytest

from scripts.verify_image_release import verify_release


def git(root, *args):
    executable = shutil.which("git")
    assert executable is not None
    # Arguments name only fixture-owned repositories and generated release tags.
    return subprocess.check_output([executable, *args], cwd=root, text=True).strip()  # noqa: S603


def release(root, version):
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    git(root, "add", "pyproject.toml")
    git(root, "commit", "-m", "Release " + version)
    git(root, "tag", "-a", "v" + version, "-m", "Release " + version)
    sha = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", sha)
    return sha


@pytest.fixture
def source(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    sha = release(tmp_path, "2.3.0")
    repository = {"id": 1, "full_name": "Owner/TGDrive"}
    event = {
        "repository": repository,
        "workflow_run": {
            "event": "push",
            "conclusion": "success",
            "pull_requests": [],
            "head_branch": "v2.3.0",
            "head_sha": sha,
            "repository": repository,
            "head_repository": repository,
        },
    }
    return tmp_path, event


def test_successful_tag_ci_binds_version_sha_and_lowercase_image(source):
    root, event = source
    result = verify_release(event, "Owner/TGDrive", root=root)
    assert result == {
        "sha": event["workflow_run"]["head_sha"],
        "version": "2.3.0",
        "image": "ghcr.io/owner/tgdrive",
        "promote_latest": "true",
        "promote_minor": "true",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"event": "pull_request"},
        {"event": "workflow_dispatch"},
        {"head_repository": {"id": 2}},
        {"repository": {"id": 2}},
        {"pull_requests": [{"number": 1}]},
        {"head_branch": "v-malicious"},
        {"head_branch": "v2.3.0\nimage=untrusted"},
        {"head_branch": "viewer-movi-2.3.0"},
        {"head_sha": "0" * 40},
        {"conclusion": "failure"},
    ],
)
def test_untrusted_or_unbound_ci_cannot_publish(source, change):
    root, event = source
    event = copy.deepcopy(event)
    event["workflow_run"].update(change)
    with pytest.raises(ValueError):
        verify_release(event, "Owner/TGDrive", root=root)


def test_rerunning_an_old_release_cannot_move_latest_or_minor_backwards(source):
    root, event = source
    release(root, "2.3.1")
    release(root, "2.4.0")
    result = verify_release(event, "Owner/TGDrive", root=root)
    assert result["promote_latest"] == result["promote_minor"] == "false"


def test_tag_outside_trusted_main_is_rejected(source):
    root, event = source
    git(root, "checkout", "-b", "unreviewed")
    sha = release(root, "2.3.1")
    git(
        root,
        "update-ref",
        "refs/remotes/origin/main",
        event["workflow_run"]["head_sha"],
    )
    event["workflow_run"].update(head_branch="v2.3.1", head_sha=sha)
    with pytest.raises(ValueError, match="trusted main"):
        verify_release(event, "Owner/TGDrive", root=root)


def test_lightweight_application_tag_is_rejected(source):
    root, event = source
    git(root, "tag", "-d", "v2.3.0")
    git(root, "tag", "v2.3.0")
    with pytest.raises(ValueError, match="annotated release tag"):
        verify_release(event, "Owner/TGDrive", root=root)


def test_tag_cannot_publish_a_different_application_version(source):
    root, event = source
    git(root, "tag", "-a", "v2.3.1", "-m", "Mismatched version")
    event["workflow_run"]["head_branch"] = "v2.3.1"
    with pytest.raises(ValueError, match="version and release tag disagree"):
        verify_release(event, "Owner/TGDrive", root=root)


def test_maintenance_release_can_promote_its_minor_without_downgrading_latest(source):
    root, event = source
    release(root, "2.4.0")
    sha = release(root, "2.3.1")
    event["workflow_run"].update(head_branch="v2.3.1", head_sha=sha)
    result = verify_release(event, "Owner/TGDrive", root=root)
    assert result["version"] == "2.3.1"
    assert result["promote_minor"] == "true"
    assert result["promote_latest"] == "false"


def test_other_repository_full_name_cannot_use_matching_run_ids(source):
    root, event = source
    with pytest.raises(ValueError, match="from this repository"):
        verify_release(event, "Other/TGDrive", root=root)
