# Set up TGDrive

[Docs home](README.md) · Next: [Using TGDrive](usage.md)

**Must read before using TGDrive: [New-user guide and FAQs](new-user-guide.md).**
It explains the storage model, channel rules, limits and backup responsibilities;
these installation steps do not replace it.

[See the interface before installing](showcase.md), including the file browser,
previews, transfers and settings.

**Use Docker Compose for every TGDrive deployment.** It builds the website and
includes Python, dependencies and preview tools, with the same runtime on laptops
and servers. TGDrive supports **Python 3.14.8 only**; the image supplies that exact
version. No host Python or Node installation is needed for the main steps.
The image also includes `aria2c` for [torrent imports](usage.md#import-a-torrent-or-magnet-link)
and libarchive for [archive extraction](usage.md#create-zips-and-extract-archives).

For a **new v2 drive**, use an empty MongoDB database and a new private Telegram
channel. v1 data and older catalog formats are not imported or upgraded.

**Contents**

- [Choose your installation](#choose-your-installation)
- [Before you start](#before-you-start)
  - [Get the source](#get-the-source)
- [1. Prepare MongoDB](#1-prepare-mongodb)
- [2. Prepare Telegram](#2-prepare-telegram)
- [3. Configure your drive](#3-configure-your-drive)
  - [Find your numeric Telegram user ID](#find-your-telegram-user-id)
- [4. Start locally](#4-start-locally)
- [5. Open your drive](#5-open-your-drive)
- [Apply updates to an existing drive](#apply-updates-to-an-existing-drive)
- [If setup stops](#if-setup-stops)
- [Reinstall or move to another machine](#reinstall-or-move-to-another-machine)
- [Additional bots and Premium accounts](#additional-bots-and-premium-accounts)
- [Hosting beyond your machine](#hosting-beyond-your-machine)
  - [Coolify](#coolify)
  - [Docker Compose on a server](#docker-compose-on-a-server)
  - [Other hosting](#other-hosting)
- [Features and settings after setup](#features-and-settings-after-setup)
- [Optional independent channel protection](#optional-independent-channel-protection)

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

The [`main` branch](https://github.com/TechShreyash/TGDrive/tree/main) receives
reviewed, tested stable updates. The [`dev` branch](https://github.com/TechShreyash/TGDrive/tree/dev)
is ongoing development and may be unstable; do not use it for a normal installation.
See [release verification](release.md) for the required checks and their limits.

With Git installed, open a terminal and clone the stable branch:

~~~bash
git clone --branch main https://github.com/TechShreyash/TGDrive.git
cd TGDrive
~~~

Run the setup commands below from that folder as your normal user. `main`
advances with reviewed versioned releases; version tags stay fixed. To pin
**2.0.0**, use this clone command instead, once the
[v2.0.0 tag](https://github.com/TechShreyash/TGDrive/tree/v2.0.0) is published:

~~~bash
git clone --branch v2.0.0 --depth 1 https://github.com/TechShreyash/TGDrive.git
cd TGDrive
~~~

Without Git, download the [stable source ZIP](https://github.com/TechShreyash/TGDrive/archive/refs/heads/main.zip),
extract it and open a terminal in `TGDrive-main`. After `v2.0.0` is published,
its [tagged source ZIP](https://github.com/TechShreyash/TGDrive/archive/refs/tags/v2.0.0.zip)
provides the same fixed-version option. A custom source ZIP with a matching
checksum may also be offered on the [release page](https://github.com/TechShreyash/TGDrive/releases/tag/v2.0.0);
follow [verify and extract](release.md#verify-a-downloaded-source-archive) for those
optional assets. Merging to `main` does not publish a tag or release assets.
Leave disk space for unfinished uploads and cache.

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

1. Open [BotFather](https://t.me/BotFather), create your main bot and keep its
   **bot token**. It will store files and accept uploads in a private chat.
2. Open [Telegram's API page](https://my.telegram.org/apps), create an application
   and keep its **API ID** and **API hash**.
3. Create a dedicated **private broadcast channel**, not a group or a public
   channel. Give it no public username and turn message auto-delete **Off**.
4. Add this bot as a channel administrator with **Post Messages** and
   **Delete Messages** enabled.
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
brackets in your values:

| Setting | Value |
| --- | --- |
| `MONGODB_URI` | Your private Atlas/replica-set connection URI |
| `API_ID` / `API_HASH` | From Telegram's API page |
| `MAIN_BOT_TOKEN` | Your main bot's token from BotFather |
| `TELEGRAM_ADMIN_IDS` | Your numeric Telegram user ID, not your `@username`; separate multiple allowed users with commas. [Find your ID](#find-your-telegram-user-id) |
| `STORAGE_CHANNEL` | Your private channel's negative numeric ID |
| `ADMIN_PASSWORD` | A unique web password; 15+ characters recommended, 12-character minimum |

<a name="find-your-telegram-user-id"></a>

<details>
<summary>Find your numeric Telegram user ID</summary>

Before starting TGDrive for the first time:

1. Send `/start` in a private chat with your new bot. It does not need to reply.
2. Open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in your browser,
   replacing `<YOUR_BOT_TOKEN>` with the token from BotFather.
3. In `result`, find your `/start` message and copy **`message.from.id`** into
   `TELEGRAM_ADMIN_IDS`. Use the sender's user ID, not the bot or channel ID.
   For multiple allowed users, have each send a private message and separate
   their IDs with commas.

This uses [Telegram's own Bot API](https://core.telegram.org/bots/api#getupdates).
Keep the token-bearing URL and response private. If `result` is empty, send a
fresh message and refresh; use a new bot that is not already running elsewhere.

</details>

The main bot starts as your first storage connection. Only the Telegram users
listed in `TELEGRAM_ADMIN_IDS` can send it files. Add further storage bots through
[Connections](#additional-bots-and-premium-accounts) after setup.

The defaults use database `tgdrive` and allow local HTTP cookies. Session and
backup keys are generated once and preserved in persistent storage; leave
`SESSION_SECRET` and `MONGODB_BACKUP_KEY` unset for a new installation. Never replace
an existing drive's keys. Save your recovery file and separate key after the
first confirmed backup.

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
3. Open **Storage & Backups → Backups & recovery → Save recovery key** and store the downloaded key
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
the existing drive automatically. Choose a published release tag, or stable
`main` source, using [the source guidance above](#get-the-source); do not follow `dev`.

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

Add extra storage bots through **Management → Connections → Add storage bot**,
rather than editing the deployment's token list. First add each bot to the same
private channel as an administrator with **Post Messages** and **Delete Messages**.
More healthy bots can handle more Telegram work at once, subject to server and
Telegram limits.

[Finish pending transfers before changing accounts](usage.md#settings-and-connections),
then save your connection changes and restart TGDrive.

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
Its self-hosted edition is free; you pay for your own server/infrastructure.
[Install Coolify](https://coolify.io/docs/start-with-self-hosted) if needed.

You need a Docker server, domain, reachable MongoDB replica set and Telegram
setup above.

Coolify's generated `sslip.io` URL uses HTTP. For TGDrive, use a domain with a
valid HTTPS certificate: the browser app needs a secure context away from
localhost. See [HTTP support and certificate limits](operations.md#can-tgdrive-run-over-http).

Create a public Git application from `https://github.com/TechShreyash/TGDrive`.
Select **`main`** for stable source updates. Prefer **`v2.0.0`** once that release
is published to pin this installation; do not deploy `dev`. Under **General →
Build pipeline**, select **Compose** as the build strategy. Set **Base Directory**
to `/` and **Docker Compose Location** to `/compose.yaml`, then save.
In **Environment Variables**, enter:

| Variable | Value |
| --- | --- |
| `MONGODB_URI` | Your private Atlas/replica-set connection URI |
| `API_ID` | Telegram application ID |
| `API_HASH` | Telegram application hash |
| `MAIN_BOT_TOKEN` | Your main bot's token from BotFather |
| `TELEGRAM_ADMIN_IDS` | Your numeric Telegram user ID; separate multiple allowed users with commas. [Find your ID](#find-your-telegram-user-id) |
| `STORAGE_CHANNEL` | The private channel's negative numeric ID |
| `ADMIN_PASSWORD` | Owner password, at least 12 characters |

Choose **Literal** for values containing `$`. Keep secrets **unavailable during
build**, **available at runtime**, then save. [Environment guide](https://coolify.io/docs/applications/configuration/environment-variables).

1. Point your domain's DNS at the server.
2. Under **Domains**, add an HTTPS domain for the **`app`** service, including
   its internal port: `https://drive.example.com:8000`. Visitors use normal HTTPS.
3. **Deploy** and wait for `app` to become healthy. If deployment fails, inspect
   the deployment and application logs.
4. Open your domain, sign in and save **Storage & Backups → Backups & recovery → Save recovery key**
   somewhere separate from the server.

The included Compose file creates persistent volumes at **`/app/data`** and
**`/app/cache`**, and configures secure cookies and proxy access. Keep those volumes
when redeploying: they hold keys, sessions and unfinished uploads as well as cache.
TGDrive initializes an empty database automatically; an existing drive reopens
with its saved settings and owner password.

[Coolify Compose guide](https://coolify.io/docs/applications/builds/docker-compose) ·
[TGDrive configuration](configuration.md) · [Updates and recovery](operations.md)

### Docker Compose on a server

Use a published release tag or the stable `main` source from
[Get the source](#get-the-source), rather than `dev`.

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
   Confirm a backup and save the recovery file and its separate key.

The server file keeps secure cookies on, publishes loopback only and defaults
to `unless-stopped` restart. Keep project name/volumes and use the same
`-f compose.yaml -f compose.server.yaml` arguments for `stop`, `restart app`
and `up -d --build`. Health checks alone do not validate DNS, TLS or files.
See [upgrade/rollback](operations.md#upgrade-and-rollback).

### Other hosting

Use Docker/Compose with persistent storage and one worker per drive. Platforms
that sleep, replace local disks or impose short request/upload limits can break
long transfers unless configured appropriately.

## Features and settings after setup

After the first upload/download works:

| Feature | Configure |
| --- | --- |
| More bots / Premium | [Connections](usage.md#settings-and-connections) |
| Telegram bot uploads | [Choose the upload folder and send files](usage.md#optional-telegram-bot-interface); the main bot and allowed users are configured during setup |
| Shares | [Shared Links](usage.md#share-and-revoke); recipients need a reachable website |
| Independent channel copy | [Setup below](#optional-independent-channel-protection) |
| Cache, preloads, upload limits, timezone | [Configuration](configuration.md) |
| Host a public demo with settings locked | [Public demo mode](configuration.md#public-demo-mode) |
| Backup interval, URL policy, sessions, proxy trust | [Deployment variables](reference.md#configuration) |

## Optional independent channel protection

After the drive works, add a separate private channel and backup bot through
**Storage & Backups → Backups & recovery → Set up backup channel**. Wait for a
confirmed restore point, then save the recovery file and its separate key outside
Telegram.
Follow [backup-channel setup](backup-recovery.md#set-up-an-independent-backup-channel)
for permissions, copying, recovery and same-provider limits.
