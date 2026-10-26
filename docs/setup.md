# Set up TGDrive

[Docs home](README.md) · Next: [Using TGDrive](usage.md)

**Read the [new-user guide and FAQ](new-user-guide.md) before storing files.**
It explains the storage model, channel rules, limits and backup responsibilities;
these installation steps do not replace it.

[See the interface before installing](showcase.md), including the file browser,
previews, transfers and settings.

**Use Docker Compose for every TGDrive deployment.** It builds the website and
includes Python, dependencies and preview tools, with the same runtime on laptops
and servers. No host Python or Node installation is needed for the main steps.

For a **new v2 drive**, use an empty MongoDB database and a new private Telegram
channel. v1 data and older catalog formats are not imported or upgraded.

[Prerequisites](#before-you-start) · [MongoDB](#1-prepare-mongodb) · [Telegram](#2-prepare-telegram) · [Configuration](#3-configure-your-drive) · [Start locally](#4-start-locally) · [Hosting](#hosting-beyond-your-machine)

## Choose your installation

| Use | Recommended setup |
| --- | --- |
| **Laptop, only when needed** | Docker Compose on Windows, macOS or Linux; follow steps 1–5 below. |
| **Your own server** | [Self-hosted Coolify](#coolify), a free tool for deployment management. |
| **Server with your own HTTPS proxy** | [Server Compose](#docker-compose-on-a-server). |
| **Prefer help running it** | [Optional managed hosting](hosting.md#optional-managed-hosting), including the server, deployment and management. |

Local Compose uses a private localhost address and runs only when you start
it. TGDrive needs internet access to Telegram and MongoDB. A sleeping or stopped
laptop cannot serve transfers, backups, bot requests or shared links. Keep source
files until uploads say **Completed**.

## Before you start

Install a maintained Docker release with **Compose 2.24+ and Buildx**, following the
official guide for your system:

| System | Recommended installation | Terminal for this guide |
| --- | --- | --- |
| Windows | [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) with its default WSL 2 backend; use **Linux containers**. | PowerShell |
| macOS | [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) for your Apple Silicon or Intel Mac. | Terminal |
| Linux laptop or server | [Docker Engine](https://docs.docker.com/engine/install/) from Docker's repository, with the [Compose plugin](https://docs.docker.com/compose/install/linux/) and [Buildx](https://docs.docker.com/build/concepts/overview/). | Your usual terminal |

Start Docker Desktop, or the Linux Docker daemon. These commands must work before
continuing:

~~~bash
docker info
docker compose version
docker buildx version
~~~

Use `docker compose` (with a space). Docker Desktop includes Compose and Buildx;
Linux users should install them using the official repository instructions.
You do not need to open an Ubuntu terminal on Windows for the main steps.
For Linux permission errors, follow Docker's [post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/).

### Get the source

The current v2 development source is on the
[`dev` branch](https://github.com/TechShreyash/TGDrive/tree/dev). To try it:

1. Download the [development ZIP](https://github.com/TechShreyash/TGDrive/archive/refs/heads/dev.zip).
2. Extract it with your file manager and open the `TGDrive-dev` folder.
3. Open a terminal in that folder. Run the commands below as your normal user.

This is development code, not a published release. Start with disposable files
and review the [remaining release checks](roadmap.md#before-public-release).
Leave disk space for unfinished uploads and cache.

When a v2 release is published on
[GitHub Releases](https://github.com/TechShreyash/TGDrive/releases), prefer its
custom source ZIP and matching checksum. Follow
[verify and extract](release.md#verify-a-downloaded-source-archive), then run the
same setup steps from the extracted folder.

If you choose to use a WSL terminal, enable Docker Desktop's
[WSL integration](https://docs.docker.com/desktop/features/wsl/) and keep the
project in Ubuntu's Linux filesystem. Use that same terminal/folder consistently.

## 1. Prepare MongoDB

[MongoDB Atlas](https://www.mongodb.com/docs/atlas/tutorial/deploy-free-tier-cluster/)
is the simplest option. Self-hosted MongoDB must be a **replica set** for
transactions; a standalone container is insufficient. TGDrive's Compose files
do not supply MongoDB.

1. Create an Atlas cluster and a **database user** with read/write access.
2. In **Network Access**, allow the public IP of the TGDrive machine.
3. Open **Connect → Drivers**, copy the URI and replace its password placeholder.
   [Encode reserved password characters](https://www.mongodb.com/docs/manual/reference/connection-string/).

Keep the URI private. Use database name `tgdrive`, or another dedicated database.
Update the IP allowlist if your laptop changes networks; avoid allowing every IP.
MongoDB holds catalog/settings; file contents live in Telegram.

## 2. Prepare Telegram

1. Open [BotFather](https://t.me/BotFather), create a bot and keep its **bot token**.
2. Open [Telegram's API page](https://my.telegram.org/apps), create an application
   and keep its **API ID** and **API hash**.
3. Create a dedicated **private broadcast channel**, not a group or a public
   channel. Give it no public username and turn message auto-delete **Off**.
4. Add **every storage bot** as an administrator. For the standard MongoDB drive,
   enable **Post Messages** and **Delete Messages**.
5. Send one identification message, then use **Copy Message Link**. For a link
   like `https://t.me/c/1234567890/1`, the channel ID is `-1001234567890`:
   prepend `-100` to the number after `/c/`. Use your own channel's ID.

> [!IMPORTANT]
> Treat the channel as app-managed storage. Make file changes in TGDrive;
> do not edit/delete its messages, clear history, change recovery pins or add
> moderation bots. Keep every enabled account's channel access/permissions.
> A database backup cannot recover file bytes deleted from Telegram.

**Post Messages** stores files/backups; **Delete Messages** cleans unfinished
uploads. **Edit Messages** is only needed for independent backup recovery pins.
Leave unrelated rights off. Reading needs channel access, not a separate
“Read Messages” administrator switch. Setup and **Connections → Refresh** check
access; see [channel damage recovery](operations.md#if-someone-changed-the-telegram-channel).

## 3. Configure your drive

Copy the template once. **Windows PowerShell**:

~~~powershell
Copy-Item .env.example .env
~~~

**Linux, WSL or macOS terminal**:

~~~bash
cp .env.example .env
chmod 600 .env
~~~

Open `.env` in a text editor. Replace these placeholders; do not leave angle
brackets or the example additional-bot text in your values:

| Setting | Value |
| --- | --- |
| `MONGODB_URI` | Your private Atlas/replica-set connection URI |
| `API_ID` / `API_HASH` | From Telegram's API page |
| `BOT_TOKENS` | Your storage bot token; start with just one |
| `STORAGE_CHANNEL` | Your private channel's negative numeric ID |
| `ADMIN_PASSWORD` | A unique web password; 15+ characters recommended, 12-character minimum |

The defaults use database `tgdrive` and allow local HTTP cookies. Session and
backup keys are generated once and preserved in persistent storage; leave
`SESSION_SECRET` and `MONGODB_BACKUP_KEY` unset for a new installation. Never replace
an existing drive's keys. Save the exported recovery key after your first login.

Keep `.env` private. Single-quote values containing `$`, including password
hashes. Do not shell-source the file. For hashed passwords, explicit keys and all
optional settings, see the [configuration reference](reference.md#configuration).

## 4. Start locally

Run one installation and one worker per drive. From the project folder with your
configured `.env`, build using these same commands on Windows, macOS and Linux:

~~~bash
docker compose config --quiet
docker compose build
~~~

Start TGDrive:

~~~bash
docker compose up -d
docker compose ps
~~~

TGDrive connects to MongoDB before becoming available. A new, empty database is
initialized automatically; an existing drive is reopened. Missing or invalid
`MONGODB_URI`, failed authentication, an unreachable database or an unsupported
MongoDB deployment stops startup. There is no SQLite fallback. Correct the
environment or database access and start again. Existing or incomplete drive
state is preserved for explicit recovery; it is never replaced with an empty drive.

The first build can take several minutes. `config --quiet` checks syntax, not
credentials. Plain `docker compose config` prints secrets; keep its output private.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and follow
[first login](#5-open-your-drive). Closing the terminal keeps the containers
running; Docker Desktop/the daemon must remain running too.

| Later action | Command |
| --- | --- |
| Start for this session | `docker compose up -d` |
| See whether it is ready | `docker compose ps` |
| Finish using it | `docker compose stop` |
| Restart after an interruption | `docker compose restart app` |
| Apply edited .env values | `docker compose up -d --force-recreate` |

Local containers do not restart automatically by default. Let Stop finish its
graceful shutdown (up to 60 seconds). To opt into restart after Docker starts,
set `TGDRIVE_RESTART_POLICY=unless-stopped` in `.env` and recreate.
You can quit Docker Desktop after stopping TGDrive; keep its saved volumes.

> [!WARNING]
> Keep `.env`, both named volumes and the same Compose project name.
> Do not use `docker compose down -v` or volume prune as a repair:
> they remove keys, unfinished bytes and recovery journals.
> Compose stores data inside its named volumes, not host `data/` and `cache/` folders.

## 5. Open your drive

1. Sign in at [http://127.0.0.1:8000](http://127.0.0.1:8000) with your web password.
2. Open **Connections → Refresh** and check your bot.
3. Open **Storage & Backups → Save recovery key** and store the downloaded key
   somewhere separate from this installation.

Before importing your library:

- [ ] Upload and download a small test file.
- [ ] Stop/start TGDrive and check the file still opens.
- [ ] Choose **Back up now** and confirm an archive.

Then [upload your files](usage.md#upload-files-or-folders).

Before a large import, review [storage budgets](configuration.md#upload-size-and-available-disk-space):
defaults are **16 GiB upload staging, 10 GiB disk cache and 256 MiB RAM cache**.
These are ceilings, not preallocated space; leave room for the system and backups.

Interrupted uploads may need their original files. A restart resumes recoverable
server work; deleted Telegram bytes or missing credentials require further action.
See [interruption recovery](operations.md#after-a-shutdown-crash-or-network-loss).

## Apply updates to an existing drive

Keep the database, channel, environment and persistent storage. Startup reopens
the existing drive automatically.

| Installation | Update |
| --- | --- |
| Local Docker | Follow [archive/Git update steps](operations.md#upgrade-and-rollback), preserving `.env` and project name; run `docker compose up -d --build`. |
| Coolify | Redeploy the updated revision with the existing Compose configuration. |
| Server Compose | Use the same `-f compose.yaml -f compose.server.yaml` files; run `up -d --build`. |

Refresh the browser and reopen interrupted playback. Saved settings and
persistent disk cache remain; RAM cache repopulates after restart.
See [upgrade/rollback](operations.md#upgrade-and-rollback) for hosted installations.

## If setup stops

| Problem | What to check |
| --- | --- |
| Docker command fails | Start Docker Desktop/the daemon; on Linux check your user's Docker access |
| Website says it needs a build | Rebuild with `docker compose up -d --build`; Docker builds the website for you |
| Database connection fails | URI password, Atlas Network Access and cluster availability |
| Bot cannot use the channel | Correct bot token, private broadcast channel, auto-delete off and every bot's administrator permissions |
| Existing drive is missing | Check the database name; do not initialize a replacement |
| Address already in use | Set a free `PORT`, such as `8001`, in `.env`, recreate the local container and open that localhost port |
| Login works only until the page reloads | Local HTTP needs `COOKIE_SECURE=false`; recreate after editing. Hosted access needs HTTPS with secure cookies |
| A setting edit has no effect | Recreate the container; saved dashboard preferences override matching startup defaults |

More help: [Troubleshooting](operations.md#troubleshooting).

## Reinstall or move to another machine

Follow [reopen your existing drive](operations.md#reopen-after-losing-the-local-installation).
Use the same MongoDB database; **do not initialize it again**.

## Additional bots and Premium accounts

Use **Management → Connections** after setup. Every account needs the same
channel; saved connection changes require a restart.
[Finish pending transfers before changing accounts](usage.md#settings-and-connections).

Premium is optional: all accounts use the same **2000 MiB** document cap and
[automatic splitting](usage.md#files-above-2-gb). Available Premium clients are
preferred for transfer speed, but neither Premium nor extra bots guarantee
higher throughput or remove Telegram cooldowns. A live Premium speedup has not
been measured. Use **Connections → Connect Premium account** to sign in, or
import an already authorized session through its advanced controls.

## Hosting beyond your machine

Use [Coolify](#coolify) to manage deployment on your server, or
[server Compose](#docker-compose-on-a-server) with an HTTPS proxy you manage.
The [hosting guide](hosting.md) compares local and server use, explains optional
managed hosting and server plans, and lists the accounts you need to prepare.

Preserve the database, private configuration and persistent volumes. If you
change hosts, follow [move or reopen your drive](operations.md#when-a-hosting-plan-expires).

### Coolify

Coolify is recommended for **managing deployment on a server you provide**.
Its [self-hosted edition is free](https://coolify.io/docs/usage); you pay for your
own server/infrastructure. A paid Coolify Cloud subscription is optional.

You need a Docker server, domain, reachable MongoDB replica set and Telegram
setup above. [Install Coolify](https://coolify.io/docs/get-started/installation)
if needed. Keep **one replica**; disable preview/parallel deployments against
the production database/channel.

Create a public Git application from `https://github.com/TechShreyash/TGDrive`.
Select **`dev`** for the development source described in this guide; for a
published v2 release, select its exact tag or commit instead. Choose **Docker
Compose**, base directory `/`, file **`/compose.yaml`**. Set these runtime variables:

| Variable | Value |
| --- | --- |
| `MONGODB_URI` | Your private Atlas/replica-set connection URI |
| `API_ID` | Telegram application ID |
| `API_HASH` | Telegram application hash |
| `BOT_TOKENS` | Your storage bot token |
| `STORAGE_CHANNEL` | The private channel's negative numeric ID |
| `ADMIN_PASSWORD` | Owner password, at least 12 characters |

Choose **Literal** for values containing `$`. Keep secrets **unavailable during
build**, **available at runtime**, then save. [Environment guide](https://coolify.io/docs/applications/configuration/environment-variables).

Optional settings use TGDrive defaults. Add a runtime variable only when an
override is needed; the stack's `env_file` passes it to the application.

1. Route `app` to internal port **8000** with HTTPS, for example
   `https://drive.example.com:8000` in Coolify. Visitors use normal HTTPS;
   no host port mapping is needed.
2. Point your domain's DNS at the server. Shared `sslip.io` preview domains can
   hit certificate rate limits. Keep `COOKIE_SECURE=true` and app/database ports private.
3. Preserve **`/app/data` and `/app/cache`** on every redeploy: keys/journals
   and unfinished uploads live there. [Storage guide](https://coolify.io/docs/core/persistent-storage/storage-mounts/overview).
4. Deploy. TGDrive checks MongoDB and initializes a new, empty database
   automatically before serving the website. If database configuration, access
   or transaction support fails, correct it and redeploy; startup fails rather
   than switching to local storage.
5. Sign in and save **Storage & Backups → Save recovery key** separately.
   Existing drives skip initialization and use their original owner password.

Compose creates the volumes; session/backup keys are generated once in `/app/data`
and saved in MongoDB during initialization. Existing drives reuse saved keys/hash.
Proxy trust defaults to `auto`, resolving `coolify-proxy` privately.
Coolify selects `compose.yaml` explicitly; local `docker compose` additionally
loads `compose.override.yaml` for localhost access.
[Coolify Compose guide](https://coolify.io/docs/applications/builds/docker-compose).

#### Keep a deployed v2 drive safe

Keep the same database, channel, credentials and both mounts. Initialize only once.
For machine loss, follow [recovery](operations.md#reopen-after-losing-the-local-installation).
Only override `FORWARDED_ALLOW_IPS` when you know the actual proxy sources.

### Docker Compose on a server

1. Prepare MongoDB/Telegram and private `.env` as in
   [configuration](#3-configure-your-drive). Keep app ingress private.
2. Name **both files explicitly** to avoid the laptop override:

~~~bash
docker compose -f compose.yaml -f compose.server.yaml config --quiet
docker compose -f compose.yaml -f compose.server.yaml build
~~~

3. Start the server; a new, empty MongoDB database is initialized automatically:

~~~bash
docker compose -f compose.yaml -f compose.server.yaml up -d
docker compose -f compose.yaml -f compose.server.yaml ps
~~~

4. Forward HTTPS to host `127.0.0.1:8000`, preserving Host and setting forwarded
   scheme/client headers. A containerized proxy needs a shared private network
   with `app:8000`, or its documented host-access route.
5. Trust only proxy source addresses the app sees through `FORWARDED_ALLOW_IPS`;
   never use `*` as a workaround. Follow
   [HTTPS/proxy checks](operations.md#https-and-private-configuration).
6. Verify the public domain, Connections and a small upload/download.
   Save the recovery key separately and confirm a database backup.

The server file keeps secure cookies on, publishes loopback only and defaults
to `unless-stopped` restart. Keep project name/volumes and use the same
`-f compose.yaml -f compose.server.yaml` arguments for `stop`, `restart app`
and `up -d --build`. Health checks alone do not validate DNS, TLS or files.
See [upgrade/rollback](operations.md#upgrade-and-rollback).

### Other hosting

Use Docker/Compose with persistent storage and one worker per drive. Platforms
that sleep, replace local disks or impose short request/upload limits can break
long transfers unless configured appropriately.

## Optional features and settings

Configure optional features after the first upload/download works:

| Feature | Configure |
| --- | --- |
| More bots / Premium | [Connections](usage.md#settings-and-connections) |
| Telegram bot uploads | [Bot interface](usage.md#settings-and-connections): `MAIN_BOT_TOKEN`, `TELEGRAM_ADMIN_IDS`; optional `PUBLIC_URL` |
| Shares | [Shared Links](usage.md#share-and-revoke); recipients need a reachable website |
| Independent channel copy | [Setup below](#optional-independent-channel-protection) |
| Cache, preloads, upload limits, timezone | [Configuration](configuration.md) |
| Host a public demo with settings locked | [Public demo mode](configuration.md#public-demo-mode) |
| Backup interval, URL policy, sessions, proxy trust | [Deployment variables](reference.md#configuration) |

## Optional independent channel protection

After the drive works, add a separate private channel and backup bot through
**Storage & Backups → Independent backup channel**. Save its recovery key outside
Telegram and wait for a confirmed restore point.
Follow [backup-channel setup](backup-recovery.md#set-up-an-independent-backup-channel)
for permissions, copying, recovery and same-provider limits.
