# TGDrive contributor guidance

**Project rule: Make sure you are documenting important features and workings of TGDrive.**

## Document important behavior with the implementation

When adding or changing an important feature, workflow, default, limit, recovery
guarantee or architectural decision, update its documentation in the same change.
Documentation is part of completing the feature.

Explain what the user experiences, how the data or work moves through TGDrive,
why the policy exists, and its practical limits. Include relevant defaults,
configuration, failure/retry behavior and when work can continue without the
browser. Verify claims against the implementation and distinguish tested behavior
from unverified device, network or Telegram performance.

Use the existing documentation structure:

| Information | Maintain here |
| --- | --- |
| Feature overview and discovery | `README.md`, `docs/README.md` |
| User workflows and status meanings | `docs/usage.md` |
| Components, data flow, scheduling and durability | `docs/architecture.md` |
| Performance policy and bottlenecks | `docs/performance.md` |
| Settings, defaults, limits and API contracts | `docs/configuration.md`, `docs/reference.md` |
| Restart, failures, backup and recovery | `docs/operations.md`, `docs/backup-recovery.md` |
| Relevant validation commands and evidence | `docs/development.md` |
| Concise description of notable changes | `CHANGELOG.md` |

Prefer updating the canonical page and linking to it over creating overlapping
guides. Keep detailed implementation thresholds in architecture/reference docs
and practical explanations in the user guide. Update links and remove stale
claims when behavior changes; do not present local checks as a live deployment
or a guarantee for every device or network.
