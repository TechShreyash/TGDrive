# TGDrive contributor guidance

**Project rule: Make sure you are documenting important features and workings of TGDrive.**

## Keep documentation current with the implementation

When adding or changing an important feature, workflow, default, limit, recovery
guarantee or architectural decision, update its documentation in the same change.
Documentation is part of completing the feature. Internal refactors that preserve
documented behavior do not need artificial documentation changes.

Explain the user experience, how data and work move through TGDrive, why the
policy exists, and its practical limits. Include relevant defaults, configuration,
failure/retry behavior and when work can continue without the browser. Keep
user-facing instructions practical; put detailed thresholds and correctness
rules in the technical documentation.

Keep documentation small and easy to scan. Lead with the steps or facts readers
need, remove repetition, and link to the canonical explanation. Use GitHub-flavored
Markdown tables, task lists and fenced code blocks where useful. Put optional
troubleshooting, implementation detail and long examples in `<details>` sections
with a clear `<summary>` and blank lines around Markdown content. Collapsing text
is not a substitute for shortening it; do not hide essential steps or warnings.

The root README is a product showcase: clear benefits, screenshots/demo media and
links to the guides. Keep installation steps and commands in `docs/setup.md`.
Write for readers who do not yet know Telegram or earlier TGDrive versions.
Use normal links rather than footnotes, and label unfinished media slots clearly.
Keep screenshot/demo replacement instructions in `docs/assets/README.md`.

Verify claims against the implementation. Keep settings, API contracts, limits
and recovery guarantees consistent with the code. Distinguish automated tests,
synthetic browser checks and live validation; never claim unrun checks, deployment
or universal device/network performance. Keep private evidence and credentials
out of public documentation.

## Choose a clear home for each topic

Use the existing structure when it fits:

| Information | Maintain here |
| --- | --- |
| Feature overview and discovery | `README.md`, `docs/README.md` |
| User workflows and status meanings | `docs/usage.md` |
| Components, data flow, scheduling and durability | `docs/architecture.md` |
| Performance policy and bottlenecks | `docs/performance.md` |
| Settings, defaults, limits and API contracts | `docs/configuration.md`, `docs/reference.md` |
| Restart, failures, backup and recovery | `docs/operations.md`, `docs/backup-recovery.md` |
| Relevant validation commands and evidence | `docs/development.md` |

Prefer updating an existing guide. Create a focused file in `docs/` only when a
distinct subject needs its own explanation and would make that guide unwieldy.
These pages are a starting structure, not a fixed list.
Use a descriptive filename, link the page from `docs/README.md` and relevant
guides, and update the root README when feature discovery warrants it. Add public
guides to `PUBLIC_DOCS` in `scripts/build_release.py` so source releases include
them; keep private notes outside that allowlist.

Give each subject one canonical explanation and cross-link it instead of copying
the same details across guides. Update or remove obsolete text, examples and
links when behavior changes. Check affected links and documented commands.

## Ask before running tests or validation

Do not run tests, typechecks, builds, linters, benchmarks or browser validation
unless the user explicitly requests those checks for the current work or approves
the specific proposed validation. Do not run unnecessary checks after every
message. A fix request, clarification, status question, documentation edit or
commit request alone is not permission to start or repeat testing.

Reuse existing results while they remain applicable. Earlier permission to test
is not standing permission for later runs. Before additional validation, explain
the smallest useful check and why it is needed, then wait for approval. Stay
within the approved scope; do not broaden it or repeat passing checks without
asking. Ordinary code reading and Git status/diff inspection may continue.

Report checks actually performed and remaining limitations. Never imply a change
was tested when validation was not requested or approved.

## Write useful commit titles and descriptions

When committing, use a concise, specific title describing the resulting change
in the imperative, such as `Adapt device upload concurrency to measured throughput`.
Include a meaningful description explaining the problem and purpose, important
behavior or architecture changes, validation actually performed, and any material
limitations, compatibility or migration requirements. Scale the detail to the
change; avoid vague titles, file lists and transcripts of the work.

Use Git commit titles and descriptions for change history. Do not maintain or
recreate `CHANGELOG.md`. Project documentation describes how TGDrive currently
works; commit messages explain what changed and why.
