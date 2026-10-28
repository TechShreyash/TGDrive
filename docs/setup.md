# Set up TGDrive

[Docs home](README.md) · [New-user essentials](new-user-guide.md) · Next: [Using TGDrive](usage.md)

**On your computer:** follow steps 1–5 below. **On a server:** prepare MongoDB
and Telegram, then use [Coolify](#coolify) or [server Compose](#docker-compose-on-a-server).
Prefer help? See [optional managed hosting](hosting.md#optional-managed-hosting).

Docker Compose supplies Python, the website and preview/import tools. You do not
need to install Python or Node separately. For a **new v2 drive**, use an empty
MongoDB database and a new private Telegram channel; v1 catalogs are not imported.

## Choose your installation

Local TGDrive works while your computer and Docker are running. A sleeping or
stopped computer cannot serve transfers, backups or share links. Use a server
for access while your computer is off. [Compare hosting options](hosting.md#local-computer-or-server).

## Before you start

Install Docker with **Compose 2.24+ and Buildx**, then start it:

| System | Install | Terminal |
| --- | --- | --- |
| Windows | [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/), default WSL 2 backend, **Linux containers** | PowerShell |
| macOS | [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) | Terminal |
| Linux | [Docker Engine](https://docs.docker.com/engine/install/), [Compose plugin](https://docs.docker.com/compose/install/linux/) and [Buildx](https://docs.docker.com/build/concepts/overview/) | Your usual terminal |

These commands must work before continuing:

~~~bash
docker info
docker compose version
docker buildx version
~~~

Use `docker compose` with a space. For Linux permission errors, follow Docker's
[post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/).
Windows users can stay in PowerShell. If you choose WSL, enable
[Docker's WSL integration](https://docs.docker.com/desktop/features/wsl/) and keep
the project in Ubuntu's filesystem; use the same terminal/folder consistently.

### Get the source

With Git installed:

~~~bash
git clone --branch main https://github.com/TechShreyash/TGDrive.git
cd TGDrive
~~~

Run the commands below from this folder as your normal user. **`main` is the
stable branch; `dev` may be unstable.** Without Git, extract the
[stable source ZIP](https://github.com/TechShreyash/TGDrive/archive/refs/heads/main.zip)
and open a terminal in `TGDrive-main`.

<details>
<summary>Pin an installation to a fixed release</summary>

To install [v2.2.0](https://github.com/TechShreyash/TGDrive/releases/tag/v2.2.0),
clone its immutable tag:

~~~bash
git clone --branch v2.2.0 --depth 1 https://github.com/TechShreyash/TGDrive.git
cd TGDrive
~~~

Or use the [tagged source ZIP](https://github.com/TechShreyash/TGDrive/archive/refs/tags/v2.2.0.zip).
For optional custom archives/checksums, see [release verification](release.md#verify-a-downloaded-source-archive).

</details>

## 1. Prepare MongoDB

MongoDB stores your file index and settings; Telegram stores file contents.
[MongoDB Atlas](https://www.mongodb.com/docs/atlas/tutorial/deploy-free-tier-cluster/)
is the simplest option. Self-hosted MongoDB must be a **replica set**, not a
standalone container. TGDrive's Compose files do not include MongoDB.

1. Create an Atlas cluster and a **database user** with read/write access.
2. In **Network Access**, allow your TGDrive machine's public IP.
3. Open **Connect → Drivers**, copy the URI and replace its password placeholder.
   [Encode reserved password characters](https://www.mongodb.com/docs/manual/reference/connection-string/).

Keep the URI private. Use a dedicated database; the default name is `tgdrive`.
Update the IP allowlist when your network changes; avoid allowing every IP.

## 2. Prepare Telegram

1. Create a bot with [BotFather](https://t.me/BotFather) and save its **bot token**.
2. Create an application at [Telegram's API page](https://my.telegram.org/apps)
   and save its **API ID** and **API hash**.
3. Create a dedicated **private broadcast channel**, with no public username and
   message auto-delete **Off**. Do not use a group.
4. Add the bot as a channel administrator with **Post Messages** and **Delete
   Messages**. **Edit Messages** is needed only for independent backup recovery pins.
   Leave unrelated administrator rights off.
5. Send one identification message and use **Copy Message Link**. From a link
   like `https://t.me/c/1234567890/1`, your channel ID is `-1001234567890`:
   prepend `-100` to the number after `/c/`.

> [!IMPORTANT]
> Make file changes through TGDrive. Do not edit/delete storage messages, clear
> channel history, change recovery pins or add moderation bots. Keep enabled
> accounts' channel access. A database backup cannot restore deleted file bytes.

## 3. Configure your drive

Copy the template once. **Windows PowerShell:**

~~~powershell
Copy-Item .env.example .env
~~~

**Linux, WSL or macOS:**

~~~bash
cp .env.example .env
chmod 600 .env
~~~

Open `.env` in a text editor and fill in:

| Setting | Value |
| --- | --- |
| `MONGODB_URI` | Your private Atlas/replica-set connection URI |
| `API_ID` / `API_HASH` | From Telegram's API page |
| `MAIN_BOT_TOKEN` | Your bot's token |
| `TELEGRAM_ADMIN_IDS` | Your numeric Telegram user ID, not `@username`; comma-separate multiple users. [Find your ID](#find-your-telegram-user-id) |
| `STORAGE_CHANNEL` | Your channel's negative numeric ID |
| `ADMIN_PASSWORD` | A unique web password: 12 characters minimum, 15+ recommended |

Keep `.env` private. Remove placeholder angle brackets, single-quote values
containing `$`, and do not shell-source the file. Leave `SESSION_SECRET` and
`MONGODB_BACKUP_KEY` unset for a new installation: TGDrive generates and preserves
them. **Never replace an existing drive's keys.**
[Optional settings and hashed passwords](reference.md#configuration).

<a name="find-your-telegram-user-id"></a>

<details>
<summary>Find your numeric Telegram user ID</summary>

Before first startup:

1. Send `/start` privately to your new bot; it does not need to reply.
2. Open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`, replacing the
   placeholder with your token.
3. Find your message in `result` and copy **`message.from.id`** into
   `TELEGRAM_ADMIN_IDS`. Use the sender's ID, not the bot/channel ID.

Keep this token-bearing URL and its response private. If `result` is empty,
send a fresh message and refresh; use a bot not already running elsewhere.
For multiple allowed users, repeat with each user and comma-separate their IDs.
Only listed users can send files to this bot.

</details>

## 4. Start locally

Run **one active installation and one worker per drive**. From the configured
project folder, on any supported system:

~~~bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
~~~

The first build can take several minutes. `config --quiet` checks syntax, not
credentials; plain `docker compose config` prints secrets.

TGDrive initializes a new empty database or reopens an existing drive. Database
connection/authentication failures stop startup; correct access and try again.
Existing or incomplete state is preserved for explicit recovery.

Closing the terminal keeps TGDrive running, but Docker must stay on. Local
containers do not restart automatically by default.

| Later action | Command |
| --- | --- |
| Start | `docker compose up -d` |
| Stop | `docker compose stop` |
| Restart | `docker compose restart app` |
| Apply edited `.env` | `docker compose up -d --force-recreate` |

Let Stop finish its graceful shutdown (up to 60 seconds). For automatic restart
after Docker starts, set `TGDRIVE_RESTART_POLICY=unless-stopped` and recreate.

> [!WARNING]
> Keep `.env`, both named volumes and the same Compose project name. Never use
> `docker compose down -v` or volume prune as a repair: they remove keys,
> unfinished uploads and recovery journals. Data lives in Docker volumes,
> not host `data/` and `cache/` folders.

## 5. Open your drive

1. Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in with your web password.
2. Open **Connections → Refresh** and check your bot.
3. Upload and download a small sample; stop/start TGDrive and confirm it still opens.
4. Under **Storage & Backups → Backups & recovery**, choose **Back up now**.
   After confirmation, choose **Download backup** and keep the file outside the
   installation. Choose **Save recovery key** and keep that key separately.
   [Recovery guide](backup-recovery.md#keep-the-mongodb-recovery-kit-outside-the-server).

You're ready to [upload files](usage.md#upload-files-or-folders). Keep originals
until **Completed**; interrupted browser uploads may need them again.
Before large imports, review [upload and disk budgets](configuration.md#upload-size-and-available-disk-space).

## Apply updates to an existing drive

Follow [upgrade and rollback](operations.md#upgrade-and-rollback). Preserve the
database, channel, `.env`, project name and volumes. Use stable `main` or a
published release tag; keep the same Compose files for server updates.

## If setup stops

| Problem | First check |
| --- | --- |
| Docker command fails | Start Docker; check Linux user permissions |
| Database connection fails | URI password, Atlas IP allowlist, cluster availability |
| Bot cannot use the channel | Token, channel ID, private broadcast channel, auto-delete off, bot permissions |
| Existing drive is missing | Database name; **do not initialize a replacement** |
| Port already in use | Set a free `PORT` in `.env`, recreate and open that localhost port |
| Login disappears on reload | Local HTTP needs `COOKIE_SECURE=false`; hosted access needs HTTPS |
| Edited setting has no effect | Recreate; saved dashboard preferences override matching startup defaults |

More help: [Troubleshooting](operations.md#troubleshooting).

## Reinstall or move to another machine

[Reopen the existing drive](operations.md#reopen-after-losing-the-local-installation)
using the same MongoDB database. **Do not initialize it again.**

## Additional bots and Premium accounts

After setup, add bots through **Management → Connections → Add storage bot**.
First add each bot as a channel administrator with **Post Messages** and
**Delete Messages** in the same channel.
Finish pending transfers before changing accounts, save changes and restart.

Premium is optional. Use **Connections → Connect Premium account** to sign in,
or its advanced controls to import an authorized session. All accounts use the
same **2000 MiB** document cap and [automatic splitting](usage.md#files-above-2-gb).
Premium clients are preferred when available; extra accounts do not guarantee
more speed or bypass Telegram cooldowns. A live Premium speedup has not been measured.

## Hosting beyond your machine

Server deployments need HTTPS, persistent storage and the same MongoDB/Telegram
preparation above. For hosting choices, see [Hosting](hosting.md).

### Coolify

[Install self-hosted Coolify](https://coolify.io/docs/start-with-self-hosted)
on your Docker server. Its self-hosted edition is free; infrastructure costs remain.

1. Create a public Git application from `https://github.com/TechShreyash/TGDrive`.
   Select **`main`**, or **`v2.2.0`** to pin the release.
2. Under **General → Build pipeline**, choose **Compose**, **Base Directory** `/`
   and **Docker Compose Location** `/compose.yaml`.
3. Enter the [required variables](#3-configure-your-drive) in **Environment Variables**.
   Choose **Literal** for `$` values. Keep secrets unavailable during build,
   available at runtime. [Coolify environment guide](https://coolify.io/docs/applications/configuration/environment-variables).
4. Point your domain's DNS at the server. Under **Domains**, add an HTTPS domain
   for **`app`**, including its internal port: `https://drive.example.com:8000`.
   Visitors use normal HTTPS.
5. **Deploy**, wait for `app` to become healthy, then complete
   [first login and backup](#5-open-your-drive) at your domain.

Use a domain with a valid HTTPS certificate; the default HTTP `sslip.io` address
does not meet TGDrive's [remote browser requirements](operations.md#can-tgdrive-run-over-http).
Compose configures proxy access and secure cookies. Preserve its **`/app/data`**
and **`/app/cache`** volumes when redeploying.
[Coolify Compose guide](https://coolify.io/docs/applications/builds/docker-compose).

### Docker Compose on a server

Prepare `.env` as above. Name **both files explicitly** to avoid the laptop override:

~~~bash
docker compose -f compose.yaml -f compose.server.yaml config --quiet
docker compose -f compose.yaml -f compose.server.yaml build
docker compose -f compose.yaml -f compose.server.yaml up -d
docker compose -f compose.yaml -f compose.server.yaml ps
~~~

1. Forward HTTPS to host `127.0.0.1:8000`, preserving Host and forwarding the
   scheme/client headers. A containerized proxy needs a shared private network
   with `app:8000`, or its documented host-access route.
2. Set `FORWARDED_ALLOW_IPS` to the proxy source addresses the app actually sees;
   do not use `*`. Follow [proxy setup](operations.md#https-and-private-configuration).
3. At the public domain, complete [first login and backup](#5-open-your-drive).
   Container health alone does not verify DNS, TLS or file access.

This setup keeps secure cookies on, publishes loopback only and restarts unless
stopped. Preserve project name/volumes and use the same `-f` arguments for
`stop`, `restart app` and `up -d --build`.

### Other hosting

Use Docker Compose, persistent storage and one worker per drive. Sleeping hosts,
replaced disks or short upload/request timeouts can interrupt transfers.

## Features and settings after setup

Start with [Using TGDrive](usage.md) for uploads, sharing and bot imports.
[Configuration](configuration.md) covers cache, limits and timezone;
[deployment variables](reference.md#configuration) cover sessions, proxy trust and backups.

## Optional independent channel protection

Under **Storage & Backups → Backups & recovery → Set up backup channel**, add
a separate private channel and backup bot. Wait for **Backup confirmed**, then
save the updated recovery file and its separate key. Follow
[backup-channel setup](backup-recovery.md#set-up-an-independent-backup-channel)
for account separation, permissions and recovery limits. Both channels still
depend on Telegram.
