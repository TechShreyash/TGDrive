# TGDrive agent guide

TGDrive stores files in Telegram and the catalog in MongoDB. The Python backend
lives in `tgdrive/` and the React, Mantine and Vite frontend lives in `website/`.

## Owner approval rules

- **Pushes:** get approval for each specific push of a commit, branch or tag.
  Finish locally, name the remote and branch, and wait.
- **Judgment:** answer questions without changing code. If another approach
  serves the owner's goal better, explain the tradeoff.

## Frontend workflow

- Route UI work with [ui-skills-root](.agents/skills/ui-skills-root/SKILL.md) or
  `npx ui-skills start`. Use `categories`, `list --category <category>`, and
  `get <slug>` as needed to load focused guidance. [UI Skills CLI](https://www.ui-skills.com/cli)
- Use [frontend-design](.agents/skills/frontend-design/SKILL.md) for new UI and
  substantial visual changes. Use [ui-taste](.agents/skills/ui-taste/SKILL.md)
  to review and polish existing screens. Ground typography, color, spacing,
  and motion in TGDrive's current design and the workflow being improved.
- Prefer existing Mantine controls. For custom interactions they do not cover,
  follow [ui-sdk](.agents/skills/ui-sdk/SKILL.md) and read the installed docs at
  `website/node_modules/@ui-sdk/react/docs/llms.txt`. For a new primitive, also
  consult `core.md` and `rendering.md` in that directory. [UI SDK docs](https://ui-sdk.dev/docs)
- For ad hoc browser checks, use the session's built-in browser controls when
  available. The automated suite in `tests/browser/` uses Playwright.

## Repository guides

- For setup, check commands, code conventions, and the code and test map, see the
  [development guide](docs/development.md). On Windows, use WSL Ubuntu with the
  checkout in the Linux filesystem.
- Before UI work, read the [product brief](PRODUCT.md). Before changing transfer
  or recovery behavior, review the [architecture guide](docs/architecture.md).
- For commits, pull requests, and documentation updates, follow the
  [contribution guide](CONTRIBUTING.md). Update the relevant guide in the same
  change when behavior, settings, defaults, or limits change.
- Use the `tgdrive-release` skill for version bumps and releases. Major version
  bumps require the owner's direction. [Release procedure](docs/release.md)

Keep lockfiles. Never commit secrets, `.env`, `data/`, or `cache/`.
