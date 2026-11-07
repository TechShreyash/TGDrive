# Status and roadmap

[Docs home](README.md) · [Architecture](architecture.md) · [Development](development.md)

TGDrive is a single-owner, self-hosted drive. This page separates available
features from remaining validation and known limits.

[See the available interface and workflows](showcase.md); the recordings are
illustrative and do not certify the validation still listed below.

## What works now

| Available | Learn more |
| --- | --- |
| Resumable file/folder uploads, automatic splitting, Trash and transfer history | [Uploads](usage.md#upload-files-or-folders) · [Transfers](usage.md#transfer-history) |
| Direct URLs, torrent/magnet imports with nested folders, and Telegram bot uploads | [URLs](usage.md#import-from-a-url) · [Torrents](usage.md#import-a-torrent-or-magnet-link) · [Bot](usage.md#upload-through-your-telegram-bot) |
| Background ZIP creation and ZIP/7z/RAR/TAR/compressed-file extraction | [Archives](usage.md#create-zips-and-extract-archives) |
| Video Continue with browser-local positions, track/subtitle menus, chapters and seek previews | [Player](usage.md#open-preview-and-download) |
| Photos, PDFs, text, EPUB ebooks and CBZ/CBR comics | [Previews](usage.md#open-preview-and-download) · [Readers](reading.md) |
| Scoped read-only share links and embeddable file/folder viewers | [Sharing](usage.md#share-and-revoke) · [Embeds](usage.md#embed-a-file-or-folder-in-a-website) |
| Preloads, monitoring and multiple Telegram accounts | [Performance](performance.md) · [Configuration](configuration.md) |
| MongoDB persistence, encrypted backups derived from `ENCRYPTION_KEY`, database restore and optional independent-channel recovery | [Backup & recovery](backup-recovery.md) |
| Encrypted new uploads with legacy plaintext reads | [Encryption coverage](reference.md#stored-file-encryption) |
| Indexed folder cursor pages and server-side sort/type filters | [Browse](usage.md#browse-and-organize) · [API](api.md#listing-and-metadata) |

Available features still have [configured limits](configuration.md) and are not
certified for every device or network.

## Before public release

Use the [release procedure](release.md) against the exact commit (and extracted
archive if offering custom source assets). Publication is a separate maintainer
action. Remaining live validation:

- [ ] Fresh laptop and hosted installs, persistent volumes, stop/start and recovery.
- [ ] Live recovery: empty MongoDB one-click restore, replacement main channel, revoked bot access, wrong `ENCRYPTION_KEY` and interrupted activation.
- [ ] Historical-image/dependency and live recovery coverage for tagged v2.2
  archives followed by schema-4 upgrade; retain rollback material until a new
  restore point verifies.
- [ ] Backup pin denial, permission repair and restart; confirm that the intended
  restore point is discoverable with MongoDB gone and explicit owner credentials.
- [ ] Live encryption: streaming/seeking encrypted parts, bot-mode encrypted import, and the encryption toggle.
- [ ] Coolify DNS/TLS, trusted forwarding and isolation of the direct app port.
- [ ] Prolonged multi-account outages and interrupted Telegram sends.
- [ ] Actual Premium speed, broader devices/codecs, ARM and hosting platforms.
- [ ] Verify published image architectures/source revision and the private
  vulnerability-reporting entry point before announcing a release.

## Known correctness and recovery gaps

| Area | Current limit |
| --- | --- |
| Authentication | Password-only owner login; no MFA/passkeys, idle session timeout or fresh reauthentication for sensitive settings/backup downloads. Login throttling is process-local. |
| Credentials | Removing or replacing an account waits for pending transfers that may need it. |
| Plaintext bot copies | Encryption-off copies have no automatic durable reconciliation after an uncertain catalog commit; check the drive before resending. Encrypted imports use the upload job pipeline. |
| History | Compaction must preserve unresolved effects and retry identities. |
| Large folders and search | Cursor pages avoid deep offset scans, but counts still cost work. Search ranks a bounded candidate set, not every match in an arbitrarily large drive. |
| Large-catalog recovery | Current V2 uses disk staging and bounded records. Complete-pipeline and accepted/rejected legacy memory validation remains provisional; transport, record/complexity, disk and database limits still apply. See [recovery limits](backup-recovery.md#how-backups-work). |
| Encryption | Legacy/plaintext copies stay readable without payload decryption; partial ranges have no authenticated block tag. Local plaintext staging and the trusted host remain outside payload encryption. |
| Direct API clients | Some request mappings are process-local and error formats vary; follow the [documented API contracts](api.md). |
| Storage channel changes | Require verified channel recovery; editing an ID does not move files. |
| Intentional fresh starts | In-place reset is temporarily unavailable. Use a fresh dedicated database/channel and preserve the old drive; see [the procedure](backup-recovery.md#start-a-new-drive). |

See [Troubleshooting](operations.md#troubleshooting) for current recovery actions.

<details>
<summary>Next improvements and scope</summary>

## Next improvements

Prioritize integrity, recovery and understandable status:

- Broader device/codec/reader coverage; first-frame and seek measurements.
- Empty-directory uploads and clearer emergency restore guidance.
- Broader real-device coverage for shared-owner sessions and simultaneous changes.
- Consider an independent media export and real modification timestamps when a
  concrete recovery or browsing need justifies them.

## Outside the current scope

Multi-user accounts, teams, tenant permissions and clustered workers.

</details>
