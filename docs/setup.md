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
| Free disk space | Holds files while they are uploading |

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

## Start or stop it later

Run these from the same project folder:

~~~bash
docker compose up -d
docker compose stop
~~~

Use one running installation per drive. Do not start a second copy against
the same database.

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
uv sync --frozen --all-groups
npm ci --prefix website
npm run build --prefix website
cp .env.example .env
chmod 600 .env
~~~

Fill `.env` using the manual configuration table above.
Initialize a **new database only**, then start:

~~~bash
uv run --frozen python -m tgdrive init
COOKIE_SECURE=false uv run --frozen uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --timeout-graceful-shutdown 45
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

See [manage connections](usage.md#settings-and-connections) for the web flow.
For a private local Premium login, use `uv run --frozen python -m tgdrive login`.
Imported sessions must be Telethon sessions; older Pyrogram/Kurigram strings
are incompatible.

## Hosting beyond your machine

Start with [HTTPS and private configuration](operations.md#https-and-private-configuration).
The included Render template is a starting point and has not been validated as
a live deployment.
