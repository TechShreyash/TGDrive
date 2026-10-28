# Status and roadmap

[Docs home](README.md) · [Validation evidence](development.md#ci-and-release-evidence)

TGDrive is a single-owner, self-hosted drive. This page separates available
features from remaining validation and known limits.

[See the available interface and workflows](showcase.md); the recordings are
illustrative and do not certify the validation still listed below.

## What works now

| Available | Learn more |
| --- | --- |
| File/folder uploads, multipart storage, Trash and history | [User guide](usage.md) |
| Previews, readers and scoped sharing | [Readers](reading.md) · [Sharing](usage.md#share-and-revoke) |
| Monitoring and multiple Telegram accounts | [Performance](performance.md) · [Configuration](configuration.md) |
| MongoDB persistence, one encrypted recovery file and optional channel protection | [Backup & recovery](backup-recovery.md) |

Available features still have [configured limits](configuration.md) and are not
certified for every device or network.

## Before public release

Use the [release procedure](release.md) against the exact commit (and extracted
archive if offering custom source assets). Publication is a separate maintainer
action. Remaining live validation:

- [ ] Fresh laptop and hosted installs, persistent volumes, stop/start and recovery.
- [ ] Live unified-file recovery: lost MongoDB, replacement main channel, revoked bot access, legacy backups and interrupted activation.
- [ ] Coolify DNS/TLS, trusted forwarding and isolation of the direct app port.
- [ ] Prolonged multi-account outages and interrupted Telegram sends.
- [ ] Actual Premium speed, broader devices/codecs, ARM and hosting platforms.

## Known correctness and recovery gaps

| Area | Current limit |
| --- | --- |
| Authentication | Password-only owner login; no MFA/passkeys, idle session timeout or fresh reauthentication for sensitive settings/key exports. Login throttling is process-local. |
| Credentials | Same-account token/session replacement needs restart; Refresh can apply verified channel-permission repair to a live client. |
| Bot imports | Do not use the web queue's full durable job model. |
| History | Compaction must preserve unresolved effects and retry identities. |
| Large folders | Listings sort/materialize before slicing; database cursor pagination remains work. |
| Direct API clients | Some request mappings are process-local and error formats vary; use documented v2 flows. |
| Storage channel changes | Require verified channel recovery; editing an ID does not move files. |

See [Troubleshooting](operations.md#troubleshooting) for current recovery actions.

<details>
<summary>Next improvements and scope</summary>

## Next improvements

Prioritize integrity, recovery and understandable status:

- Broader device/codec/reader coverage; first-frame and seek measurements.
- Empty-directory uploads and clearer emergency restore guidance.
- Broader real-device coverage for shared-owner sessions and simultaneous changes.
- Consider independent export, payload encryption and real modification timestamps.

## Outside the current scope

Multi-user accounts, teams, tenant permissions and clustered workers.

</details>
