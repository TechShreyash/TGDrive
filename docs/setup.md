# Set up TGDrive

[Docs home](README.md) · Next: [Using TGDrive](usage.md)

This guide covers a private laptop installation and a hosted server.
**Docker Compose is recommended for both**: it supplies the app's Python and Node
tools. You do not need to keep TGDrive running all day.

## Choose how you want to use it

| Deployment | Access and availability | Start here |
| --- | --- | --- |
| **Laptop, only when needed** | Only this computer can open the default localhost address. Start TGDrive to use it; stop it when finished. | Follow steps 1–4 below. |
| **Server with Coolify — recommended hosting** | An HTTPS domain lets you and people with share links reach it while the server is running. Coolify manages deployments, TLS and logs. | Prepare MongoDB/Telegram below, then follow [Coolify](#coolify). |
| **Server with your own proxy** | Docker Compose runs the app behind an HTTPS proxy you manage. | Follow [Docker Compose on a server](#docker-compose-on-a-server). |

The laptop route is private by default: no domain, router port forwarding, tunnel
or public Telegram channel is needed. Other devices cannot use its localhost
links. Sharing features require a reachable TGDrive address; Telegram does not
host the TGDrive website. Existing links stop working while TGDrive is stopped,
the laptop sleeps, or its network is unavailable. They can work again when the
same deployment returns, provided the link is still valid and reachable.

Both routes need internet access to Telegram and MongoDB. This is not an offline
drive. A stopped app also cannot finish transfers, run backups or answer the
optional Telegram bot interface. Keep originals until an upload says **Completed**.

## Before you start

| You need | What it is for |
| --- | --- |
| Linux, or WSL Ubuntu on Windows | Runs TGDrive and the setup command |
| A current Docker Engine, Compose and Buildx | Builds and starts the app |
| Python 3.10 or newer on the host | Runs the setup wizard |
| A MongoDB database | Remembers files, folders and app settings |
| A Telegram account and storage bot | Connects TGDrive to your private channel |
| Free disk space | Holds unfinished uploads and the configured disposable media cache |

Run commands in a Linux/WSL terminal as your normal user. Do not run the wizard
with sudo. Install Git if needed, then download TGDrive once:

~~~bash
git clone --branch dev https://github.com/TechShreyash/TGDrive.git
cd TGDrive
~~~

This is the development branch; review the [release status](README.md#current-release-status)
before relying on it for your only copy. Run subsequent commands **inside this same
TGDrive project folder**. You do not need Node or an app Python environment when
using Docker. On Windows, keep the checkout in WSL's Linux filesystem and enable
Docker's WSL integration if using Docker Desktop.

<details>
<summary>Need help with Docker or WSL?</summary>

Use the official [Ubuntu Docker installation guide](https://docs.docker.com/engine/install/ubuntu/).
On Windows, start with [installing WSL](https://learn.microsoft.com/en-us/windows/wsl/install),
then run TGDrive from Ubuntu.

Before starting the wizard, these commands should work in that terminal:

~~~bash
docker info
docker compose version
docker buildx version
python3 --version
~~~

If Docker reports a permission or daemon error, finish its installation before continuing.
Use a maintained Docker release: Docker documents a localhost-publishing issue in
Engine versions older than 28.0.0 in its [port publishing guide](https://docs.docker.com/engine/network/port-publishing/).
Check all wizard prerequisites without changing data:

~~~bash
python3 scripts/setup.py --check
~~~

</details>

## 1. Prepare MongoDB

MongoDB Atlas is the simplest supported option. A local MongoDB installation
must use a **replica set**: a server configuration that supports transactions.
The supplied Compose files run TGDrive, not MongoDB; they do not create a database
for you. A plain standalone MongoDB container is insufficient. Atlas avoids
having to administer your own replica set.

1. Create an Atlas cluster.
2. Create a **database user** with read/write access to your TGDrive database.
   This is separate from the account you use to log into the Atlas website.
3. In **Network Access**, allow the public IP of the machine running TGDrive.
4. Open **Connect → Drivers**, copy the connection URI and replace its password placeholder.
   Encode reserved characters in the URI password using MongoDB's
   [connection-string rules](https://www.mongodb.com/docs/manual/reference/connection-string/).
5. Keep the cluster available. A laptop's public IP may change after switching
   Wi-Fi or a router restart; update Atlas Network Access if that happens. Avoid
   allowing every IP just to work around a connection problem.

Keep that URI private. The wizard uses `tgdrive` as the database name.
Your actual file contents stay in Telegram, rather than taking up database space.

[Atlas setup help](https://www.mongodb.com/docs/atlas/tutorial/deploy-free-tier-cluster/)

## 2. Prepare Telegram

1. Open [BotFather](https://t.me/BotFather), create a bot and keep its **bot token**.
2. Open [Telegram's API page](https://my.telegram.org/apps), create an application
   and keep its **API ID** and **API hash**.
3. Create a dedicated **private broadcast channel**, not a group or a public
   channel. Give it no public username and turn message auto-delete **Off**.
4. Add **every storage bot** as an administrator. For the standard MongoDB drive,
   enable **Post Messages** and **Delete Messages**.
5. Send one identification message in the new channel, then use **Copy Message
   Link**. The wizard accepts a link such as
   `https://t.me/c/1234567890/1` and derives the channel's negative ID locally;
   paste your own link, not this example.

TGDrive reads documents to stream/download them, posts file parts and encrypted
database backups, and deletes incomplete or abandoned upload messages during
cleanup. MongoDB backups do not pin channel messages. **Edit Messages** is only
needed for legacy SQLite checkpoints and the optional independent backup channel,
which maintain recovery pins. Telegram defines these rights in
[chatAdminRights](https://core.telegram.org/constructor/chatAdminRights).
TGDrive does not need permission to add administrators, ban users, invite members,
manage calls, change channel information or post stories. Leave unrelated rights
off. Reading existing file documents requires channel access; it is not a
separate “Read Messages” administrator switch.

After setup, treat this channel as app-managed storage:

- Keep it private, keep auto-delete off, and keep every enabled bot as an admin
  with its required permissions. Premium upload accounts also need channel access
  and permission to post.
- Make file changes inside TGDrive. Do not edit, replace or delete its Telegram
  messages, clear channel history, change its recovery pins, or add unrelated posts.
  Do not enable bots that moderate or delete channel messages.
- Keep account/channel ownership and bot tokens secure. Revoking a token, removing
  an account, changing permissions or deleting the channel can interrupt access.

Setup and **Connections → Refresh** check channel access and permissions.
TGDrive cannot prevent an owner or another Telegram administrator from changing
the channel. It reports detected missing/changed documents when accessed; it
cannot recover deleted file bytes from a database backup alone. See
[channel damage](operations.md#if-someone-changed-the-telegram-channel).

## 3. Run the wizard

~~~bash
python3 scripts/setup.py
~~~

Have your MongoDB URI, bot token, API ID/hash and channel link ready.
Choose a web password of at least 12 characters. Secret prompts hide what you
type; the blank-looking input is normal.

The wizard will:

- Ask for credentials privately and let you choose a web password.
- Build TGDrive and create its private `.env` configuration.
- Check database and Telegram access.
- Ask you to type `NEW` before creating a new drive, or reuse an existing one.
- Start the app and print its local address.

The first build can take several minutes. An existing `.env` is preserved;
the wizard will not overwrite it.

## 4. Open your drive

1. Open the printed address, normally [http://127.0.0.1:8000](http://127.0.0.1:8000).
2. Sign in with the web password you chose.
3. Open **Connections** and check your bot.
4. Open **Storage & Backups → Save recovery key** and store the downloaded key
   somewhere separate from this installation.

You are ready to [upload your first file](usage.md#upload-files-or-folders).

Upload and download one small test file, stop TGDrive, start it again, and check
that the file still opens. If you use backups, choose **Back up now** and wait
for a confirmed database archive. Save its matching recovery key separately.
This checks the real credentials and persistent storage before a large import.

Before importing large files, review temporary-storage and cache limits.
The default upload staging budget is 16 GiB; the media cache defaults to
10 GiB disk and 256 MiB RAM, with free-space and effective-memory safeguards.
Budgets are upper bounds, not space allocated in advance. Leave room for the
operating system, database tools, logs, and encrypted backup staging.
[Performance and cache setup](performance.md) explains how to size them.

## Start or stop it later

Run these from the same project folder:

| Action | Command |
| --- | --- |
| Start for this session | `docker compose up -d` |
| See whether it is ready | `docker compose ps` |
| Finish using it | `docker compose stop` |
| Restart after an interruption | `docker compose restart app` |
| Apply edited .env values | `docker compose up -d --force-recreate` |

The local override disables automatic container restart by default, so you choose
when TGDrive runs. Closing the terminal does not stop detached containers; use
the Stop command. Stop before shutting down when possible; an unexpected power
loss uses the persisted recovery state, but may need the original upload file.
See [what recovers after interruption](operations.md#after-a-shutdown-crash-or-network-loss).
Docker gives the app up to 60 seconds to stop gracefully. Let the Stop command
finish; forcing the container off can interrupt more work. The
[Compose stop command](https://docs.docker.com/reference/cli/docker/compose/stop/)
keeps the container and its volumes for the next session.

Keep `.env` and both named Docker volumes. `docker compose stop` retains them.
Do not use `docker compose down -v` or Docker volume prune as a repair step:
removing volumes loses local deployment secrets, unfinished bytes and recovery
journals. Keep the same project folder/project name so Compose reuses the same
volumes. Use one running installation per drive; do not start another against the
same database.

For a laptop you intentionally want running after Docker restarts, set
`TGDRIVE_RESTART_POLICY=unless-stopped` in `.env` and recreate the container.
For normal on-demand use, leave it unset. Docker explains restart behavior in its
[restart policy guide](https://docs.docker.com/engine/containers/start-containers-automatically/).

## Apply updates to an existing drive

The streaming fixes ship with the application. They do not require a benchmark
script, a replacement player, new required environment values, or manual cache
conversion. The Docker build includes the custom player and its local decoder
assets; browsers do not need an extension or a separately installed decoder.

- **Coolify:** redeploy the updated revision using the application's existing
  Compose file. Keep its environment values, database, and persistent volumes.
  Docker builds both the frontend and backend automatically.
- **Local Docker:** obtain the updated source in the same project folder, then
  run `docker compose up -d --build`.
- **Source installation:** after updating the checkout, run the commands below,
  then restart the existing single-worker server using its normal service manager.

~~~bash
uv sync --frozen --no-dev
npm ci --prefix website
npm run build --prefix website
~~~

Keep the existing `.env`; skip the setup wizard and `init`. Saved dashboard
settings continue to apply. Cache blocks on the persistent disk volume remain
usable; RAM-only cache repopulates after a restart. A deployment interrupts active
playback, so reopen the player afterward. Refresh the browser to load the new UI.

The app sends its file-response policy directly. Both included Compose files
also supply the proxy response policy automatically; Coolify adds it to generated
domain routes. No manual labels or proxy edits are needed. If another proxy/CDN
overwrites those headers, see [HTTP delivery](operations.md#http-delivery-and-proxy-configuration).

## If setup stops

| Problem | What to check |
| --- | --- |
| Docker command fails | Docker is running and your WSL/Linux user can use it |
| Database connection fails | URI password, Atlas Network Access and cluster availability |
| Bot cannot use the channel | Correct bot token, private broadcast channel, auto-delete off and every bot's administrator permissions |
| Configuration already exists | Start it with `docker compose up -d` instead |
| Existing drive is missing | Check the database name; do not create a replacement |
| Address already in use | Set a free `PORT`, such as `8001`, in private `.env`, then recreate the local container and open that localhost port |
| Login works only until the page reloads | Local HTTP needs `COOKIE_SECURE=false`; recreate after editing it. Hosted access needs HTTPS with secure cookies |
| A setting edit has no effect | Recreate the container; saved dashboard preferences override the matching startup defaults |

More help: [Troubleshooting](operations.md#troubleshooting).

## Other installation options

<details>
<summary>Manual Docker setup — configure .env yourself</summary>

Copy the template and restrict access:

~~~bash
cp .env.example .env
chmod 600 .env
~~~

Fill the required values:

| Setting | Value |
| --- | --- |
| `MONGODB_URI` | Your private MongoDB URI |
| `MONGODB_DATABASE` | `tgdrive`, or another dedicated database |
| `MONGODB_BACKUP_KEY` | Optional with standard Compose auto-secrets; otherwise a new 64-character hexadecimal key. Always save the exported recovery key separately. |
| `API_ID` / `API_HASH` | From Telegram's API page |
| `BOT_TOKENS` | Your storage bot token |
| `STORAGE_CHANNEL` | The private channel's negative numeric ID |
| `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` | Your web login; use a generated hash when possible |
| `SESSION_SECRET` | Optional with standard Compose auto-secrets; otherwise an independent random value, at least 32 characters |
| `COOKIE_SECURE` | `false` for local HTTP; `true` for HTTPS |

Standard Compose generates missing session and backup keys once in the persistent
data volume. Leave those sample values blank to use that behavior. For explicit
keys or a source installation, run this command separately for each key:

~~~bash
python3 -c "import secrets; print(secrets.token_hex(32))"
~~~

The printed values are secrets. Put them in `.env` privately.
See [configuration reference](reference.md#configuration) for password hashing,
quoting rules and optional settings.

Check Compose syntax without printing resolved secrets:

~~~bash
docker compose config --quiet
~~~

This checks YAML and required interpolation, not live credentials or channel
access. Do not paste the output of plain `docker compose config` into support
chats: it includes resolved secrets. Use the [reference](reference.md#configuration)
for exact names; a typo in a variable name is not a feature switch.

For a **new, empty database only**:

~~~bash
docker compose build
docker compose run --rm --no-deps app python -m tgdrive init
docker compose up -d
~~~

For an existing drive, skip `init`. Initialization refuses an existing catalog.

Use **Connections → Refresh** and a small upload/download to verify actual
credentials after startup. To repeat setup's read-only access checks, stop the
app first and run:

~~~bash
docker compose run --rm --no-deps app python -m tgdrive.setup inspect
~~~

This checks the database binding and enabled storage bots' channel state without
creating a catalog, uploading a document or editing a recovery pin. It needs
network access and can briefly acquire the database's ownership lock. Start the
app afterward with `docker compose up -d`. For server Compose include its usual
`-f` arguments; in Coolify use Connections and runtime logs instead.

</details>

### Source installation

<details>
<summary>Build and run without Docker</summary>

Use supported Python 3.11–3.14, [uv](https://docs.astral.sh/uv/), and Node 24 LTS.
Exact tool versions: [Development](development.md#version-baseline-and-dependency-policy).

~~~bash
uv sync --frozen --no-dev
npm ci --prefix website
npm run build --prefix website
cp .env.example .env
chmod 600 .env
~~~

Fill `.env` using the manual configuration table above. Source installations
need explicit `SESSION_SECRET` and `MONGODB_BACKUP_KEY` values unless you
deliberately enable auto-secrets. Docker's localhost and restart defaults do not
configure a source process.
Initialize a **new database only**, then start:

~~~bash
uv run --frozen --no-dev python -m tgdrive init
COOKIE_SECURE=false uv run --frozen --no-dev uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --timeout-graceful-shutdown 45
~~~

For an existing database, skip `init`.
After frontend changes, rebuild the UI. After backend changes, restart the server.

</details>

## Reinstall or move to another machine

Follow [reopen your existing drive](operations.md#reopen-after-losing-the-local-installation).
Use the same MongoDB database; **do not initialize it again**.

## Additional bots and Premium accounts

After setup, use **Connections** to add bots or connect a Premium account.
All storage accounts need access to the same channel.

- **Refresh** checks saved credentials, channel permissions and Telegram names.
- Saved connection changes apply after an app restart.
- Premium is optional; bots already support larger files through splitting.
- More accounts can add transfer capacity, but cannot guarantee faster Telegram
  routing or remove provider cooldowns. Start with conservative live limits.

See [manage connections](usage.md#settings-and-connections) for the web flow.
For a private local Premium login, use `uv run --frozen python -m tgdrive login`.
Imported sessions must be Telethon sessions; older Pyrogram/Kurigram strings
are incompatible.

## Hosting beyond your machine

### Coolify

**Coolify is the recommended server deployment route.** It makes image builds,
HTTPS domains, environment management, logs and persistent mounts easier to
manage. You still need a server with Docker, a domain for HTTPS, a reachable
MongoDB replica set, and the Telegram setup above. Follow Coolify's
[installation guide](https://coolify.io/docs/get-started/installation) if it is
not installed. Keep one TGDrive replica; disable preview/parallel deployments
that would use the production database and channel.

Create a public Git application from `https://github.com/TechShreyash/TGDrive`,
branch `dev`, using **Docker Compose**, base directory `/`, and Compose file
`/compose.yaml`. Fill these six fields for a new drive:

| Variable | Value |
| --- | --- |
| `MONGODB_URI` | Your private Atlas/replica-set connection URI |
| `API_ID` | Telegram application ID |
| `API_HASH` | Telegram application hash |
| `BOT_TOKENS` | Your storage bot token |
| `STORAGE_CHANNEL` | The private channel's negative numeric ID |
| `ADMIN_PASSWORD` | Owner password, at least 12 characters |

Keep credentials runtime-only. If a value contains `$`, choose **Literal** in
Coolify's variable editor. You do not need to generate a password hash for this
setup. Additional limits and optional bot-mode/Premium settings can be added as
runtime variables later; defaults are already supplied by the application.
In the current editor, set **Build time → Not available during build** and
**Runtime → Available in the container** for secrets. Save the variables before
deploying. [Coolify's environment guide](https://coolify.io/docs/applications/configuration/environment-variables)
explains these controls.

Coolify creates the data/cache volumes. The app generates its session secret
and backup key once, saves them in a private file in `/app/data`, and stores them
in MongoDB when the drive is initialized. Existing drives reuse their saved
keys and owner hash. The container discovers `coolify-proxy` on its Docker
network and trusts only its resolved private IPs; there is no subnet to enter.

Route the `app` service to internal port **8000** with an HTTPS domain; in
Coolify's domain field this may look like `https://drive.example.com:8000`.
The suffix identifies the container port: visitors still use normal HTTPS.
No host port mapping is needed, so Coolify's own port 8000 cannot conflict with
TGDrive. See the official [Compose deployment guide](https://coolify.io/docs/applications/builds/docker-compose).

Use a domain you control, with its DNS record pointing to the deployment server.
Coolify's generated `sslip.io` domains are intended for HTTP previews; shared
certificate rate limits can prevent HTTPS issuance.

Keep `COOKIE_SECURE=true` for hosted HTTPS. Do not add `.env` as a public source
file or expose the database/app port publicly. Review the two persistent mounts,
`/app/data` and `/app/cache`, before the first deploy; keep them on all redeploys.
The data mount contains keys and recovery journals; the cache mount includes
unfinished uploads as well as disposable bytes. Coolify documents
[Compose-owned persistent storage](https://coolify.io/docs/core/persistent-storage/storage-mounts/overview)
and [runtime secret variables](https://coolify.io/docs/applications/configuration/environment-variables).

Deploy the app. For a **brand-new, empty database only**, use the application's
terminal to run `python -m tgdrive init` once, then restart. An uninitialized
database does not pass the health check until this explicit step is complete.
Before initialization, the container deliberately waits so that terminal access
remains available; the website is not ready yet. If this is an existing drive,
check the database URI/name instead of initializing a replacement.
For an existing drive, skip initialization and sign in with its original owner
password. Download **Storage & Backups → Save recovery key** and keep it separate.

The standard local `docker compose up` command still works: Docker automatically
loads `compose.override.yaml`, which reads your private `.env` and publishes the
local loopback port. Coolify selects `compose.yaml` explicitly and does not load
that local override.

#### Existing deployments and server moves

- `/compose.coolify.yaml` remains available for existing applications. Keep
  their environment values when upgrading; there is no required live migration.
- Stop the old app before starting the same drive on another server. Keep the
  same database, channel and original keys/password; never run `init` again.
- Copy both persistent volumes if unfinished uploads must continue. Completed
  files and the catalog remain in Telegram and MongoDB respectively.
- Allow the new server in Atlas Network Access and update the domain's DNS.
- When using proxy discovery, remove a legacy `FORWARDED_ALLOW_IPS` override,
  or set it to `auto`. Explicit overrides remain supported. Restart the app if
  the proxy is recreated with a different IP.
- Existing `ADMIN_PASSWORD_HASH` values remain supported. In Coolify 4.4.6,
  managed Compose requires **Literal** and doubled `$$` characters to preserve
  the original hash. Keep that escaping in Coolify only. Existing drives can
  instead reuse the owner hash already saved in their MongoDB bootstrap settings;
  removing an environment override does not change that saved password.

### Docker Compose on a server

Use this route when you already manage an HTTPS reverse proxy. Coolify handles
these steps for you through the route above.

1. Prepare MongoDB/Telegram, clone TGDrive, copy `.env.example` to private `.env`,
   and fill the required values from [manual Docker setup](#other-installation-options).
   Use `COOKIE_SECURE=true`. Leave `BIND_ADDRESS=127.0.0.1`.
2. Run commands with **both files explicitly named**, so Compose does not load
   the laptop override:

~~~bash
docker compose -f compose.yaml -f compose.server.yaml config --quiet
docker compose -f compose.yaml -f compose.server.yaml build
~~~

3. For a **new empty database only**, initialize once:

~~~bash
docker compose -f compose.yaml -f compose.server.yaml run --rm --no-deps app python -m tgdrive init
~~~

4. Start the server:

~~~bash
docker compose -f compose.yaml -f compose.server.yaml up -d
docker compose -f compose.yaml -f compose.server.yaml ps
~~~

5. Configure your HTTPS proxy to forward your domain to the host's
   `127.0.0.1:8000`, preserving Host and correctly setting forwarded scheme/client
   headers. If the proxy runs in another container, its own `127.0.0.1` is not
   the Docker host: use a shared private Docker network and the `app:8000`
   service address, or your proxy's documented host-access route.
6. Configure `FORWARDED_ALLOW_IPS` for only the proxy source addresses the app
   actually sees. A Docker bridge can change that source address. Never use `*`
   to work around a trust problem. Keep direct app access restricted and follow
   [HTTPS/proxy checks](operations.md#https-and-private-configuration).
7. Open the HTTPS domain, sign in, check Connections, upload/download one small
   test file, and save the recovery key outside the server. Confirm a database
   backup before relying on the installation.

The server file keeps secure cookies on and publishes only a loopback port.
It uses Docker's `unless-stopped` restart policy: an intentionally stopped app
stays stopped; it is suitable for an available server rather than on-demand
laptop use. Include the same `-f` arguments when stopping, restarting, changing
configuration or upgrading this deployment:

~~~bash
docker compose -f compose.yaml -f compose.server.yaml stop
docker compose -f compose.yaml -f compose.server.yaml restart app
docker compose -f compose.yaml -f compose.server.yaml up -d --build
~~~

Keep the same Compose project name and persistent volumes. A healthy container
does not prove that your public DNS, certificate, proxy or Telegram files are
healthy; check the domain and a representative download after redeploying.
See [upgrade and rollback](operations.md#upgrade-and-rollback) before updates.

### Other hosting

Use Docker/Compose with persistent storage and one worker per drive. Platforms
that sleep, replace local disks or impose short request/upload limits can break
long transfers unless configured appropriately. The included Render template is
a starting point and has not been validated as a live deployment.

## Optional features and settings

The basic drive needs no extra service besides Telegram and a MongoDB replica
set. Configure optional features after the first upload/download works:

| Feature | Where to configure it | Requirement |
| --- | --- | --- |
| More storage bots | **Management → Connections** | Every enabled bot must remain an administrator in the same channel. Restart after saved connection changes. |
| Premium transfer account | **Connections → Connect Premium account** | Optional authorized Telethon session and channel posting access; not required for large logical files. |
| Telegram bot uploads | `MAIN_BOT_TOKEN` and `TELEGRAM_ADMIN_IDS`; then **Bot uploads** on the website | Numeric allowed-user IDs and a private chat with the bot. `PUBLIC_URL` optionally supplies its website link; it does not expose a local server. [Bot interface](usage.md#settings-and-connections). |
| Shares | **Shared Links** | A reachable HTTPS website for recipients; the laptop's localhost address cannot serve remote recipients. |
| Independent channel copy | **Storage & Backups** | A separate private channel, dedicated backup bot, matching recovery key and a confirmed copy. [Setup below](#optional-independent-channel-protection). |
| Cache, preload, upload size and concurrency | **Configuration → Cache settings / Transfer limits** | Enough real RAM/disk; saved preferences override startup defaults. [Configuration guide](configuration.md). |
| Display timezone | **Configuration → Timezone** | Valid IANA zone; default Asia/Kolkata. Stored timestamps stay UTC. |
| URL import policy, backup interval, session lifetime and proxy trust | Private deployment environment | Apply by recreating/redeploying; use the [complete variable reference](reference.md#configuration). |

Keep these standard safeguards: loopback/private app ingress, explicit new-drive
initialization, persistent mounts, HTTPS secure cookies when hosted, and one
running app/worker per database. Unsupported settings or provider-side damage
cannot be made safe by increasing concurrency or repeatedly restarting.

## Optional independent channel protection

After the main drive works, you can add a second private channel from a separate
Telegram account under **Storage & Backups → Independent backup channel**.
Create the account/channel and backup bot in Telegram, then add that bot as an
administrator in both channels. TGDrive verifies privacy/access and starts the
copy through the web UI. Save the recovery-key JSON outside the provider and wait
for a confirmed restore point before relying on the second channel.

Follow the complete [backup setup and recovery guide](backup-recovery.md#set-up-an-independent-backup-channel)
for permissions, interruption recovery, restoring either channel, database-only
recovery and the limits of two copies on the same provider. This option is not
required for normal setup and does not replace originals outside Telegram.
