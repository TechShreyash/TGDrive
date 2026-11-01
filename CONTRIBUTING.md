# Contributing to TGDrive

Thanks for helping. This page covers how to propose changes; the
[development guide](docs/development.md) covers setup, commands and tests.

## Before you start

- Bugs and feature ideas: open an [issue](https://github.com/TechShreyash/TGDrive/issues/new/choose).
- Security problems: do not open a public issue; follow [SECURITY.md](SECURITY.md).
- Work on the `dev` branch and open pull requests against `dev`. `main` receives
  reviewed, versioned releases ([release procedure](docs/release.md)).

## Pull requests

- Keep each PR to one change. Follow the [code conventions](docs/development.md#code-conventions).
- Add or update tests for behavior changes, and say in the PR which checks you ran.
- Update documentation in the same PR when behavior, settings, defaults or limits change.
- Use a specific, imperative commit title, such as `Adapt device upload concurrency
  to measured throughput`. The description explains the problem, the change,
  checks actually run and any migration steps. TGDrive has no `CHANGELOG.md`;
  commit history is the change record.

## Documentation

Docs describe how TGDrive works now. Write for readers who don't know Telegram
or earlier TGDrive versions.

| Information | Home |
| --- | --- |
| Product overview and screenshots | `README.md`, `docs/showcase.md` |
| Installation | `docs/setup.md`, `docs/hosting.md` |
| Using the website | `docs/usage.md`, `docs/reading.md`, `docs/thumbnails.md` |
| Settings and deployment variables | `docs/configuration.md`, `docs/reference.md` |
| Restart, upgrade, backup and recovery | `docs/operations.md`, `docs/backup-recovery.md` |
| Speed and capacity | `docs/performance-tuning.md` (operators), `docs/performance.md` (internals) |
| HTTP API | `docs/api.md` |
| Components, data flow and durability | `docs/architecture.md` |
| Development, CI and releases | `docs/development.md`, `docs/release.md` |

- Give each topic one canonical page and link to it from elsewhere; don't copy details.
- Lead with the steps or facts the reader needs. Keep the root README a showcase;
  installation commands belong in `docs/setup.md`.
- Use tables, task lists and fenced code blocks where they help. Put optional
  detail in `<details>` blocks, never essential steps or warnings.
- Check every claim against the code: settings, limits, API contracts and
  recovery guarantees. Don't claim tests, deployments or device performance that
  weren't checked. Keep credentials and private evidence out of the docs.
- Test results belong in PR descriptions and CI, not in the guides.
- A new guide gets a link from `docs/README.md` and an entry in `PUBLIC_DOCS` in
  `scripts/build_release.py`. Screenshot instructions live in
  [docs/assets/README.md](docs/assets/README.md).
- Don't rename `docs/configuration.md`, `docs/backup-recovery.md` or the
  `docs/usage.md#preload-a-file-for-smoother-playback-and-downloads` anchor; the
  app links to them.
