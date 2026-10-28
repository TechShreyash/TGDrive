# Status and roadmap

[Docs home](README.md) · [Validation evidence](development.md#ci-and-release-evidence)

TGDrive is a single-owner, self-hosted drive. This page separates available
features from remaining validation and known limits.

[See the available interface and workflows](showcase.md); the recordings are
illustrative and do not certify the validation still listed below.

## What works now

| Available | Learn more |
| --- | --- |
| Resumable file/folder uploads, automatic splitting, Trash and transfer history | [Uploads](usage.md#upload-files-or-folders) · [Transfers](usage.md#transfer-history) |
| Direct URLs, torrent/magnet imports with nested folders, and Telegram bot uploads | [URLs](usage.md#import-from-a-url) · [Torrents](usage.md#import-a-torrent-or-magnet-link) · [Bot](usage.md#optional-telegram-bot-interface) |
| Background ZIP creation and ZIP/7z/RAR/TAR/compressed-file extraction | [Archives](usage.md#create-zips-and-extract-archives) |
| Video Continue with browser-local positions, track/subtitle menus, chapters and seek previews | [Player](usage.md#open-preview-and-download) |
| Photos, PDFs, text, EPUB ebooks and CBZ/CBR comics | [Previews](usage.md#open-preview-and-download) · [Readers](reading.md) |
| Scoped read-only share links and embeddable file/folder viewers | [Sharing](usage.md#share-and-revoke) · [Embeds](usage.md#embed-a-file-or-folder-in-a-website) |
| Preloads, monitoring and multiple Telegram accounts | [Performance](performance.md) · [Configuration](configuration.md) |
| MongoDB persistence, one recovery file/key pair, database restore and optional independent-channel recovery | [Backup & recovery](backup-recovery.md) |

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
| Direct API clients | Some request mappings are process-local and error formats vary; follow the [documented API contracts](reference.md#http-api). |
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
