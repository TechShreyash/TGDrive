---
name: tgdrive-release
description: Prepare a TGDrive release or version bump. Use when choosing the next version, synchronizing version metadata, writing a release PR, tagging vX.Y.Z or drafting a GitHub release.
---

# TGDrive release

[docs/release.md](../../../docs/release.md) is the canonical procedure. Read it first; this skill is the short checklist agents most often get wrong.

## Approval gates

- Do not run checks, builds or CI without approval for that specific run (AGENTS.md).
- Do not push commits, branches or tags without approval for that specific push. Name the remote and branch, then wait.

## Steps

1. Pick the version from changes since the last tag (`git describe --tags --abbrev=0`): patch for fixes/docs/maintenance, minor for compatible features, major for breaking changes.
2. Set it in `pyproject.toml` (the authority), then sync every copy:
   - `tgdrive/__init__.py`
   - `website/package.json`
   - both root version entries in `website/package-lock.json`
   - the `tgdrive` project entry in `uv.lock`
   - the Docker image version label in `Dockerfile`
3. With approval, verify sync: `python3 scripts/build_release.py --check-version`.
4. Update user docs whose settings, defaults or limits changed. Never edit or recreate `CHANGELOG.md`; history lives in commit messages.
5. Release PR from `dev` to `main` names the target version, changes, compatibility and the validation actually run.
6. After merge, tag the final `main` commit only:

```bash
git switch main
git pull --ff-only origin main
python3 scripts/build_release.py --check-version --tag vX.Y.Z
git tag -a vX.Y.Z -m "Release TGDrive X.Y.Z"
```

7. With push approval, push the tag and create the GitHub release linking the PR, the commit and the compare view against the previous tag.

## Never

- Move or reuse a published tag. Fix mistakes with a new version.
- Tag a `dev` or unmerged commit.
- Claim checks, deployments or live Telegram validation that did not run.
