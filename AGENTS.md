# TGDrive agent guide

TGDrive is a self-hosted drive that stores files in Telegram channels. The
backend is Python 3.14.8 (ASGI app `tgdrive.web.app:create_app`) in `tgdrive/`,
the frontend is React + Mantine + Vite in `website/`, and MongoDB holds the
catalog. Human contributor rules live in [CONTRIBUTING.md](CONTRIBUTING.md);
the full developer guide is [docs/development.md](docs/development.md).

## Owner approval rules

These always apply in this repository.

- **Checks:** do not run tests, typechecks, builds, linters, benchmarks or browser
  checks unless the owner asked for them for the current work. Otherwise propose
  the smallest useful check, say why, and wait. Earlier approval does not carry
  over; reuse results that still apply.
- **Pushes:** do not push commits, branches or tags unless the owner asked for
  that specific push. Finish locally, name the remote and branch, and wait.
- **Reporting:** report only checks you actually ran, and say what was not tested.
- **Judgment:** treat the owner's proposed approaches as suggestions. If you see
  a better one, say so and explain the tradeoff. Answer questions without
  changing code.

## Requirements and commands

Docker with Compose 2.24+ runs TGDrive, the test MongoDB and release builds.
Editing also needs uv and Node 24.22+ (below 25). Linux and macOS work natively;
on Windows, develop inside WSL Ubuntu with the checkout in the Linux filesystem.

```bash
uv sync --frozen --all-groups --python 3.14.8
npm ci --prefix website
npm run dev --prefix website        # Vite on 5173, proxies to the app on 8000
uv run --frozen --all-groups python -m pytest -q tests/<area>
npm run typecheck --prefix website
npm test --prefix website
```

Run the app, sample-content preview, full checks and the opt-in MongoDB and
Chromium suites as described in [docs/development.md](docs/development.md).
Browser tests need `TGDRIVE_E2E=1`; MongoDB tests need `TGDRIVE_MONGO_TESTS=1`
and a disposable `TGDRIVE_TEST_MONGODB_URI`, never the real database.

## Layout

| Path | Contents |
| --- | --- |
| `tgdrive/<domain>/` | Backend packages ([code map](docs/architecture.md#components)) |
| `website/src/app/`, `features/`, `shared/` | Shell, feature UI, shared client and theme |
| `tests/<domain>/` | Python tests by backend domain; `tests/browser/` is Chromium |
| `scripts/` | Release packaging, viewer vendoring, brand assets |
| `docs/` | Public guides; new guides go in `PUBLIC_DOCS` in `scripts/build_release.py` |

## Code rules

- Python follows Ruff in `pyproject.toml` (88 columns). TypeScript is strict with
  Prettier at 80 columns.
- Import concrete modules; no barrel or forwarding modules. Put units in names
  for time, bytes and limits.
- Transfer and recovery code must keep cancellation, request identity and
  durable-state ordering ([architecture](docs/architecture.md)).
- Keep lockfiles. Never commit secrets, `.env`, `data/` or `cache/`.

## Gotchas

- Run one server per drive. Avoid reloads during real uploads.
- The app links to `docs/configuration.md`, `docs/backup-recovery.md` and
  `docs/usage.md#preload-a-file-for-smoother-playback-and-downloads`. Keep those
  paths and anchors.
- `docs/request-tracker.md` is private and gitignored. Never publish it.

## Documentation

Update the relevant guide in the same change as any feature, default, limit or
recovery behavior. Follow the [documentation rules](CONTRIBUTING.md#documentation).

## Commits and releases

- Titles are specific and imperative, such as `Adapt device upload concurrency
  to measured throughput`. The body explains the problem, the change, checks
  actually run and any limits or migration steps. Do not create `CHANGELOG.md`.
- Versions, tags and release PRs follow [docs/release.md](docs/release.md) and the
  `tgdrive-release` skill. Change the major version only when the owner asks.
