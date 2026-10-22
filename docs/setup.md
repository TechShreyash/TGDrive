# Set up TGDrive

[Docs home](README.md) · Next: [Using TGDrive](usage.md)

This guide gets a new personal drive running on your machine.
**Docker is the recommended route**: it supplies the app's Python and Node tools.

## Before you start

| You need | What it is for |
| --- | --- |
| Linux, or WSL Ubuntu on Windows | Runs TGDrive and the setup command |
| Docker Engine, Compose and Buildx | Builds and starts the app |
| Python 3.10 or newer on the host | Runs the setup wizard |
| A MongoDB database | Remembers files, folders and app settings |
| A Telegram account and storage bot | Connects TGDrive to your private channel |
| Free disk space | Holds unfinished uploads and the configured disposable media cache |

Run the commands below in a Linux/WSL terminal, **inside the TGDrive project folder**.
You do not need Node or an app Python environment when using Docker.

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

</details>

## 1. Prepare MongoDB

MongoDB Atlas is the simplest supported option. A local MongoDB installation
must use a **replica set**: a server configuration that supports transactions.

1. Create an Atlas cluster.
2. Create a **database user** with read/write access to your TGDrive database.
   This is separate from the account you use to log into the Atlas website.
3. In **Network Access**, allow the public IP of the machine running TGDrive.
4. Open **Connect → Drivers**, copy the connection URI and replace its password placeholder.

Keep that URI private. The wizard uses `tgdrive` as the database name.
Your actual file contents stay in Telegram, rather than taking up database space.

[Atlas setup help](https://www.mongodb.com/docs/atlas/tutorial/deploy-free-tier-cluster/)

## 2. Prepare Telegram

1. Open [BotFather](https://t.me/BotFather), create a bot and keep its **bot token**.
2. Open [Telegram's API page](https://my.telegram.org/apps), create an application
   and keep its **API ID** and **API hash**.
3. Create a dedicated **private channel**.
4. Add the bot as an administrator with permission to **post and delete messages**.
5. Send a message in the channel and copy its private message link.

The wizard accepts that message link, so you do not need another bot to find
the channel's numeric ID.

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

Before importing large files, review temporary-storage and cache limits.
The default upload staging budget is 16 GiB; the media cache defaults to
10 GiB disk and 256 MiB RAM, with free-space and effective-memory safeguards.
Budgets are upper bounds, not space allocated in advance. Leave room for the
operating system, database tools, logs, and encrypted backup staging.
[Performance and cache setup](performance.md) explains how to size them.

## Start or stop it later

Run these from the same project folder:

~~~bash
docker compose up -d
docker compose stop
~~~

Use one running installation per drive. Do not start a second copy against
the same database.

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
| Bot cannot use the channel | Correct bot token, channel and administrator permissions |
| Configuration already exists | Start it with `docker compose up -d` instead |
| Existing drive is missing | Check the database name; do not create a replacement |

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
| `MONGODB_BACKUP_KEY` | A new 64-character hexadecimal key; save it separately |
| `API_ID` / `API_HASH` | From Telegram's API page |
| `BOT_TOKENS` | Your storage bot token |
| `STORAGE_CHANNEL` | The private channel's negative numeric ID |
| `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` | Your web login; use a generated hash when possible |
| `SESSION_SECRET` | An independent random value, at least 32 characters |
| `COOKIE_SECURE` | `false` for local HTTP; `true` for HTTPS |

Generate the backup key locally. Generate the session secret independently:

~~~bash
python3 -c "import secrets; print(secrets.token_hex(32))"
~~~

The printed values are secrets. Put them in `.env` privately.
See [configuration reference](reference.md#configuration) for password hashing,
quoting rules and optional settings.

For a **new, empty database only**:

~~~bash
docker compose build
docker compose run --rm --no-deps app python -m tgdrive init
docker compose up -d
~~~

For an existing drive, skip `init`. Initialization refuses an existing catalog.

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

Fill `.env` using the manual configuration table above.
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

Coolify creates the data/cache volumes. The app generates its session secret
and backup key once, saves them in a private file in `/app/data`, and stores them
in MongoDB when the drive is initialized. Existing drives reuse their saved
keys and owner hash. The container discovers `coolify-proxy` on its Docker
network and trusts only its resolved private IPs; there is no subnet to enter.

Route the `app` service to internal port **8000** with an HTTPS domain. No host
port mapping is needed, so Coolify's own port 8000 cannot conflict with TGDrive.

Use a domain you control, with its DNS record pointing to the deployment server.
Coolify's generated `sslip.io` domains are intended for HTTP previews; shared
certificate rate limits can prevent HTTPS issuance.

Deploy the app. For a **brand-new, empty database only**, use the application's
terminal to run `python -m tgdrive init` once, then restart. An uninitialized
database does not pass the health check until this explicit step is complete.
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

### Other hosting

Start with [HTTPS and private configuration](operations.md#https-and-private-configuration).
The included Render template is a starting point and has not been validated as
a live deployment.

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
