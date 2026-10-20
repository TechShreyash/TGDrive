# TGDrive

**Your files in Telegram. Your drive in the browser.**

TGDrive is a self-hosted personal file manager backed by Telegram and MongoDB.
Upload files and folders, stream videos, preview documents, and share revocable
links from one responsive web interface.

[Get started](docs/setup.md) · [User guide](docs/usage.md) ·
[Performance and caching](docs/performance.md) · [Documentation](docs/README.md)

## What you can do

| Feature | How it works |
| --- | --- |
| **Files and folders** | Upload from your device, drag and drop, or import a public HTTP(S) URL. Search names, move, rename, sort, and restore from Trash. |
| **Large files** | Split across Telegram documents while retaining one drive item, exact original downloads, and video byte-range seeking. Premium is optional. |
| **Media playback** | Locally bundled enhanced video player with supported embedded audio/subtitle tracks, chapter navigation, keyboard controls, and buffered timeline feedback. |
| **Document previews** | Images, PDFs up to 256 MiB with lazy byte ranges, and read-only text/code previews. Original downloads remain available. |
| **Cache and preload** | Reuse recently streamed blocks, prepare a file before sharing, and limit RAM, disk, and background work. Inspect ready bytes and evict cached data. |
| **Transfers** | Resumable device uploads, server-side URL jobs, batch progress, cancellation, retries, and durable history. Floating upload receipts stay scoped to the initiating browser. |
| **Sharing** | Scoped file/folder links with expiry, re-copy, and revocation. Review access in Shared Links. |
| **Recovery** | MongoDB stores the catalog and settings; encrypted database snapshots are saved separately to Telegram. |

## Start with Docker

You need Linux or WSL Ubuntu, Docker with Compose/Buildx, a MongoDB replica set
(Atlas works), and a bot with administrator access to a private Telegram channel.
The host needs Python 3.10 or newer for the guided setup script.

```bash
git clone --branch dev https://github.com/TechShreyash/TGDrive.git
cd TGDrive
python3 scripts/setup.py
```

The wizard checks access, creates private configuration, and starts the app.
It preserves an existing `.env` and asks explicitly before initializing a new
drive. [The setup guide](docs/setup.md) covers Telegram credentials, MongoDB,
source installations, and server hosting.

For **Coolify**, select `/compose.yaml` from the `dev` branch and follow the
[hosted setup](docs/setup.md#coolify). Keep credentials as private runtime values.

## How files move

```text
Your device or a file URL
          ↓
TGDrive server → Telegram documents
          ↓
       MongoDB catalog

Authorized download → RAM/disk cache → Telegram for missing blocks
```

Telegram holds file contents; MongoDB remembers their names, folders, shares,
and transfer state. Local disk holds unfinished uploads and disposable cached
bytes. Keep **one installation and one worker per drive**.

Files above a Telegram document's limit are reconstructed transparently.
The default logical-file limit is **16 GiB**, subject to staging capacity and
free disk; it can be configured. This is not unlimited server storage.

## Make streaming and shared downloads smoother

The download path shares in-flight reads, reads blocks concurrently through
available Telegram accounts, and caches verified bytes for reuse. Demand reads
take priority over background preloading. Frequently reused content gets
preference over a one-time scan.

Use **Monitor → Performance** for traffic graphs and container resources, and
**Monitor → Preloads** for cached media. See [Backup & recovery](docs/backup-recovery.md)
for the recovery key and restore procedure.

Use **Configuration → Cache settings** and **Transfer limits** to tune
cache and transport limits, and inspect current cache
coverage before relying on a prepared file. Cache entries remain evictable;
restarting does not turn local cache into a separate backup.

Uploading uses bounded MTProto chunk pipelining through **Telethon with cryptg**.
Optional smaller file splits can engage more account slots. More bots or higher
concurrency cannot guarantee higher speed: server bandwidth, Telegram routing,
datacenters, cooldowns, and the viewer's connection all matter.

[Performance guide](docs/performance.md) explains settings, queue estimates,
cache budgets, and a practical bottleneck checklist.

## Keep your drive recoverable

- Save the MongoDB access details and the recovery key separately.
- Wait for **Completed** before discarding an upload's original or staged bytes.
- Reinstalls reopen the same MongoDB database; do not initialize it again.
- Encrypted database backups restore records and settings, not deleted Telegram
  files or missing unfinished upload contents.
- Share links grant access to anyone holding them. Revoke links you no longer need.

[Operations and recovery](docs/operations.md) ·
[Security boundaries](docs/reference.md#security-and-privacy)

## Project status

TGDrive is a **public beta on `dev`**. Keep independent copies of important files.
Automated tests cover transfer recovery, byte ranges, caching, access control,
and browser workflows; live performance still depends on your deployment.

[Known limits and roadmap](docs/roadmap.md) ·
[Development and verification](docs/development.md) · [MIT License](LICENSE)

[Project updates](https://t.me/TechZBots) ·
[Support](https://t.me/TechZBots_Support)
